"""Shared helpers used by pull and push: permissions, compressor, hardlink staging."""

from __future__ import annotations

from swm.bootstrap import _privileged, _step
from swm.remote.ssh import RemoteSession
from swm.sync.paths import STAGING


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


def stage_hardlinks(session: RemoteSession, filelist: str, src: str) -> None:
    """Create a staging tree of hardlinks for only the changed files.

    Each file in *filelist* (absolute paths under *src*) gets a hardlink
    in ``STAGING`` preserving relative directory structure.  Hardlinks
    are instant and use no extra disk space.
    """
    session.exec(f"rm -rf {STAGING}", stream=False)
    session.exec(
        f"while IFS= read -r f; do "
        f"  rel=\"${{f#{src}/}}\"; "
        f"  mkdir -p \"{STAGING}/$(dirname \"$rel\")\"; "
        f"  ln \"$f\" \"{STAGING}/$rel\" 2>/dev/null "
        f"    || cp \"$f\" \"{STAGING}/$rel\"; "
        f"done < {filelist}",
        stream=False,
    )
