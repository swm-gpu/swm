"""Workspace pull: download from storage to pod (streaming or tarball)."""

from __future__ import annotations

import shlex

from swm.bootstrap import _s3_env, _s5cmd_transfer, console
from swm.remote.ssh import RemoteSession, _sh_quote
from swm.sync._common import ensure_pigz, ensure_zstd, restore_permissions
from swm.sync.paths import PUSH_STAMP, WATCH_LOG
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

# ``s5cmd cat`` downloads ranged parts concurrently and re-orders them for
# stdout, holding out-of-order parts in memory: the window is roughly
# concurrency x part size (1 GiB here), observed to peak at ~2-3x under a
# fast link. GPU hosts have the RAM; a wider window buys little because the
# pipeline is bound by the slower of the link and the extracting disk.
_CAT_CONCURRENCY = 16
_CAT_PART_MIB = 64
# GNU tar emits one heartbeat line per 1 GiB of archive read (102400 records
# of 10 KiB) so a long pull is visibly alive without a progress bar.
_TAR_CHECKPOINT = "--checkpoint=102400 --checkpoint-action=echo"


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


def tar_pull_pipeline(env: str, s3_key: str, decompressor: str, dest: str) -> str:
    """The remote pipeline: ``s5cmd cat | decompress | tar -x`` under bash.

    Download, decompression and extraction overlap, and nothing but the tree
    itself lands on the volume — a pull needs only the unpacked size of disk,
    not unpacked plus packed. Every stage's status is checked: with
    ``pipefail`` alone a truncated download that the decompressor happens to
    tolerate could report success, and the stage statuses are echoed so a
    failure names its stage.
    """
    checkpoint = (
        "CKPT=''; tar --version 2>/dev/null | grep -q GNU && "
        f"CKPT='{_TAR_CHECKPOINT}'; "
    )
    return (
        "set -o pipefail; " + checkpoint
        + f"{env} s5cmd cat --concurrency {_CAT_CONCURRENCY} "
        f"--part-size {_CAT_PART_MIB} '{s3_key}' "
        f"| {decompressor} | tar $CKPT -xf - -C '{dest}'; "
        'pcs=("${PIPESTATUS[@]}"); '
        'echo "pull stages: download=${pcs[0]:-1} decompress=${pcs[1]:-1} '
        'extract=${pcs[2]:-1}"; '
        '[ "${pcs[0]:-1}" -eq 0 ] && [ "${pcs[1]:-1}" -eq 0 ] && '
        '[ "${pcs[2]:-1}" -eq 0 ]'
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
    """Stream a tarball from S3 straight into *dest*.

    Counterpart to ``tar_push``. *workspace* is the object key without the
    bucket, with or without its ``.tar.zst`` / ``.tar.gz`` suffix (see
    :func:`tar_object`). The object is downloaded as concurrent ranged parts
    and fed through the decompressor into tar as it arrives, so the pull
    needs no staging file and takes about as long as the slower of the
    download and the extraction rather than their sum.
    """
    env = _s3_env(storage_slug)
    key, codec = tar_object(workspace, compression)
    s3_key = f"s3://{bucket}/{key}"
    decompressor = _ensure_decompressor(session, codec)

    session.exec(f"mkdir -p '{dest}'", stream=False)

    rc = _s5cmd_transfer(
        session,
        f"Streaming {s3_key} → {dest}/",
        f"bash -c {_sh_quote(tar_pull_pipeline(env, s3_key, decompressor, dest))}",
        force=force,
    )
    if rc != 0:
        raise RuntimeError("Tarball download/extraction failed")

    restore_permissions(session, dest)
    _repair_framework_links(session, dest)

    session.exec(
        f": > {WATCH_LOG} 2>/dev/null; touch {PUSH_STAMP}", stream=False,
    )
    if start_watcher(session, dest):
        console.print("  [dim]Watcher started for change tracking[/dim]")
