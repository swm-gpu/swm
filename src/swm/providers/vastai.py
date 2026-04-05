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
)

API_BASE = "https://console.vast.ai/api/v0"

GPU_TYPE_MAP = {
    "h200": "H200",
    "h100": "H100",
    "h100_sxm": "H100 SXM",
    "h100_pcie": "H100 PCIe",
    "a100_80": "A100 80GB",
    "a100_40": "A100 40GB",
    "a6000": "RTX A6000",
    "l40s": "L40S",
    "rtx_4090": "RTX 4090",
    "rtx_3090": "RTX 3090",
}

DEFAULT_IMAGE = "vastai/pytorch"

_STATUS = {
    "running": InstanceStatus.RUNNING,
    "created": InstanceStatus.PENDING,
    "loading": InstanceStatus.PENDING,
    "exited": InstanceStatus.STOPPED,
}


class VastAIProvider(CloudProvider):
    @property
    def name(self) -> str:
        return "Vast.ai"

    @property
    def slug(self) -> str:
        return "vastai"

    def is_configured(self) -> bool:
        return cfg.get("vastai.api_key") is not None

    def _api_key(self) -> str:
        key = cfg.get("vastai.api_key")
        if not key:
            raise RuntimeError(
                "Vast.ai API key not configured. Run: swm config set vastai.api_key <key>"
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
                f"{API_BASE}/{path}",
                headers=self._headers(),
                params=params,
            )
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

    def _put(self, path: str, body: dict | None = None) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.put(
                f"{API_BASE}/{path}",
                headers=self._headers(),
                json=body or {},
            )
            resp.raise_for_status()
            return resp.json()

    def _delete(self, path: str) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.delete(
                f"{API_BASE}/{path}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    # ── queries ─────────────────────────────────────────────────────

    def list_instances(self) -> list[Instance]:
        data = self._get("instances/")
        return [self._to_instance(i) for i in data.get("instances", [])]

    def list_gpus(self) -> list[GpuInfo]:
        data = self._post("bundles/", {
            "num_gpus": {"gte": 1},
            "rentable": {"eq": True},
            "order": [["dph_total", "asc"]],
            "limit": 500,
        })
        seen: dict[str, GpuInfo] = {}
        for offer in data.get("offers", []):
            gpu_name = offer.get("gpu_name", "unknown")
            if gpu_name in seen:
                existing = seen[gpu_name]
                price = offer.get("dph_total")
                if price and (
                    existing.on_demand_price is None
                    or price < existing.on_demand_price
                ):
                    existing.on_demand_price = price
                continue
            seen[gpu_name] = GpuInfo(
                provider=self.slug,
                type_id=gpu_name,
                display_name=gpu_name,
                vram_gb=int(offer.get("gpu_ram", 0) / 1024),
                on_demand_price=offer.get("dph_total"),
                stock_level="available",
                secure_cloud=offer.get("verification", "") == "verified",
            )
        return sorted(seen.values(), key=lambda g: g.vram_gb, reverse=True)

    # ── mutations ───────────────────────────────────────────────────

    def create_instance(self, config: CreateConfig) -> Instance:
        gpu_name = GPU_TYPE_MAP.get(config.gpu_type, config.gpu_type)
        image = config.image or DEFAULT_IMAGE

        search_body: dict = {
            "gpu_name": {"eq": gpu_name},
            "num_gpus": {"gte": config.gpu_count},
            "rentable": {"eq": True},
            "order": [["dph_total", "asc"]],
            "limit": 10,
        }
        if config.cloud_type == "SECURE":
            search_body["verification"] = {"eq": "verified"}

        data = self._post("bundles/", search_body)
        offers = data.get("offers", [])
        if not offers:
            raise RuntimeError(
                f"No Vast.ai offers found for {gpu_name} x{config.gpu_count}"
            )

        offer = offers[0]
        result = self._put(f"asks/{offer['id']}/", {
            "client_id": "me",
            "image": image,
            "disk": config.container_disk_gb,
            "label": config.name,
            "onstart": None,
            "runtype": "ssh_direc",
            "env": dict(config.env),
        })

        instance_id = str(result.get("new_contract") or result.get("id", ""))
        if not instance_id:
            raise RuntimeError("Vast.ai did not return an instance ID")

        time.sleep(2)
        for inst in self.list_instances():
            if inst.id == instance_id:
                return inst

        return Instance(
            provider=self.slug,
            id=instance_id,
            name=config.name,
            gpu_type=gpu_name,
            gpu_count=config.gpu_count,
            status=InstanceStatus.PENDING,
            image=image,
            container_disk_gb=config.container_disk_gb,
        )

    def start_instance(self, instance_id: str) -> Instance:
        self._put(f"instances/{instance_id}/", {"state": "running"})
        return self._get_instance(instance_id)

    def stop_instance(self, instance_id: str) -> Instance:
        self._put(f"instances/{instance_id}/", {"state": "stopped"})
        return self._get_instance(instance_id)

    def terminate_instance(self, instance_id: str) -> bool:
        self._delete(f"instances/{instance_id}/")
        return True

    # ── helpers ──────────────────────────────────────────────────────

    def _get_instance(self, instance_id: str) -> Instance:
        data = self._get("instances/")
        for inst in data.get("instances", []):
            if str(inst.get("id")) == str(instance_id):
                return self._to_instance(inst)
        raise RuntimeError(f"Instance {instance_id} not found")

    def _to_instance(self, raw: dict) -> Instance:
        actual_status = raw.get("actual_status", "")
        status = _STATUS.get(actual_status, InstanceStatus.UNKNOWN)

        uptime = None
        if raw.get("start_date"):
            uptime = int(time.time() - raw["start_date"])

        ports: dict[int, int] = {}
        for container_port, mappings in (raw.get("ports") or {}).items():
            private = int(container_port.split("/")[0])
            if mappings:
                public = int(mappings[0].get("HostPort", 0))
                if public:
                    ports[private] = public

        return Instance(
            provider=self.slug,
            id=str(raw["id"]),
            name=raw.get("label", ""),
            gpu_type=raw.get("gpu_name", "unknown"),
            gpu_count=raw.get("num_gpus", 1),
            status=status,
            cost_per_hr=raw.get("dph_total"),
            uptime_seconds=uptime,
            ip_address=raw.get("public_ipaddr"),
            ssh_host=raw.get("ssh_host"),
            ssh_port=raw.get("ssh_port"),
            ssh_user="root",
            ports=ports,
            image=raw.get("image_uuid"),
            volume_gb=int(raw["disk_space"]) if raw.get("disk_space") else None,
        )
