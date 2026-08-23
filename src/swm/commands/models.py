"""swm models -- search, pull, link, and manage AI models on pods.

All assets land under ``/workspace/models/`` on the pod with per-asset-type
subdirectories.  Framework installs (vLLM, Ollama, ComfyUI, SwarmUI) bind their
expected model paths into the same unified store via symlinks so downloaded
models become visible without further configuration.

Manifest of every download lives at ``/workspace/models/.manifest.json``.
"""
from __future__ import annotations

import re
import shlex

import click

from swm.commands._helpers import (
    console,
    _instance_for,
    absorb_pod_positional,
    complete_pod_id,
    pod_arg_callback,
)

_MODELS_ROOT = "/workspace/models"


def _humanize_count(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _humanize_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _ensure_models_root_cmd() -> str:
    return f"mkdir -p {_MODELS_ROOT}"


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
@click.option(
    "--all-types",
    is_flag=True,
    help="Include non-LLM models (image, audio, etc.)",
)
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

    with console.status("Searching HuggingFace Hub\u2026", spinner="dots"):
        results = search(query, sort=sort, limit=limit, pipeline=pipeline)

    if not results:
        console.print("[yellow]No models found.[/yellow]")
        return

    table = Table(title=f'HuggingFace Models -- "{query}"', show_lines=True)
    table.add_column("Model", style="bold cyan", min_width=35, overflow="fold")
    table.add_column("Size", justify="right", min_width=8, no_wrap=True)
    table.add_column("Downloads", justify="right", style="green", min_width=10, no_wrap=True)
    table.add_column("Likes", justify="right", min_width=7, no_wrap=True)
    table.add_column("Access", justify="center", min_width=8, no_wrap=True)
    table.add_column("Library", style="dim", min_width=12, no_wrap=True)

    for r in results:
        if r.gated:
            access = "[yellow]token[/yellow]"
        else:
            access = "[green]open[/green]"
        table.add_row(
            r.model_id,
            r.size_label or "\u2014",
            _humanize_count(r.downloads),
            f"\u2665 {_humanize_count(r.likes)}",
            access,
            r.library or "\u2014",
        )

    console.print()
    console.print(table)
    console.print()
    console.print("[bold]Next \u2192[/bold]  swm models pull <pod-id> <model-id>")
    console.print(
        "[dim]Gated models need:  swm config set hf.api_key <your-token>[/dim]"
    )


# ── info ─────────────────────────────────────────────────────────────


@models_group.command(name="info")
@click.argument("ref")
@click.option("--token", default=None, help="API token (HF or Civitai, auto-detected)")
def models_info(ref: str, token: str | None):
    """Show details for a HuggingFace or Civitai model.

    \b
    Examples:
      swm models info Qwen/Qwen3-8B
      swm models info civitai:101055
      swm models info https://civitai.com/models/101055
    """
    from rich.panel import Panel
    from rich.table import Table
    from swm.models import civitai, huggingface

    if civitai.is_civitai_ref(ref):
        _show_civitai(ref, token)
        return

    hf_token = huggingface.get_token(token)
    with console.status("Fetching model info\u2026", spinner="dots"):
        try:
            info = huggingface.model_info(ref, token=hf_token)
        except Exception as exc:
            raise click.ClickException(str(exc))

    safetensors = info.get("safetensors", {})
    param_count = safetensors.get("total") if isinstance(safetensors, dict) else None
    params_str = _humanize_count(param_count) if param_count else "\u2014"

    gated = info.get("gated", False)
    access = "[yellow]requires token[/yellow]" if gated else "[green]open access[/green]"

    siblings = info.get("siblings", [])
    total_size = sum(s.get("size", 0) for s in siblings if isinstance(s, dict))
    size_str = f"{total_size / 1e9:.1f} GB" if total_size > 0 else "\u2014"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()

    table.add_row("Model", f"[bold cyan]{info.get('id', ref)}[/bold cyan]")
    table.add_row("Author", info.get("author", "\u2014"))
    table.add_row("Parameters", params_str)
    table.add_row("Download Size", size_str)
    table.add_row("Downloads", _humanize_count(info.get("downloads", 0)))
    table.add_row("Likes", f"\u2665 {_humanize_count(info.get('likes', 0))}")
    table.add_row("Access", access)
    table.add_row("Pipeline", info.get("pipeline_tag", "\u2014"))
    table.add_row("Library", info.get("library_name", "\u2014"))
    table.add_row("License", _extract_license(info.get("tags", [])))
    table.add_row("Last Updated", (info.get("lastModified", "\u2014") or "\u2014")[:10])

    console.print()
    console.print(Panel(table, title=ref, border_style="cyan"))
    console.print()
    console.print(f"[bold]Pull \u2192[/bold]  swm models pull <pod-id> {ref}")
    console.print(f"[dim]Web  \u2192  https://huggingface.co/{ref}[/dim]\n")


def _show_civitai(ref: str, token: str | None) -> None:
    from rich.panel import Panel
    from rich.table import Table
    from swm.models import civitai

    civitai_token = civitai.get_token(token)
    with console.status("Fetching Civitai info\u2026", spinner="dots"):
        try:
            info = civitai.model_info(ref, token=civitai_token)
        except Exception as exc:
            raise click.ClickException(str(exc))

    version = info.primary_version()
    primary = next(
        (f for f in version.files if f.file_type == "Model"),
        version.files[0] if version.files else None,
    )
    size_str = (
        f"{primary.size_kb / 1024 / 1024:.2f} GB" if primary and primary.size_kb else "\u2014"
    )

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Model", f"[bold cyan]{info.name}[/bold cyan]")
    table.add_row("Creator", info.creator or "\u2014")
    table.add_row("Type", info.asset_type)
    table.add_row("Base", version.base_model or "\u2014")
    table.add_row("Version", version.name or "\u2014")
    table.add_row("File", primary.name if primary else "\u2014")
    table.add_row("Size", size_str)
    table.add_row("NSFW", "yes" if info.nsfw else "no")
    table.add_row("Canonical ref", f"civitai:{info.model_id}:{version.version_id}")

    console.print()
    console.print(Panel(table, title=f"civitai:{info.model_id}", border_style="cyan"))
    console.print()
    console.print(
        f"[bold]Pull \u2192[/bold]  swm models pull <pod-id> "
        f"civitai:{info.model_id}:{version.version_id} --as {info.asset_type}"
    )
    console.print(
        f"[dim]Web  \u2192  https://civitai.com/models/{info.model_id}[/dim]\n"
    )


def _extract_license(tags: list[str]) -> str:
    for t in tags:
        if t.startswith("license:"):
            return t.split(":", 1)[1]
    return "\u2014"


# ── pull ─────────────────────────────────────────────────────────────


_ASSET_CHOICES = click.Choice(
    [
        "llm", "llm-gguf", "ollama",
        "checkpoint", "lora", "vae", "controlnet", "embedding",
        "clip", "clip-vision", "upscaler", "unet",
        "diffusion", "text-encoder", "file",
    ],
    case_sensitive=False,
)

_SOURCE_CHOICES = click.Choice(
    ["hf", "ollama", "civitai", "url"],
    case_sensitive=False,
)


@models_group.command(name="pull")
@click.argument(
    "instance_id",
    required=False,
    shell_complete=complete_pod_id,
)
@click.argument("ref", required=False)
@click.option(
    "--as", "asset_type",
    type=_ASSET_CHOICES,
    default=None,
    help="Override the auto-detected asset type",
)
@click.option(
    "--source",
    type=_SOURCE_CHOICES,
    default=None,
    help="Override the auto-detected source (hf|ollama|civitai|url)",
)
@click.option("--token", default=None, help="API token (HF or Civitai)")
@click.option(
    "--filename",
    default=None,
    help=(
        "HF: repo file path to download (supports nested paths like "
        "split_files/loras/foo.safetensors). "
        "URL/Civitai: override the saved filename."
    ),
)
def models_pull(
    instance_id: str,
    ref: str,
    asset_type: str | None,
    source: str | None,
    token: str | None,
    filename: str | None,
):
    """Download a model to a pod's unified model store.

    \b
    Auto-detects the source from the ref:
      org/name         -> HuggingFace (vLLM / Axolotl / transformers)
      name:tag         -> Ollama
      civitai:<id>     -> Civitai (ComfyUI checkpoints / LoRAs / VAEs / ...)
      https://...      -> direct URL download

    \b
    Examples:
      swm models pull pod:abc Qwen/Qwen3-8B
      swm models pull pod:abc deepseek-r1:14b
      swm models pull pod:abc civitai:101055 --as checkpoint
      swm models pull pod:abc https://example.com/lora.safetensors --as lora
    """
    from swm.models import civitai, huggingface, resolver
    from swm.models.manifest import ModelEntry, RemoteManifest, make_key
    from swm.remote.ssh import session_from_instance

    instance_id, (ref,) = absorb_pod_positional(
        instance_id, (ref,), ("REF",),
    )
    inst = _instance_for(instance_id)

    hf_token = huggingface.get_token(token)
    civitai_token = civitai.get_token(token)

    with console.status("Resolving model reference\u2026", spinner="dots"):
        try:
            resolved = resolver.resolve(
                ref,
                source=source,
                asset_type=asset_type,
                hf_token=hf_token,
                civitai_token=civitai_token,
            )
        except ValueError as exc:
            raise click.UsageError(str(exc))

    console.print(
        f"\n[bold]Pulling [cyan]{resolved.display_name}[/cyan][/bold]  "
        f"[dim](source={resolved.source}, type={resolved.asset_type})[/dim]"
    )

    missing_engine: str | None = None
    with session_from_instance(inst) as sess:
        sess.exec(_ensure_models_root_cmd(), stream=False)

        missing_engine = _missing_consumer(sess, resolved.asset_type)
        if missing_engine:
            console.print(
                f"\n[yellow]\u26a0 Nothing installed on this pod can load a "
                f"{resolved.asset_type} model.[/yellow]\n"
                f"[yellow]  The model will be staged but cannot be used until you run:[/yellow]\n"
                f"[bold]     swm setup install {missing_engine} {inst.qualified_id}[/bold]\n"
            )

        if resolved.source == "hf":
            entries = _pull_hf(sess, resolved, hf_token, filename)
        elif resolved.source == "ollama":
            entries = [_pull_ollama(sess, resolved, hf_token=hf_token)]
        elif resolved.source == "civitai":
            entries = [_pull_civitai(sess, resolved, civitai_token, filename)]
        elif resolved.source == "url":
            entries = [_pull_url(sess, resolved, filename)]
        else:
            raise click.ClickException(f"Unhandled source {resolved.source!r}")

        rm = RemoteManifest(sess)
        for entry in entries:
            rm.upsert(entry)

    if len(entries) == 1:
        console.print(
            f"\n[green]\u2713[/green] [bold]{resolved.display_name}[/bold] staged at "
            f"[dim]{entries[0].path}[/dim]"
        )
    else:
        console.print(f"\n[green]\u2713[/green] [bold]{resolved.display_name}[/bold] staged:")
        for entry in entries:
            console.print(f"  [dim]{entry.path}[/dim]")

    if missing_engine:
        console.print(
            f"\n[yellow]\u26a0 Remember to install the engine before using it:[/yellow]\n"
            f"[bold]     swm setup install {missing_engine} {inst.qualified_id}[/bold]\n"
        )


def _probe_installed(sess, fw) -> bool:
    """Run a framework's presence probe and read the ANSWER, not the exit code.

    The command's exit code is useless here: ``probe && echo yes || echo no``
    exits 0 on both branches (the old code tested exit_code == 0 and
    therefore reported every framework as installed, always). Unique markers
    keep pod images with chatty shells from confusing the check; if neither
    marker comes back (SSH hiccup), assume installed — the lenient direction,
    matching historical behavior, so transient failures never block a pull.
    """
    _, out, _ = sess.exec(
        f"{fw.installed_probe} "
        f"&& echo __SWM_INSTALLED__ || echo __SWM_ABSENT__",
        stream=False,
    )
    if "__SWM_INSTALLED__" in out:
        return True
    if "__SWM_ABSENT__" in out:
        return False
    return True


def _missing_consumer(sess, asset_type: str) -> str | None:
    """Name a framework to install when nothing present can load *asset_type*.

    Asks the registry which frameworks consume the type, then probes each
    with its own installed_check. Returns None when at least one consumer is
    installed, or when the type has no consumers at all (plain files need no
    engine). The old version answered from two hardcoded tables and therefore
    claimed Civitai models always needed comfyui, even with SwarmUI present.
    """
    from swm.frameworks import consumers_of

    consumers = consumers_of(asset_type)
    if not consumers:
        return None
    for fw in consumers:
        if _probe_installed(sess, fw):
            return None
    return consumers[0].name


def _engine_installed(sess, framework_name: str) -> bool:
    """Is one specific framework present on the pod?

    The probe comes from the framework's own declaration rather than a local
    table, so a new framework never needs an entry here.
    """
    from swm.frameworks import get_framework

    try:
        fw = get_framework(framework_name)
    except KeyError:
        return True
    return _probe_installed(sess, fw)


def _pull_hf(sess, resolved, hf_token: str | None, filename_override: str | None) -> list:
    """Download an HF repo to /workspace/models/hf, or single-file GGUFs to /files."""
    from swm.models import resolver as r
    from swm.models.manifest import ModelEntry, make_key

    bucket = r.target_dir_for(resolved.asset_type)
    bucket_path = f"{_MODELS_ROOT}/{bucket}"

    if resolved.asset_type == "llm":
        # Full repo snapshot into HF cache layout.
        cmd = _hf_repo_download_cmd(resolved.ref, bucket_path, hf_token)
        sess.exec(f"mkdir -p {bucket_path}", stream=False)
        exit_code, _, _ = sess.exec(cmd)
        if exit_code != 0:
            raise click.ClickException(f"Failed to download {resolved.ref}")
        cache_dir = f"{bucket_path}/hub/models--{resolved.ref.replace('/', '--')}"
        size = _disk_usage(sess, cache_dir)
        return [ModelEntry(
            key=make_key("hf", resolved.ref),
            source="hf",
            ref=resolved.ref,
            asset_type=resolved.asset_type,
            path=cache_dir,
            size_bytes=size,
            display_name=resolved.display_name,
            extra={"hf_info": _slim_hf_info(resolved.extra)},
        )]

    # Single-file modes: gguf, checkpoint, lora, vae, etc. on HF.
    files = _pick_hf_files(resolved, filename_override)
    if not files:
        raise click.ClickException(
            f"Could not find a downloadable file in {resolved.ref}. "
            "Pass --filename with the repo path (e.g. split_files/loras/foo.safetensors)."
        )

    sess.exec(f"mkdir -p {bucket_path}", stream=False)
    entries: list = []
    for remote_filename, save_as in files:
        url = _hf_file_url(resolved.ref, remote_filename)
        dest = f"{bucket_path}/{save_as}"
        cmd = _hf_file_download_cmd(
            resolved.ref, remote_filename, bucket_path, dest, url, hf_token,
        )
        exit_code, _, _ = sess.exec(cmd)
        if exit_code != 0:
            hint = _hf_fetch_hint(resolved, remote_filename, filename_override)
            raise click.ClickException(
                f"Failed to fetch {remote_filename} from {resolved.ref}.{hint}"
            )
        size = _disk_usage(sess, dest)
        entries.append(ModelEntry(
            key=make_key("hf", resolved.ref, file_id=remote_filename),
            source="hf",
            ref=resolved.ref,
            asset_type=resolved.asset_type,
            path=dest,
            size_bytes=size,
            display_name=resolved.display_name,
            extra={"remote_path": remote_filename, "save_as": save_as},
        ))
    return entries


# Locates an hf CLI on the pod (PATH, then known framework venvs), derives
# its owning python, and best-effort enables hf_transfer. The env var is
# only exported when the package actually imports: huggingface_hub raises
# outright when HF_HUB_ENABLE_HF_TRANSFER=1 but hf_transfer is missing.
# Discovery order matters: `hf` only ships with modern huggingface_hub, so
# PATH-first is safe for it, while a PATH `huggingface-cli` may be an
# image-baked symlink-era relic (< 0.23) that must not shadow fresh venv
# CLIs — it is checked dead last. On hub >= 1.x the speedup actually comes
# from Xet and HF_HUB_ENABLE_HF_TRANSFER is deprecated; the hf_transfer
# gate still matters for the 0.2x-0.3x hubs where it is the speedup.
_HF_FIND_SNIPPET = (
    'HFBIN=""; '
    "command -v hf >/dev/null 2>&1 && HFBIN=$(command -v hf); "
    'if [ -z "$HFBIN" ]; then '
    "  for v in /workspace/vllm/venv /workspace/ComfyUI/venv; do "
    '    [ -x "$v/bin/hf" ] && HFBIN="$v/bin/hf" && break; '
    '    [ -x "$v/bin/huggingface-cli" ] && HFBIN="$v/bin/huggingface-cli" && break; '
    "  done; "
    "fi; "
    'if [ -z "$HFBIN" ]; then '
    "  command -v huggingface-cli >/dev/null 2>&1 "
    "    && HFBIN=$(command -v huggingface-cli); "
    "fi; "
    'HFXFER=0; '
    'if [ -n "$HFBIN" ]; then '
    '  HFPY="$(dirname "$HFBIN")/python3"; [ -x "$HFPY" ] || HFPY=python3; '
    '  "$HFPY" -c "import hf_transfer" 2>/dev/null '
    '    || "$HFPY" -m pip install -q hf_transfer 2>/dev/null || true; '
    '  "$HFPY" -c "import hf_transfer" 2>/dev/null && HFXFER=1; '
    "fi; "
)


def _hf_file_download_cmd(
    repo: str,
    remote_filename: str,
    bucket_path: str,
    dest: str,
    url: str,
    token: str | None,
) -> str:
    """Download one HF file fast, falling back to single-connection curl.

    ``hf download`` with hf_transfer fetches a file over many parallel
    range requests (~8x a single curl stream on datacenter links). The
    staging dir sits under ``<bucket>/.cache/`` — same filesystem as the
    destination (mv is a rename) and inside the sync excludes, so the
    autosync watcher can never ship a partial download.
    """
    token_env = f"HF_TOKEN={shlex.quote(token)} " if token else ""
    qd = shlex.quote(dest)
    tmp_parent = shlex.quote(f"{bucket_path}/.cache")
    return (
        f"{_HF_FIND_SNIPPET}"
        f"ok=0; "
        f'if [ -n "$HFBIN" ] && [ "$HFXFER" = "1" ]; then '
        f"  mkdir -p {tmp_parent} && "
        f"  tmp=$(mktemp -d {tmp_parent}/swm-hfdl-XXXXXX) && "
        f"  if {token_env}HF_HUB_ENABLE_HF_TRANSFER=1 "
        f"    PYTHONWARNINGS=ignore::FutureWarning "
        f'    "$HFBIN" download {shlex.quote(repo)} '
        f"    {shlex.quote(remote_filename)} "
        f'    --local-dir "$tmp"; then '
        # Symlink-era hubs (< 0.23) put a RELATIVE symlink into the hub
        # cache at $tmp/<file>; moving it would dangle. Only accept a
        # regular file — anything else falls through to curl.
        f'    mv -f "$tmp/"{shlex.quote(remote_filename)} {qd} '
        f"      && [ ! -L {qd} ] && ok=1; "
        f"  fi; "
        f'  rm -rf "$tmp"; '
        f"fi; "
        f'if [ "$ok" != "1" ]; then '
        f"  [ -L {qd} ] && rm -f {qd}; "
        f"  {_curl_to_file(url, dest, token)}; "
        f"fi && [ -s {qd} ] && [ ! -L {qd} ]"
    )


def _hf_repo_download_cmd(ref: str, cache_dir: str, token: str | None) -> str:
    token_env = f"HF_TOKEN={shlex.quote(token)} " if token else ""
    hf_env = f"{token_env}HF_HOME={shlex.quote(cache_dir)}"
    qref = shlex.quote(ref)
    return (
        f"mkdir -p {shlex.quote(cache_dir)} && "
        f"{_HF_FIND_SNIPPET}"
        f'if [ -z "$HFBIN" ]; then '
        f"  pip install -q 'huggingface_hub[cli]' && "
        f"  HFBIN=$(command -v hf || command -v huggingface-cli); "
        f"fi && "
        f'if [ "$HFXFER" = "1" ]; then '
        # Retry plainly if the accelerated attempt fails — covers CLIs
        # whose interpreter the HFPY heuristic mis-detected (hub < 1.0
        # raises when the env var is set without the package).
        f"  {hf_env} HF_HUB_ENABLE_HF_TRANSFER=1 "
        f'    PYTHONWARNINGS=ignore::FutureWarning "$HFBIN" download {qref} '
        f'  || {hf_env} "$HFBIN" download {qref}; '
        f"else "
        f'  {hf_env} "$HFBIN" download {qref}; '
        f"fi"
    )


def _pick_hf_files(resolved, filename_override: str | None) -> list[tuple[str, str]]:
    """Return ``[(remote_path, save_as)]`` to download from the HF repo.

    For HF, ``--filename`` means "download this exact file from the repo"; the
    local name is the basename of that remote path.
    """
    info = (resolved.extra or {}).get("hf_info", {})
    siblings = info.get("siblings", []) or []
    candidates = [s.get("rfilename") for s in siblings if isinstance(s, dict)]
    candidates = [c for c in candidates if c]

    if filename_override:
        if filename_override in candidates:
            remote = filename_override
        else:
            base = filename_override.rsplit("/", 1)[-1]
            basename_matches = [c for c in candidates if c.rsplit("/", 1)[-1] == base]
            if len(basename_matches) == 1:
                remote = basename_matches[0]
            else:
                remote = filename_override
        return [(remote, remote.rsplit("/", 1)[-1])]

    ext_priority = {
        "llm-gguf": (".gguf",),
        "checkpoint": (".safetensors", ".ckpt"),
        "lora": (".safetensors", ".pt", ".bin"),
        "vae": (".safetensors", ".pt"),
        "controlnet": (".safetensors", ".pth"),
        "embedding": (".safetensors", ".pt", ".bin"),
        "upscaler": (".safetensors", ".pth"),
    }.get(resolved.asset_type, (".safetensors",))

    matches = [c for c in candidates if c.lower().endswith(ext_priority)]
    if not matches:
        return []
    # Prefer files at the repo root over nested ones; tie-break by name length.
    matches.sort(key=lambda c: (c.count("/"), len(c)))
    chosen = matches[0]
    return [(chosen, chosen.rsplit("/", 1)[-1])]


def _hf_file_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{path}"


def _hf_fetch_hint(resolved, remote_filename: str, filename_override: str | None) -> str:
    """Suggest nested repo paths when a bare basename 404s."""
    info = (resolved.extra or {}).get("hf_info", {})
    siblings = info.get("siblings", []) or []
    candidates = [
        s.get("rfilename") for s in siblings if isinstance(s, dict) and s.get("rfilename")
    ]
    if not candidates:
        return " Run `swm models info` on the repo to inspect available files."

    needle = (filename_override or remote_filename).rsplit("/", 1)[-1]
    matches = [c for c in candidates if c.endswith(needle) or c.rsplit("/", 1)[-1] == needle]
    if not matches:
        return " Run `swm models info` on the repo to inspect available files."
    if len(matches) == 1:
        return f" Did you mean --filename {matches[0]!r}?"
    preview = ", ".join(matches[:3])
    suffix = f" (+{len(matches) - 3} more)" if len(matches) > 3 else ""
    return f" Matching repo paths: {preview}{suffix}."


def _curl_to_file(url: str, dest: str, token: str | None) -> str:
    auth = f"-H 'Authorization: Bearer {token}' " if token else ""
    qd = shlex.quote(dest)
    return (
        f"curl -fL --retry 3 --retry-delay 2 {auth}-o {qd} {shlex.quote(url)} && "
        f"[ -s {qd} ]"
    )


def _fast_url_download_cmd(url: str, bucket_path: str, dest: str) -> str:
    """Download an arbitrary URL fast, falling back to single-stream curl.

    aria2c fetches one file over up to 16 parallel range requests — the
    same trick that makes hf_transfer ~8x faster than a lone curl stream.
    It's installed on demand the way swm already installs inotify-tools;
    any miss (no apt, no sudo, aria2c failure, server without range
    support) degrades to the plain curl path. Staging lives under the
    destination bucket's ``.cache/`` — same filesystem (mv is a rename)
    and inside the sync excludes, so partial downloads never sync.

    Known trade-off: on failure aria2c echoes the URL, which for Civitai
    embeds the ``?token=`` — the same transient exposure the curl status
    quo has (URL on the command line / in ps). Moving Civitai auth to a
    header would eliminate it and is tracked as a follow-up.
    """
    from swm.bootstrap import _privileged

    qd = shlex.quote(dest)
    qu = shlex.quote(url)
    tmp_parent = shlex.quote(f"{bucket_path}/.cache")
    ensure = _privileged(
        "command -v aria2c >/dev/null 2>&1 || "
        "($SUDO apt-get update -qq && "
        "$SUDO apt-get install -y -qq aria2) >/dev/null 2>&1 || true"
    )
    return (
        f"{ensure}; ok=0; "
        f"if command -v aria2c >/dev/null 2>&1; then "
        f"  mkdir -p {tmp_parent} && "
        f"  tmp=$(mktemp -d {tmp_parent}/swm-dl-XXXXXX) && "
        f"  if aria2c -x16 -s16 -k1M --file-allocation=none "
        f"     --auto-file-renaming=false --console-log-level=warn "
        f"     --summary-interval=30 "
        f'     -d "$tmp" -o payload {qu}; then '
        f'    mv -f "$tmp/payload" {qd} && ok=1; '
        f"  fi; "
        f'  rm -rf "$tmp"; '
        f"fi; "
        f'if [ "$ok" != "1" ]; then '
        f"  curl -fL --retry 3 --retry-delay 2 -o {qd} {qu}; "
        f"fi && [ -s {qd} ]"
    )


def _pull_ollama(sess, resolved, hf_token: str | None):
    """Try ollama pull; if no binary, fall back to bartowski HF GGUF mirror."""
    from swm.models import huggingface, resolver
    from swm.models.manifest import ModelEntry, make_key

    if _engine_installed(sess, "ollama"):
        cmd = (
            f"OLLAMA_MODELS=/workspace/ollama/models OLLAMA_HOST=0.0.0.0:11434 "
            f"pgrep -f 'ollama serve' > /dev/null 2>&1 || "
            f"(OLLAMA_MODELS=/workspace/ollama/models OLLAMA_HOST=0.0.0.0:11434 "
            f"nohup /usr/local/bin/ollama serve > /tmp/ollama-pull.log 2>&1 &); "
            f"sleep 2 && "
            f"OLLAMA_MODELS=/workspace/ollama/models OLLAMA_HOST=0.0.0.0:11434 "
            f"/usr/local/bin/ollama pull {shlex.quote(resolved.ref)}"
        )
        exit_code, _, _ = sess.exec(cmd)
        if exit_code != 0:
            raise click.ClickException(f"Failed to pull {resolved.ref}")
        size = _disk_usage(sess, "/workspace/models/ollama")
        return ModelEntry(
            key=make_key("ollama", resolved.ref),
            source="ollama",
            ref=resolved.ref,
            asset_type="ollama",
            path="/workspace/models/ollama",
            size_bytes=size,
            display_name=resolved.display_name,
        )

    console.print(
        "\n[yellow]\u26a0 Ollama binary not found. Looking for a HuggingFace GGUF mirror\u2026[/yellow]"
    )
    mirror = huggingface.find_gguf_mirror(resolved.ref, token=hf_token)
    if not mirror:
        raise click.ClickException(
            f"Cannot pull {resolved.ref}: Ollama isn't installed and no HF GGUF "
            f"mirror was found. Install Ollama first:\n  "
            f"swm setup install ollama <pod>"
        )

    console.print(
        f"[yellow]\u2192 Falling back to HuggingFace mirror [cyan]{mirror}[/cyan][/yellow]"
    )
    info = huggingface.model_info(mirror, token=hf_token)
    fallback = resolver.Resolved(
        source="hf",
        ref=mirror,
        asset_type="llm-gguf",
        display_name=mirror,
        extra={"hf_info": info},
    )
    return _pull_hf(sess, fallback, hf_token, None)[0]


def _pull_civitai(sess, resolved, civitai_token: str | None, filename_override: str | None):
    """Download the primary file from a Civitai version."""
    from swm.models import civitai, resolver as r
    from swm.models.manifest import ModelEntry, make_key

    civitai_info = (resolved.extra or {}).get("civitai_model")
    version_id = (resolved.extra or {}).get("version_id")
    primary, url = civitai.primary_download(civitai_info, version_id, civitai_token)

    bucket = r.target_dir_for(resolved.asset_type)
    bucket_path = f"{_MODELS_ROOT}/{bucket}"
    raw_save_as = filename_override or primary.name
    # Civitai filenames frequently contain spaces, '$', '[]', and parens that
    # break unquoted bash. Sanitize to a shell-safe form when the caller didn't
    # supply an explicit override.
    if filename_override:
        save_as = filename_override
    else:
        save_as = re.sub(r"[^A-Za-z0-9._-]", "_", raw_save_as) or "download.safetensors"
    dest = f"{bucket_path}/{save_as}"

    sess.exec(f"mkdir -p {shlex.quote(bucket_path)}", stream=False)
    cmd = _fast_url_download_cmd(url, bucket_path, dest)
    exit_code, _, _ = sess.exec(cmd)
    if exit_code != 0:
        raise click.ClickException(f"Failed to download {resolved.display_name}")

    size = _disk_usage(sess, dest)
    return ModelEntry(
        key=make_key("civitai", resolved.ref),
        source="civitai",
        ref=resolved.ref,
        asset_type=resolved.asset_type,
        path=dest,
        size_bytes=size,
        display_name=resolved.display_name,
        extra={"filename": save_as},
    )


def _pull_url(sess, resolved, filename_override: str | None):
    """Direct-URL download into the asset-type bucket (defaults to ``files/``)."""
    from swm.models import resolver as r
    from swm.models.manifest import ModelEntry, make_key

    # Respect ``--as`` so e.g. ``--as text-encoder`` lands in
    # /workspace/models/text_encoders/ rather than the generic files/ dir.
    bucket = r.target_dir_for(resolved.asset_type) if resolved.asset_type else "files"
    bucket_path = f"{_MODELS_ROOT}/{bucket}"
    save_as = filename_override or resolved.display_name
    save_as = re.sub(r"[^A-Za-z0-9._-]", "_", save_as)
    if not save_as:
        save_as = "download.bin"
    dest = f"{bucket_path}/{save_as}"

    sess.exec(f"mkdir -p {shlex.quote(bucket_path)}", stream=False)
    cmd = _fast_url_download_cmd(resolved.ref, bucket_path, dest)
    exit_code, _, _ = sess.exec(cmd)
    if exit_code != 0:
        raise click.ClickException(f"Failed to download {resolved.ref}")
    size = _disk_usage(sess, dest)
    return ModelEntry(
        key=make_key("url", resolved.ref),
        source="url",
        ref=resolved.ref,
        asset_type=resolved.asset_type,
        path=dest,
        size_bytes=size,
        display_name=save_as,
        extra={"filename": save_as},
    )


def _disk_usage(sess, path: str) -> int:
    """Return on-disk size of *path* in bytes (0 if missing)."""
    _, out, _ = sess.exec(
        f"du -sb {shlex.quote(path)} 2>/dev/null | cut -f1 || echo 0",
        stream=False,
    )
    try:
        return int(out.strip().split("\n")[-1] or 0)
    except ValueError:
        return 0


def _slim_hf_info(extra: dict | None) -> dict:
    info = (extra or {}).get("hf_info", {})
    return {
        k: info.get(k)
        for k in ("id", "pipeline_tag", "library_name", "tags", "lastModified")
        if info.get(k) is not None
    }


# ── list ─────────────────────────────────────────────────────────────


@models_group.command(name="list")
@click.argument(
    "instance_id",
    required=False,
    shell_complete=complete_pod_id,
    callback=pod_arg_callback,
)
@click.option("--all", "show_all", is_flag=True, help="Also list unmanaged files")
def models_list(instance_id: str, show_all: bool):
    """List models registered on a pod, with their on-disk status."""
    from rich.table import Table
    from swm.models.manifest import RemoteManifest, reconcile_paths
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)

    with console.status("Fetching model inventory\u2026", spinner="dots"):
        with session_from_instance(inst) as sess:
            manifest = RemoteManifest(sess).load()
            path_status = reconcile_paths(manifest, sess)
            orphans: list[tuple[str, int]] = []
            if show_all:
                orphans = _scan_orphans(sess, manifest)

    if not manifest.models and not orphans:
        console.print(
            "\n[yellow]No models registered on this pod.[/yellow]\n"
            f"[dim]Pull one with:  swm models pull {inst.qualified_id} <ref>[/dim]\n"
        )
        return

    if manifest.models:
        table = Table(title="Tracked Models", show_lines=True)
        table.add_column("Name", style="bold cyan", min_width=28, overflow="fold")
        table.add_column("Source", style="dim", no_wrap=True)
        table.add_column("Type", no_wrap=True)
        table.add_column("Size", justify="right", no_wrap=True)
        table.add_column("Status", justify="center", no_wrap=True)
        table.add_column("Path", style="dim", overflow="fold")

        for key, entry in sorted(manifest.models.items()):
            ok = path_status.get(key) == "ok"
            status = "[green]\u25cf ok[/green]" if ok else "[red]\u25cf missing[/red]"
            table.add_row(
                entry.display_name or entry.ref,
                entry.source,
                entry.asset_type,
                _humanize_bytes(entry.size_bytes),
                status,
                entry.path,
            )
        console.print()
        console.print(table)

    if orphans:
        otable = Table(title="Untracked Files (under /workspace/models/)", show_lines=True)
        otable.add_column("Path", style="bold", overflow="fold")
        otable.add_column("Size", justify="right", no_wrap=True)
        for path, size in orphans:
            otable.add_row(path, _humanize_bytes(size))
        console.print()
        console.print(otable)
        console.print(
            "\n[dim]Register an orphan:  "
            f"swm models link {inst.qualified_id} <path> --as <type>[/dim]"
        )

    console.print()


def _scan_orphans(sess, manifest) -> list[tuple[str, int]]:
    """Return ``[(path, size)]`` for files under /workspace/models/ not in the manifest."""
    tracked_paths = {e.path for e in manifest.models.values()}
    _, out, _ = sess.exec(
        f"find {_MODELS_ROOT} -maxdepth 3 -type f "
        f"-not -path '*/.manifest*' "
        f"-not -path '*/hf/hub/*' "
        f"-not -path '*/ollama/blobs/*' "
        f"-not -path '*/ollama/manifests/*' "
        f"-printf '%p\\t%s\\n' 2>/dev/null || true",
        stream=False,
    )
    orphans: list[tuple[str, int]] = []
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 2:
            continue
        path, size = parts
        if path in tracked_paths:
            continue
        try:
            orphans.append((path, int(size)))
        except ValueError:
            continue
    return orphans


# ── link (register / re-categorize) ─────────────────────────────────


@models_group.command(name="link")
@click.argument(
    "instance_id",
    required=False,
    shell_complete=complete_pod_id,
)
@click.argument("source_path", required=False)
@click.option(
    "--as", "asset_type",
    type=_ASSET_CHOICES,
    required=True,
    help="Asset type the file should be filed under",
)
@click.option(
    "--name",
    default=None,
    help="Display name to record in the manifest (defaults to filename)",
)
def models_link(
    instance_id: str,
    source_path: str,
    asset_type: str,
    name: str | None,
):
    """Register an on-pod file under the unified model store.

    Moves the file into ``/workspace/models/<bucket>/`` so the appropriate
    framework (e.g. ComfyUI) discovers it via the bucket-style dir symlinks,
    and adds an entry to the manifest.

    \b
    Examples:
      swm models link pod:abc /workspace/sdxl.safetensors --as checkpoint
      swm models link pod:abc /workspace/models/files/lora.safetensors --as lora
    """
    from swm.models import resolver
    from swm.models.manifest import ModelEntry, RemoteManifest, make_key
    from swm.remote.ssh import session_from_instance

    instance_id, (source_path,) = absorb_pod_positional(
        instance_id, (source_path,), ("SOURCE_PATH",),
    )
    inst = _instance_for(instance_id)
    bucket = resolver.target_dir_for(asset_type)
    bucket_path = f"{_MODELS_ROOT}/{bucket}"

    filename = source_path.rsplit("/", 1)[-1]
    if not filename:
        raise click.UsageError("source_path must point at a file")
    dest = f"{bucket_path}/{filename}"
    display = name or filename

    with session_from_instance(inst) as sess:
        sess.exec(f"mkdir -p {bucket_path}", stream=False)
        exit_code, out, _ = sess.exec(
            f"if [ ! -e {shlex.quote(source_path)} ]; then echo MISSING; exit 1; "
            f"elif [ {shlex.quote(source_path)} -ef {shlex.quote(dest)} ]; then echo SAME; "
            f"else mv {shlex.quote(source_path)} {shlex.quote(dest)} && echo MOVED; fi",
            stream=False,
        )
        if exit_code != 0:
            raise click.ClickException(f"Source path not found: {source_path}")
        size = _disk_usage(sess, dest)
        entry = ModelEntry(
            key=make_key("link", dest),
            source="link",
            ref=dest,
            asset_type=asset_type,
            path=dest,
            size_bytes=size,
            display_name=display,
        )
        RemoteManifest(sess).upsert(entry)

    action = "registered" if "SAME" in out else "moved + registered"
    console.print(
        f"\n[green]\u2713[/green] {action}: [bold]{display}[/bold] "
        f"-> [cyan]{dest}[/cyan]\n"
    )


# ── remove ───────────────────────────────────────────────────────────


@models_group.command(name="remove")
@click.argument(
    "instance_id",
    required=False,
    shell_complete=complete_pod_id,
)
@click.argument("ref", required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def models_remove(instance_id: str, ref: str, yes: bool):
    """Remove a model from the pod's unified store.

    \b
    Removes the file(s) and clears the manifest entry. Other tracked models
    are left untouched.

    \b
    Examples:
      swm models remove pod:abc Qwen/Qwen3-8B
      swm models remove pod:abc civitai:101055:128713 -y
    """
    from swm.models.manifest import RemoteManifest
    from swm.remote.ssh import session_from_instance

    instance_id, (ref,) = absorb_pod_positional(
        instance_id, (ref,), ("REF",),
    )
    inst = _instance_for(instance_id)

    with session_from_instance(inst) as sess:
        rm = RemoteManifest(sess)
        manifest = rm.load()
        matches = manifest.find_by_display(ref)
        match = manifest.find_by_ref(ref)
        if match and match not in matches:
            matches.append(match)
        if not matches:
            raise click.UsageError(
                f"No tracked model matches {ref!r}. "
                f"Run `swm models list {inst.qualified_id}` to see what's available."
            )
        if len(matches) > 1:
            keys = ", ".join(e.key for e in matches)
            raise click.UsageError(
                f"{ref!r} is ambiguous; matched: {keys}. "
                "Pass the exact key or display name."
            )
        target = matches[0]
        if not yes:
            click.confirm(
                f"Remove {target.display_name} ({_humanize_bytes(target.size_bytes)}) "
                f"from {inst.qualified_id}?",
                abort=True,
            )

        if target.source == "ollama":
            # Ollama entries record path=/workspace/models/ollama — the whole
            # shared, content-addressed store. The generic rm -rf branch would
            # therefore delete EVERY ollama model, not the one confirmed, so
            # ollama removals must go through the binary. It routinely
            # vanishes on pod restart (/usr/local/bin isn't persisted) while
            # the store survives in /workspace.
            if not _engine_installed(sess, "ollama"):
                raise click.ClickException(
                    "Ollama isn't installed on this pod, and ollama models "
                    "share one content-addressed store — removing files "
                    "directly would delete other models too. Reinstall the "
                    f"engine first (swm setup install ollama "
                    f"{inst.qualified_id}), then re-run this remove."
                )
            sess.exec(
                f"OLLAMA_MODELS=/workspace/models/ollama "
                f"/usr/local/bin/ollama rm {shlex.quote(target.ref)} || true",
                stream=False,
            )
        else:
            sess.exec(
                f"rm -rf {shlex.quote(target.path)}",
                stream=False,
            )
        rm.remove(target.key)

    console.print(f"\n[green]\u2713[/green] Removed [bold]{target.display_name}[/bold]\n")
