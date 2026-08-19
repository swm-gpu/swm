"""Ollama — local LLM inference engine.

Installs the Ollama binary and configures model storage under
``/workspace/ollama`` so weights persist across B2 syncs.  After
install, pull models with ``ollama pull <model>`` over SSH.
"""

from swm.frameworks import Framework, Step, Usage

_OLLAMA_HOME = "/workspace/ollama"
_OLLAMA_MODELS_DIR = f"{_OLLAMA_HOME}/models"
_UNIFIED_OLLAMA = "/workspace/models/ollama"
_OLLAMA_BIN = "/usr/local/bin/ollama"
_DEFAULT_MODEL = "llama3.2:3b"

_LINK_OLLAMA_STORE = (
    f"mkdir -p {_OLLAMA_HOME} {_UNIFIED_OLLAMA} && "
    f"if [ -L {_OLLAMA_MODELS_DIR} ]; then :; "
    f"elif [ -d {_OLLAMA_MODELS_DIR} ]; then "
    f"  ( shopt -s dotglob nullglob; "
    f"    mv {_OLLAMA_MODELS_DIR}/* {_UNIFIED_OLLAMA}/ 2>/dev/null || true ); "
    f"  rmdir {_OLLAMA_MODELS_DIR} 2>/dev/null || rm -rf {_OLLAMA_MODELS_DIR}; "
    f"  ln -s {_UNIFIED_OLLAMA} {_OLLAMA_MODELS_DIR}; "
    f"else "
    f"  ln -s {_UNIFIED_OLLAMA} {_OLLAMA_MODELS_DIR}; "
    f"fi"
)

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
    consumes=frozenset({"ollama", "llm-gguf"}),
    # install_dir is only the model home; the binary is the real presence
    # signal and lives outside /workspace.
    installed_check="[ -x /usr/local/bin/ollama ]",
    # Ollama's root answers "Ollama is running" and nothing else — a browser
    # link is a dead end. It is an API, and these are the ways in.
    access="api",
    usage=[
        Usage(
            label="Chat (curl)",
            kind="curl",
            command=(
                'curl {base_url}/api/chat -d \'{"model": "llama3.2:3b", '
                '"messages": [{"role": "user", "content": "Hello!"}], '
                '"stream": false}\''
            ),
            description="Ollama's native chat API.",
        ),
        Usage(
            label="OpenAI-compatible endpoint",
            kind="openai",
            command="{base_url}/v1",
            description=(
                "Use with any OpenAI SDK: set base_url to this and api_key "
                "to any non-empty string."
            ),
        ),
        Usage(
            label="List installed models",
            kind="curl",
            command="curl {base_url}/api/tags",
        ),
    ],
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
            label="Linking Ollama store to unified model store",
            command=_LINK_OLLAMA_STORE,
            check=f"[ -L {_OLLAMA_MODELS_DIR} ]",
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
            label="Ensuring Ollama store symlink",
            command=_LINK_OLLAMA_STORE,
            check=f"[ -L {_OLLAMA_MODELS_DIR} ]",
            workdir="/workspace",
        ),
    ],
)
