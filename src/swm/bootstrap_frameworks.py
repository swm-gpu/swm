"""Framework lifecycle management for remote GPU instances.

A workspace has to start on whatever pod it lands on: a different GPU, a
different driver, a venv restored from storage with pieces missing. Starting
a framework is therefore a ladder, each rung tried only when the one before
did not produce a framework that answers on its port:

1. install anything missing, run the start steps, launch, wait until ready,
   retrying a failed step that looks transient or failed quickly;
2. targeted repair: reinstall PyTorch for this GPU, run the framework's own
   repairs, rerun every step, relaunch;
3. rebuild the framework's venv from its steps, within a time budget,
   restoring the previous venv if the rebuild fails too.

A failure that no reinstall can fix (out of memory, a port held by another
process) stops the ladder at once. Whatever happens ends in a framework that
answers, or a FrameworkStartError whose reason says why in one sentence.
"""

from __future__ import annotations

import base64
import re
import shlex
import time
from dataclasses import dataclass, field

from rich.console import Console

from swm.bootstrap import WORKSPACE_UV, StepFailed, _step
from swm.redact import SafeConsole
from swm.remote.ssh import RemoteSession

console = SafeConsole()

# Indirection so tests can run the ladder without real waits.
_sleep = time.sleep
_clock = time.monotonic

REBUILD_BUDGET_SECONDS = 20 * 60
_STEP_RETRIES = 2
_RETRY_DELAYS = (5, 20)
# A step that fails this fast costs little to retry, whatever the cause.
_QUICK_FAILURE_SECONDS = 120
# A launch whose process never appears in this window has exited.
_NEVER_SEEN_SECONDS = 90
# Where a venv waits while its replacement is built. Under /workspace so the
# move is a rename; under .cache/ so autosync never uploads it.
_ASIDE_ROOT = "/workspace/.cache/swm-rebuild"

_TRANSIENT = re.compile(
    r"could not resolve host|temporary failure in name resolution"
    r"|name or service not known|network is unreachable"
    r"|connection (?:reset|refused|timed out|closed)|timed out"
    r"|failed to (?:download|fetch|connect)|request failed"
    r"|http(?: status)?(?: error)?:? 5\d\d|early eof|unexpected disconnect"
    r"|rpc failed|ssl error|sslerror|tls handshake",
    re.IGNORECASE,
)

_FAILURE_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # A bare "Killed" line is the shell reporting the kernel OOM killer.
    ("memory", re.compile(r"(?i:out of memory|OutOfMemoryError)|(?m:^Killed\s*$)")),
    ("port", re.compile(r"address already in use|EADDRINUSE", re.IGNORECASE)),
    ("gpu", re.compile(
        r"no kernel image|driver version is insufficient"
        r"|driver on your system is too old|NoKernelImageForDevice"
        r"|not compatible with the current PyTorch|CUDA error|CUDA driver"
        r"|undefined symbol|libcudart|libcublas|libcudnn|libnvrtc"
        r"|not compiled with CUDA|cannot use this GPU", re.IGNORECASE)),
    ("packages", re.compile(
        r"ModuleNotFoundError|No module named|ImportError|cannot import name"
        r"|Failed to read metadata|PackageNotFoundError|DistributionNotFound"
        r"|Missing expected Python executable", re.IGNORECASE)),
)

_KIND_TEXT = {
    "memory": "it ran out of memory",
    "port": "its port is already in use by another process",
    "gpu": "PyTorch cannot use this GPU",
    "packages": "Python packages are missing or broken",
    "unknown": "it exited or never answered",
}

# Failures a reinstall cannot fix: stop the ladder instead of spending the
# rebuild budget on them.
_TERMINAL_KINDS = frozenset({"memory", "port"})


def classify_failure(text: str) -> str:
    """The failure class of a step or launch log: memory | port | gpu |
    packages | unknown. Order matters: "CUDA error: out of memory" is a
    memory failure, not a GPU-stack one."""
    tail = text[-8000:]
    for kind, pattern in _FAILURE_KINDS:
        if pattern.search(tail):
            return kind
    return "unknown"


@dataclass
class FrameworkStart:
    """Outcome of ensure_framework_running."""

    # False when the framework was already running and answering.
    launched: bool
    # Repairs that ran, in order, in words fit for a status line.
    repairs: list[str] = field(default_factory=list)


class FrameworkStartError(RuntimeError):
    """A framework could not be made to run. ``reason`` is one sentence for a
    status line; ``log_tail`` is the evidence."""

    def __init__(self, reason: str, log_tail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.log_tail = log_tail


class _Failure(Exception):
    def __init__(self, what: str, output: str = "") -> None:
        super().__init__(what)
        self.what = what
        self.output = output


def _self_safe(pattern: str) -> str:
    """The same regex, rewritten so it cannot match the shell that runs the
    check (that shell's command line contains the pattern text)."""
    if pattern and pattern[0].isalnum():
        return f"[{pattern[0]}]{pattern[1:]}"
    return pattern


class _Runner:
    def __init__(self, session: RemoteSession, fw, con, on_step) -> None:
        self.session = session
        self.fw = fw
        self.con = con
        self.on_step = on_step
        self.env_prefix = f"{fw.env_setup} && " if fw.env_setup else ""
        self.logfile = f"/tmp/{fw.name}.log"

    # ── output ───────────────────────────────────────────────────────

    def announce(self, label: str) -> None:
        if self.on_step is not None:
            self.on_step(label)
        else:
            self.con.print(f"\n[bold cyan]▸ {label}[/bold cyan]")

    def note(self, text: str) -> None:
        self.con.print(f"  {text}")

    # ── steps ────────────────────────────────────────────────────────

    def _retryable(self, attempt: int, started: float, output: str,
                   deadline: float | None) -> bool:
        if attempt >= _STEP_RETRIES:
            return False
        if deadline is not None and _clock() >= deadline:
            return False
        quick = _clock() - started < _QUICK_FAILURE_SECONDS
        return quick or bool(_TRANSIENT.search(output[-4000:]))

    def run(self, label: str, command: str, *,
            deadline: float | None = None) -> str:
        for attempt in range(_STEP_RETRIES + 1):
            if deadline is not None:
                left = int(deadline - _clock())
                if left <= 0:
                    raise _Failure("the rebuild time budget ran out")
                command_now = f"timeout -k 30 {left} bash -c {shlex.quote(command)}"
            else:
                command_now = command
            started = _clock()
            code, out, _ = self.session.exec(command_now)
            if code == 0:
                return out
            if deadline is not None and code == 124:
                raise _Failure(f"{label} ran past the rebuild time budget", out)
            if self._retryable(attempt, started, out, deadline):
                delay = _RETRY_DELAYS[attempt]
                self.note(f"{label} failed (exit {code}); retrying in {delay}s")
                _sleep(delay)
                continue
            raise _Failure(f"{label} failed (exit {code})", out)
        raise AssertionError("unreachable")

    def step(self, label: str, step, *, deadline: float | None = None) -> None:
        workdir = step.workdir or self.fw.install_dir
        if step.check:
            cmd = (f"{step.check} && echo '{step.label}: already done' "
                   f"|| ({self.env_prefix}cd {workdir} && {step.command})")
        else:
            cmd = f"{self.env_prefix}cd {workdir} && {step.command}"
        self.announce(label)
        self.run(step.label, cmd, deadline=deadline)

    def call(self, fn, *args) -> None:
        """A bootstrap helper (``_step``-based), with the same retry rule."""
        for attempt in range(_STEP_RETRIES + 1):
            started = _clock()
            try:
                fn(self.session, *args)
                return
            except StepFailed as exc:
                if self._retryable(attempt, started, exc.output, None):
                    delay = _RETRY_DELAYS[attempt]
                    self.note(f"{exc.label} failed; retrying in {delay}s")
                    _sleep(delay)
                    continue
                raise _Failure(str(exc), exc.output) from exc

    def prepare(self, *, install: bool, deadline: float | None = None) -> None:
        from swm.bootstrap import ensure_workspace_python, repair_venv

        fw = self.fw
        if fw.venv:
            self.announce("Preparing workspace Python")
            self.call(ensure_workspace_python)
            self.call(repair_venv, fw.venv)
        if install:
            steps = [*fw.steps, *fw.post_install]
            for idx, step in enumerate(steps, 1):
                self.step(f"[{idx}/{len(steps)}] {step.label}", step, deadline=deadline)
        for step in fw.pre_start:
            self.step(step.label, step, deadline=deadline)

    def installed(self) -> bool:
        probe = self.fw.installed_probe
        if self.fw.venv:
            probe = f"{probe} && [ -x {self.fw.venv}/bin/python ]"
        code, _, _ = self.session.exec(probe, stream=False)
        return code == 0

    def targeted_repair(self, kind: str) -> None:
        from swm.frameworks._gpu import torch_install

        fw = self.fw
        if fw.gpu_torch and fw.venv and kind in ("gpu", "unknown"):
            python = f"{fw.venv}/bin/python"
            uv_pip = f"{WORKSPACE_UV} pip install --python {python}"
            self.announce("Reinstalling PyTorch for this GPU")
            fix = torch_install(python, uv_pip, keep_version=fw.gpu_torch == "keep",
                                force=True)
            # No venv yet: nothing to reinstall; the steps that follow build it.
            self.run("Reinstalling PyTorch for this GPU",
                     f"{self.env_prefix}[ ! -x {python} ] || {{ {fix}; }}")
        for step in fw.repair:
            self.step(step.label, step)

    # ── process ──────────────────────────────────────────────────────

    def _state(self, port: int | None) -> tuple[bool, bool]:
        """(process alive, port answering)."""
        fw = self.fw
        alive = (f"pgrep -f -- {shlex.quote(_self_safe(fw.process_pattern))} "
                 ">/dev/null 2>&1 && echo ALIVE || echo GONE"
                 if fw.process_pattern else "echo ALIVE")
        if port:
            answering = (f"(curl -s -o /dev/null --max-time 3 http://127.0.0.1:{port}/ "
                         f"|| bash -c ': >/dev/tcp/127.0.0.1/{port}') >/dev/null 2>&1 "
                         "&& echo UP || echo DOWN")
        else:
            answering = "echo NOPORT"
        _, out, _ = self.session.exec(f"{alive}; {answering}", stream=False)
        return "ALIVE" in out, "UP" in out

    def probe_port(self, port: int | None) -> int | None:
        if not self.fw.ready_timeout:
            return None
        return port or next(iter(self.fw.ports), None)

    def stop(self) -> None:
        fw = self.fw
        if fw.stop_cmd:
            self.session.exec(f"{fw.stop_cmd} >/dev/null 2>&1 || true", stream=False)
        if fw.process_pattern:
            self.session.exec(
                f"pkill -f -- {shlex.quote(_self_safe(fw.process_pattern))} || true",
                stream=False)
            for _ in range(10):
                if not self._state(None)[0]:
                    return
                _sleep(1)

    def launch(self, port: int | None, extra_args: str | None) -> None:
        fw = self.fw
        launch = fw.launch_cmd
        if port and fw.ports:
            launch = launch.replace(str(next(iter(fw.ports))), str(port))
        if extra_args:
            launch = f"{launch} {' '.join(shlex.quote(a) for a in shlex.split(extra_args))}"
        self.announce(f"Starting {fw.label}")
        self.session.exec_background(launch, logfile=self.logfile,
                                     workdir=fw.launch_workdir, env_setup=fw.env_setup)

    def log_tail(self) -> str:
        _, tail, _ = self.session.exec(f"tail -n 60 {self.logfile} 2>/dev/null",
                                       stream=False)
        return tail

    def wait_ready(self, port: int | None, *, deadline: float | None = None,
                   limit: int | None = None) -> _Failure | None:
        fw = self.fw
        probe = self.probe_port(port)
        if limit is None:
            limit = fw.ready_timeout if probe else 15
        start = _clock()
        end = start + limit if deadline is None else min(start + limit, deadline)
        seen = False
        while True:
            _sleep(3)
            alive, up = self._state(probe)
            now = _clock()
            # A framework may answer before its process-name pattern matches
            # (SwarmUI's launcher builds before exec'ing SwarmUI).
            if probe and up:
                return None
            if alive and not probe and now - start >= 8:
                return None
            if alive or up:
                seen = True
            elif seen or now - start >= min(_NEVER_SEEN_SECONDS, limit):
                return _Failure(f"{fw.label} exited", self.log_tail())
            if now >= end:
                what = (f"{fw.label} did not answer on port {probe} within {limit}s"
                        if probe else f"{fw.label} did not stay running")
                return _Failure(what, self.log_tail())

    # ── rungs ────────────────────────────────────────────────────────

    def attempt(self, port: int | None, extra_args: str | None, *,
                install: bool, repair: str | None = None,
                deadline: float | None = None,
                repair_after: bool = False) -> _Failure | None:
        try:
            if repair is not None:
                self.targeted_repair(repair)
            self.prepare(install=install, deadline=deadline)
            if repair_after:
                # A rebuilt venv has only what the install steps put in it;
                # custom nodes' dependencies lived in the old one.
                for step in self.fw.repair:
                    self.step(step.label, step, deadline=deadline)
            self.stop()
            self.launch(port, extra_args)
        except _Failure as failure:
            return failure
        return self.wait_ready(port, deadline=deadline)

    def rebuild(self, port: int | None, extra_args: str | None,
                budget: int) -> _Failure | None:
        fw = self.fw
        deadline = _clock() + budget
        self.announce(f"Rebuilding the {fw.label} environment "
                      f"(up to {max(budget // 60, 1)} min)")
        self.stop()
        aside = f"{_ASIDE_ROOT}/{fw.name}-venv"
        _, out, _ = self.session.exec(
            f"mkdir -p {_ASIDE_ROOT} && rm -rf {aside} && "
            f"if [ -d {fw.venv} ]; then mv {fw.venv} {aside} && echo MOVED; fi",
            stream=False)
        moved = "MOVED" in out
        failure = self.attempt(port, extra_args, install=True, deadline=deadline,
                               repair_after=True)
        if failure is None:
            self.session.exec(f"rm -rf {aside}", stream=False)
            return None
        self.stop()
        if moved:
            self.session.exec(f"rm -rf {fw.venv} && mv {aside} {fw.venv}", stream=False)
            self.note("The rebuild failed; restored the previous environment")
        return failure

    def started(self, port: int | None, qualified_id: str | None,
                repairs: list[str]) -> FrameworkStart:
        fw = self.fw
        self.con.print(f"  [green]✓ {fw.label} started[/green]")
        self.con.print(f"  Logs: swm run {qualified_id or '<pod>'} 'tail -f {self.logfile}'")
        _print_usage(self.con, fw, self.session.host, port)
        return FrameworkStart(launched=True, repairs=repairs)


def _failed(fw, failure: _Failure, *, after: str = "") -> FrameworkStartError:
    kind = classify_failure(failure.output)
    reason = f"{fw.label} could not start{after}: {_KIND_TEXT[kind]} ({failure.what})"
    return FrameworkStartError(reason, failure.output[-4000:])


def ensure_framework_running(
    session: RemoteSession,
    name: str,
    *,
    port: int | None = None,
    extra_args: str | None = None,
    console: Console | None = None,
    on_step=None,
    restart: bool = False,
    install: str = "missing",
    rebuild_budget: int = REBUILD_BUDGET_SECONDS,
    qualified_id: str | None = None,
) -> FrameworkStart:
    """Make framework *name* run and answer on its port, repairing as needed.

    *install* is "missing" (install steps run only when the framework or its
    venv is absent) or "always" (every install step runs; each skips itself
    when its check passes). *on_step* receives each step label, and may raise
    to cancel. *rebuild_budget* bounds the rebuild rung in seconds; 0
    disables it. Raises FrameworkStartError when nothing worked.
    """
    from swm.frameworks import get_framework

    fw = get_framework(name)
    runner = _Runner(session, fw, console or globals()["console"], on_step)

    if fw.access == "none":
        # Driven from a shell (Axolotl): there is no service to launch, and
        # its CLI exits at once without a config. Ready means prepared.
        try:
            runner.prepare(install=install == "always" or not runner.installed())
        except _Failure as failure:
            raise _failed(fw, failure) from None
        runner.con.print(f"  [green]✓ {fw.label} is ready to use over SSH[/green]")
        return FrameworkStart(launched=False)

    if not restart and runner._state(None)[0]:
        if runner.wait_ready(port, limit=min(fw.ready_timeout or 15, 120)) is None:
            runner.note(f"[yellow]{fw.label} is already running[/yellow]")
            return FrameworkStart(launched=False)
        runner.note(f"{fw.label} is running but not answering; restarting it")

    install_now = install == "always" or not runner.installed()
    failure = runner.attempt(port, extra_args, install=install_now)
    if failure is None:
        return runner.started(port, qualified_id, [])

    kind = classify_failure(failure.output)
    runner.note(f"{fw.label} did not start: {failure.what}. Likely cause: {_KIND_TEXT[kind]}")
    if kind in _TERMINAL_KINDS:
        runner.stop()
        raise _failed(fw, failure)

    runner.announce(f"Repairing {fw.label}")
    failure = runner.attempt(port, extra_args, install=True, repair=kind)
    if failure is None:
        return runner.started(port, qualified_id, [f"repaired ({_KIND_TEXT[kind]})"])

    kind = classify_failure(failure.output)
    if kind in _TERMINAL_KINDS or not fw.venv:
        runner.stop()
        raise _failed(fw, failure, after=" after a repair")
    if rebuild_budget <= 0:
        runner.stop()
        raise _failed(fw, failure, after=" after a repair (automatic rebuild is off)")

    failure = runner.rebuild(port, extra_args, rebuild_budget)
    if failure is None:
        return runner.started(port, qualified_id,
                              [f"repaired ({_KIND_TEXT[kind]})",
                               "rebuilt its Python environment"])
    raise _failed(fw, failure, after=" even after rebuilding its environment")


def install_framework(
    session: RemoteSession,
    name: str,
    console: Console | None = None,
) -> None:
    """Install a framework by name using its declarative step list, retrying
    steps that fail transiently."""
    from swm.frameworks import get_framework

    fw = get_framework(name)
    _con = console or globals()["console"]
    _con.print(f"\n[bold]Installing {fw.label}[/bold]")
    runner = _Runner(session, fw, _con, None)
    try:
        from swm.bootstrap import ensure_workspace_python, repair_venv

        if fw.venv:
            runner.call(ensure_workspace_python)
            runner.call(repair_venv, fw.venv)
        steps = [*fw.steps, *fw.post_install]
        for idx, step in enumerate(steps, 1):
            runner.step(f"[{idx}/{len(steps)}] {step.label}", step)
    except _Failure as failure:
        raise RuntimeError(f"Installing {fw.label} failed: {failure.what}") from None


def _print_usage(
    con: Console, fw, host: str, port: int | None,
) -> None:
    """Show how to talk to the framework that just started.

    Printing only a URL was a dead end for API frameworks: an Ollama root
    answers with a banner and nothing else, so the user was left at a page
    that says nothing about how to actually use it.
    """
    from swm.frameworks import render_usage

    fw_port = port or (next(iter(fw.ports)) if fw.ports else None)
    if fw.access == "ui" and fw_port:
        con.print(f"  Open: http://{host}:{fw_port}")
        return
    if not fw.usage:
        return
    base = f"http://{host}:{fw_port}" if fw_port else f"http://{host}"
    for u in render_usage(fw, base):
        con.print(f"  [bold]{u.label}[/bold]")
        if u.description:
            con.print(f"    [dim]{u.description}[/dim]")
        if u.command:
            con.print(f"    {u.command}")


def start_framework(
    session: RemoteSession,
    name: str,
    port: int | None = None,
    extra_args: str | None = None,
    console: Console | None = None,
    qualified_id: str | None = None,
) -> str | None:
    """Launch a framework in the background, repairing it if it will not
    start (see ensure_framework_running). Returns None."""
    ensure_framework_running(session, name, port=port, extra_args=extra_args,
                             console=console, qualified_id=qualified_id)
    return None


def stop_framework(
    session: RemoteSession,
    name: str,
    console: Console | None = None,
) -> None:
    """Stop a running framework."""
    from swm.frameworks import get_framework

    _con = console or globals()["console"]
    fw = get_framework(name)
    if not fw.stop_cmd:
        _con.print(f"  [yellow]{fw.label} has no stop command defined[/yellow]")
        return

    _con.print(f"\n[bold cyan]▸ Stopping {fw.label}[/bold cyan]")
    with _con.status(f"Stopping {fw.label}…", spinner="dots"):
        session.exec(fw.stop_cmd, stream=False)
    _con.print(f"  [green]✓ {fw.label} stopped[/green]")


# ── Cloudflare quick tunnel ─────────────────────────────────────────

# Outside /workspace so autosync never ships tunnel state to storage.
_TUNNEL_STATE_PREFIX = "/tmp/swm-tunnel-"
_TUNNEL_WAIT_SECONDS = 30
_TUNNEL_RESULT_RE = re.compile(r"SWM_TUNNEL_(STARTED|REUSED) (https://\S+)")


def _tunnel_script_header(name: str) -> str:
    pid_file = f"{_TUNNEL_STATE_PREFIX}{name}.pid"
    log_file = f"{_TUNNEL_STATE_PREFIX}{name}.log"
    return (
        f"PID_FILE={shlex.quote(pid_file)}\n"
        f"LOG_FILE={shlex.quote(log_file)}\n"
        # Check the recorded PID's own argv instead of `pkill -f`: a pattern
        # also matches the SSH shell running this script, and a PID file
        # left from before a container restart may now name any process.
        'ours() { [ -n "$1" ] && ps -o args= -p "$1" 2>/dev/null '
        '| grep -q -- "tunnel --no-autoupdate --url "; }\n'
        'pid=$(cat "$PID_FILE" 2>/dev/null)\n'
    )


def _quick_tunnel_open_script(name: str, port: int) -> str:
    target = f"http://127.0.0.1:{int(port)}"
    return _tunnel_script_header(name) + f"""TARGET={target}
url() {{ grep -oE 'https://[a-z0-9-]+\\.trycloudflare\\.com' "$LOG_FILE" 2>/dev/null | head -1; }}
if ours "$pid"; then
  if ps -o args= -p "$pid" | grep -q -- "--url $TARGET\\$" && [ -n "$(url)" ]; then
    echo "SWM_TUNNEL_REUSED $(url)"
    exit 0
  fi
  kill "$pid" 2>/dev/null
fi
rm -f "$PID_FILE" "$LOG_FILE"
CF=$(command -v cloudflared)
if [ -z "$CF" ]; then
  case "$(uname -m)" in
    x86_64|amd64) ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) echo "SWM_TUNNEL_ERROR unsupported CPU architecture: $(uname -m)"; exit 1 ;;
  esac
  CF=/usr/local/bin/cloudflared
  if ! curl -fsSL -o "$CF.part" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH"; then
    rm -f "$CF.part"
    echo "SWM_TUNNEL_ERROR could not download cloudflared for linux-$ARCH"
    exit 1
  fi
  chmod +x "$CF.part" && mv "$CF.part" "$CF"
fi
nohup bash -c 'echo $$ > "$0"; exec "$1" tunnel --no-autoupdate --url "$2"' \\
  "$PID_FILE" "$CF" "$TARGET" > "$LOG_FILE" 2>&1 < /dev/null &
for _ in $(seq {_TUNNEL_WAIT_SECONDS}); do
  sleep 1
  u=$(url)
  if [ -n "$u" ]; then echo "SWM_TUNNEL_STARTED $u"; exit 0; fi
  ours "$(cat "$PID_FILE" 2>/dev/null)" || break
done
echo "SWM_TUNNEL_ERROR no trycloudflare.com URL in $LOG_FILE"
tail -n 15 "$LOG_FILE" 2>/dev/null
exit 1
"""


def _quick_tunnel_close_script(name: str) -> str:
    return _tunnel_script_header(name) + """if ours "$pid"; then
  kill "$pid" 2>/dev/null && echo SWM_TUNNEL_CLOSED
fi
rm -f "$PID_FILE" "$LOG_FILE"
"""


def _run_tunnel_script(session: RemoteSession, script: str) -> str:
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    _, out, _ = session.exec(f"echo {payload} | base64 -d | bash", stream=False)
    return out


def open_quick_tunnel(session: RemoteSession, name: str, port: int) -> tuple[str, bool]:
    """Serve ``127.0.0.1:<port>`` on the pod through a Cloudflare quick tunnel.

    Returns ``(url, reused)``. A tunnel already running for *name* on the
    same port is reused; one on another port is replaced. Raises
    ``RuntimeError`` carrying the tunnel log tail when no URL comes up.
    """
    out = _run_tunnel_script(session, _quick_tunnel_open_script(name, port))
    m = _TUNNEL_RESULT_RE.search(out)
    if m:
        return m.group(2), m.group(1) == "REUSED"
    detail = out.replace("SWM_TUNNEL_ERROR ", "").strip()
    raise RuntimeError(detail or "cloudflared produced no output")


def close_quick_tunnel(session: RemoteSession, name: str) -> bool:
    """Stop the tunnel ``open_quick_tunnel`` started for *name*; True if one was running."""
    out = _run_tunnel_script(session, _quick_tunnel_close_script(name))
    return "SWM_TUNNEL_CLOSED" in out


# ── symlinks ────────────────────────────────────────────────────────


def link_models_to_comfyui(session: RemoteSession) -> None:
    """Symlink /workspace/models/<type> into ComfyUI's model directory.

    Handles every per-type bucket ComfyUI knows about, and safely migrates any
    files already sitting in ``/workspace/ComfyUI/models/<type>`` so existing
    pods aren't disturbed. The script is the same one ComfyUI's own install
    steps embed — one generator, not the third drifting copy this used to be.
    """
    from swm.frameworks._model_store import DIFFUSION_BUCKETS, link_store_script

    _step(
        session,
        "Symlinking models → ComfyUI",
        link_store_script("/workspace/ComfyUI/models", DIFFUSION_BUCKETS),
    )
