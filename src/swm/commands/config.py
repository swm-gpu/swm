"""swm config — manage configuration (API keys, defaults, preferences)."""
from __future__ import annotations

import click

from swm import config as cfg
from swm.commands._helpers import console


_SENSITIVE_SUFFIXES = (
    ".api_key", ".app_key", ".secret_key", ".access_key", ".token", ".password",
)
_SENSITIVE_EXACT = frozenset({"hf_token"})


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SENSITIVE_EXACT:
        return True
    return any(lowered.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def _mask_value(key: str, value: str) -> str:
    if not _is_sensitive_key(key):
        return value
    return value[:4] + "****" if len(value) > 4 else "****"


@click.group(name="config")
def config_group():
    """Manage configuration (API keys, defaults, preferences)."""


@config_group.command(name="set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """Set a config value.  Example: swm config set runpod.api_key sk-xxx"""
    cfg.set_value(key, value)
    console.print(f"[green]✓[/green] {key} = {_mask_value(key, value)}")


@config_group.command(name="get")
@click.argument("key")
def config_get(key: str):
    """Get a config value."""
    val = cfg.get(key)
    if val is None:
        console.print(f"[yellow]⚠[/yellow]  {key} is not set")
    else:
        display = _mask_value(key, str(val))
        console.print(f"{key} = {display}")


@config_group.command(name="list")
def config_list():
    """Show all configuration values."""
    data = cfg.load()
    if not data:
        console.print(
            "[dim]No configuration set yet. "
            "Run [bold]swm config set <key> <value>[/bold] to get started.[/dim]"
        )
        return
    _print_nested(data)


@config_group.command(name="path")
def config_path():
    """Show the config file location."""
    console.print(str(cfg.CONFIG_FILE))


@config_group.command(name="delete")
@click.argument("key")
def config_delete(key: str):
    """Remove a config key."""
    if cfg.delete(key):
        console.print(f"[green]✓[/green] Deleted {key}")
    else:
        console.print(f"[yellow]⚠[/yellow]  {key} not found")


def _print_nested(d: dict, prefix: str = "") -> None:
    for k, v in d.items():
        full = f"{prefix}{k}"
        if isinstance(v, dict):
            _print_nested(v, f"{full}.")
        else:
            display = _mask_value(full, str(v))
            console.print(f"  {full} = {display}")
