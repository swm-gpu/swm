"""Shared helpers used across CLI command modules."""
from __future__ import annotations

import os

import click
from rich.console import Console
from rich.table import Table

from swm import config as cfg
from swm.providers import resolve_instance

console = Console(log_path=False)


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
    """Build the public URL for a framework running on *inst*."""
    provider = (inst.provider or "").lower()
    if provider == "runpod":
        return f"https://{inst.id}-{port}.proxy.runpod.net"
    if provider == "vastai":
        if inst.ip_address:
            mapped = (inst.ports or {}).get(port)
            if mapped:
                return f"http://{inst.ip_address}:{mapped}"
    if inst.ip_address:
        return f"http://{inst.ip_address}:{port}"
    return None


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
