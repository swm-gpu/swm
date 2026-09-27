from __future__ import annotations

import base64
import re
import subprocess
import sys
import time
import uuid
from typing import Callable

from swm import config as cfg
from swm.providers.base import Instance

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    # Multiplex every ssh/scp we spawn over one TCP+SSH connection per host.
    # A download used to pay three full handshakes (probe, stat, transfer) —
    # 3–4 s each on a 360 ms RTT path. Falls back to a fresh connection when
    # the server refuses a second channel. Short path: sun_path is 104 bytes.
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=/tmp/swm-ssh-%C",
    "-o", "ControlPersist=60",
]

# Already-compressed formats: gzip on the pod only burns CPU (single-threaded
# gzip -6 caps ~60–100 MB/s) and measured ~30 % slower on a slow path.
_INCOMPRESSIBLE_EXT = frozenset({
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".mp3", ".flac", ".ogg", ".aac",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".heic",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".rar",
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx",
    ".npz", ".parquet", ".pdf",
})
# Extensions the remote `find` counts as incompressible (same set, as a grep
# alternation; keep in sync with _INCOMPRESSIBLE_EXT).
_INCOMPRESSIBLE_GREP = "|".join(sorted(e.lstrip(".") for e in _INCOMPRESSIBLE_EXT))


# Detached remote commands (RemoteSession.exec_detached). Outside /workspace
# so nothing here is ever synced.
_DETACHED_ROOT = "/tmp/swm-run"
_DETACHED_MARK = "@@SWM-DETACHED@@"
_DETACHED_LAUNCH_ATTEMPTS = 5
_DETACHED_POLL_MAX = 2.0
_DETACHED_LOST_AFTER = 600.0
# A launch whose process has not written its pid by now never started.
_DETACHED_START_WITHIN = 30.0
# Split after "\n", or after a bare "\r" (a progress-bar redraw); "\r\n" stays
# one line ending.
_LINE_ENDS = re.compile(r"(?<=\n)|(?<=\r)(?!\n)")

# What an interactive-only relay (RunPod's ssh.runpod.io) answers a command
# run without a PTY. Retrying cannot change it.
_RELAY_REFUSAL = re.compile(r"doesn.t support PTY", re.IGNORECASE)


class SSHUnavailableError(RuntimeError):
    """SSH to a pod could not be established; the message says why."""


def is_relay_refusal(output: str) -> bool:
    return bool(_RELAY_REFUSAL.search(output))


def relay_only_message(user: str, host: str) -> str:
    return (
        f"{user}@{host} is an SSH relay that accepts interactive shells "
        "only, so swm cannot run commands through it. swm needs the pod's "
        "public SSH port (22/tcp), which the provider has not exposed; the "
        "container may not have started yet (check `swm pod status`)."
    )


def worth_compressing(path: str) -> bool:
    return not path.lower().endswith(tuple(_INCOMPRESSIBLE_EXT))

def _sh_quote(s: str) -> str:
    """Shell-quote a string using $'...' syntax to handle all special chars."""
    return "'" + s.replace("'", "'\\''") + "'"


# How much of a spooled remote stderr to quote back in an error. A flooding
# remote can write megabytes of repeated warnings; the tail carries the
# failure that actually stopped the transfer.
_STDERR_TAIL_BYTES = 8192


def _stderr_tail(fileobj, limit: int = _STDERR_TAIL_BYTES) -> str:
    """Last *limit* bytes of a spooled stderr file, for an error message."""
    try:
        size = fileobj.seek(0, 2)
        fileobj.seek(max(0, size - limit))
        return fileobj.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


_ANSI_RE = re.compile(
    r"\x1b\[\??[0-9;]*[a-zA-Z]"
    r"|\x1b\][^\x07]*\x07"
    r"|\x07"
)

_START = "__SWM_S__"
_END = "__SWM_E_"


class RemoteSession:
    """SSH session backed by the system ``ssh`` binary.

    Uses stdin-piping with ``-tt`` and output markers so that it works
    through SSH relays (e.g. RunPod ``ssh.runpod.io``) that only support
    interactive shell channels.
    """

    def __init__(
        self,
        host: str,
        port: int = 22,
        user: str = "root",
        key_path: str | None = None,
        password: str | None = None,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.key_path = key_path
        self.password = password

    def _ssh_cmd(self) -> list[str]:
        cmd = ["ssh", "-tt", *_SSH_OPTS]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def connect(self, retries: int = 12, delay: int = 10) -> RemoteSession:
        """Verify SSH connectivity by running a probe command."""
        probe_cmd = ["ssh", *_SSH_OPTS]
        if self.key_path:
            probe_cmd.extend(["-i", self.key_path])
        if self.port != 22:
            probe_cmd.extend(["-p", str(self.port)])
        probe_cmd.append(f"{self.user}@{self.host}")
        probe_cmd.append("echo __SWM_OK__")

        last_error = ""
        for attempt in range(retries):
            try:
                proc = subprocess.Popen(
                    probe_cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                out, _ = proc.communicate(timeout=30)
                if b"__SWM_OK__" in out:
                    return self
                text = out.decode("utf-8", errors="replace")
                if is_relay_refusal(text):
                    raise SSHUnavailableError(relay_only_message(self.user, self.host))
                lines = [ln.strip() for ln in text.splitlines()
                         if ln.strip()]
                last_error = lines[-1][:200] if lines else f"exit {proc.returncode}"
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                last_error = "no response within 30s"
            except OSError as exc:
                last_error = str(exc)
            if attempt < retries - 1:
                time.sleep(delay)
        raise SSHUnavailableError(
            f"SSH to {self.user}@{self.host}:{self.port} "
            f"failed after {retries} attempts"
            + (f": {last_error}" if last_error else "")
        )

    def exec(
        self,
        command: str,
        stream: bool = True,
        line_callback: "Callable[[str], None] | None" = None,
    ) -> tuple[int, str, str]:
        """Run a command over SSH in non-interactive mode.

        If *line_callback* is provided it is called with each output line
        instead of writing to stdout (regardless of *stream*).
        """
        cmd = ["ssh", *_SSH_OPTS]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        cmd.append(f"{self.user}@{self.host}")
        cmd.append(command)

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        out_parts: list[str] = []

        assert proc.stdout is not None
        buf = b""
        while True:
            chunk = proc.stdout.read(1024)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf or b"\r" in buf:
                # Split on whichever delimiter comes first
                idx_n = buf.find(b"\n")
                idx_r = buf.find(b"\r")
                if idx_n == -1:
                    idx = idx_r
                elif idx_r == -1:
                    idx = idx_n
                else:
                    idx = min(idx_n, idx_r)
                raw_line = buf[: idx + 1]
                buf = buf[idx + 1:]
                # Skip bare \n after \r\n (already consumed)
                if raw_line == b"\n" and out_parts and out_parts[-1].endswith("\r\n"):
                    continue
                line = raw_line.decode("utf-8", errors="replace")
                out_parts.append(line)
                if line_callback:
                    line_callback(line)
                elif stream:
                    sys.stdout.write(line)
                    sys.stdout.flush()
        if buf:
            line = buf.decode("utf-8", errors="replace")
            out_parts.append(line)
            if line_callback:
                line_callback(line)
            elif stream:
                sys.stdout.write(line)
                sys.stdout.flush()

        exit_code = proc.wait()
        return exit_code, "".join(out_parts), ""

    def exec_detached(
        self,
        command: str,
        stream: bool = True,
        line_callback: "Callable[[str], None] | None" = None,
        *,
        lost_after: float = _DETACHED_LOST_AFTER,
    ) -> tuple[int, str, str]:
        """Run *command* so that it survives a dropped SSH connection, with
        the same streaming and return contract as :meth:`exec`.

        A long command on one SSH connection dies with it: some hosts' sshd
        (``ClientAliveCountMax 2``) drops a connection within ~25 s once
        other SSH sessions are active, and the command is killed at its next
        write (a truncated restore, a half-installed PyTorch). Here the command
        runs under ``setsid nohup`` with its output in a file on the pod,
        followed over short SSH calls that reconnect on a drop; the exit code
        comes from a status file. Returns 255 only when the pod stays
        unreachable for *lost_after* seconds (the command may still run).
        """
        run = f"{_DETACHED_ROOT}/{uuid.uuid4().hex[:16]}"
        script = base64.b64encode(command.encode()).decode()
        # mkdir is the once-only guard, so a retried launch whose first
        # attempt did start (its reply lost) never starts a second copy.
        launch = (
            f"mkdir -p {_DETACHED_ROOT} && if mkdir {run} 2>/dev/null; then "
            f"echo {script} | base64 -d > {run}/cmd.sh && : > {run}/out && "
            # setsid makes it a process group we can stop; without it (a
            # minimal image), nohup alone still detaches it from the session.
            f"SID=$(command -v setsid || true); "
            f"($SID nohup bash -c 'echo $$ > {run}/pid; "
            f"bash {run}/cmd.sh >> {run}/out 2>&1 < /dev/null; "
            f"echo $? > {run}/rc.tmp && mv {run}/rc.tmp {run}/rc' "
            f">/dev/null 2>&1 < /dev/null &); fi; echo {_DETACHED_MARK}"
        )
        for attempt in range(_DETACHED_LAUNCH_ATTEMPTS):
            code, out, _ = self.exec(launch, stream=False)
            if code == 0 and _DETACHED_MARK in out:
                break
            time.sleep(2 * (attempt + 1))
        else:
            return 255, "", ""

        emit = line_callback
        if emit is None and stream:
            def emit(line: str) -> None:
                sys.stdout.write(line)
                sys.stdout.flush()

        try:
            return self._follow_detached(run, emit, lost_after)
        except BaseException:
            # Interrupted here (Ctrl-C, a cancelled job): stop it there too,
            # or it would outlive the caller's locks and reporting.
            self.exec(f"kill -TERM -- -$(cat {run}/pid 2>/dev/null) 2>/dev/null; "
                      f"rm -rf {run}", stream=False)
            raise

    def _follow_detached(self, run: str, emit, lost_after: float) -> tuple[int, str, str]:
        offset = 0
        pending = ""
        collected: list[str] = []
        delay = 0.3
        unreachable_since: float | None = None
        launched_at = time.monotonic()
        # Status before size: once rc exists the output is complete, so the
        # size read after it is final and no trailing bytes are missed.
        while True:
            poll = (
                f"[ -d {run} ] || {{ echo '{_DETACHED_MARK} GONE'; exit 0; }}; "
                f"R=$(cat {run}/rc 2>/dev/null); "
                f"[ -e {run}/pid ] && P=1 || P=0; "
                f"S=$(wc -c < {run}/out 2>/dev/null | tr -d ' '); S=${{S:-0}}; "
                f"[ \"$S\" -gt {offset} ] && tail -c +{offset + 1} {run}/out | head -c $((S-{offset})); "
                f"printf '\\n{_DETACHED_MARK} %s %s %s\\n' \"$S\" \"$P\" \"$R\""
            )
            code, out, _ = self.exec(poll, stream=False)
            cut = out.rfind(f"{_DETACHED_MARK} ")
            if code != 0 or cut < 0:
                now = time.monotonic()
                unreachable_since = unreachable_since or now
                if now - unreachable_since > lost_after:
                    return 255, "".join(collected) + pending, ""
                time.sleep(min(delay * 2, 10))
                continue
            unreachable_since = None
            fields = out[cut + len(_DETACHED_MARK):].split()
            if fields and fields[0] == "GONE":
                return 255, "".join(collected) + pending, ""
            chunk = out[:cut].removesuffix("\n")
            if chunk:
                pending += chunk
                *lines, pending = _LINE_ENDS.split(pending)
                for line in lines:
                    collected.append(line)
                    if emit:
                        emit(line)
                delay = 0.3
            else:
                delay = min(delay * 1.6, _DETACHED_POLL_MAX)
            offset = int(fields[0]) if fields and fields[0].isdigit() else offset
            started = len(fields) > 1 and fields[1] == "1"
            if not started and time.monotonic() - launched_at > _DETACHED_START_WITHIN:
                self.exec(f"rm -rf {run}", stream=False)
                return 255, "the command did not start on the remote", ""
            if len(fields) > 2 and fields[2].isdigit():
                if pending:
                    collected.append(pending)
                    if emit:
                        emit(pending)
                self.exec(f"rm -rf {run}", stream=False)
                return int(fields[2]), "".join(collected), ""
            time.sleep(delay)

    def exec_pipe(
        self,
        command: str,
        line_callback: "Callable[[str], None] | None" = None,
    ) -> int:
        """Run a command via non-interactive SSH with clean stdout.

        Unlike :meth:`exec`, this does **not** allocate a PTY (no ``-tt``)
        and passes *command* as an SSH argument rather than via stdin.
        Stdout is a raw pipe — ideal for parsing structured output (JSON).
        """
        cmd = ["ssh", *_SSH_OPTS]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        cmd.append(f"{self.user}@{self.host}")
        cmd.append(command)

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert proc.stdout is not None
        while raw_line := proc.stdout.readline():
            line = raw_line.decode("utf-8", errors="replace")
            if line_callback:
                line_callback(line)
        proc.wait()
        return proc.returncode

    def exec_background(
        self,
        command: str,
        logfile: str = "/dev/null",
        workdir: str | None = None,
        env_setup: str = "",
    ) -> None:
        """Launch *command* in background on the remote host and return immediately.

        Uses ``setsid`` + ``nohup`` with full FD detachment so SSH exits
        without waiting for the child process.  *env_setup* (e.g.
        ``export PATH=...``) runs before ``nohup`` so environment is
        available to the launched process.
        """
        env = f"{env_setup} && " if env_setup else ""
        cd = f"cd {workdir} && " if workdir else ""
        wrapped = (
            f"{env}{cd}nohup bash -c {_sh_quote(command)} > {logfile} 2>&1 < /dev/null &"
        )
        cmd = ["ssh", *_SSH_OPTS]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        cmd.append(f"{self.user}@{self.host}")
        cmd.append(wrapped)

        try:
            subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            pass

    def _scp_base(self) -> list[str]:
        cmd = ["scp", *_SSH_OPTS]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if self.port != 22:
            cmd.extend(["-P", str(self.port)])
        return cmd

    def upload(
        self,
        local_path: str,
        remote_path: str,
        *,
        recursive: bool = False,
    ) -> None:
        """Upload a file or directory to the remote via scp."""
        cmd = self._scp_base()
        if recursive:
            cmd.append("-r")
        cmd.extend([local_path, f"{self.user}@{self.host}:{remote_path}"])
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise RuntimeError(f"scp upload failed (exit {proc.returncode})")

    def is_directory(self, remote_path: str) -> bool:
        """Return True if *remote_path* is a directory on the remote host."""
        _, out, _ = self.exec(f"test -d '{remote_path}' && echo yes || echo no", stream=False)
        return out.strip() == "yes"

    def download(
        self,
        remote_path: str,
        local_path: str,
        *,
        recursive: bool = False,
    ) -> None:
        """Download a file via scp, compressing only when the format allows."""
        cmd = self._scp_base()
        if worth_compressing(remote_path):
            cmd.append("-C")
        cmd.extend([f"{self.user}@{self.host}:{remote_path}", local_path])
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise RuntimeError(f"scp download failed (exit {proc.returncode})")

    def file_count(self, remote_path: str) -> int:
        """Return the number of regular files under *remote_path*."""
        _, out, _ = self.exec(
            f"find '{remote_path}' -type f | wc -l", stream=False
        )
        try:
            return int(out.strip())
        except ValueError:
            return 0

    def stat_path(self, remote_path: str) -> tuple[bool, int, bool]:
        """One round trip: (is_directory, regular-file count, compress?).

        ``compress`` is False when most of the first 500 files are formats
        that gzip cannot shrink (media, model weights, archives)."""
        q = _sh_quote(remote_path)
        _, out, _ = self.exec(
            f"if test -d {q}; then "
            f"n=$(find {q} -type f | wc -l); "
            f"i=$(find {q} -type f | head -500 | grep -Eic '\\.({_INCOMPRESSIBLE_GREP})$'); "
            f"echo DIR $n $i; else echo FILE; fi",
            stream=False,
        )
        parts = out.strip().split()
        if not parts or parts[0] != "DIR":
            return False, 0, worth_compressing(remote_path)
        try:
            total, incompressible = int(parts[1]), int(parts[2])
        except (IndexError, ValueError):
            return True, 0, True
        sampled = min(total, 500)
        return True, total, not (sampled and incompressible * 2 >= sampled)

    def download_dir(
        self,
        remote_path: str,
        local_dir: str,
        progress_callback: "Callable[[str], None] | None" = None,
        *,
        compress: bool = True,
    ) -> None:
        """Stream a remote directory to *local_dir* via tar-over-SSH.

        Significantly faster than ``scp -r`` because it transfers a single
        stream instead of one negotiated sub-channel per file. The tar
        archive is never written to disk on either side. ``compress=False``
        skips gzip for trees of already-compressed files.

        *progress_callback* is called with each member name as it is extracted.
        """
        import os
        import tarfile
        import tempfile

        os.makedirs(local_dir, exist_ok=True)

        # Build the non-interactive SSH command that streams tar to stdout.
        ssh_cmd = ["ssh", *_SSH_OPTS]
        if self.key_path:
            ssh_cmd.extend(["-i", self.key_path])
        if self.port != 22:
            ssh_cmd.extend(["-p", str(self.port)])
        ssh_cmd.append(f"{self.user}@{self.host}")
        # cd to parent so the archive contains only the leaf name, not the
        # full absolute path — this makes extraction predictable.
        parent = remote_path.rstrip("/").rsplit("/", 1)[0] or "/"
        name = remote_path.rstrip("/").rsplit("/", 1)[-1]
        flags = "czf" if compress else "cf"
        ssh_cmd.append(f"tar {flags} - -C {_sh_quote(parent)} {_sh_quote(name)}")

        # Remote stderr is spooled to a file, never a pipe. Remote tar warns
        # per entry on a live workspace (files changing under it, sockets,
        # unreadable paths); once an undrained stderr pipe fills, the remote
        # blocks on it, stops producing stdout, and the extract below waits on
        # that forever — a hang indistinguishable from a slow transfer.
        with tempfile.TemporaryFile() as errfile, subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=errfile,
        ) as proc:
            assert proc.stdout is not None
            try:
                with tarfile.open(fileobj=proc.stdout, mode="r|*") as tf:
                    for member in tf:
                        tf.extract(member, local_dir)
                        if progress_callback:
                            progress_callback(member.name)
            except Exception as exc:
                proc.kill()
                stderr = _stderr_tail(errfile)
                raise RuntimeError(
                    f"tar stream failed: {exc}"
                    + (f"\nSSH stderr: {stderr}" if stderr.strip() else "")
                ) from exc

            proc.wait()
            if proc.returncode not in (0, None):
                stderr = _stderr_tail(errfile)
                raise RuntimeError(
                    f"SSH tar exited {proc.returncode}"
                    + (f": {stderr.strip()}" if stderr.strip() else "")
                )

    def close(self) -> None:
        pass

    def __enter__(self) -> RemoteSession:
        return self.connect()

    def __exit__(self, *_: object) -> None:
        self.close()


def read_ssh_public_key() -> str:
    """Read the local SSH public key for injection into pod environments.

    Checks ``ssh.key_path`` in swm config first, then standard locations.
    """
    from pathlib import Path

    custom = cfg.get("ssh.key_path")
    if custom:
        p = Path(str(custom)).expanduser()
        pub = p if p.name.endswith(".pub") else p.parent / (p.name + ".pub")
        if pub.exists():
            return pub.read_text().strip()

    for name in ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"):
        p = Path.home() / ".ssh" / name
        if p.exists():
            return p.read_text().strip()

    raise FileNotFoundError(
        "No SSH public key found. Generate one with:\n"
        "  ssh-keygen -t ed25519\n"
        "Or set a custom path: swm config set ssh.key_path <path>"
    )


def _ssh_config_for(instance: Instance) -> dict:
    key_path = cfg.get(f"{instance.provider}.ssh_key") or cfg.get("ssh.key_path")
    key_str = str(key_path) if key_path else None

    if instance.ip_address and instance.ports.get(22):
        user = str(cfg.get(f"{instance.provider}.ssh_user", "root"))
        return {
            "host": instance.ip_address,
            "port": instance.ports[22],
            "user": user,
            "key_path": key_str,
        }

    user = instance.ssh_user or str(cfg.get(f"{instance.provider}.ssh_user", "root"))
    return {
        "host": instance.ssh_host,
        "port": instance.ssh_port or 22,
        "user": user,
        "key_path": key_str,
    }


def session_from_instance(instance: Instance) -> RemoteSession:
    """Build a RemoteSession from a provider Instance."""
    if not instance.ssh_host:
        raise RuntimeError(
            f"Instance {instance.qualified_id} has no SSH endpoint. "
            "It may still be starting up — try again in a moment."
        )
    c = _ssh_config_for(instance)
    return RemoteSession(
        host=c["host"], port=c["port"], user=c["user"], key_path=c["key_path"]
    )


def interactive_ssh(instance: Instance) -> int:
    """Open an interactive SSH session via the system ``ssh`` binary."""
    if not instance.ssh_host:
        raise RuntimeError(
            f"Instance {instance.qualified_id} has no SSH endpoint. "
            "It may still be starting up — try again in a moment."
        )
    c = _ssh_config_for(instance)

    cmd = ["ssh"]
    if c["key_path"]:
        cmd.extend(["-i", c["key_path"]])
    cmd.extend(["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"])
    if c["port"] != 22:
        cmd.extend(["-p", str(c["port"])])
    cmd.append(f"{c['user']}@{c['host']}")

    return subprocess.call(cmd)
