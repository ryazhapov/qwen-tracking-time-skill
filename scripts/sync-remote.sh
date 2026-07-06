#!/usr/bin/env bash
# sync-remote.sh — mirror a remote machine's ~/.qwen usage + project logs to a
# local directory so the local tracker can attribute SSH'd sessions.
#
# Layout produced:
#   ~/.qwen-remote/<host>/usage/      ← remote ~/.qwen/usage/
#   ~/.qwen-remote/<host>/projects/   ← remote ~/.qwen/projects/
#
# IMPORTANT: skills/ is NEVER synced. State files (task_assignments.jsonl,
# branch_map.json, pending.jsonl, config.json) live only on the local machine
# so there's a single source of truth for assignments/mappings.
#
# Usage:
#   ./sync-remote.sh <user@host>                       # uses default ~/.qwen on remote
#   ./sync-remote.sh user@dev-server                   # shorthand
#   ./sync-remote.sh user@host --remote-dir /home/user/.qwen
#   REMOTE_QWEN_DIR=/srv/qwen ./sync-remote.sh user@host
#
# Recommended: run from cron every 5 minutes (see SKILL.md):
#   */5 * * * * /Users/<you>/.qwen/skills/time-tracker/scripts/sync-remote.sh user@dev-server >/dev/null 2>&1
#
# Idempotency: rsync --update transfers only changed bytes, so running it
# repeatedly is cheap and produces the same end state.
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <user@host> [--remote-dir /path/to/.qwen]" >&2
    echo "   or: REMOTE_QWEN_DIR=/path $0 <user@host>" >&2
    exit 1
fi

HOST="$1"
shift || true
REMOTE_QWEN_DIR="${REMOTE_QWEN_DIR:-$HOME/.qwen}"

# Parse --remote-dir flag
while [ $# -gt 0 ]; do
    case "$1" in
        --remote-dir)
            REMOTE_QWEN_DIR="$2"
            shift 2
            ;;
        *)
            echo "Unknown arg: $1" >&2
            exit 1
            ;;
    esac
done

# Host label: strip user@ prefix for the directory name.
HOST_LABEL="${HOST#*@}"
DEST_ROOT="$HOME/.qwen-remote/$HOST_LABEL"
LOG_FILE="$HOME/.qwen-remote/.sync.log"

mkdir -p "$DEST_ROOT"

echo "[$(date -u +%FT%TZ)] syncing $HOST → $DEST_ROOT" >> "$LOG_FILE"

# Sync usage/ and projects/ only. --update skips files that are already current.
# -m would skip empty dirs; we want the structure regardless.
rsync -a --update \
    --include='usage/' --include='usage/**' \
    --include='projects/' --include='projects/**' \
    --exclude='*' \
    "$HOST:$REMOTE_QWEN_DIR/" "$DEST_ROOT/" 2>>"$LOG_FILE" \
    && echo "[$(date -u +%FT%TZ)] ok: $HOST" >> "$LOG_FILE" \
    || echo "[$(date -u +%FT%TZ)] FAILED: $HOST (see errors above)" >> "$LOG_FILE"
