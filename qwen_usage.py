#!/usr/bin/env python3
"""
time-tracker — track tokens and focused time from Qwen Code logs.

MODEL:
  Focus is a step function that changes ONLY at user-initiated ("main") API calls.
  Between two consecutive main calls, ALL activity (background subagents, memory
  extractors, etc.) is attributed to the project of the earlier main call — the user
  was in that window waiting or reviewing.

  When a main call appears in a different project, focus switches there.
  After the last main call, focus continues up to IDLE_TIMEOUT (300s) then stops.

  This correctly handles parallel Qwen instances: each window gets focus only while
  the user is actively working in it, and switches are detected by the next prompt.

SOURCES:
  ~/.qwen/usage/token-usage-*.jsonl  — per-API-call: tokens, apiDurationMs, sessionId, source
  ~/.qwen/projects/**/chats/*.jsonl  — sessionId → project (cwd)

JOIN: usage log has tokens+time but no project path; chat log has cwd. Join on sessionId.
"""
import json, os, sys, argparse, csv
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# Data directories: the local ~/.qwen plus any remote mirrors under
# ~/.qwen-remote/<host>/ (populated by sync-remote.sh over rsync). Reading all
# of them lets the tracker attribute sessions from SSH'd machines alongside
# local ones. State files (assignments, branch_map, pending, config) stay in
# the LOCAL skill dir only — never synced — so there's a single source of truth.
def _discover_qwen_dirs():
    dirs = [Path(os.environ.get("QWEN_DATA_DIR", os.path.expanduser("~/.qwen")))]
    remote_root = Path(os.path.expanduser("~/.qwen-remote"))
    if remote_root.is_dir():
        for host_dir in sorted(remote_root.iterdir()):
            if host_dir.is_dir() and (host_dir / "usage").is_dir():
                dirs.append(host_dir)
    return dirs


QWEN_DIRS = _discover_qwen_dirs()
# The first entry is always the local machine — used for state files.
QWEN_DIR = QWEN_DIRS[0]
IDLE_TIMEOUT = 300  # seconds — max focus continuation after last activity

# Timezone for day-boundary attribution and display. Reads from the tracker's
# config.json; falls back to MSK (+03:00) if not set. Format: ±HH:MM.
def _load_tz_offset():
    cfg_path = QWEN_DIR / "skills" / "time-tracker" / "config.json"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f).get("timezone", "+03:00")
    except Exception:
        return "+03:00"


TZ_OFFSET = _load_tz_offset()


def _load_horizon_days():
    """How many days back to scan logs. Bounds work + hook timeout."""
    cfg_path = QWEN_DIR / "skills" / "time-tracker" / "config.json"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return int(json.load(f).get("history_horizon_days", 30))
    except Exception:
        return 30


HORIZON_DAYS = _load_horizon_days()


def _load_idle_timeout():
    """Max focus continuation after last user prompt (seconds). From config."""
    cfg_path = QWEN_DIR / "skills" / "time-tracker" / "config.json"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return int(json.load(f).get("idle_timeout", IDLE_TIMEOUT))
    except Exception:
        return IDLE_TIMEOUT


def horizon_cutoff():
    """Timezone-aware datetime: anything older than this is ignored."""
    from datetime import timezone, timedelta as _td
    sign = 1 if TZ_OFFSET.startswith("+") else -1
    hh, mm = TZ_OFFSET[1:].split(":")
    tz = timezone(_td(hours=sign * int(hh), minutes=sign * int(mm)))
    return datetime.now(tz) - _td(days=HORIZON_DAYS)


def _origin_for(qwen_dir):
    """Return 'local' for the primary dir, 'remote:<host>' for mirrors."""
    if qwen_dir == QWEN_DIRS[0]:
        return "local"
    # Mirror layout: ~/.qwen-remote/<host>/
    return f"remote:{qwen_dir.name}"


# ─── Parsing ───────────────────────────────────────────────────────────────

def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def load_usage_records():
    """Load usage records from ALL discovered qwen dirs (local + remote mirrors).

    Each record is enriched with '_origin' ('local' or 'remote:<host>') so
    downstream code can tell where a session came from. Records older than
    HORIZON_DAYS are skipped to bound work and hook timeouts.
    """
    cutoff = horizon_cutoff()
    records = []
    for qwen_dir in QWEN_DIRS:
        usage_dir = qwen_dir / "usage"
        if not usage_dir.exists():
            continue
        origin = _origin_for(qwen_dir)
        for f in sorted(usage_dir.glob("*.jsonl")):
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Filter by horizon — skip records older than cutoff.
                    ts = parse_iso(rec.get("timestamp"))
                    if ts and ts < cutoff:
                        continue
                    rec["_origin"] = origin
                    records.append(rec)
    return records


def load_session_projects():
    """Build sessionId → project (cwd) mapping from chat logs across all dirs."""
    cutoff = horizon_cutoff()
    mapping = {}
    for qwen_dir in QWEN_DIRS:
        projects_dir = qwen_dir / "projects"
        if not projects_dir.exists():
            continue
        for chat_file in projects_dir.rglob("*.jsonl"):
            sid = None
            project = None
            with open(chat_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Only map sessions that have recent activity.
                    ts = parse_iso(d.get("timestamp"))
                    if ts and ts >= cutoff:
                        sid = d.get("sessionId", sid)
                        project = d.get("cwd", project) or project
            if sid and project:
                mapping[sid] = project
    return mapping


def load_chat_segments():
    """
    Per-session chronological list of (start_ts, end_ts, cwd, gitBranch) slices,
    sliced wherever cwd or gitBranch changes between consecutive messages.

    Returns {sessionId: [{"start": datetime, "end": datetime, "cwd": str, "gitBranch": str}]}.

    Used by the task layer to know, at any moment, which branch the user was on
    — enabling task boundaries from branch switches and per-branch attribution.
    Background-only logs (no user/assistant turn) collapse into the preceding slice.
    """
    segments = {}
    cutoff = horizon_cutoff()
    for qwen_dir in QWEN_DIRS:
        projects_dir = qwen_dir / "projects"
        if not projects_dir.exists():
            continue
        origin = _origin_for(qwen_dir)
        for chat_file in projects_dir.rglob("*.jsonl"):
            sid = None
            rows = []  # (ts, cwd, gitBranch)
            with open(chat_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = d.get("sessionId", sid)
                    ts = parse_iso(d.get("timestamp"))
                    if not ts or ts < cutoff:
                        continue
                    rows.append((ts, d.get("cwd"), d.get("gitBranch")))
            if not sid or not rows:
                continue
            rows.sort(key=lambda r: r[0])
            # Coalesce into slices: a new slice starts when cwd or gitBranch changes.
            slices = []
            cur_start, cur_cwd, cur_branch = rows[0]
            cur_end = rows[0][0]
            for ts, cwd, branch in rows[1:]:
                if cwd != cur_cwd or branch != cur_branch:
                    slices.append({"start": cur_start, "end": ts,
                                   "cwd": cur_cwd, "gitBranch": cur_branch,
                                   "origin": origin})
                    cur_start, cur_cwd, cur_branch = ts, cwd, branch
                cur_end = ts
            slices.append({"start": cur_start, "end": cur_end,
                           "cwd": cur_cwd, "gitBranch": cur_branch,
                           "origin": origin})
            segments[sid] = slices
    return segments


def split_at_midnight(start, end):
    """
    Split a [start, end) interval at local midnight boundaries (00:00 in
    TZ_OFFSET). Returns a list of (day_start, day_end) pairs where each falls
    within one local calendar day. Used so focus time is attributed to the day
    it actually occurred on, not the day the segment started.

    Converts tz-aware datetimes to wall-clock local time first, splits naively,
    then re-attaches the offset. Day labels in output match TZ_OFFSET.
    """
    from datetime import timezone
    if start >= end:
        return []
    sign = 1 if TZ_OFFSET.startswith("+") else -1
    hh, mm = TZ_OFFSET[1:].split(":")
    tz = timezone(timedelta(hours=sign * int(hh), minutes=sign * int(mm)))
    # Convert to local wall-clock for splitting.
    s = start.astimezone(tz).replace(tzinfo=None) if start.tzinfo else start
    e = end.astimezone(tz).replace(tzinfo=None) if end.tzinfo else end
    out = []
    cur = s
    while True:
        next_midnight = (cur + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        if e <= next_midnight:
            out.append((cur.replace(tzinfo=tz), e.replace(tzinfo=tz)))
            break
        out.append((cur.replace(tzinfo=tz), next_midnight.replace(tzinfo=tz)))
        cur = next_midnight
    return out


# ─── Focus model ───────────────────────────────────────────────────────────

def compute_focus_segments(usage_records, session_projects, idle_timeout=None):
    """
    The core algorithm. Returns a list of (start, end, project) segments.

    Focus changes ONLY at "main" calls. Between consecutive main calls, all
    time belongs to the project of the first. Idle gaps (>idle_timeout with no
    main call) cap the segment.

    idle_timeout: seconds — max focus continuation after last activity.
    Defaults to IDLE_TIMEOUT constant, which itself reads config.json.
    """
    if idle_timeout is None:
        idle_timeout = _load_idle_timeout()
    # Enrich all records with project + parsed timestamp
    calls = []
    for rec in usage_records:
        ts = parse_iso(rec.get("timestamp"))
        if not ts:
            continue
        sid = rec.get("sessionId", "?")
        project = session_projects.get(sid) or f"(unknown:{sid[:8]})"
        calls.append({
            "ts": ts,
            "project": project,
            "source": rec.get("source", "?"),
        })
    calls.sort(key=lambda c: c["ts"])

    # Extract main calls only — these define focus boundaries
    main_calls = [(c["ts"], c["project"]) for c in calls if c["source"] == "main"]
    main_calls.sort(key=lambda x: x[0])

    if not main_calls:
        return []

    segments = []  # (start, end, project)

    for i, (ts, project) in enumerate(main_calls):
        if i + 1 < len(main_calls):
            next_ts = main_calls[i + 1][0]
            gap = (next_ts - ts).total_seconds()
            if gap <= idle_timeout:
                end = next_ts
            else:
                # Idle gap — cap at idle_timeout after last activity
                end = ts + timedelta(seconds=idle_timeout)
        else:
            # Last main call — extend by idle_timeout
            end = ts + timedelta(seconds=idle_timeout)
        segments.append((ts, end, project))

    return segments


def assign_calls_to_focus(calls, segments):
    """
    Assign each API call to a focus segment (i.e., to the project that had focus).
    Returns list of (call, project) where project is the FOCUSED project, not the
    call's own project (they may differ for orphaned background calls).
    """
    result = []
    for call in calls:
        ts = call["ts"]
        focused_project = None
        for start, end, project in segments:
            if start <= ts < end:
                focused_project = project
                break
        result.append((call, focused_project))
    return result


# ─── Aggregation ───────────────────────────────────────────────────────────

def build_stats(usage_records, session_projects):
    segments = compute_focus_segments(usage_records, session_projects)

    # Build enriched call list
    calls = []
    for rec in usage_records:
        ts = parse_iso(rec.get("timestamp"))
        if not ts:
            continue
        sid = rec.get("sessionId", "?")
        project = session_projects.get(sid) or f"(unknown:{sid[:8]})"
        calls.append({
            "ts": ts,
            "project": project,
            "source": rec.get("source", "?"),
            "model": rec.get("model", "?"),
            "input": rec.get("inputTokens", 0),
            "output": rec.get("outputTokens", 0),
            "cached": rec.get("cachedTokens", 0),
            "thoughts": rec.get("thoughtsTokens", 0),
            "total": rec.get("totalTokens", 0),
            "api_ms": rec.get("apiDurationMs", 0),
            "local_date": rec.get("localDate", ""),
        })
    calls.sort(key=lambda c: c["ts"])

    # Assign each call to its focused project
    assigned = assign_calls_to_focus(calls, segments)

    # Per-project stats (attributed by FOCUS, not by call origin)
    projects = defaultdict(lambda: {
        "input": 0, "output": 0, "cached": 0, "thoughts": 0, "total": 0,
        "api_calls": 0, "api_ms": 0,
        "models": defaultdict(int),
        "sources": defaultdict(int),
        "focus_seconds": 0.0,
        "sessions": set(),
    })

    for call, focused_project in assigned:
        if not focused_project:
            continue  # call outside any focus segment (orphaned after idle)
        p = projects[focused_project]
        p["input"] += call["input"]
        p["output"] += call["output"]
        p["cached"] += call["cached"]
        p["thoughts"] += call["thoughts"]
        p["total"] += call["total"]
        p["api_calls"] += 1
        p["api_ms"] += call["api_ms"]
        p["models"][call["model"]] += 1
        p["sources"][call["source"]] += 1
        p["sessions"].add(call["project"])  # track origin projects

    # Focus time per project
    for start, end, project in segments:
        projects[project]["focus_seconds"] += (end - start).total_seconds()

    for p in projects.values():
        p["sessions"] = len(p["sessions"])

    # Per-day stats
    days = defaultdict(lambda: {
        "input": 0, "output": 0, "cached": 0, "thoughts": 0, "total": 0,
        "api_calls": 0, "api_ms": 0, "focus_seconds": 0.0,
    })
    for call, focused_project in assigned:
        if not focused_project:
            continue
        day = call["local_date"]
        if not day:
            continue
        d = days[day]
        d["input"] += call["input"]
        d["output"] += call["output"]
        d["cached"] += call["cached"]
        d["thoughts"] += call["thoughts"]
        d["total"] += call["total"]
        d["api_calls"] += 1
        d["api_ms"] += call["api_ms"]
    for start, end, project in segments:
        day = start.strftime("%Y-%m-%d")
        days[day]["focus_seconds"] += (end - start).total_seconds()

    return dict(projects), dict(days), segments, calls


# ─── Formatting ────────────────────────────────────────────────────────────

def fmt_dur(s):
    if s is None or s < 0:
        return "—"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def fmt_tok(n):
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}K"
    return str(n)


def short(p):
    home = os.path.expanduser("~")
    if p.startswith(home):
        return "~" + p[len(home):]
    return p


def report(projects, days, segments, calls, args):
    if args.project:
        projects = {k: v for k, v in projects.items()
                    if args.project.lower() in k.lower()}

    if args.format == "json":
        out = {
            "projects": {k: {**v, "models": dict(v["models"]), "sources": dict(v["sources"])}
                         for k, v in projects.items()},
            "days": dict(days),
            "segments": [(s.isoformat(), e.isoformat(), p) for s, e, p in segments],
        }
        print(json.dumps(out, indent=2, default=str))
        return

    if args.format == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(["project", "focus_time_s", "api_calls", "input", "output",
                     "cached", "thoughts", "total", "api_time_s"])
        for name, p in sorted(projects.items()):
            w.writerow([name, round(p["focus_seconds"], 1), p["api_calls"],
                        p["input"], p["output"], p["cached"], p["thoughts"],
                        p["total"], round(p["api_ms"] / 1000, 1)])
        return

    # ── Table ──
    print()
    print("  QWEN CODE — FOCUS-BASED USAGE")
    print("  " + "=" * 66)

    for name, p in sorted(projects.items(), key=lambda x: -x[1]["focus_seconds"]):
        print()
        print(f"  📁 {short(name)}")
        print(f"     focus:    {fmt_dur(p['focus_seconds'])}   "
              f"({p['api_calls']} api calls, {p['sessions']} project(s) involved)")
        print(f"     tokens:   in={fmt_tok(p['input'])}  out={fmt_tok(p['output'])}  "
              f"cache={fmt_tok(p['cached'])}  think={fmt_tok(p['thoughts'])}  "
              f"╞═ {fmt_tok(p['total'])}")
        print(f"     api_time: {fmt_dur(p['api_ms'] / 1000)}")
        if p["sources"]:
            print(f"     sources:  {', '.join(f'{s}×{c}' for s, c in p['sources'].items())}")

    # Daily
    if days and not args.no_daily:
        print()
        print("  📅 BY DAY")
        print("  " + "-" * 66)
        print(f"  {'date':<12} {'calls':>5} {'in':>8} {'out':>8} {'total':>8} "
              f"{'api':>8} {'focus':>8}")
        for day in sorted(days):
            d = days[day]
            print(f"  {day:<12} {d['api_calls']:>5} {fmt_tok(d['input']):>8} "
                  f"{fmt_tok(d['output']):>8} {fmt_tok(d['total']):>8} "
                  f"{fmt_dur(d['api_ms'] / 1000):>8} {fmt_dur(d['focus_seconds']):>8}")

    # Timeline
    if segments and not args.no_timeline:
        print()
        print("  ⏱  FOCUS TIMELINE")
        print("  " + "-" * 66)
        for start, end, project in segments:
            dur = (end - start).total_seconds()
            print(f"  {start.strftime('%H:%M:%S')}→{end.strftime('%H:%M:%S')}  "
                  f"{fmt_dur(dur):>8}  {short(project)}")

    # Totals
    print()
    print("  " + "=" * 66)
    tf = sum(p["focus_seconds"] for p in projects.values())
    ta = sum(p["api_ms"] for p in projects.values()) / 1000
    tt = sum(p["total"] for p in projects.values())
    tc = sum(p["api_calls"] for p in projects.values())
    print(f"  TOTAL   focus={fmt_dur(tf)}   api={fmt_dur(ta)}   "
          f"calls={tc}   tokens={fmt_tok(tt)}")
    print()


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Track tokens and focused time from Qwen Code logs")
    ap.add_argument("-f", "--format", choices=["table", "json", "csv"], default="table")
    ap.add_argument("-p", "--project", default=None, help="Filter by project (substring)")
    ap.add_argument("--no-daily", action="store_true")
    ap.add_argument("--no-timeline", action="store_true")
    args = ap.parse_args()

    records = load_usage_records()
    if not records:
        print(f"No usage records in {QWEN_DIR / 'usage'}", file=sys.stderr)
        sys.exit(1)

    session_projects = load_session_projects()
    projects, days, segments, calls = build_stats(records, session_projects)
    report(projects, days, segments, calls, args)


if __name__ == "__main__":
    main()
