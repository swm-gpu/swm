---
name: swm-gpu-workflow
description: >
  Use when the user wants to search cloud GPUs across providers, provision a
  GPU pod, install AI frameworks (ComfyUI, SwarmUI, vLLM, Ollama, Open WebUI,
  Axolotl, H2O LLM Studio), pull models from HuggingFace / Civitai / URLs to a
  unified store, sync workspaces to S3, manage idle-pod lifecycle, or track
  GPU spend — all via the swm CLI. Do NOT use for general Docker, Kubernetes,
  or non-GPU cloud tasks.
license: Apache-2.0
compatibility: "macOS or Linux, Python 3.11+, SSH client"
metadata:
  author: swm-gpu
  version: "0.2.4"
  docs-url: https://swmgpu.com
  repository: https://github.com/swm-gpu/swm
---

# swm — Cloud GPU Workflow Manager

One CLI to search, provision, install, sync, and tear down GPU pods across 10 cloud providers. Why swm over ad-hoc SSH scripts:

- **Cross-provider search** — `swm gpus` hits 10 providers with live pricing and stock in one call.
- **Declarative frameworks** — `swm setup install <name>` brings up ComfyUI / SwarmUI / vLLM / Ollama / Open WebUI / Axolotl / llm-studio idempotently; venv, custom nodes, and multi-GPU tensor parallelism handled.
- **Unified model store** — `swm models pull` auto-detects HF / Civitai / Ollama / URL refs, lands files under `/workspace/models/<bucket>/`, symlinks them into each framework's expected path.
- **Workspace persistence** — three-tier S3 sync (inotify → s5cmd → tar) survives auto-down. `pod create -w <name>` restores the last session.
- **Lifecycle guard** — `auto-down` / `auto-stop` / `remind` policies stop idle bills.

> **Stop condition.** Don't hand off until **Phase 5** passes. A pod that's "almost up" still bills.

## Prerequisites

- `swm --version` works (`pipx install swm-gpu` or `brew tap swm-gpu/swm && brew install swm`).
- `ssh` and `scp` on PATH.
- At least one provider key: `swm config set runpod.api_key <key>` (or `vastai`, `lambda`, …).
- Optional `/workspace` persistence: `swm config set b2.key_id …` + `swm config set storage.default b2:<bucket>`.

## Phase 0 — State check

```bash
swm pod list && swm use --show               # running pods + active-pod selection
swm storage ls                               # existing workspaces in the default bucket
```

If a matching pod is `RUNNING`, set it active (`swm use <name>`) and skip to Phase 5. If a workspace prefix you want already exists in `swm storage ls`, restore it with `pod create -w <name>`. Otherwise continue.

## Phase 1 — Clarify

Use `AskQuestion`:

| Question | Options |
|---|---|
| **VRAM floor**? | `≤16 GB` / `16–24 GB` / `24–48 GB` / `48–80 GB` / `>80 GB` |
| **Provider**? | `runpod` / `vastai` / `lambda` / `tensordock` / `vultr` / `auto (cheapest)` |
| **Lifecycle**? | `auto-down` (default) / `auto-stop` / `remind` / `manual` |
| **Idle timeout**? | `10` / `20` / `30` / `60` / `120` min |
| Persist `/workspace`? | `yes` (default) / `no — one-shot, --no-storage` |

| VRAM floor | GPU class to search |
|---|---|
| ≤16 GB | `RTX 4090`, `A4000`, `RTX 6000 Ada` |
| 16–24 GB | `RTX 4090`, `RTX 6000 Ada`, `L40S` |
| 24–48 GB | `A100 40GB`, `A100 80GB`, `L40S` |
| 48–80 GB | `H100`, `H100 SXM`, `A100 80GB` |
| >80 GB | `H200`, `B200`, multi-GPU `H100 SXM` |

For unsupported frameworks, read the README for: install method (`pip` vs `uv sync` vs custom), ComfyUI custom node vs standalone server, default ports, model cache location, UI mode toggles.

## Phase 2 — Pick GPU

```bash
swm gpus -g <class> --sort price --secure | head -20
```

Pick the cheapest in-stock SKU meeting the VRAM floor. **Capture the exact display name** (e.g. `"NVIDIA H100 80GB HBM3"`) — that's what `-g` on `pod create` wants. Quote it; spaces break shells.

## Phase 3 — Provision

```bash
swm pod create -p <provider> -g "<gpu-display-name>" \
  -n <name> --lifecycle <policy> --idle-timeout <min> -y
```

- `-y` is **mandatory** in agent shells — the `Proceed?` prompt has no fallback.
- Container disk wipes on stop; only `/workspace` persists. `--volume N` sizes it (RunPod requires `>= 1`).
- `-w <name>` restores the last workspace pushed under that name.
- After this the new pod is the active pod; subsequent `swm run` / `swm setup` can omit the id.

## Phase 4 — Install

### 4a. Prefer built-in frameworks

| Framework | Use when |
|---|---|
| `comfyui` | Image/video gen, custom-node ecosystems |
| `swarmui` | Higher-level wrapper around ComfyUI |
| `vllm` | Multi-GPU LLM serving (OpenAI-compatible) |
| `ollama` | Single-GPU LLM chat |
| `open-webui` | ChatGPT-style frontend for Ollama / vLLM |
| `axolotl` | LLM fine-tuning |
| `llm-studio` | H2O no-code fine-tuning |

```bash
swm setup list
swm setup install <name>                                         # idempotent
swm setup start   <name>                                         # background, prints proxy URL, probes health
swm setup start   vllm    --model Qwen/Qwen3-8B                  # vLLM model selection
swm setup start   comfyui --port 8288 --extra-args "--use-sage-attention"
swm setup stop    <name>
```

vLLM auto-detects GPU count for tensor parallelism. swm opens SSH tunnels for unexposed ports and probes `/health` before reporting ready.

### 4b. Custom tools — chain `swm run`

Each `swm run` is its own SSH session. Use absolute paths, `&&`-chain idempotent commands, install into `/workspace`:

```bash
swm run "pip install -q uv && mkdir -p /workspace/.cache/uv \
  && cd /workspace && [ -d <repo> ] || git clone --depth 1 <git-url> \
  && cd <repo> && UV_CACHE_DIR=/workspace/.cache/uv uv sync"
```

Use `uv sync` over `pip install -e .` when `pyproject.toml` has `[tool.uv.sources]` — pip silently ignores those overrides.

### 4c. ComfyUI custom nodes

Install **before** `swm setup start comfyui`. Custom-node deps go into ComfyUI's venv (not system pip):

```bash
swm run "cd /workspace/ComfyUI/custom_nodes \
  && [ -d <node-dir> ] || git clone --depth 1 <git-url> \
  && /workspace/ComfyUI/venv/bin/pip install -r <node-dir>/requirements.txt"
```

### 4d. Models — use the unified store

`swm models pull` lands files under `/workspace/models/<bucket>/` and symlinks them into each framework's expected path. Source auto-detected from the ref shape:

```bash
swm models pull <pod> Qwen/Qwen3-8B                              # HuggingFace
swm models pull <pod> civitai:1925758 --as lora                  # Civitai
swm models pull <pod> https://example.com/m.safetensors --as checkpoint   # URL
swm models pull <pod> ollama:llama3                              # Ollama
swm models pull <pod> Comfy-Org/Wan_2.2_ComfyUI_Repackaged \
  --as lora --filename split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors

swm models search <query> [--sort downloads] [-n 20]   # find models on HuggingFace Hub
swm models info <ref>             # metadata before pulling (HF or Civitai)
swm models list <pod> [--all]     # tracked (+ untracked) files
swm models link <pod> /abs/path/to/file --as vae
swm models remove <pod> <name> [-y]
```

API keys (set once; legacy `hf_token` also read):

```bash
swm config set hf.api_key <token>        # gated HF models
swm config set civitai.api_key <token>   # Civitai gated / NSFW
```

`--filename` supports nested HF paths (`split_files/loras/foo.safetensors`). If a bare basename 404s, swm prints matching paths from the repo. Multi-file pulls from one HF repo get distinct manifest entries (`hf:{repo}:{filename}`); no overwrites.

### 4e. Background daemon pattern

Mirror swm's `exec_background`: `nohup … > log 2>&1 < /dev/null &` with `&` **outside** `bash -c`, then sleep + `pgrep` so the call returns non-zero on crash:

```bash
swm run "nohup env <VAR>=<val> /workspace/<repo>/.venv/bin/<entrypoint> \
  --host 127.0.0.1 --port <port> > /workspace/<name>.log 2>&1 < /dev/null & \
  sleep 3 && pgrep -fa <entrypoint> \
  || (tail -50 /workspace/<name>.log; exit 1)"
```

Bind `127.0.0.1` for internal services, `0.0.0.0` only if the proxy URL needs to reach it. Omit `< /dev/null` and SSH may hang on stdin.

## Phase 5 — Verify

Every claim must hold before hand-off:

- Framework main port returns `200` (ComfyUI `:8188`, vLLM `:8000/v1/models`, Ollama `:11434/api/tags`, Open WebUI `:8080/api/version`).
- Side-server `/health` returns `200` (if any).
- Custom-node directory appears in the host's startup log.
- Expected node/model types appear in `/object_info` (ComfyUI) or `/v1/models` (vLLM).
- Checkpoint dirs non-empty and roughly the size from the model card.
- VRAM in the expected ballpark (low if lazy-loaded, near full after first request).
- `df -h /workspace` not over 80 %.

If any check fails, iterate on the relevant phase. Don't hand off a half-working pod.

## Phase 6 — Hand off

1. **Public URL** verbatim (RunPod: `https://<pod>-<port>.proxy.runpod.net`).
2. **Sync status**: `swm sync status <pod>` — confirm watcher running, last push stamp recent.
3. **Local download** for workflow / config files: `swm download <relative-path> -d ~/Downloads`.
4. **Framework foot-guns** — spell out UI toggles (e.g. ACE-Step's `cloud → local` mode, model-selector defaults). #1 reason a working pod looks broken.
5. **Resume command** with `-w <name>` so the next session restores the workspace.
6. **Log tails**: `swm run "tail -f /tmp/<host>.log"` for live debugging.

See [examples.md](examples.md) for a fully worked run (ACE-Step on H100).

---

# Command reference

## GPU search

```bash
swm gpus -g h200 --secure --sort price        # filter, secure cloud only, cheapest first
swm gpus -g h200 -c 4 --max-price 4           # 4×H200 under $4/hr
swm gpus -p vastai -n 50 --all                # single provider, more rows
```

Unknown provider slugs raise `UsageError` listing valid ones.

## Pod management

```bash
swm pod create -p runpod -g h200 -n s1 -y
swm pod create -p vastai -g h200 -n train --gpu-count 4 --volume 200 --cloud-type SECURE -y
swm pod list                                  # auto-prunes stale entries vs live provider state
swm pod status  <pod>                          # GPU, cost, uptime + live sync health (autosync/watcher/last-push)
swm pod stop    <pod>                          # halt, preserve volume (resume with pod start)
swm pod start   <pod>                          # resume a stopped pod, wait for SSH
swm pod prune                                  # drop config entries for pods gone from the provider
```

**Teardown — two distinct commands:**

```bash
swm pod down <pod>                             # SAFE: push /workspace → storage, then terminate
swm pod down <pod> --no-sync                   #   skip the push (workspace already synced)
swm pod down <pod> --force-down                #   terminate even if push left no recent stamp (DANGEROUS)
swm pod terminate <pod> -y                     # DESTRUCTIVE: delete pod + volume, NO sync (irreversible)
```

`pod down` is the everyday teardown — it refuses to wipe `/workspace` or terminate unless the push wrote a fresh push stamp (guards against silent data loss). `pod terminate` skips all sync and deletes the volume; only use it for throwaway / `--no-storage` pods.

## Sync

```bash
swm sync pull  [pod]              # storage → pod (non-destructive: skips existing, never deletes)
swm sync pull  [pod] --tar        # tarball mode (600k+ small files; fails loudly on error)
swm sync push  [pod]              # pod → storage (incremental)
swm sync push  [pod] --force      # kill any running transfer, re-baseline from scratch
swm sync push  [pod] --delete     # also delete remote files removed locally (watcher must be alive)
swm sync push  [pod] --tar        # pack workspace to one tarball object (fast for many small files)
swm sync status [pod]             # s5cmd, watcher, autosync daemon, last-push stamp, pending changes
```

**Watcher vs daemon — two layers:**

```bash
swm sync watch [pod]              # start the inotify file-change watcher only (records what changed)
swm sync watch [pod] --stop       # stop the watcher
swm sync auto  [pod]              # start the background daemon: reads watcher log every 60s, pushes
swm sync auto  [pod] -i 30        # custom interval (seconds)
swm sync auto  [pod] --status     # daemon state + recent log tail
swm sync auto  [pod] --stop       # stop the daemon
swm sync auto  [pod] --force      # start without a prior successful pull/push (DANGEROUS — see below)
```

`swm pod create -w <name>` wires up the watcher + auto daemon for you; reach for `sync watch` / `sync auto` only to inspect or recover. **`sync auto --force` is dangerous**: it bypasses the safety check that requires a known-good baseline, so local deletions can propagate to storage. Only force when you've just `sync push --force`ed and know the pod is authoritative.

Three-tier sync: inotify watcher → incremental s5cmd → tar. Push reconciles inotify events with a `find -newer` scan so `pip install` bursts aren't missed (and falls back to a full `find` count when the watcher is dead). Pull re-runs framework link-repair steps so `/workspace/models/` symlinks stay wired after restore.

## Guard

```bash
swm guard set <pod> --mode auto-down --idle-timeout 30   # set/update policy (auto-down/auto-stop/remind/manual)
swm guard set <pod> --mode remind                        # notify-only
swm guard disable <pod> [--force-manual]                 # remove override / pin manual
swm guard defaults --mode auto-down --idle-timeout 30    # default policy applied to new pods
swm guard list                                           # all guarded pods + live idle status
swm guard run [pods…] [--once] [--interval N]            # run the loop in foreground (debug/cron)
swm guard stop-daemon                                    # stop the background daemon
```

Monitors 4 signals: SSH sessions (`who`), GPU utilization, filesystem writes, active processes. Setting `auto-stop`/`auto-down` auto-starts the local daemon; `guard run` is the explicit foreground runner (use `--once` for a single sweep). `--idle-timeout 0` and `--poll-interval 0` are honored as explicit values (not unset). `auto-down` runs the same push-stamp safety check as `pod down` — it leaves the pod alive rather than terminate on a partial push.

## Costs

```bash
swm costs live                                # right-now burn across active pods
swm costs summary [--period today|...]        # breakdown by provider/pod (today = UTC day)
swm costs log [-n 50] [-p vastai]             # detailed per-session history
swm costs reconcile [-p vastai]               # compare local tracking vs provider card charges
swm costs budget set 100 [-s global] [-t month]   # set a spending alert
swm costs budget show                         # active budgets + current spend
swm costs budget remove <scope> [-t month]    # remove a budget
```

## Remote

```bash
swm ssh    <pod>
swm run    [pod] '<cmd>'
swm upload   [pod] ./local remote/path
swm download [pod] remote/file -d ~/Downloads
```

## Pricing reference

```bash
swm pricing compare  --gpu h100|h200|b200|a100|rtx-4090|l40s   # per-GPU/hr across providers
swm pricing estimate -g h200 --hours 24 [--storage GB] [--tier secure]   # projected workload cost
swm pricing specs    --gpu h200                                 # hardware specs (VRAM, bandwidth) side-by-side
```

## Instance ID format

`provider:id` (`runpod:abc123`, `vastai:34182944`). A bare id auto-resolves by querying configured providers. `swm use <pod>` validates and sets the active pod.

## Providers

| Slug | API | Live pricing |
|---|---|---|
| `runpod` | GraphQL | yes |
| `vastai` | REST | yes |
| `lambda` | REST | yes |
| `vultr` | REST | yes |
| `tensordock` | REST | yes |
| `fluidstack` | REST | yes |
| `aws` | boto3 | yes |
| `gcp` | gcloud CLI | yes |
| `azure` | az CLI (`pip install swm-gpu[azure]`) | yes |
| `coreweave` | Kubernetes | yes |

## Storage (buckets & workspaces)

Inspect and manage cloud-storage buckets directly — **no running pod required**. Top-level prefixes in your default bucket are your workspace names, so `swm storage ls` is the fastest "what's in my bucket?" probe.

```bash
swm storage list                              # configured buckets across all providers
swm storage ls                                # default bucket root = workspace names
swm storage ls studio-live/                   # drill into one workspace (sizes/mtimes show here)
swm storage ls -b b2:other-bucket             # explicit provider:bucket
swm storage create my-models -p b2            # create a bucket (-l location, -c storage-class)
swm storage upload   ./file remote/path       # one-off upload
swm storage download remote/path ./file       # one-off download
swm storage rm workspace2/ --dry-run          # COUNT objects under a prefix, delete nothing
swm storage rm workspace2/ -y                 # delete all objects under prefix (S3 batch delete)
```

`storage rm` uses the S3 batch-delete API (1000 objects/request) and prompts before deleting unless `-y`. It's a hard delete on S3-compatible backends (B2/S3) — always `--dry-run` first. Top-level `ls` shows `DIR` with no size/mtime (B2 `CommonPrefixes` carry neither); drill into a prefix for real sizes.

Backend config keys (S3-compatible via s5cmd):

| Backend | Keys |
|---|---|
| Backblaze B2 | `b2.key_id`, `b2.app_key`, `b2.bucket`, `b2.s3_endpoint` |
| Amazon S3 | `s3.access_key`, `s3.secret_key`, `s3.bucket` |
| Google GCS | `gcs.hmac_access`, `gcs.hmac_secret`, `gcp.bucket` |

Default: `swm config set storage.default b2:<bucket>`. Credentials are masked by suffix in `config get/list` and never written to the pod (passed as transient env vars per s5cmd call).

## Frameworks & workspace attach (setup)

```bash
swm setup list                                # available frameworks
swm setup install <name> [pod]                # idempotent install into its own venv
swm setup start   <name> [pod] [-p port] [--model M] [--extra-args "…"]
swm setup stop    <name> [pod]
swm setup storage [pod] [-p b2|gcs|s3|all]    # install s5cmd + configure creds on an existing pod
swm setup workspace [pod] [-n name] [-b provider:bucket] [--force]
```

**`swm setup workspace` is the recovery command** for the silent-data-loss scenario: it does the full workspace bootstrap in one shot (installs s5cmd, configures creds, pulls or initializes the workspace, starts the auto-sync daemon) and persists the pod ↔ workspace ↔ bucket association. Use it when:
- you created a pod with `--no-storage` and now want persistence,
- `pod create`'s SSH probe timed out so bootstrap was skipped,
- you want to reattach a pod to a different workspace (`--force`).

## Config

```bash
swm config set <key> <value>                  # e.g. swm config set runpod.api_key sk-xxx
swm config get <key>                          # single value (secrets suffix-masked)
swm config list                               # all values (secrets masked)
swm config delete <key>                       # remove a key
swm config path                               # location of the config file
```

Common keys: provider `<slug>.api_key`; storage `b2.*` / `s3.*` / `gcs.*` + `storage.default`; model tokens `hf.api_key`, `civitai.api_key`; guard `guard.*` defaults.

## Images

```bash
swm images list [-p provider] [--cuda 12.8] [--refresh] [-n 30] [--all]
```

Lists Docker images discoverable for a provider (newest-first) for use with `swm pod create --image`. `--refresh` re-queries the provider instead of using cached results.

---

## Anti-patterns

- **Never omit `-y` on `swm pod create`** — agent shells hang on the `Proceed?` prompt.
- **Never install outside `/workspace`** — container disk wipes on stop; only `/workspace` survives auto-down.
- **Never drop `&` outside `bash -c`** when starting daemons — SSH waits for foreground and `swm run` hangs.
- **Never reuse one venv across frameworks** with conflicting `torch` pins — separate venvs (~7 GB each) beat dependency hell.
- **Never trust `pip install -e .`** for projects with `[tool.uv.sources]` — use `uv sync`.
- **Never `swm run -q`** during initial setup — quiet mode swallows install errors. Reserve `-q` for scripted health checks.
- **Never tear down before `du -sh /workspace`** — 10–30 GB bloat extends the next pull.
- **Never guess nested HF file paths** — run `swm models info <repo>` and copy the exact `rfilename`.
- **Never skip Phase 0** — a forgotten running pod bills silently.
- **Never confuse `pod down` with `pod terminate`** — `down` syncs `/workspace` first; `terminate` deletes the volume with no sync. Use `terminate` only on `--no-storage` / throwaway pods.
- **Never trust a green `sync push` alone** — confirm `swm pod status` shows a recent last-push stamp and autosync running before tearing down. A push can exit 0 yet skip the stamp on a partial transfer.
- **Never use `swm storage rm` without `--dry-run` first** — it's a hard S3 batch delete with no undo.
- **Never hand off** before Phase 5 passes.
