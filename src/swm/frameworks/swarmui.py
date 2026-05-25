"""SwarmUI framework definition."""

from swm.frameworks import Framework, Step, nvidia_ld_exports

_DOTNET_DIR = "/workspace/.dotnet"
_NUGET_DIR = "/workspace/.nuget"
_PIP_CACHE = "/workspace/.cache/pip"
_COMFY_VENV = "/workspace/ComfyUI/venv"

_BUNDLED_COMFY = "/workspace/SwarmUI/dlbackend/ComfyUI"
_COMFY_MODEL_DIRS = [
    "checkpoints", "loras", "vae", "controlnet", "embeddings",
    "clip", "clip_vision", "upscale_models", "unet",
    "diffusion_models", "text_encoders",
]


def _link_bundled_comfy() -> str:
    parts = [
        "mkdir -p /workspace/models/{" + ",".join(_COMFY_MODEL_DIRS) + "}",
        f"mkdir -p {_BUNDLED_COMFY}/models",
    ]
    for d in _COMFY_MODEL_DIRS:
        target = f"{_BUNDLED_COMFY}/models/{d}"
        store = f"/workspace/models/{d}"
        parts.append(
            f"if [ -L {target} ]; then :; "
            f"elif [ -d {target} ]; then "
            f"  ( shopt -s dotglob nullglob; mv {target}/* {store}/ 2>/dev/null || true ); "
            f"  rmdir {target} 2>/dev/null || rm -rf {target}; "
            f"  ln -s {store} {target}; "
            f"else "
            f"  ln -s {store} {target}; "
            f"fi"
        )
    return " && ".join(parts)


_LINK_SWARMUI = _link_bundled_comfy()
_ENV = (
    f"export PATH={_DOTNET_DIR}:$PATH "
    f"DOTNET_ROOT={_DOTNET_DIR} "
    f"NUGET_PACKAGES={_NUGET_DIR} "
    f"PIP_CACHE_DIR={_PIP_CACHE} && "
    f"{nvidia_ld_exports(_COMFY_VENV)}"
)

FRAMEWORK = Framework(
    name="swarmui",
    label="SwarmUI",
    repo="https://github.com/mcmonkeyprojects/SwarmUI.git",
    install_dir="/workspace/SwarmUI",
    launch_cmd="bash launch-linux.sh --launch_mode none --port 7801 --host 0.0.0.0",
    ports={7801: "http"},
    category="inference",
    stop_cmd="pkill -f 'SwarmUI.*--port'",
    process_pattern="SwarmUI.*--port",
    env_setup=_ENV,
    steps=[
        Step(
            label="Installing .NET SDK",
            command=(
                "wget -q https://dot.net/v1/dotnet-install.sh -O /tmp/dotnet-install.sh "
                "&& chmod +x /tmp/dotnet-install.sh "
                f"&& /tmp/dotnet-install.sh --channel 8.0 --install-dir {_DOTNET_DIR}"
            ),
            check=f"[ -x {_DOTNET_DIR}/dotnet ]",
            workdir="/workspace",
        ),
        Step(
            label="Cloning SwarmUI",
            command="git clone --depth 1 https://github.com/mcmonkeyprojects/SwarmUI.git",
            check="[ -d /workspace/SwarmUI ]",
            workdir="/workspace",
        ),
        Step(
            label="Building SwarmUI",
            command=f"{_ENV} && dotnet build src/SwarmUI.csproj --configuration Release",
        ),
    ],
    post_install=[
        Step(
            label="Installing ComfyUI backend",
            command=(
                "mkdir -p dlbackend "
                "&& git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git ComfyUI"
            ),
            check="[ -d /workspace/SwarmUI/dlbackend/ComfyUI ] || [ -L /workspace/SwarmUI/dlbackend/comfyui ]",
            workdir="/workspace/SwarmUI",
        ),
        Step(
            label="Updating ComfyUI to latest",
            command="git fetch origin master && git reset --hard origin/master",
            workdir="/workspace/SwarmUI/dlbackend/ComfyUI",
        ),
        Step(
            label="Installing ComfyUI requirements",
            command="pip install --no-cache-dir -r requirements.txt",
            workdir="/workspace/SwarmUI/dlbackend/ComfyUI",
        ),
        Step(
            label="Installing ComfyUI Manager",
            command=(
                "mkdir -p custom_nodes "
                "&& git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Manager.git ComfyUI-Manager"
            ),
            check="[ -d /workspace/SwarmUI/dlbackend/ComfyUI/custom_nodes/ComfyUI-Manager ]",
            workdir="/workspace/SwarmUI/dlbackend/ComfyUI",
        ),
        Step(
            label="Updating ComfyUI Manager to latest",
            command="git fetch origin main && git reset --hard origin/main",
            workdir="/workspace/SwarmUI/dlbackend/ComfyUI/custom_nodes/ComfyUI-Manager",
        ),
        Step(
            label="Linking model directories to unified store",
            command=_LINK_SWARMUI,
            check=f"[ -L {_BUNDLED_COMFY}/models/checkpoints ]",
        ),
    ],
    pre_start=[
        Step(
            label="Fixing execute permissions",
            command=(
                "find /workspace/SwarmUI -maxdepth 2 -name '*.sh' -exec chmod +x {} + "
                "&& chmod -R +x /workspace/SwarmUI/launchtools/ "
                "&& chmod +x /workspace/SwarmUI/src/bin/live_release/SwarmUI 2>/dev/null || true"
            ),
        ),
        Step(
            label="Migrating caches to /workspace",
            command=(
                f"mkdir -p {_NUGET_DIR} {_PIP_CACHE} /root/.nuget /root/.cache "
                "&& if [ -d /root/.nuget/packages ] && [ ! -L /root/.nuget/packages ]; then "
                f"cp -rn /root/.nuget/packages/* {_NUGET_DIR}/ 2>/dev/null; "
                "rm -rf /root/.nuget/packages; fi "
                f"&& ln -sfn {_NUGET_DIR} /root/.nuget/packages "
                "&& if [ -d /root/.cache/pip ] && [ ! -L /root/.cache/pip ]; then "
                "rm -rf /root/.cache/pip; fi "
                f"&& ln -sfn {_PIP_CACHE} /root/.cache/pip"
            ),
            check=f"[ -L /root/.nuget/packages ] && [ -L /root/.cache/pip ]",
        ),
        Step(
            label="Ensuring .NET SDK is available",
            command=(
                "wget -q https://dot.net/v1/dotnet-install.sh -O /tmp/dotnet-install.sh "
                "&& chmod +x /tmp/dotnet-install.sh "
                f"&& /tmp/dotnet-install.sh --channel 8.0 --install-dir {_DOTNET_DIR}"
            ),
            check=f"[ -x {_DOTNET_DIR}/dotnet ]",
            workdir="/workspace",
        ),
        Step(
            label="Linking ComfyUI backend",
            command=(
                "mkdir -p /workspace/SwarmUI/dlbackend "
                "&& ln -sfn /workspace/ComfyUI /workspace/SwarmUI/dlbackend/comfyui"
            ),
            check="[ -d /workspace/SwarmUI/dlbackend/ComfyUI ] || [ -L /workspace/SwarmUI/dlbackend/comfyui ]",
        ),
        Step(
            label="Ensuring model directory symlinks",
            command=_LINK_SWARMUI,
            check=f"[ -L {_BUNDLED_COMFY}/models/checkpoints ]",
        ),
    ],
)
