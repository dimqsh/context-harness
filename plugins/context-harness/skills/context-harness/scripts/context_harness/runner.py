"""Budgeted check runner: every test/check goes through here when the harness is in use.

Why: agent cost grows with the number of model calls times the context size. The
expensive tail is late re-runs after the work is already green, and repeated attempts
on one failing test. The runner keeps a countdown visible, refuses runs that cannot add
information, and stores full logs on disk while returning a short, exact digest.
A refusal never executes the command. An override is allowed, limited and recorded.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .shaping import digest, failed_names, strip_ansi

KINDS = ("test", "full", "other")
COUNTED = ("test", "full")
DEFAULT_BUDGET = {"max_runs": 12, "max_after_green": 1, "max_fail_streak": 3, "max_overrides": 2}
MAX_LOG_READ = 4 * 1024 * 1024


def ensure_schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS budgets(task_id TEXT NOT NULL, agent TEXT NOT NULL,
        max_runs INTEGER NOT NULL, max_after_green INTEGER NOT NULL, max_fail_streak INTEGER NOT NULL,
        max_overrides INTEGER NOT NULL, PRIMARY KEY(task_id, agent))""")
    con.execute("""CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL, agent TEXT NOT NULL, kind TEXT NOT NULL, command TEXT NOT NULL,
        cwd TEXT NOT NULL, status TEXT NOT NULL, exit_code INTEGER, duration_ms INTEGER, log TEXT,
        fingerprint TEXT, failed_tests TEXT NOT NULL DEFAULT '[]', digest TEXT, override TEXT,
        reason TEXT, created TEXT NOT NULL)""")


def set_budget(con, tid, agent, **limits):
    ensure_schema(con)
    values = {**DEFAULT_BUDGET, **{k: v for k, v in limits.items() if v is not None}}
    for key, value in values.items():
        if key not in DEFAULT_BUDGET or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 200:
            raise ValueError(f"Budget {key} must be an integer 0..200")
    con.execute("INSERT OR REPLACE INTO budgets VALUES (?,?,?,?,?,?)",
                (tid, agent, values["max_runs"], values["max_after_green"],
                 values["max_fail_streak"], values["max_overrides"]))
    return values


def budget_state(con, tid, agent):
    ensure_schema(con)
    row = con.execute("SELECT * FROM budgets WHERE task_id=? AND agent=?", (tid, agent)).fetchone()
    limits = {k: row[k] for k in DEFAULT_BUDGET} if row else dict(DEFAULT_BUDGET)
    runs = con.execute("SELECT * FROM runs WHERE task_id=? AND agent=? AND status='executed' ORDER BY id",
                       (tid, agent)).fetchall()
    overrides = con.execute("SELECT COUNT(*) FROM runs WHERE task_id=? AND agent=? AND override IS NOT NULL",
                            (tid, agent)).fetchone()[0]
    used = after_green = 0
    green_run = None
    streak = {}
    for r in runs:
        if r["override"]:
            streak = {}
        if r["kind"] not in COUNTED:
            continue
        used += 1
        if green_run is not None:
            after_green += 1
        if r["exit_code"] == 0:
            streak = {}
            if r["kind"] == "full" and green_run is None:
                green_run = r["id"]
        else:
            for name in json.loads(r["failed_tests"]):
                streak[name] = streak.get(name, 0) + 1
    hot = {k: v for k, v in streak.items() if v >= 2}
    return {"runs_left": max(0, limits["max_runs"] - used), "runs_used": used,
            "green": green_run is not None, "green_run": green_run,
            "after_green_left": (max(0, limits["max_after_green"] - after_green)
                                 if green_run is not None else None),
            "overrides_left": max(0, limits["max_overrides"] - overrides),
            "repeat_failures": dict(sorted(hot.items(), key=lambda kv: -kv[1])[:5]),
            "limits": limits}


def compact_budget(state):
    """What the agent needs every turn; limits and history stay in `budget`/`checkpoint`."""
    out = {"runs_left": state["runs_left"], "green": state["green"]}
    if state["green"]:
        out["after_green_left"] = state["after_green_left"]
    if state["overrides_left"] < state["limits"]["max_overrides"]:
        out["overrides_left"] = state["overrides_left"]
    if state["repeat_failures"]:
        out["repeat_failures"] = state["repeat_failures"]
    return out


# Tool caches change on every test run without any code change. Without this list an
# identical rerun was never recognised where they are not gitignored (e.g. a CI runner that
# writes __pycache__ next to the tests).
CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis",
                        ".tox", ".nox", ".cache", ".parcel-cache", ".turbo", ".vitest", ".nyc_output",
                        "htmlcov", "node_modules"})
CACHE_SUFFIXES = (".pyc", ".pyo", ".tsbuildinfo")


def is_cache_path(relative):
    parts = relative.split("/")
    name = parts[-1]
    return (any(part in CACHE_DIRS for part in parts[:-1]) or name.endswith(CACHE_SUFFIXES)
            or name == ".coverage" or name.startswith(".coverage.") or name == ".DS_Store")


def fingerprint(root, exclude=None):
    """Hash of tracked changes plus untracked non-cache files; None outside git (no redundancy check)."""
    try:
        diff = subprocess.run(["git", "-C", str(root), "diff", "HEAD", "--binary"],
                              capture_output=True, timeout=30)
        if diff.returncode != 0:
            return None
        other = subprocess.run(["git", "-C", str(root), "ls-files", "-o", "--exclude-standard", "-z"],
                               capture_output=True, timeout=30)
        if other.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    h = hashlib.sha256(diff.stdout)
    for raw in sorted(other.stdout.split(b"\0"))[:5000]:
        if not raw:
            continue
        relative = raw.decode("utf-8", "surrogateescape")
        if is_cache_path(relative):
            continue
        path = root / relative
        if exclude is not None and path.resolve().is_relative_to(exclude):
            continue
        h.update(raw + b"\0")
        try:
            if path.is_file() and path.stat().st_size <= 5 * 1024 * 1024:
                h.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            pass
    return h.hexdigest()


def missing_npx_binary(command, cwd):
    """`npx tool` without a local install would hit the network registry; report it as unavailable."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words or words[0] != "npx":
        return None
    args = [w for w in words[1:] if not w.startswith("-")]
    if not args or "--yes" in words or "-y" in words:
        return None
    name = args[0].split("@")[0] if not args[0].startswith("@") else "@" + args[0][1:].split("@")[0]
    for folder in (cwd, *cwd.parents):
        bin_dir = folder / "node_modules" / ".bin"
        if bin_dir.is_dir():
            return None if (bin_dir / name.split("/")[-1]).exists() else name
    return None


def shell_argv(command):
    configured = os.environ.get("CONTEXT_HARNESS_SHELL")
    if configured:
        return [*shlex.split(configured), command]
    shell = "/bin/bash" if Path("/bin/bash").exists() else (shutil.which("sh") or "/bin/sh")
    return [shell, "-c", command]


def _slug(command):
    return re.sub(r"[^A-Za-z0-9]+", "-", command)[:40].strip("-") or "command"


def _read_log(path):
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size <= MAX_LOG_READ:
            data = handle.read()
        else:
            head = handle.read(MAX_LOG_READ // 2)
            handle.seek(size - MAX_LOG_READ // 2)
            data = head + b"\n[... middle of log omitted from digest; full log on disk ...]\n" + handle.read()
    return data.decode("utf-8", "replace")


class Runner:
    def __init__(self, store):
        self.store = store

    def resolve(self, tid=None, agent=None):
        with self.store.connect() as con:
            if not tid:
                row = con.execute("SELECT value FROM meta WHERE key='current_task'").fetchone()
                if not row:
                    raise ValueError("No task given and no current task; run start first")
                tid = row[0]
            if not agent:
                row = con.execute("SELECT value FROM meta WHERE key='current_agent'").fetchone()
                agent = row[0] if row else "solver"
            if not con.execute("SELECT 1 FROM assignments WHERE task_id=? AND agent=?", (tid, agent)).fetchone():
                raise ValueError(f"No assignment for task {tid} and agent {agent}")
        return tid, agent

    def budget(self, tid=None, agent=None):
        tid, agent = self.resolve(tid, agent)
        with self.store.connect() as con:
            return {"task": tid, "agent": agent, **budget_state(con, tid, agent)}

    def _refusal(self, con, tid, agent, kind, command, cwd, fp, override):
        state = budget_state(con, tid, agent)
        missing = missing_npx_binary(command, cwd)
        reasons = []
        previous = None
        if missing:
            reasons.append(f"`{missing}` is not installed locally; npx would reach the network. "
                           "Record it as an unavailable check instead of installing it.")
        if fp is not None:
            last = con.execute("""SELECT * FROM runs WHERE task_id=? AND agent=? AND status='executed'
                AND command=? AND cwd=? ORDER BY id DESC LIMIT 1""", (tid, agent, command, str(cwd))).fetchone()
            if last and last["fingerprint"] == fp:
                previous = {"run": last["id"], "exit_code": last["exit_code"],
                            **json.loads(last["digest"] or "{}")}
                reasons.append(f"Nothing changed since identical run {last['id']} (exit {last['exit_code']}); "
                               "its result is attached.")
        if kind in COUNTED:
            if state["runs_left"] <= 0:
                reasons.append("Test/check run budget is exhausted. Report results and remaining risk.")
            if state["green"] and state["after_green_left"] is not None and state["after_green_left"] <= 0:
                reasons.append("Stop rule: the full check is already green and the confirmation run is used. "
                               "Report new edge cases as residual risk instead of continuing.")
            limit = state["limits"]["max_fail_streak"]
            stuck = [n for n, v in state["repeat_failures"].items() if v >= limit]
            if stuck:
                reasons.append(f"{stuck[0]!r} failed {limit} times. Step back: re-read the criterion and the "
                               "original test, consider reverting, or report it as unresolved.")
        if not reasons:
            return None, state, False
        if override:
            if state["overrides_left"] > 0:
                return None, state, True
            reasons.append("Override budget is exhausted.")
        return {"refused": True, "reasons": reasons, "previous": previous, "budget": compact_budget(state),
                "next": "Report with `report` (status completed/blocked/failed and remaining risk), "
                        "or pass an override stating a new hypothesis if one remains."}, state, False

    def run(self, command, *, kind="test", tid=None, agent=None, cwd=None, timeout=900,
            override=None, passthrough_bytes=None, progress=None, heartbeat_s=None):
        """`progress(event)` is called at start and every `heartbeat_s` seconds while the command runs.

        Agent shells (e.g. Codex) return control after a short output window; a silent long check
        made the agent poll the process and the log with extra model calls. A visible heartbeat
        says: still running, result follows here, do not poll or re-run.
        """
        if kind not in KINDS:
            raise ValueError("Run kind must be test, full or other")
        if not isinstance(command, str) or not command.strip() or len(command) > 4000:
            raise ValueError("Command must be nonempty text of at most 4000 characters")
        if override is not None and not override.strip():
            raise ValueError("Override needs a reason (the new hypothesis)")
        if os.name == "nt":
            raise ValueError("`run` is not supported on Windows (it needs POSIX process groups); use WSL")
        tid, agent = self.resolve(tid, agent)
        project = self.store.project
        workdir = (project / (cwd or ".")).resolve()
        if not workdir.is_relative_to(project) or not workdir.is_dir():
            raise ValueError("Run directory must be an existing folder inside the project")
        fp = fingerprint(project, exclude=self.store.root)
        with self.store.connect() as con:
            ensure_schema(con)
            refusal, state, overridden = self._refusal(con, tid, agent, kind, command, workdir, fp, override)
            override = override if overridden else None
            if refusal:
                con.execute("""INSERT INTO runs(task_id,agent,kind,command,cwd,status,fingerprint,reason,override,created)
                    VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
                            (tid, agent, kind, command, str(workdir), "refused", fp,
                             " | ".join(refusal["reasons"]), None))
                self.store.emit(con, "check_refused", {"agent": agent, "kind": kind, "command": command,
                                                      "reasons": refusal["reasons"]}, tid)
                return refusal
            number = con.execute("SELECT COUNT(*) FROM runs WHERE task_id=?", (tid,)).fetchone()[0] + 1
        log_dir = self.store.root / "logs" / tid
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"{number:03d}-{_slug(command)}.log"
        started = time.monotonic()
        timed_out = False
        if heartbeat_s is None:
            heartbeat_s = float(os.environ.get("CONTEXT_HARNESS_HEARTBEAT", "15"))
        with log.open("wb") as handle:
            proc = subprocess.Popen(shell_argv(command), cwd=workdir, stdout=handle, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            if progress:
                progress({"stage": "started", "run": number, "kind": kind, "elapsed_s": 0, "log": str(log),
                          "message": f"running {kind} check; the result digest is printed here when it "
                                     "finishes. Do not poll the process or re-run it."})
            code = None
            while code is None:
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    break
                wait = min(timeout - elapsed, heartbeat_s if progress and heartbeat_s > 0 else timeout)
                try:
                    code = proc.wait(timeout=max(0.05, wait))
                except subprocess.TimeoutExpired:
                    if progress and time.monotonic() - started < timeout:
                        progress({"stage": "running", "run": number, "kind": kind,
                                  "elapsed_s": round(time.monotonic() - started),
                                  "log_bytes": log.stat().st_size,
                                  "message": "still running; wait for the digest, do not poll or re-run."})
            if code is None:
                timed_out = True
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(proc.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        proc.wait(timeout=5)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                code = 124
        duration = round((time.monotonic() - started) * 1000)
        kwargs = {"exit_code": code}
        if passthrough_bytes is not None:
            kwargs["passthrough_bytes"] = passthrough_bytes
        text = _read_log(log)
        view = digest(text, **kwargs)
        names = view.get("failed_tests") or (failed_names(strip_ansi(text).splitlines()) if code != 0 else [])
        if timed_out:
            view["timed_out"] = True
        stored = {k: view[k] for k in ("counts", "failed_tests", "first_failure", "tail", "output") if k in view}
        with self.store.connect() as con:
            ensure_schema(con)
            cur = con.execute("""INSERT INTO runs(task_id,agent,kind,command,cwd,status,exit_code,duration_ms,log,
                fingerprint,failed_tests,digest,override,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                              (tid, agent, kind, command, str(workdir), "executed", code, duration, str(log), fp,
                               json.dumps(names, ensure_ascii=False),
                               json.dumps(stored, ensure_ascii=False), override))
            run_id = cur.lastrowid
            self.store.emit(con, "check_run", {"agent": agent, "kind": kind, "command": command,
                                              "exit_code": code, "duration_ms": duration,
                                              "failed_tests": names[:5],
                                              "override": override}, tid)
            after = budget_state(con, tid, agent)
        result = {"run": run_id, "kind": kind, **view, "duration_ms": duration, "budget": compact_budget(after)}
        if view.get("shaped") or code != 0:
            result["log"] = str(log)
        if not view.get("shaped"):
            result.pop("output_bytes", None)
            result.pop("output_lines", None)
            result.pop("shaped", None)
        notices = []
        if kind == "full" and code == 0 and after["green_run"] == run_id:
            notices.append("Full check is green. If every acceptance criterion has a passing test, stop editing "
                           "and report; at most %d more test/check run(s)." % after["limits"]["max_after_green"])
        limit = after["limits"]["max_fail_streak"]
        for name, count in after["repeat_failures"].items():
            if count == limit - 1:
                notices.append(f"{name!r} failed {count} times; the next failure blocks further runs.")
        if after["runs_left"] <= 2 and kind in COUNTED:
            notices.append(f"{after['runs_left']} test/check run(s) left.")
        if notices:
            result["notice"] = " ".join(notices)
        if view.get("shaped"):
            result["read_more"] = "Full log is on disk: grep or read line ranges of `log` instead of re-running."
        return result
