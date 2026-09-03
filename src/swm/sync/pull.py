"""Workspace pull: download from storage to pod (streaming or tarball)."""

from __future__ import annotations

import shlex

from swm.bootstrap import _s3_env, _s5cmd_transfer, console
from swm.remote.ssh import RemoteSession, _sh_quote
from swm.sync._common import ensure_pigz, ensure_zstd, restore_permissions
from swm.sync.paths import PUSH_STAMP, TAR_PATH, WATCH_LOG
from swm.sync.watcher import start_watcher


def _is_link_repair_step(label: str) -> bool:
    lowered = label.lower()
    return "link" in lowered or "symlink" in lowered


def _repair_framework_links(session: RemoteSession, dest: str) -> None:
    """Re-run idempotent framework link repair steps after a workspace pull."""
    normalized = dest.rstrip("/")
    if normalized != "/workspace" and not normalized.startswith("/workspace/"):
        return

    from swm.frameworks import list_frameworks

    repaired = 0
    for fw in list_frameworks():
        if not fw.pre_start:
            continue

        _, installed, _ = session.exec(
            f"test -d {shlex.quote(fw.install_dir)} && echo yes || echo no",
            stream=False,
        )
        if installed.strip() != "yes":
            continue

        env_prefix = f"{fw.env_setup} && " if fw.env_setup else ""
        ran_repair = False
        for step in fw.pre_start:
            if not _is_link_repair_step(step.label):
                continue

            ran_repair = True
            workdir = shlex.quote(step.workdir or fw.install_dir)
            if step.check:
                cmd = (
                    f"{step.check} && echo '{step.label}: already done' "
                    f"|| ({env_prefix}cd {workdir} && {step.command})"
                )
            else:
                cmd = f"{env_prefix}cd {workdir} && {step.command}"

            code, _, _ = session.exec(cmd, stream=False)
            if code != 0:
                console.print(
                    f"  [yellow]⚠ Framework repair step failed: "
                    f"{fw.name} - {step.label}[/yellow]"
                )

        if ran_repair:
            repaired += 1

    if repaired:
        console.print(f"  [dim]Framework repair checked {repaired} install(s)[/dim]")


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
    _repair_framework_links(session, dest)

    session.exec(f": > {WATCH_LOG} 2>/dev/null; touch {PUSH_STAMP}", stream=False)
    if start_watcher(session, dest):
        console.print("  [dim]Watcher started for change tracking[/dim]")


# Archive suffix → codec. Anything else is treated as a bare name and gets the
# historical ``.tar.gz`` (or ``.tar.zst`` when the caller names the codec).
TAR_SUFFIXES: dict[str, str] = {".tar.zst": "zstd", ".tar.gz": "gzip"}

# The archive is downloaded to a staging file (concurrent ranged parts written
# at their offsets) and extracted from there. Streaming it through
# ``s5cmd cat`` was tried: its ordered writer head-of-line blocks on one slow
# part and buffers every later part without bound — a 100 GB restore stalled
# after 3.6 GB with 2.5 GB resident and tar starved. ``cp`` has no ordering
# constraint, so a slow part costs only its own time.
_CP_CONCURRENCY = 64
_CP_PART_MIB = 100
# GNU tar emits one heartbeat line per 1 GiB of archive read (102400 records
# of 10 KiB) so a long extract is visibly alive without a progress bar.
_TAR_CHECKPOINT = "--checkpoint=102400 --checkpoint-action=echo"


def tar_staging_path(codec: str) -> str:
    """Where the downloaded archive lands before extraction; the suffix keeps
    the file self-describing for anyone inspecting a half-finished restore."""
    return TAR_PATH if codec == "gzip" else TAR_PATH[: -len(".gz")] + ".zst"


def tar_object(workspace: str, compression: str | None = None) -> tuple[str, str]:
    """Resolve the object key and codec for a tar pull.

    *workspace* may carry its archive suffix (``.tar.zst`` / ``.tar.gz``);
    a bare name gets ``.tar.gz`` unless *compression* is ``"zstd"``.
    """
    for suffix, codec in TAR_SUFFIXES.items():
        if workspace.endswith(suffix):
            if compression and compression != codec:
                raise ValueError(
                    f"{workspace!r} is a {codec} archive but compression="
                    f"{compression!r} was requested")
            return workspace, codec
    if compression is None or compression == "gzip":
        return f"{workspace}.tar.gz", "gzip"
    if compression == "zstd":
        return f"{workspace}.tar.zst", "zstd"
    raise ValueError(f"unsupported tar compression {compression!r}")


def _ensure_decompressor(session: RemoteSession, codec: str) -> str:
    """The stdin→stdout decompression command for *codec*, installing the tool
    on the pod when needed. zstd has no always-present fallback, so a pod
    that cannot get it fails here rather than mid-stream."""
    if codec == "zstd":
        tool = ensure_zstd(session, console)
        if tool is None:
            raise RuntimeError(
                "zstd is not available on this pod and could not be "
                "installed (apt-get install zstd)")
        # pzstd decompresses independent frames in parallel and reads plain
        # single-frame zstd too; zstd -d is single-threaded but still fast.
        return f"{tool} -d -c -q"
    return f"{ensure_pigz(session, console)} -d -c"


def tar_extract_pipeline(decompressor: str, staged: str, dest: str) -> str:
    """The remote extract: ``decompress < staged | tar -x`` under bash.

    Both stages' statuses are checked: with ``pipefail`` alone a truncated
    archive the decompressor happens to tolerate could report success, and
    the statuses are echoed so a failure names its stage.
    """
    checkpoint = (
        "CKPT=''; tar --version 2>/dev/null | grep -q GNU && "
        f"CKPT='{_TAR_CHECKPOINT}'; "
    )
    return (
        "set -o pipefail; " + checkpoint
        + f"{decompressor} < '{staged}' | tar $CKPT -xf - -C '{dest}'; "
        'pcs=("${PIPESTATUS[@]}"); '
        'echo "extract stages: decompress=${pcs[0]:-1} extract=${pcs[1]:-1}"; '
        '[ "${pcs[0]:-1}" -eq 0 ] && [ "${pcs[1]:-1}" -eq 0 ]'
    )


def tar_pull(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    dest: str = "/workspace",
    force: bool = False,
    *,
    compression: str | None = None,
) -> None:
    """Download a tarball from S3 and extract it into *dest*.

    Counterpart to ``tar_push``. *workspace* is the object key without the
    bucket, with or without its ``.tar.zst`` / ``.tar.gz`` suffix (see
    :func:`tar_object`). The archive is fetched as concurrent ranged parts
    into a staging file beside *dest*, then decompressed (in parallel across
    frames for ``pzstd`` archives) into tar; the volume must hold the packed
    archive and the unpacked tree at once.
    """
    env = _s3_env(storage_slug)
    key, codec = tar_object(workspace, compression)
    s3_key = f"s3://{bucket}/{key}"
    decompressor = _ensure_decompressor(session, codec)
    staged = tar_staging_path(codec)

    session.exec(f"mkdir -p '{dest}'", stream=False)

    rc = _s5cmd_transfer(
        session,
        f"Downloading {s3_key}",
        f"{env} s5cmd cp --show-progress "
        f"--concurrency {_CP_CONCURRENCY} --part-size {_CP_PART_MIB} "
        f"'{s3_key}' {staged}",
        force=force,
    )
    if rc != 0:
        raise RuntimeError("Tarball download failed")

    _, tar_size, _ = session.exec(
        f"ls -lh {staged} 2>/dev/null | awk '{{print $5}}'",
        stream=False,
    )
    console.print(f"  [dim]Tarball: {tar_size.strip() or '?'} — extracting[/dim]")

    extract_rc = _s5cmd_transfer(
        session,
        f"Extracting → {dest}/",
        f"bash -c {_sh_quote(tar_extract_pipeline(decompressor, staged, dest))}",
        force=False,
    )
    if extract_rc != 0:
        raise RuntimeError("Tarball extraction failed")

    session.exec(f"rm -f {staged}", stream=False)

    restore_permissions(session, dest)
    _repair_framework_links(session, dest)

    session.exec(
        f": > {WATCH_LOG} 2>/dev/null; touch {PUSH_STAMP}", stream=False,
    )
    if start_watcher(session, dest):
        console.print("  [dim]Watcher started for change tracking[/dim]")
