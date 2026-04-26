"""swm models — search, download, and manage AI models on pods."""
from __future__ import annotations

import click

from swm.commands._helpers import (
    console,
    _instance_for,
    complete_pod_id,
    pod_arg_callback,
)

_VLLM_HF_CACHE = "/workspace/vllm/hf_cache"
_VLLM_MODEL_FILE = "/workspace/vllm/model.txt"
_OLLAMA_MODELS = "/workspace/ollama/models"
_OLLAMA_BIN = "/usr/local/bin/ollama"


def _humanize_count(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _is_ollama_model(name: str) -> bool:
    """Ollama models use 'name:tag' format; HuggingFace models use 'org/name'."""
    return "/" not in name


# ── Command group ────────────────────────────────────────────────────


@click.group(name="models")
def models_group():
    """Search, download, and manage AI models on your pods."""


# ── search ───────────────────────────────────────────────────────────


@models_group.command(name="search")
@click.argument("query")
@click.option(
    "--sort",
    default="downloads",
    type=click.Choice(["downloads", "likes", "trending"], case_sensitive=False),
    help="Sort order (default: downloads)",
)
@click.option("--limit", "-n", default=15, type=int, help="Max results")
@click.option("--all-types", is_flag=True, help="Include non-LLM models (image, audio, etc.)")
def models_search(query: str, sort: str, limit: int, all_types: bool):
    """Search HuggingFace Hub for models.

    \b
    Examples:
      swm models search "qwen3 instruct"
      swm models search "llama 4" --sort likes
      swm models search "stable diffusion" --all-types
    """
    from rich.table import Table
    from swm.models.huggingface import search

    pipeline = None if all_types else "text-generation"

    with console.status("Searching HuggingFace Hub…", spinner="dots"):
        results = search(query, sort=sort, limit=limit, pipeline=pipeline)

    if not results:
        console.print("[yellow]No models found.[/yellow]")
        return

    table = Table(title=f"HuggingFace Models — \"{query}\"", show_lines=True)
    table.add_column("Model", style="bold cyan", min_width=30, no_wrap=True)
    table.add_column("Size", justify="right")
    table.add_column("Downloads", justify="right", style="green")
    table.add_column("Likes", justify="right")
    table.add_column("Access", justify="center")
    table.add_column("Library", style="dim")

    for r in results:
        if r.gated:
            access = "[yellow]token[/yellow]"
        else:
            access = "[green]open[/green]"
        table.add_row(
            r.model_id,
            r.size_label or "—",
            _humanize_count(r.downloads),
            f"♥ {_humanize_count(r.likes)}",
            access,
            r.library or "—",
        )

    console.print()
    console.print(table)
    console.print()
    console.print(
        "[bold]Next →[/bold]  swm models pull <pod-id> <model-id>"
    )
    console.print(
        "[dim]Gated models need:  swm config set hf_token <your-token>[/dim]"
    )


# ── pull ─────────────────────────────────────────────────────────────


@models_group.command(name="pull")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
@click.argument("model")
@click.option("--token", default=None, help="HuggingFace token (overrides swm config)")
def models_pull(instance_id: str, model: str, token: str | None):
    """Download a model to a running pod.

    \b
    Auto-detects engine from model name:
      org/model-name  →  HuggingFace download (for vLLM)
      model:tag       →  Ollama pull

    \b
    Examples:
      swm models pull runpod:abc123 Qwen/Qwen3-8B
      swm models pull runpod:abc123 deepseek-r1:14b
      swm models pull runpod:abc123 meta-llama/Llama-4-Scout --token hf_xxx
    """
    inst = _instance_for(instance_id)

    if _is_ollama_model(model):
        _pull_ollama(inst, model)
    else:
        from swm import config as cfg

        hf_token = token or cfg.get("hf_token")
        _pull_huggingface(inst, model, hf_token)


def _pull_huggingface(inst, model: str, token: str | None) -> None:
    from swm.remote.ssh import session_from_instance

    console.print(f"\n[bold]Downloading [cyan]{model}[/cyan] via HuggingFace…[/bold]")
    console.print(f"[dim]Cache → {_VLLM_HF_CACHE}[/dim]\n")

    token_env = f"HF_TOKEN='{token}' " if token else ""
    hf_env = f"{token_env}HF_HOME={_VLLM_HF_CACHE}"

    cmd = (
        f"mkdir -p {_VLLM_HF_CACHE} && "
        f"if [ -x /workspace/vllm/venv/bin/huggingface-cli ]; then "
        f"  {hf_env} /workspace/vllm/venv/bin/huggingface-cli download {model}; "
        f"elif command -v huggingface-cli >/dev/null 2>&1; then "
        f"  {hf_env} huggingface-cli download {model}; "
        f"else "
        f"  pip install -q 'huggingface_hub[cli]' && "
        f"  {hf_env} huggingface-cli download {model}; "
        f"fi"
    )

    with session_from_instance(inst) as sess:
        exit_code, _, _ = sess.exec(cmd)

    if exit_code != 0:
        raise click.ClickException(f"Failed to download {model}")

    console.print(f"\n[green]✓[/green] [bold]{model}[/bold] downloaded")
    console.print(
        f"[dim]Activate for vLLM:  swm models set {inst.qualified_id} {model}[/dim]"
    )


def _pull_ollama(inst, model: str) -> None:
    from swm.remote.ssh import session_from_instance

    console.print(f"\n[bold]Pulling [cyan]{model}[/cyan] via Ollama…[/bold]")
    console.print(f"[dim]Storage → {_OLLAMA_MODELS}[/dim]\n")

    env = f"OLLAMA_MODELS={_OLLAMA_MODELS} OLLAMA_HOST=0.0.0.0:11434"

    with session_from_instance(inst) as sess:
        sess.exec(
            f"pgrep -f 'ollama serve' > /dev/null 2>&1 || "
            f"({env} nohup {_OLLAMA_BIN} serve > /tmp/ollama-pull.log 2>&1 &) "
            f"&& sleep 2",
            stream=False,
        )
        exit_code, _, _ = sess.exec(f"{env} {_OLLAMA_BIN} pull {model}")

    if exit_code != 0:
        raise click.ClickException(f"Failed to pull {model}")

    console.print(f"\n[green]✓[/green] [bold]{model}[/bold] ready")
    console.print("[dim]Model is available in Open WebUI immediately[/dim]")


# ── set (activate for vLLM) ─────────────────────────────────────────


@models_group.command(name="set")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
@click.argument("model")
@click.option(
    "--restart/--no-restart",
    default=True,
    help="Restart vLLM after setting (default: yes)",
)
def models_set(instance_id: str, model: str, restart: bool):
    """Set the active model for vLLM and restart the server.

    \b
    Examples:
      swm models set runpod:abc123 Qwen/Qwen3.5-72B
      swm models set runpod:abc123 Qwen/Qwen3-8B --no-restart
    """
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)

    console.print(f"\n[bold]Setting active model → [cyan]{model}[/cyan][/bold]")

    with session_from_instance(inst) as sess:
        sess.exec(f"mkdir -p $(dirname {_VLLM_MODEL_FILE}) && echo '{model}' > {_VLLM_MODEL_FILE}", stream=False)
        console.print(f"  [green]✓[/green] Model file updated")

        if not restart:
            console.print(
                f"[dim]Restart manually:  swm setup stop vllm {inst.qualified_id} && "
                f"swm setup start vllm {inst.qualified_id}[/dim]\n"
            )
            return

        from swm.bootstrap_frameworks import stop_framework, start_framework

        _, out, _ = sess.exec(
            "pgrep -f 'vllm serve' > /dev/null 2>&1 && echo running || echo stopped",
            stream=False,
        )
        if "running" in out:
            stop_framework(sess, "vllm", console=console)

        import time
        time.sleep(2)

        _, gpu_out, _ = sess.exec(
            "nvidia-smi -L 2>/dev/null | wc -l | tr -d ' '",
            stream=False,
        )
        tp = gpu_out.strip() or "1"
        console.print(f"\n[bold]Starting vLLM with [cyan]{model}[/cyan] on {tp} GPU(s)…[/bold]")

        try:
            start_framework(
                sess,
                "vllm",
                port=8000,
                console=console,
                qualified_id=inst.qualified_id,
            )
        except RuntimeError:
            console.print(
                "\n[yellow]vLLM is loading the model in the background.[/yellow]"
            )
            console.print(
                f"[dim]Watch progress:  swm run {inst.qualified_id} "
                "'tail -f /tmp/vllm.log'[/dim]"
            )
            return

    console.print(
        f"\n[dim]The model may take a few minutes to load weights. "
        f"Monitor with:\n  swm run {inst.qualified_id} 'tail -f /tmp/vllm.log'[/dim]\n"
    )


# ── list ─────────────────────────────────────────────────────────────


@models_group.command(name="list")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
def models_list(instance_id: str):
    """List downloaded models on a pod.

    \b
    Shows both HuggingFace (vLLM) and Ollama models.
    """
    from rich.table import Table
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)

    with console.status("Fetching model inventory…", spinner="dots"):
        with session_from_instance(inst) as sess:
            _, active_raw, _ = sess.exec(
                f"cat {_VLLM_MODEL_FILE} 2>/dev/null || echo ''",
                stream=False,
            )
            active_model = active_raw.strip()

            _, hf_raw, _ = sess.exec(
                f"ls -1d {_VLLM_HF_CACHE}/hub/models--* 2>/dev/null "
                "| xargs -I{{}} basename {{}} "
                "| sed 's/^models--//' | sed 's/--/\\//g'",
                stream=False,
            )
            hf_models = [
                m.strip() for m in hf_raw.strip().splitlines() if m.strip()
            ]

            _, ollama_raw, _ = sess.exec(
                f"OLLAMA_MODELS={_OLLAMA_MODELS} {_OLLAMA_BIN} list 2>/dev/null || true",
                stream=False,
            )
            ollama_lines = [
                ln.strip()
                for ln in ollama_raw.strip().splitlines()
                if ln.strip() and not ln.strip().upper().startswith("NAME")
            ]

    has_any = bool(hf_models or ollama_lines)

    if hf_models:
        table = Table(title="vLLM Models (HuggingFace)", show_lines=True)
        table.add_column("Model", style="bold cyan", min_width=35)
        table.add_column("Status", justify="center")

        for m in sorted(hf_models):
            status = (
                "[green bold]● active[/green bold]"
                if m == active_model
                else "[dim]cached[/dim]"
            )
            table.add_row(m, status)

        console.print()
        console.print(table)

    if ollama_lines:
        table = Table(title="Ollama Models", show_lines=True)
        table.add_column("Name", style="bold cyan")
        table.add_column("Size", justify="right")
        table.add_column("Quantization", style="dim")

        for line in ollama_lines:
            parts = line.split()
            name = parts[0] if parts else line
            size = ""
            quant = ""
            for p in parts[1:]:
                if any(c.isdigit() for c in p) and ("GB" in p.upper() or "MB" in p.upper()):
                    size = p
                elif p.upper().startswith("Q") or "fp" in p.lower():
                    quant = p
            table.add_row(name, size or "—", quant or "—")

        console.print()
        console.print(table)

    if not has_any:
        console.print("\n[yellow]No models found on this pod.[/yellow]")

    console.print()
    if not hf_models:
        console.print(
            f"[dim]Pull a vLLM model:   swm models pull {inst.qualified_id} Qwen/Qwen3-8B[/dim]"
        )
    if not ollama_lines:
        console.print(
            f"[dim]Pull an Ollama model: swm models pull {inst.qualified_id} llama3.2:3b[/dim]"
        )
    if hf_models and active_model:
        console.print(
            f"[dim]Switch model:  swm models set {inst.qualified_id} <model>[/dim]"
        )
    console.print()


# ── remove ───────────────────────────────────────────────────────────


@models_group.command(name="remove")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
@click.argument("model")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def models_remove(instance_id: str, model: str, yes: bool):
    """Remove a downloaded model from the pod.

    \b
    Examples:
      swm models remove runpod:abc123 Qwen/Qwen3-8B
      swm models remove runpod:abc123 llama3.2:3b -y
    """
    from swm.remote.ssh import session_from_instance

    if not yes:
        click.confirm(f"Remove {model} from pod?", abort=True)

    inst = _instance_for(instance_id)

    with session_from_instance(inst) as sess:
        if _is_ollama_model(model):
            exit_code, _, _ = sess.exec(
                f"OLLAMA_MODELS={_OLLAMA_MODELS} {_OLLAMA_BIN} rm {model}",
            )
        else:
            cache_name = model.replace("/", "--")
            exit_code, _, _ = sess.exec(
                f"rm -rf {_VLLM_HF_CACHE}/hub/models--{cache_name} && "
                f"rm -rf {_VLLM_HF_CACHE}/hub/.locks/models--{cache_name}",
            )

    if exit_code != 0:
        raise click.ClickException(f"Failed to remove {model}")

    console.print(f"\n[green]✓[/green] Removed [bold]{model}[/bold]\n")


# ── info ─────────────────────────────────────────────────────────────


@models_group.command(name="info")
@click.argument("model")
@click.option("--token", default=None, help="HuggingFace token for gated models")
def models_info(model: str, token: str | None):
    """Show details for a HuggingFace model.

    \b
    Example:
      swm models info Qwen/Qwen3-8B
    """
    from rich.panel import Panel
    from rich.table import Table
    from swm.models.huggingface import model_info
    from swm import config as cfg

    hf_token = token or cfg.get("hf_token")

    with console.status("Fetching model info…", spinner="dots"):
        try:
            info = model_info(model, token=hf_token)
        except Exception as exc:
            raise click.ClickException(str(exc))

    safetensors = info.get("safetensors", {})
    param_count = safetensors.get("total") if isinstance(safetensors, dict) else None
    params_str = _humanize_count(param_count) if param_count else "—"

    gated = info.get("gated", False)
    access = "[yellow]requires token[/yellow]" if gated else "[green]open access[/green]"

    siblings = info.get("siblings", [])
    total_size = sum(s.get("size", 0) for s in siblings if isinstance(s, dict))
    size_str = f"{total_size / 1e9:.1f} GB" if total_size > 0 else "—"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()

    table.add_row("Model", f"[bold cyan]{info.get('id', model)}[/bold cyan]")
    table.add_row("Author", info.get("author", "—"))
    table.add_row("Parameters", params_str)
    table.add_row("Download Size", size_str)
    table.add_row("Downloads", _humanize_count(info.get("downloads", 0)))
    table.add_row("Likes", f"♥ {_humanize_count(info.get('likes', 0))}")
    table.add_row("Access", access)
    table.add_row("Pipeline", info.get("pipeline_tag", "—"))
    table.add_row("Library", info.get("library_name", "—"))
    table.add_row("License", _extract_license(info.get("tags", [])))
    table.add_row("Last Updated", (info.get("lastModified", "—") or "—")[:10])

    console.print()
    console.print(Panel(table, title=model, border_style="cyan"))
    console.print()
    console.print(f"[bold]Pull →[/bold]  swm models pull <pod-id> {model}")
    console.print(f"[dim]Web  →  https://huggingface.co/{model}[/dim]\n")


def _extract_license(tags: list[str]) -> str:
    """Pull the license tag out of the HuggingFace tags list."""
    for t in tags:
        if t.startswith("license:"):
            return t.split(":", 1)[1]
    return "—"
