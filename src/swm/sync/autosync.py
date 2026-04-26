"""Background auto-sync daemon: periodically pushes changes to storage."""

from __future__ import annotations

import base64
import time
from pathlib import Path

from swm import config as cfg
from swm.remote.ssh import RemoteSession
from swm.sync.paths import (
    AUTO_LOCK,
    AUTO_LOG,
    AUTO_PID,
    AUTO_SCRIPT,
    PUSH_STAMP,
    TRANSFER_LOCK,
    WATCH_LOG,
)
from swm.sync.watcher import is_watcher_alive, start_watcher

_DAEMON_TEMPLATE = Path(__file__).with_name("_autosync_daemon.sh")


def _pid_alive(session: RemoteSession) -> bool:
    _, out, _ = session.exec(
        f"test -f {AUTO_PID} && kill -0 $(cat {AUTO_PID}) 2>/dev/null "
        "&& echo alive || echo dead",
        stream=False,
    )
    return "alive" in out


def is_autosync_alive(session: RemoteSession) -> bool:
    """Check if the background auto-sync daemon is running."""
    return _pid_alive(session)


def _storage_env_exports(storage_slug: str) -> str:
    """Build shell `export` lines for s5cmd credentials."""
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

    lines = [
        f"export AWS_ACCESS_KEY_ID='{ak}'",
        f"export AWS_SECRET_ACCESS_KEY='{sk}'",
    ]
    if endpoint:
        lines.append(f"export S3_ENDPOINT_URL='{endpoint}'")
    return "\n".join(lines)


def _render_daemon_script(
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str,
    interval: int,
) -> str:
    """Render the daemon bash script with concrete values substituted."""
    template = _DAEMON_TEMPLATE.read_text()
    replacements = {
        "__SWM_INTERVAL__": str(interval),
        "__SWM_SRC__": src,
        "__SWM_WORKSPACE__": workspace,
        "__SWM_BUCKET__": bucket,
        "__SWM_WATCH_LOG__": WATCH_LOG,
        "__SWM_PUSH_STAMP__": PUSH_STAMP,
        "__SWM_AUTO_LOG__": AUTO_LOG,
        "__SWM_TRANSFER_LOCK__": TRANSFER_LOCK,
        "__SWM_ENV_EXPORTS__": _storage_env_exports(storage_slug),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


class AutosyncUnsafeError(RuntimeError):
    """Raised when auto-sync is started before a successful pull/push.

    Starting the daemon before the pod and bucket are known to be
    consistent is dangerous: a stray local deletion would propagate to
    storage and wipe the remote copy.
    """


def _pull_stamp_exists(session: RemoteSession) -> bool:
    _, out, _ = session.exec(
        f"test -f {PUSH_STAMP} && echo yes || echo no", stream=False,
    )
    return out.strip() == "yes"


def start_autosync(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str = "/workspace",
    interval: int = 60,
    force: bool = False,
) -> bool:
    """Start a background daemon that syncs the workspace on an interval.

    The daemon reads the inotify watcher log every *interval* seconds,
    uploads changed files, and deletes removed files from storage.

    Requires the filesystem watcher (``start_watcher``) to be running.
    If the watcher isn't running, this function will try to start it.

    **Safety:** Refuses to start unless a successful pull or push has
    already occurred (marked by the presence of ``PUSH_STAMP``).  Pass
    ``force=True`` to bypass this check — only safe when the workspace
    has no remote copy yet, or you explicitly want the pod's current
    state to become the authoritative copy.

    Returns True if the daemon was started (or was already running).
    Raises ``AutosyncUnsafeError`` if the safety check fails.
    """
    if _pid_alive(session):
        return True

    if not force and not _pull_stamp_exists(session):
        raise AutosyncUnsafeError(
            f"No push stamp at {PUSH_STAMP} — pod and bucket are not "
            f"known to be in sync. Run `swm sync pull` first, or pass "
            f"force=True to accept the risk (local deletions will "
            f"propagate to storage)."
        )

    if not is_watcher_alive(session):
        if not start_watcher(session, src):
            return False

    script_body = _render_daemon_script(
        storage_slug, bucket, workspace, src, interval,
    )
    b64 = base64.b64encode(script_body.encode()).decode()
    session.exec(
        f"echo '{b64}' | base64 -d > {AUTO_SCRIPT} && "
        f"chmod +x {AUTO_SCRIPT} && "
        f"( setsid bash {AUTO_SCRIPT} < /dev/null >> {AUTO_LOG} 2>&1 "
        f"& echo $! > {AUTO_PID} ) &",
        stream=False,
    )

    time.sleep(1)
    return _pid_alive(session)


def stop_autosync(session: RemoteSession) -> None:
    """Stop the background auto-sync daemon if running."""
    session.exec(
        f"test -f {AUTO_PID} && kill $(cat {AUTO_PID}) 2>/dev/null; "
        f"rm -f {AUTO_PID} {AUTO_LOCK}",
        stream=False,
    )


def autosync_status(session: RemoteSession) -> tuple[bool, str]:
    """Return (running, last_log_tail) for the auto-sync daemon."""
    alive = _pid_alive(session)
    _, tail, _ = session.exec(
        f"tail -n 20 {AUTO_LOG} 2>/dev/null || echo '(no log)'",
        stream=False,
    )
    return alive, tail
