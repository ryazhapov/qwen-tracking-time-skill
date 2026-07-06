---
name: time-tracker
description: Track tokens and focused work-time from Qwen Code usage logs, handling parallel instances, with task assignment layer.
---

# time-tracker

Tracks **tokens**, **API time**, and **focused work time** from Qwen Code logs,
correctly handling multiple parallel instances/windows, and assigns time to tasks.

## ⚠ Session-start behavior

At session start, the `on_session_start.sh` hook injects a hidden context block
telling you how much time is **unattributed (pending)** from prior sessions.

**IMPORTANT — known limitation of qwen-code:** the `SessionStart` hook's
`additionalContext` is wrapped in `<qwen:session-start-context hidden="true">`
and is **not displayed visibly to the user**. The model receives it as soft
context but is not forced to surface it. In practice the model often stays
silent, so the user sees nothing.

Therefore: **if you see a `SessionStart additional context` block mentioning
pending / неатрибутированное время, surface it to the user as your FIRST
action** — run `python3 task_layer.py pending` and ask how to attribute it.
But do not rely on this channel alone. The user's primary, reliable path is
the explicit **`/track-pending`** slash command.

## Primary entry points (reliable)

| Command | Purpose |
|---------|---------|
| `/track-status` | Show assignments + time-by-task + pending count |
| `/track-pending` | Show accumulated unattributed time (primary way to discover it) |
| `/track-log` | Per-session breakdown — what would be submitted |
| `/track-switch TASK-XXX` | Switch task mid-session |

## Components

| File | Purpose |
|------|---------|
| `qwen_usage.py` | Core parser: logs → focus segments, tokens, time |
| `task_layer.py` | Task assignment: sessions → tasks, produces time entries |
| `task_assignments.jsonl` | Append-only log of session→task assignments (auto-created) |

## What it does

Reads two log sources and joins them:

| Source | Has | Lacks |
|--------|-----|-------|
| `~/.qwen/usage/token-usage-*.jsonl` | tokens, `apiDurationMs`, `sessionId`, `source` | project path |
| `~/.qwen/projects/**/*.jsonl` | `sessionId` → `cwd` (project path) | token counts |

**Join key:** `sessionId`

## Focus-time model

The user may run multiple Qwen Code windows in parallel. Focus is a step function that changes **only at user-initiated ("main") API calls**:

```
main(A) ──[focus: A]──→ main(B) ──[focus: B]──→ main(A) ──→
```

- Between two consecutive `main` calls, ALL activity (background subagents, memory extractors) is attributed to the project of the earlier call — the user was in that window.
- When a `main` call appears in a different project, focus switches there.
- Idle gaps (>300s with no `main` call) cap the segment at 300s.
- Background calls (Explore subagents, `managed-auto-memory-extractor`) that occur while a different project has focus are attributed to the focused project, not their own origin.

This correctly handles: user starts a long Explore subagent in project A, switches to project B while it runs → the subagent's tokens are attributed to B (where the user's attention is).

## Usage

```bash
# Default: table report with focus timeline
python3 ~/.qwen/skills/time-tracker/qwen_usage.py

# Compact (no timeline / daily breakdown)
python3 ~/.qwen/skills/time-tracker/qwen_usage.py --no-timeline --no-daily

# Filter by project (substring match)
python3 ~/.qwen/skills/time-tracker/qwen_usage.py -p vector

# JSON output (for piping / dashboards)
python3 ~/.qwen/skills/time-tracker/qwen_usage.py -f json

# CSV output
python3 ~/.qwen/skills/time-tracker/qwen_usage.py -f csv
```

## Metrics explained

| Metric | Source | Meaning |
|--------|--------|---------|
| **focus time** | computed | Wall-clock time the user was actively working on a project (between main prompts, with idle cap) |
| **api_time** | `sum(apiDurationMs)` | Actual LLM compute time — always ≤ focus time |
| **tokens (in/out/cache/think)** | `usageMetadata` | Per-call token counts, summed per project |
| **api_calls** | count | Number of LLM API calls, including subagent and background calls |

## Config

`config.json` in the skill directory:

| Key | Default | Purpose |
|-----|---------|---------|
| `timezone` | `"+03:00"` (MSK) | Timezone for day-boundary attribution and display. Configure per team. |
| `history_horizon_days` | `30` | Only scan logs from the last N days. Bounds work + hook timeout. Set lower for faster `status`/`gaps`. |
| `idle_timeout` | `300` | Max focus continuation after last user prompt (seconds). |
| `merge_short_visits_s` | `300` | Visits shorter than this merge into the neighbouring task. |
| `viewing_branches` | `["master","main","release/*","develop","HEAD"]` | Branches never auto-attributed (quick checkouts to read). |
| `midnight_split` | `true` | Split focus segments at local midnight for correct day attribution. |
| `auto_submit_on_quit` | `true` | Auto-submit resolved entries on `/quit` (via tracker MCP). |
| `sanitize_paths` | `true` | Strip `cwd`/`gitBranch` from worklog comments sent to tracker. |
| `branch_regex` | `null` | Optional regex to auto-extract taskKey from branch name (e.g. `feature/([A-Z]+-\\d+)`). |

`QWEN_DATA_DIR` env var overrides `~/.qwen` (matches Qwen Code's own convention).

## Multi-host: tracking SSH'd sessions

When you run qwen-code on a remote machine over SSH, its logs land in that
machine's `~/.qwen/`, invisible to the local tracker. To include them:

1. **Mirror the remote logs locally** via `scripts/sync-remote.sh` (or cron):
   ```bash
   # one-off
   ~/.qwen/skills/time-tracker/scripts/sync-remote.sh user@dev-server

   # cron every 5 minutes (recommended — freshness 5–15 min)
   crontab -e
   */5 * * * * ~/.qwen/skills/time-tracker/scripts/sync-remote.sh user@dev-server >/dev/null 2>&1
   ```
   This pulls `usage/` and `projects/` into `~/.qwen-remote/<host>/`. State
   files (`skills/`) are NEVER synced — they stay local for a single source
   of truth.

2. **The tracker auto-discovers** `~/.qwen-remote/*/` directories at startup —
   no config needed. Sessions from each host get tagged `origin: remote:<host>`
   and show up in `status`/`gaps`/`entries` alongside local ones.

3. **Branch mapping is per-real-path.** Because `/Users/you/work/foo` (local)
   and `/home/you/work/foo` (server) are different paths, `branch_map.json`
   needs an entry for each:
   ```bash
   python3 task_layer.py map auth-rewrite TASK-123 --repo /Users/you/work/foo   # local
   python3 task_layer.py map auth-rewrite TASK-123 --repo /home/you/work/foo    # server
   ```

4. **Requires**: passwordless SSH (key-based) to the remote, `rsync` installed
   locally and remotely. mosh does NOT work — only SSH transfers files.

Limitations:
- Freshness is bounded by cron interval (default 5 min). For real-time, you'd
  need an SSH-call in the tracker (not implemented).
- Each host needs its own cron line. One `~/.qwen-remote/<host>/` per host.

## Task assignment layer (`task_layer.py`)

Bind focus-time to tasks so it can be submitted to a tracker. Three attribution
mechanisms, applied as a cascade (first match wins):

1. **Explicit `/switch`** — mid-session task switch (highest priority).
2. **`branch_map.json`** — learned mapping of working branch → task. Apply once,
   reused for all past and future sessions on that branch (pure-function recompute).
3. **Whole-session `assign`** — legacy, for when the whole session is one task.
4. **Pending** — none of the above matched → asked about at next session start.

`gitBranch` is used as a **reliable task boundary** (branch switch = task switch),
but an **unreliable task key** (branches aren't always named after tickets).
Viewing branches (`master`, `main`, `release/*`, `HEAD`, …) are never attributed
via branch_map — quick checkouts to read something don't fragment focus.

### Commands

```bash
# Whole-session assignment (start of session)
python3 task_layer.py assign TASK-123 --url "https://tracker/browse/TASK-123"

# Switch task mid-session (closes previous range, opens new)
python3 task_layer.py switch TASK-456

# Learn a branch → task mapping (applied retroactively to ALL history)
python3 task_layer.py map auth-rewrite TASK-123 --repo /path/to/repo
python3 task_layer.py map --list
python3 task_layer.py map auth-rewrite --unmap --repo /path/to/repo

# Mark session as not tracked
python3 task_layer.py untrack --session <sessionId>

# Mark pending time as resolved (after user attributes it or says skip).
# Clears from pending.jsonl so it isn't asked about again.
python3 task_layer.py resolve --all                    # clear everything
python3 task_layer.py resolve --session <sessionId>    # clear one session
python3 task_layer.py resolve --branch feat-x --repo /path  # clear one branch

# Show assignments + time-by-task + pending count
python3 task_layer.py status

# Show unattributed segments with suggested `map` commands
python3 task_layer.py gaps

# Show accumulated unattributed time (fallback when agent stayed silent at startup)
python3 task_layer.py pending

# Interactive per-session breakdown (shows what would be submitted)
python3 task_layer.py log

# Produce time entries as JSON (for MCP submission / pipeline)
python3 task_layer.py entries
```

### `/track-*` slash commands

Four user-invoked commands are registered in `~/.qwen/commands/`:

| Command | What it does |
|---------|--------------|
| `/track-status` | Show current assignments + time-by-task + pending count |
| `/track-pending` | Show accumulated unattributed time (fallback when the SessionStart hook was silent) |
| `/track-log` | Per-session breakdown — what would be submitted |
| `/track-switch TASK-XXX` | Switch task mid-session (closes previous range) |

These are also reachable as plain CLI: `python3 task_layer.py {status,pending,log,switch}`.

### Agent triggers

When the user says any of:
- "переключись на TASK-XXX", "switch to TASK-XXX", "/track-switch TASK-XXX"
- "работаю над TASK-XXX теперь", "now on TASK-XXX"

→ run `python3 task_layer.py switch TASK-XXX` to close the current task range
and open a new one.

When the user says any of:
- "запиши время", "списать время", "log time", "/track-log", "зalogируй"
- "закрой задачу и отправь в трекер"

→ run `python3 task_layer.py log` to show the per-session breakdown, then submit
via the MCP `log_time` tool (see MCP_SERVER_SPEC.md).

### Pending time → next session start

When `on_session_start.sh` hook detects unattributed time from prior sessions,
it injects a prompt via `additionalContext` like:

> "В прошлой сессии осталось Nm неатрибутировано: ветка `auth-rewrite` (45m,
> repo X), ветка `db-cleanup` (20m, repo Y). Разнести? Назови taskKey или
> скажи `skip`."

Resolve by either:
- naming a taskKey → `python3 task_layer.py map <branch> <TASK-XXX> --repo <repo>`
- saying "skip" → run `python3 task_layer.py untrack --session <sessionId>`

**How to actually resolve pending** (silence the prompt):

The SessionStart prompt is driven by `compute_task_entries` (recomputed from
raw logs), NOT by `pending.jsonl`. So clearing `pending.jsonl` via `resolve`
does NOT silence the prompt — the time will still be reported as pending until
it is attributed in the source of truth (the logs + assignments).

To actually resolve:
- **Attribute the branch** → `python3 task_layer.py map <branch> <TASK-XXX> --repo <repo>`
  (applied retroactively; the time moves from pending to that task).
- **Mark as untracked** → `python3 task_layer.py untrack --session <sessionId>`
  (the time is dropped from pending and never submitted).
- `python3 task_layer.py resolve --all` clears the staging `pending.jsonl`
  file but does NOT change what the prompt shows — it's mainly for the future
  MCP submission workflow. Don't rely on it to silence the prompt.

### Assignment model

- One session may contain MANY tasks (multi-task sessions). Each task occupies
  a time-range `{fromTs, toTs}`; the active range has `toTs=null`.
- `task_assignments.jsonl` is append-only. Opening a new range closes the
  previous one (a closure record is appended).
- `branch_map.json` is `{repo_path: {branch_name: taskKey}}` — per-repo, because
  the same branch name in two repos is two different tasks.
- Re-mapping a branch overwrites (last write wins) and is applied retroactively.

## Limitations

- Sessions in the usage log without a corresponding chat log are labeled `(unknown:...)`.
- Focus detection relies on `source: "main"` in the usage log. If Qwen Code changes this field name, tracking breaks.
- Idle timeout is a heuristic — if the user stares at a response for 6 minutes without prompting, the last minute is unattributed.
