"""Shared filesystem paths used by the sync subsystem on the pod."""

from __future__ import annotations

# ── Watcher / push bookkeeping on the pod ──────────────────────────

PUSH_STAMP = "/workspace/.swm_last_push"
WATCH_LOG = "/workspace/.swm_changes.log"
WATCH_PID = "/tmp/.swm_watcher.pid"
WATCHER_SCRIPT = "/tmp/.swm_start_watcher.sh"

# Fingerprint of the regex passed to inotifywait --exclude on the
# currently-running watcher.  Lets swm detect when a pod is running a
# watcher started by an older version with a stale exclude list, and
# restart it so changes to WATCH_EXCLUDES below actually take effect on
# long-lived pods.
WATCHER_EXCLUDES_FILE = "/tmp/.swm_watcher.excludes"

# ── Push-time staging ──────────────────────────────────────────────

# Staging must live on the same filesystem as the tree being pushed so the
# hardlink tree costs zero bytes. /tmp is typically a small container overlay
# on a different device than /workspace — hardlinks fail there, and a copy
# fallback can fill the overlay and take the pod down.
#
# The staging root and its push/autosync subdirs are PERSISTENT: cycles
# delete only the staged files (find -type f -delete), never the dirs.
# Ephemeral excluded dirs are poison — the bare-path create/delete events
# they emit evade slash-anchored excludes (and inotify-tools >= 3.22
# bypasses --exclude entirely for directory creates), and a vanished
# logged path becomes an `s5cmd rm` on a nonexistent key that wedges
# delete-reconciliation. Also referenced (as a literal) in
# bootstrap._WS_MARKER_NAMES and _autosync_daemon.sh.
STAGING_ROOT_NAME = ".swm_staging"


def staging_dir_for(src: str) -> str:
    """Push staging directory for *src*, on the same filesystem."""
    return f"{src.rstrip('/')}/{STAGING_ROOT_NAME}/push"


TAR_PATH = "/workspace/.swm_workspace.tar.gz"
DELETED_LIST = "/tmp/.swm_push_deleted"

# ── Auto-sync daemon ───────────────────────────────────────────────

AUTO_PID = "/tmp/.swm_autosync.pid"
AUTO_LOG = "/workspace/.swm_autosync.log"
AUTO_SCRIPT = "/tmp/.swm_autosync.sh"
# Storage credentials live in a separate 0600 file sourced by the daemon
# each cycle — never inside the (world-readable, hash-compared) script.
AUTO_ENV = "/tmp/.swm_autosync.env"

# Shared across manual push + auto-sync so they don't clobber each other.
TRANSFER_LOCK = "/tmp/.swm_transfer.lock"

# Default regex excludes used by the inotify watcher.
WATCH_EXCLUDES: tuple[str, ...] = (
    r"\.swm_changes\.log",
    r"\.swm_last_push",
    r"\.swm_watcher\.pid",
    r"\.swm_workspace\.tar\.gz",
    # Push/autosync staging lives inside the synced tree (same filesystem,
    # so hardlinks work) and must never sync itself. The daemon and tier-1
    # push additionally filter their snapshot lists through these excludes,
    # because inotify-tools >= 3.22 lets directory-create events through
    # regardless of --exclude.
    r"/\.swm_staging/",
    r"/\.swm_guard/",
    r"/\.git/",
    r"__pycache__",
    r"\.log$",
    r"/\.cache/",
    r"/\.nv/",
    # uv's build/wheel cache (workspace-owned uv from v0.2.x). Regenerable,
    # and its ephemeral builds-v0/.tmp* dirs churn constantly — syncing it
    # both wastes storage and causes delete-reconciliation failures.
    r"/\.uv-cache/",
    # Managed-CPython terminfo database (workspace-owned python). Its entries
    # are dedup-hardlinked, which breaks hardlink-staging (s5cmd then can't
    # open them → push fails). Non-essential for any venv/ML workload.
    r"/terminfo/",
)
