#!/usr/bin/env python3
"""
eval_suite.py — model-eval-suite v2 orchestrator.

Pipeline: discover candidates -> run each (model x task) through the real Hermes
agent loop in isolation -> objective verify + reliability -> pairwise judge vs the
incumbent (MiniMax-M2.7) -> usage-weighted report.

Usage:
  python3 eval_suite.py [--models a,b] [--tasks t1,t2] [--judge minimax-m3|claude-external]
                        [--resume-judge] [--concurrency N] [--out ~/agent-eval]

Default judge = minimax-m3 (autonomous). Neutral judging:
  python3 eval_suite.py --judge claude-external      # runs + writes judge_queue.jsonl, stops
  # (Claude reads judge_queue.jsonl, writes judge_verdicts.jsonl)
  python3 eval_suite.py --judge claude-external --resume-judge   # scores from verdicts
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner
import verifiers
import judge

SUITE_DIR = Path(__file__).resolve().parent
OUT = Path(runner.EVAL_ROOT)


def load_tasks(path: str = "tasks.json") -> list:
    p = path if Path(path).is_absolute() else str(SUITE_DIR / path)
    return json.loads(Path(p).read_text())


def run_consistency(models: list, tasks: list, repeat: int, concurrency: int, out: Path):
    """Run each (model x task) `repeat` times; score pass^k + latency variance.
    Fully automated (objective verifiers + reliability) — no pairwise judge."""
    import statistics
    tasks_by_id = {t["id"]: t for t in tasks}
    jobs = [(m, t, rep) for m in models for t in tasks for rep in range(repeat)]
    print(f"running {len(jobs)} jobs ({len(models)} models x {len(tasks)} tasks x {repeat} reps), "
          f"concurrency={concurrency} ...", flush=True)

    def _do(m, t, rep):
        r = runner.run_task(m, t["prompt"], t["toolsets"],
                            timeout_s=t.get("timeout_s", 240), max_iter=t.get("max_iter", 20),
                            fixtures=t.get("fixtures"), task_id=f'{t["id"]}_rep{rep}')
        return m, t, rep, r

    raw = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(_do, m, t, rep) for (m, t, rep) in jobs]
        done = 0
        for fut in as_completed(futs):
            m, t, rep, r = fut.result(); raw.append((m, t, r)); done += 1
            passed, _ = verifiers.verify(t, r)
            print(f"  [{done}/{len(jobs)}] {m['model_id']:<17} {t['id']:<22} r{rep} "
                  f"{'PASS' if passed else 'FAIL':>4} {r.elapsed_s:>6.1f}s tools={r.tool_calls}", flush=True)
            d = out / "trajectories" / m["model_id"].replace("/", "_")
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{r.task_id.replace('/', '_')}.json").write_text(json.dumps(
                {"meta": r.to_compact(), "answer": r.answer, "trajectory": r.trajectory}, indent=2))

    # aggregate per model
    per = {}
    for m, t, r in raw:
        passed, why = verifiers.verify(t, r)
        rel = verifiers.reliability(r)
        w = float(t.get("weight", 1))
        c = per.setdefault(m["model_id"], {"runs": 0, "run_pass": 0, "w_run": 0.0, "w_run_pass": 0.0,
                                           "timeouts": 0, "api_errors": 0, "refusals": 0, "malformed": 0,
                                           "lat": [], "by_task": {}})
        c["runs"] += 1; c["run_pass"] += int(passed)
        c["w_run"] += w; c["w_run_pass"] += w if passed else 0
        c["timeouts"] += int(rel["timed_out"]); c["api_errors"] += int(rel["api_error"])
        c["refusals"] += int(rel["refusal"]); c["malformed"] += rel["malformed_tool_calls"]
        c["lat"].append(r.elapsed_s)
        bt = c["by_task"].setdefault(t["id"], {"passes": 0, "n": 0, "weight": w})
        bt["passes"] += int(passed); bt["n"] += 1

    for m, c in per.items():
        lat = c.pop("lat")
        c["mean_pass_rate"] = round(c["w_run_pass"] / (c["w_run"] or 1), 3)         # weighted over all runs
        # pass^k: fraction of tasks (usage-weighted) where ALL reps passed
        wk = sum(bt["weight"] for bt in c["by_task"].values())
        c["passk_rate"] = round(sum(bt["weight"] for bt in c["by_task"].values()
                                    if bt["passes"] == bt["n"]) / (wk or 1), 3)
        # flaky tasks: passed some-but-not-all reps
        c["flaky_tasks"] = sorted(tid for tid, bt in c["by_task"].items()
                                  if 0 < bt["passes"] < bt["n"])
        c["failed_tasks"] = sorted(tid for tid, bt in c["by_task"].items() if bt["passes"] == 0)
        c["p50_latency_s"] = round(statistics.median(lat), 1) if lat else 0
        c["latency_stdev_s"] = round(statistics.pstdev(lat), 1) if len(lat) > 1 else 0

    (out / "consistency.json").write_text(json.dumps(per, indent=2))

    print("\n" + "=" * 92)
    print(f"  CONSISTENCY — each task run {repeat}x  (pass^k = all reps passed; usage-weighted)")
    print("=" * 92)
    print(f"{'model':<18}{'pass^k':>8}{'mean':>7}{'TO':>5}{'apierr':>7}{'refuse':>7}{'malf':>6}{'p50s':>7}{'lat_sd':>7}  flaky/failed")
    for m, c in sorted(per.items(), key=lambda x: (-x[1]['passk_rate'], -x[1]['mean_pass_rate'])):
        marks = []
        if c["flaky_tasks"]: marks.append("flaky:" + ",".join(c["flaky_tasks"]))
        if c["failed_tasks"]: marks.append("FAIL:" + ",".join(c["failed_tasks"]))
        print(f"{m:<18}{c['passk_rate']:>8.2f}{c['mean_pass_rate']:>7.2f}{c['timeouts']:>5}{c['api_errors']:>7}"
              f"{c['refusals']:>7}{c['malformed']:>6}{c['p50_latency_s']:>7.1f}{c['latency_stdev_s']:>7.1f}  {'; '.join(marks)}")
    print(f"\nconsistency report: {out/'consistency.json'}")


def run_all(models: list, tasks: list, concurrency: int) -> list:
    jobs = [(m, t) for m in models for t in tasks]
    results = [None] * len(jobs)
    print(f"running {len(jobs)} (model x task) jobs, concurrency={concurrency} ...", flush=True)

    def _do(i, m, t):
        r = runner.run_task(m, t["prompt"], t["toolsets"],
                            timeout_s=t.get("timeout_s", 180), max_iter=t.get("max_iter", 12),
                            fixtures=t.get("fixtures"), task_id=t["id"])
        return i, r

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(_do, i, m, t) for i, (m, t) in enumerate(jobs)]
        done = 0
        for fut in as_completed(futs):
            i, r = fut.result()
            results[i] = r
            done += 1
            m, t = jobs[i]
            print(f"  [{done}/{len(jobs)}] {m['model_id']:<16} {t['id']:<22} "
                  f"{'ok' if r.ok else 'ERR':>3} {r.elapsed_s:>6.1f}s tools={r.tool_calls}", flush=True)
    return results


def objective_report(results: list, tasks_by_id: dict) -> dict:
    per = {}
    for r in results:
        t = tasks_by_id[r.task_id]
        passed, why = verifiers.verify(t, r)
        rel = verifiers.reliability(r)
        w = float(t.get("weight", 1))
        c = per.setdefault(r.model, {"pass": 0, "fail": 0, "w_pass": 0.0, "w_total": 0.0,
                                     "timeouts": 0, "api_errors": 0, "refusals": 0,
                                     "malformed": 0, "lat": [], "tasks": []})
        c["pass" if passed else "fail"] += 1
        c["w_pass"] += w if passed else 0
        c["w_total"] += w
        c["timeouts"] += int(rel["timed_out"])
        c["api_errors"] += int(rel["api_error"])
        c["refusals"] += int(rel["refusal"])
        c["malformed"] += rel["malformed_tool_calls"]
        c["lat"].append(rel["elapsed_s"])
        c["tasks"].append({"task": r.task_id, "passed": passed, "why": why, **{k: rel[k] for k in
                          ("timed_out", "api_error", "refusal", "malformed_tool_calls", "trajectory_len")}})
    for m, c in per.items():
        n = c["pass"] + c["fail"]
        lat = sorted(c["lat"]); c.pop("lat")
        c["objective_pass_rate"] = round(c["w_pass"] / (c["w_total"] or 1), 3)
        c["n"] = n
        c["p50_latency_s"] = lat[len(lat) // 2] if lat else 0
        c["p95_latency_s"] = lat[max(0, int(len(lat) * 0.95) - 1)] if lat else 0
    return per


def save_trajectories(results: list):
    tdir = OUT / "trajectories"
    for r in results:
        d = tdir / r.model.replace("/", "_")
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{r.task_id}.json").write_text(json.dumps(
            {"meta": r.to_compact(), "answer": r.answer, "trajectory": r.trajectory}, indent=2))


def print_summary(obj: dict, pair: dict | None):
    print("\n" + "=" * 78)
    print("  SUMMARY — objective pass-rate (usage-weighted) + reliability")
    print("=" * 78)
    print(f"{'model':<18}{'obj_pass':>9}{'pass/n':>9}{'timeout':>8}{'apierr':>7}{'refuse':>7}{'malf':>6}{'p50s':>7}{'p95s':>7}")
    for m, c in sorted(obj.items(), key=lambda x: -x[1]["objective_pass_rate"]):
        print(f"{m:<18}{c['objective_pass_rate']:>9.2f}{c['pass']:>5}/{c['n']:<3}"
              f"{c['timeouts']:>8}{c['api_errors']:>7}{c['refusals']:>7}{c['malformed']:>6}"
              f"{c['p50_latency_s']:>7.1f}{c['p95_latency_s']:>7.1f}")
    if pair:
        print("\n  PAIRWISE vs incumbent (MiniMax-M2.7) — usage-weighted win-rate")
        print(f"{'model':<18}{'win_rate':>9}{'W':>4}{'T':>4}{'L':>4}")
        for m, c in sorted(pair.items(), key=lambda x: -x[1]["weighted_win_rate"]):
            print(f"{m:<18}{c['weighted_win_rate']:>9.2f}{c['win']:>4}{c['tie']:>4}{c['loss']:>4}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="")
    ap.add_argument("--tasks", default="")
    ap.add_argument("--tasks-file", default="tasks.json")
    ap.add_argument("--repeat", type=int, default=1, help=">1 enables consistency mode (no pairwise judge)")
    ap.add_argument("--judge", default="minimax-m3", choices=["minimax-m3", "claude-external"])
    ap.add_argument("--resume-judge", action="store_true")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks(args.tasks_file)
    if args.tasks:
        want = set(args.tasks.split(","))
        tasks = [t for t in tasks if t["id"] in want]
    tasks_by_id = {t["id"]: t for t in tasks}

    candidates = runner.discover_models(include_incumbent=False)
    if args.models:
        want = [w.strip() for w in args.models.split(",") if w.strip()]
        disc = {m["model_id"]: m for m in candidates}
        candidates = []
        for w in want:
            if w in disc:
                candidates.append(disc[w])
            else:
                m = runner.resolve_model(w)
                if m:
                    candidates.append(m)
                    print(f"note: '{w}' not in discovery cache; resolved via provider prefix ({m['provider']})")
                else:
                    print(f"WARNING: skipping unknown/uncredentialed model '{w}'")
    models = [runner.INCUMBENT] + candidates
    print(f"incumbent: {runner.INCUMBENT['model_id']}")
    print(f"candidates: {[m['model_id'] for m in candidates]}")
    print(f"tasks: {[t['id'] for t in tasks]}")

    # ── consistency mode: repeat each task, score pass^k + variance (no judge) ──
    if args.repeat > 1:
        run_consistency(models, tasks, args.repeat, args.concurrency, out)
        return

    # ── resume-judge path: skip running, score existing verdicts ──
    if args.resume_judge:
        keys = json.loads((out / "judge_key.json").read_text())
        keys = list(keys.values())
        verdicts = judge.load_verdicts(out / "judge_verdicts.jsonl")
        pair = judge.score(verdicts, keys, tasks_by_id)
        obj = json.loads((out / "objective.json").read_text())
        report = {"objective": obj, "pairwise": pair, "judge": "claude-external"}
        (out / "report.json").write_text(json.dumps(report, indent=2))
        print_summary(obj, pair)
        print(f"\nfinal report: {out/'report.json'}")
        return

    # ── run everything ──
    results = run_all(models, tasks, args.concurrency)
    save_trajectories(results)
    obj = objective_report(results, tasks_by_id)
    (out / "objective.json").write_text(json.dumps(obj, indent=2))

    # ── build pairs + judge ──
    packets, keys = judge.build_pairs(results, tasks_by_id)
    if args.judge == "claude-external":
        q = judge.write_queue(packets, keys, out)
        print(f"\n[claude-external] wrote {len(packets)} pairs to {q}")
        print("Claude: read judge_queue.jsonl, write judge_verdicts.jsonl "
              '(one JSON/line: {"pair_id","winner":"A|B|tie","confidence","rationale"}),')
        print(f"then rerun: python3 eval_suite.py --judge claude-external --resume-judge")
        print_summary(obj, None)
        return

    print(f"\njudging {len(packets)} pairs with MiniMax-M3 ...", flush=True)
    verdicts = judge.judge_minimax(packets, judge_model=judge.DEFAULT_JUDGE)
    with (out / "judge_verdicts.jsonl").open("w") as f:
        for v in verdicts:
            f.write(json.dumps(v) + "\n")
    (out / "judge_key.json").write_text(json.dumps({k["pair_id"]: k for k in keys}, indent=2))
    pair = judge.score(verdicts, keys, tasks_by_id)
    report = {"objective": obj, "pairwise": pair, "judge": judge.DEFAULT_JUDGE}
    (out / "report.json").write_text(json.dumps(report, indent=2))
    print_summary(obj, pair)
    print(f"\nfinal report: {out/'report.json'}")


if __name__ == "__main__":
    main()
