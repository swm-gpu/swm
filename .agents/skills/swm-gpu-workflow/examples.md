# swm worked examples

Full end-to-end runs that pass Phase 5. Use these as templates when bringing up a new framework.

## ACE-Step on H100 (ComfyUI custom node + side server)

Music generation with the ACE-Step diffusion model. Two processes: ComfyUI on `:8188` (public via RunPod proxy) and `acestep-openrouter` on `127.0.0.1:8002` (internal, called by the custom node).

```bash
swm pod create -p runpod -g "NVIDIA H100 80GB HBM3" \
  -n ace-step-test --lifecycle auto-down --idle-timeout 20 -y

swm setup install comfyui

swm run "pip install -q uv"
swm run "mkdir -p /workspace/.cache/uv /workspace/.cache/huggingface \
  && cd /workspace && [ -d ACE-Step-1.5 ] \
    || git clone --depth 1 https://github.com/ace-step/ACE-Step-1.5.git \
  && cd ACE-Step-1.5 && UV_CACHE_DIR=/workspace/.cache/uv uv sync"

swm run "cd /workspace/ComfyUI/custom_nodes \
  && [ -d ACE-Step-ComfyUI ] \
    || git clone --depth 1 https://github.com/ace-step/ACE-Step-ComfyUI.git \
  && /workspace/ComfyUI/venv/bin/pip install \
       -r ACE-Step-ComfyUI/requirements.txt"

swm run "nohup env \
  ACESTEP_CHECKPOINTS_DIR=/workspace/.cache/acestep-checkpoints \
  HF_HOME=/workspace/.cache/huggingface \
  /workspace/ACE-Step-1.5/.venv/bin/acestep-openrouter \
  --host 127.0.0.1 --port 8002 \
  > /workspace/acestep.log 2>&1 < /dev/null & \
  sleep 3 && pgrep -fa acestep-openrouter \
  || (tail -60 /workspace/acestep.log; exit 1)"

swm setup start comfyui
```

**Verified end state:**

- ComfyUI 0.19.3 on `:8188`, ACE-Step custom node imported in 0.0 s
- `acestep-openrouter` on `127.0.0.1:8002`, `/health` → `{"status":"ok"}`, `/v1/models` → `acemusic/acestep-v15-turbo`
- Checkpoints (9.4 GB) under `/workspace/.cache/acestep-checkpoints/`: `acestep-v15-turbo` (4.5 GB), `acestep-5Hz-lm-1.7B` (3.5 GB), `Qwen3-Embedding-0.6B` (1.2 GB), `vae` (322 MB)
- Workflow at `/workspace/ComfyUI/custom_nodes/ACE-Step-ComfyUI/workflows/text2music.json`
- GPU memory 8.6 / 80 GiB before first inference

**Hand-off foot-gun:** `AceStepText2MusicServer` defaults to `cloud` mode and must be flipped to `local` in the ComfyUI node UI — otherwise the H100 sits idle and burns money.

**Switch to XL 4B variant** (without recreating the pod):

```bash
swm run "pkill -f acestep-openrouter; nohup env \
  ACESTEP_CONFIG_PATH=acestep-v15-xl-turbo \
  ACESTEP_CHECKPOINTS_DIR=/workspace/.cache/acestep-checkpoints \
  HF_HOME=/workspace/.cache/huggingface \
  /workspace/ACE-Step-1.5/.venv/bin/acestep-openrouter \
  --host 127.0.0.1 --port 8002 \
  > /workspace/acestep.log 2>&1 < /dev/null & \
  sleep 3 && pgrep -fa acestep-openrouter"
```

**Resume next session** (restores `/workspace` from S3, custom nodes and checkpoints intact):

```bash
swm pod create -p runpod -g "NVIDIA H100 80GB HBM3" -n ace-step-test \
  -w ace-step-test --lifecycle auto-down --idle-timeout 20 -y
swm setup start comfyui
```
