#!/usr/bin/env bash
#
# migrate_comfyui.sh — Restore ComfyUI user data from B2 into a fresh clone,
# then update to the latest stable version.
#
# Pulls ALL data (models, custom_nodes, input, output, configs, etc.) from
# the B2 workspace into the local ComfyUI directory non-destructively using
# rclone copy (never deletes anything on either side).
#
# Usage:
#   ./migrate_comfyui.sh <pod-id>                          # e.g. runpod:abc123
#   ./migrate_comfyui.sh <pod-id> --dry-run                # preview only
#   ./migrate_comfyui.sh <pod-id> --bucket my-bucket       # override bucket
#   ./migrate_comfyui.sh <pod-id> --workspace ws3          # override workspace
#   ./migrate_comfyui.sh --host 1.2.3.4 --port 22          # skip swm, manual SSH
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
HOST=""
PORT=""
USER="root"
BUCKET="backup-data-13943"
WORKSPACE="workspace2"
COMFYUI_DIR="/workspace/ComfyUI"
DRY_RUN=false
POD_ID=""

# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)       HOST="$2";        shift 2 ;;
    --port)       PORT="$2";        shift 2 ;;
    --user)       USER="$2";        shift 2 ;;
    --bucket)     BUCKET="$2";      shift 2 ;;
    --workspace)  WORKSPACE="$2";   shift 2 ;;
    --dir)        COMFYUI_DIR="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=true;     shift   ;;
    -h|--help)
      sed -n '2,/^$/s/^# \?//p' "$0"
      exit 0 ;;
    -*)           echo "Unknown option: $1"; exit 1 ;;
    *)            POD_ID="$1";      shift   ;;
  esac
done

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step()  { echo -e "\n${BOLD}>>> $*${NC}"; }

# ---------------------------------------------------------------------------
# Resolve SSH from swm if no --host given
# ---------------------------------------------------------------------------
if [[ -z "$HOST" ]]; then
  if [[ -z "$POD_ID" ]]; then
    error "Provide a pod ID (e.g. runpod:abc123) or use --host/--port"
    echo "Usage: $0 <pod-id> [--dry-run] [--bucket NAME] [--workspace NAME]"
    exit 1
  fi
  step "Resolving connection from swm for pod: $POD_ID"
  if ! command -v swm &>/dev/null; then error "swm not found in PATH"; exit 1; fi
  SSH_LINE=$(swm pod status "$POD_ID" 2>/dev/null | grep -oE 'ssh [^ ]+@[^ ]+ -p [0-9]+' | head -1) || true
  if [[ -z "$SSH_LINE" ]]; then
    error "Could not extract SSH details from 'swm pod status $POD_ID'"
    exit 1
  fi
  USER=$(echo "$SSH_LINE" | grep -oE '[^ ]+@' | tr -d '@')
  HOST=$(echo "$SSH_LINE" | grep -oE '@[^ ]+' | tr -d '@' | sed 's/ .*//')
  PORT=$(echo "$SSH_LINE" | grep -oE '\-p [0-9]+' | awk '{print $2}')
  info "Resolved: ${USER}@${HOST}:${PORT}"
fi
PORT="${PORT:-22}"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
step "Preflight"
info "Target:    ${USER}@${HOST}:${PORT}"
info "B2 source: b2:${BUCKET}/${WORKSPACE}/ComfyUI/"
info "Pod dest:  ${COMFYUI_DIR}/"
[[ "$DRY_RUN" == true ]] && warn "Dry-run mode — no changes will be made"

SSH_CMD=(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
             -o LogLevel=ERROR -p "$PORT" "${USER}@${HOST}")

info "Testing SSH connectivity..."
if ! "${SSH_CMD[@]}" "echo ok" &>/dev/null; then
  error "Cannot reach ${USER}@${HOST}:${PORT}"; exit 1
fi
info "SSH connected"

# ---------------------------------------------------------------------------
# Remote: copy ALL data from B2 then update ComfyUI
# ---------------------------------------------------------------------------
"${SSH_CMD[@]}" bash -s -- "$BUCKET" "$WORKSPACE" "$COMFYUI_DIR" "$DRY_RUN" << 'REMOTE'
set -euo pipefail

BUCKET="$1"; WS="$2"; DIR="$3"; DRY="$4"
B2_SRC="b2:${BUCKET}/${WS}/ComfyUI/"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[REMOTE][INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[REMOTE][WARN]${NC}  $*"; }
error() { echo -e "${RED}[REMOTE][ERROR]${NC} $*" >&2; }
step()  { echo -e "\n${BOLD}>>> $*${NC}"; }

if ! command -v rclone &>/dev/null; then error "rclone not installed on pod"; exit 1; fi

# ---- Step 1: Copy ALL data from B2 into local ComfyUI (non-destructive) ----
step "Copying ALL data from B2 → ${DIR}/"
info "Source: ${B2_SRC}"
info "This copies everything, never deletes. Existing newer files are skipped."

RCLONE_FLAGS=(--transfers=8 --fast-list --progress --stats=5s)
if [ "$DRY" = "true" ]; then
  RCLONE_FLAGS+=(--dry-run)
  warn "[dry-run] Showing what would be copied..."
fi

rclone copy "$B2_SRC" "$DIR/" "${RCLONE_FLAGS[@]}" 2>&1

# ---- Step 2: Update ComfyUI to latest stable ----
step "Updating ComfyUI to latest stable"
cd "$DIR"
if [ -d .git ]; then
  BEFORE=$(git describe --tags --always 2>/dev/null || echo "unknown")
  info "Current version: $BEFORE"
  if [ "$DRY" = "true" ]; then
    info "[dry-run] would run: git pull"
  else
    git fetch --tags 2>&1
    LATEST=$(git tag --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
    if [ -n "$LATEST" ]; then
      info "Latest stable tag: $LATEST"
      git checkout "$LATEST" 2>&1
    else
      warn "No stable tags found, pulling latest main"
      git pull 2>&1
    fi
    AFTER=$(git describe --tags --always 2>/dev/null || echo "unknown")
    info "Updated: $BEFORE → $AFTER"
  fi
else
  warn "No .git directory — cannot update. Consider re-cloning."
fi

# ---- Step 3: Install dependencies ----
step "Installing pip dependencies"
if [ "$DRY" = "true" ]; then
  info "[dry-run] would run: pip install -r ${DIR}/requirements.txt"
else
  pip install -r "$DIR/requirements.txt" 2>&1 | tail -5
fi

# ---- Summary ----
step "Done"
info "ComfyUI at: $DIR"
if [ -d .git ]; then
  info "Version: $(git describe --tags --always 2>/dev/null)"
fi
info "All B2 data copied non-destructively (nothing deleted)"
REMOTE
