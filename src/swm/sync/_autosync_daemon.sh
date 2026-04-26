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

__SWM_ENV_EXPORTS__

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$AUTO_LOG"
}

lock_held() {
  [ -f "$TRANSFER_LOCK" ] && kill -0 "$(cat "$TRANSFER_LOCK" 2>/dev/null)" 2>/dev/null
}

ensure_watcher_healthy() {
  # Detect and recover from a watcher whose stdout fd points to a deleted
  # inode (events get silently dropped). Also restart it if the process
  # is gone entirely. Relies on start_watcher having installed
  # /tmp/.swm_start_watcher.sh.
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
  fi

  if [ "$restart" = "1" ] && [ -x "$watcher_script" ]; then
    rm -f "$WATCH_LOG"
    : > "$WATCH_LOG"
    bash "$watcher_script" >/dev/null 2>&1 || true
  fi
}

sync_once() {
  [ -s "$WATCH_LOG" ] || return 0

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
  local snap="/tmp/.swm_autosync_snap.log"
  cp "$WATCH_LOG" "$snap" 2>/dev/null || return 0
  : > "$WATCH_LOG"

  local uploads="/tmp/.swm_autosync_uploads"
  local deletes="/tmp/.swm_autosync_deletes"
  sort -u "$snap" | while IFS= read -r f; do [ -f "$f" ] && echo "$f"; done > "$uploads"
  sort -u "$snap" | while IFS= read -r f; do [ ! -e "$f" ] && echo "$f"; done > "$deletes"

  local n_up n_del
  n_up=$(wc -l < "$uploads" 2>/dev/null || echo 0)
  n_del=$(wc -l < "$deletes" 2>/dev/null || echo 0)

  echo $$ > "$TRANSFER_LOCK"

  if [ "$n_up" -gt 0 ]; then
    log "uploading $n_up file(s)"
    local staging="/tmp/.swm_autosync_staging"
    rm -rf "$staging"
    while IFS= read -r f; do
      rel="${f#$SRC/}"
      mkdir -p "$staging/$(dirname "$rel")"
      ln "$f" "$staging/$rel" 2>/dev/null || cp "$f" "$staging/$rel"
    done < "$uploads"
    s5cmd --log error cp "$staging/*" "s3://$BUCKET/$WORKSPACE/" >> "$AUTO_LOG" 2>&1
    rm -rf "$staging"
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
    rm -f "$keylist"
  fi

  touch "$PUSH_STAMP"
  rm -f "$snap" "$uploads" "$deletes" "$TRANSFER_LOCK"
  log "cycle complete: $n_up uploaded, $n_del deleted"
}

log "auto-sync daemon starting (interval=${INTERVAL}s, src=$SRC, dest=s3://$BUCKET/$WORKSPACE)"
while true; do
  ensure_watcher_healthy
  sync_once
  sleep "$INTERVAL"
done
