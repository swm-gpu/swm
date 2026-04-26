"""Shared helpers used across CLI command modules."""
from __future__ import annotations

import os

import click
from rich.console import Console
from rich.table import Table

from swm import config as cfg
from swm.providers import resolve_instance

console = Console(log_path=False)


_ACTIVE_POD_KEY = "active_pod"
_ACTIVE_POD_ENV = "SWM_POD"


def get_active_pod() -> str | None:
    """Return the active pod id from ``$SWM_POD`` or the config file."""
    env = os.environ.get(_ACTIVE_POD_ENV)
    if env:
        return env.strip()
    val = cfg.get(_ACTIVE_POD_KEY)
    return str(val).strip() if val else None


def set_active_pod(instance_id: str) -> None:
    """Persist *instance_id* as the active pod in the config file."""
    cfg.set_value(_ACTIVE_POD_KEY, instance_id)


def clear_active_pod(if_matches: str | None = None) -> None:
    """Remove the active pod from config, optionally only if it matches."""
    current = cfg.get(_ACTIVE_POD_KEY)
    if current is None:
        return
    if if_matches is not None and current != if_matches:
        return
    cfg.delete(_ACTIVE_POD_KEY)


def resolve_active_pod(instance_id: str | None) -> str:
    """Resolve an instance id, falling back to env/config defaults.

    Resolution order:
      1. Explicit CLI argument
      2. ``$SWM_POD`` environment variable
      3. ``active_pod`` key in the config file

    Raises ``click.UsageError`` with a helpful message if none are set.
    """
    if instance_id:
        return instance_id
    active = get_active_pod()
    if active:
        return active
    raise click.UsageError(
        "No pod specified and no active pod is set. "
        "Pass the pod id explicitly, set $SWM_POD, or run `swm use <pod_id>`."
    )


def _iter_configured_pod_ids() -> list[str]:
    """Return pod ids known to swm's config (`pods.<provider:id>`)."""
    pods = cfg.get("pods") or {}
    if not isinstance(pods, dict):
        return []
    return sorted(pods.keys())


def complete_pod_id(ctx, param, incomplete: str):
    """Click shell completion callback for pod id arguments."""
    return [pid for pid in _iter_configured_pod_ids() if pid.startswith(incomplete)]


def pod_arg_callback(ctx, param, value):
    """Click callback that fills in the active pod when *value* is missing."""
    if ctx.resilient_parsing:
        return value
    return resolve_active_pod(value)


_PROVIDER_PREFIXES = (
    "runpod:", "vastai:", "lambda:", "aws:", "gcp:", "coreweave:", "vultr:",
)


def looks_like_pod_id(value: str) -> bool:
    """Heuristic: does *value* look like a pod id (vs. a command token)?"""
    if not value:
        return False
    if any(value.startswith(p) for p in _PROVIDER_PREFIXES):
        return True
    return value in _iter_configured_pod_ids()


def split_pod_and_command(
    instance_id: str | None,
    command: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    """Disambiguate a positional arg that may be a pod id or the first command word.

    Used by commands where ``instance_id`` and a trailing ``COMMAND...`` share
    the positional space. If *instance_id* doesn't look like a pod id and an
    active pod is configured, treat it as the start of the command and fall
    back to the active pod.
    """
    if instance_id and not looks_like_pod_id(instance_id) and get_active_pod():
        command = (instance_id, *command)
        instance_id = None
    return resolve_active_pod(instance_id), command


def _instance_for(instance_id: str):
    """Resolve an ID and fetch the full Instance object."""
    with console.status("Resolving instance…", spinner="dots"):
        provider, raw_id = resolve_instance(instance_id)
        instances = provider.list_instances()
    inst = next((i for i in instances if i.id == raw_id), None)
    if inst is None:
        raise click.ClickException(f"Instance {raw_id} not found on {provider.name}")
    return inst


def _framework_url(inst, port: int) -> str | None:
    """Build the public URL for a framework running on *inst*.

    Returns None when the port is not externally reachable (e.g. not
    mapped on Vast.ai).
    """
    provider = (inst.provider or "").lower()
    if provider == "runpod":
        return f"https://{inst.id}-{port}.proxy.runpod.net"
    mapped = (inst.ports or {}).get(port)
    if mapped and inst.ip_address:
        return f"http://{inst.ip_address}:{mapped}"
    return None


def _open_tunnel(inst, ports: dict[int, str]) -> list[int] | None:
    """Open a background SSH tunnel for ports not externally mapped.

    Returns the list of tunnelled ports, or None if no tunnel was needed.
    """
    import subprocess

    if not inst.ssh_host:
        return None
    unmapped = [p for p in ports if p not in (inst.ports or {})]
    if not unmapped:
        return None

    ssh_port = inst.ssh_port or 22
    user = inst.ssh_user or "root"

    cmd = [
        "ssh", "-N",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
    ]
    for p in unmapped:
        cmd.extend(["-L", f"{p}:localhost:{p}"])
    if ssh_port != 22:
        cmd.extend(["-p", str(ssh_port)])
    cmd.append(f"{user}@{inst.ssh_host}")

    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return unmapped


def _probe_url(url: str, timeout: int = 60) -> bool:
    """Try reaching *url* with retries over *timeout* seconds. Returns True if reachable."""
    import httpx
    import time

    deadline = time.monotonic() + timeout
    interval = 3
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=5, follow_redirects=True)
            if r.status_code < 500:
                return True
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError):
            pass
        remaining = deadline - time.monotonic()
        time.sleep(min(interval, max(remaining, 0)))
    return False


def _preflight_pull(
    storage_slug: str,
    bucket: str,
    workspace: str,
    volume_gb: int,
) -> tuple[list[str], int, int]:
    """Run pre-flight size check and return (extra_excludes, total_bytes, total_files).

    Runs locally — no SSH session needed.
    Returns an empty exclude list when the workspace fits.
    Raises ``SystemExit`` if the user aborts.
    """
    from swm.bootstrap import preflight_check, _humanize

    check = preflight_check(storage_slug, bucket, workspace, volume_gb)

    if check.fits:
        return [], check.workspace_bytes, 0

    if check.dir_sizes:
        table = Table(title="Directory Breakdown", show_lines=True)
        table.add_column("Directory", style="bold")
        table.add_column("Size", justify="right", style="cyan")

        for d, size in sorted(check.dir_sizes.items(), key=lambda x: -x[1]):
            table.add_row(f"{d}/", _humanize(size))

        console.print()
        console.print(table)

    console.print(
        f"\n  Workspace: [bold]{_humanize(check.workspace_bytes)}[/bold]"
        f"  Disk: [bold]{_humanize(check.available_bytes)}[/bold]"
        f"  Over by: [bold red]{_humanize(check.overshoot)}[/bold red]"
    )
    console.print()
    console.print("[bold]Options:[/bold]")
    console.print("  1. Exclude directories (comma-separated names from the table)")
    console.print("  2. Continue anyway (risk running out of disk)")
    console.print("  3. Abort")

    choice = click.prompt(
        "\nExclude dirs, 'continue', or 'abort'",
        default="abort",
    ).strip()

    if choice.lower() == "abort":
        raise SystemExit("Aborted — workspace too large for disk.")

    if choice.lower() == "continue":
        console.print("[yellow]⚠ Proceeding — disk may fill up[/yellow]")
        return [], check.workspace_bytes, 0

    extra = [f"{d.strip()}/**" for d in choice.split(",") if d.strip()]
    if extra:
        remaining = check.workspace_bytes - sum(
            check.dir_sizes.get(d.strip().rstrip("/"), 0)
            for d in choice.split(",")
        )
        console.print(
            f"  Adjusted size: [bold]{_humanize(remaining)}[/bold] "
            f"(excluding {', '.join(d.strip() for d in choice.split(','))})"
        )
    return extra, check.workspace_bytes, 0
