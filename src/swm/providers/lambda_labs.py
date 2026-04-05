from __future__ import annotations

import re

import httpx

from swm import config as cfg
from swm.providers.base import (
    CloudProvider,
    CreateConfig,
    GpuInfo,
    Instance,
    InstanceStatus,
)

API_BASE = "https://cloud.lambdalabs.com/api/v1"

GPU_TYPE_MAP = {
    "h200": "h200_sxm_141gb",
    "h100_sxm": "h100_sxm_80gb",
    "h100_pcie": "h100_pcie_80gb",
    "a100_80": "a100_80gb_sxm4",
    "a100_40": "a100_pcie_40gb",
    "a10": "a10_24gb",
    "l40s": "l40s",
    "rtx_4090": "rtx_4090",
}

_VRAM_RE = re.compile(r"(\d+)\s*gb", re.IGNORECASE)

_STATUS = {
    "active": InstanceStatus.RUNNING,
    "booting": InstanceStatus.PENDING,
    "unhealthy": InstanceStatus.UNKNOWN,
    "terminated": InstanceStatus.TERMINATED,
}


def _parse_vram(desc: str) -> int:
    m = _VRAM_RE.search(desc)
    return int(m.group(1)) if m else 0


class LambdaLabsProvider(CloudProvider):
    @property
    def name(self) -> str:
        return "Lambda Labs"

    @property
    def slug(self) -> str:
        return "lambda"

    def is_configured(self) -> bool:
        return cfg.get("lambda.api_key") is not None

    def _api_key(self) -> str:
        key = cfg.get("lambda.api_key")
        if not key:
            raise RuntimeError(
                "Lambda API key not configured. Run: swm config set lambda.api_key <key>"
            )
        return str(key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{API_BASE}/{path}", headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    def _post(self, path: str, body: dict | None = None) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{API_BASE}/{path}",
                headers=self._headers(),
                json=body or {},
            )
            resp.raise_for_status()
            return resp.json()

    # ── queries ─────────────────────────────────────────────────────

    def list_instances(self) -> list[Instance]:
        data = self._get("instances")
        return [self._to_instance(i) for i in data.get("data", [])]

    def get_instance(self, instance_id: str) -> Instance:
        data = self._get(f"instances/{instance_id}")
        return self._to_instance(data.get("data", {}))

    def list_gpus(self) -> list[GpuInfo]:
        data = self._get("instance-types")
        results: list[GpuInfo] = []

        for type_name, info in data.get("data", {}).items():
            spec = info.get("instance_type", {})
            desc = spec.get("description", type_name)
            price_cents = spec.get("price_cents_per_hour")
            regions = info.get("regions_with_capacity_available", [])
            gpu_spec = spec.get("specs", {})

            gpu_count_match = re.match(r"gpu_(\d+)x_", type_name)
            gpu_count = int(gpu_count_match.group(1)) if gpu_count_match else 1

            results.append(GpuInfo(
                provider=self.slug,
                type_id=type_name,
                display_name=desc,
                vram_gb=_parse_vram(desc) or _parse_vram(type_name),
                min_gpu_count=gpu_count,
                on_demand_price=price_cents / 100 if price_cents else None,
                stock_level="available" if regions else "unavailable",
            ))

        return sorted(results, key=lambda g: g.vram_gb, reverse=True)

    # ── mutations ───────────────────────────────────────────────────

    def create_instance(self, config: CreateConfig) -> Instance:
        gpu_suffix = GPU_TYPE_MAP.get(config.gpu_type, config.gpu_type)
        type_name = f"gpu_{config.gpu_count}x_{gpu_suffix}"

        avail = self._get("instance-types")
        type_info = avail.get("data", {}).get(type_name)
        if not type_info:
            available = ", ".join(avail.get("data", {}).keys())
            raise RuntimeError(
                f"Instance type '{type_name}' not found. Available: {available}"
            )

        regions = type_info.get("regions_with_capacity_available", [])
        if not regions:
            raise RuntimeError(f"No regions with capacity for {type_name}")

        region = config.region or regions[0].get("name", regions[0].get("description", ""))

        ssh_keys = self._get("ssh-keys")
        key_names = [k["name"] for k in ssh_keys.get("data", [])]
        if not key_names:
            raise RuntimeError(
                "No SSH keys registered with Lambda Labs. "
                "Add one at https://cloud.lambda.ai/ssh-keys"
            )

        body = {
            "region_name": region,
            "instance_type_name": type_name,
            "ssh_key_names": key_names[:1],
            "name": config.name,
            "quantity": 1,
        }

        result = self._post("instance-operations/launch", body)
        ids = result.get("data", {}).get("instance_ids", [])
        if not ids:
            raise RuntimeError("Lambda did not return an instance ID")

        return self.get_instance(ids[0])

    def start_instance(self, instance_id: str) -> Instance:
        raise RuntimeError(
            "Lambda Labs does not support stop/start. "
            "Terminate and re-launch instead."
        )

    def stop_instance(self, instance_id: str) -> Instance:
        raise RuntimeError(
            "Lambda Labs does not support stop/start. "
            "Terminate and re-launch instead."
        )

    def terminate_instance(self, instance_id: str) -> bool:
        self._post("instance-operations/terminate", {"instance_ids": [instance_id]})
        return True

    # ── helpers ──────────────────────────────────────────────────────

    def _to_instance(self, raw: dict) -> Instance:
        status_str = raw.get("status", "")
        status = _STATUS.get(status_str, InstanceStatus.UNKNOWN)
        ip = raw.get("ip")

        return Instance(
            provider=self.slug,
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            gpu_type=raw.get("instance_type", {}).get("description", "unknown")
                if isinstance(raw.get("instance_type"), dict)
                else str(raw.get("instance_type", "unknown")),
            gpu_count=1,
            status=status,
            ip_address=ip,
            ssh_host=ip,
            ssh_port=22,
            ssh_user="ubuntu",
            region=raw.get("region", {}).get("name") if isinstance(raw.get("region"), dict) else None,
        )
