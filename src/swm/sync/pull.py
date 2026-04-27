"""Workspace pull: download from storage to pod (streaming or tarball)."""

from __future__ import annotations

from swm.bootstrap import _s3_env, _s5cmd_transfer, console
from swm.remote.ssh import RemoteSession
from swm.sync._common import ensure_pigz, restore_permissions
from swm.sync.paths import PUSH_STAMP, TAR_PATH, WATCH_LOG
from swm.sync.watcher import start_watcher


def workspace_pull(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    dest: str = "/workspace",
    extra_excludes: list[str] | None = None,
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
    )

    restore_permissions(session, dest)

    session.exec(f": > {WATCH_LOG} 2>/dev/null; touch {PUSH_STAMP}", stream=False)
    if start_watcher(session, dest):
        console.print("  [dim]Watcher started for change tracking[/dim]")


def tar_pull(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    dest: str = "/workspace",
    force: bool = False,
) -> None:
    """Download a tarball from S3 and extract it into *dest*.

    Counterpart to ``tar_push``.  Uses pigz for parallel decompression
    when available.
    """
    env = _s3_env(storage_slug)
    s3_key = f"s3://{bucket}/{workspace}.tar.gz"

    compressor = ensure_pigz(session, console)
    decompressor = f"{compressor} -d"

    session.exec(f"mkdir -p '{dest}'", stream=False)

    rc = _s5cmd_transfer(
        session,
        f"Downloading {s3_key}",
        f"{env} s5cmd cp --show-progress "
        f"--concurrency 64 --part-size 100 "
        f"'{s3_key}' {TAR_PATH}",
        force=force,
    )
    if rc != 0:
        console.print("  [red]✗ Tarball download failed[/red]")
        return

    _, tar_size, _ = session.exec(
        f"ls -lh {TAR_PATH} 2>/dev/null | awk '{{print $5}}'",
        stream=False,
    )
    console.print(f"  [dim]Tarball: {tar_size.strip() or '?'} — extracting[/dim]")

    _s5cmd_transfer(
        session,
        f"Extracting → {dest}/",
        f"{decompressor} < {TAR_PATH} | tar -xf - -C '{dest}'",
        force=False,
    )

    session.exec(f"rm -f {TAR_PATH}", stream=False)

    restore_permissions(session, dest)

    session.exec(
        f": > {WATCH_LOG} 2>/dev/null; touch {PUSH_STAMP}", stream=False,
    )
    if start_watcher(session, dest):
        console.print("  [dim]Watcher started for change tracking[/dim]")
