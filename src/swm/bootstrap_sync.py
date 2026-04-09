"""Workspace sync, preflight checks, and filesystem watcher."""

from __future__ import annotations

from dataclasses import dataclass, field

from swm.bootstrap import (
    SAFETY_MARGIN,
    _humanize,
    _s3_env,
    _s5cmd_transfer,
    _step,
    console,
)
from swm.remote.ssh import RemoteSession


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


_PUSH_STAMP = "/workspace/.swm_last_push"
_WATCH_LOG = "/workspace/.swm_changes.log"
_WATCH_PID = "/tmp/.swm_watcher.pid"
_WATCH_EXCLUDES = (
    r"\.swm_changes\.log",
    r"\.swm_last_push",
    r"\.swm_watcher\.pid",
    r"/\.git/",
    r"__pycache__",
)


_WATCHER_SCRIPT = "/tmp/.swm_start_watcher.sh"


def _watcher_pid_alive(session: RemoteSession) -> bool:
    """True only if the PID file exists and the process is running."""
    _, out, _ = session.exec(
        f"test -f {_WATCH_PID} && kill -0 $(cat {_WATCH_PID}) 2>/dev/null "
        "&& echo alive || echo dead",
        stream=False,
    )
    return "alive" in out


def start_watcher(session: RemoteSession, src: str = "/workspace") -> bool:
    """Start an inotifywait daemon to track filesystem changes.

    Returns True if the watcher was started (or already running).
    """
    if _watcher_pid_alive(session):
        return True

    _, has_cmd, _ = session.exec(
        "command -v inotifywait >/dev/null 2>&1 && echo yes || echo no",
        stream=False,
    )
    if "yes" not in has_cmd:
        return False

    exclude_re = "|".join(_WATCH_EXCLUDES)
    script_body = (
        "#!/bin/bash\n"
        f"nohup inotifywait -m -r "
        f"--exclude '({exclude_re})' "
        f"-e modify,create,delete,moved_to "
        f"--format '%w%f' "
        f"'{src}' >> {_WATCH_LOG} 2>/dev/null &\n"
        f"echo $! > {_WATCH_PID}\n"
    )
    import base64
    b64 = base64.b64encode(script_body.encode()).decode()
    session.exec(
        f"echo '{b64}' | base64 -d > {_WATCHER_SCRIPT} && "
        f"chmod +x {_WATCHER_SCRIPT} && bash {_WATCHER_SCRIPT}",
        stream=False,
    )

    import time as _t
    _t.sleep(1)
    return _watcher_pid_alive(session)


def stop_watcher(session: RemoteSession) -> None:
    """Stop the inotifywait daemon if running."""
    session.exec(
        f"test -f {_WATCH_PID} && kill $(cat {_WATCH_PID}) 2>/dev/null; "
        f"rm -f {_WATCH_PID}",
        stream=False,
    )


def is_watcher_alive(session: RemoteSession) -> bool:
    """Check if the filesystem watcher daemon is running."""
    return _watcher_pid_alive(session)


def _restore_permissions(session: RemoteSession, dest: str) -> None:
    """Restore execute bits stripped by B2/S3 storage after a pull.

    B2 does not preserve Unix permissions, so venv binaries, shell
    scripts, and compiled shared objects all lose their +x bit.
    """
    _step(
        session,
        "Restoring execute permissions",
        f"find '{dest}' -path '*/bin/*' -type f -exec chmod +x {{}} + "
        f"&& find '{dest}' -name '*.sh' -type f -exec chmod +x {{}} + "
        f"&& find '{dest}' -name '*.so' -type f -exec chmod +x {{}} +",
    )


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

    _restore_permissions(session, dest)

    session.exec(f": > {_WATCH_LOG} 2>/dev/null; touch {_PUSH_STAMP}", stream=False)
    if start_watcher(session, dest):
        console.print("  [dim]Watcher started for change tracking[/dim]")


_STAGING = "/tmp/.swm_push_staging"


def _stage_hardlinks(
    session: RemoteSession, filelist: str, src: str,
) -> None:
    """Create a staging tree of hardlinks for only the changed files.

    Each file in *filelist* (absolute paths under *src*) gets a hardlink
    in ``_STAGING`` preserving relative directory structure.  Hardlinks
    are instant and use no extra disk space.
    """
    session.exec(f"rm -rf {_STAGING}", stream=False)
    session.exec(
        f"while IFS= read -r f; do "
        f"  rel=\"${{f#{src}/}}\"; "
        f"  mkdir -p \"{_STAGING}/$(dirname \"$rel\")\"; "
        f"  ln \"$f\" \"{_STAGING}/$rel\" 2>/dev/null "
        f"    || cp \"$f\" \"{_STAGING}/$rel\"; "
        f"done < {filelist}",
        stream=False,
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

    Three-tier strategy:
    1. **Watcher alive** — read changed paths from the inotify log (instant).
    2. **Watcher dead / no log** — ``find -newer`` against the push stamp (~30-40s).
    3. **No stamp (first push)** — full ``cp --if-size-differ``.
    """
    env = _s3_env(storage_slug)
    excludes = ""
    for pat in (extra_excludes or []):
        excludes += f" --exclude '{pat}'"

    _, stamp_check, _ = session.exec(
        f"test -f {_PUSH_STAMP} && echo yes || echo no", stream=False,
    )
    has_stamp = stamp_check.strip() == "yes"

    filelist = "/tmp/.swm_push_files"
    rc: int

    if has_stamp and is_watcher_alive(session):
        # ── Tier 1: use inotify change log (instant) ──
        console.print("  [dim]Watcher active — reading change log[/dim]")
        session.exec(
            f"sort -u {_WATCH_LOG} 2>/dev/null"
            f" | while IFS= read -r f; do [ -f \"$f\" ] && echo \"$f\"; done"
            f" > {filelist}",
            stream=False,
        )
        _, count_out, _ = session.exec(f"wc -l < {filelist}", stream=False)
        changed = int(count_out.strip() or "0")
        console.print(f"  [dim]{changed} file(s) changed since last push[/dim]")

        if changed == 0:
            console.print(f"\n[green]✓ Nothing to push — workspace is up to date[/green]")
            session.exec(f": > {_WATCH_LOG} && touch {_PUSH_STAMP}", stream=False)
            return

        _stage_hardlinks(session, filelist, src)
        rc = _s5cmd_transfer(
            session,
            f"Pushing {changed} changed file(s) → {workspace}/ on s3://{bucket}",
            f"{env} s5cmd cp --show-progress "
            f"'{_STAGING}/*' 's3://{bucket}/{workspace}/'",
            force=force,
        )
        session.exec(f"rm -rf {_STAGING}", stream=False)
        if rc == 0:
            session.exec(f": > {_WATCH_LOG} && touch {_PUSH_STAMP}", stream=False)

    elif has_stamp:
        # ── Tier 2: find -newer (watcher not running) ──
        console.print("  [dim]Watcher not running — scanning with find[/dim]")
        find_cmd = f"find '{src}' -newer {_PUSH_STAMP} -type f"
        for pat in (extra_excludes or []):
            find_cmd += f" ! -path '{src}/{pat}'"

        with console.status("Scanning for changes…", spinner="dots"):
            session.exec(f"{find_cmd} > {filelist}", stream=False)
            _, count_out, _ = session.exec(f"wc -l < {filelist}", stream=False)

        changed = int(count_out.strip() or "0")
        console.print(f"  [dim]{changed} file(s) changed since last push[/dim]")

        if changed == 0:
            console.print(f"\n[green]✓ Nothing to push — workspace is up to date[/green]")
            session.exec(f"touch {_PUSH_STAMP}", stream=False)
            return

        _stage_hardlinks(session, filelist, src)
        rc = _s5cmd_transfer(
            session,
            f"Pushing {changed} changed file(s) → {workspace}/ on s3://{bucket}",
            f"{env} s5cmd cp --show-progress "
            f"'{_STAGING}/*' 's3://{bucket}/{workspace}/'",
            force=force,
        )
        session.exec(f"rm -rf {_STAGING}", stream=False)
        if rc == 0:
            session.exec(f"touch {_PUSH_STAMP}", stream=False)

        # Auto-restart the watcher for next time.
        if start_watcher(session, src):
            console.print("  [dim]Watcher restarted for next push[/dim]")

    else:
        # ── Tier 3: first push — full scan ──
        console.print("  [dim]First push — scanning all files[/dim]")
        rc = _s5cmd_transfer(
            session,
            f"Pushing {src}/ → {workspace}/ on s3://{bucket}",
            f"{env} s5cmd cp --if-size-differ --show-progress{excludes} "
            f"'{src}/*' 's3://{bucket}/{workspace}/'",
            force=force,
        )
        if rc == 0:
            session.exec(f"touch {_PUSH_STAMP}", stream=False)
            if start_watcher(session, src):
                console.print("  [dim]Watcher started for future pushes[/dim]")
