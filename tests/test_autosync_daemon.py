"""Behavioural tests for the rendered on-pod autosync daemon.

The daemon is a bash script deployed over SSH; here it is rendered with
``_render_daemon_script`` and run against a temp workspace with a fake
``s5cmd`` first on PATH. The shim records its argv, can sleep, can fail with
an s5cmd-style error line, and dumps the staging tree (symlink flag + inode)
at upload time so materialisation can be asserted exactly.

``SWM_AUTOSYNC_ONCE=1`` makes the script run one health check + one cycle
and exit instead of looping.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from swm.sync.autosync import _render_daemon_script
from swm.sync.paths import AUTOSYNC_FAIL_STREAK, TRANSFER_LOCK_HOLDER_TAG

LINUX = sys.platform == "linux"
linux_only = pytest.mark.skipif(
    not LINUX, reason="needs /proc/<pid>/cmdline, GNU xargs -a or readlink -f",
)

_SHIM = r'''#!/bin/bash
printf '%s\n' "$*" >> "$SWM_SHIM_ARGV"
if [ -n "${SWM_SHIM_SNAPSHOT:-}" ] && [ -d "${SWM_SHIM_STAGING:-}" ]; then
  __PYTHON__ - "$SWM_SHIM_STAGING" >> "$SWM_SHIM_SNAPSHOT" <<'EOF'
import json, os, sys
root = sys.argv[1]
out = {}
for dp, dns, fns in os.walk(root):
    for n in fns + [d for d in dns if os.path.islink(os.path.join(dp, d))]:
        p = os.path.join(dp, n)
        out[os.path.relpath(p, root)] = {"islink": os.path.islink(p), "ino": os.lstat(p).st_ino}
print(json.dumps(out))
EOF
fi
[ -n "${SWM_SHIM_SLEEP:-}" ] && sleep "$SWM_SHIM_SLEEP"
if [ "${SWM_SHIM_RC:-0}" != 0 ]; then
  echo "ERROR \"cp $*\": given object not found"
fi
[ -n "${SWM_SHIM_DONE:-}" ] && echo done >> "$SWM_SHIM_DONE"
exit "${SWM_SHIM_RC:-0}"
'''


def _age(path: Path, seconds: int) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


@dataclass
class Daemon:
    tmp: Path
    ws: Path
    scratch: Path
    script: Path
    env: dict[str, str]

    @property
    def watch_log(self) -> Path:
        return self.ws / ".swm_changes.log"

    @property
    def push_stamp(self) -> Path:
        return self.ws / ".swm_last_push"

    @property
    def auto_log(self) -> Path:
        return self.ws / ".swm_autosync.log"

    @property
    def marker(self) -> Path:
        return self.ws / ".swm_autosync.failing"

    @property
    def lock(self) -> Path:
        return self.scratch / ".swm_transfer.lock"

    @property
    def counter(self) -> Path:
        return self.scratch / ".swm_autosync_failstreak"

    @property
    def staging(self) -> Path:
        return self.ws / ".swm_staging" / "autosync"

    @property
    def watcher_script(self) -> Path:
        return self.scratch / ".swm_start_watcher.sh"

    def run_once(
        self, *, rc: int = 0, shim_sleep: str | None = None, timeout: int = 60,
    ) -> subprocess.CompletedProcess:
        env = dict(self.env, SWM_AUTOSYNC_ONCE="1", SWM_SHIM_RC=str(rc))
        if shim_sleep:
            env["SWM_SHIM_SLEEP"] = shim_sleep
        return subprocess.run(
            ["bash", str(self.script)], env=env, capture_output=True,
            text=True, timeout=timeout, check=False,
        )

    def log_text(self) -> str:
        return self.auto_log.read_text() if self.auto_log.exists() else ""

    def argv_lines(self) -> list[str]:
        p = Path(self.env["SWM_SHIM_ARGV"])
        return p.read_text().splitlines() if p.exists() else []

    def snapshots(self) -> list[dict]:
        p = Path(self.env["SWM_SHIM_SNAPSHOT"])
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def staged_entries(self) -> list[Path]:
        out: list[Path] = []
        for dp, dns, fns in os.walk(self.staging):
            for n in fns + [d for d in dns if os.path.islink(os.path.join(dp, d))]:
                out.append(Path(dp) / n)
        return out


@pytest.fixture
def daemon(tmp_path: Path) -> Daemon:
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    shim_dir = tmp_path / "bin"
    for d in (ws, scratch, shim_dir):
        d.mkdir()

    shim = shim_dir / "s5cmd"
    shim.write_text(_SHIM.replace("__PYTHON__", sys.executable))
    shim.chmod(0o755)

    body = _render_daemon_script("b2", "bucket", "ws", str(ws), interval=1)
    # Everything the script keys off /tmp/.swm_* or /workspace/ moves into
    # the tmpdir so tests are hermetic and can run in parallel.
    body = body.replace("/tmp/.swm_", f"{scratch}/.swm_").replace("/workspace/", f"{ws}/")
    script = scratch / ".swm_autosync.sh"
    script.write_text(body)
    script.chmod(0o755)

    env_file = scratch / ".swm_autosync.env"
    env_file.write_text(
        "export AWS_ACCESS_KEY_ID='test'\nexport AWS_SECRET_ACCESS_KEY='test'\n",
    )
    env_file.chmod(0o600)

    stamp = ws / ".swm_last_push"
    stamp.touch()
    _age(stamp, 3600)
    (ws / ".swm_changes.log").touch()

    env = dict(os.environ)
    env.update(
        PATH=f"{shim_dir}:{env['PATH']}",
        SWM_SHIM_ARGV=str(tmp_path / "shim_argv"),
        SWM_SHIM_SNAPSHOT=str(tmp_path / "shim_snapshot"),
        SWM_SHIM_STAGING=str(ws / ".swm_staging" / "autosync"),
        SWM_SHIM_DONE=str(tmp_path / "shim_done"),
    )
    env.pop("SWM_AUTOSYNC_ONCE", None)
    env.pop("SWM_SHIM_RC", None)
    env.pop("SWM_SHIM_SLEEP", None)
    return Daemon(tmp=tmp_path, ws=ws, scratch=scratch, script=script, env=env)


def _cp_lines(d: Daemon) -> list[str]:
    return [line for line in d.argv_lines() if " cp " in f" {line} "]


def _rm_lines(d: Daemon) -> list[str]:
    return [line for line in d.argv_lines() if " rm " in f" {line} "]


# ── (1) symlinks to regular files materialise as the target's content ────


@linux_only
def test_symlinks_to_files_materialise_as_hardlinks_of_target(daemon: Daemon):
    ws = daemon.ws
    (ws / "a.txt").write_text("a")
    (ws / "link_a").symlink_to("a.txt")
    old = ws / "b_old.txt"
    old.write_text("b")
    _age(old, 7200)  # older than the stamp: itself NOT in this cycle's uploads
    (ws / "sub").mkdir()
    (ws / "sub" / "only_link").symlink_to("../b_old.txt")

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    assert "cycle complete" in daemon.log_text()
    snaps = daemon.snapshots()
    assert len(snaps) == 1
    staged = snaps[0]
    assert set(staged) == {"a.txt", "link_a", "sub/only_link"}
    assert not any(e["islink"] for e in staged.values())
    assert staged["link_a"]["ino"] == staged["a.txt"]["ino"] == os.lstat(ws / "a.txt").st_ino
    assert staged["sub/only_link"]["ino"] == os.lstat(old).st_ino
    assert "skipped" not in daemon.log_text()


# ── (2) dangling / directory symlinks are skipped, cycle still succeeds ──


@linux_only
def test_unmaterialisable_symlinks_are_skipped_not_fatal(daemon: Daemon):
    ws = daemon.ws
    (ws / "real.txt").write_text("r")
    (ws / "dang").symlink_to("nowhere")
    (ws / "subdir").mkdir()
    (ws / "dlink").symlink_to("subdir")
    # The watcher also logged the dangling link: it must not become a delete.
    daemon.watch_log.write_text(f"{ws}/dang\n")
    before = daemon.push_stamp.stat().st_mtime

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    log = daemon.log_text()
    assert "cycle complete" in log
    assert log.count("skipped 2 symlink(s) that cannot be materialised") == 1
    assert daemon.push_stamp.stat().st_mtime > before + 1000
    assert set(daemon.snapshots()[0]) == {"real.txt"}
    assert _rm_lines(daemon) == []
    assert not daemon.marker.exists()


# ── (3) start-up sweep removes poison symlinks left in staging ───────────


def test_start_sweep_removes_symlinks_from_both_staging_dirs(daemon: Daemon):
    auto = daemon.ws / ".swm_staging" / "autosync" / "bin"
    push = daemon.ws / ".swm_staging" / "push" / "bin"
    auto.mkdir(parents=True)
    push.mkdir(parents=True)
    (auto / "python3").symlink_to("python3.11")
    (push / "python3").symlink_to("python3.11")

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    assert not (auto / "python3").is_symlink() and not (auto / "python3").exists()
    assert not (push / "python3").is_symlink() and not (push / "python3").exists()
    assert auto.is_dir() and push.is_dir()


# ── (4) staging is fully cleared after a failed cycle ─────────────────────


def test_failed_cycle_leaves_no_files_or_symlinks_in_staging(daemon: Daemon):
    (daemon.ws / "a.txt").write_text("a")
    (daemon.ws / "link").symlink_to("a.txt")

    result = daemon.run_once(rc=1)

    assert result.returncode == 0, result.stderr
    assert "transfer failed" in daemon.log_text()
    assert daemon.staged_entries() == []
    assert not daemon.lock.exists()


# ── (5) s5cmd flags ───────────────────────────────────────────────────────


def test_cp_uses_no_follow_symlinks(daemon: Daemon):
    (daemon.ws / "a.txt").write_text("a")

    daemon.run_once()

    cps = _cp_lines(daemon)
    assert len(cps) == 1
    assert "--no-follow-symlinks" in cps[0].split()
    assert cps[0].split()[:3] == ["--log", "error", "cp"]


@linux_only
def test_rm_uses_raw_keys(daemon: Daemon):
    daemon.watch_log.write_text(f"{daemon.ws}/gone?.txt\n")

    daemon.run_once()

    rms = _rm_lines(daemon)
    assert len(rms) == 1
    argv = rms[0].split()
    assert argv[:4] == ["--log", "error", "rm", "--raw"]
    assert argv[4] == "s3://bucket/ws/gone?.txt"
    assert "cycle complete" in daemon.log_text()


# ── (6) failure streak marker ─────────────────────────────────────────────


def test_fail_streak_writes_marker_and_clean_cycle_clears_it(daemon: Daemon):
    (daemon.ws / "a.txt").write_text("a")

    for _ in range(AUTOSYNC_FAIL_STREAK - 1):
        daemon.run_once(rc=1)
    assert not daemon.marker.exists()
    assert daemon.counter.read_text().splitlines()[0] == str(AUTOSYNC_FAIL_STREAK - 1)

    daemon.run_once(rc=1)

    assert daemon.marker.exists()
    lines = daemon.marker.read_text().splitlines()
    assert re.fullmatch(
        rf"count={AUTOSYNC_FAIL_STREAK} since=\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ", lines[0],
    )
    assert any("given object not found" in ln for ln in lines[1:])
    assert len(lines) <= 6
    assert "consecutive failed cycles" in daemon.log_text()

    daemon.run_once(rc=0)

    assert not daemon.marker.exists()
    assert not daemon.counter.exists()
    assert f"recovered after {AUTOSYNC_FAIL_STREAK} failed cycle(s)" in daemon.log_text()


def test_marker_is_excluded_from_sync(daemon: Daemon):
    (daemon.ws / "a.txt").write_text("a")
    daemon.marker.write_text("count=9 since=2026-01-01T00:00:00Z\n")

    daemon.run_once()

    assert set(daemon.snapshots()[0]) == {"a.txt"}


# ── (7) lock holder validation ────────────────────────────────────────────


def test_dead_lock_holder_is_stale_and_cycle_proceeds(daemon: Daemon):
    dead = subprocess.Popen(["true"])
    dead.wait()
    daemon.lock.write_text(f"{dead.pid}\n")
    (daemon.ws / "a.txt").write_text("a")

    daemon.run_once()

    log = daemon.log_text()
    assert "removing stale transfer lock" in log
    assert re.search(r"cycle complete:\s+1 uploaded", log)
    assert not daemon.lock.exists()


@linux_only
def test_live_untagged_lock_holder_is_stale(daemon: Daemon):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        daemon.lock.write_text(f"{sleeper.pid}\n")
        (daemon.ws / "a.txt").write_text("a")

        daemon.run_once()

        log = daemon.log_text()
        assert "removing stale transfer lock" in log
        assert re.search(r"cycle complete:\s+1 uploaded", log)
    finally:
        sleeper.kill()
        sleeper.wait()


@linux_only
@pytest.mark.parametrize("argv0", [TRANSFER_LOCK_HOLDER_TAG, "/tmp/.swm_autosync.sh"])
def test_recognised_live_holder_skips_cycle(daemon: Daemon, argv0: str):
    holder = subprocess.Popen(["bash", "-c", f"exec -a {argv0} sleep 30"])
    try:
        time.sleep(0.2)
        daemon.lock.write_text(f"{holder.pid}\n")
        (daemon.ws / "a.txt").write_text("a")

        daemon.run_once()

        log = daemon.log_text()
        assert "manual transfer in progress, skipping cycle" in log
        assert "cycle complete" not in log
        assert daemon.lock.read_text().strip() == str(holder.pid)
        assert daemon.argv_lines() == []
    finally:
        holder.kill()
        holder.wait()


# ── (8) graceful drain on SIGTERM ─────────────────────────────────────────


def test_sigterm_mid_upload_drains_then_exits(daemon: Daemon):
    (daemon.ws / "a.txt").write_text("a")
    before = daemon.push_stamp.stat().st_mtime
    argv = Path(daemon.env["SWM_SHIM_ARGV"])
    done = Path(daemon.env["SWM_SHIM_DONE"])
    env = dict(daemon.env, SWM_SHIM_SLEEP="3", SWM_SHIM_RC="0")

    proc = subprocess.Popen(["bash", str(daemon.script)], env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 15
        while not argv.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert argv.exists(), "shim never started"
        assert not done.exists()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    assert done.exists(), "in-flight upload was killed instead of drained"
    log = daemon.log_text()
    assert re.search(r"cycle complete:\s+1 uploaded", log)
    assert "daemon stopped (drained)" in log
    assert log.rstrip().endswith("daemon stopped (drained)")
    assert daemon.push_stamp.stat().st_mtime > before + 1000
    assert not daemon.lock.exists()


# ── (9) watcher restart carries pending entries over ──────────────────────


def test_watcher_restart_preserves_pending_entries(daemon: Daemon):
    ws = daemon.ws
    for name in ("p1.txt", "p2.txt"):
        f = ws / name
        f.write_text(name)
        _age(f, 7200)  # not caught by the reconciliation scan
    daemon.watch_log.write_text(f"{ws}/p1.txt\n{ws}/p2.txt\n")
    ran = daemon.tmp / "watcher_ran"
    daemon.watcher_script.write_text(
        "#!/bin/bash\n"
        f"rm -f {daemon.watch_log}\n"
        f": > {daemon.watch_log}\n"
        f"echo $$ > {daemon.scratch}/.swm_watcher.pid\n"
        f"echo ran >> {ran}\n"
    )
    daemon.watcher_script.chmod(0o755)

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    assert ran.exists()
    log = daemon.log_text()
    assert "watcher process not running" in log
    assert re.search(r"uploading\s+2 file\(s\)", log)
    assert set(daemon.snapshots()[0]) == {"p1.txt", "p2.txt"}


# ── misc ──────────────────────────────────────────────────────────────────


def test_only_unmaterialisable_symlinks_do_not_invoke_s5cmd(daemon: Daemon):
    (daemon.ws / "subdir").mkdir()
    (daemon.ws / "dlink").symlink_to("subdir")
    before = daemon.push_stamp.stat().st_mtime

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    assert daemon.argv_lines() == []
    assert "cycle complete" in daemon.log_text()
    assert daemon.push_stamp.stat().st_mtime > before + 1000


def test_script_parses_and_has_no_placeholders(daemon: Daemon):
    body = daemon.script.read_text()
    assert not re.search(r"__SWM_[A-Z_]+__", body)
    assert subprocess.run(["bash", "-n", str(daemon.script)], check=False).returncode == 0


# ── a quota that charges each staging hardlink its full size ──────────────

_FAKE_LN = r'''#!/bin/bash
src="${@: -2:1}"
if [ -n "${SWM_FAKE_LN_MATCH:-}" ] && [[ "$src" == *"$SWM_FAKE_LN_MATCH"* ]]; then
  echo "ln: failed to create hard link '${@: -1}': $SWM_FAKE_LN_ERROR" >&2
  exit 1
fi
exec /bin/ln "$@"
'''


def _refuse_links(d: Daemon, name: str, error: str = "Disk quota exceeded") -> None:
    ln = d.tmp / "bin" / "ln"
    ln.write_text(_FAKE_LN)
    ln.chmod(0o755)
    d.env.update(SWM_FAKE_LN_MATCH=name, SWM_FAKE_LN_ERROR=error)


@linux_only
def test_quota_refused_links_upload_in_place_and_the_cycle_completes(daemon: Daemon):
    ws = daemon.ws
    (ws / "a.txt").write_text("a")
    (ws / "models").mkdir()
    (ws / "models" / "big.bin").write_text("big")
    (ws / "big_link").symlink_to("models/big.bin")
    _refuse_links(daemon, "big.bin")

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    log = daemon.log_text()
    assert "cycle complete" in log
    assert "uploading models/big.bin in place" in log
    staged_copy, *in_place = _cp_lines(daemon)
    assert f"{ws}/.swm_staging/autosync/*" in staged_copy
    assert set(in_place) == {
        f"--log error cp --no-follow-symlinks {ws}/models/big.bin s3://bucket/ws/models/big.bin",
        f"--log error cp --no-follow-symlinks {ws}/models/big.bin s3://bucket/ws/big_link",
    }
    assert set(daemon.snapshots()[0]) == {"a.txt"}
    assert not daemon.marker.exists()


@linux_only
def test_in_place_upload_of_a_file_that_changes_meanwhile_is_requeued(daemon: Daemon):
    big = daemon.ws / "big.bin"
    big.write_text("big")
    daemon.watch_log.write_text(f"{big}\n")
    _refuse_links(daemon, "big.bin")
    shim = daemon.tmp / "bin" / "s5cmd"
    real = shim.with_name("s5cmd.real")
    shim.rename(real)
    shim.write_text(
        f'#!/bin/bash\n"{real}" "$@"; rc=$?\n'
        f'case "$*" in *"big.bin s3://"*) echo more >> "{big}" ;; esac\n'
        f'exit $rc\n'
    )
    shim.chmod(0o755)
    before = daemon.push_stamp.stat().st_mtime

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    log = daemon.log_text()
    assert "big.bin changed while uploading in place" in log
    assert "re-queueing entries" in log
    assert str(big) in daemon.watch_log.read_text()
    assert daemon.push_stamp.stat().st_mtime == before


# ── renames and directory moves ("EVENTS /path" watch-log lines) ─────────


@linux_only
def test_a_renamed_file_uploads_under_its_new_name_and_drops_its_old_key(daemon: Daemon):
    ws = daemon.ws
    renamed = ws / "a2.png"
    renamed.write_text("a")
    _age(renamed, 7200)  # a rename keeps the file's mtime
    daemon.watch_log.write_text(f"MOVED_FROM {ws}/a.png\nMOVED_TO {renamed}\n")

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    assert set(daemon.snapshots()[0]) == {"a2.png"}
    assert _rm_lines(daemon) == ["--log error rm --raw s3://bucket/ws/a.png"]


@linux_only
def test_a_directory_moved_into_place_uploads_the_files_it_brought(daemon: Daemon):
    ws = daemon.ws
    moved = ws / "newdir"
    (moved / "sub").mkdir(parents=True)
    (moved / ".cache").mkdir()
    for p in (moved / "sub" / "c.png", moved / "d.png", moved / ".cache" / "tmp"):
        p.write_text("x")
        _age(p, 7200)
    daemon.watch_log.write_text(f"MOVED_TO,ISDIR {moved}\n")

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    assert set(daemon.snapshots()[0]) == {"newdir/sub/c.png", "newdir/d.png"}
    assert _rm_lines(daemon) == []


@linux_only
def test_a_directory_moved_away_has_its_stored_copy_deleted(daemon: Daemon):
    ws = daemon.ws
    daemon.watch_log.write_text(
        f"MOVED_FROM,ISDIR {ws}/olddir\nMOVED_FROM,ISDIR {ws}/x/.cache\n")

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    assert _rm_lines(daemon) == ["--log error rm s3://bucket/ws/olddir/*"]
    log = daemon.log_text()
    assert "deleting the stored copy of moved directory olddir" in log
    assert "cycle complete" in log


@linux_only
def test_a_moved_away_directory_made_again_keeps_its_stored_copy(daemon: Daemon):
    ws = daemon.ws
    (ws / "out").mkdir()
    (ws / "out" / "new.png").write_text("n")
    daemon.watch_log.write_text(
        f"MOVED_FROM,ISDIR {ws}/out\nCREATE,ISDIR {ws}/out\nCREATE {ws}/out/new.png\n")

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    assert _rm_lines(daemon) == []
    assert set(daemon.snapshots()[0]) == {"out/new.png"}


@linux_only
def test_a_moved_away_directory_named_like_a_wildcard_is_left_alone(daemon: Daemon):
    daemon.watch_log.write_text(f"MOVED_FROM,ISDIR {daemon.ws}/we*ird\n")

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    assert _rm_lines(daemon) == []
    log = daemon.log_text()
    assert "kept the stored copy of moved directory we*ird" in log
    assert "cycle complete" in log


@linux_only
def test_a_failed_directory_delete_is_reported_but_never_wedges_the_cycle(daemon: Daemon):
    daemon.watch_log.write_text(f"MOVED_FROM,ISDIR {daemon.ws}/olddir\n")

    result = daemon.run_once(rc=1)

    assert result.returncode == 0, result.stderr
    log = daemon.log_text()
    assert "could not delete the stored copy of moved directory olddir" in log
    assert "cycle complete" in log
    assert not daemon.marker.exists()


@linux_only
def test_bare_path_lines_from_an_older_watcher_still_sync(daemon: Daemon):
    ws = daemon.ws
    daemon.watch_log.write_text(f"{ws}/gone_old.txt\nDELETE {ws}/gone_new.txt\n")

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    assert sorted(_rm_lines(daemon)[0].split()[4:]) == [
        "s3://bucket/ws/gone_new.txt", "s3://bucket/ws/gone_old.txt"]


@linux_only
def test_other_link_failures_fail_the_cycle_with_the_real_error(daemon: Daemon):
    (daemon.ws / "a.txt").write_text("a")
    _refuse_links(daemon, "a.txt", "Operation not permitted")

    result = daemon.run_once()

    assert result.returncode == 0, result.stderr
    log = daemon.log_text()
    assert re.search(r"hardlink staging failed for \S*a\.txt: .*Operation not permitted", log)
    assert "re-queueing entries" in log
    assert _cp_lines(daemon) == []
