from __future__ import annotations

from swm import config as cfg
from swm.providers.base import (
    CloudProvider,
    CreateConfig,
    GpuInfo,
    Instance,
    InstanceStatus,
)

GPU_RESOURCE_MAP: dict[str, dict] = {
    "h200": {"resource": "nvidia.com/h200-sxm", "display": "H200 SXM", "vram": 141},
    "b200": {"resource": "nvidia.com/b200-nvl", "display": "B200 NVL", "vram": 192},
    "h100": {"resource": "nvidia.com/h100-sxm", "display": "H100 SXM", "vram": 80},
}

DEFAULT_IMAGE = "nvcr.io/nvidia/pytorch:24.04-py3"

_STATUS = {
    "Pending": InstanceStatus.PENDING,
    "Running": InstanceStatus.RUNNING,
    "Succeeded": InstanceStatus.TERMINATED,
    "Failed": InstanceStatus.TERMINATED,
    "Unknown": InstanceStatus.UNKNOWN,
}


def _k8s():
    try:
        from kubernetes import client as kc, config as kcfg
        return kc, kcfg
    except ImportError:
        raise RuntimeError(
            "kubernetes required for CoreWeave. "
            "Install with: pip install 'swm[coreweave]'"
        )


class CoreWeaveProvider(CloudProvider):
    @property
    def name(self) -> str:
        return "CoreWeave"

    @property
    def slug(self) -> str:
        return "coreweave"

    def _namespace(self) -> str:
        return str(cfg.get("coreweave.namespace", "default"))

    def _api(self):
        kc, kcfg = _k8s()
        kubeconfig = cfg.get("coreweave.kubeconfig")
        if kubeconfig:
            kcfg.load_kube_config(config_file=str(kubeconfig))
        else:
            kcfg.load_kube_config()
        return kc.CoreV1Api()

    def is_configured(self) -> bool:
        try:
            self._api().list_namespaced_pod(namespace=self._namespace(), limit=1)
            return True
        except Exception:
            return False

    def list_instances(self) -> list[Instance]:
        pods = self._api().list_namespaced_pod(
            namespace=self._namespace(), label_selector="swm.managed=true"
        )
        return [self._to_instance(p) for p in pods.items]

    def create_instance(self, config: CreateConfig) -> Instance:
        kc, _ = _k8s()
        spec = GPU_RESOURCE_MAP.get(config.gpu_type)
        if not spec:
            raise RuntimeError(
                f"Unknown GPU type '{config.gpu_type}' for CoreWeave. "
                f"Available: {', '.join(GPU_RESOURCE_MAP)}"
            )

        pod = kc.V1Pod(
            metadata=kc.V1ObjectMeta(
                name=config.name,
                labels={"swm.managed": "true", "swm.gpu": config.gpu_type},
            ),
            spec=kc.V1PodSpec(
                restart_policy="Never",
                containers=[
                    kc.V1Container(
                        name="gpu",
                        image=config.image or DEFAULT_IMAGE,
                        resources=kc.V1ResourceRequirements(
                            limits={spec["resource"]: str(config.gpu_count)},
                        ),
                        ports=[kc.V1ContainerPort(container_port=22)],
                    )
                ],
            ),
        )

        created = self._api().create_namespaced_pod(
            namespace=self._namespace(), body=pod
        )
        return self._to_instance(created)

    def start_instance(self, instance_id: str) -> Instance:
        raise RuntimeError(
            "CoreWeave pods cannot be resumed. Delete and recreate instead."
        )

    def stop_instance(self, instance_id: str) -> Instance:
        raise RuntimeError(
            "CoreWeave pods cannot be stopped. Delete to release resources."
        )

    def terminate_instance(self, instance_id: str) -> bool:
        self._api().delete_namespaced_pod(
            name=instance_id, namespace=self._namespace()
        )
        return True

    def list_gpus(self) -> list[GpuInfo]:
        from swm.pricing.providers import OFFERINGS

        return [
            GpuInfo(
                provider=self.slug,
                type_id=GPU_RESOURCE_MAP.get(o.gpu, {}).get("resource", o.gpu),
                display_name=o.gpu.upper(),
                vram_gb=GPU_RESOURCE_MAP.get(o.gpu, {}).get("vram", 0),
                min_gpu_count=o.min_gpus,
                on_demand_price=o.on_demand,
                stock_level="Available",
                secure_cloud=True,
            )
            for o in OFFERINGS
            if o.provider == "CoreWeave"
        ]

    def _to_instance(self, pod) -> Instance:
        labels = pod.metadata.labels or {}
        gpu_key = labels.get("swm.gpu", "unknown")
        spec = GPU_RESOURCE_MAP.get(gpu_key, {})

        gpu_count = 1
        if pod.spec and pod.spec.containers:
            limits = (pod.spec.containers[0].resources or {})
            if hasattr(limits, "limits") and limits.limits:
                for res in GPU_RESOURCE_MAP.values():
                    if res["resource"] in limits.limits:
                        gpu_count = int(limits.limits[res["resource"]])
                        break

        phase = pod.status.phase if pod.status else "Unknown"

        return Instance(
            provider=self.slug,
            id=pod.metadata.name,
            name=pod.metadata.name,
            gpu_type=spec.get("display", gpu_key),
            gpu_count=gpu_count,
            status=_STATUS.get(phase, InstanceStatus.UNKNOWN),
            ip_address=pod.status.pod_ip if pod.status else None,
        )
