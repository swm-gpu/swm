from __future__ import annotations

import json
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

API_BASE = "https://console.vast.ai/api/v0"
API_BASE_V1 = "https://console.vast.ai/api/v1"

# v1 rejects anything larger and silently clamps values <= 0 to 5.
V1_PAGE_LIMIT = 25

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

    def _get_v1(self, path: str, params: dict | None = None) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{API_BASE_V1}/{path}",
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

    def _instance_rows(self, select_filters: dict | None = None) -> list[dict]:
        """Fetch raw instance rows from v1; the v0 collection listing is deprecated.

        v1 caps pages at 25 rows and uses keyset pagination, where the cursor is
        read from the response as ``next_token`` but sent back as ``after_token``.
        Unknown query params are ignored rather than rejected, so sending the
        wrong name would silently re-request the first page forever.
        """
        rows: list[dict] = []
        params: dict[str, str | int] = {
            "limit": V1_PAGE_LIMIT,
            "order_by": json.dumps([{"col": "id", "dir": "asc"}]),
        }
        if select_filters:
            params["select_filters"] = json.dumps(select_filters)

        seen_tokens: set[str] = set()
        while True:
            data = self._get_v1("instances/", params)
            rows.extend(data.get("instances") or [])
            token = data.get("next_token")
            if not token or token in seen_tokens:
                break
            seen_tokens.add(token)
            params["after_token"] = token
        return rows

    def list_instances(self) -> list[Instance]:
        return [self._to_instance(i) for i in self._instance_rows()]

    def list_gpus(self, gpu_count: int | None = None) -> list[GpuInfo]:
        num_filter: dict = {"eq": gpu_count} if gpu_count else {"gte": 1}
        data = self._post("bundles/", {
            "num_gpus": num_filter,
            "rentable": {"eq": True},
            "order": [["dph_total", "asc"]],
            "limit": 3000,
        })
        seen: dict[tuple[str, int], GpuInfo] = {}
        for offer in data.get("offers", []):
            gpu_name = offer.get("gpu_name", "unknown")
            n = offer.get("num_gpus", 1)
            key = (gpu_name, n)
            price = offer.get("dph_total")
            geo = offer.get("geolocation", "")
            if key in seen:
                existing = seen[key]
                if price and (
                    existing.on_demand_price is None
                    or price < existing.on_demand_price
                ):
                    existing.on_demand_price = price
                if geo and geo not in existing.regions:
                    existing.regions.append(geo)
                continue
            seen[key] = GpuInfo(
                provider=self.slug,
                type_id=gpu_name,
                display_name=gpu_name,
                vram_gb=int(offer.get("gpu_ram", 0) / 1024),
                gpu_count=n,
                on_demand_price=price,
                stock_level="available",
                secure_cloud=offer.get("verification", "") == "verified",
                regions=[geo] if geo else [],
            )
        return sorted(seen.values(), key=lambda g: (-g.vram_gb, g.gpu_count))

    # ── mutations ───────────────────────────────────────────────────

    def _gpu_names(self) -> list[str]:
        """Fetch the set of distinct GPU names currently on the marketplace."""
        data = self._post("bundles/", {
            "rentable": {"eq": True},
            "limit": 10000,
        })
        return list({o.get("gpu_name", "") for o in data.get("offers", []) if o.get("gpu_name")})

    def create_instance(self, config: CreateConfig) -> Instance:
        gpu_name = resolve_gpu_type(config.gpu_type, self._gpu_names())
        image = config.image or DEFAULT_IMAGE

        disk_gb = max(config.container_disk_gb, config.volume_gb)
        search_body: dict = {
            "gpu_name": {"eq": gpu_name},
            "num_gpus": {"gte": config.gpu_count},
            "rentable": {"eq": True},
            "disk_space": {"gte": disk_gb},
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

        rent_body = {
            "client_id": "me",
            "image": image,
            "disk": disk_gb,
            "label": config.name,
            "onstart": None,
            "runtype": "ssh_direct",
            "env": dict(config.env),
        }

        result = None
        last_err = None
        for offer in offers[:5]:
            try:
                result = self._put(f"asks/{offer['id']}/", rent_body)
                break
            except Exception as e:
                last_err = e
                continue

        if result is None:
            raise RuntimeError(
                f"Failed to rent any of {min(5, len(offers))} offers for "
                f"{gpu_name} x{config.gpu_count}: {last_err}"
            )

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
        # There is no v1 single-instance endpoint, so filter the collection
        # server-side to avoid paging through every instance.
        try:
            select_filters: dict | None = {"id": {"eq": int(instance_id)}}
        except ValueError:
            select_filters = None
        for raw in self._instance_rows(select_filters):
            if str(raw.get("id")) == str(instance_id):
                return self._to_instance(raw)
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

        status_msg = raw.get("status_msg") or None
        if status_msg:
            status_msg = status_msg.strip().split("\n")[-1].strip()

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
            status_detail=status_msg,
        )
