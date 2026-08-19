"""配置加载:YAML -> 嵌套 dot-dict。"""

from __future__ import annotations

from pathlib import Path

import yaml

_DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


class Config:
    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, key: str):
        if key in self._data:
            return Config(self._data[key]) if isinstance(self._data[key], dict) else self._data[key]
        raise AttributeError(f"no config key: {key}")

    def get(self, key: str, default=None):
        if key in self._data:
            v = self._data[key]
            return Config(v) if isinstance(v, dict) else v
        return default

    def to_dict(self) -> dict:
        return self._data


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else _DEFAULT_CONFIG
    with open(p, encoding="utf-8") as f:
        return Config(yaml.safe_load(f))
