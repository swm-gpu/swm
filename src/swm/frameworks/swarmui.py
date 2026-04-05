"""SwarmUI framework definition."""

from swm.frameworks import Framework, Step

FRAMEWORK = Framework(
    name="swarmui",
    label="SwarmUI",
    repo="https://github.com/mcmonkeyprojects/SwarmUI.git",
    install_dir="/workspace/SwarmUI",
    launch_cmd="bash launch-linux.sh --launch --port 7801",
    ports={7801: "http"},
    category="inference",
    stop_cmd="pkill -f 'SwarmUI.*--port'",
    process_pattern="SwarmUI.*--port",
    steps=[
        Step(
            label="Installing .NET SDK",
            command=(
                "wget -q https://dot.net/v1/dotnet-install.sh -O /tmp/dotnet-install.sh "
                "&& chmod +x /tmp/dotnet-install.sh "
                "&& /tmp/dotnet-install.sh --channel 8.0 --install-dir /usr/share/dotnet "
                "&& ln -sf /usr/share/dotnet/dotnet /usr/bin/dotnet"
            ),
            check="command -v dotnet >/dev/null 2>&1",
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
            command="dotnet build src/SwarmUI.csproj --configuration Release",
        ),
    ],
    post_install=[
        Step(
            label="Installing ComfyUI backend",
            command="mkdir -p /workspace/SwarmUI/dlbackend && git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git",
            check="[ -d /workspace/SwarmUI/dlbackend/ComfyUI ]",
            workdir="/workspace/SwarmUI/dlbackend",
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
            command="git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Manager.git",
            check="[ -d /workspace/SwarmUI/dlbackend/ComfyUI/custom_nodes/ComfyUI-Manager ]",
            workdir="/workspace/SwarmUI/dlbackend/ComfyUI/custom_nodes",
        ),
        Step(
            label="Updating ComfyUI Manager to latest",
            command="git fetch origin main && git reset --hard origin/main",
            workdir="/workspace/SwarmUI/dlbackend/ComfyUI/custom_nodes/ComfyUI-Manager",
        ),
    ],
)
