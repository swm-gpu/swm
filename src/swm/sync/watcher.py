"""Filesystem change watcher (inotify) running on the pod."""

from __future__ import annotations

import base64
import time

from swm.remote.ssh import RemoteSession
from swm.sync.paths import (
    WATCH_EXCLUDES,
    WATCH_LOG,
    WATCH_PID,
    WATCHER_EXCLUDES_FILE,
    WATCHER_SCRIPT,
    WATCHER_SPEC_FILE,
)

# Pending change-log entries are parked here across a watcher restart: the
# start script truncates WATCH_LOG, and losing in-flight paths/deletions on
# every swm upgrade that changes WATCH_EXCLUDES is not acceptable.
_WATCH_CARRY = "/tmp/.swm_watch_carry"

# Each log line is "EVENTS /path" (e.g. "MOVED_FROM,ISDIR /workspace/out"):
# a rename's old name and a moved directory are only recognisable by their
# events. Readers still accept the bare "/path" lines older watchers wrote.
_WATCH_EVENTS = "modify,create,delete,moved_to,moved_from"
_WATCH_FORMAT = "%e %w%f"
WATCH_SPEC = f"{_WATCH_EVENTS} {_WATCH_FORMAT}"


def _current_excludes_regex() -> str:
    return "|".join(WATCH_EXCLUDES)


def _pid_alive(session: RemoteSession) -> bool:
    _, out, _ = session.exec(
        f"test -f {WATCH_PID} && kill -0 $(cat {WATCH_PID}) 2>/dev/null "
        "&& echo alive || echo dead",
        stream=False,
    )
    return "alive" in out


def _excludes_fingerprint_matches(session: RemoteSession) -> bool:
    """True iff the on-pod fingerprint file matches the current exclude regex.

    Used to detect long-lived pods running a watcher started by an older
    version of swm whose exclude list has since been updated.
    """
    desired = _current_excludes_regex()
    _, out, _ = session.exec(
        f"cat {WATCHER_EXCLUDES_FILE} 2>/dev/null", stream=False,
    )
    return out.rstrip("\n") == desired


def _spec_matches(session: RemoteSession) -> bool:
    """True iff the running watcher logs the current events and format."""
    _, out, _ = session.exec(f"cat {WATCHER_SPEC_FILE} 2>/dev/null", stream=False)
    return out.rstrip("\n") == WATCH_SPEC


def start_watcher(session: RemoteSession, src: str = "/workspace") -> bool:
    """Start an inotifywait daemon to track filesystem changes.

    If a watcher is already running with the *current* exclude list, this
    is a no-op. If the running watcher was started with a stale exclude
    list (because swm was upgraded since the pod was bootstrapped), it is
    killed and restarted with the latest excludes so changes to
    ``WATCH_EXCLUDES`` actually take effect on long-lived pods.

    Returns True if the watcher is running with the current excludes.
    """
    if (_pid_alive(session) and _excludes_fingerprint_matches(session)
            and _spec_matches(session)):
        return True

    restarting = _pid_alive(session)
    if restarting:
        session.exec(
            f"cp -f {WATCH_LOG} {_WATCH_CARRY} 2>/dev/null || : > {_WATCH_CARRY}",
            stream=False,
        )
        stop_watcher(session)

    _, has_cmd, _ = session.exec(
        "command -v inotifywait >/dev/null 2>&1 && echo yes || echo no",
        stream=False,
    )
    if "yes" not in has_cmd:
        return False

    exclude_re = _current_excludes_regex()
    excludes_b64 = base64.b64encode(exclude_re.encode()).decode()
    spec_b64 = base64.b64encode(WATCH_SPEC.encode()).decode()
    script_body = (
        "#!/bin/bash\n"
        f"rm -f {WATCH_LOG}\n"
        f": > {WATCH_LOG}\n"
        f"echo '{excludes_b64}' | base64 -d > {WATCHER_EXCLUDES_FILE}\n"
        f"echo '{spec_b64}' | base64 -d > {WATCHER_SPEC_FILE}\n"
        f"nohup inotifywait -m -r "
        f"--exclude '({exclude_re})' "
        f"-e {_WATCH_EVENTS} "
        f"--format '{_WATCH_FORMAT}' "
        f"'{src}' >> {WATCH_LOG} 2>/dev/null &\n"
        f"echo $! > {WATCH_PID}\n"
    )
    b64 = base64.b64encode(script_body.encode()).decode()
    session.exec(
        f"echo '{b64}' | base64 -d > {WATCHER_SCRIPT} && "
        f"chmod +x {WATCHER_SCRIPT} && bash {WATCHER_SCRIPT}",
        stream=False,
    )
    if restarting:
        session.exec(
            f"cat {_WATCH_CARRY} >> {WATCH_LOG} 2>/dev/null; rm -f {_WATCH_CARRY}",
            stream=False,
        )

    time.sleep(1)
    return _pid_alive(session)


def stop_watcher(session: RemoteSession) -> None:
    """Stop the inotifywait daemon if running.

    Belt-and-braces: kills by PID file, then by process name in case the
    PID file was lost or pointed at a stale pid (orphaned watcher).
    """
    session.exec(
        f"test -f {WATCH_PID} && kill $(cat {WATCH_PID}) 2>/dev/null; "
        "pkill -f 'inotifywait -m -r --exclude' 2>/dev/null || true; "
        f"rm -f {WATCH_PID} {WATCHER_EXCLUDES_FILE} {WATCHER_SPEC_FILE}",
        stream=False,
    )


def is_watcher_alive(session: RemoteSession) -> bool:
    """Check if the filesystem watcher daemon is running."""
    return _pid_alive(session)
