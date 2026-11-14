"""Custom Web UI — serve any Gradio, FastAPI, Streamlit, or Next.js app from /workspace/app."""

from __future__ import annotations

from swm.bootstrap import (
    PYTHON_DEFAULT_MINOR,
    UV_ENV_EXPORTS,
    WORKSPACE_UV,
)
from swm.frameworks import Framework, Step, nvidia_ld_exports

_INSTALL_DIR = "/workspace/app"
_VENV = f"{_INSTALL_DIR}/venv"
_PYTHON = f"{_VENV}/bin/python"
_LAUNCHER = f"{_INSTALL_DIR}/start.sh"
_DEFAULT_PORT = 7860
_PIP_CACHE = "/workspace/.cache/pip"
_REQS_FILE = f"{_INSTALL_DIR}/requirements.txt"
_REQS_STAMP = f"{_INSTALL_DIR}/.swm_reqs_installed"

_ENV = (
    f"{UV_ENV_EXPORTS} && "
    f"export PIP_CACHE_DIR={_PIP_CACHE} && "
    f"{nvidia_ld_exports(_VENV)}"
)

_WRITE_LAUNCHER = (
    f"if [ ! -f {_LAUNCHER} ]; then\n"
    f"cat > {_LAUNCHER} << 'SWM_EOF'\n"
    "#!/bin/bash\n"
    "set -euo pipefail\n"
    'PORT="${PORT:-7860}"\n'
    'for arg in "$@"; do\n'
    '  case "$arg" in\n'
    '    --port=*) PORT="${arg#*=}" ;;\n'
    "  esac\n"
    "done\n"
    "while [ $# -gt 0 ]; do\n"
    '  case "$1" in\n'
    '    --port|-p) PORT="$2"; shift 2 ;;\n'
    "    *) break ;;\n"
    "  esac\n"
    "done\n"
    'export PORT GRADIO_SERVER_NAME="0.0.0.0" GRADIO_SERVER_PORT="$PORT" HOST="0.0.0.0"\n'
    f"cd {_INSTALL_DIR}\n"
    f"if [ -x {_PYTHON} ]; then\n"
    f'  export PATH="{_VENV}/bin:$PATH"\n'
    "fi\n"
    f"if [ -f {_INSTALL_DIR}/run.sh ]; then\n"
    f'  exec bash {_INSTALL_DIR}/run.sh "$@"\n'
    f"elif [ -f {_INSTALL_DIR}/app.py ]; then\n"
    f'  exec {_PYTHON} {_INSTALL_DIR}/app.py "$@"\n'
    f"elif [ -f {_INSTALL_DIR}/server.py ]; then\n"
    f'  exec {_PYTHON} {_INSTALL_DIR}/server.py "$@"\n'
    f"elif [ -f {_INSTALL_DIR}/main.py ]; then\n"
    f'  exec {_PYTHON} {_INSTALL_DIR}/main.py "$@"\n'
    f"elif [ -f {_INSTALL_DIR}/package.json ]; then\n"
    '  exec npm run start -- --port "$PORT" --hostname 0.0.0.0 "$@"\n'
    "else\n"
    f'  exec {_PYTHON} -m http.server "$PORT" --bind 0.0.0.0\n'
    "fi\n"
    "SWM_EOF\n"
    "fi\n"
    f"chmod +x {_LAUNCHER}"
)

_HAS_USER_APP_CHECK = (
    f"[ -f {_INSTALL_DIR}/run.sh ] || [ -f {_INSTALL_DIR}/app.py ] "
    f"|| [ -f {_INSTALL_DIR}/server.py ] || [ -f {_INSTALL_DIR}/main.py ] "
    f"|| [ -f {_INSTALL_DIR}/package.json ] || [ -f {_INSTALL_DIR}/index.html ]"
)

_WRITE_DEFAULT_APP = (
    f"if [ ! -f {_INSTALL_DIR}/run.sh ] && [ ! -f {_INSTALL_DIR}/app.py ] "
    f"&& [ ! -f {_INSTALL_DIR}/server.py ] && [ ! -f {_INSTALL_DIR}/main.py ] "
    f"&& [ ! -f {_INSTALL_DIR}/package.json ] && [ ! -f {_INSTALL_DIR}/index.html ]; then\n"
    f"cat > {_INSTALL_DIR}/app.py << 'SWM_EOF'\n"
    "from __future__ import annotations\n\n"
    "import os\n"
    "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n\n"
    'PORT = int(os.environ.get("PORT", "7860"))\n\n'
    "_HTML = b\"\"\"<!doctype html>\n"
    '<html lang="en">\n'
    "<head>\n"
    '  <meta charset="utf-8">\n'
    '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
    "  <title>Custom Web UI Ready</title>\n"
    "  <style>\n"
    "    body { font-family: system-ui, -apple-system, sans-serif; background: #0b0d10; color: #e5e7eb; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }\n"
    "    main { max-width: 560px; padding: 32px; border: 1px solid #1f2937; border-radius: 12px; background: #111827; }\n"
    "    h1 { font-size: 20px; margin: 0 0 12px; color: #f9fafb; }\n"
    "    p { font-size: 14px; line-height: 1.6; color: #9ca3af; margin: 0 0 12px; }\n"
    "    code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #60a5fa; }\n"
    "  </style>\n"
    "</head>\n"
    "<body>\n"
    "  <main>\n"
    "    <h1>Custom Web UI is running</h1>\n"
    "    <p>Place your Gradio, FastAPI, Streamlit, or Next.js entrypoint in <code>/workspace/app</code> (<code>app.py</code>, <code>server.py</code>, <code>main.py</code>, <code>run.sh</code>, or <code>start.sh</code>) and restart the framework.</p>\n"
    "  </main>\n"
    "</body>\n"
    "</html>\n"
    '"""\n\n\n'
    "class _Handler(BaseHTTPRequestHandler):\n"
    "    def do_GET(self) -> None:\n"
    "        self.send_response(200)\n"
    '        self.send_header("Content-Type", "text/html; charset=utf-8")\n'
    '        self.send_header("Content-Length", str(len(_HTML)))\n'
    "        self.end_headers()\n"
    "        self.wfile.write(_HTML)\n\n"
    "    def do_HEAD(self) -> None:\n"
    "        self.send_response(200)\n"
    '        self.send_header("Content-Type", "text/html; charset=utf-8")\n'
    '        self.send_header("Content-Length", str(len(_HTML)))\n'
    "        self.end_headers()\n\n"
    "    def log_message(self, format: str, *args: object) -> None:\n"
    "        return\n\n\n"
    'if __name__ == "__main__":\n'
    "    ThreadingHTTPServer.allow_reuse_address = True\n"
    '    with ThreadingHTTPServer(("0.0.0.0", PORT), _Handler) as httpd:\n'
    "        httpd.serve_forever()\n"
    "SWM_EOF\n"
    "fi"
)

_INSTALL_REQS = (
    f"if [ -f {_REQS_FILE} ] && [ ! -f {_REQS_STAMP} ]; then "
    f"{WORKSPACE_UV} pip install --python {_PYTHON} -r {_REQS_FILE} "
    f"&& touch {_REQS_STAMP}; "
    "fi"
)

_REQS_CHECK = f"[ ! -f {_REQS_FILE} ] || [ -f {_REQS_STAMP} ]"

FRAMEWORK = Framework(
    name="webapp",
    label="Custom Web UI",
    repo="",
    install_dir=_INSTALL_DIR,
    venv=_VENV,
    description="Serve any Gradio, FastAPI, Streamlit, or Next.js web UI from /workspace/app",
    launch_cmd=f"PORT={_DEFAULT_PORT} bash {_LAUNCHER} --port {_DEFAULT_PORT}",
    ports={_DEFAULT_PORT: "http"},
    ready_timeout=600,
    category="inference",
    access="ui",
    installed_check=f"[ -f {_LAUNCHER} ]",
    stop_cmd="pkill -f '/workspace/app/(start\\.sh|run\\.sh|app\\.py|server\\.py|main\\.py)' 2>/dev/null; true",
    process_pattern="/workspace/app/",
    env_setup=_ENV,
    steps=[
        Step(
            label="Creating application directory",
            command=f"mkdir -p {_INSTALL_DIR}",
            check=f"[ -d {_INSTALL_DIR} ]",
            workdir="/workspace",
        ),
        Step(
            label="Creating virtual environment",
            command=f"{WORKSPACE_UV} venv --python {PYTHON_DEFAULT_MINOR} --seed {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Installing application requirements",
            command=_INSTALL_REQS,
            check=_REQS_CHECK,
        ),
        Step(
            label="Creating default application entrypoint",
            command=_WRITE_DEFAULT_APP,
            check=_HAS_USER_APP_CHECK,
        ),
        Step(
            label="Creating launcher script",
            command=_WRITE_LAUNCHER,
            check=f"[ -x {_LAUNCHER} ]",
        ),
    ],
    pre_start=[
        Step(
            label="Ensuring application directory exists",
            command=f"mkdir -p {_INSTALL_DIR}",
            check=f"[ -d {_INSTALL_DIR} ]",
            workdir="/workspace",
        ),
        Step(
            label="Ensuring virtual environment exists",
            command=f"{WORKSPACE_UV} venv --python {PYTHON_DEFAULT_MINOR} --seed {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Ensuring application requirements are installed",
            command=_INSTALL_REQS,
            check=_REQS_CHECK,
        ),
        Step(
            label="Ensuring application entrypoint exists",
            command=_WRITE_DEFAULT_APP,
            check=_HAS_USER_APP_CHECK,
        ),
        Step(
            label="Ensuring launcher script exists",
            command=_WRITE_LAUNCHER,
            check=f"[ -x {_LAUNCHER} ]",
        ),
    ],
    repair=[
        Step(
            label="Reinstalling application requirements",
            command=(
                f"if [ -f {_REQS_FILE} ]; then "
                f"{WORKSPACE_UV} pip install --python {_PYTHON} -r {_REQS_FILE} "
                f"&& touch {_REQS_STAMP}; "
                "fi"
            ),
        ),
    ],
)
