"""Vultr Cloud GPU provider."""

from __future__ import annotations

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

API_BASE = "https://api.vultr.com/v2"

_STATUS = {
    "running": InstanceStatus.RUNNING,
    "stopped": InstanceStatus.STOPPED,
    "pending": InstanceStatus.PENDING,
}


class VultrProvider(CloudProvider):
    @property
    def name(self) -> str:
        return "Vultr"

    @property
    def slug(self) -> str:
        return "vultr"

    def is_configured(self) -> bool:
        return cfg.get("vultr.api_key") is not None

    def _api_key(self) -> str:
        key = cfg.get("vultr.api_key")
        if not key:
            raise RuntimeError(
                "Vultr API key not configured. Run: swm config set vultr.api_key <key>"
            )
        return str(key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{API_BASE}/{path}", headers=self._headers(), params=params,
            )
            resp.raise_for_status()
            return resp.json()

    def _post(self, path: str, body: dict | None = None) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{API_BASE}/{path}", headers=self._headers(), json=body or {},
            )
            resp.raise_for_status()
            return resp.json()

    def _delete(self, path: str) -> None:
        with httpx.Client(timeout=30) as client:
            resp = client.delete(f"{API_BASE}/{path}", headers=self._headers())
            resp.raise_for_status()

    # ── queries ─────────────────────────────────────────────────────

    def list_instances(self) -> list[Instance]:
        data = self._get("instances")
        return [self._to_instance(i) for i in data.get("instances", [])]

    def get_instance(self, instance_id: str) -> Instance:
        data = self._get(f"instances/{instance_id}")
        return self._to_instance(data.get("instance", {}))

    def list_gpus(self, gpu_count: int | None = None) -> list[GpuInfo]:
        data = self._get("plans")
        results: list[GpuInfo] = []

        for plan in data.get("plans", []):
            plan_id: str = plan.get("id", "")
            if not plan_id.startswith("vcg-"):
                continue

            gpu_type = plan.get("gpu_type", "")
            vram = plan.get("gpu_vram_gb", 0)
            n_gpus = plan.get("gpu_count", 1) if "gpu_count" in plan else 1
            price = plan.get("monthly_cost", 0) / 720 if plan.get("monthly_cost") else None

            if gpu_count is not None and n_gpus != gpu_count:
                continue

            regions = plan.get("locations", [])
            results.append(GpuInfo(
                provider=self.slug,
                type_id=plan_id,
                display_name=f"{gpu_type} {vram}GB" if gpu_type else plan_id,
                vram_gb=int(vram),
                gpu_count=n_gpus,
                on_demand_price=price,
                stock_level="available" if regions else "unavailable",
            ))

        return sorted(results, key=lambda g: g.vram_gb, reverse=True)

    # ── mutations ───────────────────────────────────────────────────

    def _ensure_ssh_key(self) -> str:
        """Return the ID of an existing SSH key, or upload one from ~/.ssh."""
        existing = self._get("ssh-keys")
        keys = existing.get("ssh_keys", [])
        for k in keys:
            if k.get("name", "").startswith("swm-"):
                return k["id"]
        if keys:
            return keys[0]["id"]

        import pathlib
        for name in ("id_ed25519.pub", "id_rsa.pub"):
            path = pathlib.Path.home() / ".ssh" / name
            if path.exists():
                pub = path.read_text().strip()
                resp = self._post("ssh-keys", {"name": "swm-key", "ssh_key": pub})
                return resp["ssh_key"]["id"]

        raise RuntimeError(
            "No SSH keys found. Add one at https://my.vultr.com/settings/#settingsapi "
            "or create ~/.ssh/id_ed25519.pub"
        )

    def create_instance(self, config: CreateConfig) -> Instance:
        gpus = self.list_gpus(config.gpu_count)
        candidates = [g.type_id for g in gpus]
        plan_id = resolve_gpu_type(config.gpu_type, candidates)

        plan_info = next((g for g in gpus if g.type_id == plan_id), None)
        regions = self._get("plans")
        plan_regions: list[str] = []
        for p in regions.get("plans", []):
            if p.get("id") == plan_id:
                plan_regions = p.get("locations", [])
                break

        if not plan_regions:
            raise RuntimeError(f"No regions available for plan {plan_id}")
        region = config.region or plan_regions[0]

        ssh_key_id = self._ensure_ssh_key()

        body = {
            "region": region,
            "plan": plan_id,
            "os_id": 2284,
            "label": config.name,
            "hostname": config.name,
            "sshkey_id": [ssh_key_id],
        }

        resp = self._post("instances", body)
        inst = resp.get("instance", {})
        return self._wait_ready(inst["id"])

    def start_instance(self, instance_id: str) -> Instance:
        self._post("instances/start", {"instance_ids": [instance_id]})
        time.sleep(2)
        return self.get_instance(instance_id)

    def stop_instance(self, instance_id: str) -> Instance:
        self._post("instances/halt", {"instance_ids": [instance_id]})
        time.sleep(2)
        return self.get_instance(instance_id)

    def terminate_instance(self, instance_id: str) -> bool:
        self._delete(f"instances/{instance_id}")
        return True

    # ── helpers ──────────────────────────────────────────────────────

    def _wait_ready(self, instance_id: str, timeout: int = 300) -> Instance:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            inst = self.get_instance(instance_id)
            if inst.status == InstanceStatus.RUNNING:
                return inst
            time.sleep(10)
        raise TimeoutError(f"Vultr instance {instance_id} not ready within {timeout}s")

    def _to_instance(self, raw: dict) -> Instance:
        power = raw.get("power_status", "")
        status_str = raw.get("status", "")
        if power == "running" and status_str == "active":
            status = InstanceStatus.RUNNING
        elif power == "stopped":
            status = InstanceStatus.STOPPED
        elif status_str == "pending":
            status = InstanceStatus.PENDING
        else:
            status = _STATUS.get(power, InstanceStatus.UNKNOWN)

        ip = raw.get("main_ip") or None
        plan_id = raw.get("plan", "")

        return Instance(
            provider=self.slug,
            id=raw.get("id", ""),
            name=raw.get("label", ""),
            gpu_type=plan_id,
            gpu_count=1,
            status=status,
            cost_per_hr=raw.get("monthly_cost", 0) / 720 if raw.get("monthly_cost") else None,
            region=raw.get("region", ""),
            ip_address=ip,
            ssh_host=ip,
            ssh_port=22,
            ssh_user="root",
        )
