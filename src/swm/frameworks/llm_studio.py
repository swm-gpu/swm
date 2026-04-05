"""H2O LLM Studio — no-code LLM fine-tuning UI."""

from swm.frameworks import Framework, Step

FRAMEWORK = Framework(
    name="llm-studio",
    label="H2O LLM Studio",
    repo="https://github.com/h2oai/h2o-llmstudio.git",
    install_dir="/workspace/h2o-llmstudio",
    launch_cmd="make llmstudio",
    ports={10101: "http"},
    category="training",
    stop_cmd="pkill -f 'wave.*llmstudio'",
    process_pattern="wave.*llmstudio",
    steps=[
        Step(
            label="Cloning H2O LLM Studio",
            command="git clone --depth 1 https://github.com/h2oai/h2o-llmstudio.git",
            check="[ -d /workspace/h2o-llmstudio ]",
            workdir="/workspace",
        ),
        Step(
            label="Installing LLM Studio",
            command="pip install -r requirements.txt",
        ),
    ],
    post_install=[
        Step(
            label="Verifying installation",
            command="python -c 'import llm_studio; print(\"LLM Studio OK\")'",
        ),
    ],
)
