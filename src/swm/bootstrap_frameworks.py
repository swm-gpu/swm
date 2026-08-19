"""Framework lifecycle management for remote GPU instances."""

from __future__ import annotations

import shlex
import time


from rich.console import Console

from swm.bootstrap import _step
from swm.redact import SafeConsole
from swm.remote.ssh import RemoteSession

console = SafeConsole()


def install_framework(
    session: RemoteSession,
    name: str,
    console: Console | None = None,
) -> None:
    """Install a framework by name using its declarative step list."""
    from swm.bootstrap import ensure_workspace_python, repair_venv
    from swm.frameworks import Framework, get_framework

    _con = console or globals()["console"]

    fw: Framework = get_framework(name)
    _con.print(f"\n[bold]Installing {fw.label}[/bold]")

    if fw.venv:
        # Make sure /workspace/.uv/ and /workspace/.python/ are populated so
        # the framework's venv-creation step can use ``uv venv --python``
        # against a hermetic, workspace-owned interpreter.
        ensure_workspace_python(session)
        # Backward-compat: if a venv already exists at fw.venv (e.g. from
        # a pulled workspace built on a different host), rebind it to the
        # workspace-owned Python before any step touches the venv.  This
        # turns a stale venv into a runnable one without losing any
        # installed packages.
        repair_venv(session, fw.venv)

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


def _print_usage(
    con: Console, fw, host: str, port: int | None,
) -> None:
    """Show how to talk to the framework that just started.

    Printing only a URL was a dead end for API frameworks: an Ollama root
    answers with a banner and nothing else, so the user was left at a page
    that says nothing about how to actually use it.
    """
    from swm.frameworks import render_usage

    fw_port = port or (next(iter(fw.ports)) if fw.ports else None)
    if fw.access == "ui" and fw_port:
        con.print(f"  Open: http://{host}:{fw_port}")
        return
    if not fw.usage:
        return
    base = f"http://{host}:{fw_port}" if fw_port else f"http://{host}"
    for u in render_usage(fw, base):
        con.print(f"  [bold]{u.label}[/bold]")
        if u.description:
            con.print(f"    [dim]{u.description}[/dim]")
        if u.command:
            con.print(f"    {u.command}")


def start_framework(
    session: RemoteSession,
    name: str,
    port: int | None = None,
    extra_args: str | None = None,
    console: Console | None = None,
    qualified_id: str | None = None,
) -> str | None:
    """Launch a framework in the background. Returns proxy URL if applicable."""
    from swm.bootstrap import ensure_workspace_python, repair_venv
    from swm.frameworks import get_framework

    _con = console or globals()["console"]
    fw = get_framework(name)

    if fw.venv:
        # Pulled-workspace path: a venv built on a previous pod may be
        # unrunnable on this host until rebind.  Make sure uv + managed
        # Python are present, then repair the venv if needed.  Both
        # calls are idempotent and cheap on healthy pods.
        ensure_workspace_python(session)
        repair_venv(session, fw.venv)

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
    if extra_args:
        launch = f"{launch} {' '.join(shlex.quote(arg) for arg in shlex.split(extra_args))}"

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
        _print_usage(_con, fw, session.host, port)
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


# ── symlinks ────────────────────────────────────────────────────────


COMFYUI_MODEL_DIRS = [
    "checkpoints", "loras", "vae", "controlnet", "embeddings",
    "clip", "clip_vision", "upscale_models", "unet",
    "diffusion_models", "text_encoders",
]


def _comfyui_link_script() -> str:
    """Bash that symlinks ComfyUI's per-type model dirs to /workspace/models/.

    Preserves any existing content under ``/workspace/ComfyUI/models/<type>`` by
    moving it into ``/workspace/models/<type>`` before replacing with a symlink.
    Idempotent — re-running is a no-op once the symlinks exist.
    """
    parts = [
        "mkdir -p /workspace/models/{" + ",".join(COMFYUI_MODEL_DIRS) + "}",
        "mkdir -p /workspace/ComfyUI/models",
    ]
    for d in COMFYUI_MODEL_DIRS:
        target = f"/workspace/ComfyUI/models/{d}"
        store = f"/workspace/models/{d}"
        parts.append(
            f"if [ -L {target} ]; then :; "
            f"elif [ -d {target} ]; then "
            f"  ( shopt -s dotglob nullglob; mv {target}/* {store}/ 2>/dev/null || true ); "
            f"  rmdir {target} 2>/dev/null || rm -rf {target}; "
            f"  ln -s {store} {target}; "
            f"else "
            f"  ln -s {store} {target}; "
            f"fi"
        )
    return " && ".join(parts)


def link_models_to_comfyui(session: RemoteSession) -> None:
    """Symlink /workspace/models/<type> into ComfyUI's model directory.

    Handles every per-type bucket ComfyUI knows about, and safely migrates any
    files already sitting in ``/workspace/ComfyUI/models/<type>`` so existing
    pods aren't disturbed.
    """
    _step(session, "Symlinking models → ComfyUI", _comfyui_link_script())
