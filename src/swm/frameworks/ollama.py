"""Ollama — local LLM inference engine.

Installs the Ollama binary and configures model storage under
``/workspace/ollama`` so weights persist across B2 syncs.  After
install, pull models with ``ollama pull <model>`` over SSH.
"""

from swm.frameworks import Framework, Step

_OLLAMA_HOME = "/workspace/ollama"
_OLLAMA_BIN = "/usr/local/bin/ollama"
_DEFAULT_MODEL = "llama3.2:3b"

FRAMEWORK = Framework(
    name="ollama",
    label="Ollama",
    repo="https://github.com/ollama/ollama",
    install_dir=_OLLAMA_HOME,
    description="Simple LLM engine — best for single-GPU, casual chat",
    launch_cmd=(
        f"OLLAMA_MODELS={_OLLAMA_HOME}/models "
        f"OLLAMA_HOST=0.0.0.0:11434 "
        f"{_OLLAMA_BIN} serve"
    ),
    ports={11434: "http"},
    category="llm",
    stop_cmd="pkill -x ollama 2>/dev/null; sleep 1; echo stopped",
    process_pattern="ollama serve",
    env_setup=f"export OLLAMA_MODELS={_OLLAMA_HOME}/models OLLAMA_HOST=0.0.0.0:11434",
    steps=[
        Step(
            label="Installing Ollama",
            command="curl -fsSL https://ollama.com/install.sh | sh",
            check=f"[ -x {_OLLAMA_BIN} ]",
            workdir="/tmp",
        ),
        Step(
            label="Creating model storage directory",
            command=f"mkdir -p {_OLLAMA_HOME}/models",
            check=f"[ -d {_OLLAMA_HOME}/models ]",
            workdir="/workspace",
        ),
        Step(
            label="Starting Ollama server for model pull",
            command=(
                f"(OLLAMA_MODELS={_OLLAMA_HOME}/models "
                f"OLLAMA_HOST=0.0.0.0:11434 "
                f"{_OLLAMA_BIN} serve < /dev/null > /tmp/ollama-boot.log 2>&1 &); "
                "sleep 3 && curl -sf http://localhost:11434/ > /dev/null"
            ),
        ),
        Step(
            label=f"Pulling default model ({_DEFAULT_MODEL})",
            command=(
                f"OLLAMA_MODELS={_OLLAMA_HOME}/models "
                f"OLLAMA_HOST=0.0.0.0:11434 "
                f"{_OLLAMA_BIN} pull {_DEFAULT_MODEL}"
            ),
        ),
        Step(
            label="Stopping bootstrap Ollama server",
            command="pkill -x ollama 2>/dev/null; sleep 1; echo stopped",
        ),
    ],
    post_install=[
        Step(
            label="Verifying Ollama installation",
            command=f"{_OLLAMA_BIN} --version",
        ),
    ],
    pre_start=[
        Step(
            label="Ensuring Ollama is installed",
            command="curl -fsSL https://ollama.com/install.sh | sh",
            check=f"[ -x {_OLLAMA_BIN} ]",
            workdir="/tmp",
        ),
        Step(
            label="Ensuring model directory exists",
            command=f"mkdir -p {_OLLAMA_HOME}/models",
            check=f"[ -d {_OLLAMA_HOME}/models ]",
            workdir="/workspace",
        ),
    ],
)
