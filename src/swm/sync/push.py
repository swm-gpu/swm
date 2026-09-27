"""Workspace push: upload changes from pod to storage (streaming or tarball)."""

from __future__ import annotations

import shlex

from swm.bootstrap import _s3_env, _s5cmd_transfer, console, transfer_lock
from swm.remote.ssh import RemoteSession
from swm.sync._common import PUSH_IN_PLACE, clear_staged_files, ensure_pigz, stage_hardlinks
from swm.sync.paths import (
    DELETED_LIST,
    PUSH_STAMP,
    STAGING_ROOT_NAME,
    TAR_PATH,
    WATCH_EXCLUDES,
    WATCH_LOG,
    staging_dir_for,
)
from swm.sync.watcher import is_watcher_alive, start_watcher

_FILELIST = "/tmp/.swm_push_files"
_FINDLIST = "/tmp/.swm_push_find_files"
_WATCH_SNAP = "/tmp/.swm_push_watch_snap"
_CYCLE_MARK = "/tmp/.swm_push_cycle_mark"
_IN_PLACE = PUSH_IN_PLACE


def _touch_cycle_mark(session: RemoteSession) -> None:
    """Create a high-watermark timestamp for this push cycle."""
    session.exec(f": > {_CYCLE_MARK}", stream=False)


def _stamp_to_cycle_mark(session: RemoteSession) -> None:
    """Advance the sync stamp only to the cycle's high-watermark time."""
    session.exec(f"touch -r {_CYCLE_MARK} {PUSH_STAMP}", stream=False)


def _cleanup_incremental_files(session: RemoteSession) -> None:
    session.exec(
        f"rm -f {_FILELIST} {_FINDLIST} {_WATCH_SNAP} {_CYCLE_MARK} {_IN_PLACE}",
        stream=False,
    )


def _file_signature(session: RemoteSession, path: str) -> str | None:
    """Size and mtime of *path*, or None if it no longer exists."""
    q = shlex.quote(path)
    code, out, err = session.exec(
        f"[ -e {q} ] || exit 3; stat -c '%s %y' -- {q}", stream=False,
    )
    if code == 3:
        return None
    if code != 0 or not out.strip():
        raise RuntimeError(f"Could not stat {path}: {(err or out).strip()}")
    return out.strip()


def _push_in_place(
    session: RemoteSession, env: str, bucket: str, workspace: str, force: bool,
) -> int:
    """Upload the files the volume quota refused to stage, each read from
    where it lives. One that changed mid-upload fails the push so it is
    retried; staging's hardlinks never froze contents either."""
    _, out, _ = session.exec(f"cat {shlex.quote(_IN_PLACE)} 2>/dev/null", stream=False)
    entries = [line.split("\t", 1) for line in out.splitlines() if "\t" in line]
    if not entries:
        return 0
    console.print(
        f"  [dim]{len(entries)} file(s) do not fit the volume's staging quota; "
        f"uploading them in place[/dim]"
    )
    for path, rel in entries:
        before = _file_signature(session, path)
        if before is None:
            continue
        dest = shlex.quote(f"s3://{bucket}/{workspace}/{rel}")
        rc = _s5cmd_transfer(
            session,
            f"Pushing {rel} in place",
            f"{env} s5cmd cp --no-follow-symlinks --show-progress "
            f"{shlex.quote(path)} {dest}",
            force=force,
        )
        if rc != 0:
            return rc
        if _file_signature(session, path) != before:
            console.print(f"  [yellow]⚠ {rel} changed while uploading; it will be pushed again[/yellow]")
            return 1
    return 0


def _push_staged(
    session: RemoteSession, staging: str, env: str, bucket: str, workspace: str,
    label: str, force: bool,
) -> int:
    """Copy the staging tree, then whatever had to be uploaded in place.
    An empty tree skips its copy: s5cmd fails a wildcard matching nothing."""
    _, first, _ = session.exec(
        f"find {shlex.quote(staging)} -type f -print -quit", stream=False,
    )
    rc = 0
    if first.strip():
        rc = _s5cmd_transfer(
            session,
            label,
            f"{env} s5cmd cp --no-follow-symlinks --show-progress "
            f"'{staging}/*' 's3://{bucket}/{workspace}/'",
            force=force,
        )
    clear_staged_files(session, staging)
    if rc == 0:
        rc = _push_in_place(session, env, bucket, workspace, force)
    return rc


def _find_excludes(src: str, extra_excludes: list[str] | None) -> str:
    clauses = ""
    for pat in (extra_excludes or []):
        path_pat = pat if pat.startswith("/") else f"{src.rstrip('/')}/{pat}"
        clauses += f" ! -path {shlex.quote(path_pat)}"
    return clauses


def _find_changed_command(
    src: str,
    upper_mark: str,
    extra_excludes: list[str] | None = None,
) -> str:
    """Shell command that finds changed files between PUSH_STAMP and upper_mark.

    Symlinks are included so ones created mid-session (uv/venv installs)
    reach staging, which materialises or skips them.
    """
    cmd = (
        f"find {shlex.quote(src)} -newer {PUSH_STAMP} "
        f"! -newer {upper_mark} \\( -type f -o -type l \\)"
        f"{_find_excludes(src, extra_excludes)}"
    )
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
        ".swm_workspace.tar.gz", ".swm_workspace.tar.zst", ".swm_staging", ".cache",
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
    else:
        # A tarball is one object: a failed upload left nothing usable in
        # storage, so no flag may stamp the pod as synced.
        console.print(
            f"  [yellow]⚠ Tarball upload had errors (exit {rc}). "
            f"Stamp NOT written (--force does not override this); "
            f"autosync will refuse to start. Re-run the push.[/yellow]"
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
    console.print("  [dim]Watcher active — reconciling change log with filesystem scan[/dim]")

    # A watcher started by an older swm may lack the current exclude list
    # (notably the staging dir). start_watcher no-ops when the fingerprint
    # matches and restarts otherwise; it truncates WATCH_LOG on restart, so
    # this must happen BEFORE the snapshot below.
    start_watcher(session, src)

    _touch_cycle_mark(session)
    session.exec(
        f"cp {WATCH_LOG} {_WATCH_SNAP} 2>/dev/null || : > {_WATCH_SNAP}; "
        f": > {WATCH_LOG}",
        stream=False,
    )
    try:
        return _push_watcher_snapshot(
            session, storage_slug, bucket, workspace, src,
            extra_excludes, force, delete,
        )
    except BaseException:
        # The snapshot is the only record of watcher-observed deletions:
        # the stamp was not advanced, so changed files are rediscovered by
        # find, but a snapshot lost here would drop deletions for good.
        session.exec(
            f"cat {_WATCH_SNAP} >> {WATCH_LOG} 2>/dev/null || true",
            stream=False,
        )
        clear_staged_files(session, staging_dir_for(src))
        _cleanup_incremental_files(session)
        raise


def _push_watcher_snapshot(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    src: str,
    extra_excludes: list[str] | None,
    force: bool,
    delete: bool,
) -> int:
    """Reconcile the consumed watch-log snapshot with a scan and push it."""
    env = _s3_env(storage_slug)
    # Filter watcher-derived paths through the excludes: inotify-tools
    # >= 3.22 lets directory-create events through regardless of
    # --exclude, and an older watcher may have logged now-excluded paths.
    exclude_re = shlex.quote("(" + "|".join(WATCH_EXCLUDES) + ")")

    find_cmd = _find_changed_command(src, _CYCLE_MARK, extra_excludes)
    session.exec(f"{find_cmd} > {_FINDLIST}", stream=False)
    session.exec(
        f"{{ sort -u {_WATCH_SNAP} | grep -Ev {exclude_re}"
        f" | while IFS= read -r f; do [ -f \"$f\" ] && echo \"$f\"; done; "
        f"cat {_FINDLIST}; }} | sort -u > {_FILELIST}",
        stream=False,
    )
    if delete:
        session.exec(
            f"sort -u {_WATCH_SNAP} 2>/dev/null | grep -Ev {exclude_re}"
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
        staging = stage_hardlinks(session, _FILELIST, src, _IN_PLACE)
        rc = _push_staged(
            session, staging, env, bucket, workspace,
            f"Pushing {changed} changed file(s) → {workspace}/ on s3://{bucket}",
            force,
        )

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

    try:
        staging = stage_hardlinks(session, _FILELIST, src, _IN_PLACE)
    except RuntimeError:
        _cleanup_incremental_files(session)
        raise
    rc = _push_staged(
        session, staging, env, bucket, workspace,
        f"Pushing {changed} changed file(s) → {workspace}/ on s3://{bucket}",
        force,
    )
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
    """Tier 3: no push stamp yet, full parallel upload.

    The stamp is written only after a clean exit. s5cmd stops walking a
    top-level entry at its first error, so a nonzero exit can mean whole
    subtrees were never visited — and those files keep their old mtimes,
    so stamping anyway (as ``--force`` once did) would hide them from
    every later incremental push.
    """
    env = _s3_env(storage_slug)
    excludes = ""
    for pat in (extra_excludes or []):
        excludes += f" --exclude '{pat}'"

    _, du_out, _ = session.exec(f"du -sh '{src}' 2>/dev/null | cut -f1", stream=False)
    size = du_out.strip() or "?"
    console.print(f"  [dim]First push — {size} to upload[/dim]")

    def tree_copy(flags: str) -> str:
        return (
            f"{env} s5cmd --numworkers 512 --log error cp{flags} "
            f"--no-follow-symlinks --show-progress{excludes} "
            f"'{src}/' 's3://{bucket}/{workspace}/'"
        )

    _touch_cycle_mark(session)
    rc = _s5cmd_transfer(
        session, f"Pushing {src}/ → {workspace}/", tree_copy(""), force=force,
    )
    if rc != 0:
        # Only objects still missing are uploaded (one HEAD each), which
        # turns the common transient failure into a clean run.
        console.print(
            f"  [yellow]⚠ Initial push had errors (s5cmd exit {rc}) — "
            f"retrying objects that are still missing[/yellow]"
        )
        rc = _s5cmd_transfer(
            session,
            f"Retrying {src}/ → {workspace}/ (missing objects only)",
            tree_copy(" -n"),
            force=force,
        )
    if rc == 0:
        rc = _push_tree_symlinks(
            session, env, bucket, workspace, src, extra_excludes, force,
        )
    if rc == 0:
        _stamp_to_cycle_mark(session)
        if start_watcher(session, src):
            console.print("  [dim]Watcher started for future pushes[/dim]")
    else:
        console.print(
            f"  [yellow]⚠ Initial push had errors (s5cmd exit {rc}). "
            f"Stamp NOT written (--force does not override this: files "
            f"skipped by s5cmd would never be retried); autosync will "
            f"refuse to start. Check the s5cmd errors above, fix or "
            f"exclude (-x) the offending paths, and re-run the push — "
            f"with no stamp it is a full pass again.[/yellow]"
        )
    _cleanup_incremental_files(session)
    return rc


def _push_tree_symlinks(
    session: RemoteSession,
    env: str,
    bucket: str,
    workspace: str,
    src: str,
    extra_excludes: list[str] | None,
    force: bool,
) -> int:
    """Tier-3 companion: upload the tree's symlinks as their target files.

    The tree copy runs with ``--no-follow-symlinks`` because one symlink
    s5cmd cannot resolve halts the walk of its whole top-level entry. The
    resolvable ones are hardlink-staged under the link name here — the
    same object a following upload produced — and the rest are skipped
    and reported.
    """
    q_src = shlex.quote(src)
    staging_root = shlex.quote(f"{src.rstrip('/')}/{STAGING_ROOT_NAME}")
    exclude_re = shlex.quote("(" + "|".join(WATCH_EXCLUDES) + ")")
    find_cmd = (
        f"find {q_src} -path {staging_root} -prune -o -type l"
        f"{_find_excludes(src, extra_excludes)} -print"
    )
    session.exec(
        f"( {find_cmd} 2>/dev/null | grep -Ev {exclude_re} || true ) > {_FILELIST}",
        stream=False,
    )
    _, count_out, _ = session.exec(f"wc -l < {_FILELIST}", stream=False)
    found = int(count_out.strip() or "0")
    if found == 0:
        return 0

    console.print(
        f"  [dim]{found} symlink(s) in the tree — materialising those that "
        f"resolve to files[/dim]"
    )
    staging = stage_hardlinks(session, _FILELIST, src, _IN_PLACE)
    _, staged_out, _ = session.exec(
        f"find {shlex.quote(staging)} -type f | wc -l", stream=False,
    )
    staged = int(staged_out.strip() or "0")
    return _push_staged(
        session, staging, env, bucket, workspace,
        f"Pushing {staged} materialised symlink(s) → {workspace}/",
        force,
    )


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
    # Normalize: a trailing slash breaks the ${f#src/} prefix strip used
    # by staging, silently shifting every uploaded S3 key.
    src = "/" + src.strip("/") if src.strip("/") else "/"

    # The lock spans the whole push: the watch-log snapshot below is
    # consumed only once the daemon cannot start a cycle, and the daemon
    # cannot start one while staging (minutes on a big tree) is under way.
    with transfer_lock(session, force=force):
        # Clear staged files a crashed push or autosync daemon may have
        # left behind so tier-3 full uploads (which don't apply
        # WATCH_EXCLUDES) can't ship them, and so leftover hardlinks stop
        # pinning deleted inodes. Keeps the dir skeleton (see
        # paths.STAGING_ROOT_NAME).
        clear_staged_files(session, f"{src.rstrip('/')}/{STAGING_ROOT_NAME}")

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
