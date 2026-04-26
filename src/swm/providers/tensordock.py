"""TensorDock cloud GPU provider."""

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

API_BASE = "https://dashboard.tensordock.com/api/v2"

_STATUS = {
    "running": InstanceStatus.RUNNING,
    "stopped": InstanceStatus.STOPPED,
    "StoppedDisassociated": InstanceStatus.STOPPED,
    "stopping": InstanceStatus.PENDING,
    "starting": InstanceStatus.PENDING,
    "deploying": InstanceStatus.PENDING,
}


class TensorDockProvider(CloudProvider):
    @property
    def name(self) -> str:
        return "TensorDock"

    @property
    def slug(self) -> str:
        return "tensordock"

    def is_configured(self) -> bool:
        return cfg.get("tensordock.api_token") is not None

    def _token(self) -> str:
        tok = cfg.get("tensordock.api_token")
        if not tok:
            raise RuntimeError(
                "TensorDock API token not configured. "
                "Run: swm config set tensordock.api_token <token>"
            )
        return str(tok)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
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

    def _delete(self, path: str) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.delete(f"{API_BASE}/{path}", headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    # ── queries ─────────────────────────────────────────────────────

    def list_instances(self) -> list[Instance]:
        data = self._get("instances")
        instances = data.get("data", {}).get("instances", data.get("instances", []))
        if isinstance(instances, dict):
            instances = list(instances.values())
        return [self._to_instance(i) for i in instances]

    def get_instance(self, instance_id: str) -> Instance:
        data = self._get(f"instances/{instance_id}")
        raw = data.get("data", data)
        return self._to_instance(raw)

    def list_gpus(self, gpu_count: int | None = None) -> list[GpuInfo]:
        data = self._get("locations")
        locations = data.get("data", {}).get("locations", data.get("locations", []))
        if isinstance(locations, dict):
            locations = list(locations.values())

        seen: dict[str, GpuInfo] = {}
        for loc in locations:
            gpu_offerings = loc.get("gpus", loc.get("gpu_types", {}))
            if isinstance(gpu_offerings, dict):
                gpu_offerings = [
                    {**v, "v0Name": k} if isinstance(v, dict) else {"v0Name": k}
                    for k, v in gpu_offerings.items()
                ]

            loc_name = loc.get("location", loc.get("name", loc.get("id", "")))

            for gpu in gpu_offerings:
                type_id = gpu.get("v0Name", gpu.get("name", ""))
                if not type_id:
                    continue

                display = gpu.get("displayName", gpu.get("display_name", type_id))
                max_count = int(gpu.get("max_count", gpu.get("maxCount", 1)))
                price = gpu.get("price_per_hr", gpu.get("pricePerHr"))
                vram = _extract_vram(type_id) or int(gpu.get("vram_gb", gpu.get("vramGb", 0)))

                if gpu_count is not None and max_count < gpu_count:
                    continue

                key = f"{type_id}:{gpu_count or 1}"
                if key not in seen:
                    seen[key] = GpuInfo(
                        provider=self.slug,
                        type_id=type_id,
                        display_name=display,
                        vram_gb=vram,
                        gpu_count=gpu_count or 1,
                        on_demand_price=float(price) if price else None,
                        stock_level="available",
                    )
                if loc_name and loc_name not in seen[key].regions:
                    seen[key].regions.append(loc_name)

        return sorted(seen.values(), key=lambda g: g.vram_gb, reverse=True)

    # ── mutations ───────────────────────────────────────────────────

    def create_instance(self, config: CreateConfig) -> Instance:
        gpus = self.list_gpus()
        candidates = [g.type_id for g in gpus]
        gpu_type = resolve_gpu_type(config.gpu_type, candidates)

        locations = self._get("locations")
        loc_list = locations.get("data", {}).get("locations", locations.get("locations", []))
        if isinstance(loc_list, dict):
            loc_list = list(loc_list.values())

        target_loc = None
        for loc in loc_list:
            gpu_offerings = loc.get("gpus", loc.get("gpu_types", {}))
            if isinstance(gpu_offerings, dict) and gpu_type in gpu_offerings:
                target_loc = loc
                break

        if not target_loc:
            raise RuntimeError(f"No location with {gpu_type} available")

        loc_id = target_loc.get("id", target_loc.get("location_id", ""))

        import pathlib
        ssh_key = ""
        for name in ("id_ed25519.pub", "id_rsa.pub"):
            path = pathlib.Path.home() / ".ssh" / name
            if path.exists():
                ssh_key = path.read_text().strip()
                break

        if not ssh_key:
            raise RuntimeError("No SSH public key found in ~/.ssh/")

        body = {
            "data": {
                "type": "virtualmachine",
                "attributes": {
                    "name": config.name,
                    "image": "ubuntu2404",
                    "resources": {
                        "vcpu_count": max(4, config.gpu_count * 4),
                        "ram_gb": max(16, config.gpu_count * 16),
                        "storage_gb": max(config.volume_gb, 100),
                        "gpus": {gpu_type: {"count": config.gpu_count}},
                    },
                    "location_id": loc_id,
                    "useDedicatedIp": True,
                    "ssh_key": ssh_key,
                },
            }
        }

        resp = self._post("instances", body)
        inst_data = resp.get("data", resp)
        inst_id = inst_data.get("id", "")
        return self._wait_ready(inst_id)

    def start_instance(self, instance_id: str) -> Instance:
        self._post(f"instances/{instance_id}/start")
        time.sleep(2)
        return self.get_instance(instance_id)

    def stop_instance(self, instance_id: str) -> Instance:
        self._post(f"instances/{instance_id}/stop")
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
        raise TimeoutError(
            f"TensorDock instance {instance_id} not ready within {timeout}s"
        )

    def _to_instance(self, raw: dict) -> Instance:
        status_str = raw.get("status", raw.get("state", ""))
        status = _STATUS.get(status_str, InstanceStatus.UNKNOWN)

        ip = raw.get("ipAddress", raw.get("ip", None))
        port_fwds = raw.get("portForwards", raw.get("port_forwards", []))
        ssh_port = 22
        for pf in port_fwds:
            if pf.get("internal_port") == 22:
                ssh_port = pf.get("external_port", 22)
                break

        resources = raw.get("resources", {})
        gpus = resources.get("gpus", {})
        gpu_type = ""
        gpu_count = 0
        for gtype, ginfo in gpus.items():
            gpu_type = gtype
            gpu_count = ginfo.get("count", 1) if isinstance(ginfo, dict) else 1
            break

        return Instance(
            provider=self.slug,
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            gpu_type=gpu_type,
            gpu_count=gpu_count,
            status=status,
            cost_per_hr=raw.get("rateHourly", raw.get("rate_hourly")),
            ip_address=ip,
            ssh_host=ip,
            ssh_port=ssh_port,
            ssh_user="user",
        )


def _extract_vram(type_id: str) -> int:
    """Extract VRAM in GB from a TensorDock GPU type ID like 'h100-sxm5-80gb'."""
    import re
    m = re.search(r"(\d+)gb", type_id, re.IGNORECASE)
    return int(m.group(1)) if m else 0
