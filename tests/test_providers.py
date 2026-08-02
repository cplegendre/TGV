import json
from unittest.mock import Mock, patch

import httpx
import pytest

from tgv.exceptions import ConfigurationError, ModelProviderError
from tgv.models import ModelRoleConfig, Route, RoutingConfig
from tgv.providers import OllamaProvider, OpenAICompatibleProvider, create_local_provider
from tgv.router import Router


def config(**overrides: object) -> ModelRoleConfig:
    values: dict[str, object] = {
        "provider": "ollama",
        "model": "test-model",
        "base_url": "http://ollama.test",
        "timeout_seconds": 1,
        "maximum_retries": 1,
        "retry_backoff_seconds": 0,
    }
    values.update(overrides)
    return ModelRoleConfig.model_validate(values)


def test_provider_rejects_missing_url() -> None:
    with pytest.raises(ConfigurationError, match="base_url"):
        OllamaProvider(config(base_url=None))


def test_provider_retries_and_raises_when_unreachable() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline", request=request)

    provider = OllamaProvider(config(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ModelProviderError, match="after retries"):
        provider.route("Where is auth?")
    assert calls == 2


def test_router_falls_back_when_provider_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    provider = OllamaProvider(config(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    decision = Router(RoutingConfig(), provider).route("Where is auth?")
    assert decision.route is Route.LOCAL_FAST
    assert decision.confidence == 0.78


def test_router_falls_back_on_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "not-json"}})

    provider = OllamaProvider(config(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    decision = Router(RoutingConfig(), provider).route("Fix the parser")
    assert decision.route is Route.LOCAL_CODE


def test_provider_accepts_valid_structured_response() -> None:
    content = json.dumps(
        {
            "route": "local_fast",
            "confidence": 0.95,
            "rationale": "Direct lookup",
            "risk_signals": [],
            "search_terms": ["auth"],
            "suggested_validation": [],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": content}})

    provider = OllamaProvider(config(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert provider.route("Where is auth?").confidence == 0.95


def test_provider_closes_only_owned_client() -> None:
    owned = Mock()
    with patch("tgv.providers.httpx.Client", return_value=owned):
        provider = OllamaProvider(config())
        provider.close()
    owned.close.assert_called_once()

    external = Mock()
    provider = OllamaProvider(config(), client=external)
    provider.close()
    external.close.assert_not_called()


def openai_config(**overrides: object) -> ModelRoleConfig:
    values: dict[str, object] = {
        "provider": "openai_compatible",
        "model": "test-model",
        "base_url": "http://gateway.test",
        "api_key": "secret",
        "timeout_seconds": 1,
        "maximum_retries": 0,
    }
    values.update(overrides)
    return ModelRoleConfig.model_validate(values)


def test_openai_compatible_success_and_auth() -> None:
    content = json.dumps(
        {
            "route": "local_code",
            "confidence": 0.9,
            "rationale": "Bounded change",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer secret"
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(openai_config(), client=client)
    assert provider.route("Fix parser").route is Route.LOCAL_CODE


def test_openai_compatible_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    provider = OpenAICompatibleProvider(
        openai_config(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ModelProviderError, match="OpenAI-compatible"):
        provider.route("Fix parser")


def test_openai_compatible_non_json_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    provider = OpenAICompatibleProvider(
        openai_config(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(ModelProviderError, match="OpenAI-compatible"):
        provider.route("Fix parser")


def test_openai_compatible_availability_and_factory() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(openai_config(api_key=None), client=client)
    assert provider.available() is True
    assert isinstance(create_local_provider(openai_config()), OpenAICompatibleProvider)
    assert create_local_provider(None) is None
    assert create_local_provider(ModelRoleConfig(provider="external", model="auto")) is None
    with pytest.raises(ConfigurationError, match="Unsupported"):
        create_local_provider(ModelRoleConfig(provider="unknown", model="x"))
    with pytest.raises(ConfigurationError, match="base_url"):
        OpenAICompatibleProvider(openai_config(base_url=None))
    with pytest.raises(ConfigurationError, match="Expected"):
        OpenAICompatibleProvider(config())


def test_openai_compatible_unavailable_and_owned_close() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    provider = OpenAICompatibleProvider(
        openai_config(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert provider.available() is False
    provider.close()

    owned = Mock()
    with patch("tgv.providers.httpx.Client", return_value=owned):
        created = OpenAICompatibleProvider(openai_config())
        with created:
            pass
    owned.close.assert_called_once()


def test_openai_compatible_retries_and_rejects_non_string() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": 4}}]})

    provider = OpenAICompatibleProvider(
        openai_config(maximum_retries=1, retry_backoff_seconds=0),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ModelProviderError):
        provider.route("Fix parser")
    assert calls == 2
