"""Framework registry — data-driven installer for GPU workloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module


@dataclass
class Step:
    """A single install / setup step executed on the remote pod."""

    label: str
    command: str
    check: str | None = None
    workdir: str | None = None


@dataclass
class Usage:
    """One way to talk to a running framework, renderable by any client.

    ``command`` templates take ``{base_url}`` (scheme://host[:port] of the
    framework's HTTP endpoint — a tunnel URL on swm.cloud, host:port over
    plain swm) and ``{model}`` (the launch model, when one is configured).
    Rendering is the client's job because only the client knows its routing.
    """

    label: str
    kind: str  # "browser" | "curl" | "openai" | "cli"
    command: str = ""
    description: str = ""


@dataclass
class Framework:
    """Declarative description of an installable framework."""

    name: str
    label: str
    repo: str
    install_dir: str
    launch_cmd: str
    ports: dict[int, str] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    post_install: list[Step] = field(default_factory=list)
    pre_start: list[Step] = field(default_factory=list)
    env_setup: str = ""
    stop_cmd: str = ""
    process_pattern: str = ""
    category: str = "inference"
    description: str = ""

    # What answers on the framework's port. "ui" opens in a browser; "api"
    # serves programmatic clients only, so a browser link is a dead end (an
    # Ollama root answers with a plain-text banner, a vLLM root with JSON);
    # "none" listens on nothing and is driven over SSH.
    access: str = "ui"  # "ui" | "api" | "none"

    # How to actually use it once started, in preference order. Empty means
    # "open the URL", which remains the right default for every web UI.
    usage: list[Usage] = field(default_factory=list)

    # Asset types (swm.models.resolver vocabulary) this framework can load
    # from the unified store at /workspace/models/. Empty means it consumes
    # no models — Open WebUI fronts other engines rather than loading
    # weights itself. This is the single source of truth for "which engine
    # can use this model": the model layer never names frameworks.
    consumes: frozenset[str] = frozenset()

    # Shell probe for "is this framework present on the pod". Empty derives
    # a default from install_dir. Override when presence is not equivalent
    # to the install dir existing (Ollama's binary lives in /usr/local/bin;
    # a vLLM venv can exist half-built without its entrypoint).
    installed_check: str = ""

    # Absolute path to the Python venv this framework owns, or None for
    # frameworks that don't need a venv (e.g. Go binaries like Ollama).
    # When set, swm will ensure workspace-owned Python + uv exist and
    # repair the venv on host changes before any install/start step.
    venv: str | None = None

    @property
    def launch_workdir(self) -> str:
        return self.install_dir

    @property
    def installed_probe(self) -> str:
        """Shell that exits 0 when this framework is present on the pod."""
        return self.installed_check or f"[ -d {self.install_dir} ]"


def nvidia_ld_exports(venv: str) -> str:
    """Return a bash snippet that prepends a venv's bundled NVIDIA libs to LD_LIBRARY_PATH.

    Recent torch wheels pip-install companion packages (``nvidia-cuda-cupti-cu12``,
    ``nvidia-cudnn-cu12``, ``nvidia-cublas-cu12``, …) under
    ``<venv>/lib/python*/site-packages/nvidia/*/lib/`` and expect the dynamic
    linker to pick them up. On many pod images an older system libcupti /
    libcudnn ships in ``/usr/local/cuda/lib64``, gets resolved first, and
    crashes torch with errors like::

        ImportError: ... undefined symbol: cuptiActivityEnableDriverApi,
        version libcupti.so.12

    Prepending the venv-local NVIDIA lib dirs fixes that without touching the
    image. No-op when the venv has no bundled libs.
    """
    return (
        f'NVLIBS=$(ls -d {venv}/lib/python*/site-packages/nvidia/*/lib 2>/dev/null '
        f'| tr "\\n" ":" | sed "s/:$//"); '
        f'if [ -n "$NVLIBS" ]; then export LD_LIBRARY_PATH="$NVLIBS:${{LD_LIBRARY_PATH:-}}"; fi'
    )


_FRAMEWORK_MODULES = [
    "comfyui",
    "swarmui",
    "axolotl",
    "llm_studio",
    "ollama",
    "open_webui",
    "vllm_server",
]

_registry: dict[str, Framework] | None = None


def _load_registry() -> dict[str, Framework]:
    global _registry
    if _registry is not None:
        return _registry
    _registry = {}
    for mod_name in _FRAMEWORK_MODULES:
        mod = import_module(f"swm.frameworks.{mod_name}")
        fw: Framework = mod.FRAMEWORK
        _registry[fw.name] = fw
    return _registry


def render_usage(fw: Framework, base_url: str, model: str | None = None) -> list[Usage]:
    """Materialize a framework's usage entries against a concrete endpoint.

    ``base_url`` is wherever this client reaches the framework — a tunnel URL
    on swm.cloud, ``http://host:port`` over plain swm — and is stripped of any
    trailing slash so templates can safely write ``{base_url}/v1``. Entries
    that need a model are dropped rather than rendered with a placeholder:
    a snippet that cannot run as pasted is worse than no snippet.
    """
    base = base_url.rstrip("/")
    out: list[Usage] = []
    for u in fw.usage:
        if "{model}" in u.command and not model:
            continue
        out.append(Usage(
            label=u.label,
            kind=u.kind,
            command=u.command.replace("{base_url}", base).replace("{model}", model or ""),
            description=u.description,
        ))
    return out


def consumers_of(asset_type: str) -> list[Framework]:
    """Frameworks that can load models of *asset_type* from the unified store.

    This query replaces the tables that used to hardcode framework names in
    the model layer (resolver's needs_engine map, the CLI's probe dict): the
    declaration lives on each framework, and everything downstream asks.

    Serving frameworks sort before training ones. Callers that surface a
    single suggestion take the first entry, and "install vLLM to run this
    LLM" is the right default; suggesting the fine-tuner would be technically
    true and practically wrong.
    """
    hits = [fw for fw in list_frameworks() if asset_type in fw.consumes]
    return sorted(hits, key=lambda fw: fw.category == "training")


def get_framework(name: str) -> Framework:
    reg = _load_registry()
    if name not in reg:
        avail = ", ".join(sorted(reg))
        raise KeyError(f"Unknown framework '{name}'. Available: {avail}")
    return reg[name]


def list_frameworks() -> list[Framework]:
    return list(_load_registry().values())
