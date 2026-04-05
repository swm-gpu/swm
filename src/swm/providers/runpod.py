from __future__ import annotations

import httpx

from swm import config as cfg
from swm.providers.base import (
    CloudProvider,
    CreateConfig,
    GpuInfo,
    Instance,
    InstanceStatus,
)

API_URL = "https://api.runpod.io/graphql"

GPU_TYPE_MAP = {
    "h200": "NVIDIA H200",
    "h200_nvl": "NVIDIA H200 NVL",
    "h100_sxm": "NVIDIA H100 80GB HBM3",
    "h100_nvl": "NVIDIA H100 NVL",
    "h100_pcie": "NVIDIA H100 PCIe",
    "a100_80": "NVIDIA A100 80GB PCIe",
    "a100_sxm": "NVIDIA A100-SXM4-80GB",
    "l40s": "NVIDIA L40S",
    "rtx_4090": "NVIDIA GeForce RTX 4090",
}

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


class RunPodProvider(CloudProvider):
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
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                API_URL,
                params={"api_key": self._api_key()},
                json={"query": query},
                headers={"Content-Type": "application/json"},
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
            f'query {{ pod(input: {{podId: "{pod_id}"}}) {{ {POD_FIELDS} }} }}'
        )
        return self._to_instance(data["pod"])

    def list_gpus(self) -> list[GpuInfo]:
        data = self._gql("""
            query {
                gpuTypes {
                    id displayName memoryInGb secureCloud communityCloud
                    lowestPrice(input: { gpuCount: 1 }) {
                        minimumBidPrice uninterruptablePrice stockStatus
                    }
                }
            }
        """)
        results = []
        for g in data["gpuTypes"]:
            lp = g.get("lowestPrice") or {}
            results.append(
                GpuInfo(
                    provider=self.slug,
                    type_id=g["id"],
                    display_name=g["displayName"],
                    vram_gb=g.get("memoryInGb", 0),
                    on_demand_price=lp.get("uninterruptablePrice"),
                    spot_price=lp.get("minimumBidPrice"),
                    stock_level=lp.get("stockStatus", ""),
                    secure_cloud=bool(g.get("secureCloud")),
                )
            )
        return sorted(results, key=lambda g: g.vram_gb, reverse=True)

    # ── mutations ───────────────────────────────────────────────────

    def create_instance(self, config: CreateConfig) -> Instance:
        gpu_id = GPU_TYPE_MAP.get(config.gpu_type, config.gpu_type)
        image = config.image or DEFAULT_IMAGE
        env_entries = ", ".join(
            f'{{ key: "{k}", value: "{v}" }}' for k, v in config.env.items()
        )

        dc_line = f'dataCenterId: "{config.region}"' if config.region else ""

        data = self._gql(f"""
            mutation {{
                podFindAndDeployOnDemand(input: {{
                    cloudType: {config.cloud_type}
                    gpuCount: {config.gpu_count}
                    gpuTypeId: "{gpu_id}"
                    name: "{config.name}"
                    imageName: "{image}"
                    volumeInGb: {config.volume_gb}
                    containerDiskInGb: {config.container_disk_gb}
                    volumeMountPath: "/workspace"
                    ports: "{config.ports}"
                    env: [{env_entries}]
                    {dc_line}
                }}) {{ {POD_FIELDS} }}
            }}
        """)
        return self._to_instance(data["podFindAndDeployOnDemand"])

    def start_instance(self, instance_id: str) -> Instance:
        data = self._gql(f"""
            mutation {{
                podResume(input: {{ podId: "{instance_id}", gpuCount: 1 }}) {{
                    {POD_FIELDS}
                }}
            }}
        """)
        return self._to_instance(data["podResume"])

    def stop_instance(self, instance_id: str) -> Instance:
        data = self._gql(f"""
            mutation {{
                podStop(input: {{ podId: "{instance_id}" }}) {{
                    id name desiredStatus
                }}
            }}
        """)
        return self._to_instance(data["podStop"])

    def terminate_instance(self, instance_id: str) -> bool:
        self._gql(f"""
            mutation {{
                podTerminate(input: {{ podId: "{instance_id}" }})
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
