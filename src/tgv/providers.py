from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from tgv.exceptions import ConfigurationError, ModelProviderError
from tgv.models import HandoffMode, ModelRoleConfig, ProviderLifecycle, RouteDecision


class LocalModelProvider(Protocol):
    def route(self, request: str) -> RouteDecision:
        """Return a validated route decision."""


def _route_prompt(request: str) -> str:
    return (
        "Classify this software-engineering request for TGV. "
        "Use local_fast for extractive/simple questions, local_code for bounded low-risk "
        "coding, frontier_standard for clear multi-file or substantial coding work, and "
        "frontier_deep for security, architecture, ambiguity, concurrency, broad migrations, "
        "or expert work. Return only JSON matching the supplied schema. "
        "Preserve uncertainty.\n\n"
        f"Request:\n{request}"
    )


class OllamaProvider:
    def __init__(
        self,
        config: ModelRoleConfig,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.provider != "ollama":
            raise ConfigurationError(f"Expected ollama provider, received {config.provider!r}")
        if config.base_url is None:
            raise ConfigurationError("Ollama provider requires a non-empty base_url")
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client()
        self.sleep = sleep

    def close(self) -> None:
        """Close the HTTP client only when this provider created it."""
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> OllamaProvider:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def available(self) -> bool:
        try:
            response = self.client.get(
                f"{self.base_url}/api/tags",
                timeout=min(self.config.timeout_seconds, 5.0),
            )
            return response.is_success
        except httpx.HTTPError:
            return False

    def route(self, request: str) -> RouteDecision:
        payload = self._route_payload(request)
        last_error: Exception | None = None
        for attempt in range(self.config.maximum_retries + 1):
            try:
                response = self.client.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                    timeout=self.config.timeout_seconds,
                )
                response.raise_for_status()
                body = response.json()
                content = body["message"]["content"]
                if not isinstance(content, str):
                    raise ValueError("Ollama response message.content must be a string")
                return RouteDecision.model_validate_json(content)
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt < self.config.maximum_retries:
                    self.sleep(self.config.retry_backoff_seconds * (2**attempt))
        raise ModelProviderError(
            f"Ollama routing failed after retries: {last_error}"
        ) from last_error

    def _route_payload(self, request: str) -> dict[str, object]:
        schema = RouteDecision.model_json_schema()
        prompt = _route_prompt(request)
        return {
            "model": self.config.model,
            "stream": False,
            "format": schema,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": self.config.temperature},
        }


class OpenAICompatibleProvider:
    """Route through any OpenAI-compatible ``/v1/chat/completions`` endpoint."""

    def __init__(
        self,
        config: ModelRoleConfig,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.provider != "openai_compatible":
            raise ConfigurationError(
                f"Expected openai_compatible provider, received {config.provider!r}"
            )
        if config.base_url is None:
            raise ConfigurationError("OpenAI-compatible provider requires a non-empty base_url")
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client()
        self.sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> OpenAICompatibleProvider:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}

    def available(self) -> bool:
        try:
            response = self.client.get(
                f"{self.base_url}/v1/models",
                headers=self._headers(),
                timeout=min(self.config.timeout_seconds, 5.0),
            )
            return response.is_success
        except httpx.HTTPError:
            return False

    def route(self, request: str) -> RouteDecision:
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": _route_prompt(request)}],
            "temperature": self.config.temperature,
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(self.config.maximum_retries + 1):
            try:
                response = self.client.post(
                    f"{self.base_url}/v1/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=self.config.timeout_seconds,
                )
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise ValueError("OpenAI-compatible response content must be a string")
                return RouteDecision.model_validate_json(content)
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt < self.config.maximum_retries:
                    self.sleep(self.config.retry_backoff_seconds * (2**attempt))
        raise ModelProviderError(
            f"OpenAI-compatible routing failed after retries: {last_error}"
        ) from last_error


def create_local_provider(
    config: ModelRoleConfig | None,
) -> OllamaProvider | OpenAICompatibleProvider | None:
    if config is None or config.provider == "external":
        return None
    if config.provider == "ollama":
        return OllamaProvider(config)
    if config.provider == "openai_compatible":
        return OpenAICompatibleProvider(config)
    raise ConfigurationError(f"Unsupported local model provider: {config.provider!r}")


class HandoffProvider(Protocol):
    def invoke(self, markdown_path: str, json_path: str, *, dry_run: bool = False) -> list[str]:
        """Invoke or describe an external coding-agent handoff."""


class CommandHandoffProvider:
    """Safe subprocess handoff using argument arrays and bounded diagnostics."""

    def __init__(
        self,
        command: list[str],
        timeout_seconds: float = 300.0,
        *,
        provider_name: str = "custom",
        result_path: Path | None = None,
    ) -> None:
        if not command:
            raise ConfigurationError("External handoff command must not be empty")
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.provider_name = provider_name
        self.result_path = result_path

    def build_command(self, markdown_path: str, json_path: str) -> list[str]:
        md = str(Path(markdown_path).resolve())
        js = str(Path(json_path).resolve())
        result = [
            part.replace("{handoff_markdown}", md).replace("{bundle_json}", js)
            for part in self.command
        ]
        if result == self.command:
            result = [*result, md, js]
        return result

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _bounded(value: str, limit: int = 2000) -> str:
        return value.strip()[:limit]

    def _ingest_result(self, metadata: ProviderLifecycle) -> ProviderLifecycle:
        if self.result_path is None:
            return metadata
        path = self.result_path.resolve()
        if not path.is_file():
            return metadata
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return metadata.model_copy(update={"result_path": str(path)})
        return metadata.model_copy(
            update={
                "result_path": str(path),
                "result_summary": raw.get("summary")
                if isinstance(raw.get("summary"), str)
                else None,
                "changed_files": raw.get("changed_files", [])
                if isinstance(raw.get("changed_files"), list)
                else [],
                "suggested_next_steps": raw.get("suggested_next_steps", [])
                if isinstance(raw.get("suggested_next_steps"), list)
                else [],
                "command_status": raw.get("command_status", metadata.command_status)
                if isinstance(raw.get("command_status"), str)
                else metadata.command_status,
            }
        )

    def invoke_with_metadata(
        self,
        markdown_path: str,
        json_path: str,
        *,
        dry_run: bool = False,
    ) -> ProviderLifecycle:
        import subprocess

        from tgv.exceptions import CommandExecutionError, ProviderUnavailableError

        command = self.build_command(markdown_path, json_path)
        started = self._timestamp()
        metadata = ProviderLifecycle(
            provider_name=self.provider_name,
            mode=HandoffMode.COMMAND,
            command=command,
            dry_run=dry_run,
            started_at=started,
            command_status="dry-run" if dry_run else "running",
        )
        if dry_run:
            return metadata.model_copy(
                update={"completed_at": self._timestamp(), "command_status": "dry-run"}
            )
        try:
            completed = subprocess.run(
                command,
                check=False,
                timeout=self.timeout_seconds,
                text=True,
                capture_output=True,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise ProviderUnavailableError(f"Handoff command is unavailable: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandExecutionError(
                f"Handoff command timed out after {self.timeout_seconds:g} seconds"
            ) from exc
        metadata = metadata.model_copy(
            update={
                "completed_at": self._timestamp(),
                "exit_code": completed.returncode,
                "stdout_excerpt": self._bounded(completed.stdout),
                "stderr_excerpt": self._bounded(completed.stderr),
                "command_status": "succeeded" if completed.returncode == 0 else "failed",
            }
        )
        if completed.returncode != 0:
            detail = metadata.stderr_excerpt or metadata.stdout_excerpt or "no diagnostic output"
            raise CommandExecutionError(
                f"Handoff command failed with exit code {completed.returncode}: {detail[:500]}"
            )
        return self._ingest_result(metadata)

    def invoke(self, markdown_path: str, json_path: str, *, dry_run: bool = False) -> list[str]:
        """Compatibility API returning the executed or proposed argument array."""
        return self.invoke_with_metadata(markdown_path, json_path, dry_run=dry_run).command
