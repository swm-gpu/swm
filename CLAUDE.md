# CLAUDE.md

Project context for Claude Code and Cursor sessions.

## What This Is

`swm` is a Python CLI for managing cloud GPU workflows. It provisions pods across 6 providers, installs AI frameworks, and syncs workspaces to S3-compatible storage — all over SSH.

## Common Commands

```bash
# Install and run
pip install -e "."
swm --help

# Verify changes load
python3 -c "from swm.cli import main; print('OK')"

# Test GPU search (hits live APIs for configured providers)
swm gpus -g h200
swm gpus -g h200 -c 4 --secure

# Check static pricing
swm pricing compare --gpu h200
```

There are no automated tests. Verify by running the CLI.

## Architecture

The CLI is in `src/swm/cli.py`. Every command is a Click command/group under `main()`.

Key flow: `swm gpus` → `swm pod create` → `swm setup install` → `swm sync pull/push` → `swm pod down`.

All remote operations go through `RemoteSession` in `src/swm/remote/ssh.py` using non-interactive SSH (`ssh host command`, not `ssh -tt` with stdin piping).

Providers implement `CloudProvider` ABC (`src/swm/providers/base.py`). Storage providers implement `S3CompatProvider` (`src/swm/storage/base.py`).

Frameworks are defined declaratively in `src/swm/frameworks/` as `Framework` dataclasses with `Step` lists.

## Code Style

- `from __future__ import annotations` everywhere
- `X | None` not `Optional[X]`; `list[X]` not `List[X]`
- `@dataclass` for data types
- `rich.console.Console` for all output, never `print()`
- `click.ClickException` for user-facing errors
- No comments that just narrate what code does

## Key Types

- `GpuInfo` — Normalized GPU availability (provider, type, vram, price, stock, gpu_count)
- `Instance` — Running pod (provider, id, gpu_type, gpu_count, status, ssh_host, ports)
- `CreateConfig` — Pod creation parameters
- `Framework` / `Step` — Declarative framework installation definitions
- `RemoteSession` — SSH connection to a pod

## Things To Know

- `GpuInfo.gpu_count` (not `min_gpu_count`) represents the specific GPU config.
- `list_gpus(gpu_count=N)` is parameterized — RunPod passes it to GraphQL, Vast.ai filters the API query.
- `OFFERINGS` in `pricing/providers.py` is static reference data. `swm gpus` merges it with live API results.
- Providers without `CloudProvider` implementations (Runcrate, Spheron, Nebius, Azure, Cudo) appear as "static" rows from `OFFERINGS`.
- Storage credentials are never written to pods. They're passed as env vars on each s5cmd invocation.
- Vast.ai uses direct SSH (public IP + port from `Instance.ports`), not the SSH relay.
