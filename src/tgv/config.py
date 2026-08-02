from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from tgv.exceptions import ConfigurationError
from tgv.models import TGVConfig


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise ConfigurationError(f"Duplicate configuration key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def default_config() -> TGVConfig:
    return TGVConfig.model_validate(
        {
            "version": 1,
            "models": {
                "fast": {
                    "provider": "ollama",
                    "model": "qwen3:4b",
                    "base_url": "http://127.0.1:11434",
                    "timeout_seconds": 45,
                    "temperature": 0,
                    "reasoning": "minimal",
                },
                "code": {
                    "provider": "ollama",
                    "model": "qwen3-coder:30b",
                    "base_url": "http://127.0.1:11434",
                    "timeout_seconds": 120,
                    "temperature": 0.1,
                    "reasoning": "standard",
                },
                "frontier_standard": {
                    "provider": "external",
                    "model": "auto",
                    "reasoning": "standard",
                },
                "frontier_deep": {
                    "provider": "external",
                    "model": "auto",
                    "reasoning": "high",
                },
            },
        }
    )


def load_config(path: Path | None) -> TGVConfig:
    if path is None:
        return default_config()
    try:
        raw: Any = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
        if raw is None:
            raise ConfigurationError(f"Configuration is empty: {path}")
        return TGVConfig.model_validate(raw)
    except OSError as exc:
        raise ConfigurationError(f"Cannot read configuration {path}: {exc}") from exc
    except ConfigurationError as exc:
        raise ConfigurationError(f"Invalid configuration {path}: {exc}") from exc
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"Invalid configuration {path}: {exc}") from exc
