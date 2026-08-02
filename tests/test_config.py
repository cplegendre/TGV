from pathlib import Path

import pytest

from tgv.config import default_config, load_config
from tgv.exceptions import ConfigurationError


def test_default_config_has_model_roles() -> None:
    config = default_config()
    assert set(config.models) == {"fast", "code", "frontier_standard", "frontier_deep"}


def test_invalid_config_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("models: [broken", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_empty_and_invalid_config_raise(tmp_path) -> None:
    from tgv.exceptions import ConfigurationError

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="empty"):
        load_config(empty)

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("models: [", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Invalid"):
        load_config(invalid)


def test_missing_config_raises(tmp_path) -> None:
    from tgv.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError, match="Cannot read"):
        load_config(tmp_path / "missing.yaml")


def test_example_config_loads() -> None:
    config = load_config(Path("config/tgv.example.yaml"))
    assert config.routing.use_local_model is True
    assert config.routing.patterns.simple


def test_duplicate_keys_are_actionable(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yaml"
    path.write_text("version: 1\nversion: 2\nmodels: {}\n", encoding="utf-8")
    pattern = "Invalid configuration.*Duplicate configuration key.*line 2"
    with pytest.raises(ConfigurationError, match=pattern):
        load_config(path)


def test_default_model_reasoning_budgets() -> None:
    config = default_config()
    assert config.models["fast"].reasoning.value == "minimal"
    assert config.models["code"].reasoning.value == "standard"
    assert config.models["frontier_standard"].reasoning.value == "standard"
    assert config.models["frontier_deep"].reasoning.value == "high"
