"""Open WebUI — ChatGPT-style interface for local LLMs.

Installs Open WebUI in a venv under ``/workspace/open-webui`` and
connects to Ollama (localhost:11434) by default.  Provides model
browsing, downloading, tool calling, thinking display, RAG, and
file upload out of the box.

Install Ollama first (``swm setup install ollama``), then start both:
``swm setup start ollama <id> && swm setup start open-webui <id>``.
"""

from swm.frameworks import Framework, Step, nvidia_ld_exports

_INSTALL_DIR = "/workspace/open-webui"
_VENV = f"{_INSTALL_DIR}/venv"
_PIP = f"{_VENV}/bin/pip"
_PYTHON = f"{_VENV}/bin/python"
_PIP_CACHE = "/workspace/.cache/pip"
_DATA_DIR = f"{_INSTALL_DIR}/data"
_UV = "/usr/local/bin/uv"

FRAMEWORK = Framework(
    name="open-webui",
    label="Open WebUI",
    repo="https://github.com/open-webui/open-webui",
    install_dir=_INSTALL_DIR,
    description="ChatGPT-style web UI — connects to Ollama or vLLM",
    launch_cmd=(
        f"DATA_DIR={_DATA_DIR} "
        f"OLLAMA_BASE_URL=http://127.0.0.1:11434 "
        f"OPENAI_API_BASE_URL=http://127.0.0.1:8000/v1 "
        f"WEBUI_AUTH=false "
        f"{_VENV}/bin/open-webui serve --host 0.0.0.0 --port 8080"
    ),
    ports={8080: "http"},
    category="llm",
    stop_cmd="pkill -f 'open-webui serve' 2>/dev/null; true",
    process_pattern="open-webui serve",
    env_setup=(
        f"export DATA_DIR={_DATA_DIR} "
        f"OLLAMA_BASE_URL=http://127.0.0.1:11434 "
        f"OPENAI_API_BASE_URL=http://127.0.0.1:8000/v1 && "
        f"{nvidia_ld_exports(_VENV)}"
    ),
    steps=[
        Step(
            label="Creating install directory",
            command=f"mkdir -p {_INSTALL_DIR} {_DATA_DIR}",
            check=f"[ -d {_INSTALL_DIR} ]",
            workdir="/workspace",
        ),
        Step(
            label="Installing uv (Python manager)",
            command=f"pip3 install -q uv",
            check=f"[ -x {_UV} ]",
            workdir="/tmp",
        ),
        Step(
            label="Creating virtual environment (Python 3.11+)",
            command=f"{_UV} venv --python 3.11 {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Installing Open WebUI",
            command=f"{_UV} pip install --python {_PYTHON} open-webui",
        ),
    ],
    post_install=[
        Step(
            label="Verifying Open WebUI installation",
            command=f"{_VENV}/bin/open-webui --help > /dev/null",
        ),
    ],
    pre_start=[
        Step(
            label="Ensuring install directory exists",
            command=f"mkdir -p {_INSTALL_DIR} {_DATA_DIR}",
            check=f"[ -d {_INSTALL_DIR} ]",
            workdir="/workspace",
        ),
        Step(
            label="Ensuring uv is installed",
            command=f"pip3 install -q uv",
            check=f"[ -x {_UV} ]",
            workdir="/tmp",
        ),
        Step(
            label="Ensuring virtual environment exists",
            command=f"{_UV} venv --python 3.11 {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Ensuring Open WebUI is installed",
            command=f"{_UV} pip install --python {_PYTHON} open-webui",
            check=f"[ -x {_VENV}/bin/open-webui ]",
        ),
    ],
)
