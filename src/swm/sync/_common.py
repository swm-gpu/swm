"""Shared helpers used by pull and push: permissions, compressor, hardlink staging."""

from __future__ import annotations

import shlex

from swm.bootstrap import _privileged, _step, console
from swm.remote.ssh import RemoteSession
from swm.sync.paths import staging_dir_for


def restore_permissions(session: RemoteSession, dest: str) -> None:
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


def ensure_pigz(session: RemoteSession, console) -> str:
    """Ensure pigz is available; return 'pigz' or 'gzip' as the compressor name."""
    _, has_pigz, _ = session.exec(
        "command -v pigz >/dev/null 2>&1 && echo yes || echo no",
        stream=False,
    )
    if "yes" in has_pigz:
        return "pigz"

    console.print("  [dim]Installing pigz for parallel compression…[/dim]")
    # Falls back to gzip if this fails, so the sudo probe staying quiet is fine.
    session.exec(_privileged("$SUDO apt-get install -y -qq pigz 2>/dev/null"),
                 stream=False)
    _, has_pigz, _ = session.exec(
        "command -v pigz >/dev/null 2>&1 && echo yes || echo no",
        stream=False,
    )
    return "pigz" if "yes" in has_pigz else "gzip"


def ensure_zstd(session: RemoteSession, console) -> str | None:
    """Ensure a Zstandard tool is available; return ``'pzstd'`` (parallel,
    multi-frame) or ``'zstd'``, or None when neither can be had.

    Unlike gzip there is no universally present fallback, so callers must
    treat None as a hard error rather than silently degrading.
    """
    probe = ("command -v pzstd >/dev/null 2>&1 && echo pzstd || "
             "(command -v zstd >/dev/null 2>&1 && echo zstd || echo no)")
    _, found, _ = session.exec(probe, stream=False)
    found = found.strip()
    if found in ("pzstd", "zstd"):
        return found

    console.print("  [dim]Installing zstd…[/dim]")
    session.exec(_privileged("$SUDO apt-get install -y -qq zstd 2>/dev/null"),
                 stream=False)
    _, found, _ = session.exec(probe, stream=False)
    found = found.strip()
    return found if found in ("pzstd", "zstd") else None


def clear_staged_files(session: RemoteSession, staging: str) -> None:
    """Delete staged files but keep the directory skeleton.

    The staging dirs are deliberately persistent: deleting them would emit
    bare-path inotify events that evade the slash-anchored excludes and
    poison delete-reconciliation with nonexistent S3 keys.
    """
    q = shlex.quote(staging)
    session.exec(
        f"[ -d {q} ] && find {q} \\( -type f -o -type l \\) -delete 2>/dev/null; true",
        stream=False,
    )


PUSH_IN_PLACE = "/tmp/.swm_push_in_place"


def stage_hardlinks(
    session: RemoteSession, filelist: str, src: str, in_place: str = PUSH_IN_PLACE,
) -> str:
    """Create a staging tree of hardlinks for only the changed files.

    Each file in *filelist* (absolute paths under *src*) gets a hardlink
    in a persistent staging dir **inside** *src* — same filesystem, so
    links are instant and use no extra disk space. Files that vanished
    since the scan are skipped (they surface as deletions next cycle).
    A link failure on an existing file aborts: silently falling back to
    ``cp`` used to duplicate the workspace onto the container overlay
    and could upload partial files as corrupt objects.

    The one exception is a link the volume's quota refuses. Some network
    volumes (MooseFS) charge every hardlink its file's full size, so a
    large new file that fits on the volume once cannot be staged. Those
    are written to *in_place* as ``<file>\\t<relative key>`` lines for the
    caller to upload from where they live. The list sits on the container
    overlay because, with the quota exhausted, writes under *src* fail too.

    A symlink is never linked as itself: GNU ``ln`` would stage the link
    inode, whose relative target dangles inside the staging tree, and
    s5cmd (which follows symlinks) then aborts the walk of that whole
    top-level entry. Instead the resolved target file is linked under the
    link's name — the same object a follow-symlinks upload produced — and
    links that resolve to nothing, to a directory, or across filesystems
    are skipped and counted.

    Returns the staging directory path. Raises ``RuntimeError`` if
    staging could not be completed.
    """
    staging = staging_dir_for(src)
    q = shlex.quote(staging)
    qi = shlex.quote(in_place)
    clear_staged_files(session, staging)
    exit_code, out, _ = session.exec(
        f"mkdir -p {q} && : > {qi} && fail=0; links=0; skipped=0; "
        f"while IFS= read -r f; do "
        f"  if [ -L \"$f\" ]; then "
        f"    t=$(readlink -f -- \"$f\" 2>/dev/null); "
        f"    if [ -z \"$t\" ] || [ ! -f \"$t\" ] || [ -L \"$t\" ]; then "
        f"      skipped=$((skipped+1)); continue; fi; "
        f"  elif [ -f \"$f\" ]; then t=\"$f\"; "
        f"  else continue; fi; "
        f"  rel=\"${{f#{src}/}}\"; "
        f"  mkdir -p \"{staging}/$(dirname \"$rel\")\" "
        f"    || {{ echo \"SWM_STAGE_FAIL(mkdir): $rel\"; fail=1; break; }}; "
        f"  if err=$(LC_ALL=C ln -f -- \"$t\" \"{staging}/$rel\" 2>&1); then "
        f"    [ -L \"$f\" ] && links=$((links+1)); "
        f"  else "
        f"    case \"$err\" in "
        f"      *'Disk quota exceeded'*) printf '%s\\t%s\\n' \"$t\" \"$rel\" >> {qi} ;; "
        f"      *) if [ -L \"$f\" ]; then skipped=$((skipped+1)); "
        f"         else echo \"SWM_STAGE_FAIL(ln): $f: $err\"; fail=1; break; fi ;; "
        f"    esac; "
        f"  fi; "
        f"done < {shlex.quote(filelist)}; "
        f"echo \"SWM_STAGE_LINKS: $links $skipped\"; "
        f"exit $fail",
        stream=False,
    )
    links = skipped = 0
    for line in out.splitlines():
        if line.startswith("SWM_STAGE_LINKS:"):
            links, skipped = (int(n) for n in line.split()[1:3])
    if links:
        console.print(f"  [dim]{links} symlink(s) materialised as their target files[/dim]")
    if skipped:
        console.print(
            f"  [dim]skipped {skipped} symlink(s) that cannot be materialised "
            f"(dangling, directory, or cross-device)[/dim]"
        )
    if exit_code != 0:
        clear_staged_files(session, staging)
        detail = next(
            (l.strip() for l in out.splitlines() if "SWM_STAGE_FAIL" in l),
            "unknown file",
        )
        raise RuntimeError(
            f"Hardlink staging failed ({detail}). The push was aborted "
            f"rather than silently copying data."
        )
    return staging
