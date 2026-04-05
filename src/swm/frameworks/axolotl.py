"""Axolotl — LLM fine-tuning framework."""

from swm.frameworks import Framework, Step

FRAMEWORK = Framework(
    name="axolotl",
    label="Axolotl",
    repo="https://github.com/axolotl-ai-cloud/axolotl.git",
    install_dir="/workspace/axolotl",
    launch_cmd="python -m axolotl.cli.train",
    ports={},
    process_pattern="axolotl\\.cli\\.train",
    category="training",
    steps=[
        Step(
            label="Cloning Axolotl",
            command="git clone --depth 1 https://github.com/axolotl-ai-cloud/axolotl.git",
            check="[ -d /workspace/axolotl ]",
            workdir="/workspace",
        ),
        Step(
            label="Installing Axolotl",
            command="pip install -e '.[flash-attn,deepspeed]'",
        ),
    ],
    post_install=[
        Step(
            label="Verifying installation",
            command="python -c 'import axolotl; print(f\"axolotl {axolotl.__version__}\")'",
        ),
    ],
)
