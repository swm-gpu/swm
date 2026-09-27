#!/bin/bash
# swm auto-sync daemon: periodically upload changes and propagate deletions.
# Variables prefixed with __SWM_ are substituted at deploy time.
set -u

INTERVAL="__SWM_INTERVAL__"
SRC="__SWM_SRC__"
WORKSPACE="__SWM_WORKSPACE__"
BUCKET="__SWM_BUCKET__"
WATCH_LOG="__SWM_WATCH_LOG__"
PUSH_STAMP="__SWM_PUSH_STAMP__"
AUTO_LOG="__SWM_AUTO_LOG__"
TRANSFER_LOCK="__SWM_TRANSFER_LOCK__"
LOCK_TAG="__SWM_LOCK_TAG__"
FAILING_MARKER="__SWM_FAILING_MARKER__"
FAIL_STREAK="__SWM_FAIL_STREAK__"
WATCHER_EXCLUDES_FILE="__SWM_WATCHER_EXCLUDES_FILE__"
# Single-quoted to preserve regex meta-characters ($, |, \) verbatim.
EXPECTED_EXCLUDES='__SWM_WATCHER_EXCLUDES__'

# Storage credentials are NOT embedded here: this script is world-readable
# and its content hash decides redeploys. They live in a root-only 0600
# file written by swm at (re)start and sourced fresh every cycle, so a
# credential rotation takes effect within one interval.
ENV_FILE="__SWM_ENV_FILE__"

# A transfer-lock holder is recognised by its cmdline: this script's name
# (the daemon mid-cycle) or the tag a manual push gives its holder process.
DAEMON_NAME=".swm_autosync.sh"
FAIL_COUNTER="/tmp/.swm_autosync_failstreak"
S5_OUT="/tmp/.swm_autosync_s5out"
STOP=0

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$AUTO_LOG"
}

s5note() {
  echo "$*" >> "$S5_OUT"
}

run_bg() {
  # Run "$@" as a background child and wait for it. A trapped TERM/INT
  # interrupts `wait` (setting STOP) but never the child, so an in-flight
  # transfer is allowed to finish rather than leaving partial objects.
  "$@" &
  local pid=$! rc
  wait "$pid"; rc=$?
  while [ "$rc" -gt 128 ] && kill -0 "$pid" 2>/dev/null; do
    wait "$pid"; rc=$?
  done
  return "$rc"
}

file_paths() {
  # Paths of the file events in watch-log snapshot $1. The watcher writes
  # "EVENTS /path"; a bare "/path" line comes from one an older swm started.
  # Directory (ISDIR) events are left to moved_dirs.
  awk '/^\// { print; next }
       /^[A-Z_,]+ \// { if (!index("," $1 ",", ",ISDIR,")) { sub(/^[^ ]+ /, ""); print } }' "$1"
}

moved_dirs() {
  # Directories logged with event $1 (MOVED_TO or MOVED_FROM) in snapshot
  # $2, minus excluded ones, matched with a trailing slash (the form the
  # excludes are written for).
  awk -v ev="$1" '/^[A-Z_,]+ \// { f = "," $1 ","
       if (index(f, ",ISDIR,") && index(f, "," ev ",")) { sub(/^[^ ]+ /, ""); print } }' "$2" \
    | sort -u | sed 's#$#/#' | grep -Ev "$EXPECTED_EXCLUDES" | sed 's#/$##'
}

delete_stored_dir() {
  # Remove every stored object under $1 (relative), a directory that was
  # moved away, so its old name does not come back on the next restore. Its
  # files were never logged one by one. Best effort: a persistent error must
  # not wedge every later cycle, and skipping leaves only the stale copy
  # there already was. A name containing an s5cmd wildcard is left alone,
  # since as a pattern it could match other keys.
  local rel="$1" out
  case "$rel" in
    *'*'*|*'?'*)
      s5note "WARN: kept the stored copy of moved directory $rel (its name contains a wildcard character)"
      return 0 ;;
  esac
  log "deleting the stored copy of moved directory $rel"
  out=$(s5cmd --log error rm "s3://$BUCKET/$WORKSPACE/$rel/*" 2>&1) && return 0
  case "$out" in *"no object found"*) return 0 ;; esac
  s5note "WARN: could not delete the stored copy of moved directory $rel: $out"
}

quota_refused() {
  # Some network volumes (MooseFS) charge every hardlink its file's full
  # size, so a large new file that fits on the volume once cannot be staged.
  case "$1" in *"Disk quota exceeded"*) return 0 ;; esac
  return 1
}

upload_in_place() {
  # Upload the "<file>\t<key>" entries staging could not link, each read
  # from where it lives. One that changed mid-upload fails the cycle so it
  # is re-queued; staging's hardlinks never froze contents either.
  local path rel before
  while IFS=$'\t' read -r path rel; do
    [ "$STOP" -eq 1 ] && return 1
    [ -e "$path" ] || continue
    before=$(stat -c '%s %y' -- "$path") || return 1
    log "uploading $rel in place (it does not fit the volume's staging quota)"
    run_bg s5cmd --log error cp --no-follow-symlinks "$path" "s3://$BUCKET/$WORKSPACE/$rel" \
      >> "$S5_OUT" 2>&1 || return 1
    if [ "$(stat -c '%s %y' -- "$path" 2>/dev/null)" != "$before" ]; then
      s5note "WARN: $rel changed while uploading in place; re-queueing"
      return 1
    fi
  done < "$1"
}

lock_held() {
  [ -f "$TRANSFER_LOCK" ] || return 1
  local pid cmdline
  pid=$(cat "$TRANSFER_LOCK" 2>/dev/null)
  if [ -n "$pid" ] && [ "$pid" != "$$" ] && kill -0 "$pid" 2>/dev/null; then
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
    case "$cmdline" in
      *"$DAEMON_NAME"*|*"$LOCK_TAG"*) return 0 ;;
    esac
  fi
  # Dead or unrecognised holder: a stale lock must never block syncing, nor
  # count as activity for the idle guard.
  log "removing stale transfer lock (pid=${pid:-none})"
  rm -f "$TRANSFER_LOCK"
  return 1
}

release_own_lock() {
  [ "$(cat "$TRANSFER_LOCK" 2>/dev/null)" = "$$" ] && rm -f "$TRANSFER_LOCK"
}

ensure_watcher_healthy() {
  # Detect and recover from:
  #   - a watcher whose stdout fd points to a deleted inode (events get
  #     silently dropped),
  #   - a watcher process that's gone entirely,
  #   - a watcher started with a stale exclude regex (swm was upgraded
  #     since the pod was bootstrapped — current excludes won't take
  #     effect on this pod until the watcher is restarted).
  # Relies on start_watcher having installed /tmp/.swm_start_watcher.sh.
  local wpid_file="/tmp/.swm_watcher.pid"
  local watcher_script="/tmp/.swm_start_watcher.sh"
  local wpid
  wpid=$(cat "$wpid_file" 2>/dev/null)

  local restart=0
  if [ -z "$wpid" ] || ! kill -0 "$wpid" 2>/dev/null; then
    log "watcher process not running — restarting"
    restart=1
  else
    local fd_target
    fd_target=$(readlink "/proc/$wpid/fd/1" 2>/dev/null)
    case "$fd_target" in
      *"(deleted)"*)
        log "watcher stdout fd orphaned ($fd_target) — restarting"
        kill "$wpid" 2>/dev/null || true
        sleep 1
        restart=1
        ;;
    esac
    if [ "$restart" = "0" ] && [ -n "$WATCHER_EXCLUDES_FILE" ]; then
      local actual
      actual=$(cat "$WATCHER_EXCLUDES_FILE" 2>/dev/null)
      if [ "$actual" != "$EXPECTED_EXCLUDES" ]; then
        log "watcher exclude list stale — restarting (was: ${actual:0:60}…)"
        kill "$wpid" 2>/dev/null || true
        pkill -f 'inotifywait -m -r --exclude' 2>/dev/null || true
        sleep 1
        restart=1
      fi
    fi
  fi

  if [ "$restart" = "1" ] && [ -x "$watcher_script" ]; then
    # The watcher script truncates WATCH_LOG; carry pending entries across
    # the restart so changes/deletions seen since the last cycle survive.
    local carry="/tmp/.swm_autosync_watch_carry"
    cp "$WATCH_LOG" "$carry" 2>/dev/null || : > "$carry"
    bash "$watcher_script" >/dev/null 2>&1 || true
    cat "$carry" >> "$WATCH_LOG" 2>/dev/null || true
    rm -f "$carry"
  fi
}

record_failure() {
  local n since
  n=$(sed -n 1p "$FAIL_COUNTER" 2>/dev/null)
  since=$(sed -n 2p "$FAIL_COUNTER" 2>/dev/null)
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  [ "$n" -gt 0 ] && [ -n "$since" ] || since=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  n=$((n + 1))
  printf '%s\n%s\n' "$n" "$since" > "$FAIL_COUNTER"
  [ "$n" -ge "$FAIL_STREAK" ] || return 0
  {
    echo "count=$n since=$since"
    grep -E 'ERROR|WARN' "$S5_OUT" 2>/dev/null | tail -n 5
  } > "$FAILING_MARKER"
  if [ "$n" -eq "$FAIL_STREAK" ]; then
    log "WARN: $n consecutive failed cycles, wrote $FAILING_MARKER; slowing down until a cycle succeeds"
  fi
}

record_recovery() {
  [ -f "$FAIL_COUNTER" ] || [ -f "$FAILING_MARKER" ] || return 0
  local n
  n=$(sed -n 1p "$FAIL_COUNTER" 2>/dev/null)
  rm -f "$FAIL_COUNTER" "$FAILING_MARKER"
  log "recovered after ${n:-?} failed cycle(s)"
}

sleep_seconds() {
  if [ -f "$FAILING_MARKER" ]; then
    local n=$((INTERVAL * 10))
    [ "$n" -gt 600 ] && n=600
    echo "$n"
  else
    echo "$INTERVAL"
  fi
}

sync_once() {
  if [ ! -f "$ENV_FILE" ]; then
    log "credentials file missing ($ENV_FILE) — skipping cycle; re-run 'swm sync auto'"
    return 0
  fi
  . "$ENV_FILE"

  # Defense in depth: refuse to sync if the push stamp has disappeared.
  # Without it we cannot assume the pod and bucket are in sync, and
  # propagating deletions could wipe data on storage.
  if [ ! -f "$PUSH_STAMP" ]; then
    log "push stamp missing ($PUSH_STAMP) — refusing to sync"
    return 0
  fi

  if lock_held; then
    log "manual transfer in progress, skipping cycle"
    return 0
  fi

  # Copy-and-truncate rotation: preserves the inode that inotifywait has
  # open, so new events keep flowing into WATCH_LOG.  A plain `mv` would
  # orphan the inode and silently drop every subsequent event.
  #
  # The cycle marker is the upper bound for the reconciliation scan below.
  # On success PUSH_STAMP is advanced to this marker, not to "now", so files
  # written while this cycle is scanning/uploading stay eligible next time.
  local cycle_mark="/tmp/.swm_autosync_cycle_mark"
  : > "$cycle_mark"

  local snap="/tmp/.swm_autosync_snap.log"
  cp "$WATCH_LOG" "$snap" 2>/dev/null || : > "$snap"
  : > "$WATCH_LOG"

  local uploads="/tmp/.swm_autosync_uploads"
  local deletes="/tmp/.swm_autosync_deletes"
  local found="/tmp/.swm_autosync_found"
  find "$SRC" -newer "$PUSH_STAMP" ! -newer "$cycle_mark" \( -type f -o -type l \) 2>/dev/null \
    | grep -Ev "$EXPECTED_EXCLUDES" > "$found" || true
  # Snapshot paths are filtered through the excludes too: inotify-tools
  # >= 3.22 lets directory-create events through regardless of --exclude,
  # and a watcher started by an older swm may have logged now-excluded
  # paths. Without this, an excluded path that later vanishes becomes an
  # `s5cmd rm` on a nonexistent key and wedges every subsequent cycle.
  local paths="/tmp/.swm_autosync_paths"
  local moved_in="/tmp/.swm_autosync_moved_in"
  local moved_out="/tmp/.swm_autosync_moved_out"
  file_paths "$snap" | sort -u | grep -Ev "$EXPECTED_EXCLUDES" > "$paths"
  moved_dirs MOVED_TO "$snap" > "$moved_in"
  moved_dirs MOVED_FROM "$snap" > "$moved_out"
  {
    while IFS= read -r f; do [ -f "$f" ] && echo "$f"; done < "$paths"
    # A directory moved into place brings files whose mtimes predate the
    # stamp: neither their own events nor the scan above cover them.
    while IFS= read -r d; do
      [ -d "$d" ] && find "$d" \( -type f -o -type l \) 2>/dev/null
    done < "$moved_in" | grep -Ev "$EXPECTED_EXCLUDES"
    cat "$found"
  } | sort -u > "$uploads"
  # A dangling symlink still exists as a path: it is skipped by staging,
  # never reconciled into an S3 delete of a key that may not exist.
  while IFS= read -r f; do
    [ ! -e "$f" ] && [ ! -L "$f" ] && echo "$f"
  done < "$paths" > "$deletes"

  local n_up n_del
  n_up=$(wc -l < "$uploads" 2>/dev/null || echo 0)
  n_del=$(wc -l < "$deletes" 2>/dev/null || echo 0)

  # Exclusive create: a manual push that installed its holder between the
  # lock_held check and here wins, and this cycle's snapshot is re-queued
  # instead of racing that push over the staging tree.
  if ! ( set -C; echo $$ > "$TRANSFER_LOCK" ) 2>/dev/null; then
    log "transfer lock taken during scan; skipping cycle"
    cat "$snap" >> "$WATCH_LOG" 2>/dev/null || true
    rm -f "$cycle_mark" "$snap" "$uploads" "$deletes" "$found" "$paths" "$moved_in" "$moved_out"
    return 0
  fi
  : > "$S5_OUT"

  local cp_rc=0
  local rm_rc=0

  if [ "$n_up" -gt 0 ]; then
    log "uploading $n_up file(s)"
    # Staging lives INSIDE $SRC: same filesystem, so hardlinks are free.
    # Never fall back to cp — on a cross-device or unlinkable file that
    # silently duplicated the workspace onto the container overlay and
    # could upload partial files as corrupt objects. The dir skeleton is
    # persistent (only files are cleared): deleting the dirs would emit
    # bare-path inotify events that evade the excludes and poison
    # delete-reconciliation with nonexistent S3 keys.
    local staging="${SRC%/}/.swm_staging/autosync"
    # On the container overlay: once staged links exhaust the volume quota,
    # even appending to a list under $SRC fails.
    local in_place="/tmp/.swm_autosync_in_place"
    mkdir -p "$staging"
    find "$staging" \( -type f -o -type l \) -delete 2>/dev/null
    : > "$in_place"
    local stage_rc=0 n_staged=0 n_skipped=0 target err
    while IFS= read -r f; do
      rel="${f#$SRC/}"
      if [ -L "$f" ]; then
        # Resolve-or-skip: hardlink the TARGET under the link's name so
        # s5cmd uploads real content. Hardlinking the symlink inode itself
        # planted a dangling link in staging that halted s5cmd's walk and
        # survived the regular-files-only cleanup, wedging every cycle.
        target=$(readlink -f -- "$f" 2>/dev/null)
        if [ -n "$target" ] && [ -f "$target" ] && [ ! -L "$target" ] \
          && mkdir -p "$staging/$(dirname "$rel")" 2>/dev/null; then
          if err=$(LC_ALL=C ln -f -- "$target" "$staging/$rel" 2>&1); then
            n_staged=$((n_staged + 1))
          elif quota_refused "$err"; then
            printf '%s\t%s\n' "$target" "$rel" >> "$in_place"
          else
            n_skipped=$((n_skipped + 1))
          fi
        else
          n_skipped=$((n_skipped + 1))
        fi
        continue
      fi
      [ -f "$f" ] || continue
      mkdir -p "$staging/$(dirname "$rel")" || { stage_rc=1; break; }
      if ! err=$(LC_ALL=C ln -f -- "$f" "$staging/$rel" 2>&1); then
        if quota_refused "$err"; then
          printf '%s\t%s\n' "$f" "$rel" >> "$in_place"
          continue
        fi
        s5note "WARN: hardlink staging failed for $f: $err"
        stage_rc=1
        break
      fi
      n_staged=$((n_staged + 1))
    done < "$uploads"
    if [ "$n_skipped" -gt 0 ]; then
      log "skipped $n_skipped symlink(s) that cannot be materialised"
    fi
    if [ "$stage_rc" -ne 0 ]; then
      cp_rc=1
    elif [ "$n_staged" -gt 0 ]; then
      run_bg s5cmd --log error cp --no-follow-symlinks "$staging/*" "s3://$BUCKET/$WORKSPACE/" >> "$S5_OUT" 2>&1
      cp_rc=$?
    fi
    find "$staging" \( -type f -o -type l \) -delete 2>/dev/null
    if [ "$cp_rc" -eq 0 ] && [ -s "$in_place" ]; then
      upload_in_place "$in_place" || cp_rc=1
    fi
    rm -f "$in_place"
  fi

  if [ "$n_del" -gt 0 ]; then
    log "deleting $n_del file(s) from storage"
    local keylist="/tmp/.swm_autosync_keys"
    : > "$keylist"
    while IFS= read -r f; do
      rel="${f#$SRC/}"
      echo "s3://$BUCKET/$WORKSPACE/$rel" >> "$keylist"
    done < "$deletes"
    # --raw: a deleted name containing ? or * is a literal key, never a
    # wildcard that would delete other objects.
    run_bg xargs -a "$keylist" -d '\n' -n 100 s5cmd --log error rm --raw >> "$S5_OUT" 2>&1
    rm_rc=$?
    rm -f "$keylist"
  fi

  while IFS= read -r d; do
    # Made again since it moved away: its new contents share the prefix.
    [ -e "$d" ] || [ -L "$d" ] || delete_stored_dir "${d#$SRC/}"
  done < "$moved_out"

  cat "$S5_OUT" >> "$AUTO_LOG" 2>/dev/null

  if [ "$cp_rc" -ne 0 ] || [ "$rm_rc" -ne 0 ]; then
    # Re-queue the snapshot so the next cycle retries; do NOT advance
    # PUSH_STAMP because the bucket is not in sync with the pod.
    log "WARN: transfer failed (cp_rc=$cp_rc rm_rc=$rm_rc) — re-queueing entries"
    cat "$snap" >> "$WATCH_LOG" 2>/dev/null || true
    rm -f "$cycle_mark" "$snap" "$uploads" "$deletes" "$found" "$paths" "$moved_in" "$moved_out"
    release_own_lock
    record_failure
    return 0
  fi

  touch -r "$cycle_mark" "$PUSH_STAMP"
  rm -f "$cycle_mark" "$snap" "$uploads" "$deletes" "$found" "$paths" "$moved_in" "$moved_out"
  release_own_lock
  log "cycle complete: $n_up uploaded, $n_del deleted"
  record_recovery
}

trap 'STOP=1' TERM INT

log "auto-sync daemon starting (interval=${INTERVAL}s, src=$SRC, dest=s3://$BUCKET/$WORKSPACE)"
# The staging skeleton must exist from the moment the daemon runs so any
# staging path that leaks into the watch log always refers to a live
# directory and can never be reconciled into an S3 delete.
mkdir -p "${SRC%/}/.swm_staging/autosync"
# Symlinks left in either staging tree by an older daemon (whose cleanup
# removed regular files only) would halt every s5cmd walk until removed.
find "${SRC%/}/.swm_staging" -type l -delete 2>/dev/null

# SWM_AUTOSYNC_ONCE=1 runs exactly one health check + cycle (tests).
while :; do
  ensure_watcher_healthy
  sync_once
  if [ "$STOP" != 0 ] || [ "${SWM_AUTOSYNC_ONCE:-0}" = 1 ]; then
    break
  fi
  sleep "$(sleep_seconds)" &
  wait $!
  if [ "$STOP" != 0 ]; then
    kill $! 2>/dev/null
    break
  fi
done
release_own_lock
log "daemon stopped (drained)"
