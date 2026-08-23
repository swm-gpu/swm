"""Shared helpers used across CLI command modules."""
from __future__ import annotations

import os

import click
from rich.table import Table

from swm import config as cfg
from swm.providers import resolve_instance
from swm.redact import SafeConsole

console = SafeConsole(log_path=False)


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
    if if_matches is not None and str(current) != str(if_matches):
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
    """Return qualified pod ids known to swm's config (`provider:id`)."""
    pods = cfg.get("pods") or {}
    if not isinstance(pods, dict):
        return []
    out: list[str] = []
    for pid, meta in pods.items():
        prov = (meta or {}).get("provider", "")
        out.append(f"{prov}:{pid}" if prov else pid)
    return sorted(out)


def complete_pod_id(ctx, param, incomplete: str):
    """Click shell completion callback for pod id arguments."""
    pods = cfg.get("pods") or {}
    if not isinstance(pods, dict):
        return []
    candidates: set[str] = set()
    for pid, meta in pods.items():
        prov = (meta or {}).get("provider", "")
        qid = f"{prov}:{pid}" if prov else pid
        candidates.add(qid)
        candidates.add(pid)
    return sorted(c for c in candidates if c.startswith(incomplete))


def pod_arg_callback(ctx, param, value):
    """Click callback that fills in the active pod when *value* is missing."""
    if ctx.resilient_parsing:
        return value
    return resolve_active_pod(value)


_PROVIDER_PREFIXES = (
    "runpod:", "vastai:", "lambda:", "aws:", "gcp:", "coreweave:", "vultr:",
    "tensordock:", "fluidstack:", "azure:",
)


def looks_like_pod_id(value: str) -> bool:
    """Heuristic: does *value* look like a pod id (vs. a command token)?"""
    if not value:
        return False
    if any(value.startswith(p) for p in _PROVIDER_PREFIXES):
        return True
    pods = cfg.get("pods") or {}
    return isinstance(pods, dict) and value in pods


def absorb_pod_positional(
    instance_id: str | None,
    values: tuple,
    names: tuple[str, ...],
    required: int | None = None,
) -> tuple[str, list]:
    """Disambiguate ``[INSTANCE_ID] ARG...`` positionals.

    Click fills positionals greedily left-to-right, so with an active pod
    configured, ``swm models pull <ref>`` lands the ref in INSTANCE_ID and
    errors "Missing argument 'REF'". When the trailing value is missing
    and the leading value doesn't look like a pod id, shift everything
    one slot right and resolve the active pod.

    *required* is how many leading *values* must be present after the
    shift (default: all). Returns ``(resolved_instance_id, values)``.
    """
    vals = list(values)
    if vals and vals[-1] is None and instance_id and not looks_like_pod_id(instance_id):
        vals = [instance_id, *vals[:-1]]
        instance_id = None
    need = len(names) if required is None else required
    for value, name in list(zip(vals, names))[:need]:
        if value is None:
            raise click.UsageError(f"Missing argument '{name}'.")
    return resolve_active_pod(instance_id), vals


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


def safe_resolve_instance(instance_id: str):
    """Resolve an instance id, converting ValueError into click.UsageError.

    Auto-clears the active pod if it was the stale value that failed to
    resolve, so subsequent commands don't hit the same wall.
    """
    try:
        return resolve_instance(instance_id)
    except ValueError as e:
        cleared = ""
        if get_active_pod() == instance_id:
            clear_active_pod(if_matches=instance_id)
            cleared = " Cleared stale active pod."
        raise click.UsageError(
            f"{e}{cleared} "
            "Set a new active pod with `swm use <provider:id>`."
        )


def _instance_for(instance_id: str):
    """Resolve an ID and fetch the full Instance object."""
    with console.status("Resolving instance…", spinner="dots"):
        provider, raw_id = safe_resolve_instance(instance_id)
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

    from swm.remote.ssh import _ssh_config_for

    if not inst.ssh_host:
        return None
    unmapped = [p for p in ports if p not in (inst.ports or {})]
    if not unmapped:
        return None

    # Resolve host/port/user/key the same way `swm run`/`swm ssh` do, so the
    # tunnel uses the configured identity (e.g. ssh.key_path) and the direct
    # IP + mapped port-22 when available. Building a bare `ssh` command here
    # silently fell back to default keys and could miss a custom key.
    c = _ssh_config_for(inst)

    cmd = ["ssh", "-N"]
    if c["key_path"]:
        cmd.extend(["-i", str(c["key_path"])])
    cmd.extend([
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
    ])
    for p in unmapped:
        cmd.extend(["-L", f"{p}:localhost:{p}"])
    if c["port"] != 22:
        cmd.extend(["-p", str(c["port"])])
    cmd.append(f"{c['user']}@{c['host']}")

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
        except (httpx.HTTPError, OSError):
            # httpx.HTTPError covers all transport-level failures —
            # ConnectError, ReadError ("connection reset by peer" while a
            # freshly-opened SSH tunnel settles), WriteError, TimeoutException,
            # RemoteProtocolError, etc. A probe must never crash the command;
            # an unreachable URL just means "not ready yet, retry".
            pass
        remaining = deadline - time.monotonic()
        time.sleep(min(interval, max(remaining, 0)))
    return False


def _preflight_pull(
    storage_slug: str,
    bucket: str,
    workspace: str,
    volume_gb: int,
) -> list[str]:
    """Run pre-flight size check and return any directory excludes.

    Runs locally — no SSH session needed.
    Returns an empty list when the workspace fits.
    Raises ``SystemExit`` if the user aborts.
    """
    from swm.bootstrap import preflight_check, _humanize

    check = preflight_check(storage_slug, bucket, workspace, volume_gb)

    if check.fits:
        return []

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
        return []

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
    return extra
