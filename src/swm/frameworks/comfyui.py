"""ComfyUI framework definition."""

from swm.frameworks import Framework, Step

FRAMEWORK = Framework(
    name="comfyui",
    label="ComfyUI",
    repo="https://github.com/comfyanonymous/ComfyUI.git",
    install_dir="/workspace/ComfyUI",
    launch_cmd="python main.py --listen 0.0.0.0 --port 8188",
    ports={8188: "http"},
    category="inference",
    stop_cmd="pkill -f 'python main.py.*--port 8188'",
    process_pattern="python main.py.*--listen",
    steps=[
        Step(
            label="Cloning ComfyUI",
            command="git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git",
            check="[ -d /workspace/ComfyUI ]",
            workdir="/workspace",
        ),
        Step(
            label="Installing Python requirements",
            command="pip install -r requirements.txt",
        ),
    ],
    post_install=[
        Step(
            label="Installing ComfyUI Manager",
            command="git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Manager.git",
            check="[ -d /workspace/ComfyUI/custom_nodes/ComfyUI-Manager ]",
            workdir="/workspace/ComfyUI/custom_nodes",
        ),
        Step(
            label="Creating model directories",
            command=(
                "mkdir -p /workspace/ComfyUI/models/"
                "{checkpoints,loras,vae,controlnet,clip,upscale_models,unet,"
                "diffusion_models,text_encoders,clip_vision}"
            ),
        ),
    ],
)
