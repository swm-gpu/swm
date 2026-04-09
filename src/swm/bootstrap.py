"""Bootstrap scripts for setting up remote GPU instances with storage and tools."""

from __future__ import annotations

import subprocess

from rich.console import Console

from swm.remote.ssh import RemoteSession

console = Console()

S5CMD_VERSION = "2.3.0"
S5CMD_URL = (
    f"https://github.com/peak/s5cmd/releases/download/v{S5CMD_VERSION}/"
    f"s5cmd_{S5CMD_VERSION}_Linux-64bit.tar.gz"
)

SAFETY_MARGIN = 0.90


def _humanize(n: int | float) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024:
            return f"{v:.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v:.1f} PB"


def _step(session: RemoteSession, label: str, command: str) -> tuple[int, str, str]:
    """Run a labelled step on the remote, streaming output to the terminal."""
    console.print(f"\n[bold cyan]▸ {label}[/bold cyan]")
    code, stdout, stderr = session.exec(command, stream=True)
    if code != 0:
        raise RuntimeError(f"Step failed (exit {code}): {label}")
    return code, stdout, stderr


# ── s5cmd ──────────────────────────────────────────────────────────


def _s3_env(storage_slug: str) -> str:
    """Build env-var prefix for s5cmd from swm config."""
    from swm import config as cfg

    if storage_slug == "b2":
        endpoint = cfg.get("b2.s3_endpoint") or ""
        ak = cfg.get("b2.key_id") or ""
        sk = cfg.get("b2.app_key") or ""
    elif storage_slug == "gcs":
        endpoint = "https://storage.googleapis.com"
        ak = cfg.get("gcs.hmac_access") or ""
        sk = cfg.get("gcs.hmac_secret") or ""
    elif storage_slug == "s3":
        endpoint = ""
        ak = cfg.get("s3.access_key") or ""
        sk = cfg.get("s3.secret_key") or ""
    else:
        raise ValueError(f"Unknown storage slug: {storage_slug}")

    parts = [
        f"AWS_ACCESS_KEY_ID='{ak}'",
        f"AWS_SECRET_ACCESS_KEY='{sk}'",
    ]
    if endpoint:
        parts.append(f"S3_ENDPOINT_URL='{endpoint}'")
    return " ".join(parts)


def install_s5cmd(session: RemoteSession) -> None:
    """Download the s5cmd static binary if not already present."""
    _step(
        session,
        "Installing s5cmd",
        f"command -v s5cmd >/dev/null 2>&1 && echo 's5cmd already installed' || "
        f"(curl -sL '{S5CMD_URL}' | tar xz -C /usr/local/bin s5cmd "
        f"&& chmod +x /usr/local/bin/s5cmd && s5cmd version)",
    )


def _install_inotify(session: RemoteSession) -> None:
    """Install inotify-tools for the filesystem change watcher."""
    _step(
        session,
        "Installing inotify-tools",
        "command -v inotifywait >/dev/null 2>&1 && echo 'inotify-tools already installed' || "
        "(apt-get update -qq && apt-get install -y -qq inotify-tools && echo 'installed')",
    )


def configure_storage(
    session: RemoteSession, storage_slug: str, bucket: str = "",
) -> None:
    """Install s5cmd, inotify-tools, and verify the S3-compatible connection."""
    install_s5cmd(session)
    try:
        _install_inotify(session)
    except RuntimeError:
        console.print("  [yellow]⚠ inotify-tools install failed — push will use find[/yellow]")
    env = _s3_env(storage_slug)
    target = f"s3://{bucket}/" if bucket else ""
    _step(
        session,
        f"Verifying {storage_slug} connection",
        f"{env} s5cmd ls {target} 2>&1 | head -5 || true",
    )


_LOCK_FILE = "/tmp/.swm_transfer.lock"


def _acquire_transfer_lock(session: RemoteSession, force: bool = False) -> None:
    """Check for an existing transfer and acquire the lock.

    If a lock exists with a live PID, raises unless *force* is True
    (which kills the stale process first).
    """
    code, out, _ = session.exec(
        f"cat {_LOCK_FILE} 2>/dev/null", stream=False,
    )
    old_pid = out.strip()

    if old_pid:
        _, alive, _ = session.exec(
            f"kill -0 {old_pid} 2>/dev/null && echo alive || echo dead",
            stream=False,
        )
        if "alive" in alive:
            if not force:
                raise RuntimeError(
                    f"A transfer is already running (PID {old_pid}). "
                    "Use --force to kill it and start a new one."
                )
            console.print(
                f"  [yellow]⚠ Killing existing transfer (PID {old_pid})[/yellow]"
            )
            session.exec(f"kill -9 {old_pid} 2>/dev/null; sleep 1", stream=False)

        # Stale lock — clean up temp files left behind
        console.print("  [dim]Cleaning up stale temp files…[/dim]")
        _, cleanup_out, _ = session.exec(
            "find /workspace -maxdepth 5 -type f -regex '.*\\.[a-z]*[0-9]\\{9,\\}$' "
            "-delete -print 2>/dev/null | wc -l",
            stream=False,
        )
        n = cleanup_out.strip()
        if n and n != "0":
            console.print(f"  [dim]Removed {n} orphaned temp files[/dim]")

    session.exec(f"echo $$ > {_LOCK_FILE}", stream=False)


def _s5cmd_transfer(
    session: RemoteSession,
    label: str,
    s5cmd_cmd: str,
    force: bool = False,
    total_bytes: int = 0,
    total_files: int = 0,
) -> int:
    """Run an s5cmd transfer with output streamed directly to the terminal.

    Acquires a lock file on the pod, wraps the command in a shell trap
    for guaranteed cleanup (even on SSH disconnect), and streams
    s5cmd's native ``--show-progress`` output to the terminal.

    Returns the process exit code.
    """
    console.print(f"\n[bold cyan]▸ {label}[/bold cyan]")
    _acquire_transfer_lock(session, force=force)

    wrapped = (
        f"trap 'rm -f {_LOCK_FILE}' EXIT; "
        f"echo $$ > {_LOCK_FILE}; "
        f"{s5cmd_cmd}"
    )
    cmd = session._ssh_cmd() + [wrapped]
    code = subprocess.call(cmd)

    if code != 0:
        console.print(f"  [yellow]⚠ Transfer finished with warnings (exit {code})[/yellow]")
    else:
        console.print(f"  [green]✓ {label} — done[/green]")

    return code


# Backward-compatible re-exports (lazy to avoid circular imports)
_RE_EXPORTS: dict[str, str] = {
    "install_framework": "swm.bootstrap_frameworks",
    "start_framework": "swm.bootstrap_frameworks",
    "stop_framework": "swm.bootstrap_frameworks",
    "install_comfyui": "swm.bootstrap_frameworks",
    "install_swarmui": "swm.bootstrap_frameworks",
    "link_models_to_comfyui": "swm.bootstrap_frameworks",
    "wait_for_ssh": "swm.bootstrap_ssh",
    "next_workspace_name": "swm.bootstrap_ssh",
    "DiskCheck": "swm.bootstrap_sync",
    "preflight_check": "swm.bootstrap_sync",
    "start_watcher": "swm.bootstrap_sync",
    "stop_watcher": "swm.bootstrap_sync",
    "is_watcher_alive": "swm.bootstrap_sync",
    "workspace_pull": "swm.bootstrap_sync",
    "workspace_push": "swm.bootstrap_sync",
}


def __getattr__(name: str):  # noqa: E302
    if name in _RE_EXPORTS:
        import importlib
        mod = importlib.import_module(_RE_EXPORTS[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
