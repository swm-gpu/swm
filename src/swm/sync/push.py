"""Workspace push: upload changes from pod to storage (streaming or tarball)."""

from __future__ import annotations

import shlex

from swm.bootstrap import _s3_env, _s5cmd_transfer, console
from swm.remote.ssh import RemoteSession
from swm.sync._common import ensure_pigz, stage_hardlinks
from swm.sync.paths import (
    DELETED_LIST,
    PUSH_STAMP,
    STAGING,
    TAR_PATH,
    WATCH_EXCLUDES,
    WATCH_LOG,
)
from swm.sync.watcher import is_watcher_alive, start_watcher

_FILELIST = "/tmp/.swm_push_files"
_FINDLIST = "/tmp/.swm_push_find_files"
_WATCH_SNAP = "/tmp/.swm_push_watch_snap"
_CYCLE_MARK = "/tmp/.swm_push_cycle_mark"


def _touch_cycle_mark(session: RemoteSession) -> None:
    """Create a high-watermark timestamp for this push cycle."""
    session.exec(f": > {_CYCLE_MARK}", stream=False)


def _stamp_to_cycle_mark(session: RemoteSession) -> None:
    """Advance the sync stamp only to the cycle's high-watermark time."""
    session.exec(f"touch -r {_CYCLE_MARK} {PUSH_STAMP}", stream=False)


def _cleanup_incremental_files(session: RemoteSession) -> None:
    session.exec(
        f"rm -f {_FILELIST} {_FINDLIST} {_WATCH_SNAP} {_CYCLE_MARK}",
        stream=False,
    )


def _find_changed_command(
    src: str,
    upper_mark: str,
    extra_excludes: list[str] | None = None,
) -> str:
    """Shell command that finds changed files between PUSH_STAMP and upper_mark."""
    cmd = (
        f"find {shlex.quote(src)} -newer {PUSH_STAMP} "
        f"! -newer {upper_mark} -type f"
    )
    for pat in (extra_excludes or []):
        path_pat = pat if pat.startswith("/") else f"{src.rstrip('/')}/{pat}"
        cmd += f" ! -path {shlex.quote(path_pat)}"
    exclude_re = shlex.quote("(" + "|".join(WATCH_EXCLUDES) + ")")
    return f"( {cmd} 2>/dev/null | grep -Ev {exclude_re} || true )"


def _tar_push(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str = "/workspace",
    extra_excludes: list[str] | None = None,
    force: bool = False,
) -> int:
    """Pack workspace into a tarball and upload as a single S3 object.

    Uses pigz (parallel gzip) when available for multi-core compression.
    """
    env = _s3_env(storage_slug)
    compressor = ensure_pigz(session, console)

    tar_excludes = ""
    for pat in (extra_excludes or []):
        tar_excludes += f" --exclude='{pat}'"
    for builtin in (
        ".git", "__pycache__", ".swm_changes.log",
        ".swm_last_push", ".swm_watcher.pid",
        ".swm_workspace.tar.gz",
    ):
        tar_excludes += f" --exclude='{builtin}'"

    _, du_out, _ = session.exec(
        f"du -sh '{src}' 2>/dev/null | cut -f1", stream=False,
    )
    size = du_out.strip() or "?"
    console.print(f"  [dim]Tar mode — packing {size} with {compressor}[/dim]")

    tar_cmd = (
        f"tar -cf - -C '{src}'{tar_excludes} . "
        f"| {compressor} > {TAR_PATH}"
    )
    _s5cmd_transfer(
        session,
        f"Packing {src}/ into tarball",
        f"{tar_cmd} && ls -lh {TAR_PATH} | awk '{{print $5}}'",
        force=force,
    )

    _, tar_size, _ = session.exec(
        f"ls -lh {TAR_PATH} 2>/dev/null | awk '{{print $5}}'",
        stream=False,
    )
    console.print(f"  [dim]Tarball: {tar_size.strip() or '?'}[/dim]")

    s3_key = f"s3://{bucket}/{workspace}.tar.gz"
    rc = _s5cmd_transfer(
        session,
        f"Uploading tarball → {s3_key}",
        f"{env} s5cmd cp --show-progress "
        f"--concurrency 64 --part-size 100 "
        f"{TAR_PATH} '{s3_key}'",
        force=False,
    )

    session.exec(f"rm -f {TAR_PATH}", stream=False)
    if rc == 0:
        console.print(f"  [dim]Tarball uploaded as {workspace}.tar.gz[/dim]")
        session.exec(f"touch {PUSH_STAMP}", stream=False)
    elif force:
        console.print(
            f"  [yellow]⚠ Tarball upload had errors (exit {rc}). "
            f"Advancing stamp anyway since --force was used; any failed "
            f"files will be retried by the next autosync cycle.[/yellow]"
        )
        session.exec(f"touch {PUSH_STAMP}", stream=False)
    else:
        console.print(
            f"  [yellow]⚠ Tarball upload had errors (exit {rc}). "
            f"Stamp NOT written; autosync will refuse to start. "
            f"Re-run with --force to mark synced anyway.[/yellow]"
        )
    return rc


def _sync_deletions(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str,
) -> int:
    """Read the deleted-files list from the pod and remove them from storage.

    Returns the number of keys deleted.
    """
    _, raw, _ = session.exec(f"cat {DELETED_LIST} 2>/dev/null", stream=False)
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        return 0

    prefix = src.rstrip("/") + "/"
    s3_keys = [
        f"{workspace}/{line.removeprefix(prefix)}"
        for line in lines
        if line.startswith(prefix)
    ]
    if not s3_keys:
        return 0

    from swm.storage import get_storage
    from swm.storage.base import S3CompatProvider

    provider = get_storage(storage_slug)
    if not isinstance(provider, S3CompatProvider):
        return 0

    deleted = provider.delete_keys(bucket, s3_keys)
    console.print(f"  [dim]{deleted} deleted file(s) removed from storage[/dim]")
    session.exec(f"rm -f {DELETED_LIST}", stream=False)
    return deleted


def _push_watcher_tier(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str,
    extra_excludes: list[str] | None,
    force: bool,
    delete: bool,
) -> int:
    """Tier 1: watcher is alive, read change log for incremental push."""
    env = _s3_env(storage_slug)
    console.print("  [dim]Watcher active — reconciling change log with filesystem scan[/dim]")

    _touch_cycle_mark(session)
    session.exec(
        f"cp {WATCH_LOG} {_WATCH_SNAP} 2>/dev/null || : > {_WATCH_SNAP}; "
        f": > {WATCH_LOG}",
        stream=False,
    )
    find_cmd = _find_changed_command(src, _CYCLE_MARK, extra_excludes)
    session.exec(f"{find_cmd} > {_FINDLIST}", stream=False)
    session.exec(
        f"{{ sort -u {_WATCH_SNAP}"
        f" | while IFS= read -r f; do [ -f \"$f\" ] && echo \"$f\"; done; "
        f"cat {_FINDLIST}; }} | sort -u > {_FILELIST}",
        stream=False,
    )
    if delete:
        session.exec(
            f"sort -u {_WATCH_SNAP} 2>/dev/null"
            f" | while IFS= read -r f; do [ ! -e \"$f\" ] && echo \"$f\"; done"
            f" > {DELETED_LIST}",
            stream=False,
        )

    _, count_out, _ = session.exec(f"wc -l < {_FILELIST}", stream=False)
    changed = int(count_out.strip() or "0")

    deleted_count = 0
    if delete:
        _, del_out, _ = session.exec(
            f"wc -l < {DELETED_LIST} 2>/dev/null || echo 0", stream=False,
        )
        deleted_count = int(del_out.strip() or "0")

    console.print(
        f"  [dim]{changed} file(s) changed"
        + (f", {deleted_count} file(s) deleted" if deleted_count else "")
        + " since last push[/dim]"
    )

    if changed == 0 and deleted_count == 0:
        console.print("\n[green]✓ Nothing to push — workspace is up to date[/green]")
        _stamp_to_cycle_mark(session)
        _cleanup_incremental_files(session)
        return 0

    rc = 0
    if changed > 0:
        stage_hardlinks(session, _FILELIST, src)
        rc = _s5cmd_transfer(
            session,
            f"Pushing {changed} changed file(s) → {workspace}/ on s3://{bucket}",
            f"{env} s5cmd cp --show-progress "
            f"'{STAGING}/*' 's3://{bucket}/{workspace}/'",
            force=force,
        )
        session.exec(f"rm -rf {STAGING}", stream=False)

    if rc == 0 and deleted_count > 0:
        _sync_deletions(session, storage_slug, bucket, workspace, src)

    if rc == 0:
        _stamp_to_cycle_mark(session)
    else:
        console.print(
            f"  [yellow]⚠ Push had errors (s5cmd exit {rc}). Stamp NOT "
            f"advanced; failed entries re-queued for the next push.[/yellow]"
        )
        session.exec(f"cat {_WATCH_SNAP} >> {WATCH_LOG} 2>/dev/null || true", stream=False)

    _cleanup_incremental_files(session)
    return rc


def _push_find_tier(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str,
    extra_excludes: list[str] | None,
    force: bool,
    delete: bool,
) -> int:
    """Tier 2: watcher dead, fall back to find -newer."""
    env = _s3_env(storage_slug)
    if delete:
        raise RuntimeError(
            "Watcher is not running, so deletions cannot be detected. "
            "Refusing to push silently — local deletions would not "
            "propagate and could cause stale storage. Start the watcher "
            "first (`swm sync watch <pod>`), or re-run without --delete."
        )
    console.print("  [dim]Watcher not running — scanning with find[/dim]")

    _touch_cycle_mark(session)
    find_cmd = _find_changed_command(src, _CYCLE_MARK, extra_excludes)

    with console.status("Scanning for changes…", spinner="dots"):
        session.exec(f"{find_cmd} > {_FILELIST}", stream=False)
        _, count_out, _ = session.exec(f"wc -l < {_FILELIST}", stream=False)

    changed = int(count_out.strip() or "0")
    console.print(f"  [dim]{changed} file(s) changed since last push[/dim]")

    if changed == 0:
        console.print("\n[green]✓ Nothing to push — workspace is up to date[/green]")
        _stamp_to_cycle_mark(session)
        _cleanup_incremental_files(session)
        return 0

    stage_hardlinks(session, _FILELIST, src)
    rc = _s5cmd_transfer(
        session,
        f"Pushing {changed} changed file(s) → {workspace}/ on s3://{bucket}",
        f"{env} s5cmd cp --show-progress "
        f"'{STAGING}/*' 's3://{bucket}/{workspace}/'",
        force=force,
    )
    session.exec(f"rm -rf {STAGING}", stream=False)
    if rc == 0:
        _stamp_to_cycle_mark(session)
    else:
        console.print(
            f"  [yellow]⚠ Push had errors (s5cmd exit {rc}). Stamp NOT "
            f"advanced; next push will re-scan and retry.[/yellow]"
        )
    _cleanup_incremental_files(session)

    if start_watcher(session, src):
        console.print("  [dim]Watcher restarted for next push[/dim]")
    return rc


def _push_first_tier(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str,
    extra_excludes: list[str] | None,
    force: bool,
) -> int:
    """Tier 3: no push stamp yet, full parallel upload."""
    env = _s3_env(storage_slug)
    excludes = ""
    for pat in (extra_excludes or []):
        excludes += f" --exclude '{pat}'"

    _, du_out, _ = session.exec(f"du -sh '{src}' 2>/dev/null | cut -f1", stream=False)
    size = du_out.strip() or "?"
    console.print(f"  [dim]First push — {size} to upload[/dim]")

    _touch_cycle_mark(session)
    rc = _s5cmd_transfer(
        session,
        f"Pushing {src}/ → {workspace}/",
        f"{env} s5cmd --numworkers 512 --log error cp --show-progress{excludes} "
        f"'{src}/' 's3://{bucket}/{workspace}/'",
        force=force,
    )
    if rc == 0:
        _stamp_to_cycle_mark(session)
        if start_watcher(session, src):
            console.print("  [dim]Watcher started for future pushes[/dim]")
    elif force:
        # --force means the caller has explicitly asked for this pod's
        # state to become authoritative. Transient per-file errors
        # (e.g. B2 503 SlowDown on the long tail of a 100k+ object
        # upload) should not block the stamp; the watcher + next
        # autosync cycle will retry any missed files via find -newer.
        console.print(
            f"  [yellow]⚠ Initial push had errors (s5cmd exit {rc}). "
            f"Advancing stamp anyway since --force was used; any failed "
            f"files will be retried by the next autosync cycle.[/yellow]"
        )
        _stamp_to_cycle_mark(session)
        if start_watcher(session, src):
            console.print("  [dim]Watcher started for future pushes[/dim]")
    else:
        console.print(
            f"  [yellow]⚠ Initial push had errors (s5cmd exit {rc}). "
            f"Stamp NOT written; autosync will refuse to start. Re-run "
            f"with --force to mark synced anyway.[/yellow]"
        )
    _cleanup_incremental_files(session)
    return rc


def workspace_push(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str = "/workspace",
    extra_excludes: list[str] | None = None,
    force: bool = False,
    tar: bool = False,
    delete: bool = False,
) -> int:
    """Non-destructive push: upload pod workspace to storage.

    When *tar* is True, packs the workspace into a compressed tarball
    and uploads it as a single object — dramatically faster for
    workspaces with many small files (100k+).

    When *delete* is True and the watcher is alive (Tier 1), files
    deleted locally since the last push are also deleted from storage.

    Otherwise uses the three-tier strategy:
    1. **Watcher alive** — read changed paths from the inotify log (instant).
    2. **Watcher dead / no log** — ``find -newer`` against the push stamp.
    3. **No stamp (first push)** — full parallel upload.

    Returns the s5cmd exit code (0 on success, non-zero on partial
    failure). Callers should propagate non-zero to their own exit code.
    """
    if tar:
        return _tar_push(session, storage_slug, bucket, workspace, src,
                         extra_excludes, force)

    if force:
        session.exec(f"rm -f {PUSH_STAMP} {WATCH_LOG}", stream=False)

    _, stamp_check, _ = session.exec(
        f"test -f {PUSH_STAMP} && echo yes || echo no", stream=False,
    )
    has_stamp = stamp_check.strip() == "yes"

    if has_stamp and is_watcher_alive(session):
        return _push_watcher_tier(
            session, storage_slug, bucket, workspace, src,
            extra_excludes, force, delete,
        )
    elif has_stamp:
        return _push_find_tier(
            session, storage_slug, bucket, workspace, src,
            extra_excludes, force, delete,
        )
    else:
        return _push_first_tier(
            session, storage_slug, bucket, workspace, src,
            extra_excludes, force,
        )
