# Limitations & Known Issues

Honest record of what doesn't work and why. Read before promising behaviour.

## 1. Auto-prompt at session start is UNRELIABLE

**Symptom:** The `on_session_start.sh` hook runs and injects
`additionalContext` about pending time, but the user sees nothing at startup.

**Root cause (verified in qwen-code 0.19.6 source, `chunk-AGZF43UV.js`):**

1. The `SessionStart` event handler uses **only** `getAdditionalContext()` from
   hook output — `systemMessage` and other fields are silently ignored
   (`chunk-AGZF43UV.js:141449`).
2. The returned context is wrapped as:
   ```js
   <qwen:session-start-context hidden="true">
   SessionStart additional context:
   ...
   </qwen:session-start-context>
   ```
   (`chunk-AGZF43UV.js:46432`). The `hidden="true"` means it is **never
   displayed in the UI** — only passed to the model as soft context.
3. The model is free to ignore it. In practice it often does, especially when
   the user immediately types a work-related prompt.

**What works:** the hook fires (verified via `.shown_sessions`), the context
reaches the chat log (verified by grepping `session-start-context` in session
JSONL), and the model *can* act on it — but does not *have to*.

**Workaround:** the explicit `/track-pending` slash command is the only
reliable way for the user to discover pending time. The SessionStart hook is
best-effort and may fire when the skill happens to be loaded.

**Do not promise users "you'll be prompted at startup".** Promise them
"/track-pending works".

## 2. MCP server for tracker submission is NOT implemented

`MCP_SERVER_SPEC.md` describes the design (log_time, get_task_info,
get_pending_time, get-before-post idempotency, unsubmit, retry). The Python
code that produces time entries (`task_layer.py entries`) is ready, but the
MCP server itself is not built. Until it is, submission to a tracker must be
done manually (e.g. `task_layer.py entries | jq ... | curl ...`).

## 3. Branch reuse after merge

If a branch `feature/X` is merged and later a new branch with the same name
is created for a different task, `branch_map.json` will attribute both to the
same taskKey. `gitBranch` in the logs does not distinguish them.

**Mitigation:** the user is responsible for not reusing branch names across
tasks, or for re-mapping when starting a new task on a reused name.

## 4. Concurrent submits (get-before-post race)

Two simultaneous `log_time` calls for the same taskKey+date can both miss the
existing worklog and both POST, creating duplicates. In practice this is
unlikely (two windows active on the same task simultaneously is rare), but
not impossible.

**Mitigation:** none implemented. `unsubmit` can remove duplicates.

## 5. SSH server clock skew

In multi-host mode, remote logs may have timestamps that differ from local
time if the server's clock is skewed. The focus-segment algorithm sorts all
records globally by timestamp, so skew can place remote `main` calls in the
wrong position of the timeline and mis-cut segments.

**Mitigation:** ensure NTP is running on remote servers. Documented in
`scripts/sync-remote.sh`.

## 6. `additionalContext` is the only SessionStart channel

qwen-code offers no way for a SessionStart hook to print a visible message to
the user. `systemMessage` is ignored for this event type (verified in source).
This is an upstream limitation, not something the skill can fix. If qwen-code
adds visible SessionStart messaging in the future, the hook already returns
the right content and would Just Work.

## 7. Skill must be loaded for the model to act on context

The MANDATORY instruction in `SKILL.md` only takes effect when the skill is
actually loaded into the model's context. Skills are model-invoked based on
the description in frontmatter — they are not loaded automatically at session
start. So even if the hook injects pending context, the model may not know
how to act on it unless `/skills time-tracker` or a matching user
request has loaded the skill.

## 8. pending.jsonl is a staging cache, not source of truth

`pending.jsonl` is written by `on_session_end.sh` on every `/quit`. It is a
write-only staging file in the current design — **no display path reads it**.
`status`, `pending`, `gaps`, `entries`, and `on_session_start.sh` all recompute
pending time from raw qwen-code logs via `compute_task_entries`. Therefore:

- Clearing `pending.jsonl` (via `resolve`) does NOT silence the SessionStart
  prompt — the time will still show as pending until attributed via `map` or
  dropped via `untrack`.
- `pending.jsonl` exists for the future MCP submission workflow, which may use
  it as a work queue.

The authoritative source is always `task_layer.py entries` (recomputed from
raw logs). This is intentional: it guarantees `status` and `pending` always
agree, and survives staging file drift.
