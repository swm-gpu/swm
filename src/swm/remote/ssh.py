from __future__ import annotations

import re
import subprocess
import sys
import time
from typing import Callable

from swm import config as cfg
from swm.providers.base import Instance

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
]

def _sh_quote(s: str) -> str:
    """Shell-quote a string using $'...' syntax to handle all special chars."""
    return "'" + s.replace("'", "'\\''") + "'"


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
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except OSError:
                pass
            if attempt < retries - 1:
                time.sleep(delay)
        raise RuntimeError(
            f"SSH to {self.user}@{self.host}:{self.port} "
            f"failed after {retries} attempts"
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
        proc = subprocess.run(cmd)
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
        """Download a file from the remote via scp with compression."""
        cmd = self._scp_base() + ["-C"]
        cmd.extend([f"{self.user}@{self.host}:{remote_path}", local_path])
        proc = subprocess.run(cmd)
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

    def download_dir(
        self,
        remote_path: str,
        local_dir: str,
        progress_callback: "Callable[[str], None] | None" = None,
    ) -> None:
        """Stream a remote directory to *local_dir* via tar-over-SSH.

        Significantly faster than ``scp -r`` because it transfers a single
        compressed stream instead of one negotiated sub-channel per file.
        The tar archive is never written to disk on either side.

        *progress_callback* is called with each member name as it is extracted.
        """
        import os
        import tarfile

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
        ssh_cmd.append(f"tar czf - -C '{parent}' '{name}'")

        with subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            assert proc.stdout is not None
            try:
                with tarfile.open(fileobj=proc.stdout, mode="r|gz") as tf:
                    for member in tf:
                        tf.extract(member, local_dir)
                        if progress_callback:
                            progress_callback(member.name)
            except Exception as exc:
                proc.kill()
                stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                raise RuntimeError(
                    f"tar stream failed: {exc}"
                    + (f"\nSSH stderr: {stderr}" if stderr.strip() else "")
                ) from exc

            proc.wait()
            if proc.returncode not in (0, None):
                stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
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
