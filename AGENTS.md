# AGENTS.md

Instructions for AI coding agents working on this codebase.

## Project Overview

`swm` is a Python CLI tool that manages cloud GPU instances across multiple providers (RunPod, Vast.ai, Lambda Labs, AWS, GCP, CoreWeave). It provisions pods, installs AI frameworks (ComfyUI, SwarmUI, Axolotl), and syncs workspaces to S3-compatible storage — all over direct SSH.

- **Language**: Python 3.11+
- **CLI framework**: Click
- **Terminal UI**: Rich (tables, progress bars)
- **HTTP**: httpx (provider APIs)
- **Storage**: boto3 (S3-compatible: B2, GCS, S3)
- **Config**: TOML at `~/.config/swm/config.toml`
- **Entry point**: `src/swm/cli.py` → `main()` Click group

## Development Environment

```bash
# Install in editable mode
pip install -e "."

# Run the CLI
swm --help
swm gpus -g h200

# Run with optional CoreWeave support
pip install -e ".[coreweave]"
```

There is no test suite yet. Verify changes by running the CLI directly.

## Codebase Structure

```
src/swm/
├── cli.py               # ALL user-facing commands (Click). Start here.
├── config.py            # TOML config read/write
├── bootstrap.py         # Remote pod setup: s5cmd, frameworks, workspace sync
├── pricing/
│   ├── providers.py     # Static GPU specs + pricing database (OFFERINGS list)
│   └── calculator.py    # Cost estimation engine
├── providers/
│   ├── base.py          # ABC: CloudProvider, GpuInfo, Instance, CreateConfig
│   ├── runpod.py        # RunPod (GraphQL via httpx)
│   ├── vastai.py        # Vast.ai (REST via httpx)
│   ├── lambda_labs.py   # Lambda Labs (REST via httpx)
│   ├── aws.py           # AWS EC2 (boto3)
│   ├── gcp.py           # GCP (gcloud CLI subprocess)
│   └── coreweave.py     # CoreWeave (Kubernetes python client)
├── frameworks/
│   ├── __init__.py      # Framework + Step dataclasses, registry
│   ├── comfyui.py       # ComfyUI framework definition
│   ├── swarmui.py       # SwarmUI framework definition
│   ├── axolotl.py       # Axolotl framework definition
│   └── llm_studio.py    # H2O LLM Studio framework definition
├── remote/
│   └── ssh.py           # SSH sessions, SCP, key management
└── storage/
    ├── base.py          # ABC: StorageProvider, S3CompatProvider
    ├── b2.py            # Backblaze B2
    ├── gcs.py           # Google Cloud Storage
    └── s3.py            # Amazon S3
```

## Code Style & Conventions

- **Imports**: `from __future__ import annotations` at the top of every file.
- **Type hints**: Use `X | None` (not `Optional[X]`), `list[X]` (not `List[X]`).
- **Dataclasses**: Use `@dataclass` for all data types. Use `field(default_factory=...)` for mutable defaults.
- **Provider pattern**: Every provider implements `CloudProvider` ABC from `base.py`. The `list_gpus(gpu_count)` method must accept an optional `gpu_count` parameter.
- **Config access**: Always go through `swm.config.get()` / `swm.config.set_value()`. Never read env vars for config.
- **SSH execution**: Use `RemoteSession.exec()` for running commands on pods. Non-interactive SSH (command passed as argument to `ssh`, not piped to stdin).
- **Storage**: All providers use the S3 API via boto3. No provider-specific CLIs for data operations.
- **CLI output**: Use `rich.console.Console` and `rich.table.Table` for all terminal output. No bare `print()`.
- **Error handling**: Raise `click.ClickException` for user-facing errors in CLI commands. Raise `RuntimeError` in provider/bootstrap code.
- **No comments that narrate code**. Comments only for non-obvious intent or tradeoffs.

## Critical Files

- `src/swm/cli.py` — All commands. Most changes start here.
- `src/swm/providers/base.py` — Core types: `GpuInfo`, `Instance`, `CreateConfig`, `CloudProvider` ABC.
- `src/swm/bootstrap.py` — Remote setup logic: `install_framework()`, `workspace_pull()`, `workspace_push()`.
- `src/swm/remote/ssh.py` — SSH session lifecycle. `exec()` runs commands, `connect()` probes readiness.
- `src/swm/pricing/providers.py` — Static `OFFERINGS` list and `GPU_SPECS`. Update when adding pricing data.

## Boundaries

### Always

- Keep edits minimal and specific to the task.
- Use existing patterns (provider ABC, dataclass types, Click options).
- Verify the CLI loads: `python3 -c "from swm.cli import main"`.

### Ask First

- Changing the `CloudProvider` ABC signature (affects all 6 providers).
- Adding new dependencies to `pyproject.toml`.
- Modifying SSH connection logic in `ssh.py`.

### Never

- Store credentials on the pod filesystem.
- Use `print()` instead of `console.print()`.
- Add provider-specific CLI tools as hard dependencies (b2, gcloud, aws).
- Delete user data or workspaces without explicit confirmation.
