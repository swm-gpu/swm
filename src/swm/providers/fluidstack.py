"""FluidStack (Atlas) cloud GPU provider."""

from __future__ import annotations

import re
import time

import httpx

from swm import config as cfg
from swm.providers.base import (
    CloudProvider,
    CreateConfig,
    GpuInfo,
    Instance,
    InstanceStatus,
    resolve_gpu_type,
)

MGMT_BASE = "https://api.atlas.fluidstack.io/api/v1alpha1"

_STATUS = {
    "running": InstanceStatus.RUNNING,
    "stopped": InstanceStatus.STOPPED,
    "creating": InstanceStatus.PENDING,
    "stopping": InstanceStatus.PENDING,
    "starting": InstanceStatus.PENDING,
    "deleting": InstanceStatus.TERMINATED,
}

_VRAM_RE = re.compile(r"(\d+)\s*GB", re.IGNORECASE)


class FluidStackProvider(CloudProvider):
    @property
    def name(self) -> str:
        return "FluidStack"

    @property
    def slug(self) -> str:
        return "fluidstack"

    def is_configured(self) -> bool:
        return cfg.get("fluidstack.api_key") is not None

    def _api_key(self) -> str:
        key = cfg.get("fluidstack.api_key")
        if not key:
            raise RuntimeError(
                "FluidStack API key not configured. "
                "Run: swm config set fluidstack.api_key <key>"
            )
        return str(key)

    def _region_url(self) -> str:
        url = cfg.get("fluidstack.region_url")
        if not url:
            regions = self._mgmt_get("regions")
            if regions:
                url = regions[0].get("url", "")
                if url:
                    return str(url)
            raise RuntimeError(
                "FluidStack region not configured. "
                "Run: swm config set fluidstack.region_url <url>"
            )
        return str(url)

    def _project_id(self) -> str:
        pid = cfg.get("fluidstack.project_id")
        if not pid:
            raise RuntimeError(
                "FluidStack project ID not configured. "
                "Run: swm config set fluidstack.project_id <id>"
            )
        return str(pid)

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

    def _infra_headers(self) -> dict[str, str]:
        return {**self._auth_headers(), "X-PROJECT-ID": self._project_id()}

    # ── HTTP helpers ────────────────────────────────────────────────

    def _mgmt_get(self, path: str) -> list | dict:
        with httpx.Client(timeout=30) as c:
            resp = c.get(f"{MGMT_BASE}/{path}", headers=self._auth_headers())
            resp.raise_for_status()
            return resp.json()

    def _mgmt_post(self, path: str, body: dict | None = None) -> dict:
        with httpx.Client(timeout=30) as c:
            resp = c.post(
                f"{MGMT_BASE}/{path}", headers=self._auth_headers(), json=body or {},
            )
            if resp.status_code == 409:
                return {}
            resp.raise_for_status()
            return resp.json()

    def _infra_get(self, path: str) -> list | dict:
        base = self._region_url()
        with httpx.Client(timeout=30) as c:
            resp = c.get(f"{base}/api/v1alpha1/{path}", headers=self._infra_headers())
            resp.raise_for_status()
            return resp.json()

    def _infra_post(self, path: str, body: dict | None = None) -> dict:
        base = self._region_url()
        with httpx.Client(timeout=30) as c:
            resp = c.post(
                f"{base}/api/v1alpha1/{path}",
                headers=self._infra_headers(),
                json=body or {},
            )
            resp.raise_for_status()
            return resp.json()

    def _infra_delete(self, path: str) -> None:
        base = self._region_url()
        with httpx.Client(timeout=30) as c:
            resp = c.delete(
                f"{base}/api/v1alpha1/{path}", headers=self._infra_headers(),
            )
            resp.raise_for_status()

    # ── queries ─────────────────────────────────────────────────────

    def list_instances(self) -> list[Instance]:
        data = self._infra_get("instances")
        items = data if isinstance(data, list) else data.get("instances", [])
        return [self._to_instance(i) for i in items]

    def get_instance(self, instance_id: str) -> Instance:
        data = self._infra_get(f"instances/{instance_id}")
        return self._to_instance(data)

    def list_gpus(self, gpu_count: int | None = None) -> list[GpuInfo]:
        types_data = self._infra_get("instance-types")
        items = types_data if isinstance(types_data, list) else types_data.get("instance_types", [])

        capacity_data = self._infra_get("capacity")
        cap_items = capacity_data if isinstance(capacity_data, list) else capacity_data.get("capacity", [])
        cap_map = {c.get("name", ""): c.get("capacity", 0) for c in cap_items}

        results: list[GpuInfo] = []
        for t in items:
            type_name = t.get("name", "")
            gpu_model = t.get("gpuModel", "")
            n_gpus = int(t.get("gpuCount", t.get("gpu_count", 1)))
            vram = _parse_vram_from_model(gpu_model)

            if gpu_count is not None and n_gpus != gpu_count:
                continue

            avail = cap_map.get(type_name, 0)
            results.append(GpuInfo(
                provider=self.slug,
                type_id=type_name,
                display_name=f"{_pretty_gpu(gpu_model)} x{n_gpus}",
                vram_gb=vram,
                gpu_count=n_gpus,
                stock_level="available" if avail > 0 else "unavailable",
            ))

        return sorted(results, key=lambda g: g.vram_gb, reverse=True)

    # ── mutations ───────────────────────────────────────────────────

    def _ensure_ssh_key(self) -> None:
        """Register the user's SSH key with FluidStack if not already present."""
        import pathlib
        for name in ("id_ed25519.pub", "id_rsa.pub"):
            path = pathlib.Path.home() / ".ssh" / name
            if path.exists():
                pub = path.read_text().strip()
                self._mgmt_post("user/keys", {"name": "swm-key", "key": pub})
                return
        raise RuntimeError("No SSH public key found in ~/.ssh/")

    def create_instance(self, config: CreateConfig) -> Instance:
        self._ensure_ssh_key()

        gpus = self.list_gpus()
        candidates = [g.type_id for g in gpus if g.stock_level == "available"]
        if not candidates:
            candidates = [g.type_id for g in gpus]
        instance_type = resolve_gpu_type(config.gpu_type, candidates)

        body: dict = {
            "name": config.name,
            "type": instance_type,
            "image": "image://ubuntu22.04",
            "preemptible": False,
            "ephemeral": False,
        }

        resp = self._infra_post("instances", body)
        inst_id = resp.get("id", "")
        return self._wait_ready(inst_id)

    def start_instance(self, instance_id: str) -> Instance:
        self._infra_post(f"instances/{instance_id}/actions/start")
        time.sleep(2)
        return self.get_instance(instance_id)

    def stop_instance(self, instance_id: str) -> Instance:
        self._infra_post(f"instances/{instance_id}/actions/stop")
        time.sleep(2)
        return self.get_instance(instance_id)

    def terminate_instance(self, instance_id: str) -> bool:
        self._infra_delete(f"instances/{instance_id}")
        return True

    # ── helpers ──────────────────────────────────────────────────────

    def _wait_ready(self, instance_id: str, timeout: int = 300) -> Instance:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            inst = self.get_instance(instance_id)
            if inst.status == InstanceStatus.RUNNING:
                return inst
            time.sleep(10)
        raise TimeoutError(
            f"FluidStack instance {instance_id} not ready within {timeout}s"
        )

    def _to_instance(self, raw: dict) -> Instance:
        state = raw.get("state", raw.get("status", ""))
        status = _STATUS.get(state, InstanceStatus.UNKNOWN)
        ip = raw.get("ip") or None

        type_name = raw.get("type", "")
        gpu_model, n_gpus = _parse_type_name(type_name)

        return Instance(
            provider=self.slug,
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            gpu_type=gpu_model,
            gpu_count=n_gpus,
            status=status,
            ip_address=ip,
            ssh_host=ip,
            ssh_port=22,
            ssh_user="ubuntu",
        )


def _parse_type_name(type_name: str) -> tuple[str, int]:
    """Parse 'h100.8x' into ('H100', 8)."""
    m = re.match(r"([a-zA-Z0-9-]+)\.(\d+)x", type_name)
    if m:
        return m.group(1).upper(), int(m.group(2))
    return type_name, 1


def _parse_vram_from_model(gpu_model: str) -> int:
    """Extract VRAM from model strings like 'GH100_H100_SXM5_80GB'."""
    m = _VRAM_RE.search(gpu_model)
    return int(m.group(1)) if m else 0


def _pretty_gpu(gpu_model: str) -> str:
    """'GH100_H100_SXM5_80GB' -> 'H100 SXM5 80GB'."""
    parts = gpu_model.replace("_", " ").split()
    if len(parts) > 1 and parts[0].startswith("G"):
        parts = parts[1:]
    return " ".join(parts)
