"""vLLM — high-performance multi-GPU LLM inference server.

Installs vLLM in a venv and serves models with an OpenAI-compatible
API.  Tensor parallelism is auto-configured to use all available GPUs,
making this the right choice for large models (70B+) or high-throughput
concurrent inference.

The model is read from ``/workspace/vllm/model.txt`` at startup.
Set or change it with::

    swm setup start vllm <pod> --model meta-llama/Llama-3.3-70B-Instruct

HuggingFace weights live under ``/workspace/models/hf`` (the unified
model store) and ``/workspace/vllm/hf_cache`` is a symlink to it, so
they persist across B2 syncs and are visible to other frameworks.

Connect Open WebUI to vLLM by pointing it at ``http://127.0.0.1:8000/v1``.
"""

from swm.bootstrap import (
    PYTHON_DEFAULT_MINOR,
    UV_ENV_EXPORTS,
    WORKSPACE_UV,
)
from swm.frameworks import Framework, Step, Usage, nvidia_ld_exports

_INSTALL_DIR = "/workspace/vllm"
_VENV = f"{_INSTALL_DIR}/venv"
_PYTHON = f"{_VENV}/bin/python"
_PIP_CACHE = "/workspace/.cache/pip"
_HF_CACHE = f"{_INSTALL_DIR}/hf_cache"
_UNIFIED_HF = "/workspace/models/hf"
_MODEL_FILE = f"{_INSTALL_DIR}/model.txt"
_DEFAULT_MODEL = "Qwen/Qwen3-8B"
_LAUNCHER = f"{_INSTALL_DIR}/start.sh"
_UV_PIP = f"{WORKSPACE_UV} pip install --python {_PYTHON}"

# Migrate ``_HF_CACHE`` to a symlink pointing at the unified store at
# ``/workspace/models/hf`` so ``swm models`` writes show up here automatically.
_LINK_HF_CACHE = (
    f"mkdir -p {_UNIFIED_HF} && "
    f"if [ -L {_HF_CACHE} ]; then :; "
    f"elif [ -d {_HF_CACHE} ]; then "
    f"  ( shopt -s dotglob nullglob; mv {_HF_CACHE}/* {_UNIFIED_HF}/ 2>/dev/null || true ); "
    f"  rmdir {_HF_CACHE} 2>/dev/null || rm -rf {_HF_CACHE}; "
    f"  ln -s {_UNIFIED_HF} {_HF_CACHE}; "
    f"else "
    f"  ln -s {_UNIFIED_HF} {_HF_CACHE}; "
    f"fi"
)

_ENV = (
    f"{UV_ENV_EXPORTS} && "
    f"export PIP_CACHE_DIR={_PIP_CACHE} "
    f"HF_HOME={_HF_CACHE} "
    f"VLLM_WORKER_MULTIPROC_METHOD=spawn && "
    f"{nvidia_ld_exports(_VENV)}"
)

_WRITE_LAUNCHER = (
    f"cat > {_LAUNCHER} << 'SWM_EOF'\n"
    f"#!/bin/bash\n"
    f"MODEL=$(cat {_MODEL_FILE} 2>/dev/null || echo {_DEFAULT_MODEL})\n"
    f"TP=$(nvidia-smi -L 2>/dev/null | wc -l | xargs)\n"
    '[ "$TP" -lt 1 ] && TP=1\n'
    'echo "Serving $MODEL on $TP GPU(s)"\n'
    f"export HF_HOME={_HF_CACHE}\n"
    f"export VLLM_WORKER_MULTIPROC_METHOD=spawn\n"
    f'exec {_VENV}/bin/vllm serve "$MODEL" \\\n'
    f"    --host 0.0.0.0 --port 8000 \\\n"
    '    --tensor-parallel-size "$TP" \\\n'
    f"    --enable-auto-tool-choice \\\n"
    f"    --tool-call-parser hermes \\\n"
    f"    --max-model-len 8192\n"
    f"SWM_EOF\n"
    f"chmod +x {_LAUNCHER}"
)

FRAMEWORK = Framework(
    name="vllm",
    label="vLLM",
    repo="https://github.com/vllm-project/vllm",
    install_dir=_INSTALL_DIR,
    venv=_VENV,
    description="Fast multi-GPU inference — tensor parallelism, tools, thinking",
    launch_cmd=f"bash {_LAUNCHER}",
    ports={8000: "http"},
    category="llm",
    consumes=frozenset({"llm"}),
    # The venv dir can exist half-built; the entrypoint is the real signal.
    installed_check="[ -x /workspace/vllm/venv/bin/vllm ]",
    # vLLM serves the OpenAI wire protocol; its root URL is JSON, not a page.
    access="api",
    usage=[
        Usage(
            label="OpenAI-compatible endpoint",
            kind="openai",
            command="{base_url}/v1",
            description=(
                "Point any OpenAI SDK here; api_key may be any non-empty "
                "string unless one was set with --api-key."
            ),
        ),
        Usage(
            label="Chat completion (curl)",
            kind="curl",
            command=(
                'curl {base_url}/v1/chat/completions -H "Content-Type: application/json" '
                '-d \'{"model": "{model}", "messages": '
                '[{"role": "user", "content": "Hello!"}]}\''
            ),
        ),
        Usage(
            label="List served models",
            kind="curl",
            command="curl {base_url}/v1/models",
        ),
    ],
    stop_cmd="pkill -f 'vllm serve'",
    process_pattern="vllm serve",
    env_setup=_ENV,
    steps=[
        Step(
            label="Creating install directory",
            command=f"mkdir -p {_INSTALL_DIR}",
            check=f"[ -d {_INSTALL_DIR} ]",
            workdir="/workspace",
        ),
        Step(
            label="Linking HF cache to unified model store",
            command=_LINK_HF_CACHE,
            check=f"[ -L {_HF_CACHE} ]",
        ),
        Step(
            label="Creating virtual environment",
            command=f"{WORKSPACE_UV} venv --python {PYTHON_DEFAULT_MINOR} --seed {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Installing vLLM",
            command=f"{_UV_PIP} vllm",
        ),
        Step(
            label=f"Setting default model ({_DEFAULT_MODEL})",
            command=f"echo {_DEFAULT_MODEL} > {_MODEL_FILE}",
            check=f"[ -f {_MODEL_FILE} ]",
        ),
        Step(
            label="Creating launcher script",
            command=_WRITE_LAUNCHER,
            check=f"[ -x {_LAUNCHER} ]",
        ),
    ],
    post_install=[
        Step(
            label="Verifying vLLM installation",
            command=f"{_VENV}/bin/vllm --version",
        ),
        Step(
            label="Detecting GPUs",
            command=(
                "nvidia-smi -L 2>/dev/null | head -8 && "
                "echo && "
                "TP=$(nvidia-smi -L 2>/dev/null | wc -l | xargs) && "
                f"echo \"vLLM will use tensor parallelism across $TP GPU(s)\" && "
                f"echo \"Default model: $(cat {_MODEL_FILE})\" && "
                f"echo \"Change model: swm setup start vllm <id> --model <model>\""
            ),
        ),
    ],
    pre_start=[
        Step(
            label="Ensuring install directory exists",
            command=f"mkdir -p {_INSTALL_DIR}",
            check=f"[ -d {_INSTALL_DIR} ]",
            workdir="/workspace",
        ),
        Step(
            label="Ensuring HF cache symlink",
            command=_LINK_HF_CACHE,
            check=f"[ -L {_HF_CACHE} ]",
        ),
        Step(
            label="Ensuring virtual environment exists",
            command=f"{WORKSPACE_UV} venv --python {PYTHON_DEFAULT_MINOR} --seed {_VENV}",
            check=f"[ -x {_PYTHON} ]",
        ),
        Step(
            label="Ensuring vLLM is installed",
            command=f"{_UV_PIP} vllm",
            check=f"[ -x {_VENV}/bin/vllm ]",
        ),
        Step(
            label="Ensuring launcher script exists",
            command=_WRITE_LAUNCHER,
            check=f"[ -x {_LAUNCHER} ]",
        ),
    ],
)
