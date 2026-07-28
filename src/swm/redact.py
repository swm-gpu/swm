"""Credential redaction for anything swm writes to a terminal or log.

Some provider APIs require the key in the URL rather than a header — RunPod's
GraphQL endpoint is the notable one — so httpx builds exception messages that
embed a live credential. Rather than auditing every raise site, output is
scrubbed at the boundaries: the Rich consoles, Click's error reporter, and the
traceback hook.

Two passes run over each string. The value pass replaces the exact secrets
found in the user's config, which catches a credential however it was
formatted. The pattern pass catches secrets that never reach config — inline
tokens, another user's key echoed back by an API — by matching the shapes
credentials usually travel in.
"""
from __future__ import annotations

import re
import sys
import traceback

import click
from rich.console import Console

from swm import config as cfg

# Matched as substrings rather than suffixes: an exact-suffix list missed real
# credentials such as `gcs.hmac_secret`, which `swm config list` then printed in
# full. Path-like keys (`ssh.key_path`, `aws.key_name`) deliberately do not
# match, since hiding them would make the config unreadable for no benefit.
_SENSITIVE_MARKERS = (
    "api_key", "apikey", "app_key", "access_key", "secret", "token",
    "password", "passwd", "hmac", "credential", "private_key",
)
_SENSITIVE_EXACT = frozenset({"hf_token"})

# Short values are skipped: they collide with ordinary words and would turn
# unrelated output into asterisks.
_MIN_SECRET_LEN = 8

_MASK = "****"

_PATTERNS = (
    # api_key=… in a query string, and "token": "…" in a JSON error body that
    # an API echoed back. The optional quotes cover both spellings.
    re.compile(
        r"((?:api[_-]?key|apikey|access[_-]?key|secret[_-]?key|token|password)"
        r"[\"']?\s*[=:]\s*[\"']?)([^\s&'\"<>\\]{8,})",
        re.IGNORECASE,
    ),
    # Authorization: Bearer … / Basic …
    re.compile(r"((?:Bearer|Basic|Token)\s+)([A-Za-z0-9._\-+/=]{8,})", re.IGNORECASE),
)

_cache: tuple[float, int, tuple[str, ...]] | None = None


def is_sensitive_key(key: str) -> bool:
    """Whether a dot-separated config key holds a credential."""
    lowered = key.lower()
    if lowered in _SENSITIVE_EXACT:
        return True
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def mask(value: str) -> str:
    """Mask a credential, keeping a short prefix so it stays identifiable."""
    return value[:4] + _MASK if len(value) > 4 else _MASK


def _walk(node: dict, prefix: str = ""):
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _walk(value, path)
        elif isinstance(value, str) and len(value) >= _MIN_SECRET_LEN:
            if is_sensitive_key(path):
                yield value


def secret_values() -> tuple[str, ...]:
    """Every credential currently reachable through config, longest first.

    Longest-first matters: masking a short secret that is a prefix of a longer
    one would leave the remainder of the longer secret in the output.
    """
    global _cache
    try:
        stat = cfg.CONFIG_FILE.stat()
        stamp = (stat.st_mtime, stat.st_size)
    except OSError:
        stamp = (0.0, 0)

    if _cache is None or (_cache[0], _cache[1]) != stamp:
        try:
            values = set(_walk(cfg.load()))
        except Exception:
            values = set()
        _cache = (stamp[0], stamp[1], tuple(values))

    found = set(_cache[2])
    # The overlay is per-context and cheap to read, so it is never cached.
    for key, value in (cfg.overlay_values() or {}).items():
        if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN and is_sensitive_key(key):
            found.add(value)
    return tuple(sorted(found, key=len, reverse=True))


def _mask_match(match: re.Match) -> str:
    # Leave values that a caller already masked, so deliberate hints such as
    # `rpa_****` from `swm config list` survive intact.
    if _MASK in match.group(2):
        return match.group(0)
    return match.group(1) + _MASK


def scrub(text: str) -> str:
    """Remove credentials from *text*, leaving the rest of the message intact."""
    if not text or not isinstance(text, str):
        return text

    try:
        for secret in secret_values():
            if secret in text:
                text = text.replace(secret, mask(secret))
    except Exception:
        # Redaction must never be the reason a command fails to report an error.
        pass

    for pattern in _PATTERNS:
        text = pattern.sub(_mask_match, text)
    return text


class SafeConsole(Console):
    """Console that scrubs credentials out of every string it renders."""

    def print(self, *objects, **kwargs):
        super().print(*(scrub(o) if isinstance(o, str) else o for o in objects), **kwargs)

    def log(self, *objects, **kwargs):
        # Rich derives the logged source location by walking the stack, so this
        # wrapper frame has to be accounted for or every log line is attributed
        # to this module.
        kwargs["_stack_offset"] = kwargs.get("_stack_offset", 1) + 1
        super().log(*(scrub(o) if isinstance(o, str) else o for o in objects), **kwargs)


def install_error_redaction() -> None:
    """Route Click errors and uncaught tracebacks through :func:`scrub`.

    Click writes exception messages straight to stderr and the interpreter
    prints tracebacks itself, so neither passes through :class:`SafeConsole`.
    Both are wrapped once at startup rather than at ~60 raise sites.
    """
    if getattr(click.ClickException, "_swm_redacted", False):
        return

    original_show = click.ClickException.show

    def show(self, file=None):
        self.message = scrub(str(self.message))
        original_show(self, file)

    click.ClickException.show = show
    click.ClickException._swm_redacted = True

    def excepthook(exc_type, exc, tb):
        rendered = "".join(traceback.format_exception(exc_type, exc, tb))
        sys.stderr.write(scrub(rendered))

    sys.excepthook = excepthook
