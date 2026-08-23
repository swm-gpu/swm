"""Shared helpers used by pull and push: permissions, compressor, hardlink staging."""

from __future__ import annotations

import shlex

from swm.bootstrap import _privileged, _step
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


def clear_staged_files(session: RemoteSession, staging: str) -> None:
    """Delete staged files but keep the directory skeleton.

    The staging dirs are deliberately persistent: deleting them would emit
    bare-path inotify events that evade the slash-anchored excludes and
    poison delete-reconciliation with nonexistent S3 keys.
    """
    q = shlex.quote(staging)
    session.exec(
        f"[ -d {q} ] && find {q} -type f -delete 2>/dev/null; true",
        stream=False,
    )


def stage_hardlinks(session: RemoteSession, filelist: str, src: str) -> str:
    """Create a staging tree of hardlinks for only the changed files.

    Each file in *filelist* (absolute paths under *src*) gets a hardlink
    in a persistent staging dir **inside** *src* — same filesystem, so
    links are instant and use no extra disk space. Files that vanished
    since the scan are skipped (they surface as deletions next cycle).
    A link failure on an existing file aborts: silently falling back to
    ``cp`` used to duplicate the workspace onto the container overlay
    and could upload partial files as corrupt objects.

    Returns the staging directory path. Raises ``RuntimeError`` if
    staging could not be completed.
    """
    staging = staging_dir_for(src)
    q = shlex.quote(staging)
    clear_staged_files(session, staging)
    exit_code, out, _ = session.exec(
        f"mkdir -p {q} && fail=0; "
        f"while IFS= read -r f; do "
        f"  [ -f \"$f\" ] || continue; "
        f"  rel=\"${{f#{src}/}}\"; "
        f"  mkdir -p \"{staging}/$(dirname \"$rel\")\" "
        f"    || {{ echo \"SWM_STAGE_FAIL(mkdir): $rel\"; fail=1; break; }}; "
        f"  ln -f \"$f\" \"{staging}/$rel\" "
        f"    || {{ echo \"SWM_STAGE_FAIL(ln): $f\"; fail=1; break; }}; "
        f"done < {shlex.quote(filelist)}; "
        f"exit $fail",
        stream=False,
    )
    if exit_code != 0:
        clear_staged_files(session, staging)
        detail = next(
            (l.strip() for l in out.splitlines() if "SWM_STAGE_FAIL" in l),
            "unknown file",
        )
        raise RuntimeError(
            f"Hardlink staging failed ({detail}). Staging lives inside "
            f"{src} so links never cross filesystems; a failure here "
            f"means the file is unlinkable (permissions, immutable, or "
            f"hardlink limit) and the push was aborted rather than "
            f"silently copying data."
        )
    return staging
