from __future__ import annotations

import tomllib
from pathlib import Path

import tomli_w

CONFIG_DIR = Path.home() / ".config" / "swm"
CONFIG_FILE = CONFIG_DIR / "config.toml"


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    _ensure_dirs()
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    return {}


def save(config: dict) -> None:
    _ensure_dirs()
    with open(CONFIG_FILE, "wb") as f:
        tomli_w.dump(config, f)


def get(key: str, default=None):
    """Retrieve a nested value using dot-separated key (e.g. 'runpod.api_key')."""
    node = load()
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def set_value(key: str, value: str) -> None:
    """Set a nested value using dot-separated key."""
    config = load()
    parts = key.split(".")
    node = config
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]

    if value.lower() in ("true", "false"):
        node[parts[-1]] = value.lower() == "true"
    elif value.isdigit():
        node[parts[-1]] = int(value)
    else:
        try:
            node[parts[-1]] = float(value)
        except ValueError:
            node[parts[-1]] = value

    save(config)


def delete(key: str) -> bool:
    """Remove a key. Returns True if the key existed."""
    config = load()
    parts = key.split(".")
    node = config
    for part in parts[:-1]:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return False
    if parts[-1] in node:
        del node[parts[-1]]
        save(config)
        return True
    return False
