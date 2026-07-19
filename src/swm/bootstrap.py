"""Bootstrap scripts for setting up remote GPU instances with storage and tools."""

from __future__ import annotations

import subprocess

from rich.console import Console

from swm.remote.ssh import RemoteSession

console = Console()

S5CMD_VERSION = "2.3.0"
S5CMD_URL = (
    f"https://github.com/peak/s5cmd/releases/download/v{S5CMD_VERSION}/"
    f"s5cmd_{S5CMD_VERSION}_Linux-64bit.tar.gz"
)

SAFETY_MARGIN = 0.90

# ── workspace-owned Python toolchain ─────────────────────────────────
#
# Everything under /workspace/ so a pulled workspace is fully runnable
# on any pod image — no apt install, no host python dependency.

UV_VERSION = "0.11.16"
UV_LINUX_X86_64_SHA256 = (
    "74947fe2c03315cf07e82ab3acc703eddef01aba4d5232a98e4c6825ec116131"
)
UV_LINUX_X86_64_URL = (
    f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
    "uv-x86_64-unknown-linux-gnu.tar.gz"
)

WORKSPACE_UV_ROOT = "/workspace/.uv"
WORKSPACE_UV = f"{WORKSPACE_UV_ROOT}/uv"
WORKSPACE_UV_CACHE = "/workspace/.uv-cache"
WORKSPACE_PY_ROOT = "/workspace/.python"

# The minor we install by default.  uv resolves the latest matching patch
# release from python-build-standalone.  Frameworks should pass a fully
# pinned spec (``3.11``) — we deliberately do not pin patch versions so
# uv keeps Python current within a minor without swm releases.
PYTHON_DEFAULT_MINOR = "3.11"

# Shell exports that every uv invocation (and every venv whose Python is
# uv-managed) needs to see so it finds the workspace-local uv cache and
# python install dir.  Used inside framework ``env_setup`` and the
# bootstrap helpers below.
UV_ENV_EXPORTS = (
    f"export UV_CACHE_DIR={WORKSPACE_UV_CACHE} && "
    f"export UV_PYTHON_INSTALL_DIR={WORKSPACE_PY_ROOT} && "
    f"export UV_NO_MANAGED_PYTHON_DOWNLOAD=0 && "
    f"export PATH={WORKSPACE_UV_ROOT}:$PATH"
)


def _humanize(n: int | float) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024:
            return f"{v:.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v:.1f} PB"


def _step(session: RemoteSession, label: str, command: str) -> tuple[int, str, str]:
    """Run a labelled step on the remote, streaming output to the terminal."""
    console.print(f"\n[bold cyan]▸ {label}[/bold cyan]")
    code, stdout, stderr = session.exec(command, stream=True)
    if code != 0:
        raise RuntimeError(f"Step failed (exit {code}): {label}")
    return code, stdout, stderr


# ── s5cmd ──────────────────────────────────────────────────────────


def _s3_env(storage_slug: str) -> str:
    """Build env-var prefix for s5cmd from swm config."""
    from swm import config as cfg

    if storage_slug == "b2":
        endpoint = cfg.get("b2.s3_endpoint") or ""
        ak = cfg.get("b2.key_id") or ""
        sk = cfg.get("b2.app_key") or ""
    elif storage_slug == "gcs":
        endpoint = "https://storage.googleapis.com"
        ak = cfg.get("gcs.hmac_access") or ""
        sk = cfg.get("gcs.hmac_secret") or ""
    elif storage_slug == "s3":
        endpoint = cfg.get("s3.endpoint_url") or ""
        ak = cfg.get("s3.access_key") or ""
        sk = cfg.get("s3.secret_key") or ""
    else:
        raise ValueError(f"Unknown storage slug: {storage_slug}")

    region = str(cfg.get("aws.region") or "")
    parts = [
        f"AWS_ACCESS_KEY_ID='{ak}'",
        f"AWS_SECRET_ACCESS_KEY='{sk}'",
    ]
    if endpoint:
        parts.append(f"S3_ENDPOINT_URL='{endpoint}'")
    if region:
        parts.append(f"AWS_REGION='{region}'")
    return " ".join(parts)


def install_s5cmd(session: RemoteSession) -> None:
    """Download the s5cmd static binary if not already present."""
    _step(
        session,
        "Installing s5cmd",
        f"command -v s5cmd >/dev/null 2>&1 && echo 's5cmd already installed' || "
        f"(curl -sL '{S5CMD_URL}' | tar xz -C /usr/local/bin s5cmd "
        f"&& chmod +x /usr/local/bin/s5cmd && s5cmd version)",
    )


# ── workspace-owned Python (uv + python-build-standalone) ──────────


def ensure_uv(session: RemoteSession) -> None:
    """Install or verify the workspace-local uv binary.

    Idempotent.  Pinned by version + sha256 so a compromised release
    cannot silently land on the pod.  Extracts the single binary to
    ``/workspace/.uv/uv`` and leaves uv's own ``~/.local/bin`` install
    path untouched.
    """
    cmd = (
        f"set -e; "
        f"mkdir -p {WORKSPACE_UV_ROOT} {WORKSPACE_UV_CACHE} {WORKSPACE_PY_ROOT}; "
        f"if [ -x {WORKSPACE_UV} ]; then "
        f"  current=$({WORKSPACE_UV} --version 2>/dev/null | head -1 | awk '{{print $2}}'); "
        f"  if [ \"$current\" = \"{UV_VERSION}\" ]; then "
        f"    echo \"uv {UV_VERSION} already installed\"; "
        f"    exit 0; "
        f"  fi; "
        f"  echo \"uv $current present — replacing with {UV_VERSION}\"; "
        f"fi; "
        f"tmp=$(mktemp -d); "
        f"trap 'rm -rf \"$tmp\"' EXIT; "
        f"echo \"Downloading uv {UV_VERSION}...\"; "
        f"curl -fsSL \"{UV_LINUX_X86_64_URL}\" -o \"$tmp/uv.tar.gz\"; "
        f"echo \"{UV_LINUX_X86_64_SHA256}  $tmp/uv.tar.gz\" | sha256sum -c -; "
        f"tar -xzf \"$tmp/uv.tar.gz\" -C \"$tmp\"; "
        f"install -m 0755 \"$tmp\"/uv-*/uv {WORKSPACE_UV}; "
        f"{WORKSPACE_UV} --version"
    )
    _step(session, f"Installing uv {UV_VERSION}", cmd)


def _python_link_repair_script(minor: str) -> str:
    """Bash that restores uv's minor-version symlink when a workspace sync
    materialized it into a real directory.

    uv lays out ``/workspace/.python/cpython-<x.y.z>-<platform>/`` plus a
    ``cpython-<x.y>-<platform>`` symlink pointing at it. s5cmd-based workspace
    push/pull follows symlinks, so after a restore onto a fresh pod the link
    comes back as a second full copy of the install. ``uv python install``
    then fails with "Is a directory (os error 21)" trying to recreate its
    link. Detect that shape and swap the copy back to a symlink (idempotent;
    a healthy layout is untouched).
    """
    # Subshell so the early exit (no .python dir yet — fresh pod) cannot
    # abort the caller's chained uv install.
    return (
        '( cd /workspace/.python 2>/dev/null || exit 0; '
        f'for d in cpython-{minor}-*; do '
        '  [ -d "$d" ] && [ ! -L "$d" ] || continue; '
        f'  plat="${{d#cpython-{minor}-}}"; '
        f'  full=$(ls -d cpython-{minor}.*-"$plat" 2>/dev/null | sort -V | tail -1); '
        '  if [ -n "$full" ] && [ -x "$full/bin/python3" ]; then '
        '    rm -rf "$d" && ln -s "$full" "$d" '
        '    && echo "Repaired materialized python link: $d -> $full"; '
        '  fi; '
        'done )'
    )


def ensure_python(
    session: RemoteSession,
    minor: str = PYTHON_DEFAULT_MINOR,
) -> None:
    """Install/verify a workspace-managed CPython via uv.

    Delegates to ``uv python install``, which fetches the matching
    python-build-standalone tarball into ``/workspace/.python/``.
    Idempotent — uv reports the install as already-present on reruns.
    Self-heals a symlink that a workspace sync materialized into a real
    directory before invoking uv (see _python_link_repair_script).
    """
    cmd = (
        f"{_python_link_repair_script(minor)} && "
        f"{UV_ENV_EXPORTS} && "
        f"{WORKSPACE_UV} python install {minor} && "
        f"{WORKSPACE_UV} python find {minor}"
    )
    _step(session, f"Installing Python {minor} (workspace-owned)", cmd)


def ensure_workspace_python(
    session: RemoteSession,
    minor: str = PYTHON_DEFAULT_MINOR,
) -> None:
    """Ensure uv + a managed CPython are both present under /workspace/.

    Call this once per pod before any framework venv-creation step.
    After this returns, ``uv venv --python <minor> <path>`` will create
    a venv whose interpreter lives entirely inside ``/workspace/`` and
    survives a workspace pull onto a different pod image.
    """
    ensure_uv(session)
    ensure_python(session, minor=minor)


def repair_venv(
    session: RemoteSession,
    venv_path: str,
    minor: str = PYTHON_DEFAULT_MINOR,
) -> None:
    """Rebind an existing venv to workspace-owned Python if it can't run here.

    This is the backward-compat path for venvs that were created against
    the host's system Python on a previous pod (e.g. pulled from B2 onto a
    new image that no longer has the matching ``/usr/lib/pythonX.Y/``).
    Such a venv fails with ``ModuleNotFoundError: No module named 'encodings'``
    because Python looks for stdlib at the absent host prefix.

    The repair rewrites ``pyvenv.cfg`` to point at the uv-managed CPython
    under ``/workspace/.python`` and replaces ``bin/python*`` with the new
    interpreter binary.  Site-packages are preserved — CPython keeps a
    stable ABI across same-minor patch releases, so wheels installed under
    3.11.10 continue to import under 3.11.15.

    Idempotent: no-op when the venv already runs.  Raises if the existing
    venv is a different minor than the workspace-owned one — that's a
    full rebuild case (``swm setup install <framework>``).
    """
    script = f"""
set -e
{UV_ENV_EXPORTS}

VENV="{venv_path}"
WANTED_MINOR="{minor}"

if [ ! -d "$VENV" ]; then
    echo "  no venv at $VENV — nothing to repair"
    exit 0
fi
if [ -x "$VENV/bin/python" ] \\
   && "$VENV/bin/python" -c "import encodings, sys" >/dev/null 2>&1; then
    echo "  venv $VENV already runs (Python $("$VENV/bin/python" --version 2>&1 | awk '{{print $2}}'))"
    exit 0
fi

echo "  venv at $VENV not runnable here — rebinding to workspace-owned Python $WANTED_MINOR"

UV_PY=$({WORKSPACE_UV} python find "$WANTED_MINOR")
UV_HOME=$(dirname "$UV_PY")
UV_VER=$("$UV_PY" --version 2>&1 | awk '{{print $2}}')
UV_MINOR=$(echo "$UV_VER" | cut -d. -f1,2)

EXISTING_MINOR=""
if [ -f "$VENV/pyvenv.cfg" ]; then
    EXISTING_VER=$(awk -F'= *' '/^version *=/{{print $2; exit}}' "$VENV/pyvenv.cfg" | tr -d ' ')
    if [ -n "$EXISTING_VER" ]; then
        EXISTING_MINOR=$(echo "$EXISTING_VER" | cut -d. -f1,2)
    fi
fi

if [ -n "$EXISTING_MINOR" ] && [ "$UV_MINOR" != "$EXISTING_MINOR" ]; then
    echo "  ERROR: existing venv is Python $EXISTING_MINOR but workspace-owned Python is $UV_MINOR."
    echo "         C-extension ABI does not match across Python minors."
    echo "         Rebuild from scratch with:  swm setup install <framework>"
    exit 1
fi

echo "  Rewriting $VENV/pyvenv.cfg  (home → $UV_HOME, version → $UV_VER)"
cat > "$VENV/pyvenv.cfg" <<PYVENV
home = $UV_HOME
include-system-site-packages = false
version = $UV_VER
executable = $UV_PY
command = swm repair_venv ($VENV)
PYVENV

PY_MINOR_BIN="python$WANTED_MINOR"
rm -f "$VENV/bin/python" "$VENV/bin/python3" "$VENV/bin/$PY_MINOR_BIN"
# python-build-standalone resolves its stdlib via realpath(argv[0]),
# so the binary must remain at its uv-managed install path. Symlink,
# never copy, otherwise Python falls back to its baked-in /install
# prefix and fails with "No module named 'encodings'".
ln -s "$UV_PY" "$VENV/bin/$PY_MINOR_BIN"
ln -s "$PY_MINOR_BIN" "$VENV/bin/python3"
ln -s "$PY_MINOR_BIN" "$VENV/bin/python"

"$VENV/bin/python" -c "import encodings, sys; print('  repaired:', sys.executable, '→ Python', sys.version.split()[0])"
"""
    _step(session, f"Checking venv {venv_path}", script)


def _install_inotify(session: RemoteSession) -> None:
    """Install inotify-tools for the filesystem change watcher."""
    _step(
        session,
        "Installing inotify-tools",
        "command -v inotifywait >/dev/null 2>&1 && echo 'inotify-tools already installed' || "
        "(apt-get update -qq && apt-get install -y -qq inotify-tools && echo 'installed')",
    )


def configure_storage(
    session: RemoteSession, storage_slug: str, bucket: str = "",
) -> None:
    """Install s5cmd, inotify-tools, and verify the S3-compatible connection."""
    install_s5cmd(session)
    try:
        _install_inotify(session)
    except RuntimeError:
        console.print("  [yellow]⚠ inotify-tools install failed — push will use find[/yellow]")
    env = _s3_env(storage_slug)
    target = f"s3://{bucket}/" if bucket else ""
    _step(
        session,
        f"Verifying {storage_slug} connection",
        f"{env} s5cmd ls {target} 2>&1 | head -5 || true",
    )


_WS_MARKER_NAMES = (
    ".swm_last_push",
    ".swm_changes.log",
    ".swm_workspace.tar.gz",
    ".swm_autosync.log",
    ".swm_guard",
    ".swm_watcher.pid",
)


def _workspace_leftover_files(session: RemoteSession) -> list[str]:
    """Return paths in /workspace/ that aren't swm markers.

    Non-marker files commonly appear because docker images use
    /workspace as a default WORKDIR and seed it with content (e.g. the
    pytorch base image). Callers decide how to handle these — usually
    by uploading them as the initial baseline rather than failing.
    """
    excludes = " ".join(f"-not -name '{n}'" for n in _WS_MARKER_NAMES)
    cmd = (
        f"find /workspace -mindepth 1 -maxdepth 1 {excludes} 2>/dev/null"
    )
    _, out, _ = session.exec(cmd, stream=False)
    return [line for line in out.splitlines() if line.strip()]


def _ensure_workspace_empty_on_pod(session: RemoteSession) -> None:
    """Raise if /workspace/ on the pod contains any non-marker files.

    Retained for callers that want the strict behavior. Most call sites
    should prefer ``_workspace_leftover_files`` and seed-by-pushing.
    """
    leftover = _workspace_leftover_files(session)
    if leftover:
        sample = ", ".join(p.rsplit("/", 1)[-1] for p in leftover[:3])
        more = "" if len(leftover) <= 3 else f" (+ more)"
        raise RuntimeError(
            f"/workspace/ on the pod is not empty (e.g. {sample}{more}). "
            "Refusing to mark this as a fresh workspace because those "
            "files would not be uploaded. Either: (a) upload them first "
            "with `swm sync push <pod> -b <provider:bucket> -d <name> "
            "--force`, then re-run setup; or (b) clear /workspace/ on "
            "the pod and re-run; or (c) re-run with an existing "
            "workspace name to pull from storage."
        )


def bootstrap_workspace_on_pod(
    session: RemoteSession,
    storage_slug: str,
    bucket: str,
    workspace: str,
    *,
    qualified_id: str,
    is_new: bool,
    extra_excludes: list[str] | None = None,
    autosync_interval: int = 60,
    console_obj: Console | None = None,
) -> list[tuple[str, str]]:
    """Configure storage, pull (or watcher-init), and start auto-sync on a pod.

    Returns a list of ``(label, recovery_command)`` tuples for any sub-step
    that failed. An empty list means full success.
    """
    from swm.sync import start_watcher
    from swm.sync.autosync import AutosyncUnsafeError, start_autosync
    from swm.sync.paths import PUSH_STAMP, WATCH_LOG
    from swm.sync.pull import workspace_pull

    _con = console_obj or console
    failed: list[tuple[str, str]] = []

    def _fail(label: str, cmd: str, exc: Exception) -> None:
        _con.print(f"  [yellow]⚠ {label} failed: {exc}[/yellow]")
        failed.append((label, cmd))

    storage_ok = False
    try:
        with _con.status(
            "Installing s5cmd & configuring storage…", spinner="dots"
        ):
            configure_storage(session, storage_slug, bucket=bucket)
        _con.print("[green]✓[/green] Storage configured")
        storage_ok = True
    except Exception as exc:
        _fail("Storage configuration", f"swm setup storage {qualified_id}", exc)

    pull_ok = False
    if storage_ok:
        try:
            if is_new:
                # Docker images commonly seed /workspace with content
                # (e.g. pytorch base image). Rather than fail bootstrap
                # and leave the pod without autosync — the historical
                # failure mode that caused silent data loss — seed the
                # new workspace by uploading whatever is already on the
                # pod as the initial baseline.
                leftover = _workspace_leftover_files(session)
                if leftover:
                    sample = ", ".join(
                        p.rsplit("/", 1)[-1] for p in leftover[:3]
                    )
                    more = (
                        "" if len(leftover) <= 3
                        else f" (+ {len(leftover) - 3} more)"
                    )
                    _con.print(
                        f"  [yellow]/workspace contains existing files "
                        f"(e.g. {sample}{more}) — uploading them as the "
                        f"new workspace baseline before starting "
                        f"auto-sync.[/yellow]"
                    )
                    from swm.sync.push import workspace_push
                    rc = workspace_push(
                        session, storage_slug, bucket, workspace,
                        extra_excludes=extra_excludes, force=True,
                    )
                    if rc != 0:
                        raise RuntimeError(
                            f"Initial baseline push failed (s5cmd exit "
                            f"{rc}). Re-run `swm sync push "
                            f"{qualified_id} --force` then "
                            f"`swm sync auto {qualified_id}`."
                        )
                else:
                    _con.print("  [dim]New workspace — skipping pull[/dim]")
                    session.exec(
                        f": > {WATCH_LOG} 2>/dev/null; touch {PUSH_STAMP}",
                        stream=False,
                    )
                    if start_watcher(session, "/workspace"):
                        _con.print(
                            "  [dim]Watcher started for change tracking[/dim]"
                        )
            else:
                workspace_pull(
                    session, storage_slug, bucket, workspace,
                    extra_excludes=extra_excludes,
                )
            pull_ok = True
        except Exception as exc:
            _fail("Workspace pull", f"swm sync pull {qualified_id}", exc)
    else:
        failed.append(("Workspace pull", f"swm sync pull {qualified_id}"))

    if pull_ok:
        try:
            if start_autosync(
                session, storage_slug, bucket, workspace,
                interval=autosync_interval,
            ):
                _con.print(
                    "  [dim]Auto-sync started "
                    f"(every {autosync_interval}s → "
                    f"{storage_slug}:{bucket}/{workspace})[/dim]"
                )
        except Exception as exc:
            _fail("Auto-sync start", f"swm sync auto {qualified_id}", exc)
    else:
        failed.append(("Auto-sync start", f"swm sync auto {qualified_id}"))

    return failed


def _acquire_transfer_lock(session: RemoteSession, force: bool = False) -> None:
    """Check for an existing transfer and acquire the lock.

    If a lock exists with a live PID, raises unless *force* is True
    (which kills the stale process first).
    """
    from swm.sync.paths import TRANSFER_LOCK

    code, out, _ = session.exec(
        f"cat {TRANSFER_LOCK} 2>/dev/null", stream=False,
    )
    old_pid = out.strip()

    if old_pid:
        _, alive, _ = session.exec(
            f"kill -0 {old_pid} 2>/dev/null && echo alive || echo dead",
            stream=False,
        )
        if "alive" in alive:
            if not force:
                raise RuntimeError(
                    f"A transfer is already running (PID {old_pid}). "
                    "Use --force to kill it and start a new one."
                )
            console.print(
                f"  [yellow]⚠ Killing existing transfer (PID {old_pid})[/yellow]"
            )
            session.exec(f"kill -9 {old_pid} 2>/dev/null; sleep 1", stream=False)

        # Stale lock — clean up temp files left behind
        console.print("  [dim]Cleaning up stale temp files…[/dim]")
        _, cleanup_out, _ = session.exec(
            "find /workspace -maxdepth 5 -type f -regex '.*\\.[a-z]*[0-9]\\{9,\\}$' "
            "-delete -print 2>/dev/null | wc -l",
            stream=False,
        )
        n = cleanup_out.strip()
        if n and n != "0":
            console.print(f"  [dim]Removed {n} orphaned temp files[/dim]")

    session.exec(f"echo $$ > {TRANSFER_LOCK}", stream=False)


def _s5cmd_transfer(
    session: RemoteSession,
    label: str,
    s5cmd_cmd: str,
    force: bool = False,
) -> int:
    """Run an s5cmd transfer with output streamed directly to the terminal.

    Acquires a lock file on the pod, wraps the command in a shell trap
    for guaranteed cleanup (even on SSH disconnect), and streams
    s5cmd's native ``--show-progress`` output to the terminal.

    Returns the process exit code.
    """
    from swm.sync.paths import TRANSFER_LOCK

    console.print(f"\n[bold cyan]▸ {label}[/bold cyan]")
    _acquire_transfer_lock(session, force=force)

    wrapped = (
        f"trap 'rm -f {TRANSFER_LOCK}' EXIT; "
        f"echo $$ > {TRANSFER_LOCK}; "
        f"{s5cmd_cmd}"
    )
    cmd = session._ssh_cmd() + [wrapped]
    code = subprocess.call(cmd)

    if code != 0:
        console.print(f"  [yellow]⚠ Transfer finished with warnings (exit {code})[/yellow]")
    else:
        console.print(f"  [green]✓ {label} — done[/green]")

    return code


# Backward-compatible re-exports (lazy to avoid circular imports)
_RE_EXPORTS: dict[str, str] = {
    "install_framework": "swm.bootstrap_frameworks",
    "start_framework": "swm.bootstrap_frameworks",
    "stop_framework": "swm.bootstrap_frameworks",
    "link_models_to_comfyui": "swm.bootstrap_frameworks",
    "wait_for_ssh": "swm.bootstrap_ssh",
    "next_workspace_name": "swm.bootstrap_ssh",
    "DiskCheck": "swm.sync",
    "preflight_check": "swm.sync",
    "start_watcher": "swm.sync",
    "stop_watcher": "swm.sync",
    "is_watcher_alive": "swm.sync",
    "workspace_pull": "swm.sync",
    "workspace_push": "swm.sync",
    "tar_pull": "swm.sync",
    "start_autosync": "swm.sync",
    "stop_autosync": "swm.sync",
    "is_autosync_alive": "swm.sync",
    "autosync_status": "swm.sync",
}


def __getattr__(name: str):  # noqa: E302
    if name in _RE_EXPORTS:
        import importlib
        mod = importlib.import_module(_RE_EXPORTS[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
