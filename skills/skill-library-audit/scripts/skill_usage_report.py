#!/usr/bin/env python3
"""Skill-library usage & freshness report — see SKILL.md.

Per skill: skill_view load count + last-used date (state.db), SKILL.md and
newest-content mtimes + CHANGELOG head (skill dirs).

Usage:  python3 skill_usage_report.py [--top N | --all] [--json]

Baked-in lessons: role='assistant' filter (tool-result rows match the same
LIKE but hold no calls); content mtimes only (__pycache__/, *.db, *.log,
*.csv change on every run, not on edits); counts are a floor.
"""
import argparse
import datetime
import glob
import json
import os
import sqlite3
from collections import Counter

HERMES = os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes")
SKILLS_DIR = os.path.join(HERMES, "skills")
DB_PATH = os.path.join(HERMES, "state.db")

# Files that change on every RUN of a skill, not on edits — excluded from freshness.
SKIP_SUFFIXES = (".pyc", ".db", ".db-wal", ".db-shm", ".log", ".csv")


def usage_from_state_db():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        counts, last_used = Counter(), {}
        sql = ("SELECT timestamp, tool_calls FROM messages "
               "WHERE role='assistant' AND tool_calls LIKE '%skill_view%'")
        for ts, tcs in con.execute(sql):
            try:
                calls = json.loads(tcs) if tcs else []
            except Exception:
                continue
            if not isinstance(calls, list):
                continue
            for call in calls:
                fn = (call or {}).get("function") or {}
                if fn.get("name") != "skill_view":
                    continue
                try:
                    name = json.loads(fn.get("arguments") or "{}").get("name")
                except Exception:
                    name = None
                if not name:
                    continue
                counts[name] += 1
                last_used[name] = max(last_used.get(name, 0.0), float(ts))
        return counts, last_used
    finally:
        con.close()


def discover_skills():
    skills = {}
    for path in glob.glob(os.path.join(SKILLS_DIR, "**", "SKILL.md"), recursive=True):
        if f"{os.sep}.archive{os.sep}" in path:
            continue
        d = os.path.dirname(path)
        skills[os.path.basename(d)] = d
    return skills


def newest_content_mtime(d):
    newest, newest_file = 0.0, ""
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for f in files:
            if f.endswith(SKIP_SUFFIXES):
                continue
            fp = os.path.join(root, f)
            try:
                m = os.path.getmtime(fp)
            except OSError:
                continue
            if m > newest:
                newest, newest_file = m, fp
    return newest, newest_file


def changelog_head(d):
    for rel in ("CHANGELOG.md", os.path.join("references", "changelog.md")):
        fp = os.path.join(d, rel)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("## "):
                        return line.lstrip("# ").strip()
        except OSError:
            pass
    return ""


def fmt_day(ts):
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser(description="Skill-library usage & freshness report")
    ap.add_argument("--top", type=int, default=20, help="rows to show (default 20)")
    ap.add_argument("--all", action="store_true", help="show every loaded skill")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    counts, last_used = usage_from_state_db()
    skills = discover_skills()

    ranked = counts.most_common(None if args.all else args.top)
    rows = []
    for name, uses in ranked:
        d = skills.get(name, "")
        sk_md = os.path.join(d, "SKILL.md") if d else ""
        sk_m = os.path.getmtime(sk_md) if sk_md and os.path.exists(sk_md) else 0.0
        content_m, _ = newest_content_mtime(d) if d else (0.0, "")
        rows.append({
            "skill": name,
            "uses": uses,
            "last_used": fmt_day(last_used.get(name, 0.0)),
            "skill_md_updated": fmt_day(sk_m),
            "content_updated": fmt_day(content_m),
            "changelog_head": changelog_head(d) if d else "",
            "dir": d,
        })

    never_loaded = sorted(set(skills) - set(counts))
    summary = {
        "total_views": sum(counts.values()),
        "distinct_skills": len(counts),
        "installed_skills": len(skills),
        "never_loaded_count": len(never_loaded),
    }
    if args.json:
        print(json.dumps({"summary": summary, "rows": rows,
                          "never_loaded": never_loaded}, indent=2))
        return

    print(f"skill_view loads: {summary['total_views']} across "
          f"{summary['distinct_skills']} of {summary['installed_skills']} installed skills "
          f"({summary['never_loaded_count']} never loaded)")
    print("Counts cover agent loads only — cron runs and work done over SSH don't register; "
          "counts are a floor (compaction/pruning/repairs drop history).\n")
    header = (f"{'#':>3}  {'skill':<30}{'uses':>5}  {'last used':<11}"
              f"{'updated':<11}{'changelog head'}")
    print(header)
    print("-" * len(header))
    for i, r in enumerate(rows, 1):
        updated = max(r["skill_md_updated"], r["content_updated"])
        print(f"{i:>3}  {r['skill']:<30}{r['uses']:>5}  "
              f"{r['last_used']:<11}{updated:<11}{r['changelog_head']}")


if __name__ == "__main__":
    main()
