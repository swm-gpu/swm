"""A pod swm cannot reach says why, quickly, and without a traceback.

The case behind these tests: RunPod rented a B200 but never started its
container. RunPod still reported it RUNNING, so swm probed its
ssh.runpod.io relay, which refuses any command run without a PTY; each
command then retried for minutes and ended in a traceback, while the pod
billed.
"""

from __future__ import annotations

import os
import time

import pytest
from click.testing import CliRunner

from swm import bootstrap_ssh
from swm import config as cfg
from swm.cli import main
from swm.providers.base import InstanceStatus
from swm.providers.runpod import NOT_STARTED, SSH_RELAY_HOST, RunPodProvider
from swm.remote.ssh import RemoteSession, SSHUnavailableError

RELAY_REFUSAL = "Error: Your SSH client doesn't support PTY"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config" / "config.toml")


def _runpod_pod(runtime, status="RUNNING") -> dict:
    return {
        "id": "abc123", "name": "wan", "desiredStatus": status,
        "costPerHr": 6.79, "gpuCount": 1,
        "machine": {"podHostId": "abc123-64411f3b", "gpuDisplayName": "B200"},
        "runtime": runtime,
    }


_PUBLIC = {"uptimeInSeconds": 40, "ports": [
    {"ip": "1.2.3.4", "isIpPublic": True, "privatePort": 22, "publicPort": 40022, "type": "tcp"}]}
_NO_PORTS = {"uptimeInSeconds": 5, "ports": []}


# ── RunPod: a container that has not started is pending ────────────

def test_runpod_pod_without_a_container_is_pending_with_the_reason():
    inst = RunPodProvider()._to_instance(_runpod_pod(runtime=None))
    assert inst.status == InstanceStatus.PENDING
    assert inst.status_detail == NOT_STARTED
    assert inst.ip_address is None and inst.ssh_host == SSH_RELAY_HOST


def test_runpod_pod_with_a_container_is_running_with_its_public_port():
    inst = RunPodProvider()._to_instance(_runpod_pod(runtime=_PUBLIC))
    assert inst.status == InstanceStatus.RUNNING and inst.status_detail is None
    assert inst.ip_address == "1.2.3.4" and inst.ports == {22: 40022}


def test_runpod_stopped_pod_stays_stopped():
    inst = RunPodProvider()._to_instance(_runpod_pod(runtime=None, status="EXITED"))
    assert inst.status == InstanceStatus.STOPPED and inst.status_detail is None


# ── pod create's wait: pending and relay-only both end with the reason ──

class _Provider:
    slug = "runpod"
    name = "RunPod"

    def __init__(self, runtimes):
        self._runtimes = list(runtimes)
        self.polls = 0

    def get_instance(self, instance_id):
        runtime = self._runtimes[min(self.polls, len(self._runtimes) - 1)]
        self.polls += 1
        return RunPodProvider()._to_instance(_runpod_pod(runtime=runtime))


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(bootstrap_ssh.time, "sleep", lambda _s: None)


def _fake_probe(monkeypatch, answer):
    argvs: list[list[str]] = []

    class _Done:
        def __init__(self, stdout: bytes, stderr: bytes):
            self.stdout, self.stderr, self.returncode = stdout, stderr, 0

    def run(argv, **_):
        argvs.append(argv)
        return _Done(*answer(argv))

    monkeypatch.setattr(bootstrap_ssh.subprocess, "run", run)
    return argvs


def test_a_container_that_never_starts_times_out_saying_so(no_sleep):
    provider = _Provider([None])
    with pytest.raises(TimeoutError) as err:
        bootstrap_ssh.wait_for_ssh(provider, "abc123", timeout=0.2, poll_interval=0)
    assert f"Last status: pending ({NOT_STARTED})" in str(err.value)


def test_relay_refusal_switches_to_the_public_port_once_published(monkeypatch, no_sleep):
    provider = _Provider([_NO_PORTS, _NO_PORTS, _NO_PORTS, _PUBLIC])

    def answer(argv):
        if argv[-2].endswith("@1.2.3.4"):
            return b"__SWM_OK__\n", b""
        return b"", RELAY_REFUSAL.encode()

    argvs = _fake_probe(monkeypatch, answer)
    inst = bootstrap_ssh.wait_for_ssh(provider, "abc123", poll_interval=0,
                                      direct_grace=0, probe_timeout=5)
    assert inst.ip_address == "1.2.3.4" and inst.ports[22] == 40022
    assert argvs[0][-2] == f"abc123-64411f3b@{SSH_RELAY_HOST}"
    assert argvs[-1][-2] == "root@1.2.3.4" and "40022" in argvs[-1]


def test_relay_only_pod_fails_explaining_the_relay(monkeypatch, no_sleep):
    provider = _Provider([_NO_PORTS])
    _fake_probe(monkeypatch, lambda argv: (b"", RELAY_REFUSAL.encode()))
    with pytest.raises(TimeoutError) as err:
        bootstrap_ssh.wait_for_ssh(provider, "abc123", poll_interval=0,
                                   direct_grace=0, probe_timeout=0.2)
    msg = str(err.value)
    assert "accepts interactive shells only" in msg and "22/tcp" in msg


# ── RemoteSession.connect against a fake ssh ────────────────────────

@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """An ``ssh`` on PATH that logs each call and prints $FAKE_SSH_OUT."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "ssh"
    shim.write_text('#!/bin/bash\necho call >> "$FAKE_SSH_LOG"\n'
                    'printf "%b" "$FAKE_SSH_OUT" >&2\nexit "${FAKE_SSH_RC:-255}"\n')
    shim.chmod(0o755)
    log = tmp_path / "calls"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))

    def calls() -> int:
        return len(log.read_text().splitlines()) if log.exists() else 0

    return calls


def test_connect_stops_at_the_relay_refusal(fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_OUT", RELAY_REFUSAL + "\\r\\n")
    monkeypatch.setenv("FAKE_SSH_RC", "1")
    sess = RemoteSession(SSH_RELAY_HOST, user="abc123-64411f3b")
    started = time.monotonic()
    with pytest.raises(SSHUnavailableError) as err:
        sess.connect(retries=12, delay=10)
    assert time.monotonic() - started < 5
    assert fake_ssh() == 1
    assert "accepts interactive shells only" in str(err.value)


def test_connect_failure_names_the_last_ssh_error(fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_OUT", (
        "mux_client_request_session: session request failed: x\\r\\n"
        "ssh: connect to host 10.0.0.9 port 22: Connection refused\\r\\n"))
    sess = RemoteSession("10.0.0.9")
    with pytest.raises(SSHUnavailableError) as err:
        sess.connect(retries=2, delay=0)
    assert fake_ssh() == 2
    assert isinstance(err.value, RuntimeError)
    assert str(err.value) == ("SSH to root@10.0.0.9:22 failed after 2 attempts: "
                              "ssh: connect to host 10.0.0.9 port 22: Connection refused")


# ── The CLI: one error line and a non-zero exit, never a traceback ──

def test_setup_start_on_a_relay_only_pod_is_one_error_line(fake_ssh, monkeypatch):
    from swm.commands import setup as setup_cmd

    monkeypatch.setenv("FAKE_SSH_OUT", RELAY_REFUSAL + "\\r\\n")
    monkeypatch.setenv("FAKE_SSH_RC", "1")
    inst = RunPodProvider()._to_instance(_runpod_pod(runtime=_NO_PORTS))
    monkeypatch.setattr(setup_cmd, "_instance_for", lambda _id: inst)

    result = CliRunner().invoke(main, ["setup", "start", "comfyui", "runpod:abc123"])
    assert result.exit_code == 1, result.output
    assert "Error: abc123-64411f3b@ssh.runpod.io is an SSH relay" in result.output
    assert "Traceback" not in result.output
    assert fake_ssh() == 1


def _create(monkeypatch, wait):
    from swm import bootstrap
    from swm.commands import pod as pod_cmd
    from swm.costs import budget, tracker
    from swm.remote import ssh as ssh_mod

    # The cost database path is fixed at import, outside the isolated config.
    monkeypatch.setattr(tracker, "record_start", lambda *_a, **_k: None)
    monkeypatch.setattr(budget, "check_budget", lambda _slug: None)

    pending = RunPodProvider()._to_instance(_runpod_pod(runtime=None))

    class _Creating:
        slug, name = "runpod", "RunPod"

        def create_instance(self, _config):
            return pending

    monkeypatch.setattr(pod_cmd, "get_provider", lambda _slug: _Creating())
    monkeypatch.setattr(ssh_mod, "read_ssh_public_key", lambda: "ssh-ed25519 AAAA test")
    monkeypatch.setattr(bootstrap, "wait_for_ssh", wait)
    return CliRunner().invoke(main, ["pod", "create", "-p", "runpod", "-g", "b200",
                                     "-n", "wan", "--no-storage", "-y"])


def test_pod_create_exits_non_zero_when_ssh_never_comes_up(monkeypatch):
    def never(_provider, _iid):
        raise TimeoutError(f"Pod not running after 600s. Last status: pending ({NOT_STARTED}).")

    result = _create(monkeypatch, never)
    assert result.exit_code == 1, result.output
    text = " ".join(result.output.split())
    assert "BILLING" in text and NOT_STARTED in text
    assert "Traceback" not in text


def test_pod_create_exits_zero_when_ssh_is_ready(monkeypatch):
    ready = RunPodProvider()._to_instance(_runpod_pod(runtime=_PUBLIC))
    result = _create(monkeypatch, lambda _provider, _iid: ready)
    assert result.exit_code == 0, result.output
    assert "Pod ready" in result.output


def test_pod_status_shows_the_provider_detail(monkeypatch):
    from swm.commands import pod as pod_cmd

    pending = RunPodProvider()._to_instance(_runpod_pod(runtime=None))

    class _Listing:
        name = "RunPod"

        def list_instances(self):
            return [pending]

    monkeypatch.setattr(pod_cmd, "safe_resolve_instance", lambda _id: (_Listing(), "abc123"))
    result = CliRunner().invoke(main, ["pod", "status", "runpod:abc123"])
    assert result.exit_code == 0, result.output
    assert "pending" in result.output and f"Detail:     {NOT_STARTED}" in result.output
