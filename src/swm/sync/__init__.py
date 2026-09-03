"""Workspace sync subsystem: preflight, watcher, pull, push, auto-sync."""

from __future__ import annotations

from swm.sync._common import ensure_zstd
from swm.sync.autosync import (
    AutosyncUnsafeError,
    autosync_status,
    is_autosync_alive,
    start_autosync,
    stop_autosync,
)
from swm.sync.paths import PUSH_STAMP, WATCH_LOG
from swm.sync.preflight import DiskCheck, preflight_check
from swm.sync.pull import tar_object, tar_pull, workspace_pull
from swm.sync.push import workspace_push
from swm.sync.watcher import is_watcher_alive, start_watcher, stop_watcher

__all__ = [
    "DiskCheck",
    "preflight_check",
    "start_watcher",
    "stop_watcher",
    "is_watcher_alive",
    "workspace_pull",
    "tar_pull",
    "tar_object",
    "ensure_zstd",
    "workspace_push",
    "start_autosync",
    "stop_autosync",
    "is_autosync_alive",
    "autosync_status",
    "AutosyncUnsafeError",
    "PUSH_STAMP",
    "WATCH_LOG",
]
