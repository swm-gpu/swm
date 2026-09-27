"""The resilient framework start (bootstrap_frameworks.ensure_framework_running).

A scripted fake pod plays each failure the ladder exists for: a step that
fails transiently, a launch that crashes on import or on a GPU mismatch, one
that never answers, one that runs out of memory. A fake clock makes the
readiness timeouts instant.
"""

from __future__ import annotations

import subprocess

import pytest

from swm import bootstrap_frameworks as bf
from swm.frameworks import get_framework, list_frameworks
from swm.frameworks._gpu import _PINS_SNIPPET, CUDA_INDEX_SNIPPET, torch_install


class FakePod:
    """Answers the runner's commands. ``launches`` scripts what each launch
    does: "ok" serves, "hang" runs without answering, anything else crashes
    with that text as the log."""

    host = "pod"

    def __init__(self, launches, *, installed=True, fail=None, running=False):
        self.launches = list(launches)
        self.installed = installed
        self.fail = dict(fail or {})
        self.running = running
        self.up = running
        self.log = ""
        self.commands: list[str] = []
        self.launched: list[str] = []

    def exec(self, command, stream=True, line_callback=None):
        self.commands.append(command)
        if "echo ALIVE" in command:
            state = ("ALIVE" if self.running else "GONE") + "\n"
            state += ("UP" if self.up else "DOWN") if "echo UP" in command else "NOPORT"
            return 0, state, ""
        if command.startswith("pkill") or "pkill -f" in command:
            self.running = self.up = False
            return 0, "", ""
        if command.startswith("tail -n"):
            return 0, self.log, ""
        if command.startswith("[ -d ") and "&& [ -x " in command and "echo" not in command:
            return (0 if self.installed else 1), "", ""
        if "mv " in command and "echo MOVED" in command:
            return 0, "MOVED\n", ""
        for needle, outcomes in self.fail.items():
            if needle in command and outcomes:
                code, out = outcomes.pop(0)
                return code, out, ""
        return 0, "", ""

    def exec_background(self, command, logfile="/dev/null", workdir=None, env_setup=""):
        self.launched.append(command)
        outcome = self.launches.pop(0) if self.launches else "ok"
        if outcome == "ok":
            self.running, self.up, self.log = True, True, ""
        elif outcome == "hang":
            self.running, self.up, self.log = True, False, "loading…"
        else:
            self.running, self.up, self.log = False, False, outcome

    def ran(self, needle: str) -> int:
        return sum(needle in c for c in self.commands)


class Recorder:
    def __init__(self):
        self.steps: list[str] = []

    def __call__(self, label):
        self.steps.append(label)


class QuietConsole:
    def print(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def fake_time(monkeypatch):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(bf, "_sleep", sleep)
    monkeypatch.setattr(bf, "_clock", lambda: now[0])
    return now


def _start(pod, name="comfyui", **kwargs):
    steps = Recorder()
    result = bf.ensure_framework_running(pod, name, console=QuietConsole(),
                                         on_step=steps, **kwargs)
    return result, steps


# ── the ladder ───────────────────────────────────────────────────────


def test_a_healthy_start_needs_no_repair():
    pod = FakePod(["ok"])
    result, steps = _start(pod)
    assert result.launched and result.repairs == []
    assert len(pod.launched) == 1
    assert not any(s.startswith("Repairing") for s in steps.steps)
    # Installed already, so only the start steps ran, not the install steps.
    assert "[1/6] Cloning ComfyUI" not in steps.steps


def test_install_always_runs_every_install_step():
    pod = FakePod(["ok"])
    _, steps = _start(pod, install="always")
    assert "[1/6] Cloning ComfyUI" in steps.steps


def test_a_missing_framework_is_installed_before_it_starts():
    pod = FakePod(["ok"], installed=False)
    _, steps = _start(pod)
    assert "[1/6] Cloning ComfyUI" in steps.steps


def test_a_transient_step_failure_is_retried_not_escalated():
    pod = FakePod(["ok"], fail={"-r requirements.txt": [
        (1, "error: Could not resolve host: pypi.org")]})
    result, steps = _start(pod)
    assert result.repairs == []
    assert pod.ran("-r requirements.txt") == 2
    assert not any(s.startswith("Repairing") for s in steps.steps)


def test_an_import_crash_is_fixed_by_the_targeted_repair():
    pod = FakePod(["ModuleNotFoundError: No module named 'aiohttp'", "ok"])
    result, steps = _start(pod)
    assert result.repairs == ["repaired (Python packages are missing or broken)"]
    assert "Installing custom-node requirements" in steps.steps
    # A package failure does not spend a PyTorch download on the GPU stack.
    assert "Reinstalling PyTorch for this GPU" not in steps.steps
    assert len(pod.launched) == 2


def test_a_gpu_crash_reinstalls_pytorch_for_this_gpu():
    pod = FakePod(["RuntimeError: CUDA error: no kernel image is available", "ok"])
    result, steps = _start(pod)
    assert result.repairs == ["repaired (PyTorch cannot use this GPU)"]
    assert "Reinstalling PyTorch for this GPU" in steps.steps
    assert pod.ran("--reinstall --index-url https://download.pytorch.org/whl") >= 1


def test_a_persistent_failure_rebuilds_the_environment(fake_time):
    crash = "ImportError: cannot import name 'x'"
    pod = FakePod([crash, crash, "ok"])
    result, steps = _start(pod)
    assert result.repairs[-1] == "rebuilt its Python environment"
    assert any(s.startswith("Rebuilding the ComfyUI environment") for s in steps.steps)
    assert pod.ran("mv /workspace/ComfyUI/venv /workspace/.cache/swm-rebuild/comfyui-venv") == 1
    assert pod.ran("rm -rf /workspace/.cache/swm-rebuild/comfyui-venv") >= 1
    # The rebuild's steps run under the time budget.
    assert pod.ran("timeout -k 30 ") >= 1


def test_a_failed_rebuild_restores_the_previous_environment():
    crash = "ImportError: cannot import name 'x'"
    pod = FakePod([crash, crash, crash])
    with pytest.raises(bf.FrameworkStartError) as exc:
        _start(pod)
    assert "even after rebuilding its environment" in exc.value.reason
    assert "Python packages are missing or broken" in exc.value.reason
    assert pod.ran("mv /workspace/.cache/swm-rebuild/comfyui-venv /workspace/ComfyUI/venv") == 1


def test_out_of_memory_stops_at_once_without_reinstalling():
    pod = FakePod(["torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2 GiB"])
    steps = Recorder()
    with pytest.raises(bf.FrameworkStartError) as exc:
        bf.ensure_framework_running(pod, "comfyui", console=QuietConsole(), on_step=steps)
    assert "ran out of memory" in exc.value.reason
    assert len(pod.launched) == 1
    assert not any(s.startswith(("Repairing", "Rebuilding", "Reinstalling"))
                   for s in steps.steps)


def test_a_framework_that_never_answers_is_a_failed_start():
    pod = FakePod(["hang", "ok"])
    result, _ = _start(pod)
    assert result.repairs, "a hung launch must not read as started"
    assert len(pod.launched) == 2


def test_a_crash_before_the_process_is_ever_seen_is_still_caught():
    pod = FakePod(["Segmentation fault", "Segmentation fault", "Segmentation fault"])
    with pytest.raises(bf.FrameworkStartError) as exc:
        _start(pod)
    assert "it exited or never answered" in exc.value.reason


def test_rebuild_can_be_disabled():
    crash = "ImportError: cannot import name 'x'"
    pod = FakePod([crash, crash])
    with pytest.raises(bf.FrameworkStartError) as exc:
        _start(pod, rebuild_budget=0)
    assert "automatic rebuild is off" in exc.value.reason
    assert pod.ran("echo MOVED") == 0


def test_an_answering_framework_is_left_running():
    pod = FakePod([], running=True)
    result, _ = _start(pod)
    assert result.launched is False
    assert pod.launched == []


def test_restart_relaunches_even_when_answering():
    pod = FakePod(["ok"], running=True)
    result, _ = _start(pod, restart=True)
    assert result.launched and len(pod.launched) == 1


def test_a_shell_driven_framework_is_prepared_never_launched():
    pod = FakePod([])
    result, _ = _start(pod, name="axolotl")
    assert result.launched is False
    assert pod.launched == []


def test_cancellation_from_on_step_propagates():
    class Cancelled(Exception):
        pass

    def cancel(label):
        raise Cancelled(label)

    with pytest.raises(Cancelled):
        bf.ensure_framework_running(FakePod(["ok"]), "comfyui",
                                    console=QuietConsole(), on_step=cancel)


def test_start_framework_keeps_its_old_contract():
    assert bf.start_framework(FakePod(["ok"]), "comfyui", console=QuietConsole()) is None
    with pytest.raises(RuntimeError):
        bf.start_framework(FakePod(["CUDA out of memory"]), "comfyui",
                           console=QuietConsole())


# ── classification ───────────────────────────────────────────────────


@pytest.mark.parametrize(("text", "kind"), [
    ("RuntimeError: CUDA error: out of memory", "memory"),
    ("Killed\n", "memory"),
    ("[Errno 98] Address already in use", "port"),
    ("CUDA driver version is insufficient for CUDA runtime version", "gpu"),
    ("ImportError: libcudart.so.12: cannot open shared object file", "gpu"),
    ("ModuleNotFoundError: No module named 'aiohttp'", "packages"),
    ("error: Failed to read metadata for: pydantic-settings==2.15.0", "packages"),
    ("the old process was killed and replaced", "unknown"),
    ("Segmentation fault", "unknown"),
])
def test_failures_are_classified(text, kind):
    assert bf.classify_failure(text) == kind


# ── every framework's shell is well formed ───────────────────────────


def _bash_parses(script: str) -> None:
    result = subprocess.run(["bash", "-n", "-c", script], capture_output=True,
                            text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("fw", list_frameworks(), ids=lambda fw: fw.name)
def test_every_framework_step_is_valid_bash(fw):
    for step in [*fw.steps, *fw.post_install, *fw.pre_start, *fw.repair]:
        _bash_parses(step.command)
        if step.check:
            _bash_parses(step.check)
    _bash_parses(fw.env_setup or ":")


@pytest.mark.parametrize("keep", [True, False])
@pytest.mark.parametrize("force", [True, False])
def test_torch_install_is_valid_bash(keep, force):
    _bash_parses(torch_install("/venv/bin/python", "uv pip install --python /venv/bin/python",
                               keep_version=keep, force=force))


def test_gpu_snippets_run_without_a_gpu():
    """Off a GPU host the index selector still answers (the cu128 default)
    and the pin reader prints nothing for an environment without torch."""
    import sys

    idx = subprocess.run([sys.executable, "-c", CUDA_INDEX_SNIPPET],
                         capture_output=True, text=True, check=True, timeout=20)
    assert idx.stdout.strip().startswith("cu")
    subprocess.run([sys.executable, "-c", _PINS_SNIPPET],
                   capture_output=True, text=True, check=True, timeout=20)
    assert "'" not in CUDA_INDEX_SNIPPET and "'" not in _PINS_SNIPPET


def test_every_gpu_framework_checks_pytorch_before_it_starts():
    for fw in list_frameworks():
        if not fw.gpu_torch:
            continue
        labels = [s.label for s in fw.pre_start]
        assert any("PyTorch" in label for label in labels), fw.name


def test_the_comfyui_definition_used_by_the_tests_is_unchanged():
    fw = get_framework("comfyui")
    assert len(fw.steps) + len(fw.post_install) == 6
