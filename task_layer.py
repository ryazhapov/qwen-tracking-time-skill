#!/usr/bin/env python3
"""
task_layer.py — assigns focused time to tasks and produces loggable time entries.

INPUT:
  1. Focus segments from qwen_usage.py (computed from logs)
  2. Task assignments file: ~/.qwen/skills/time-tracker/task_assignments.jsonl
     Each line is a per-time-range assignment:
       {"sessionId": "...", "taskKey": "TASK-123", "taskUrl": "...",
        "fromTs": "ISO-timestamp", "toTs": "ISO-timestamp or null",
        "source": "session-start|switch|branch_map|infer", "note": "optional"}
     A session may have MANY ranges (multi-task sessions). The active range has
     toTs=null; opening a new range closes the previous one.
  3. Branch map: branch_map.json — {repo_path: {branch_name: taskKey}}
     Learned mapping of working-branch → task. Applied retroactively across
     the whole history (aggregation is a pure function of the logs + maps).
  4. Config: config.json — viewing_branches, merge_short_visits_s, etc.

OUTPUT:
  Time entries ready for submission to a task tracker:
  {"taskKey": "TASK-123", "date": "2026-07-04", "focusSeconds": 654,
   "apiSeconds": 75, "tokens": {"total": 117260, ...}, "sessions": [...]}

ATTRIBUTION RESOLVER (per moment in a session):
  1. Explicit /switch range (source="switch") — highest priority.
  2. branch_map[repo][gitBranch] for working branches (source="branch_map").
  3. Session-start assignment without a range (legacy, source="session-start").
  4. None → pending (ambiguous, not submitted, asked about at next SessionStart).

Short visits (< merge_short_visits_s) to viewing branches (master/main/release/...)
or to unmaped branches are merged into the neighbouring task segment so a quick
`git checkout release` to read something doesn't fragment focus.

USAGE:
  # View current task assignments and time-by-task
  python3 task_layer.py status

  # Assign a task to the current/latest session (full-session legacy form)
  python3 task_layer.py assign TASK-123 --url "https://tracker/browse/TASK-123"

  # Switch task mid-session (opens a new range, closes the previous)
  python3 task_layer.py switch TASK-456

  # Learn a branch → task mapping (applied retroactively to all history)
  python3 task_layer.py map auth-rewrite TASK-123 --repo /path/to/repo
  python3 task_layer.py map --list
  python3 task_layer.py map auth-rewrite --unmap --repo /path/to/repo

  # Mark session as explicitly untracked
  python3 task_layer.py untrack --session <sessionId>

  # Produce time entries (for MCP/submission)
  python3 task_layer.py entries

  # Show unassigned / unmapped segments (forgotten mappings?)
  python3 task_layer.py gaps

  # Interactive per-session breakdown + confirm submission
  python3 task_layer.py log
"""
import json, os, sys, argparse, fnmatch
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict


def _now_iso():
    """UTC now as ISO-8601 with milliseconds + Z (timezone-aware, no deprecation)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"

SKILL_DIR = Path(os.path.expanduser("~/.qwen/skills/time-tracker"))
ASSIGNMENTS_FILE = SKILL_DIR / "task_assignments.jsonl"
BRANCH_MAP_FILE = SKILL_DIR / "branch_map.json"
PENDING_FILE = SKILL_DIR / "pending.jsonl"
CONFIG_FILE = SKILL_DIR / "config.json"

DEFAULT_CONFIG = {
    "idle_timeout": 300,
    "midnight_split": True,
    "merge_short_visits_s": 300,     # visits shorter than this merge into neighbour
    "viewing_branches": ["master", "main", "release/*", "develop", "HEAD"],
}

# Import focus computation from the main module
sys.path.insert(0, str(SKILL_DIR))
from qwen_usage import (
    load_usage_records, load_session_projects, load_chat_segments,
    parse_iso, fmt_dur, fmt_tok,
    split_at_midnight,
)


# ─── Config / branch map / pending storage ─────────────────────────────────

def load_config():
    """Load config.json with defaults for missing keys."""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def load_branch_map():
    """Load {repo_path: {branch_name: taskKey}}."""
    if not BRANCH_MAP_FILE.exists():
        return {}
    try:
        with open(BRANCH_MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_branch_map(bm):
    BRANCH_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(BRANCH_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(bm, f, indent=2, ensure_ascii=False)


def clear_pending(session_id=None, git_branch=None, repo=None):
    """
    Remove entries from pending.jsonl. NOTE: pending.jsonl is a write-only
    staging file in the current design — display paths (status/pending/gaps)
    recompute from raw logs via compute_task_entries, NOT from this file.
    So clearing it has no effect on what the user sees.

    Kept for compatibility and for the future MCP submission workflow, which
    may use pending.jsonl as the work queue. To actually 'resolve' time in the
    user-visible sense, attribute it via `map` or mark the session `untracked`.
    """
    """
    Remove pending entries that have been resolved. If filters are given,
    only matching entries are cleared (so resolving one branch doesn't wipe
    others). With no filters, clears everything.

    Called by the agent after the user attributes pending time (via `map` or
    says "skip"), so the same time isn't asked about again at the next start.
    """
    if not PENDING_FILE.exists():
        return 0
    survivors = []
    removed = 0
    with open(PENDING_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                survivors.append(line)
                continue
            matches = True
            if session_id and rec.get("sessionId") != session_id:
                matches = False
            if git_branch and rec.get("gitBranch") != git_branch:
                matches = False
            if repo and rec.get("cwd") != repo:
                matches = False
            if matches:
                removed += 1
            else:
                survivors.append(line)
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        for line in survivors:
            f.write(line + "\n")
    return removed


# ─── Task assignment storage ───────────────────────────────────────────────

def load_assignments():
    """
    Load all task assignments. Returns {sessionId: [range_dict, ...]} sorted by fromTs.

    A range is {taskKey, taskUrl, fromTs(datetime|None), toTs(datetime|None),
                source, note, assignedAt}.

    The assignments file is append-only. save_assignment(close_previous=True)
    writes a closure record (note="range-closed-by-new-assignment", toTs set)
    alongside the original open record (toTs=None). Here we collapse those pairs
    back into a single closed range so the resolver sees only one entry per
    logical task period. Without this, the resolver matches the still-open
    original record for any ts >= fromTs and never reaches the new range.
    """
    if not ASSIGNMENTS_FILE.exists():
        return {}
    by_session = defaultdict(list)
    with open(ASSIGNMENTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = d.get("sessionId")
            if not sid:
                continue
            by_session[sid].append({
                "taskKey": d.get("taskKey"),
                "taskUrl": d.get("taskUrl"),
                "fromTs": parse_iso(d.get("fromTs")) or parse_iso(d.get("assignedAt")),
                "toTs": parse_iso(d.get("toTs")),
                "source": d.get("source", "session-start"),
                "note": d.get("note"),
                "assignedAt": d.get("assignedAt"),
            })

    # Collapse open+closure pairs: an open record (toTs=None) followed by a
    # closure record (note=range-closed-by-new-assignment, same taskKey, same
    # fromTs) becomes a single closed record with toTs from the closure.
    for sid, ranges in by_session.items():
        # Index closure records by (taskKey, fromTs) → toTs.
        closures = {}
        for r in ranges:
            if (r.get("note") == "range-closed-by-new-assignment"
                    and r["taskKey"] and r["fromTs"] and r["toTs"]):
                closures[(r["taskKey"], r["fromTs"])] = r["toTs"]
        # Apply: if an open record has a matching closure, fill its toTs.
        for r in ranges:
            if r["toTs"] is None and r["taskKey"] and r["fromTs"]:
                close_ts = closures.get((r["taskKey"], r["fromTs"]))
                if close_ts:
                    r["toTs"] = close_ts
                    r["note"] = "collapsed-closure"
        # Drop the closure records themselves — redundant after applying.
        by_session[sid] = [r for r in ranges
                           if r.get("note") != "range-closed-by-new-assignment"]
        by_session[sid].sort(key=lambda r: r["fromTs"] or datetime.min)
    return dict(by_session)


def save_assignment(session_id, task_key=None, task_url=None, note=None,
                    source="session-start", from_ts=None, to_ts=None,
                    close_previous=True):
    """
    Append a new assignment range. If close_previous, the current open range
    (toTs=None) for this session gets toTs=now written as a separate record,
    so the open range becomes closed. This keeps task_assignments.jsonl
    append-only: we never rewrite a prior line.
    """
    ASSIGNMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = _now_iso()
    records = []
    if close_previous:
        # Close any currently-open range for this session by appending a
        # closure marker referencing the same sessionId. The resolver treats
        # an open range as closed once a later range's fromTs passes its fromTs.
        # For explicit bookkeeping we also write the toTs here.
        current = load_assignments().get(session_id, [])
        for r in current:
            if r["toTs"] is None and r["taskKey"]:
                close_dt = to_ts if isinstance(to_ts, datetime) else datetime.now(timezone.utc)
                records.append({
                    "sessionId": session_id,
                    "taskKey": r["taskKey"],
                    "taskUrl": r["taskUrl"],
                    "fromTs": r["fromTs"].isoformat() if r["fromTs"] else None,
                    "toTs": close_dt.isoformat(timespec="milliseconds"),
                    "source": r["source"],
                    "assignedAt": now,
                    "note": "range-closed-by-new-assignment",
                })
    new_from = (from_ts if isinstance(from_ts, datetime)
                else datetime.now(timezone.utc))
    records.append({
        "sessionId": session_id,
        "taskKey": task_key,
        "taskUrl": task_url,
        "fromTs": new_from.isoformat(timespec="milliseconds"),
        "toTs": to_ts,
        "source": source,
        "assignedAt": now,
        "note": note,
    })
    with open(ASSIGNMENTS_FILE, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return records


def get_latest_session_id():
    """Find the most recently active session from usage logs."""
    records = load_usage_records()
    if not records:
        return None
    latest = max(records, key=lambda r: parse_iso(r.get("timestamp")) or datetime.min)
    return latest.get("sessionId")


# ─── Resolver: per-moment task attribution ─────────────────────────────────

def is_viewing_branch(branch, viewing_patterns):
    """True if branch matches any viewing-branch glob (master/main/release*/...)."""
    if not branch:
        return True  # unknown branch treated as viewing (won't be branch_map'd)
    return any(fnmatch.fnmatch(branch, pat) for pat in viewing_patterns)


def resolve_task_at(session_id, ts, cwd, git_branch,
                    assignments, branch_map, viewing_patterns):
    """
    Determine the taskKey active at a given moment in a session. Returns
    (taskKey | None, source). None means ambiguous → pending.

    Cascade (highest priority first):
      1. Explicit assignment range covering ts (any source: switch/assign).
      2. branch_map[cwd][git_branch] for non-viewing branches.
      3. None (pending).

    Legacy records without fromTs/toTs are ignored — the skill requires the
    per-time-range format.
    """
    # 1: explicit assignment range covering this moment
    for r in assignments.get(session_id, []):
        f = r["fromTs"]
        t = r["toTs"]
        if f is None:
            continue  # malformed/legacy — skip
        if f <= ts and (t is None or ts < t):
            if r["taskKey"]:
                return r["taskKey"], r["source"]
            # taskKey=None means "explicitly untracked" → not pending, just dropped
            return None, "untracked"

    # 2: branch_map for working branches only
    if cwd and git_branch and not is_viewing_branch(git_branch, viewing_patterns):
        repo_map = branch_map.get(cwd, {})
        if git_branch in repo_map and repo_map[git_branch]:
            return repo_map[git_branch], "branch_map"

    # 3: ambiguous
    return None, "pending"


# ─── Pipeline: slice → resolve → merge short visits ────────────────────────

def build_resolved_segments(chat_segments, assignments, branch_map, cfg):
    """
    For every session's chat slice, resolve a taskKey. Returns a flat list of
    dicts: {sessionId, start, end, cwd, gitBranch, taskKey, source}.

    Slices come from load_chat_segments() (sliced wherever cwd/gitBranch changes).
    """
    viewing = cfg.get("viewing_branches", DEFAULT_CONFIG["viewing_branches"])
    out = []
    for sid, slices in chat_segments.items():
        for s in slices:
            task_key, source = resolve_task_at(
                sid, s["start"], s["cwd"], s["gitBranch"],
                assignments, branch_map, viewing)
            out.append({
                "sessionId": sid,
                "start": s["start"],
                "end": s["end"],
                "cwd": s["cwd"],
                "gitBranch": s["gitBranch"],
                "origin": s.get("origin", "local"),
                "taskKey": task_key,
                "source": source,
            })
    return out


def merge_short_visits(segments, threshold_s):
    """
    Merge short (< threshold_s) segments into their neighbour when the
    neighbour on at least one side shares the same taskKey. This prevents a
    quick `git checkout release` to read something from fragmenting the focus
    on the real task.

    Segments must be sorted by (sessionId, start). Only same-session neighbours
    are merged. Untracked (source="untracked") and pending segments can both
    be absorbed; the absorbed duration is added to the absorbing task.
    """
    if not segments:
        return []
    by_session = defaultdict(list)
    for s in segments:
        by_session[s["sessionId"]].append(s)
    merged_all = []
    for sid, segs in by_session.items():
        segs.sort(key=lambda s: s["start"])
        n = len(segs)
        keep = [True] * n
        absorbed_into = [None] * n  # index of absorber, or None
        for i, s in enumerate(segs):
            dur = (s["end"] - s["start"]).total_seconds()
            if dur >= threshold_s:
                continue
            # Only absorb short segments that are NOT already resolved to a task
            # via switch/branch_map (those are intentional). Pending/untracked/
            # viewing-branch visits are candidates for absorption.
            if s["source"] in ("switch", "branch_map", "session-start") and s["taskKey"]:
                continue
            left_idx = i - 1
            right_idx = i + 1
            left_task = segs[left_idx]["taskKey"] if left_idx >= 0 else None
            right_task = segs[right_idx]["taskKey"] if right_idx < n else None
            # Prefer a neighbour with a concrete taskKey that's still kept.
            absorber = None
            for idx, task in ((left_idx, left_task), (right_idx, right_task)):
                if task and idx >= 0 and idx < n and keep[idx] and segs[idx]["taskKey"]:
                    absorber = idx
                    break
            if absorber is not None:
                keep[i] = False
                absorbed_into[i] = absorber
        # Build merged segments
        for i, s in enumerate(segs):
            if not keep[i]:
                # Add this segment's duration to its absorber
                a = absorbed_into[i]
                if a is not None:
                    # Extend absorber's end if adjacent
                    segs[a]["end"] = max(segs[a]["end"], s["end"])
                continue
            merged_all.append(s)
    merged_all.sort(key=lambda s: (s["sessionId"], s["start"]))
    return merged_all


# ─── Focus-to-task attribution ─────────────────────────────────────────────

def compute_task_entries(usage_records, session_projects, assignments,
                         branch_map, cfg):
    """
    Join focus segments + usage records with task assignments.
    Returns:
      task_entries: {taskKey: {focus_seconds, api_seconds, tokens, sessions, dates}}
      pending:      list of (sessionId, start, end, cwd, gitBranch, seconds)
    """
    chat_segments = load_chat_segments()
    resolved = build_resolved_segments(chat_segments, assignments, branch_map, cfg)
    threshold = cfg.get("merge_short_visits_s", DEFAULT_CONFIG["merge_short_visits_s"])
    resolved = merge_short_visits(resolved, threshold)

    task_entries = defaultdict(lambda: {
        "focus_seconds": 0.0,
        "api_seconds": 0.0,
        "tokens": {"input": 0, "output": 0, "cached": 0, "thoughts": 0, "total": 0},
        "sessions": [],
        "dates": set(),
    })
    pending = []

    # Attribute each API call to the task active at that moment in its session
    for rec in usage_records:
        ts = parse_iso(rec.get("timestamp"))
        if not ts:
            continue
        sid = rec.get("sessionId", "?")
        project = session_projects.get(sid) or f"(unknown:{sid[:8]})"
        # Find the resolved slice covering ts in this session
        slice_rec = _find_slice(resolved, sid, ts)
        if slice_rec:
            task_key = slice_rec["taskKey"]
            source = slice_rec["source"]
        else:
            task_key, source = None, "pending"

        day = rec.get("localDate") or ts.strftime("%Y-%m-%d")

        if task_key:
            e = task_entries[task_key]
            e["tokens"]["input"] += rec.get("inputTokens", 0)
            e["tokens"]["output"] += rec.get("outputTokens", 0)
            e["tokens"]["cached"] += rec.get("cachedTokens", 0)
            e["tokens"]["thoughts"] += rec.get("thoughtsTokens", 0)
            e["tokens"]["total"] += rec.get("totalTokens", 0)
            e["api_seconds"] += rec.get("apiDurationMs", 0) / 1000
            e["dates"].add(day)
            if sid not in [s["sessionId"] for s in e["sessions"]]:
                e["sessions"].append({"sessionId": sid, "project": project})

    # Attribute focus time via resolved segments (with midnight split)
    midnight_split = cfg.get("midnight_split", True)
    for r in resolved:
        if not r["taskKey"]:
            # Pending segment — record for later asking
            dur = (r["end"] - r["start"]).total_seconds()
            pending.append({
                "sessionId": r["sessionId"],
                "start": r["start"],
                "end": r["end"],
                "cwd": r["cwd"],
                "gitBranch": r["gitBranch"],
                "origin": r.get("origin", "local"),
                "seconds": dur,
            })
            continue
        intervals = (split_at_midnight(r["start"], r["end"]) if midnight_split
                     else [(r["start"], r["end"])])
        for s, e in intervals:
            dur = (e - s).total_seconds()
            day = s.strftime("%Y-%m-%d")
            task_entries[r["taskKey"]]["focus_seconds"] += dur
            task_entries[r["taskKey"]]["dates"].add(day)

    for e in task_entries.values():
        e["dates"] = sorted(e["dates"])

    return dict(task_entries), pending


def _find_slice(resolved, session_id, ts):
    """Find the resolved segment whose [start, end) covers ts in session_id."""
    for r in resolved:
        if r["sessionId"] != session_id:
            continue
        if r["start"] <= ts < r["end"]:
            return r
    return None


# ─── CLI commands ──────────────────────────────────────────────────────────

def cmd_status(args):
    """Show current assignments and quick stats."""
    assignments = load_assignments()
    branch_map = load_branch_map()
    cfg = load_config()
    records = load_usage_records()
    session_projects = load_session_projects()

    if not any(r["taskKey"] for ranges in assignments.values() for r in ranges):
        print("\n  No task assignments yet.")
        print("  Use: python3 task_layer.py assign TASK-123 --url <url>")
        print("       python3 task_layer.py switch TASK-123   (mid-session)")
        print("       python3 task_layer.py map <branch> TASK-123 --repo <path>\n")
    else:
        print("\n  TASK ASSIGNMENTS")
        print("  " + "=" * 60)
        for sid, ranges in sorted(assignments.items()):
            shown = False
            for r in ranges:
                if not r["taskKey"]:
                    continue
                if not shown:
                    proj = session_projects.get(sid, "?")
                    print(f"\n  session {sid[:12]}...  project: {proj}")
                    shown = True
                f = r["fromTs"].strftime("%m-%d %H:%M") if r["fromTs"] else "?"
                t = r["toTs"].strftime("%m-%d %H:%M") if r["toTs"] else "now"
                print(f"    {r['taskKey']:<14} {f}→{t}  [{r['source']}]")

    task_entries, pending = compute_task_entries(
        records, session_projects, assignments, branch_map, cfg)
    if task_entries:
        print("\n  " + "=" * 60)
        print("  TIME BY TASK")
        print("  " + "-" * 60)
        for task, entry in sorted(task_entries.items(), key=lambda x: -x[1]["focus_seconds"]):
            print(f"  {task:<16} focus={fmt_dur(entry['focus_seconds']):>8}  "
                  f"api={fmt_dur(entry['api_seconds']):>8}  "
                  f"tokens={fmt_tok(entry['tokens']['total'])}")

    if pending:
        total_p = sum(p["seconds"] for p in pending)
        print(f"\n  ⚠ {len(pending)} pending segment(s) — {fmt_dur(total_p)} unattributed")
        print("    Run `python3 task_layer.py gaps` to resolve, or `map` branches.")
    print()


def cmd_assign(args):
    """Assign a task to a session from now until changed.

    Writes an open per-time-range assignment (source="session-start"). If an
    open range already exists for this session, it is closed (a closure record
    is appended) — same mechanism as `switch`. Use `assign` for the first task
    of a session and `switch` for subsequent ones; behaviour is equivalent.
    """
    sid = args.session or get_latest_session_id()
    if not sid:
        print("ERROR: No session found. Specify --session.", file=sys.stderr)
        sys.exit(1)
    save_assignment(sid, args.task_key, args.url, args.note, source="session-start")
    print(f"Assigned {args.task_key} to session {sid[:12]}...")
    if args.url:
        print(f"  URL: {args.url}")


def cmd_switch(args):
    """Switch task mid-session: close current range, open new one."""
    sid = args.session or get_latest_session_id()
    if not sid:
        print("ERROR: No session found. Specify --session.", file=sys.stderr)
        sys.exit(1)
    save_assignment(sid, args.task_key, args.url, args.note,
                    source="switch", close_previous=True)
    print(f"Switched to {args.task_key} in session {sid[:12]}... "
          f"(previous range closed)")


def cmd_map(args):
    """Learn / list / forget a branch → task mapping."""
    bm = load_branch_map()
    if args.list:
        if not bm:
            print("\n  No branch mappings yet.\n")
            return
        print("\n  BRANCH → TASK MAPPINGS")
        print("  " + "=" * 60)
        for repo, branches in sorted(bm.items()):
            print(f"\n  {repo}")
            for br, task in sorted(branches.items()):
                print(f"    {br:<30} → {task}")
        print()
        return

    if not args.branch:
        print("ERROR: branch required (or use --list).", file=sys.stderr)
        sys.exit(1)

    repo = args.repo or os.getcwd()
    bm.setdefault(repo, {})

    if args.unmap:
        if args.branch in bm[repo]:
            del bm[repo][args.branch]
            # Drop the repo entry if it's now empty — keeps branch_map clean.
            if not bm[repo]:
                del bm[repo]
            save_branch_map(bm)
            print(f"Forgot mapping for '{args.branch}' in {repo}")
        else:
            print(f"No mapping for '{args.branch}' in {repo}")
        return

    if not args.task_key:
        print("ERROR: task_key required (or use --unmap).", file=sys.stderr)
        sys.exit(1)
    bm[repo][args.branch] = args.task_key
    save_branch_map(bm)
    print(f"Mapped '{args.branch}' → {args.task_key} in {repo}")
    print("  (applied retroactively across all history)")


def cmd_untrack(args):
    """Mark a session as explicitly untracked."""
    sid = args.session or get_latest_session_id()
    if not sid:
        print("ERROR: No session found.", file=sys.stderr)
        sys.exit(1)
    save_assignment(sid, task_key=None, note="explicitly untracked",
                    source="untracked")
    print(f"Marked session {sid[:12]}... as untracked")


def cmd_entries(args):
    """Produce time entries for submission."""
    assignments = load_assignments()
    branch_map = load_branch_map()
    cfg = load_config()
    records = load_usage_records()
    session_projects = load_session_projects()
    task_entries, pending = compute_task_entries(
        records, session_projects, assignments, branch_map, cfg)

    output = {
        "tasks": task_entries,
        "pending": [{"sessionId": p["sessionId"],
                     "start": p["start"].isoformat() if hasattr(p["start"], "isoformat") else p["start"],
                     "end": p["end"].isoformat() if hasattr(p["end"], "isoformat") else p["end"],
                     "cwd": p["cwd"], "gitBranch": p["gitBranch"],
                     "seconds": round(p["seconds"], 1)} for p in pending],
    }
    print(json.dumps(output, indent=2, default=str, ensure_ascii=False))


def cmd_gaps(args):
    """Show unassigned / unmapped segments."""
    assignments = load_assignments()
    branch_map = load_branch_map()
    cfg = load_config()
    records = load_usage_records()
    session_projects = load_session_projects()
    _, pending = compute_task_entries(
        records, session_projects, assignments, branch_map, cfg)

    print("\n  UNATTRIBUTED SEGMENTS")
    print("  " + "=" * 60)
    if not pending:
        print("  Everything attributed ✓")
        print()
        return

    # Group pending by (cwd, gitBranch, origin) for cleaner display
    by_key = defaultdict(list)
    for p in pending:
        by_key[(p["cwd"], p["gitBranch"], p.get("origin", "local"))].append(p)

    for (cwd, branch, origin), segs in sorted(by_key.items(), key=lambda x: -sum(s["seconds"] for s in x[1])):
        total = sum(s["seconds"] for s in segs)
        origin_tag = f"  [{origin}]" if origin != "local" else ""
        print(f"\n  {fmt_dur(total)}  branch={branch!r}  repo={cwd}{origin_tag}")
        for s in segs[:3]:
            st = s["start"].strftime("%m-%d %H:%M") if hasattr(s["start"], "strftime") else s["start"]
            print(f"    {st}  ({fmt_dur(s['seconds'])})")
        if len(segs) > 3:
            print(f"    ... and {len(segs) - 3} more")
        if branch and branch not in ("HEAD", "main", "master"):
            print(f"    → python3 task_layer.py map '{branch}' TASK-XXX --repo '{cwd}'")
    print()


def cmd_resolve(args):
    """
    Mark pending time as resolved. Called by the agent after the user
    attributes pending time (names a taskKey → map it) or says skip.
    Removes the matching entries so they aren't asked about again.

    With --all: clears everything.
    With --session: clears that session's pending.
    With --branch/--repo: clears matching branch in repo.
    """
    if args.all:
        n = clear_pending()
        print(f"Cleared {n} pending entr{'y' if n==1 else 'ies'}.")
        return
    n = clear_pending(session_id=args.session, git_branch=args.branch, repo=args.repo)
    filters = []
    if args.session: filters.append(f"session={args.session[:12]}")
    if args.branch: filters.append(f"branch={args.branch!r}")
    if args.repo: filters.append(f"repo={args.repo}")
    flt = ", ".join(filters) if filters else "(no filter)"
    print(f"Cleared {n} pending entr{'y' if n==1 else 'ies'} matching {flt}.")


def cmd_pending(args):
    """Show accumulated unattributed time (fallback when agent stayed silent).

    Source of truth is `compute_task_entries` (recomputed from raw logs), NOT
    pending.jsonl. The staging file drifts when hooks miss sessions or state
    is cleared during tests, so reading it produced inconsistent numbers vs
    `status` (which always recomputes). Both commands now share one source.

    Lists pending entries grouped by (repo, branch, origin), so the user can
    manually resolve via `map` / `untrack` / `resolve`.
    """
    cfg = load_config()
    records = load_usage_records()
    session_projects = load_session_projects()
    assignments = load_assignments()
    branch_map = load_branch_map()
    _, pending = compute_task_entries(records, session_projects, assignments,
                                      branch_map, cfg)
    if not pending:
        print("\n  No pending time — everything is attributed ✓\n")
        return

    from collections import defaultdict
    by_key = defaultdict(list)
    for p in pending:
        by_key[(p.get("cwd", "?"), p.get("gitBranch") or "?",
                p.get("origin", "local"))].append(p)

    print("\n  PENDING (unattributed time)")
    print("  " + "=" * 60)
    total_all = 0.0
    for (cwd, branch, origin), segs in sorted(
            by_key.items(), key=lambda x: -sum(s.get("seconds", 0) for s in x[1])):
        total = sum(s.get("seconds", 0) for s in segs)
        total_all += total
        origin_tag = f"  [{origin}]" if origin != "local" else ""
        print(f"\n  {fmt_dur(total)}  branch={branch!r}  repo={cwd}{origin_tag}")
        if branch not in ("HEAD", "main", "master", "?"):
            print(f"    → python3 task_layer.py map '{branch}' TASK-XXX --repo '{cwd}'")
    print("\n  " + "-" * 60)
    print(f"  TOTAL pending: {fmt_dur(total_all)}")
    print("  Resolve via `map`, `untrack`, or `resolve --all`\n")


def cmd_log(args):
    """Interactive per-session breakdown — show, then prompt to submit."""
    assignments = load_assignments()
    branch_map = load_branch_map()
    cfg = load_config()
    records = load_usage_records()
    session_projects = load_session_projects()
    task_entries, pending = compute_task_entries(
        records, session_projects, assignments, branch_map, cfg)

    sid = args.session or get_latest_session_id()
    print(f"\n  SESSION {sid[:12]}... — TIME BREAKDOWN")
    print("  " + "=" * 60)

    # Filter entries touching this session
    shown = False
    for task, e in sorted(task_entries.items(), key=lambda x: -x[1]["focus_seconds"]):
        if sid not in [s["sessionId"] for s in e["sessions"]]:
            continue
        shown = True
        print(f"\n  {task}")
        print(f"    focus: {fmt_dur(e['focus_seconds'])}   api: {fmt_dur(e['api_seconds'])}")
        print(f"    tokens: {fmt_tok(e['tokens']['total'])} "
              f"(in={fmt_tok(e['tokens']['input'])} out={fmt_tok(e['tokens']['output'])})")

    sess_pending = [p for p in pending if p["sessionId"] == sid]
    if sess_pending:
        total_p = sum(p["seconds"] for p in sess_pending)
        print(f"\n  ⚠ {fmt_dur(total_p)} unattributed in this session")

    if not shown and not sess_pending:
        print("\n  No tracked time for this session.")
    print("\n  (Submission via MCP `log_time` — see MCP_SERVER_SPEC.md)")
    print()


def main():
    ap = argparse.ArgumentParser(description="Task layer for Qwen usage tracker")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show assignments and time-by-task")
    sub.add_parser("entries", help="Produce time entries JSON for submission")
    sub.add_parser("gaps", help="Show unattributed segments")
    sub.add_parser("pending", help="Show accumulated unattributed time from pending.jsonl")
    p_log = sub.add_parser("log", help="Interactive per-session breakdown")
    p_log.add_argument("--session", help="Session ID (default: latest)")

    p_assign = sub.add_parser("assign", help="Assign a task to a session (whole session)")
    p_assign.add_argument("task_key", help="Task key, e.g. TASK-123")
    p_assign.add_argument("--url", help="Task URL")
    p_assign.add_argument("--session", help="Session ID (default: latest)")
    p_assign.add_argument("--note", help="Optional note")

    p_switch = sub.add_parser("switch", help="Switch task mid-session")
    p_switch.add_argument("task_key", help="Task key, e.g. TASK-456")
    p_switch.add_argument("--url", help="Task URL")
    p_switch.add_argument("--session", help="Session ID (default: latest)")
    p_switch.add_argument("--note", help="Optional note")

    p_map = sub.add_parser("map", help="Learn/list/forget branch → task mapping")
    p_map.add_argument("branch", nargs="?", help="Branch name")
    p_map.add_argument("task_key", nargs="?", help="Task key")
    p_map.add_argument("--repo", help="Repository path (default: cwd)")
    p_map.add_argument("--unmap", action="store_true", help="Forget mapping")
    p_map.add_argument("--list", action="store_true", help="List all mappings")

    p_untrack = sub.add_parser("untrack", help="Mark session as not tracked")
    p_untrack.add_argument("--session", help="Session ID (default: latest)")

    p_resolve = sub.add_parser("resolve",
                               help="Mark pending time as resolved (clears from pending.jsonl)")
    p_resolve.add_argument("--all", action="store_true", help="Clear all pending")
    p_resolve.add_argument("--session", help="Clear pending for this session")
    p_resolve.add_argument("--branch", help="Clear pending for this branch")
    p_resolve.add_argument("--repo", help="Clear pending for this repo")

    args = ap.parse_args()

    if args.command == "status":
        cmd_status(args)
    elif args.command == "assign":
        cmd_assign(args)
    elif args.command == "switch":
        cmd_switch(args)
    elif args.command == "map":
        cmd_map(args)
    elif args.command == "untrack":
        cmd_untrack(args)
    elif args.command == "entries":
        cmd_entries(args)
    elif args.command == "gaps":
        cmd_gaps(args)
    elif args.command == "pending":
        cmd_pending(args)
    elif args.command == "log":
        cmd_log(args)
    elif args.command == "resolve":
        cmd_resolve(args)


if __name__ == "__main__":
    main()
