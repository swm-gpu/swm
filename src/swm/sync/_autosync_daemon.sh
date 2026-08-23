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
WATCHER_EXCLUDES_FILE="__SWM_WATCHER_EXCLUDES_FILE__"
# Single-quoted to preserve regex meta-characters ($, |, \) verbatim.
EXPECTED_EXCLUDES='__SWM_WATCHER_EXCLUDES__'

# Storage credentials are NOT embedded here: this script is world-readable
# and its content hash decides redeploys. They live in a root-only 0600
# file written by swm at (re)start and sourced fresh every cycle, so a
# credential rotation takes effect within one interval.
ENV_FILE="__SWM_ENV_FILE__"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$AUTO_LOG"
}

lock_held() {
  [ -f "$TRANSFER_LOCK" ] && kill -0 "$(cat "$TRANSFER_LOCK" 2>/dev/null)" 2>/dev/null
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
    rm -f "$WATCH_LOG"
    : > "$WATCH_LOG"
    bash "$watcher_script" >/dev/null 2>&1 || true
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
  find "$SRC" -newer "$PUSH_STAMP" ! -newer "$cycle_mark" -type f 2>/dev/null \
    | grep -Ev "$EXPECTED_EXCLUDES" > "$found" || true
  # Snapshot paths are filtered through the excludes too: inotify-tools
  # >= 3.22 lets directory-create events through regardless of --exclude,
  # and a watcher started by an older swm may have logged now-excluded
  # paths. Without this, an excluded path that later vanishes becomes an
  # `s5cmd rm` on a nonexistent key and wedges every subsequent cycle.
  {
    sort -u "$snap" | grep -Ev "$EXPECTED_EXCLUDES" \
      | while IFS= read -r f; do [ -f "$f" ] && echo "$f"; done
    cat "$found"
  } | sort -u > "$uploads"
  sort -u "$snap" | grep -Ev "$EXPECTED_EXCLUDES" \
    | while IFS= read -r f; do [ ! -e "$f" ] && echo "$f"; done > "$deletes"

  local n_up n_del
  n_up=$(wc -l < "$uploads" 2>/dev/null || echo 0)
  n_del=$(wc -l < "$deletes" 2>/dev/null || echo 0)

  echo $$ > "$TRANSFER_LOCK"

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
    mkdir -p "$staging"
    find "$staging" -type f -delete 2>/dev/null
    local stage_rc=0
    while IFS= read -r f; do
      [ -f "$f" ] || continue
      rel="${f#$SRC/}"
      mkdir -p "$staging/$(dirname "$rel")" || { stage_rc=1; break; }
      ln -f "$f" "$staging/$rel" \
        || { log "WARN: hardlink staging failed for $f"; stage_rc=1; break; }
    done < "$uploads"
    if [ "$stage_rc" -ne 0 ]; then
      cp_rc=1
    else
      s5cmd --log error cp "$staging/*" "s3://$BUCKET/$WORKSPACE/" >> "$AUTO_LOG" 2>&1
      cp_rc=$?
    fi
    find "$staging" -type f -delete 2>/dev/null
  fi

  if [ "$n_del" -gt 0 ]; then
    log "deleting $n_del file(s) from storage"
    local keylist="/tmp/.swm_autosync_keys"
    : > "$keylist"
    while IFS= read -r f; do
      rel="${f#$SRC/}"
      echo "s3://$BUCKET/$WORKSPACE/$rel" >> "$keylist"
    done < "$deletes"
    xargs -a "$keylist" -d '\n' -n 100 s5cmd --log error rm >> "$AUTO_LOG" 2>&1
    rm_rc=$?
    rm -f "$keylist"
  fi

  if [ "$cp_rc" -ne 0 ] || [ "$rm_rc" -ne 0 ]; then
    # Re-queue the snapshot so the next cycle retries; do NOT advance
    # PUSH_STAMP because the bucket is not in sync with the pod.
    log "WARN: transfer failed (cp_rc=$cp_rc rm_rc=$rm_rc) — re-queueing entries"
    cat "$snap" >> "$WATCH_LOG" 2>/dev/null || true
    rm -f "$cycle_mark" "$snap" "$uploads" "$deletes" "$found" "$TRANSFER_LOCK"
    return 0
  fi

  touch -r "$cycle_mark" "$PUSH_STAMP"
  rm -f "$cycle_mark" "$snap" "$uploads" "$deletes" "$found" "$TRANSFER_LOCK"
  log "cycle complete: $n_up uploaded, $n_del deleted"
}

log "auto-sync daemon starting (interval=${INTERVAL}s, src=$SRC, dest=s3://$BUCKET/$WORKSPACE)"
# The staging skeleton must exist from the moment the daemon runs so any
# staging path that leaks into the watch log always refers to a live
# directory and can never be reconciled into an S3 delete.
mkdir -p "${SRC%/}/.swm_staging/autosync"
while true; do
  ensure_watcher_healthy
  sync_once
  sleep "$INTERVAL"
done
