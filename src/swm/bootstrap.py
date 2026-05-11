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
        endpoint = cfg.get("s3.endpoint_url") or ""
        ak = cfg.get("s3.access_key") or ""
        sk = cfg.get("s3.secret_key") or ""
    else:
        raise ValueError(f"Unknown storage slug: {storage_slug}")

    region = str(cfg.get("aws.region") or "")
    parts = [
        f"AWS_ACCESS_KEY_ID='{ak}'",
        f"AWS_SECRET_ACCESS_KEY='{sk}'",
    ]
    if endpoint:
        parts.append(f"S3_ENDPOINT_URL='{endpoint}'")
    if region:
        parts.append(f"AWS_REGION='{region}'")
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


_WS_MARKER_NAMES = (
    ".swm_last_push",
    ".swm_changes.log",
    ".swm_workspace.tar.gz",
    ".swm_autosync.log",
    ".swm_guard",
    ".swm_watcher.pid",
)


def _ensure_workspace_empty_on_pod(session: RemoteSession) -> None:
    """Raise if /workspace/ on the pod contains any non-marker files.

    A blind ``touch PUSH_STAMP`` on a non-empty workspace would silently
    declare those files synced even though they were never uploaded.
    Refuse, and tell the user to push or clear before attaching.
    """
    excludes = " ".join(f"-not -name '{n}'" for n in _WS_MARKER_NAMES)
    cmd = (
        f"find /workspace -mindepth 1 -maxdepth 1 {excludes} 2>/dev/null "
        "| head -5"
    )
    _, out, _ = session.exec(cmd, stream=False)
    leftover = [line for line in out.splitlines() if line.strip()]
    if leftover:
        sample = ", ".join(p.rsplit("/", 1)[-1] for p in leftover[:3])
        more = "" if len(leftover) <= 3 else f" (+ more)"
        raise RuntimeError(
            f"/workspace/ on the pod is not empty (e.g. {sample}{more}). "
            "Refusing to mark this as a fresh workspace because those "
            "files would not be uploaded. Either: (a) upload them first "
            "with `swm sync push <pod> -b <provider:bucket> -d <name> "
            "--force`, then re-run setup; or (b) clear /workspace/ on "
            "the pod and re-run; or (c) re-run with an existing "
            "workspace name to pull from storage."
        )


def bootstrap_workspace_on_pod(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    *,
    qualified_id: str,
    is_new: bool,
    extra_excludes: list[str] | None = None,
    autosync_interval: int = 60,
    console_obj: Console | None = None,
) -> list[tuple[str, str]]:
    """Configure storage, pull (or watcher-init), and start auto-sync on a pod.

    Returns a list of ``(label, recovery_command)`` tuples for any sub-step
    that failed. An empty list means full success.
    """
    from swm.sync import start_watcher
    from swm.sync.autosync import AutosyncUnsafeError, start_autosync
    from swm.sync.paths import PUSH_STAMP, WATCH_LOG
    from swm.sync.pull import workspace_pull

    _con = console_obj or console
    failed: list[tuple[str, str]] = []

    def _fail(label: str, cmd: str, exc: Exception) -> None:
        _con.print(f"  [yellow]⚠ {label} failed: {exc}[/yellow]")
        failed.append((label, cmd))

    storage_ok = False
    try:
        with _con.status(
            "Installing s5cmd & configuring storage…", spinner="dots"
        ):
            configure_storage(session, storage_slug, bucket=bucket)
        _con.print("[green]✓[/green] Storage configured")
        storage_ok = True
    except Exception as exc:
        _fail("Storage configuration", f"swm setup storage {qualified_id}", exc)

    pull_ok = False
    if storage_ok:
        try:
            if is_new:
                _ensure_workspace_empty_on_pod(session)
                _con.print("  [dim]New workspace — skipping pull[/dim]")
                session.exec(
                    f": > {WATCH_LOG} 2>/dev/null; touch {PUSH_STAMP}",
                    stream=False,
                )
                if start_watcher(session, "/workspace"):
                    _con.print("  [dim]Watcher started for change tracking[/dim]")
            else:
                workspace_pull(
                    session, storage_slug, bucket, workspace,
                    extra_excludes=extra_excludes,
                )
            pull_ok = True
        except Exception as exc:
            _fail("Workspace pull", f"swm sync pull {qualified_id}", exc)
    else:
        failed.append(("Workspace pull", f"swm sync pull {qualified_id}"))

    if pull_ok:
        try:
            if start_autosync(
                session, storage_slug, bucket, workspace,
                interval=autosync_interval,
            ):
                _con.print(
                    "  [dim]Auto-sync started "
                    f"(every {autosync_interval}s → "
                    f"{storage_slug}:{bucket}/{workspace})[/dim]"
                )
        except Exception as exc:
            _fail("Auto-sync start", f"swm sync auto {qualified_id}", exc)
    else:
        failed.append(("Auto-sync start", f"swm sync auto {qualified_id}"))

    return failed


def _acquire_transfer_lock(session: RemoteSession, force: bool = False) -> None:
    """Check for an existing transfer and acquire the lock.

    If a lock exists with a live PID, raises unless *force* is True
    (which kills the stale process first).
    """
    from swm.sync.paths import TRANSFER_LOCK

    code, out, _ = session.exec(
        f"cat {TRANSFER_LOCK} 2>/dev/null", stream=False,
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

    session.exec(f"echo $$ > {TRANSFER_LOCK}", stream=False)


def _s5cmd_transfer(
    session: RemoteSession,
    label: str,
    s5cmd_cmd: str,
    force: bool = False,
) -> int:
    """Run an s5cmd transfer with output streamed directly to the terminal.

    Acquires a lock file on the pod, wraps the command in a shell trap
    for guaranteed cleanup (even on SSH disconnect), and streams
    s5cmd's native ``--show-progress`` output to the terminal.

    Returns the process exit code.
    """
    from swm.sync.paths import TRANSFER_LOCK

    console.print(f"\n[bold cyan]▸ {label}[/bold cyan]")
    _acquire_transfer_lock(session, force=force)

    wrapped = (
        f"trap 'rm -f {TRANSFER_LOCK}' EXIT; "
        f"echo $$ > {TRANSFER_LOCK}; "
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
    "link_models_to_comfyui": "swm.bootstrap_frameworks",
    "wait_for_ssh": "swm.bootstrap_ssh",
    "next_workspace_name": "swm.bootstrap_ssh",
    "DiskCheck": "swm.sync",
    "preflight_check": "swm.sync",
    "start_watcher": "swm.sync",
    "stop_watcher": "swm.sync",
    "is_watcher_alive": "swm.sync",
    "workspace_pull": "swm.sync",
    "workspace_push": "swm.sync",
    "tar_pull": "swm.sync",
    "start_autosync": "swm.sync",
    "stop_autosync": "swm.sync",
    "is_autosync_alive": "swm.sync",
    "autosync_status": "swm.sync",
}


def __getattr__(name: str):  # noqa: E302
    if name in _RE_EXPORTS:
        import importlib
        mod = importlib.import_module(_RE_EXPORTS[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
