#!/usr/bin/env bash
# on_session_end.sh — fires on SessionEnd (prompt_input_exit | other).
#
# For the closing session:
#   - Ambiguous (pending) segments belonging to the CLOSING session only
#     → append to pending.jsonl so the next session start can ask the user.
#   - Resolved task entries → auto-submitted by the tracker MCP server
#     (this hook only stages data; it does not do HTTP itself).
#
# Dedup: pending.jsonl is append-only, so before writing we drop any prior
# lines whose (sessionId, start, gitBranch) already match the ones we're about
# to add — otherwise every quit re-stages the whole backlog.
#
# This hook is fire-and-forget: SessionEnd cannot block quit.
set -uo pipefail

SKILL_DIR="$HOME/.qwen/skills/time-tracker"
TASK_LAYER="$SKILL_DIR/task_layer.py"
PENDING_LOG="$SKILL_DIR/pending.jsonl"

export HOME="${HOME:-$(eval echo ~)}"

# Read hook input from stdin; extract the closing session_id.
INPUT=$(cat)
CLOSING_SID=$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try: print(json.load(sys.stdin).get('session_id', ''))
except Exception: print('')
" 2>/dev/null || echo "")

python3 - "$TASK_LAYER" "$PENDING_LOG" "$CLOSING_SID" <<'PYEOF'
import json, subprocess, sys, datetime
from pathlib import Path

task_layer = Path(sys.argv[1])
pending_log = Path(sys.argv[2])
closing_sid = sys.argv[3] if len(sys.argv) > 3 else ""

# Compute pending entries for the closing session only.
try:
    out = subprocess.run(
        ["python3", str(task_layer), "entries"],
        capture_output=True, text=True, timeout=30,
    )
    entries = json.loads(out.stdout) if out.returncode == 0 else {"pending": []}
except Exception:
    entries = {"pending": []}

all_pending = entries.get("pending", [])

# Keep only segments from the closing session. If we couldn't determine the
# closing sid, skip writing entirely rather than staging the whole backlog
# (which caused duplicates on every quit).
if closing_sid:
    new_rows = [p for p in all_pending if (p.get("sessionId") or "") == closing_sid]
else:
    new_rows = []

if not new_rows:
    sys.exit(0)

# Build dedup keys for what we're about to write.
def key(p):
    return (p.get("sessionId"), p.get("start"), p.get("gitBranch"))

new_keys = {key(p) for p in new_rows}

# Read existing lines, drop any whose key collides with a new one.
survivors = []
if pending_log.exists():
    with open(pending_log, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                survivors.append(line)  # keep unparseable as-is
                continue
            if key(rec) in new_keys:
                continue  # will be re-added with fresh recordedAt below
            survivors.append(line)

# Rebuild + append the fresh rows.
pending_log.parent.mkdir(parents=True, exist_ok=True)
now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
with open(pending_log, "w", encoding="utf-8") as f:
    for line in survivors:
        f.write(line + "\n")
    for p in new_rows:
        rec = {
            "sessionId": p.get("sessionId"),
            "start": p.get("start"),
            "end": p.get("end"),
            "cwd": p.get("cwd"),
            "gitBranch": p.get("gitBranch"),
            "seconds": p.get("seconds"),
            "recordedAt": now_iso,
        }
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PYEOF

exit 0
