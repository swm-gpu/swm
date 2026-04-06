"""Bootstrap scripts for setting up remote GPU instances with storage and tools."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

from rich.console import Console

from swm.providers.base import Instance, InstanceStatus
from swm.remote.ssh import RemoteSession

console = Console()

S5CMD_VERSION = "2.3.0"
S5CMD_URL = (
    f"https://github.com/peak/s5cmd/releases/download/v{S5CMD_VERSION}/"
    f"s5cmd_{S5CMD_VERSION}_Linux-64bit.tar.gz"
)

SAFETY_MARGIN = 0.90


def _humanize(n: int | float) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024:
            return f"{v:.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v:.1f} PB"


def _step(session: RemoteSession, label: str, command: str) -> tuple[int, str, str]:
    """Run a labelled step on the remote, streaming output to the terminal."""
    console.print(f"\n[bold cyan]▸ {label}[/bold cyan]")
    code, stdout, stderr = session.exec(command, stream=True)
    if code != 0:
        raise RuntimeError(f"Step failed (exit {code}): {label}")
    return code, stdout, stderr


# ── s5cmd ──────────────────────────────────────────────────────────


def _s3_env(storage_slug: str) -> str:
    """Build env-var prefix for s5cmd from swm config."""
    from swm import config as cfg

    if storage_slug == "b2":
        endpoint = cfg.get("b2.s3_endpoint") or ""
        ak = cfg.get("b2.key_id") or ""
        sk = cfg.get("b2.app_key") or ""
    elif storage_slug == "gcs":
        endpoint = "https://storage.googleapis.com"
        ak = cfg.get("gcs.hmac_access") or ""
        sk = cfg.get("gcs.hmac_secret") or ""
    elif storage_slug == "s3":
        endpoint = ""
        ak = cfg.get("s3.access_key") or ""
        sk = cfg.get("s3.secret_key") or ""
    else:
        raise ValueError(f"Unknown storage slug: {storage_slug}")

    parts = [
        f"AWS_ACCESS_KEY_ID='{ak}'",
        f"AWS_SECRET_ACCESS_KEY='{sk}'",
    ]
    if endpoint:
        parts.append(f"S3_ENDPOINT_URL='{endpoint}'")
    return " ".join(parts)


def install_s5cmd(session: RemoteSession) -> None:
    """Download the s5cmd static binary if not already present."""
    _step(
        session,
        "Installing s5cmd",
        f"command -v s5cmd >/dev/null 2>&1 && echo 's5cmd already installed' || "
        f"(curl -sL '{S5CMD_URL}' | tar xz -C /usr/local/bin s5cmd "
        f"&& chmod +x /usr/local/bin/s5cmd && s5cmd version)",
    )


def configure_storage(
    session: RemoteSession, storage_slug: str, bucket: str = "",
) -> None:
    """Install s5cmd and verify the S3-compatible connection."""
    install_s5cmd(session)
    env = _s3_env(storage_slug)
    target = f"s3://{bucket}/" if bucket else ""
    _step(
        session,
        f"Verifying {storage_slug} connection",
        f"{env} s5cmd ls {target} 2>&1 | head -5 || true",
    )


_LOCK_FILE = "/tmp/.swm_transfer.lock"


def _acquire_transfer_lock(session: RemoteSession, force: bool = False) -> None:
    """Check for an existing transfer and acquire the lock.

    If a lock exists with a live PID, raises unless *force* is True
    (which kills the stale process first).
    """
    code, out, _ = session.exec(
        f"cat {_LOCK_FILE} 2>/dev/null", stream=False,
    )
    old_pid = out.strip()

    if old_pid:
        _, alive, _ = session.exec(
            f"kill -0 {old_pid} 2>/dev/null && echo alive || echo dead",
            stream=False,
        )
        if "alive" in alive:
            if not force:
                raise RuntimeError(
                    f"A transfer is already running (PID {old_pid}). "
                    "Use --force to kill it and start a new one."
                )
            console.print(
                f"  [yellow]⚠ Killing existing transfer (PID {old_pid})[/yellow]"
            )
            session.exec(f"kill -9 {old_pid} 2>/dev/null; sleep 1", stream=False)

        # Stale lock — clean up temp files left behind
        console.print("  [dim]Cleaning up stale temp files…[/dim]")
        _, cleanup_out, _ = session.exec(
            "find /workspace -maxdepth 5 -type f -regex '.*\\.[a-z]*[0-9]\\{9,\\}$' "
            "-delete -print 2>/dev/null | wc -l",
            stream=False,
        )
        n = cleanup_out.strip()
        if n and n != "0":
            console.print(f"  [dim]Removed {n} orphaned temp files[/dim]")

    session.exec(f"echo $$ > {_LOCK_FILE}", stream=False)


def _s5cmd_transfer(
    session: RemoteSession,
    label: str,
    s5cmd_cmd: str,
    force: bool = False,
    total_bytes: int = 0,
    total_files: int = 0,
) -> None:
    """Run an s5cmd transfer with output streamed directly to the terminal.

    Acquires a lock file on the pod, wraps the command in a shell trap
    for guaranteed cleanup (even on SSH disconnect), and streams
    s5cmd's native ``--show-progress`` output to the terminal.
    """
    console.print(f"\n[bold cyan]▸ {label}[/bold cyan]")
    _acquire_transfer_lock(session, force=force)

    wrapped = (
        f"trap 'rm -f {_LOCK_FILE}' EXIT; "
        f"echo $$ > {_LOCK_FILE}; "
        f"{s5cmd_cmd}"
    )
    cmd = session._ssh_cmd() + [wrapped]
    code = subprocess.call(cmd)

    if code != 0:
        console.print(f"  [yellow]⚠ Transfer finished with warnings (exit {code})[/yellow]")
    else:
        console.print(f"  [green]✓ {label} — done[/green]")


# ── disk pre-flight ─────────────────────────────────────────────────


@dataclass
class DiskCheck:
    """Result of a workspace-vs-disk size comparison."""

    workspace_bytes: int = 0
    available_bytes: int = 0
    dir_sizes: dict[str, int] = field(default_factory=dict)

    @property
    def fits(self) -> bool:
        return self.workspace_bytes <= int(self.available_bytes * SAFETY_MARGIN)

    @property
    def overshoot(self) -> int:
        limit = int(self.available_bytes * SAFETY_MARGIN)
        return max(0, self.workspace_bytes - limit)


def _workspace_info_s3(
    storage_slug: str, bucket: str, workspace: str,
) -> tuple[int, int, dict[str, int]]:
    """Return (total_bytes, file_count, dir_sizes) via S3 ListObjectsV2.

    Uses the storage provider's cached boto3 S3 client.
    """
    from swm.storage import get_storage

    provider = get_storage(storage_slug)
    client = provider.s3
    paginator = client.get_paginator("list_objects_v2")
    prefix = f"{workspace}/"
    total = 0
    count = 0
    dir_sizes: dict[str, int] = {}
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            sz = obj["Size"]
            total += sz
            count += 1
            rel = obj["Key"][len(prefix):]
            top = rel.split("/", 1)[0] if "/" in rel else ""
            if top:
                dir_sizes[top] = dir_sizes.get(top, 0) + sz
    return total, count, dir_sizes


def preflight_check(
    storage_slug: str,
    bucket: str,
    workspace: str,
    volume_gb: int,
) -> DiskCheck:
    """Check whether a workspace fits on the pod's disk.

    Runs entirely locally — queries the bucket via S3-compatible API
    and uses *volume_gb* from the provider API.  No SSH required.
    """
    console.print(f"\n[bold cyan]▸ Checking workspace size (local)[/bold cyan]")

    avail = int(volume_gb) * 1_073_741_824

    try:
        total, count, dir_sizes = _workspace_info_s3(storage_slug, bucket, workspace)
    except Exception as exc:
        console.print(f"  [yellow]⚠ Could not query bucket: {exc}[/yellow]")
        total, count, dir_sizes = 0, 0, {}

    console.print(
        f"  Workspace: [bold]{_humanize(total)}[/bold] ({count:,} files)  "
        f"Volume: [bold]{_humanize(avail)}[/bold]"
    )

    check = DiskCheck(workspace_bytes=total, available_bytes=avail)

    if check.fits:
        console.print("  [green]✓ Fits on disk[/green]")
        return check

    console.print(f"  [yellow]⚠ Workspace exceeds usable disk by {_humanize(check.overshoot)}[/yellow]")
    check.dir_sizes = dir_sizes

    return check


# ── framework installer ─────────────────────────────────────────────


def install_framework(
    session: RemoteSession,
    name: str,
    console: Console | None = None,
) -> None:
    """Install a framework by name using its declarative step list."""
    from swm.frameworks import Framework, get_framework

    _con = console or globals()["console"]

    fw: Framework = get_framework(name)
    _con.print(f"\n[bold]Installing {fw.label}[/bold]")

    total = len(fw.steps) + len(fw.post_install)
    for idx, step in enumerate(fw.steps, 1):
        workdir = step.workdir or fw.install_dir
        if step.check:
            cmd = f"{step.check} && echo '{step.label}: already done' || (cd {workdir} && {step.command})"
        else:
            cmd = f"cd {workdir} && {step.command}"
        _step(session, f"[{idx}/{total}] {step.label}", cmd)

    for idx, step in enumerate(fw.post_install, len(fw.steps) + 1):
        workdir = step.workdir or fw.install_dir
        if step.check:
            cmd = f"{step.check} && echo '{step.label}: already done' || (cd {workdir} && {step.command})"
        else:
            cmd = f"cd {workdir} && {step.command}"
        _step(session, f"[{idx}/{total}] {step.label}", cmd)


def start_framework(
    session: RemoteSession,
    name: str,
    port: int | None = None,
    console: Console | None = None,
) -> str | None:
    """Launch a framework in the background. Returns proxy URL if applicable."""
    from swm.frameworks import get_framework

    _con = console or globals()["console"]
    fw = get_framework(name)

    if fw.process_pattern:
        with _con.status(f"Checking if {fw.label} is running…", spinner="dots"):
            _, out, _ = session.exec(
                f"pgrep -fa '{fw.process_pattern}' | grep -v grep || true",
                stream=False,
            )
        if out.strip():
            pid = out.strip().split("\n")[0].split()[0]
            _con.print(
                f"  [yellow]{fw.label} is already running (PID {pid})[/yellow]"
            )
            return None

    _con.print(f"\n[bold cyan]▸ Starting {fw.label}[/bold cyan]")
    launch = fw.launch_cmd
    if port and fw.ports:
        default_port = str(next(iter(fw.ports)))
        launch = launch.replace(default_port, str(port))

    logfile = f"/tmp/{fw.name}.log"

    with _con.status(f"Launching {fw.label}…", spinner="dots"):
        session.exec_background(
            launch,
            logfile=logfile,
            workdir=fw.launch_workdir,
        )

    # Verify the process actually stayed alive.
    max_checks = 5
    alive = False
    for i in range(max_checks):
        time.sleep(3 if i == 0 else 2)
        if not fw.process_pattern:
            alive = True
            break
        _, out, _ = session.exec(
            f"pgrep -fa '{fw.process_pattern}' | grep -v grep || true",
            stream=False,
        )
        if out.strip():
            alive = True
            break

    if alive:
        _con.print(f"  [green]✓ {fw.label} started[/green]")
        _con.print(f"  Logs: swm run <pod> 'tail -f {logfile}'")
    else:
        _con.print(f"  [red]✗ {fw.label} failed to start[/red]")
        _con.print(f"  Last lines from {logfile}:")
        _, tail, _ = session.exec(f"tail -15 {logfile} 2>/dev/null", stream=False)
        if tail.strip():
            for line in tail.strip().splitlines():
                _con.print(f"    [dim]{line}[/dim]")
        raise RuntimeError(f"{fw.label} exited immediately — check logs above")

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


def install_comfyui(session: RemoteSession) -> None:
    """Backward-compatible wrapper."""
    install_framework(session, "comfyui")


def install_swarmui(session: RemoteSession) -> None:
    """Backward-compatible wrapper."""
    install_framework(session, "swarmui")


# ── symlinks ────────────────────────────────────────────────────────


def link_models_to_comfyui(session: RemoteSession) -> None:
    """Symlink /workspace/models into ComfyUI's model directory."""
    dirs = ["checkpoints", "loras", "vae", "controlnet", "clip", "upscale_models", "unet"]
    cmds = ["mkdir -p /workspace/models/{" + ",".join(dirs) + "}"]
    for d in dirs:
        cmds.append(
            f"[ -L /workspace/ComfyUI/models/{d} ] || "
            f"(rm -rf /workspace/ComfyUI/models/{d} "
            f"&& ln -s /workspace/models/{d} /workspace/ComfyUI/models/{d})"
        )
    _step(session, "Symlinking models → ComfyUI", " && ".join(cmds))


# ── workspace lifecycle ─────────────────────────────────────────────


def _has_direct_ssh(inst: Instance) -> bool:
    """True when the instance exposes a public IP with a mapped SSH port."""
    return bool(inst.ip_address and inst.ports.get(22))


def _has_relay_ssh(inst: Instance) -> bool:
    return bool(inst.ssh_host and inst.ssh_port)


def _is_relay_host(inst: Instance) -> bool:
    """True when ssh_host is a relay proxy, not the instance's own IP."""
    if not inst.ssh_host:
        return False
    return inst.ssh_host != inst.ip_address


def wait_for_ssh(
    provider,
    instance_id: str,
    timeout: int = 300,
    poll_interval: int = 10,
    direct_grace: int = 30,
) -> Instance:
    """Poll until the instance is running and SSH is reachable.

    Prefers a direct IP+port connection over the provider relay.  When
    only a relay endpoint is found, keeps polling up to *direct_grace*
    extra seconds for a direct endpoint before falling back.  Providers
    that expose SSH directly (Lambda, GCP, AWS) skip the grace window.
    """
    from swm import config as _cfg

    start = time.time()
    last_status = ""
    inst = None
    relay_seen_at: float | None = None

    # Phase 1: wait for the instance to be RUNNING with an SSH endpoint.
    while time.time() - start < timeout:
        try:
            if hasattr(provider, "get_instance"):
                inst = provider.get_instance(instance_id)
            else:
                instances = provider.list_instances()
                inst = next((i for i in instances if i.id == instance_id), None)

            if inst:
                status = inst.status.value
                if status != last_status:
                    console.print(f"  Status: [bold]{status}[/bold]")
                    last_status = status

                if inst.status == InstanceStatus.RUNNING:
                    if _has_direct_ssh(inst):
                        break
                    if _has_relay_ssh(inst):
                        if not _is_relay_host(inst):
                            # ssh_host is the instance's own IP (Lambda,
                            # GCP, AWS) — no relay/direct split, proceed.
                            break
                        if relay_seen_at is None:
                            relay_seen_at = time.time()
                        elif time.time() - relay_seen_at >= direct_grace:
                            break
        except Exception:
            pass

        time.sleep(poll_interval)
    else:
        raise TimeoutError(f"Pod not running after {timeout}s for instance {instance_id}")

    # Phase 2: pick the best SSH path — direct mapped port always wins.
    if _has_direct_ssh(inst):
        ssh_target = inst.ip_address
        port = inst.ports[22]
        ssh_user = "root"
        console.print(f"  Direct SSH: {ssh_target}:{port}")
    elif _has_relay_ssh(inst):
        ssh_target = inst.ssh_host
        port = inst.ssh_port
        ssh_user = inst.ssh_user or "root"
        if _is_relay_host(inst):
            console.print(f"  Relay SSH: {ssh_user}@{ssh_target}:{port}")
        else:
            console.print(f"  SSH: {ssh_user}@{ssh_target}:{port}")
    else:
        console.print("  [yellow]No SSH endpoint found — returning anyway[/yellow]")
        return inst

    # Phase 3: probe until SSH actually responds.
    probe = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=5",
        "-o", "LogLevel=ERROR",
        "-p", str(port),
    ]
    key = _cfg.get(f"{provider.slug}.ssh_key") or _cfg.get("ssh.key_path")
    if key:
        probe.extend(["-i", str(key)])
    probe.extend([f"{ssh_user}@{ssh_target}", "echo __SWM_OK__"])

    while time.time() - start < timeout:
        try:
            result = subprocess.run(probe, capture_output=True, timeout=15)
            if b"__SWM_OK__" in result.stdout:
                console.print("  [green]✓ SSH ready[/green]")
                return inst
        except (subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(5)

    raise TimeoutError(f"SSH not reachable after {timeout}s for instance {instance_id}")


def next_workspace_name(storage_provider, bucket: str) -> str:
    """Find the next available workspace name in a bucket.

    Existing: workspace/, workspace2/ → returns "workspace3".
    Empty bucket → returns "workspace".
    """
    try:
        objects = storage_provider.ls(bucket)
    except Exception:
        return "workspace"

    existing: set[int] = set()
    for obj in objects:
        name = obj.key.rstrip("/")
        if name == "workspace":
            existing.add(1)
        elif name.startswith("workspace"):
            suffix = name.removeprefix("workspace")
            try:
                existing.add(int(suffix))
            except ValueError:
                pass

    if not existing:
        return "workspace"

    return f"workspace{max(existing) + 1}"


def workspace_pull(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    dest: str = "/workspace",
    extra_excludes: list[str] | None = None,
    total_bytes: int = 0,
    total_files: int = 0,
    force: bool = False,
) -> None:
    """Non-destructive pull: download workspace from storage to pod.

    On a fresh pod (empty *dest*), downloads everything directly — no
    per-file existence checks.  On a pod with existing data, uses
    ``--no-clobber`` to skip files that already exist.
    """
    env = _s3_env(storage_slug)
    excludes = ""
    for pat in (extra_excludes or []):
        excludes += f" --exclude '{pat}'"
    session.exec(f"mkdir -p '{dest}'", stream=False)

    _, out, _ = session.exec(f"ls -1A '{dest}' 2>/dev/null | head -1", stream=False)
    is_fresh = not out.strip()

    if is_fresh:
        console.print("  [dim]Fresh pod — downloading all files[/dim]")
        noclobber = ""
    else:
        console.print("  [dim]Existing data — skipping files already on disk[/dim]")
        noclobber = " --no-clobber"

    _s5cmd_transfer(
        session,
        f"Pulling {workspace}/ → {dest}/",
        f"{env} s5cmd cp{noclobber} --show-progress{excludes} "
        f"'s3://{bucket}/{workspace}/*' '{dest}/'",
        force=force,
        total_bytes=total_bytes,
        total_files=total_files,
    )


def workspace_push(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str = "/workspace",
    extra_excludes: list[str] | None = None,
    force: bool = False,
) -> None:
    """Non-destructive push: upload pod workspace to storage.

    Uses ``s5cmd sync --size-only`` — only uploads new/changed files.
    """
    env = _s3_env(storage_slug)
    excludes = ""
    for pat in (extra_excludes or []):
        excludes += f" --exclude '{pat}'"
    _s5cmd_transfer(
        session,
        f"Pushing {src}/ → {workspace}/ on s3://{bucket}",
        f"{env} s5cmd sync --size-only{excludes} "
        f"'{src}/*' 's3://{bucket}/{workspace}/'",
        force=force,
    )
