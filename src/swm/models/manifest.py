"""Manifest of models pulled onto a pod.

The manifest lives at ``/workspace/models/.manifest.json`` and records every
pull and link operation so ``swm models list`` can reconcile what's actually
on disk with what swm thinks should be there.

Reads and writes happen over SSH; this module exposes pure helpers plus a
thin :class:`RemoteManifest` wrapper that uses a :class:`RemoteSession`.
"""
from __future__ import annotations

import base64
import json
import shlex
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swm.remote.ssh import RemoteSession

MANIFEST_PATH = "/workspace/models/.manifest.json"
MODELS_ROOT = "/workspace/models"
MANIFEST_VERSION = 1


@dataclass
class ModelEntry:
    key: str  # unique key (e.g. "hf:Qwen/Qwen3-8B")
    source: str  # "hf" | "ollama" | "civitai" | "url"
    ref: str
    asset_type: str  # swm asset-type vocab
    path: str  # absolute path on pod
    size_bytes: int = 0
    display_name: str = ""
    pulled_at: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ModelEntry:
        return cls(
            key=data.get("key", ""),
            source=data.get("source", ""),
            ref=data.get("ref", ""),
            asset_type=data.get("asset_type", "file"),
            path=data.get("path", ""),
            size_bytes=int(data.get("size_bytes", 0)),
            display_name=data.get("display_name", ""),
            pulled_at=data.get("pulled_at", ""),
            extra=dict(data.get("extra", {})),
        )


@dataclass
class Manifest:
    version: int = MANIFEST_VERSION
    models: dict[str, ModelEntry] = field(default_factory=dict)

    def upsert(self, entry: ModelEntry) -> None:
        if not entry.pulled_at:
            entry.pulled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.models[entry.key] = entry

    def remove(self, key: str) -> ModelEntry | None:
        return self.models.pop(key, None)

    def find_by_ref(self, ref: str) -> ModelEntry | None:
        for entry in self.models.values():
            if entry.ref == ref:
                return entry
        return None

    def find_by_display(self, needle: str) -> list[ModelEntry]:
        n = needle.lower()
        out: list[ModelEntry] = []
        for entry in self.models.values():
            if n in entry.display_name.lower() or n in entry.ref.lower() or n == entry.key.lower():
                out.append(entry)
        return out

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "models": {k: e.to_dict() for k, e in self.models.items()},
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> Manifest:
        if not raw or not raw.strip():
            return cls()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        version = int(data.get("version", MANIFEST_VERSION))
        models_data = data.get("models", {}) or {}
        models = {
            k: ModelEntry.from_dict(v)
            for k, v in models_data.items()
            if isinstance(v, dict)
        }
        return cls(version=version, models=models)


def make_key(source: str, ref: str, *, file_id: str | None = None) -> str:
    """Canonical manifest key for a (source, ref[, file]) tuple.

    *file_id* disambiguates multiple files pulled from the same HF repo
    (e.g. high/low LoRA pairs).
    """
    base = f"{source}:{ref}"
    return f"{base}:{file_id}" if file_id else base


class RemoteManifest:
    """Convenience wrapper around the manifest file on a remote pod."""

    def __init__(self, session: RemoteSession):
        self.session = session

    def load(self) -> Manifest:
        cmd = f"cat {MANIFEST_PATH} 2>/dev/null || true"
        _, out, _ = self.session.exec(cmd, stream=False)
        if out.strip():
            try:
                json.loads(out)
            except json.JSONDecodeError:
                # Preserve the unparseable file instead of letting the next
                # save() silently replace it — the pre-fix writer corrupted
                # manifests containing $/`/" and this is the recovery path.
                self.session.exec(
                    f"cp {MANIFEST_PATH} "
                    f"{MANIFEST_PATH}.corrupt-$(date +%s) 2>/dev/null || true",
                    stream=False,
                )
        return Manifest.from_json(out)

    def save(self, manifest: Manifest) -> None:
        # Base64 transport: byte-exact for any content. The previous
        # escape-then-quoted-heredoc approach corrupted every manifest
        # whose JSON contained $, `, " or non-ASCII (a quoted heredoc
        # performs no expansion, so the "escaping" landed literally),
        # and a corrupted manifest reloaded as empty — wiping all
        # tracked models on the next save.
        body = manifest.to_json()
        b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")

        if len(b64) > 100_000:
            # Linux caps a single argv element at 128 KiB (MAX_ARG_STRLEN);
            # the whole ssh command travels as one. Large manifests
            # (~90+ tracked models) go over scp instead.
            self._save_via_scp(body)
            return

        cmd = (
            f"mkdir -p {MODELS_ROOT} && "
            f"echo '{b64}' | base64 -d > {MANIFEST_PATH}.tmp && "
            f"mv {MANIFEST_PATH}.tmp {MANIFEST_PATH}"
        )
        exit_code, _, _ = self.session.exec(cmd, stream=False)
        if exit_code != 0:
            raise RuntimeError(f"failed to write manifest to {MANIFEST_PATH}")

    def _save_via_scp(self, body: str) -> None:
        import os
        import tempfile

        fd, local = tempfile.mkstemp(suffix=".manifest.json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            self.session.exec(f"mkdir -p {MODELS_ROOT}", stream=False)
            self.session.upload(local, f"{MANIFEST_PATH}.tmp")
            exit_code, _, _ = self.session.exec(
                f"mv {MANIFEST_PATH}.tmp {MANIFEST_PATH}", stream=False,
            )
            if exit_code != 0:
                raise RuntimeError(
                    f"failed to write manifest to {MANIFEST_PATH}"
                )
        finally:
            os.unlink(local)

    def upsert(self, entry: ModelEntry) -> Manifest:
        manifest = self.load()
        manifest.upsert(entry)
        self.save(manifest)
        return manifest

    def remove(self, key: str) -> Manifest:
        manifest = self.load()
        manifest.remove(key)
        self.save(manifest)
        return manifest


def reconcile_paths(manifest: Manifest, session: RemoteSession) -> dict[str, str]:
    """Verify each manifest entry's path exists on the pod.

    Returns a mapping ``{key: "ok"|"missing"}``.
    """
    if not manifest.models:
        return {}
    # Keys and paths are user/provider-controlled: URL keys contain & and ?,
    # link paths can contain spaces — all of which broke (or backgrounded
    # parts of) the unquoted command.
    paths_check = " ; ".join(
        f"echo {shlex.quote(key)} "
        f"$([ -e {shlex.quote(entry.path)} ] && echo ok || echo missing)"
        for key, entry in manifest.models.items()
    )
    _, out, _ = session.exec(paths_check, stream=False)
    result: dict[str, str] = {}
    for line in out.splitlines():
        # rsplit: keys may themselves contain spaces (link keys embed paths).
        parts = line.strip().rsplit(None, 1)
        if len(parts) == 2 and parts[1] in ("ok", "missing"):
            result[parts[0]] = parts[1]
    return result
