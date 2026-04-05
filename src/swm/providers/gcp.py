from __future__ import annotations

import json
import shutil
import subprocess

from swm import config as cfg
from swm.providers.base import (
    CloudProvider,
    CreateConfig,
    GpuInfo,
    Instance,
    InstanceStatus,
)

GPU_MACHINE_MAP: dict[str, dict] = {
    "h200": {"machine": "a3-ultragpu-8g", "gpus": 8, "display": "H200 SXM", "vram": 141},
    "b200": {"machine": "a4-highgpu-8g", "gpus": 8, "display": "B200", "vram": 192},
    "h100": {"machine": "a3-highgpu-8g", "gpus": 8, "display": "H100 SXM", "vram": 80},
}

_MACHINE_TO_GPU = {v["machine"]: k for k, v in GPU_MACHINE_MAP.items()}

_STATUS = {
    "PROVISIONING": InstanceStatus.PENDING,
    "STAGING": InstanceStatus.PENDING,
    "RUNNING": InstanceStatus.RUNNING,
    "STOPPING": InstanceStatus.STOPPED,
    "SUSPENDING": InstanceStatus.STOPPED,
    "SUSPENDED": InstanceStatus.STOPPED,
    "TERMINATED": InstanceStatus.STOPPED,
}

DEFAULT_IMAGE_FAMILY = "common-cu124-debian-12-py311"
DEFAULT_IMAGE_PROJECT = "ml-images"


def _gcloud() -> str:
    path = shutil.which("gcloud")
    if not path:
        raise RuntimeError(
            "gcloud CLI not found on PATH. "
            "Install from https://cloud.google.com/sdk/docs/install"
        )
    return path


def _run(args: list[str], check: bool = True) -> str:
    """Execute a gcloud command and return stdout."""
    result = subprocess.run(
        [_gcloud(), *args, "--format=json"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"gcloud error: {err}")
    return result.stdout


def _run_json(args: list[str]) -> list | dict:
    raw = _run(args)
    if not raw.strip():
        return []
    return json.loads(raw)


class GCPProvider(CloudProvider):
    @property
    def name(self) -> str:
        return "GCP"

    @property
    def slug(self) -> str:
        return "gcp"

    def _project(self) -> str:
        project = cfg.get("gcp.project")
        if not project:
            raise RuntimeError(
                "GCP project not set. Run: swm config set gcp.project <id>"
            )
        return str(project)

    def _zone(self) -> str:
        return str(cfg.get("gcp.zone", "us-central1-a"))

    def _base_args(self) -> list[str]:
        return [f"--project={self._project()}", f"--zone={self._zone()}"]

    def is_configured(self) -> bool:
        if not cfg.get("gcp.project"):
            return False
        try:
            _gcloud()
            result = subprocess.run(
                [_gcloud(), "auth", "list", "--format=json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            accounts = json.loads(result.stdout) if result.stdout.strip() else []
            return any(a.get("status") == "ACTIVE" for a in accounts)
        except Exception:
            return False

    # ── queries ─────────────────────────────────────────────────────

    def list_instances(self) -> list[Instance]:
        data = _run_json([
            "compute", "instances", "list",
            f"--project={self._project()}",
        ])
        if isinstance(data, dict):
            data = data.get("items", [])
        return [self._to_instance(i) for i in data if isinstance(i, dict)]

    def list_gpus(self) -> list[GpuInfo]:
        from swm.pricing.providers import OFFERINGS

        static = [
            GpuInfo(
                provider=self.slug,
                type_id=GPU_MACHINE_MAP.get(o.gpu, {}).get("machine", o.gpu),
                display_name=f"{o.gpu.upper()} ({GPU_MACHINE_MAP.get(o.gpu, {}).get('machine', '?')})",
                vram_gb=GPU_MACHINE_MAP.get(o.gpu, {}).get("vram", 0),
                min_gpu_count=o.min_gpus,
                on_demand_price=o.on_demand,
                spot_price=o.spot,
                stock_level="Available",
                secure_cloud=True,
            )
            for o in OFFERINGS
            if o.provider == "GCP"
        ]

        try:
            accel_data = _run_json([
                "compute", "accelerator-types", "list",
                f"--project={self._project()}",
            ])
            if isinstance(accel_data, list):
                for a in accel_data:
                    aname = a.get("name", "")
                    desc = a.get("description", "")
                    zone = (a.get("zone") or "").rsplit("/", 1)[-1]
                    already = any(
                        aname in g.type_id or desc in g.display_name
                        for g in static
                    )
                    if not already and ("h200" in aname.lower() or "b200" in aname.lower()):
                        static.append(
                            GpuInfo(
                                provider=self.slug,
                                type_id=aname,
                                display_name=f"{desc} ({zone})",
                                vram_gb=0,
                                stock_level="Available",
                                secure_cloud=True,
                            )
                        )
        except Exception:
            pass

        return static

    # ── mutations ───────────────────────────────────────────────────

    def create_instance(self, config: CreateConfig) -> Instance:
        spec = GPU_MACHINE_MAP.get(config.gpu_type)
        if not spec:
            raise RuntimeError(
                f"Unknown GPU type '{config.gpu_type}' for GCP. "
                f"Available: {', '.join(GPU_MACHINE_MAP)}"
            )

        zone = config.region or self._zone()
        args = [
            "compute", "instances", "create", config.name,
            f"--project={self._project()}",
            f"--zone={zone}",
            f"--machine-type={spec['machine']}",
            f"--boot-disk-size={config.volume_gb}GB",
            "--boot-disk-auto-delete",
        ]

        if config.image:
            args.append(f"--image={config.image}")
        else:
            args.extend([
                f"--image-family={DEFAULT_IMAGE_FAMILY}",
                f"--image-project={DEFAULT_IMAGE_PROJECT}",
            ])

        data = _run_json(args)
        instances = data if isinstance(data, list) else [data]
        return self._to_instance(instances[0])

    def start_instance(self, instance_id: str) -> Instance:
        _run([
            "compute", "instances", "start", instance_id,
            *self._base_args(),
        ])
        return self._get_instance(instance_id)

    def stop_instance(self, instance_id: str) -> Instance:
        _run([
            "compute", "instances", "stop", instance_id,
            *self._base_args(),
        ])
        return self._get_instance(instance_id)

    def terminate_instance(self, instance_id: str) -> bool:
        _run([
            "compute", "instances", "delete", instance_id,
            *self._base_args(), "--quiet",
        ])
        return True

    # ── helpers ──────────────────────────────────────────────────────

    def _get_instance(self, name: str) -> Instance:
        data = _run_json([
            "compute", "instances", "describe", name,
            *self._base_args(),
        ])
        return self._to_instance(data)

    def _to_instance(self, inst: dict) -> Instance:
        machine_type = (inst.get("machineType") or "").rsplit("/", 1)[-1]
        gpu_key = _MACHINE_TO_GPU.get(machine_type, "")
        spec = GPU_MACHINE_MAP.get(gpu_key, {})

        ip = None
        for iface in inst.get("networkInterfaces") or []:
            for ac in iface.get("accessConfigs") or []:
                if ac.get("natIP"):
                    ip = ac["natIP"]
                    break

        zone_full = inst.get("zone") or ""
        zone_short = zone_full.rsplit("/", 1)[-1] if zone_full else None

        return Instance(
            provider=self.slug,
            id=inst.get("name", ""),
            name=inst.get("name", ""),
            gpu_type=spec.get("display", machine_type),
            gpu_count=spec.get("gpus", 1),
            status=_STATUS.get(inst.get("status", ""), InstanceStatus.UNKNOWN),
            ip_address=ip,
            ssh_host=ip,
            ssh_port=22,
            region=zone_short,
            image=(inst.get("disks") or [{}])[0]
                .get("source", "")
                .rsplit("/", 1)[-1] if inst.get("disks") else None,
        )
