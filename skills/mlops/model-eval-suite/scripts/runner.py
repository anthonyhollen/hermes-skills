#!/usr/bin/env python3
"""
runner.py — model-eval-suite v2 execution + isolation harness.

Drives the shipped `hermes -z` headless runner for one (model x task) pair inside
an isolated sandbox HERMES_HOME, with a restricted toolset, a disposable workdir,
and a subset environment (only inference-provider creds). Captures the final
answer, the full tool-call trajectory (from the sandbox state.db), latency,
tokens, and error signals.

Validated mechanism (see DESIGN.md §3-§4):
  - sandbox HERMES_HOME => own state.db/sessions; live state.db never touched
  - HERMES_SESSION_ID pins the session; fresh sandbox => exactly one session
  - restricted -t toolset => side-effecting tools (discord/cron/spotify/...) absent
  - HERMES_MAX_ITERATIONS caps runaway loops; `timeout` caps wall-clock
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HA = Path(os.environ.get("HERMES_REAL_HOME", os.path.expanduser("~/.hermes")))
HAGENT = HA / "hermes-agent"
HERMES_BIN = HAGENT / "venv" / "bin" / "hermes"
PYBIN = HAGENT / "venv" / "bin" / "python"
EVAL_ROOT = Path(os.environ.get("EVAL_ROOT", os.path.expanduser("~/agent-eval")))

# provider name (as in provider_models_cache.json) -> env var that must be present
PROVIDER_KEY = {
    "minimax": "MINIMAX_API_KEY",
    "xiaomi": "XIAOMI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
INCUMBENT = {"name": "MiniMax-M2.7", "provider": "minimax", "model_id": "MiniMax-M2.7"}

# Only these env vars (plus the provider creds) cross into the agent subprocess.
SAFE_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")
ENV_KEYS = ("MINIMAX_API_KEY", "XIAOMI_API_KEY", "XIAOMI_BASE_URL", "MINIMAX_BASE_URL",
            "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")


def _read_env_file() -> dict:
    """Parse the real ~/.hermes/.env into a dict (KEY=VALUE lines)."""
    out = {}
    p = HA / ".env"
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def resolve_model(model_id: str) -> Optional[dict]:
    """Resolve an explicitly-requested model id to {name, provider, model_id},
    even if absent from provider_models_cache.json (e.g. MiniMax-M3, which the
    cache omits but the API serves). Provider inferred by name prefix; requires
    that provider's API key to be present."""
    mid = model_id.strip()
    low = mid.lower()
    if low.startswith("minimax"):
        prov = "minimax"
    elif low.startswith("mimo"):
        prov = "xiaomi"
    elif low.startswith("deepseek"):
        prov = "deepseek"
    else:
        return None
    envf = _read_env_file()
    if not envf.get(PROVIDER_KEY.get(prov, "")):
        return None
    return {"name": mid, "provider": prov, "model_id": mid}


def discover_models(include_incumbent: bool = False) -> list[dict]:
    cache = json.loads((HA / "provider_models_cache.json").read_text())
    envf = _read_env_file()
    have = {prov for prov, key in PROVIDER_KEY.items() if envf.get(key)}
    models = []
    for prov, blob in cache.items():
        if prov not in have:
            continue
        for mid in blob.get("models", []):
            if not include_incumbent and mid == INCUMBENT["model_id"]:
                continue
            models.append({"name": mid, "provider": prov, "model_id": mid})
    return models


def build_sandbox(home: Path) -> None:
    """Create an isolated HERMES_HOME: copied config, trimmed .env (inference
    creds only), symlinked skills, empty state/session dirs."""
    if home.is_symlink() or home.exists():
        shutil.rmtree(home, ignore_errors=True)
        if home.is_symlink():
            home.unlink()
    for d in ("sessions", "memories", "logs", "plans", "cron", "scripts"):
        (home / d).mkdir(parents=True, exist_ok=True)
    shutil.copy2(HA / "config.yaml", home / "config.yaml")
    if (HA / "SOUL.md").exists():
        shutil.copy2(HA / "SOUL.md", home / "SOUL.md")
    skills_link = home / "skills"
    if skills_link.is_symlink() or skills_link.exists():
        skills_link.unlink()
    skills_link.symlink_to(HA / "skills")
    envf = _read_env_file()
    env_lines = [f"{k}={envf[k]}" for k in ENV_KEYS if k in envf]
    (home / ".env").write_text("\n".join(env_lines) + "\n")
    (home / ".env").chmod(0o600)


def _subset_env(home: Path, session_id: str, max_iter: int) -> dict:
    base = {k: os.environ[k] for k in SAFE_ENV_PASSTHROUGH if k in os.environ}
    base["PATH"] = f"{HAGENT/'venv'/'bin'}:/usr/bin:/bin"
    base["LANG"] = base.get("LANG", "C.UTF-8")
    base.update({
        "HERMES_HOME": str(home),
        "HERMES_SKIP_CHMOD": "1",
        "HERMES_SESSION_ID": session_id,
        "HERMES_MAX_ITERATIONS": str(max_iter),
        "HERMES_REDACT_SECRETS": "1",
    })
    return base


@dataclass
class RunResult:
    model: str
    provider: str
    task_id: str
    ok: bool
    exit_code: int
    elapsed_s: float
    timed_out: bool
    answer: str = ""
    stderr: str = ""
    trajectory: list = field(default_factory=list)   # [{role, content, tool_calls, tool_name, finish_reason, tokens}]
    tool_calls: int = 0
    total_tokens: int = 0
    session_model: str = ""

    def to_compact(self) -> dict:
        d = self.__dict__.copy()
        d.pop("trajectory", None)
        d["answer_preview"] = (self.answer or "")[:300]
        d.pop("answer", None)
        d.pop("stderr", None)
        return d


def _read_trajectory(db: Path) -> tuple[list, dict]:
    """Read the (single) session's messages from a fresh sandbox state.db."""
    if not db.exists():
        return [], {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = con.cursor()
    sess = {}
    try:
        row = cur.execute(
            "select id, model, message_count, tool_call_count from sessions "
            "order by started_at desc limit 1"
        ).fetchone()
        if row:
            sess = {"id": row[0], "model": row[1], "message_count": row[2], "tool_call_count": row[3]}
    except Exception:
        pass
    traj = []
    try:
        rows = cur.execute(
            "select role, content, tool_calls, tool_name, finish_reason, token_count "
            "from messages order by id asc"
        ).fetchall()
        for role, content, tool_calls, tool_name, finish, toks in rows:
            tcs = []
            if tool_calls:
                try:
                    tcs = json.loads(tool_calls)
                except Exception:
                    tcs = [{"_unparsed": tool_calls}]
            traj.append({
                "role": role, "content": content or "", "tool_calls": tcs,
                "tool_name": tool_name, "finish_reason": finish, "tokens": toks or 0,
            })
    except Exception:
        pass
    con.close()
    return traj, sess


def run_task(model: dict, prompt: str, toolsets: list[str], *,
             timeout_s: int = 180, max_iter: int = 12,
             home: Optional[Path] = None, workdir: Optional[Path] = None,
             fixtures: Optional[dict] = None, task_id: str = "task") -> RunResult:
    """Run one (model x task). Returns a RunResult with answer + trajectory.

    fixtures: {filename: content} written into the disposable workdir before the
    run, so tasks operate on self-contained, secret-free inputs."""
    tag = f"{model['model_id']}__{task_id}".replace("/", "_").replace(" ", "_")
    home = home or (EVAL_ROOT / "homes" / tag)
    workdir = workdir or (EVAL_ROOT / "work" / tag)
    build_sandbox(home)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    for fname, content in (fixtures or {}).items():
        (workdir / fname).write_text(content)

    session_id = f"eval_{tag}"
    env = _subset_env(home, session_id, max_iter)
    cmd = [
        str(HERMES_BIN), "-z", prompt,
        "-m", model["model_id"], "--provider", model["provider"],
        "-t", ",".join(toolsets),
    ]
    start = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd, cwd=str(workdir), env=env,
            capture_output=True, text=True, timeout=timeout_s,
        )
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err, timed_out = 124, (e.stdout or "") if isinstance(e.stdout, str) else "", "TIMEOUT", True
    elapsed = round(time.time() - start, 2)

    traj, sess = _read_trajectory(home / "state.db")
    result = RunResult(
        model=model["model_id"], provider=model["provider"], task_id=task_id,
        ok=(rc == 0 and bool((out or "").strip())), exit_code=rc, elapsed_s=elapsed,
        timed_out=timed_out, answer=(out or "").strip(), stderr=(err or "")[:2000],
        trajectory=traj, tool_calls=sess.get("tool_call_count", 0) or sum(len(m["tool_calls"]) for m in traj),
        total_tokens=sum(m["tokens"] for m in traj), session_model=sess.get("model", ""),
    )
    # Sandbox + workdir are disposable scratch (each sandbox carries a ~2MB
    # models_dev_cache.json that Hermes regenerates). The trajectory is already
    # captured in `result`, so reclaim the space unless asked to keep it for
    # debugging (EVAL_KEEP_ARTIFACTS=1).
    if not os.environ.get("EVAL_KEEP_ARTIFACTS"):
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)
    return result


if __name__ == "__main__":
    # Self-test: confirm discovery + a single trivial run + trajectory parse.
    print("discovered candidates:")
    for m in discover_models():
        print("  ", m)
    print("\nrunning trivial task on mimo-v2.5 ...")
    r = run_task(
        {"name": "mimo-v2.5", "provider": "xiaomi", "model_id": "mimo-v2.5"},
        "Use the terminal to compute 17*23 and reply with ONLY the number.",
        ["terminal", "code_execution", "file"],
        timeout_s=150, max_iter=8, task_id="selftest",
    )
    print(json.dumps(r.to_compact(), indent=2))
    print(f"\ntrajectory ({len(r.trajectory)} msgs):")
    for m in r.trajectory:
        tcs = ", ".join(tc.get("function", {}).get("name", tc.get("_unparsed", "?"))
                        if isinstance(tc, dict) else str(tc) for tc in m["tool_calls"])
        print(f"  [{m['role']}] tools=[{tcs}] finish={m['finish_reason']} "
              f"content={m['content'][:80]!r}")
