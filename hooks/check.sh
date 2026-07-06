#!/usr/bin/env bash
# check.sh — health check for the usage-tracker hooks.
#
# Run this after any edit to the hook scripts or settings.json. It verifies:
#   1. Both hooks exist and are executable.
#   2. on_session_start.sh emits valid JSON with additionalContext.
#   3. on_session_end.sh runs without error and writes to pending.jsonl.
#   4. Hooks are registered in ~/.qwen/settings.json.
#   5. Core scripts (qwen_usage.py, task_layer.py) import cleanly.
#
# Exits non-zero if any check fails. Prints a summary at the end.
#
# Usage:
#   ./check.sh                  # full check
#   ./check.sh --quiet          # only print on failure
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_DIR="$SKILL_DIR/hooks"
SETTINGS="$HOME/.qwen/settings.json"

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

# Colors / markers (plain text — works in any terminal).
PASS="✓"
FAIL="✗"
WARN="!"

errors=0
warnings=0

log()     { [ "$QUIET" -eq 0 ] && echo "$@" || true; }
log_pass(){ [ "$QUIET" -eq 0 ] && echo "  $PASS $1" || true; }
log_warn(){ [ "$QUIET" -eq 0 ] && echo "  $WARN $1" || true; warnings=$((warnings+1)); }
log_fail(){ echo "  $FAIL $1"; errors=$((errors+1)); }

section() { [ "$QUIET" -eq 0 ] && echo "" && echo "$1" || true; }

# ─── 1. Executable bits ────────────────────────────────────────────────────
section "1. Hook scripts exist and are executable"

for h in on_session_start.sh on_session_end.sh; do
    f="$HOOKS_DIR/$h"
    if [ ! -f "$f" ]; then
        log_fail "$h missing"
        continue
    fi
    if [ -x "$f" ]; then
        log_pass "$h executable"
    else
        log_fail "$h NOT executable (run: chmod +x $f)"
    fi
done

# ─── 2. Python core imports ────────────────────────────────────────────────
section "2. Core Python scripts import cleanly"

if python3 -c "import sys; sys.path.insert(0, '$SKILL_DIR'); import task_layer" 2>/dev/null; then
    log_pass "task_layer.py imports"
else
    log_fail "task_layer.py import failed (syntax error or missing dep)"
fi

if python3 -c "import sys; sys.path.insert(0, '$SKILL_DIR'); import qwen_usage" 2>/dev/null; then
    log_pass "qwen_usage.py imports"
else
    log_fail "qwen_usage.py import failed"
fi

# ─── 3. settings.json registration ─────────────────────────────────────────
section "3. Hooks registered in ~/.qwen/settings.json"

if [ ! -f "$SETTINGS" ]; then
    log_fail "settings.json not found at $SETTINGS"
else
    if ! python3 -c "import json; json.load(open('$SETTINGS'))" 2>/dev/null; then
        log_fail "settings.json is not valid JSON"
    else
        # Check SessionStart hook is registered.
        ss_cmd=$(python3 -c "
import json
s = json.load(open('$SETTINGS'))
for entry in s.get('hooks',{}).get('SessionStart',[]):
    for h in entry.get('hooks',[]):
        if 'on_session_start' in h.get('command',''):
            print(h['command']); break
" 2>/dev/null)
        if [ -n "$ss_cmd" ]; then
            log_pass "SessionStart registered: $ss_cmd"
        else
            log_fail "SessionStart hook NOT registered in settings.json"
        fi

        se_cmd=$(python3 -c "
import json
s = json.load(open('$SETTINGS'))
for entry in s.get('hooks',{}).get('SessionEnd',[]):
    for h in entry.get('hooks',[]):
        if 'on_session_end' in h.get('command',''):
            print(h['command']); break
" 2>/dev/null)
        if [ -n "$se_cmd" ]; then
            log_pass "SessionEnd registered: $se_cmd"
        else
            log_fail "SessionEnd hook NOT registered in settings.json"
        fi
    fi
fi

# ─── 4. on_session_start.sh smoke test ─────────────────────────────────────
section "4. on_session_start.sh smoke test (valid JSON + additionalContext)"

START_HOOK="$HOOKS_DIR/on_session_start.sh"
SHOWN_LOG="$SKILL_DIR/.shown_sessions"

if [ -x "$START_HOOK" ]; then
    # Back up shown-sessions state.
    [ -f "$SHOWN_LOG" ] && cp "$SHOWN_LOG" /tmp/.tracker_check_shown.bak
    rm -f "$SHOWN_LOG"

    # The hook recomputes pending from raw logs via `task_layer.py entries`.
    # We can't fake a branch without polluting real logs, so we only verify:
    #   (a) it produces valid JSON,
    #   (b) IF there's real pending, additionalContext is present and non-empty,
    #   (c) IF there's no pending, the hook exits silently (equally valid).
    OUT=$(echo '{"session_id":"check-test-session","hook_event_name":"SessionStart","source":"startup","cwd":"/test"}' \
          | "$START_HOOK" 2>/dev/null)

    if [ -z "$OUT" ]; then
        # Silent exit is valid when there's no pending time in real logs.
        log_pass "silent exit (no pending time in logs — valid)"
    elif echo "$OUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
hs = d.get('hookSpecificOutput', {})
ctx = hs.get('additionalContext', '')
if not ctx:
    print('MISSING_ADDITIONAL_CONTEXT'); sys.exit(1)
if 'неатрибутиров' not in ctx and 'pending' not in ctx.lower():
    print('CONTENT_NOT_PENDING_RELATED'); sys.exit(1)
print('OK')
" 2>/dev/null | grep -q "^OK$"; then
        log_pass "emits valid JSON with additionalContext about pending"
    else
        diag=$(echo "$OUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    hs = d.get('hookSpecificOutput', {})
    ctx = hs.get('additionalContext', '')
    if not ctx: print('missing additionalContext')
    else: print(f'unexpected content (len={len(ctx)})')
except Exception as e:
    print(f'invalid JSON: {e}')
" 2>&1)
        log_fail "on_session_start.sh output invalid: $diag"
    fi

    # Verify dedup: second run with same session_id should be silent.
    OUT2=$(echo '{"session_id":"check-test-session","source":"startup"}' | "$START_HOOK" 2>/dev/null)
    if [ -z "$OUT2" ]; then
        log_pass "dedup works (second run for same session is silent)"
    else
        log_warn "dedup: second run for same session emitted output (expected silent)"
    fi

    # Restore shown-sessions state.
    if [ -f /tmp/.tracker_check_shown.bak ]; then
        mv /tmp/.tracker_check_shown.bak "$SHOWN_LOG"
    else
        rm -f "$SHOWN_LOG"
    fi
fi

# ─── 5. on_session_end.sh smoke test ───────────────────────────────────────
section "5. on_session_end.sh smoke test (no crash)"

END_HOOK="$HOOKS_DIR/on_session_end.sh"
PENDING_LOG="$SKILL_DIR/pending.jsonl"
if [ -x "$END_HOOK" ]; then
    # Back up pending.
    [ -f "$PENDING_LOG" ] && cp "$PENDING_LOG" /tmp/.tracker_check_pending2.bak

    ERR=$(echo '{"session_id":"check-end-session","reason":"prompt_input_exit"}' \
          | "$END_HOOK" 2>&1 >/dev/null)
    ec=$?
    if [ $ec -eq 0 ] && [ -z "$ERR" ]; then
        log_pass "runs cleanly (exit 0, no stderr)"
    else
        log_fail "on_session_end.sh failed (exit $ec): $ERR"
    fi

    # Restore.
    if [ -f /tmp/.tracker_check_pending2.bak ]; then
        mv /tmp/.tracker_check_pending2.bak "$PENDING_LOG"
    fi
fi

# ─── 6. State files ────────────────────────────────────────────────────────
section "6. State files"

for f in config.json branch_map.json task_assignments.jsonl pending.jsonl; do
    p="$SKILL_DIR/$f"
    if [ -f "$p" ]; then
        log_pass "$f exists"
    else
        log_warn "$f does not exist yet (will be auto-created on first use)"
    fi
done

# ─── Summary ───────────────────────────────────────────────────────────────
section "Summary"
if [ $errors -eq 0 ] && [ $warnings -eq 0 ]; then
    log "  $PASS All checks passed."
    exit 0
elif [ $errors -eq 0 ]; then
    log "  $WARN $warnings warning(s), no errors."
    exit 0
else
    echo "  $FAIL $errors error(s), $warnings warning(s). Fix the errors above before relying on hooks."
    exit 1
fi
