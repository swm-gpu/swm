"""Filesystem change watcher (inotify) running on the pod."""

from __future__ import annotations

import base64
import time

from swm.remote.ssh import RemoteSession
from swm.sync.paths import (
    WATCH_EXCLUDES,
    WATCH_LOG,
    WATCH_PID,
    WATCHER_SCRIPT,
)


def _pid_alive(session: RemoteSession) -> bool:
    _, out, _ = session.exec(
        f"test -f {WATCH_PID} && kill -0 $(cat {WATCH_PID}) 2>/dev/null "
        "&& echo alive || echo dead",
        stream=False,
    )
    return "alive" in out


def start_watcher(session: RemoteSession, src: str = "/workspace") -> bool:
    """Start an inotifywait daemon to track filesystem changes.

    Returns True if the watcher was started (or already running).
    """
    if _pid_alive(session):
        return True

    _, has_cmd, _ = session.exec(
        "command -v inotifywait >/dev/null 2>&1 && echo yes || echo no",
        stream=False,
    )
    if "yes" not in has_cmd:
        return False

    exclude_re = "|".join(WATCH_EXCLUDES)
    script_body = (
        "#!/bin/bash\n"
        f"rm -f {WATCH_LOG}\n"
        f": > {WATCH_LOG}\n"
        f"nohup inotifywait -m -r "
        f"--exclude '({exclude_re})' "
        f"-e modify,create,delete,moved_to "
        f"--format '%w%f' "
        f"'{src}' >> {WATCH_LOG} 2>/dev/null &\n"
        f"echo $! > {WATCH_PID}\n"
    )
    b64 = base64.b64encode(script_body.encode()).decode()
    session.exec(
        f"echo '{b64}' | base64 -d > {WATCHER_SCRIPT} && "
        f"chmod +x {WATCHER_SCRIPT} && bash {WATCHER_SCRIPT}",
        stream=False,
    )

    time.sleep(1)
    return _pid_alive(session)


def stop_watcher(session: RemoteSession) -> None:
    """Stop the inotifywait daemon if running."""
    session.exec(
        f"test -f {WATCH_PID} && kill $(cat {WATCH_PID}) 2>/dev/null; "
        f"rm -f {WATCH_PID}",
        stream=False,
    )


def is_watcher_alive(session: RemoteSession) -> bool:
    """Check if the filesystem watcher daemon is running."""
    return _pid_alive(session)
