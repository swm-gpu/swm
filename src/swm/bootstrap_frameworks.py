"""Framework lifecycle management for remote GPU instances."""

from __future__ import annotations

import time

from rich.console import Console

from swm.bootstrap import _step
from swm.remote.ssh import RemoteSession

console = Console()


def install_framework(
    session: RemoteSession,
    name: str,
    console: Console | None = None,
) -> None:
    """Install a framework by name using its declarative step list."""
    from swm.frameworks import Framework, get_framework

    _con = console or globals()["console"]

    fw: Framework = get_framework(name)
    _con.print(f"\n[bold]Installing {fw.label}[/bold]")
    env_prefix = f"{fw.env_setup} && " if fw.env_setup else ""

    total = len(fw.steps) + len(fw.post_install)
    for idx, step in enumerate(fw.steps, 1):
        workdir = step.workdir or fw.install_dir
        if step.check:
            cmd = f"{step.check} && echo '{step.label}: already done' || ({env_prefix}cd {workdir} && {step.command})"
        else:
            cmd = f"{env_prefix}cd {workdir} && {step.command}"
        _step(session, f"[{idx}/{total}] {step.label}", cmd)

    for idx, step in enumerate(fw.post_install, len(fw.steps) + 1):
        workdir = step.workdir or fw.install_dir
        if step.check:
            cmd = f"{step.check} && echo '{step.label}: already done' || ({env_prefix}cd {workdir} && {step.command})"
        else:
            cmd = f"{env_prefix}cd {workdir} && {step.command}"
        _step(session, f"[{idx}/{total}] {step.label}", cmd)


def start_framework(
    session: RemoteSession,
    name: str,
    port: int | None = None,
    console: Console | None = None,
    qualified_id: str | None = None,
) -> str | None:
    """Launch a framework in the background. Returns proxy URL if applicable."""
    from swm.frameworks import get_framework

    _con = console or globals()["console"]
    fw = get_framework(name)

    if fw.process_pattern:
        with _con.status(f"Checking if {fw.label} is running…", spinner="dots"):
            _, out, _ = session.exec(
                f"pgrep -fa '{fw.process_pattern}' | grep -v grep || true",
                stream=False,
            )
        if out.strip():
            pid = out.strip().split("\n")[0].split()[0]
            _con.print(
                f"  [yellow]{fw.label} is already running (PID {pid})[/yellow]"
            )
            return None

    env_prefix = f"{fw.env_setup} && " if fw.env_setup else ""
    if fw.pre_start:
        _con.print(f"\n[bold cyan]▸ Preparing {fw.label}[/bold cyan]")
        for step in fw.pre_start:
            workdir = step.workdir or fw.install_dir
            if step.check:
                cmd = (
                    f"{step.check} && echo '{step.label}: already done' "
                    f"|| ({env_prefix}cd {workdir} && {step.command})"
                )
            else:
                cmd = f"{env_prefix}cd {workdir} && {step.command}"
            _step(session, step.label, cmd)

    _con.print(f"\n[bold cyan]▸ Starting {fw.label}[/bold cyan]")
    launch = fw.launch_cmd
    if port and fw.ports:
        default_port = str(next(iter(fw.ports)))
        launch = launch.replace(default_port, str(port))

    logfile = f"/tmp/{fw.name}.log"

    with _con.status(f"Launching {fw.label}…", spinner="dots"):
        session.exec_background(
            launch,
            logfile=logfile,
            workdir=fw.launch_workdir,
            env_setup=fw.env_setup,
        )

    max_checks = 5
    alive = False
    for i in range(max_checks):
        time.sleep(3 if i == 0 else 2)
        if not fw.process_pattern:
            alive = True
            break
        _, out, _ = session.exec(
            f"pgrep -fa '{fw.process_pattern}' | grep -v grep || true",
            stream=False,
        )
        if out.strip():
            alive = True
            break

    if alive:
        _con.print(f"  [green]✓ {fw.label} started[/green]")
        _pod_ref = qualified_id or "<pod>"
        _con.print(f"  Logs: swm run {_pod_ref} 'tail -f {logfile}'")
    else:
        _con.print(f"  [red]✗ {fw.label} failed to start[/red]")
        _con.print(f"  Last lines from {logfile}:")
        _, tail, _ = session.exec(f"tail -15 {logfile} 2>/dev/null", stream=False)
        if tail.strip():
            for line in tail.strip().splitlines():
                _con.print(f"    [dim]{line}[/dim]")
        raise RuntimeError(f"{fw.label} exited immediately — check logs above")

    return None


def stop_framework(
    session: RemoteSession,
    name: str,
    console: Console | None = None,
) -> None:
    """Stop a running framework."""
    from swm.frameworks import get_framework

    _con = console or globals()["console"]
    fw = get_framework(name)
    if not fw.stop_cmd:
        _con.print(f"  [yellow]{fw.label} has no stop command defined[/yellow]")
        return

    _con.print(f"\n[bold cyan]▸ Stopping {fw.label}[/bold cyan]")
    with _con.status(f"Stopping {fw.label}…", spinner="dots"):
        session.exec(fw.stop_cmd, stream=False)
    _con.print(f"  [green]✓ {fw.label} stopped[/green]")


def install_comfyui(session: RemoteSession) -> None:
    """Backward-compatible wrapper."""
    install_framework(session, "comfyui")


def install_swarmui(session: RemoteSession) -> None:
    """Backward-compatible wrapper."""
    install_framework(session, "swarmui")


# ── symlinks ────────────────────────────────────────────────────────


def link_models_to_comfyui(session: RemoteSession) -> None:
    """Symlink /workspace/models into ComfyUI's model directory."""
    dirs = ["checkpoints", "loras", "vae", "controlnet", "clip", "upscale_models", "unet"]
    cmds = ["mkdir -p /workspace/models/{" + ",".join(dirs) + "}"]
    for d in dirs:
        cmds.append(
            f"[ -L /workspace/ComfyUI/models/{d} ] || "
            f"(rm -rf /workspace/ComfyUI/models/{d} "
            f"&& ln -s /workspace/models/{d} /workspace/ComfyUI/models/{d})"
        )
    _step(session, "Symlinking models → ComfyUI", " && ".join(cmds))
