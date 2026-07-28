"""SSH waiting utilities for remote GPU instances."""

from __future__ import annotations

import subprocess
import time


from swm.providers.base import Instance, InstanceStatus
from swm.redact import SafeConsole

console = SafeConsole()


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
    timeout: int = 600,
    poll_interval: int = 10,
    direct_grace: int = 30,
) -> Instance:
    """Poll until the instance is running and SSH is reachable.

    Prefers a direct IP+port connection over the provider relay.  When
    only a relay endpoint is found, keeps polling up to *direct_grace*
    extra seconds for a direct endpoint before falling back.  Providers
    that expose SSH directly (Lambda, GCP, AWS) skip the grace window.

    Displays ``status_detail`` from the provider (e.g. Vast.ai Docker
    build progress) when available to give the user visibility into
    what the remote host is doing.
    """
    from swm import config as _cfg

    start = time.time()
    last_status = ""
    last_detail = ""
    inst = None
    relay_seen_at: float | None = None

    def _elapsed() -> str:
        return f"{int(time.time() - start)}s"

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
                    console.print(f"  Status: [bold]{status}[/bold]  ({_elapsed()})")
                    last_status = status

                detail = inst.status_detail or ""
                if detail and detail != last_detail:
                    truncated = (detail[:100] + "…") if len(detail) > 100 else detail
                    console.print(f"  [dim]{truncated}[/dim]")
                    last_detail = detail

                if inst.status == InstanceStatus.RUNNING:
                    if _has_direct_ssh(inst):
                        break
                    if _has_relay_ssh(inst):
                        if not _is_relay_host(inst):
                            break
                        if relay_seen_at is None:
                            relay_seen_at = time.time()
                        elif time.time() - relay_seen_at >= direct_grace:
                            break
        except Exception:
            pass

        time.sleep(poll_interval)
    else:
        raise TimeoutError(
            f"Pod not running after {timeout}s for instance {instance_id}. "
            f"Last status: {last_status}"
        )

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
    console.print(f"  Probing SSH…  ({_elapsed()})")
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
                console.print(f"  [green]✓ SSH ready[/green]  ({_elapsed()})")
                return inst
        except (subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(5)

    raise TimeoutError(
        f"SSH not reachable after {timeout}s for instance {instance_id}"
    )


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
