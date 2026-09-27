"""Tests for the Cloudflare quick-tunnel scripts (bootstrap_frameworks).

The open/close scripts run on pods over SSH; these tests run them in a local
bash against a fake ``cloudflared`` on PATH that prints a trycloudflare.com
URL for its target port and then idles, so PID tracking, reuse, replacement
and close are exercised without a network.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

import swm.bootstrap_frameworks as bf

_FAKE_CLOUDFLARED = """#!/bin/bash
if [ -n "${SWM_FAKE_CF_FAIL:-}" ]; then
  echo "ERR failed to request quick Tunnel: fake failure"
  exit 1
fi
target="${@: -1}"
echo "INF |  https://fake-${target##*:}-$$.trycloudflare.com  |"
trap 'exit 0' TERM
while :; do sleep 0.2; done
"""


@pytest.fixture
def tunnel_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    fake = shim_dir / "cloudflared"
    fake.write_text(_FAKE_CLOUDFLARED)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    monkeypatch.delenv("SWM_FAKE_CF_FAIL", raising=False)
    monkeypatch.setattr(bf, "_TUNNEL_STATE_PREFIX", f"{tmp_path}/swm-tunnel-")
    yield tmp_path
    for pid_file in tmp_path.glob("swm-tunnel-*.pid"):
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)
        except (ValueError, ProcessLookupError):
            pass


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        timeout=60, check=False,
    )


def _open(name: str = "comfyui", port: int = 8188) -> subprocess.CompletedProcess:
    return _run(bf._quick_tunnel_open_script(name, port))


def _result(proc: subprocess.CompletedProcess) -> tuple[str, str]:
    m = bf._TUNNEL_RESULT_RE.search(proc.stdout)
    assert m, proc.stdout + proc.stderr
    return m.group(1), m.group(2)


def _pid(state: Path, name: str = "comfyui") -> int:
    return int((state / f"swm-tunnel-{name}.pid").read_text().strip())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_open_starts_tunnel_and_records_pid(tunnel_state):
    proc = _open()

    assert proc.returncode == 0, proc.stdout
    kind, url = _result(proc)
    assert kind == "STARTED"
    assert url.startswith("https://fake-8188-")
    assert _alive(_pid(tunnel_state))


def test_reopen_on_same_port_reuses_the_running_tunnel(tunnel_state):
    _, first = _result(_open())
    pid = _pid(tunnel_state)

    assert _result(_open()) == ("REUSED", first)
    assert _pid(tunnel_state) == pid


def test_port_change_replaces_the_tunnel(tunnel_state):
    _open(port=8188)
    old = _pid(tunnel_state)

    kind, url = _result(_open(port=8288))

    assert kind == "STARTED"
    assert url.startswith("https://fake-8288-")
    assert _wait_dead(old)
    assert _pid(tunnel_state) != old


def test_close_stops_the_tunnel_and_clears_state(tunnel_state):
    _open()
    pid = _pid(tunnel_state)

    proc = _run(bf._quick_tunnel_close_script("comfyui"))

    assert "SWM_TUNNEL_CLOSED" in proc.stdout
    assert _wait_dead(pid)
    assert not list(tunnel_state.glob("swm-tunnel-comfyui.*"))


def test_stale_pid_naming_another_process_is_never_killed(tunnel_state):
    """A PID file left from before a container restart can name anything."""
    bystander = subprocess.Popen(["sleep", "30"])
    try:
        (tunnel_state / "swm-tunnel-comfyui.pid").write_text(f"{bystander.pid}\n")

        closed = _run(bf._quick_tunnel_close_script("comfyui"))
        assert "SWM_TUNNEL_CLOSED" not in closed.stdout

        (tunnel_state / "swm-tunnel-comfyui.pid").write_text(f"{bystander.pid}\n")
        kind, _ = _result(_open())
        assert kind == "STARTED"

        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_failed_start_reports_the_log_without_waiting_out_the_timeout(
    tunnel_state, monkeypatch,
):
    monkeypatch.setenv("SWM_FAKE_CF_FAIL", "1")
    started = time.monotonic()

    proc = _open()

    assert proc.returncode == 1
    assert "SWM_TUNNEL_ERROR" in proc.stdout
    assert "fake failure" in proc.stdout
    assert time.monotonic() - started < 10


def test_open_and_close_through_a_session(tunnel_state, session):
    url, reused = bf.open_quick_tunnel(session, "comfyui", 8188)

    assert url.startswith("https://fake-8188-") and not reused
    assert bf.open_quick_tunnel(session, "comfyui", 8188) == (url, True)
    assert bf.close_quick_tunnel(session, "comfyui") is True
    assert bf.close_quick_tunnel(session, "comfyui") is False


def test_open_raises_with_the_log_tail(tunnel_state, session, monkeypatch):
    monkeypatch.setenv("SWM_FAKE_CF_FAIL", "1")

    with pytest.raises(RuntimeError, match="fake failure"):
        bf.open_quick_tunnel(session, "comfyui", 8188)
