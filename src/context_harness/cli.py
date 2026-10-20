"""Portable CLI; stdout is JSON, failures return a nonzero exit code."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from .home import resolve_state
from .store import Store


REFUSED_EXIT = 75


def parser():
    p = argparse.ArgumentParser(description="Budgeted project work, frozen tests and project memory for coding agents")
    p.add_argument("--state", type=Path, help="State directory (default: derived from --project or the cwd's git root)")
    p.add_argument("--project", type=Path, help="Project root; selects the default state directory")
    p.add_argument("--compact", action="store_true", help="Emit compact JSON without changing data")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("init")
    q.add_argument("--project", dest="init_project")
    q = sub.add_parser("start", help="Create a task, snapshot tests, retrieve context and set budgets in one call")
    q.add_argument("--title", required=True)
    q.add_argument("--goal", required=True)
    q.add_argument("--acceptance", action="append", required=True)
    q.add_argument("--query", help="Symptom in English plus known file/identifier names (default: title + goal)")
    q.add_argument("--domain")
    q.add_argument("--agent", default="solver")
    q.add_argument("--allow-path", action="append")
    q.add_argument("--backend", choices=("auto", "lexical", "hybrid", "decisions", "responses"), default="auto")
    q.add_argument("--variant", action="append", default=[])
    q.add_argument("--view", choices=("pointers", "inline", "index", "full"), default="pointers")
    q.add_argument("--budget-bytes", type=int, default=8000)
    budget_flags(q)
    q.add_argument("--no-test-snapshot", action="store_true")
    q = sub.add_parser("run", help="Run a test/check with budgets; full log on disk, short digest inline")
    q.add_argument("--full", action="store_true", help="This is the complete project check")
    q.add_argument("--kind", choices=("test", "full", "other"))
    q.add_argument("--task")
    q.add_argument("--agent")
    q.add_argument("--cwd", help="Directory inside the project")
    q.add_argument("--timeout", type=int, default=900)
    q.add_argument("--override", help="Run despite a refusal; state the new hypothesis (limited, recorded)")
    q.add_argument("cmd", nargs=argparse.REMAINDER)
    q = sub.add_parser("budget", help="Show the run budget countdown")
    q.add_argument("--task")
    q.add_argument("--agent")
    q = sub.add_parser("tests-snapshot", help="Snapshot pre-existing test files (done by start/assign)")
    q.add_argument("--task")
    q.add_argument("--pattern", action="append")
    q.add_argument("--replace", action="store_true")
    q = sub.add_parser("guard", help="Show rewritten/deleted pre-existing tests; --run checks originals")
    q.add_argument("--task")
    q.add_argument("--agent")
    q.add_argument("--run", dest="run_template", help="Test command with {files}, e.g. 'npx vitest run {files}'")
    q.add_argument("--cwd")
    q.add_argument("--timeout", type=int, default=900)
    q = sub.add_parser("lesson-candidates", help="Post-session lesson drafts from recorded runs and guard")
    q.add_argument("task")
    q.add_argument("--domain", default="general")
    q.add_argument("--apply", action="store_true", help="Store drafts as proposed (unverified) memories")
    q = sub.add_parser("mcp", help="Serve the harness as an MCP stdio server")
    q.add_argument("--read-only", action="store_true", help="Expose no tool that runs commands or writes")
    q = sub.add_parser("install", help="Install the skill and/or register the MCP server for agents")
    q.add_argument("--skill", action="append", default=[],
                   help="claude-code | codex | claude-project | DIR (repeatable)")
    q.add_argument("--mcp", action="append", default=[],
                   help="claude-code | claude-project | codex | cursor | claude-desktop | gemini | json")
    q.add_argument("--python", help="Interpreter for the MCP server (default: this one)")
    q.add_argument("--target-project", type=Path, help="Project for claude-project targets")
    q.add_argument("--write", action="store_true", help="Apply changes (default: dry run)")
    q.add_argument("--force", action="store_true", help="Replace an existing install (backup kept)")
    q = sub.add_parser("memory-add")
    for name in ("title", "content", "domain", "source"):
        q.add_argument("--" + name, required=True)
    q.add_argument("--status", choices=("proposed", "verified"), default="proposed")
    q.add_argument("--evidence", default="")
    q = sub.add_parser("memory-verify")
    q.add_argument("id")
    q.add_argument("--evidence", required=True)
    q = sub.add_parser("search")
    q.add_argument("query")
    q.add_argument("--domain")
    q.add_argument("--limit", type=int, default=5)
    q.add_argument("--task")
    q.add_argument("--agent")
    search_flags(q)
    q.add_argument("--view", choices=("pointers", "inline", "index", "full"), default="pointers")
    q.add_argument("--budget-bytes", type=int, default=6000)
    q.add_argument("--offset", type=int, default=0)
    q = sub.add_parser("memory-read", help="Read explicitly selected verified, fresh memories")
    q.add_argument("ids", nargs="+")
    q.add_argument("--domain")
    q.add_argument("--budget-bytes", type=int, default=8000)
    q.add_argument("--offset", type=int, default=0)
    q = sub.add_parser("semantic-build", help="Build local embeddings; never downloads a model")
    q.add_argument("--model-dir", type=Path, required=True)
    q = sub.add_parser("task-create")
    q.add_argument("--title", required=True)
    q.add_argument("--goal", required=True)
    q.add_argument("--acceptance", action="append", required=True)
    q = sub.add_parser("assign")
    q.add_argument("task")
    for name in ("agent", "domain", "goal", "query"):
        q.add_argument("--" + name, required=True)
    q.add_argument("--allow-path", action="append", default=[])
    q.add_argument("--context-view", choices=("pointers", "inline", "index", "full"), default="pointers")
    q.add_argument("--budget-bytes", type=int, default=12000)
    budget_flags(q)
    q.add_argument("--no-test-snapshot", action="store_true")
    q.add_argument("--parent", default="coordinator", help="Agent that created this one (roadmap tree)")
    q.add_argument("--role", help="What this agent does (default: --goal)")
    q.add_argument("--branch", help="Git branch or worktree it works on (default: current branch)")
    q.add_argument("--agent-model", help="Model the agent runs on, for the roadmap")
    search_flags(q)
    q = sub.add_parser("usage", help="Record token usage for an agent by hand")
    q.add_argument("task")
    q.add_argument("--agent", required=True)
    q.add_argument("--input", type=int, required=True, help="All input tokens, cached ones included")
    q.add_argument("--output", type=int, required=True, help="All output tokens, reasoning included")
    q.add_argument("--cached", type=int, default=0)
    q.add_argument("--cache-write", type=int, default=0)
    q.add_argument("--reasoning", type=int, default=0)
    q.add_argument("--requests", type=int, default=0)
    q.add_argument("--model")
    q.add_argument("--source", default="manual")
    q = sub.add_parser("import-session", help="Import agents and token usage from a Claude Code or Codex log")
    q.add_argument("task", nargs="?")
    q.add_argument("--transcript", type=Path, required=True,
                   help="Claude Code session .jsonl (subagents are found next to it) or Codex rollout/exec JSONL")
    q.add_argument("--agent", help="Node name for the main agent (default: the task's current agent)")
    q.add_argument("--parent", help="Codex only: agent that started this run")
    q.add_argument("--role", help="Codex only: what this run did")
    q = sub.add_parser("agents", help="Agent roadmap of a task: tree, branches, models and tokens")
    q.add_argument("task", nargs="?")
    q = sub.add_parser("checkpoint", help="Resume from current task/checks, without event replay")
    q.add_argument("task")
    q.add_argument("--budget-bytes", type=int, default=12000)
    q = sub.add_parser("refine-plan", help="Record a bounded proposal/probe plan, without launching a model")
    q.add_argument("task")
    q.add_argument("--max-probes", type=int, default=1)
    q.add_argument("--token-budget", type=int, default=20000)
    q = sub.add_parser("lesson-propose", help="Propose a sourced lesson after independent acceptance")
    q.add_argument("task")
    for name in ("title", "content", "domain", "source"):
        q.add_argument("--" + name, required=True)
    q = sub.add_parser("output-ingest", help="Snapshot an explicit UTF-8 log; no command is executed")
    q.add_argument("path", type=Path)
    q.add_argument("--label", default="")
    q = sub.add_parser("output-search")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=5)
    q.add_argument("--budget-bytes", type=int, default=4000)
    q = sub.add_parser("output-read")
    q.add_argument("id")
    q.add_argument("--start-line", type=int, default=1)
    q.add_argument("--max-lines", type=int, default=40)
    q.add_argument("--budget-bytes", type=int, default=6000)
    q = sub.add_parser("report")
    q.add_argument("task", nargs="?")
    q.add_argument("--agent")
    q.add_argument("--status", choices=("completed", "blocked", "failed"), required=True)
    q.add_argument("--summary", required=True)
    q.add_argument("--artifact", action="append", default=[])
    q.add_argument("--evidence", action="append", default=[])
    q.add_argument("--test-change", action="append", default=[],
                   help="'PATH: why' for each pre-existing test you rewrote or deleted")
    q = sub.add_parser("progress")
    q.add_argument("task")
    q.add_argument("--agent", required=True)
    q.add_argument("--stage", choices=("running", "reviewing", "blocked"), required=True)
    q.add_argument("--summary", required=True)
    q = sub.add_parser("verify")
    q.add_argument("task")
    q.add_argument("--summary", required=True)
    q.add_argument("--evidence", action="append", required=True)
    group = q.add_mutually_exclusive_group(required=True)
    group.add_argument("--passed", action="store_true")
    group.add_argument("--failed", action="store_true")
    q = sub.add_parser("summary")
    q.add_argument("task")
    q = sub.add_parser("task-close")
    q.add_argument("task")
    q.add_argument("--reason", required=True)
    q = sub.add_parser("events")
    q.add_argument("--task")
    q = sub.add_parser("export-report")
    q.add_argument("task")
    q.add_argument("--output", type=Path, required=True)
    q = sub.add_parser("export-dashboard")
    q.add_argument("task")
    q.add_argument("--output", type=Path, required=True)
    q = sub.add_parser("dashboard")
    q.add_argument("task")
    q.add_argument("--port", type=int, default=8765)
    return p


def budget_flags(p):
    p.add_argument("--max-runs", type=int, help="Test/check runs allowed (default 12)")
    p.add_argument("--max-after-green", type=int, help="Runs allowed after the full check is green (default 1)")
    p.add_argument("--max-fail-streak", type=int, help="Failures of one test before runs stop (default 3)")
    p.add_argument("--max-overrides", type=int, help="Recorded overrides allowed (default 2)")


def budget_args(a):
    return {"max_runs": a.max_runs, "max_after_green": a.max_after_green,
            "max_fail_streak": a.max_fail_streak, "max_overrides": a.max_overrides}


def print_progress(event):
    """Heartbeat on stderr; stdout stays one JSON document."""
    if event["stage"] == "started":
        line = f"[context-harness] run {event['run']}: {event['message']} Log: {event['log']}"
    else:
        line = f"[context-harness] run {event['run']}: {event['elapsed_s']}s, {event['message']}"
    print(line, file=sys.stderr, flush=True)


def run_command_text(parts):
    parts = list(parts)
    if parts and parts[0] == "--":
        parts = parts[1:]
    if not parts:
        raise ValueError("Give the command after --, e.g. run -- npm test")
    return parts[0] if len(parts) == 1 else shlex.join(parts)


def search_flags(p):
    p.add_argument("--backend", choices=("auto", "lexical", "hybrid", "decisions", "responses"), default="auto")
    p.add_argument("--model", help="Explicit override; no automatic model substitution")
    p.add_argument("--retrieval", choices=("lexical", "hybrid"), default="lexical",
                   help="Candidate retrieval before optional API ranking; hybrid backend implies hybrid")
    p.add_argument("--variant", action="append", default=[],
                   help="Explicit translated/paraphrased query, up to two; hybrid retrieval required")


def report_markdown(data):
    task = data["task"]
    lines = ["# " + task["title"], "", task["goal"], "", "## Acceptance criteria", ""]
    lines += ["- " + x for x in task["acceptance"]]
    lines += ["", "## Agent reports", ""]
    for a in data["agents"]:
        lines += [f"### {a['agent']} — {a['status']}", ""]
        if a["report"]:
            r = a["report"]
            lines += [r["summary"], ""]
            lines += ["- Artifact: " + x for x in r["artifacts"]]
            lines += ["- Evidence: " + x for x in r["evidence"]]
            lines += [""]
    lines += ["## Coordinator verification", "", f"Verified: {data['verified']}", ""]
    if data["verification"]:
        v = data["verification"]
        lines += [v["summary"], "", f"Stale: {v['stale']}", ""]
        lines += ["- " + x for x in v["evidence"]]
    else:
        lines += ["No independent coordinator verification has been recorded."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    a = parser().parse_args(argv)
    exit_code = 0
    try:
        if a.command == "mcp":
            from .mcp_server import serve
            return serve(state=a.state, project=a.project, read_only=a.read_only)
        if a.command == "install":
            from .install import install
            result = install(skills=a.skill, mcps=a.mcp, python=a.python, project=a.target_project,
                             write=a.write, force=a.force)
            print(json.dumps(result, ensure_ascii=False, indent=None if a.compact else 2))
            return 0
        project = a.project or (getattr(a, "init_project", None) and Path(a.init_project))
        state, derived_project = resolve_state(a.state, project)
        s = Store(state)
        if a.command in ("init", "start") and not s.db.exists():
            if not derived_project:
                raise ValueError("Give --project to initialize this state")
            s.init(derived_project)
    except Exception as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        if a.command == "init":
            result = s.init(derived_project or project)
        elif a.command == "start":
            from .session import start
            result = start(s, title=a.title, goal=a.goal, acceptance=a.acceptance, query=a.query,
                           domain=a.domain, agent=a.agent, allow_paths=a.allow_path or ["."],
                           backend=a.backend, variants=a.variant, view=a.view, budget_bytes=a.budget_bytes,
                           budget=budget_args(a), snapshot_tests=not a.no_test_snapshot)
        elif a.command == "run":
            from .runner import Runner
            kind = a.kind or ("full" if a.full else "test")
            result = Runner(s).run(run_command_text(a.cmd), kind=kind, tid=a.task, agent=a.agent, cwd=a.cwd,
                                   timeout=a.timeout, override=a.override, progress=print_progress)
            exit_code = REFUSED_EXIT if result.get("refused") else min(255, max(0, result["exit_code"]))
        elif a.command == "budget":
            from .runner import Runner
            result = Runner(s).budget(a.task, a.agent)
        elif a.command == "tests-snapshot":
            from .runner import Runner
            from .testguard import TestGuard
            tid, _ = Runner(s).resolve(a.task, None) if not a.task else (a.task, None)
            result = TestGuard(s, tid).snapshot(a.pattern, a.replace)
        elif a.command == "guard":
            from .runner import Runner
            from .testguard import TestGuard
            runner = Runner(s)
            tid, agent = runner.resolve(a.task, a.agent)
            guard = TestGuard(s, tid)
            if a.run_template:
                result = guard.run_original(a.run_template, runner, agent=agent, cwd=a.cwd, timeout=a.timeout)
            else:
                result = guard.check()
        elif a.command == "lesson-candidates":
            from .session import lesson_candidates
            result = lesson_candidates(s, a.task, apply=a.apply, domain=a.domain)
        elif a.command == "memory-add":
            result = s.memory_add(a.title, a.content, a.domain, a.source, a.status, a.evidence)
        elif a.command == "memory-verify":
            result = s.memory_verify(a.id, a.evidence)
        elif a.command == "search":
            result = s.search(a.query, a.domain, a.backend, a.model, a.limit,
                              task_id=a.task, agent=a.agent, retrieval=a.retrieval, query_variants=a.variant)
            from .context import search_view
            result = search_view(result, view=a.view, budget_bytes=a.budget_bytes, offset=a.offset,
                                 project=s.project)
        elif a.command == "memory-read":
            result = s.memory_read(a.ids, a.domain, a.budget_bytes, a.offset)
        elif a.command == "semantic-build":
            from .semantic import SemanticIndex
            model_dir = a.model_dir.expanduser().resolve(strict=True)
            result = SemanticIndex(s, model_dir=model_dir).build()
            (s.root / "semantic-model.json").write_text(
                json.dumps({"model_dir": str(model_dir)}, indent=2), encoding="utf-8")
        elif a.command == "task-create":
            result = s.task_create(a.title, a.goal, a.acceptance)
        elif a.command == "assign":
            result = s.assign(a.task, a.agent, a.domain, a.goal, a.allow_path, a.query,
                              a.backend, a.model, a.retrieval, a.variant, a.context_view, a.budget_bytes,
                              budget=budget_args(a), snapshot_tests=not a.no_test_snapshot,
                              parent=a.parent or None, role=a.role, branch=a.branch, agent_model=a.agent_model)
        elif a.command == "usage":
            from .agents import record_manual_usage
            result = record_manual_usage(s, a.task, a.agent, {
                "input_tokens": a.input, "cached_input_tokens": a.cached, "cache_write_tokens": a.cache_write,
                "output_tokens": a.output, "reasoning_tokens": a.reasoning, "requests": a.requests},
                source=a.source, model=a.model)
        elif a.command in ("import-session", "agents"):
            from .agents import import_transcript, roadmap
            with s.connect() as con:
                meta = {row[0]: row[1] for row in con.execute(
                    "SELECT key, value FROM meta WHERE key IN ('current_task','current_agent')")}
            tid = a.task or meta.get("current_task")
            if not tid:
                raise ValueError("Give the task id; there is no current task")
            agent = getattr(a, "agent", None) or (meta.get("current_agent") if tid == meta.get("current_task") else None) \
                or "orchestrator"
            if a.command == "agents":
                result = {"task": tid, **roadmap(s, tid)}
            else:
                result = import_transcript(s, tid, a.transcript, agent, parent=a.parent, role=a.role)
        elif a.command == "checkpoint":
            result = s.checkpoint(a.task, a.budget_bytes)
        elif a.command == "refine-plan":
            result = s.refine_plan(a.task, a.max_probes, a.token_budget)
        elif a.command == "lesson-propose":
            result = s.lesson_propose(a.task, a.title, a.content, a.domain, a.source)
        elif a.command in ("output-ingest", "output-search", "output-read"):
            from .vault import OutputVault
            vault = OutputVault(s)
            if a.command == "output-ingest":
                result = vault.ingest(a.path, label=a.label)
            elif a.command == "output-search":
                result = vault.search(a.query, limit=a.limit, budget_bytes=a.budget_bytes)
            else:
                result = vault.read(a.id, start_line=a.start_line, max_lines=a.max_lines, budget_bytes=a.budget_bytes)
        elif a.command == "report":
            from .runner import Runner
            tid, agent = Runner(s).resolve(a.task, a.agent)
            result = s.report(tid, agent, a.status, a.summary, a.artifact, a.evidence, a.test_change)
        elif a.command == "progress":
            result = s.progress(a.task, a.agent, a.stage, a.summary)
        elif a.command == "verify":
            result = s.verify(a.task, a.passed, a.summary, a.evidence)
        elif a.command == "summary":
            result = s.summary(a.task)
        elif a.command == "task-close":
            result = s.close(a.task, a.reason)
        elif a.command == "events":
            result = s.events(a.task)
        elif a.command == "export-report":
            data = s.summary(a.task)
            a.output.parent.mkdir(parents=True, exist_ok=True)
            a.output.write_text(report_markdown(data), encoding="utf-8")
            result = {"output": str(a.output.resolve()), "verified": data["verified"]}
        elif a.command == "export-dashboard":
            from .dashboard import export_dashboard
            result = export_dashboard(s, a.task, a.output)
        elif a.command == "dashboard":
            from .dashboard import serve
            serve(s, a.task, a.port)
            return 0
        else:
            raise ValueError("Unknown command")
    except Exception as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2
    if a.compact:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


def mcp_main(argv=None):
    """`context-harness-mcp`: the MCP stdio server without the CLI's subcommand syntax."""
    p = argparse.ArgumentParser(prog="context-harness-mcp", description="context-harness MCP stdio server")
    p.add_argument("--state", type=Path, help="Bind the server to one state directory")
    p.add_argument("--project", type=Path, help="Default project root")
    p.add_argument("--read-only", action="store_true", help="Expose no tool that runs commands or writes")
    a = p.parse_args(argv)
    from .mcp_server import serve
    return serve(state=a.state, project=a.project, read_only=a.read_only)


if __name__ == "__main__":
    raise SystemExit(main())
