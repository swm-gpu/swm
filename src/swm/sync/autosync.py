"""Background auto-sync daemon: periodically pushes changes to storage."""

from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path

from swm import config as cfg
from swm.remote.ssh import RemoteSession
from swm.sync.paths import (
    AUTO_ENV,
    AUTO_LOG,
    AUTO_PID,
    AUTO_SCRIPT,
    PUSH_STAMP,
    TRANSFER_LOCK,
    WATCH_EXCLUDES,
    WATCH_LOG,
    WATCHER_EXCLUDES_FILE,
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
        endpoint = cfg.get("s3.endpoint_url") or ""
        ak = cfg.get("s3.access_key") or ""
        sk = cfg.get("s3.secret_key") or ""
    else:
        raise ValueError(f"Unknown storage slug: {storage_slug}")

    region = str(cfg.get("aws.region") or "")
    lines = [
        f"export AWS_ACCESS_KEY_ID='{ak}'",
        f"export AWS_SECRET_ACCESS_KEY='{sk}'",
    ]
    if endpoint:
        lines.append(f"export S3_ENDPOINT_URL='{endpoint}'")
    if region:
        lines.append(f"export AWS_REGION='{region}'")
    return "\n".join(lines)


def _render_daemon_script(
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str,
    interval: int,
) -> str:
    """Render the daemon bash script with concrete values substituted.

    Credentials are deliberately NOT part of the render — they live in a
    separate 0600 file (see ``_write_env_file``) that the daemon sources
    every cycle. The script stays free of secrets and its content hash
    (``is_autosync_current``) stays stable across credential rotations.
    """
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
        "__SWM_WATCHER_EXCLUDES_FILE__": WATCHER_EXCLUDES_FILE,
        "__SWM_WATCHER_EXCLUDES__": "|".join(WATCH_EXCLUDES),
        "__SWM_ENV_FILE__": AUTO_ENV,
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def _write_env_file(session: RemoteSession, storage_slug: str) -> None:
    """Atomically write the daemon's credentials file with 0600 perms.

    The umask guarantees the temp file is 0600 from creation; the rename
    replaces any pre-existing file (and its permissions) atomically, so a
    concurrently running daemon cycle sees either the old or the new
    credentials, never a partial or missing file.
    """
    body = _storage_env_exports(storage_slug) + "\n"
    b64 = base64.b64encode(body.encode()).decode()
    exit_code, _, _ = session.exec(
        f"( umask 077; echo '{b64}' | base64 -d > {AUTO_ENV}.tmp ) && "
        f"mv -f {AUTO_ENV}.tmp {AUTO_ENV}",
        stream=False,
    )
    if exit_code != 0:
        raise RuntimeError(
            f"failed to write daemon credentials to {AUTO_ENV}"
        )


def refresh_credentials(session: RemoteSession, storage_slug: str) -> None:
    """Public entry point: rewrite the daemon's 0600 credentials file.

    The daemon sources the file every cycle, so a rotated key takes
    effect within one interval without restarting anything.
    """
    _write_env_file(session, storage_slug)


def is_autosync_current(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str = "/workspace",
    interval: int = 60,
) -> bool:
    """True iff the deployed daemon script matches the current render.

    A running daemon keeps executing the script it was deployed with —
    exclude lists, staging layout, and credentials baked at deploy time.
    Comparing content hashes lets callers detect and replace a stale
    daemon after an swm upgrade or a config change.
    """
    body = _render_daemon_script(storage_slug, bucket, workspace, src, interval)
    want = hashlib.sha256(body.encode()).hexdigest()
    _, out, _ = session.exec(
        f"sha256sum {AUTO_SCRIPT} 2>/dev/null | cut -d' ' -f1",
        stream=False,
    )
    return out.strip() == want


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

    Returns True if the daemon was started (or was already running with
    a current script). A running daemon whose deployed script is stale
    (older swm, changed config) is stopped and redeployed.
    Raises ``AutosyncUnsafeError`` if the safety check fails.
    """
    if _pid_alive(session):
        if is_autosync_current(
            session, storage_slug, bucket, workspace, src, interval,
        ):
            # Script is current — refresh only the credentials file so a
            # rotated key takes effect within one cycle, no restart.
            _write_env_file(session, storage_slug)
            return True
        stop_autosync(session)
        time.sleep(1)

    if not force and not _pull_stamp_exists(session):
        raise AutosyncUnsafeError(
            f"No push stamp at {PUSH_STAMP} — pod and bucket are not "
            f"known to be in sync. Run `swm sync pull` first, or pass "
            f"force=True to accept the risk (local deletions will "
            f"propagate to storage)."
        )

    # When force=True, also write the push stamp so the daemon's own
    # defense-in-depth stamp check inside _autosync_daemon.sh does not
    # cause every cycle to no-op. Bypassing the client-side
    # AutosyncUnsafeError without also creating the stamp leaves the
    # daemon running but inert — silently the same problem we were
    # trying to bypass.
    if force and not _pull_stamp_exists(session):
        session.exec(f"touch {PUSH_STAMP}", stream=False)

    if not is_watcher_alive(session):
        if not start_watcher(session, src):
            return False

    _write_env_file(session, storage_slug)
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
    """Stop the background auto-sync daemon and remove its credentials."""
    session.exec(
        f"test -f {AUTO_PID} && kill $(cat {AUTO_PID}) 2>/dev/null; "
        f"rm -f {AUTO_PID} {AUTO_ENV}",
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
