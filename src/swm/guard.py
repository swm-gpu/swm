"""Lifecycle guard helpers for idle pod reminders and automation."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass

import click

from swm import config as cfg
from swm.bootstrap import console
from swm.sync.paths import WATCH_LOG as _WATCH_LOG
from swm.commands._helpers import _instance_for
from swm.providers import resolve_instance
from swm.providers.base import Instance, InstanceStatus
from swm.remote.ssh import RemoteSession, session_from_instance

_GUARD_DIR = "/workspace/.swm_guard"
_GUARD_SCRIPT = f"{_GUARD_DIR}/watcher.py"
_GUARD_STATUS = f"{_GUARD_DIR}/status.json"
_GUARD_PID = "/tmp/.swm_guard.pid"
_GUARD_LOG = "/tmp/.swm_guard.log"
_GPU_ACTIVE_THRESHOLD = 10.0
_REMINDER_COOLDOWN_SECONDS = 30 * 60

_WATCHER_SOURCE = f"""#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import sys
import time

GUARD_DIR = "{_GUARD_DIR}"
STATUS_PATH = "{_GUARD_STATUS}"
PID_PATH = "{_GUARD_PID}"
WATCH_LOG = "{_WATCH_LOG}"
TRANSFER_LOCK = "/tmp/.swm_transfer.lock"
POLL = int(sys.argv[1]) if len(sys.argv) > 1 else 60
GPU_THRESHOLD = float(sys.argv[2]) if len(sys.argv) > 2 else {_GPU_ACTIVE_THRESHOLD}
stop_requested = False
last_active_ts = time.time()


def sh(command: str) -> str:
    proc = subprocess.run(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return proc.stdout.strip()


def ssh_connections() -> int:
    out = sh("who 2>/dev/null | wc -l")
    try:
        return int(out.strip() or "0")
    except ValueError:
        return 0


def avg_gpu_util() -> float:
    out = sh("nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null")
    vals = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            vals.append(float(line))
        except ValueError:
            pass
    if not vals:
        return 0.0
    return round(sum(vals) / len(vals), 1)


def recent_fs_write(now: float) -> bool:
    # Detect REAL workspace writes (user data, generation output, installs),
    # excluding swm's own bookkeeping (autosync change-log/stamps/guard), build
    # caches, logs, and framework noise. Keying off WATCH_LOG mtime was a bug:
    # the autosync watcher rewrites that file every cycle, so the pod looked
    # perpetually active and auto-down could never fire.
    minutes = max(2, int(max(POLL * 2, 120) / 60))
    out = sh(
        "find /workspace -mindepth 1 -mmin -%d -type f "
        "-not -path '*/.swm_*' -not -path '*/.swm_guard/*' "
        "-not -path '*/.uv-cache/*' -not -path '*/.cache/*' "
        "-not -path '*/__pycache__/*' -not -name '*.log' "
        "-not -path '*/terminfo/*' -not -path '*/.git/*' "
        "-not -path '*/.nv/*' 2>/dev/null | head -1" % minutes
    )
    return bool(out.strip())


def transfer_locked() -> bool:
    return os.path.exists(TRANSFER_LOCK)


def busy_processes() -> list[str]:
    out = sh(
        "pgrep -af 'pip install|huggingface-cli download|hf download|s5cmd |tar czf|scp -r|rsync|uv pip install' "
        "| grep -v swm_guard | head -5"
    )
    return [line for line in out.splitlines() if line.strip()]


def loadavg() -> float:
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def write_status(payload: dict) -> None:
    os.makedirs(GUARD_DIR, exist_ok=True)
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, STATUS_PATH)


def handle_signal(*_args) -> None:
    global stop_requested
    stop_requested = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)
os.makedirs(GUARD_DIR, exist_ok=True)
with open(PID_PATH, "w", encoding="utf-8") as f:
    f.write(str(os.getpid()))

while not stop_requested:
    now = time.time()
    ssh = ssh_connections()
    gpu = avg_gpu_util()
    recent_write = recent_fs_write(now)
    locked = transfer_locked()
    busy = busy_processes()
    load = loadavg()
    active = bool(ssh or locked or busy or recent_write or gpu >= GPU_THRESHOLD)
    if active:
        last_active_ts = now
    write_status(
        {{
            "timestamp": now,
            "last_active_ts": last_active_ts,
            "idle_seconds": round(max(0.0, now - last_active_ts), 1),
            "active": active,
            "ssh_connections": ssh,
            "avg_gpu_util": gpu,
            "recent_fs_write": recent_write,
            "transfer_locked": locked,
            "busy_processes": busy,
            "loadavg": load,
            "poll_interval_seconds": POLL,
            "gpu_active_threshold": GPU_THRESHOLD,
        }}
    )
    time.sleep(POLL)

try:
    os.remove(PID_PATH)
except OSError:
    pass
"""


@dataclass
class GuardPolicy:
    mode: str = "manual"
    idle_timeout_minutes: int = 60
    poll_interval_seconds: int = 60

    @property
    def enabled(self) -> bool:
        return self.mode != "manual"

    @property
    def action_label(self) -> str:
        return self.mode


def _coalesce_int(*values, default: int) -> int:
    """Return ``int(first non-None value)``, falling back to ``default``.

    Distinct from ``int(... or default)`` because explicit ``0`` is honoured
    instead of being silently replaced by ``default``.
    """
    for v in values:
        if v is not None:
            return int(v)
    return default


def _normalize_mode(mode: str | None) -> str:
    raw = str(mode or "manual").strip().lower()
    aliases = {
        "off": "manual",
        "disabled": "manual",
        "manual": "manual",
        "remind": "remind",
        "auto-stop": "auto-stop",
        "stop": "auto-stop",
        "auto-down": "auto-down",
        "down": "auto-down",
    }
    return aliases.get(raw, "manual")


def get_defaults() -> GuardPolicy:
    """Return the global default guard policy from config."""
    d = cfg.get("guard.defaults", {}) or {}
    return GuardPolicy(
        mode=_normalize_mode(d.get("mode") or "manual"),
        idle_timeout_minutes=_coalesce_int(d.get("idle_timeout_minutes"), default=60),
        poll_interval_seconds=_coalesce_int(d.get("poll_interval_seconds"), default=60),
    )


def set_defaults(
    *,
    mode: str | None = None,
    idle_timeout_minutes: int | None = None,
    poll_interval_seconds: int | None = None,
) -> GuardPolicy:
    """Update the global default guard policy in config."""
    cur = get_defaults()
    policy = GuardPolicy(
        mode=_normalize_mode(mode) if mode else cur.mode,
        idle_timeout_minutes=_coalesce_int(idle_timeout_minutes, default=cur.idle_timeout_minutes),
        poll_interval_seconds=_coalesce_int(poll_interval_seconds, default=cur.poll_interval_seconds),
    )
    cfg.set_value("guard.defaults.mode", policy.mode)
    cfg.set_value("guard.defaults.idle_timeout_minutes", str(policy.idle_timeout_minutes))
    cfg.set_value("guard.defaults.poll_interval_seconds", str(policy.poll_interval_seconds))
    return policy


def get_policy(instance_id: str) -> GuardPolicy:
    defaults = cfg.get("guard.defaults", {}) or {}
    pod = cfg.get(f"pods.{instance_id}.guard", {}) or {}
    mode = _normalize_mode(pod.get("mode") or defaults.get("mode") or "manual")
    idle = _coalesce_int(
        pod.get("idle_timeout_minutes"),
        defaults.get("idle_timeout_minutes"),
        default=60,
    )
    poll = _coalesce_int(
        pod.get("poll_interval_seconds"),
        defaults.get("poll_interval_seconds"),
        default=60,
    )
    return GuardPolicy(mode=mode, idle_timeout_minutes=idle, poll_interval_seconds=poll)


def set_policy(
    instance_id: str,
    *,
    mode: str,
    idle_timeout_minutes: int | None = None,
    poll_interval_seconds: int | None = None,
) -> GuardPolicy:
    existing = get_policy(instance_id)
    policy = GuardPolicy(
        mode=_normalize_mode(mode),
        idle_timeout_minutes=_coalesce_int(
            idle_timeout_minutes, existing.idle_timeout_minutes, default=60,
        ),
        poll_interval_seconds=_coalesce_int(
            poll_interval_seconds, existing.poll_interval_seconds, default=60,
        ),
    )
    cfg.set_value(f"pods.{instance_id}.guard.mode", policy.mode)
    cfg.set_value(f"pods.{instance_id}.guard.idle_timeout_minutes", str(policy.idle_timeout_minutes))
    cfg.set_value(f"pods.{instance_id}.guard.poll_interval_seconds", str(policy.poll_interval_seconds))
    return policy


def disable_policy(instance_id: str, *, force_manual: bool = False) -> None:
    if force_manual:
        set_policy(instance_id, mode="manual", idle_timeout_minutes=0, poll_interval_seconds=60)
    else:
        cfg.delete(f"pods.{instance_id}.guard")


def _format_idle(seconds: float | int) -> str:
    total = int(seconds)
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}h {mins}m"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def _write_remote_script(session: RemoteSession) -> None:
    payload = base64.b64encode(_WATCHER_SOURCE.encode("utf-8")).decode("ascii")
    session.exec(
        f"mkdir -p '{_GUARD_DIR}' && "
        f"echo '{payload}' | base64 -d > '{_GUARD_SCRIPT}' && "
        f"chmod +x '{_GUARD_SCRIPT}'",
        stream=False,
    )


def remote_guard_running(session: RemoteSession) -> bool:
    _, out, _ = session.exec(
        f"test -f {_GUARD_PID} && kill -0 $(cat {_GUARD_PID}) 2>/dev/null && echo yes || echo no",
        stream=False,
    )
    return out.strip() == "yes"


def start_remote_guard(session: RemoteSession, policy: GuardPolicy) -> bool:
    from swm.sync import start_watcher

    start_watcher(session, "/workspace")
    _write_remote_script(session)
    stop_remote_guard(session)
    session.exec_background(
        f"python3 '{_GUARD_SCRIPT}' {policy.poll_interval_seconds} {_GPU_ACTIVE_THRESHOLD}",
        logfile=_GUARD_LOG,
        workdir="/workspace",
    )
    time.sleep(1)
    return remote_guard_running(session)


_STOP_GUARD_SCRIPT = "/tmp/.swm_stop_guard.sh"


def stop_remote_guard(session: RemoteSession) -> None:
    """Terminate the remote guard daemon and any zombies.

    Sends SIGTERM (via PID file + ``pkill -f``), waits up to ~3 s for
    graceful exit, then escalates to SIGKILL for any survivors. The
    ``-9`` fallback catches daemons that lost SIGTERM (overwritten PID
    file, blocked I/O, race-spawned siblings) so the next
    ``start_remote_guard`` is guaranteed to be the only watcher running.

    The kill logic is written to a tiny on-pod script and executed via
    ``bash /tmp/.swm_stop_guard.sh``. This is critical: ``pkill -f``
    inspects the full command line of every process, so passing the
    pattern inline (``ssh host "pkill -f .../watcher.py"``) would match
    our own controlling shell — killing it before the SIGKILL stage and
    leaving zombies alive. The script file's argv is just
    ``bash /tmp/.swm_stop_guard.sh`` which does not contain the watcher
    path, so the only matches are the actual watcher daemons.

    Safe even if ``status.json`` is mid-write: it uses an atomic
    ``rename``, so a SIGKILL leaves either the old or new file intact,
    never a partial one.
    """
    script = (
        "#!/bin/bash\n"
        f"test -f {_GUARD_PID} && kill $(cat {_GUARD_PID}) 2>/dev/null\n"
        f"pkill -f '{_GUARD_SCRIPT}' 2>/dev/null\n"
        "for _ in 1 2 3; do\n"
        f"  pgrep -f '{_GUARD_SCRIPT}' >/dev/null 2>&1 || break\n"
        "  sleep 1\n"
        "done\n"
        f"pkill -9 -f '{_GUARD_SCRIPT}' 2>/dev/null || true\n"
        f"rm -f {_GUARD_PID}\n"
    )
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    session.exec(
        f"echo '{payload}' | base64 -d > {_STOP_GUARD_SCRIPT} && "
        f"bash {_STOP_GUARD_SCRIPT}; "
        f"rm -f {_STOP_GUARD_SCRIPT}",
        stream=False,
    )


def read_remote_guard_status(session: RemoteSession) -> dict | None:
    _, out, _ = session.exec(
        f"test -f '{_GUARD_STATUS}' && cat '{_GUARD_STATUS}' || true",
        stream=False,
    )
    if not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def ensure_remote_guard(instance: Instance, policy: GuardPolicy) -> bool:
    if not policy.enabled or instance.status != InstanceStatus.RUNNING:
        return False
    with session_from_instance(instance) as sess:
        return start_remote_guard(sess, policy)


def _tracked_pods() -> list[tuple[str, dict]]:
    data = cfg.get("pods", {}) or {}
    if not isinstance(data, dict):
        return []
    return [(instance_id, meta) for instance_id, meta in data.items() if isinstance(meta, dict)]


def _guard_instance_ids(explicit_ids: tuple[str, ...]) -> list[str]:
    if explicit_ids:
        resolved: list[str] = []
        for instance_id in explicit_ids:
            _, raw_id = resolve_instance(instance_id)
            resolved.append(raw_id)
        return resolved
    result: list[str] = []
    for instance_id, _meta in _tracked_pods():
        if get_policy(instance_id).enabled:
            result.append(instance_id)
    return result


def evaluate_instance(instance_id: str) -> dict:
    meta = cfg.get(f"pods.{instance_id}", {}) or {}
    provider = meta.get("provider")
    if not provider:
        raise click.ClickException(f"Pod metadata for {instance_id} is missing provider info.")

    inst = _instance_for(f"{provider}:{instance_id}")
    policy = get_policy(instance_id)
    state = None
    remote_running = False
    if inst.status == InstanceStatus.RUNNING:
        with session_from_instance(inst) as sess:
            if policy.enabled and not remote_guard_running(sess):
                start_remote_guard(sess, policy)
            remote_running = remote_guard_running(sess)
            state = read_remote_guard_status(sess)
    return {
        "instance": inst,
        "policy": policy,
        "state": state,
        "remote_guard_running": remote_running,
    }


def _record_notice(instance_id: str) -> None:
    cfg.set_value(f"pods.{instance_id}.guard.last_notice_ts", str(int(time.time())))


def _notice_due(instance_id: str) -> bool:
    last = cfg.get(f"pods.{instance_id}.guard.last_notice_ts")
    try:
        return time.time() - float(last or 0) >= _REMINDER_COOLDOWN_SECONDS
    except (TypeError, ValueError):
        return True


def _auto_down(inst: Instance) -> None:
    meta = cfg.get(f"pods.{inst.id}", {}) or {}
    has_workspace = meta.get("workspace") and meta.get("storage")

    if has_workspace:
        from swm.bootstrap import workspace_push
        from swm.sync.paths import PUSH_STAMP

        slug, bucket = str(meta["storage"]).split(":", 1)
        workspace = str(meta["workspace"])
        with session_from_instance(inst) as sess:
            rc = workspace_push(sess, slug, bucket, workspace)
            # Defense in depth — raise if the push didn't actually
            # update the stamp. The guard catches the exception and
            # leaves the pod alive, which is the safe outcome. Without
            # this check, an exit-0-but-silently-incomplete push would
            # cause auto-down to terminate the pod and lose data.
            _, stamp_age, _ = sess.exec(
                f"find {PUSH_STAMP} -mmin -10 -print 2>/dev/null",
                stream=False,
            )
            if rc != 0 or not stamp_age.strip():
                why = (
                    f"s5cmd exit {rc}" if rc != 0
                    else "no recent push stamp"
                )
                raise RuntimeError(
                    f"auto-down refused: workspace push did not "
                    f"complete cleanly ({why}). Pod left running to "
                    f"prevent silent data loss; investigate with "
                    f"`swm pod status {inst.qualified_id}` and "
                    f"`swm sync status {inst.qualified_id}`."
                )

    provider, raw_id = resolve_instance(inst.qualified_id)
    provider.terminate_instance(raw_id)
    try:
        from swm.costs.tracker import record_stop

        record_stop(raw_id, provider.slug)
    except Exception:
        pass
    cfg.delete(f"pods.{raw_id}")


def _auto_stop(inst: Instance) -> None:
    provider, raw_id = resolve_instance(inst.qualified_id)
    provider.stop_instance(raw_id)
    try:
        from swm.costs.tracker import record_stop

        record_stop(raw_id, provider.slug)
    except Exception:
        pass


def run_guard_cycle(instance_ids: tuple[str, ...] = ()) -> list[str]:
    messages: list[str] = []
    for instance_id in _guard_instance_ids(instance_ids):
        policy = get_policy(instance_id)
        if not policy.enabled:
            continue

        try:
            result = evaluate_instance(instance_id)
        except Exception as exc:
            messages.append(f"[yellow]⚠ guard {instance_id}: {exc}[/yellow]")
            continue

        inst: Instance = result["instance"]
        state = result["state"] or {}
        if inst.status != InstanceStatus.RUNNING:
            messages.append(f"[dim]guard {inst.qualified_id}: {inst.status.value} — skipped[/dim]")
            continue

        idle_seconds = float(state.get("idle_seconds", 0))
        if idle_seconds < policy.idle_timeout_minutes * 60:
            messages.append(
                f"[dim]guard {inst.qualified_id}: idle { _format_idle(idle_seconds) } / "
                f"{policy.idle_timeout_minutes}m[/dim]"
            )
            continue

        if policy.mode == "remind":
            if _notice_due(instance_id):
                _record_notice(instance_id)
                cost = inst.cost_per_hr
                cost_str = f"${cost:.2f}/hr" if cost is not None else "unknown $/hr"
                messages.append(
                    f"[yellow]⚠ {inst.qualified_id} idle for {_format_idle(idle_seconds)} "
                    f"at {cost_str} — consider stop or down[/yellow]"
                )
            continue

        if policy.mode == "auto-stop":
            try:
                _auto_stop(inst)
                messages.append(
                    f"[green]✓ auto-stop {inst.qualified_id} after {_format_idle(idle_seconds)} idle[/green]"
                )
            except Exception as exc:
                messages.append(f"[yellow]⚠ auto-stop {inst.qualified_id} failed: {exc}[/yellow]")
            continue

        if policy.mode == "auto-down":
            try:
                _auto_down(inst)
                messages.append(
                    f"[green]✓ auto-down {inst.qualified_id} after {_format_idle(idle_seconds)} idle[/green]"
                )
            except Exception as exc:
                messages.append(f"[yellow]⚠ auto-down {inst.qualified_id} failed: {exc}[/yellow]")

    return messages


# ── Local background daemon ─────────────────────────────────────────

_LOCAL_PID_FILE = cfg.CONFIG_DIR / "guard.pid"
_LOCAL_LOG_FILE = cfg.CONFIG_DIR / "guard.log"


def _local_daemon_alive() -> bool:
    if not _LOCAL_PID_FILE.exists():
        return False
    try:
        pid = int(_LOCAL_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        _LOCAL_PID_FILE.unlink(missing_ok=True)
        return False


def ensure_local_daemon(interval: int = 60) -> bool:
    """Start a background ``swm guard run`` process if one isn't already running.

    Returns True if the daemon is confirmed running (started or already alive).
    """
    import shutil
    import subprocess

    if _local_daemon_alive():
        return True

    swm_bin = shutil.which("swm")
    if not swm_bin:
        return False

    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_LOCAL_LOG_FILE, "a") as log_fd:
        proc = subprocess.Popen(
            [swm_bin, "guard", "run", "--interval", str(interval)],
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
        )
    _LOCAL_PID_FILE.write_text(str(proc.pid))
    return True


def stop_local_daemon() -> bool:
    """Stop the background guard daemon if running. Returns True if it was stopped."""
    import signal

    if not _LOCAL_PID_FILE.exists():
        return False
    try:
        pid = int(_LOCAL_PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        _LOCAL_PID_FILE.unlink(missing_ok=True)
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        _LOCAL_PID_FILE.unlink(missing_ok=True)
        return False
