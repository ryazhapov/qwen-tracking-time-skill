#!/usr/bin/env bash
# on_session_start.sh — fires on SessionStart (startup | resume | clear).
#
# Asks the model to surface unattributed time at session start.
#
# Source of truth: task_layer.py entries (recomputed from raw logs each call).
# We deliberately do NOT read pending.jsonl directly — that file is a staging
# cache that can drift from reality if state was cleared or hooks missed a
# session. Recomputing from logs guarantees we never lose or hide time.
#
# Dedup: a session is prompted AT MOST ONCE per unique pending state. We hash
# the pending summary and skip re-prompting if the hash is unchanged since the
# last prompt for this session (covers /resume, /clear of an empty backlog).
# Pending that grows (new unattributed time) re-prompts.
#
# We deliberately do NOT match source=compact — compaction continues the same
# session, so re-prompting would be noise.
set -uo pipefail

SKILL_DIR="$HOME/.qwen/skills/time-tracker"
TASK_LAYER="$SKILL_DIR/task_layer.py"
SHOWN_LOG="$SKILL_DIR/.shown_sessions"

export HOME="${HOME:-$(eval echo ~)}"

INPUT=$(cat)

python3 - "$TASK_LAYER" "$SHOWN_LOG" "$INPUT" <<'PYEOF'
import json, subprocess, sys, hashlib
from collections import defaultdict

task_layer, shown_log = sys.argv[1], sys.argv[2]
hook_input = sys.argv[3] if len(sys.argv) > 3 else "{}"

try:
    this_sid = json.loads(hook_input).get("session_id", "")
except Exception:
    this_sid = ""

# Recompute pending from raw logs — single source of truth.
try:
    out = subprocess.run(
        ["python3", str(task_layer), "entries"],
        capture_output=True, text=True, timeout=30,
    )
    entries = json.loads(out.stdout) if out.returncode == 0 else {"pending": []}
except Exception:
    entries = {"pending": []}

pending = entries.get("pending", [])

# Build a deterministic fingerprint of current pending state.
def fingerprint(rows):
    sigs = sorted(
        (r.get("sessionId", ""), r.get("start", ""),
         r.get("gitBranch") or "", r.get("cwd") or "")
        for r in rows
    )
    return hashlib.sha1(json.dumps(sigs).encode()).hexdigest()[:12]

fp = fingerprint(pending)

# Load "last shown" records: {session_id: fingerprint_at_last_prompt}.
shown = {}
try:
    with open(shown_log) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                shown[rec.get("sid", "")] = rec.get("fp", "")
            except json.JSONDecodeError:
                continue
except OSError:
    pass

# Skip if this session was already prompted AND the pending state is unchanged.
# New pending (different fingerprint) re-prompts even for the same session.
if this_sid and shown.get(this_sid) == fp:
    sys.exit(0)

if not pending:
    sys.exit(0)

# Record that we've now prompted this session at this fingerprint.
shown[this_sid] = fp
try:
    with open(shown_log, "w") as f:
        for sid, fprint in shown.items():
            f.write(json.dumps({"sid": sid, "fp": fprint}) + "\n")
except OSError:
    pass

# Group by (cwd basename, gitBranch, origin) and sum seconds.
groups = defaultdict(float)
for r in pending:
    cwd = r.get("cwd") or "?"
    cwd_short = cwd.rstrip("/").split("/")[-1] or cwd
    branch = r.get("gitBranch") or "?"
    origin = r.get("origin", "local")
    groups[(cwd_short, branch, origin)] += r.get("seconds", 0.0)


def fmt_dur(s):
    if s < 60:
        return f"{int(s)}s"
    m = int(s // 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m"


lines = [f"В прошлой сессии осталось неатрибутированное время ({fmt_dur(sum(r.get('seconds',0) for r in pending))} всего):"]
for (cwd, branch, origin), secs in sorted(groups.items(), key=lambda x: -x[1]):
    tag = f" [{origin}]" if origin != "local" else ""
    lines.append(f"  • {fmt_dur(secs)} — ветка {branch!r} (repo: {cwd}){tag}")
lines.append("")
lines.append('Разнести? Назови taskKey для каждой ветки (например "TASK-123"),')
lines.append('или скажи "skip" чтобы не учитывать.')
lines.append('Чтобы запомнить маппинг навсегда: "auth-rewrite = TASK-123".')
lines.append('Или вызови `/track-pending` чтобы посмотреть всё накопленное.')

text = "\n".join(lines)
out = {"hookSpecificOutput": {"hookEventName": "SessionStart",
                              "additionalContext": text}}
print(json.dumps(out, ensure_ascii=False))
PYEOF
exit 0
