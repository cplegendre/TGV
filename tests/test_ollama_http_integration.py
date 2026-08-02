from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tgv.exceptions import ModelProviderError
from tgv.models import ModelRoleConfig
from tgv.providers import OllamaProvider


class OllamaHandler(BaseHTTPRequestHandler):
    attempts = 0
    delay = 0.0

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"models": []}')

    def do_POST(self) -> None:
        type(self).attempts += 1
        if type(self).delay:
            time.sleep(type(self).delay)
        length = int(self.headers.get("Content-Length", "0"))
        json.loads(self.rfile.read(length))
        content = json.dumps({"route": "local_fast", "confidence": 0.91, "rationale": "mock"})
        body = json.dumps({"message": {"content": content}}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass


def config(base_url: str, timeout: float, retries: int) -> ModelRoleConfig:
    return ModelRoleConfig(
        provider="ollama",
        model="mock",
        base_url=base_url,
        timeout_seconds=timeout,
        maximum_retries=retries,
        retry_backoff_seconds=0,
    )


def test_real_http_adapter_success_timeout_and_retry() -> None:
    server = ThreadingHTTPServer(("127.0.1", 0), OllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.1:{server.server_port}"
    try:
        OllamaHandler.attempts = 0
        OllamaHandler.delay = 0
        with OllamaProvider(config(base_url, 1, 1)) as provider:
            assert provider.available()
            assert provider.route("Where?").confidence == 0.91
        assert OllamaHandler.attempts == 1

        OllamaHandler.attempts = 0
        OllamaHandler.delay = 0.08
        with (
            OllamaProvider(config(base_url, 0.02, 1)) as provider,
            pytest.raises(ModelProviderError, match="after retries"),
        ):
            provider.route("Where?")
        assert OllamaHandler.attempts == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
