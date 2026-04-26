"""swm guard — lifecycle automation and idle pod protection."""

from __future__ import annotations

import time

import click
from rich.table import Table

from swm.commands._helpers import console, complete_pod_id, pod_arg_callback
from swm.guard import (
    _format_idle,
    _local_daemon_alive,
    _LOCAL_PID_FILE,
    disable_policy,
    ensure_remote_guard,
    evaluate_instance,
    get_defaults,
    get_policy,
    run_guard_cycle,
    set_defaults,
    set_policy,
    stop_local_daemon,
    stop_remote_guard,
)


@click.group()
def guard():
    """Configure and run idle pod lifecycle automation."""


@guard.command(name="defaults")
@click.option(
    "--mode",
    default=None,
    type=click.Choice(["manual", "remind", "auto-stop", "auto-down"], case_sensitive=False),
    help="Default lifecycle policy for new pods.",
)
@click.option("--idle-timeout", default=None, type=int, help="Default idle timeout in minutes.")
@click.option("--poll-interval", default=None, type=int, help="Default on-pod watcher poll interval in seconds.")
def guard_defaults(mode: str | None, idle_timeout: int | None, poll_interval: int | None):
    """View or update the default guard policy for new pods.

    Run without options to see current defaults.
    """
    if mode or idle_timeout or poll_interval:
        policy = set_defaults(
            mode=mode,
            idle_timeout_minutes=idle_timeout,
            poll_interval_seconds=poll_interval,
        )
        console.print(f"[green]✓[/green] Guard defaults updated:")
    else:
        policy = get_defaults()
        console.print("[bold]Guard defaults[/bold] (applied to new pods):")

    console.print(f"  Mode:          {policy.mode}")
    console.print(f"  Idle timeout:  {policy.idle_timeout_minutes}m")
    console.print(f"  Poll interval: {policy.poll_interval_seconds}s")


@guard.command(name="set")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
@click.option(
    "--mode",
    required=True,
    type=click.Choice(["manual", "remind", "auto-stop", "auto-down"], case_sensitive=False),
    help="Lifecycle policy for this pod.",
)
@click.option("--idle-timeout", default=None, type=int, help="Idle timeout in minutes before the policy triggers.")
@click.option("--poll-interval", default=None, type=int, help="On-pod watcher poll interval in seconds.")
def guard_set(instance_id: str, mode: str, idle_timeout: int | None, poll_interval: int | None):
    """Set the lifecycle guard policy for a pod."""
    from swm.commands._helpers import _instance_for

    inst = _instance_for(instance_id)
    policy = set_policy(
        inst.id,
        mode=mode,
        idle_timeout_minutes=idle_timeout,
        poll_interval_seconds=poll_interval,
    )
    if policy.enabled and inst.status.value == "running":
        ensure_remote_guard(inst, policy)
    console.print(
        f"[green]✓[/green] Guard for {inst.qualified_id}: {policy.mode} "
        f"after {policy.idle_timeout_minutes}m idle"
    )


@guard.command(name="disable")
@click.argument("instance_id", required=False, shell_complete=complete_pod_id, callback=pod_arg_callback)
def guard_disable(instance_id: str):
    """Disable lifecycle automation for a pod."""
    from swm.commands._helpers import _instance_for
    from swm.remote.ssh import session_from_instance

    inst = _instance_for(instance_id)
    if inst.status.value == "running":
        with session_from_instance(inst) as sess:
            stop_remote_guard(sess)
    disable_policy(inst.id)
    console.print(f"[green]✓[/green] Guard disabled for {inst.qualified_id}")


@guard.command(name="list")
def guard_list():
    """List all pods with guard policies and their live status."""
    from swm import config as cfg

    pods = cfg.get("pods", {}) or {}
    guarded = [(pid, get_policy(pid)) for pid in sorted(pods) if get_policy(pid).enabled]

    if not guarded:
        console.print("[dim]No guard policies configured.[/dim]")
        console.print("[dim]Set defaults:  swm guard defaults --mode auto-stop --idle-timeout 30[/dim]")
        return

    results: list[tuple[str, object, dict | None]] = []
    for instance_id, policy in guarded:
        try:
            results.append((instance_id, policy, evaluate_instance(instance_id)))
        except Exception as exc:
            results.append((instance_id, policy, exc))

    rows = 0
    for instance_id, policy, result in results:
        rows += 1
        if isinstance(result, Exception):
            exc = result
            console.print(f"\n[bold]{instance_id}[/bold]")
            console.print(f"  Policy:    {policy.mode} after {policy.idle_timeout_minutes}m idle")
            console.print(f"  Status:    [red]error: {exc}[/red]")
            continue

        inst = result["instance"]
        state = result["state"] or {}

        console.print(f"\n[bold]{inst.qualified_id}[/bold]")
        console.print(f"  Policy:    {policy.mode} after {policy.idle_timeout_minutes}m idle (poll {policy.poll_interval_seconds}s)")
        console.print(f"  Status:    {inst.status_rich}")
        if inst.cost_per_hr:
            console.print(f"  Cost:      ${inst.cost_per_hr:.2f}/hr")
        console.print(f"  Watcher:   {'running' if result['remote_guard_running'] else 'stopped'}")
        if state:
            idle_str = _format_idle(float(state.get("idle_seconds", 0)))
            active = state.get("active", False)
            console.print(f"  Active:    {active}")
            console.print(f"  Idle:      {idle_str}")
            console.print(f"  SSH:       {state.get('ssh_connections', 0)} connection(s)")
            console.print(f"  GPU util:  {state.get('avg_gpu_util', 0)}%")
            console.print(f"  FS writes: {state.get('recent_fs_write', False)}")
            console.print(f"  Locked:    {state.get('transfer_locked', False)}")
            busy = state.get("busy_processes") or []
            if busy:
                console.print("  Busy:      " + "; ".join(str(x) for x in busy))

    console.print()
    if _local_daemon_alive():
        pid = _LOCAL_PID_FILE.read_text().strip()
        console.print(f"  [dim]Local guard daemon: running (pid {pid})[/dim]")
    else:
        console.print("  [dim]Local guard daemon: not running[/dim]")


@guard.command(name="stop-daemon")
def guard_stop_daemon():
    """Stop the background guard daemon."""
    if stop_local_daemon():
        console.print("[green]✓[/green] Guard daemon stopped.")
    else:
        console.print("[dim]No guard daemon running.[/dim]")


@guard.command(name="run")
@click.argument("instance_ids", nargs=-1)
@click.option("--once", is_flag=True, help="Run a single guard evaluation cycle and exit.")
@click.option("--interval", default=60, type=int, help="Polling interval in seconds for continuous mode.")
def guard_run(instance_ids: tuple[str, ...], once: bool, interval: int):
    """Run the local lifecycle guard loop using your local provider credentials."""
    if once:
        for line in run_guard_cycle(instance_ids):
            console.print(line)
        return

    console.print("[bold]Lifecycle guard running[/bold]  Press Ctrl-C to stop.")
    while True:
        for line in run_guard_cycle(instance_ids):
            console.print(line)
        time.sleep(interval)
