from __future__ import annotations

import json

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
    resolve_gpu_type,
)

API_URL = "https://api.runpod.io/graphql"

DEFAULT_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

_STATUS = {
    "RUNNING": InstanceStatus.RUNNING,
    "EXITED": InstanceStatus.STOPPED,
    "TERMINATED": InstanceStatus.TERMINATED,
    "CREATED": InstanceStatus.PENDING,
}

POD_FIELDS = """
    id name desiredStatus costPerHr gpuCount
    imageName volumeInGb containerDiskInGb
    machine { gpuDisplayName podHostId }
    runtime {
        uptimeInSeconds
        ports { ip isIpPublic privatePort publicPort type }
    }
"""

SSH_RELAY_HOST = "ssh.runpod.io"

_CLOUD_TYPES = ("SECURE", "COMMUNITY", "ALL")


def _gql_str(value: object) -> str:
    """Render a value as a GraphQL string literal, quotes included.

    GraphQL string literals share JSON's escaping rules, so json.dumps
    both quotes and escapes correctly. Interpolating raw f-string values
    let a double quote in a pod name (or any user-controlled field)
    corrupt the whole mutation.
    """
    return json.dumps(str(value))


class RunPodProvider(CloudProvider):
    native_search_fields = frozenset({
        GpuSearchField.GPU_COUNT,
        GpuSearchField.SECURE,
    })

    @property
    def name(self) -> str:
        return "RunPod"

    @property
    def slug(self) -> str:
        return "runpod"

    def is_configured(self) -> bool:
        return cfg.get("runpod.api_key") is not None

    def _api_key(self) -> str:
        key = cfg.get("runpod.api_key")
        if not key:
            raise RuntimeError(
                "RunPod API key not configured. Run: swm config set runpod.api_key <key>"
            )
        return str(key)

    def _gql(self, query: str) -> dict:
        # Bearer header, never a URL query param: URLs land in proxy and
        # server logs and in httpx exception messages; headers do not.
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                API_URL,
                json={"query": query},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key()}",
                },
            )
            resp.raise_for_status()
            body = resp.json()
            if "errors" in body:
                msgs = "; ".join(e.get("message", str(e)) for e in body["errors"])
                raise RuntimeError(f"RunPod API: {msgs}")
            return body["data"]

    # ── queries ─────────────────────────────────────────────────────

    def list_instances(self) -> list[Instance]:
        data = self._gql(f"query {{ myself {{ pods {{ {POD_FIELDS} }} }} }}")
        return [self._to_instance(p) for p in data["myself"]["pods"]]

    def get_instance(self, pod_id: str) -> Instance:
        data = self._gql(
            f"query {{ pod(input: {{podId: {_gql_str(pod_id)}}}) {{ {POD_FIELDS} }} }}"
        )
        return self._to_instance(data["pod"])

    def list_gpus(self, gpu_count: int | None = None) -> list[GpuInfo]:
        return self._search_gpus(GpuSearchQuery(gpu_count=gpu_count))

    def _search_gpus(self, query: GpuSearchQuery) -> list[GpuInfo]:
        n = int(query.gpu_count or 1)
        price_queries = f"""
            securePrice: lowestPrice(input: {{
                gpuCount: {n}, secureCloud: true
            }}) {{
                minimumBidPrice uninterruptablePrice stockStatus
            }}
        """
        if not query.secure_only:
            price_queries += f"""
                communityPrice: lowestPrice(input: {{
                    gpuCount: {n}, secureCloud: false
                }}) {{
                    minimumBidPrice uninterruptablePrice stockStatus
                }}
            """
        data = self._gql(f"""
            query {{
                gpuTypes {{
                    id displayName memoryInGb secureCloud communityCloud
                    {price_queries}
                }}
            }}
        """)

        results = []
        for g in data["gpuTypes"]:
            tiers = [("securePrice", True)]
            if not query.secure_only:
                tiers.append(("communityPrice", False))
            for price_key, secure in tiers:
                if secure and not g.get("secureCloud"):
                    continue
                if not secure and not g.get("communityCloud"):
                    continue
                lp = g.get(price_key) or {}
                # The catalog flags describe possible placement, while a null
                # lowestPrice means no current offer in that tier. Do not show
                # a Secure checkmark for theoretical but unavailable capacity.
                if not lp or not any(value is not None for value in lp.values()):
                    continue
                results.append(
                    GpuInfo(
                        provider=self.slug,
                        type_id=g["id"],
                        display_name=g["displayName"],
                        vram_gb=g.get("memoryInGb", 0),
                        gpu_count=n,
                        on_demand_price=lp.get("uninterruptablePrice"),
                        spot_price=lp.get("minimumBidPrice"),
                        stock_level=lp.get("stockStatus", ""),
                        secure_cloud=secure,
                    )
                )
        return sorted(results, key=lambda g: g.vram_gb, reverse=True)

    # ── mutations ───────────────────────────────────────────────────

    def create_instance(self, config: CreateConfig) -> Instance:
        all_ids = [g["id"] for g in self._gql(
            "query { gpuTypes { id } }"
        )["gpuTypes"]]
        gpu_id = resolve_gpu_type(config.gpu_type, all_ids)
        image = config.image or DEFAULT_IMAGE
        env_entries = ", ".join(
            f"{{ key: {_gql_str(k)}, value: {_gql_str(v)} }}"
            for k, v in config.env.items()
        )

        # cloudType is a GraphQL enum token (unquoted), so it can't be
        # string-escaped — validate against the closed set instead.
        cloud_type = str(config.cloud_type).upper()
        if cloud_type not in _CLOUD_TYPES:
            raise RuntimeError(
                f"Invalid cloud type {config.cloud_type!r}; "
                f"expected one of {', '.join(_CLOUD_TYPES)}"
            )

        dc_line = (
            f"dataCenterId: {_gql_str(config.region)}" if config.region else ""
        )

        data = self._gql(f"""
            mutation {{
                podFindAndDeployOnDemand(input: {{
                    cloudType: {cloud_type}
                    gpuCount: {int(config.gpu_count)}
                    gpuTypeId: {_gql_str(gpu_id)}
                    name: {_gql_str(config.name)}
                    imageName: {_gql_str(image)}
                    volumeInGb: {int(config.volume_gb)}
                    containerDiskInGb: {int(config.container_disk_gb)}
                    volumeMountPath: "/workspace"
                    ports: {_gql_str(config.ports)}
                    env: [{env_entries}]
                    {dc_line}
                }}) {{ {POD_FIELDS} }}
            }}
        """)
        return self._to_instance(data["podFindAndDeployOnDemand"])

    def start_instance(self, instance_id: str) -> Instance:
        # podResume requires gpuCount; hardcoding 1 silently downsized
        # multi-GPU pods on restart. Use the pod's own count — and let a
        # failed lookup propagate rather than guessing 1, which would be
        # the original bug wearing a trenchcoat.
        gpu_count = int(self.get_instance(instance_id).gpu_count or 1)
        data = self._gql(f"""
            mutation {{
                podResume(input: {{ podId: {_gql_str(instance_id)}, gpuCount: {gpu_count} }}) {{
                    {POD_FIELDS}
                }}
            }}
        """)
        return self._to_instance(data["podResume"])

    def stop_instance(self, instance_id: str) -> Instance:
        data = self._gql(f"""
            mutation {{
                podStop(input: {{ podId: {_gql_str(instance_id)} }}) {{
                    id name desiredStatus
                }}
            }}
        """)
        return self._to_instance(data["podStop"])

    def terminate_instance(self, instance_id: str) -> bool:
        self._gql(f"""
            mutation {{
                podTerminate(input: {{ podId: {_gql_str(instance_id)} }})
            }}
        """)
        return True

    # ── helpers ──────────────────────────────────────────────────────

    def _to_instance(self, pod: dict) -> Instance:
        runtime = pod.get("runtime") or {}
        machine = pod.get("machine") or {}
        ports: dict[int, int] = {}
        ip = None

        for p in runtime.get("ports") or []:
            ports[p["privatePort"]] = p["publicPort"]
            if p.get("isIpPublic"):
                ip = p["ip"]

        pod_host_id = machine.get("podHostId")
        ssh_host = SSH_RELAY_HOST if pod_host_id else None
        ssh_user = pod_host_id

        return Instance(
            provider=self.slug,
            id=pod["id"],
            name=pod.get("name", ""),
            gpu_type=machine.get("gpuDisplayName", "unknown"),
            gpu_count=pod.get("gpuCount", 1),
            status=_STATUS.get(pod.get("desiredStatus", ""), InstanceStatus.UNKNOWN),
            cost_per_hr=pod.get("costPerHr"),
            uptime_seconds=runtime.get("uptimeInSeconds"),
            ip_address=ip,
            ssh_host=ssh_host,
            ssh_port=22,
            ssh_user=ssh_user,
            ports=ports,
            image=pod.get("imageName"),
            volume_gb=pod.get("volumeInGb"),
            container_disk_gb=pod.get("containerDiskInGb"),
        )
