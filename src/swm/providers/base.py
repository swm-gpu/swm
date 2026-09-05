from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class InstanceStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    PENDING = "pending"
    TERMINATED = "terminated"
    UNKNOWN = "unknown"


STATUS_STYLES: dict[InstanceStatus, str] = {
    InstanceStatus.RUNNING: "green",
    InstanceStatus.STOPPED: "yellow",
    InstanceStatus.PENDING: "blue",
    InstanceStatus.TERMINATED: "red",
    InstanceStatus.UNKNOWN: "dim",
}


@dataclass
class GpuInfo:
    """Normalized GPU availability info from a provider."""

    provider: str
    type_id: str
    display_name: str
    vram_gb: int
    gpu_count: int = 1
    on_demand_price: float | None = None
    spot_price: float | None = None
    stock_level: str = ""
    secure_cloud: bool = False
    regions: list[str] = field(default_factory=list)
    upload_mbps: float | None = None
    download_mbps: float | None = None


class GpuSearchField(str, Enum):
    GPU = "gpu"
    GPU_COUNT = "gpu_count"
    MAX_PRICE = "max_price"
    REGION = "region"
    SECURE = "secure"
    MIN_VRAM = "min_vram"
    MIN_DOWNLOAD = "min_download"


@dataclass(frozen=True)
class GpuSearchQuery:
    """Provider-neutral constraints for GPU searches."""

    gpu: str | None = None
    gpu_count: int | None = None
    max_price: float | None = None
    region: str | None = None
    secure_only: bool = False
    min_vram_gb: float | None = None
    min_download_mbps: float | None = None

    @property
    def active_fields(self) -> frozenset[GpuSearchField]:
        fields: set[GpuSearchField] = set()
        if self.gpu:
            fields.add(GpuSearchField.GPU)
        if self.gpu_count is not None:
            fields.add(GpuSearchField.GPU_COUNT)
        if self.max_price is not None:
            fields.add(GpuSearchField.MAX_PRICE)
        if self.region:
            fields.add(GpuSearchField.REGION)
        if self.secure_only:
            fields.add(GpuSearchField.SECURE)
        if self.min_vram_gb is not None:
            fields.add(GpuSearchField.MIN_VRAM)
        if self.min_download_mbps is not None:
            fields.add(GpuSearchField.MIN_DOWNLOAD)
        return frozenset(fields)


class UnsupportedSearchFilters(ValueError):
    """Raised when a provider cannot guarantee one or more search filters."""


@dataclass
class Instance:
    """Unified representation of a GPU instance across all providers."""

    provider: str
    id: str
    name: str
    gpu_type: str
    gpu_count: int
    status: InstanceStatus
    cost_per_hr: float | None = None
    uptime_seconds: int | None = None
    region: str | None = None
    ip_address: str | None = None
    ssh_host: str | None = None
    ssh_port: int | None = None
    ssh_user: str | None = None
    ports: dict[int, int] = field(default_factory=dict)
    image: str | None = None
    volume_gb: int | None = None
    container_disk_gb: int | None = None
    status_detail: str | None = None

    @property
    def qualified_id(self) -> str:
        return f"{self.provider}:{self.id}"

    @property
    def uptime_display(self) -> str:
        if self.uptime_seconds is None:
            return "—"
        h, rem = divmod(self.uptime_seconds, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m}m" if h else f"{m}m"

    @property
    def ssh_command(self) -> str | None:
        if self.ip_address and self.ports.get(22):
            return f"ssh root@{self.ip_address} -p {self.ports[22]}"
        if not self.ssh_host:
            return None
        user = self.ssh_user or "root"
        port_flag = f" -p {self.ssh_port}" if self.ssh_port and self.ssh_port != 22 else ""
        return f"ssh {user}@{self.ssh_host}{port_flag}"

    @property
    def status_rich(self) -> str:
        c = STATUS_STYLES.get(self.status, "dim")
        return f"[{c}]{self.status.value}[/{c}]"


@dataclass
class CreateConfig:
    """Provider-agnostic configuration for creating an instance."""

    name: str
    gpu_type: str = "h200"
    gpu_count: int = 1
    volume_gb: int = 100
    container_disk_gb: int = 40
    image: str = ""
    region: str | None = None
    cloud_type: str = "SECURE"
    ports: str = "22/tcp,8888/http,8188/http"
    env: dict[str, str] = field(default_factory=dict)


def _normalize(s: str) -> str:
    """Lowercase, strip non-alphanumeric (except dots), collapse spaces."""
    return re.sub(r"[^a-z0-9.]+", " ", s.lower()).strip()


def normalize_gpu_search(s: str) -> str:
    """Normalize GPU text so compact CLI names match provider display names."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def resolve_gpu_type(needle: str, candidates: list[str]) -> str:
    """Fuzzy-match a user-supplied GPU name against a list of real type IDs.

    Matching strategy (first wins):
      1. Exact match (case-insensitive)
      2. Candidate contains the full needle as a substring
      3. All tokens in the needle appear somewhere in the candidate

    Among substring/token matches, prefers the shortest candidate
    (most specific).  Raises RuntimeError with suggestions on failure.
    """
    if not candidates:
        raise RuntimeError("No GPU types available from this provider.")

    norm_needle = _normalize(needle)
    tokens = norm_needle.split()

    # Pass 1: exact match (case-insensitive)
    for c in candidates:
        if _normalize(c) == norm_needle:
            return c

    # Pass 2: substring containment
    substr_hits = [c for c in candidates if norm_needle in _normalize(c)]
    if substr_hits:
        return min(substr_hits, key=len)

    # Pass 3: all tokens present
    token_hits = [
        c for c in candidates
        if all(t in _normalize(c) for t in tokens)
    ]
    if token_hits:
        return min(token_hits, key=len)

    # No match — suggest closest options
    suggestions = ", ".join(sorted(candidates)[:15])
    raise RuntimeError(
        f"No GPU matching '{needle}' found on this provider.\n"
        f"  Available: {suggestions}"
    )


class CloudProvider(ABC):
    """Interface that every cloud GPU provider must implement."""

    native_search_fields = frozenset({GpuSearchField.GPU_COUNT})
    local_search_fields = frozenset({
        GpuSearchField.GPU,
        GpuSearchField.GPU_COUNT,
        GpuSearchField.MAX_PRICE,
        GpuSearchField.REGION,
        GpuSearchField.SECURE,
        GpuSearchField.MIN_VRAM,
    })

    def search_gpus(self, query: GpuSearchQuery) -> list[GpuInfo]:
        """Search GPUs while refusing constraints the provider cannot honor."""
        supported = self.native_search_fields | self.local_search_fields
        unsupported = query.active_fields - supported
        if unsupported:
            names = ", ".join(sorted(field.value for field in unsupported))
            raise UnsupportedSearchFilters(f"unsupported filters: {names}")

        results = self._search_gpus(query)
        return self._apply_local_search_filters(results, query)

    def _search_gpus(self, query: GpuSearchQuery) -> list[GpuInfo]:
        return self.list_gpus(gpu_count=query.gpu_count)

    @staticmethod
    def _apply_local_search_filters(
        results: list[GpuInfo],
        query: GpuSearchQuery,
    ) -> list[GpuInfo]:
        if query.gpu:
            needle = normalize_gpu_search(query.gpu)
            results = [
                gpu
                for gpu in results
                if needle in normalize_gpu_search(gpu.display_name)
                or needle in normalize_gpu_search(gpu.type_id)
            ]
        if query.gpu_count is not None:
            results = [gpu for gpu in results if gpu.gpu_count == query.gpu_count]
        if query.max_price is not None:
            results = [
                gpu
                for gpu in results
                if gpu.on_demand_price is not None
                and gpu.on_demand_price <= query.max_price
            ]
        if query.region:
            needle = query.region.lower()
            results = [
                gpu
                for gpu in results
                if any(needle in region.lower() for region in gpu.regions)
            ]
        if query.secure_only:
            results = [gpu for gpu in results if gpu.secure_cloud]
        if query.min_vram_gb is not None:
            results = [gpu for gpu in results if gpu.vram_gb >= query.min_vram_gb]
        return results

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def slug(self) -> str: ...

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def list_instances(self) -> list[Instance]: ...

    @abstractmethod
    def create_instance(self, config: CreateConfig) -> Instance: ...

    @abstractmethod
    def start_instance(self, instance_id: str) -> Instance: ...

    @abstractmethod
    def stop_instance(self, instance_id: str) -> Instance: ...

    @abstractmethod
    def terminate_instance(self, instance_id: str) -> bool: ...

    @abstractmethod
    def list_gpus(self, gpu_count: int | None = None) -> list[GpuInfo]: ...
