from __future__ import annotations

import json
import time

import httpx

from swm import config as cfg
from swm.providers.base import (
    CloudProvider,
    CreateConfig,
    GpuInfo,
    GpuSearchField,
    GpuSearchQuery,
    Instance,
    InstanceStatus,
    normalize_gpu_search,
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
    native_search_fields = frozenset({
        GpuSearchField.GPU,
        GpuSearchField.GPU_COUNT,
        GpuSearchField.MAX_PRICE,
        GpuSearchField.REGION,
        GpuSearchField.SECURE,
        GpuSearchField.MIN_VRAM,
        GpuSearchField.MIN_DOWNLOAD,
    })

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
        return self._search_gpus(GpuSearchQuery(gpu_count=gpu_count))

    def _search_gpus(self, query: GpuSearchQuery) -> list[GpuInfo]:
        num_filter: dict = (
            {"eq": query.gpu_count}
            if query.gpu_count is not None
            else {"gte": 1}
        )
        search_body: dict = {
            "num_gpus": num_filter,
            "rentable": {"eq": True},
            "order": [["dph_total", "asc"]],
            "limit": 3000,
        }
        if query.gpu:
            needle = normalize_gpu_search(query.gpu)
            matches = [
                name
                for name in self._gpu_names()
                if needle in normalize_gpu_search(name)
            ]
            if not matches:
                return []
            search_body["gpu_name"] = {"in": matches}
        if query.max_price is not None:
            search_body["dph_total"] = {"lte": query.max_price}
        if query.region:
            search_body["geolocation"] = {
                "eq": query.region.strip().upper(),
            }
        if query.secure_only:
            search_body["verified"] = {"eq": True}
        if query.min_vram_gb is not None:
            search_body["gpu_ram"] = {"gte": query.min_vram_gb * 1024}
        if query.min_download_mbps is not None:
            search_body["inet_down"] = {"gte": query.min_download_mbps}

        data = self._post("bundles/", search_body)
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
        """Fetch Vast.ai's complete GPU-name catalog."""
        data = self._get("gpu_names/unique/")
        return [str(name) for name in data.get("gpu_names", []) if name]

    def _excluded_machines(self) -> set[str]:
        """Machine IDs the user has blocklisted via config.

        ``vastai.exclude_machines`` accepts a list or a comma-separated
        string. The marketplace re-lists broken hosts (e.g. ones that
        never populate authorized_keys) at the top of the price sort, so
        without this a retry rents the identical bad machine.
        """
        raw = cfg.get("vastai.exclude_machines")
        if raw is None:
            return set()
        if isinstance(raw, (list, tuple)):
            return {str(m).strip() for m in raw if str(m).strip()}
        return {m.strip() for m in str(raw).split(",") if m.strip()}

    def create_instance(self, config: CreateConfig) -> Instance:
        gpu_name = resolve_gpu_type(config.gpu_type, self._gpu_names())
        image = config.image or DEFAULT_IMAGE

        disk_gb = max(config.container_disk_gb, config.volume_gb)
        search_body: dict = {
            "gpu_name": {"eq": gpu_name},
            # eq, not gte: a gte match can rent (and bill) more GPUs than
            # asked. list_gpus already uses eq for the same reason.
            "num_gpus": {"eq": config.gpu_count},
            "rentable": {"eq": True},
            "disk_space": {"gte": disk_gb},
            "order": [["dph_total", "asc"]],
            "limit": 10,
        }
        excluded = self._excluded_machines()
        if excluded:
            # Exclusions filter AFTER the fetch; widen it so a blocklist
            # can't starve the candidate pool below the rent loop's needs.
            search_body["limit"] = 10 + len(excluded)
        if str(config.cloud_type).upper() == "SECURE":
            search_body["verification"] = {"eq": "verified"}
        if config.region:
            # Vast matches two-letter country codes, case-sensitively and
            # uppercase (verified against the live search API). Previously
            # --region was silently ignored on this provider.
            search_body["geolocation"] = {"eq": str(config.region).strip().upper()}

        data = self._post("bundles/", search_body)
        offers = data.get("offers", [])

        if excluded:
            offers = [
                o for o in offers if str(o.get("machine_id")) not in excluded
            ]

        if not offers:
            raise RuntimeError(
                f"No Vast.ai offers found for {gpu_name} x{config.gpu_count}"
                + (f" in region {config.region!r}" if config.region else "")
                + (" (after machine blocklist)" if excluded else "")
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
