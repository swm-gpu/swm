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

STAGING = "/tmp/.swm_push_staging"
TAR_PATH = "/workspace/.swm_workspace.tar.gz"
DELETED_LIST = "/tmp/.swm_push_deleted"

# ── Auto-sync daemon ───────────────────────────────────────────────

AUTO_PID = "/tmp/.swm_autosync.pid"
AUTO_LOG = "/workspace/.swm_autosync.log"
AUTO_SCRIPT = "/tmp/.swm_autosync.sh"

# Shared across manual push + auto-sync so they don't clobber each other.
TRANSFER_LOCK = "/tmp/.swm_transfer.lock"

# Default regex excludes used by the inotify watcher.
WATCH_EXCLUDES: tuple[str, ...] = (
    r"\.swm_changes\.log",
    r"\.swm_last_push",
    r"\.swm_watcher\.pid",
    r"\.swm_workspace\.tar\.gz",
    r"/\.swm_guard/",
    r"/\.git/",
    r"__pycache__",
    r"\.log$",
    r"/\.cache/",
    r"/\.nv/",
)
