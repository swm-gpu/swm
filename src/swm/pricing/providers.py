from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GpuSpec:
    name: str
    vram_gb: int
    mem_bandwidth_tbs: float
    bf16_tflops: int
    fp8_tflops: int
    generation_seconds: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class GpuOffering:
    provider: str
    gpu: str
    on_demand: float | None
    spot: float | None = None
    reserved: float | None = None
    min_gpus: int = 1
    instance_type: str = ""
    security: tuple[str, ...] = ()
    stop_resume: bool = False
    idle_cost: str = ""
    notes: str = ""
    estimated: bool = False


GPU_SPECS: dict[str, GpuSpec] = {
    "h200": GpuSpec(
        name="NVIDIA H200 SXM",
        vram_gb=141,
        mem_bandwidth_tbs=4.8,
        bf16_tflops=989,
        fp8_tflops=1979,
        generation_seconds={"480p": 30, "720p": 60, "1080p": 120},
    ),
    "b200": GpuSpec(
        name="NVIDIA B200",
        vram_gb=192,
        mem_bandwidth_tbs=8.0,
        bf16_tflops=2250,
        fp8_tflops=10000,
        generation_seconds={"480p": 12, "720p": 25, "1080p": 50},
    ),
}


# All prices are per-GPU per-hour (USD).
# 8-GPU node prices are divided by 8 for apples-to-apples comparison.

OFFERINGS: list[GpuOffering] = [
    # ── Single-GPU: H200 ────────────────────────────────────────────
    GpuOffering(
        provider="RunPod",
        gpu="h200",
        on_demand=3.59,
        security=("SOC 2 Type II", "HIPAA", "GDPR"),
        stop_resume=True,
        idle_cost="$0.20/GB/mo volume storage",
        notes="Secure Cloud tier; per-second billing",
    ),
    GpuOffering(
        provider="Runcrate",
        gpu="h200",
        on_demand=2.25,
        reserved=1.57,
        security=(),
        notes="~30% reserved discount; verify security certs",
    ),
    GpuOffering(
        provider="Lambda",
        gpu="h200",
        on_demand=None,
        security=("SOC 2 Type II",),
        stop_resume=False,
        idle_cost="$0.20/GB/mo persistent filesystem",
        notes="Single H200 not currently listed; single-tenant isolation",
    ),
    GpuOffering(
        provider="Cudo",
        gpu="h200",
        on_demand=2.50,
        estimated=True,
        notes="Enterprise tier available",
    ),
    # ── Single-GPU: B200 ────────────────────────────────────────────
    GpuOffering(
        provider="RunPod",
        gpu="b200",
        on_demand=4.49,
        estimated=True,
        security=("SOC 2 Type II", "HIPAA", "GDPR"),
        stop_resume=True,
        idle_cost="$0.20/GB/mo volume storage",
        notes="Estimated $3.50-$5.00; Secure Cloud tier",
    ),
    GpuOffering(
        provider="Runcrate",
        gpu="b200",
        on_demand=3.40,
        reserved=2.38,
        notes="~30% reserved discount; verify security certs",
    ),
    GpuOffering(
        provider="Lambda",
        gpu="b200",
        on_demand=5.29,
        security=("SOC 2 Type II",),
        stop_resume=False,
        idle_cost="$0.20/GB/mo persistent filesystem",
        notes="No egress fees; ML frameworks pre-installed",
    ),
    GpuOffering(
        provider="Spheron",
        gpu="b200",
        on_demand=2.25,
        notes="Cheapest listed; verify security certs",
    ),
    GpuOffering(
        provider="Nebius",
        gpu="b200",
        on_demand=5.50,
        notes="Enterprise tier",
    ),
    # ── 8-GPU nodes: H200 ──────────────────────────────────────────
    GpuOffering(
        provider="GCP",
        gpu="h200",
        on_demand=10.85,
        spot=5.44,
        reserved=6.72,
        min_gpus=8,
        instance_type="a3-ultragpu-8g",
        security=("SOC 2", "HIPAA", "ISO 27001"),
        stop_resume=True,
        idle_cost="~$0.04-0.17/GB/mo persistent disk",
        notes="3-year CUD for reserved rate; 1-year CUD ~$9.85/gpu/hr",
    ),
    GpuOffering(
        provider="AWS",
        gpu="h200",
        on_demand=7.91,
        spot=2.29,
        reserved=5.86,
        min_gpus=8,
        instance_type="p5en.48xlarge",
        security=("SOC 2", "HIPAA", "FedRAMP", "PCI DSS"),
        stop_resume=True,
        idle_cost="~$0.08/GB/mo EBS",
        notes="1-year Savings Plan for reserved rate; Nitro Enclaves",
    ),
    GpuOffering(
        provider="Azure",
        gpu="h200",
        on_demand=10.60,
        min_gpus=8,
        instance_type="Standard_ND96isr_H200_v5",
        security=("SOC 2", "HIPAA", "ISO 27001", "FedRAMP High"),
        stop_resume=True,
        idle_cost="~$0.05-0.15/GB/mo managed disk",
        notes="Confidential Computing (TEE); reserved pricing via sales",
    ),
    GpuOffering(
        provider="CoreWeave",
        gpu="h200",
        on_demand=6.30,
        min_gpus=1,
        security=("SOC 2 Type II", "HIPAA"),
        notes="Also available as 8-GPU; Kubernetes-native",
    ),
    # ── 8-GPU nodes: B200 ──────────────────────────────────────────
    GpuOffering(
        provider="GCP",
        gpu="b200",
        on_demand=8.06,
        min_gpus=8,
        instance_type="a4-highgpu-8g",
        security=("SOC 2", "HIPAA", "ISO 27001"),
        stop_resume=True,
        idle_cost="~$0.04-0.17/GB/mo persistent disk",
        estimated=True,
        notes="DWS Flex-start rate only; standard pricing via sales",
    ),
    GpuOffering(
        provider="AWS",
        gpu="b200",
        on_demand=14.24,
        min_gpus=8,
        instance_type="p6-b200.48xlarge",
        security=("SOC 2", "HIPAA", "FedRAMP", "PCI DSS"),
        stop_resume=True,
        idle_cost="~$0.08/GB/mo EBS",
        notes="No Savings Plan or RI published yet",
    ),
    GpuOffering(
        provider="CoreWeave",
        gpu="b200",
        on_demand=10.50,
        min_gpus=1,
        security=("SOC 2 Type II", "HIPAA"),
        notes="NVL variant; Kubernetes-native",
    ),
]
