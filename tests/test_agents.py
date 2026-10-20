"""Agent roadmap: tree from start/assign and imports, branches, models, and imported token usage."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from context_harness.agents import (detect_format, import_codex, import_transcript, parse_claude_transcript,
                                    record_manual_usage, roadmap)
from context_harness.cli import main
from context_harness.session import start
from context_harness.store import Store


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def claude_turn(msg_id, ts, inp, read, write, out, tools=(), model="claude-test", branch="feat/x", **extra):
    content = [{"type": "tool_use", "id": tid, "name": name, "input": {}} for tid, name in tools]
    message = {"id": msg_id, "model": model, "content": content or [{"type": "text", "text": "ok"}],
               "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                         "cache_creation_input_tokens": write, "output_tokens": out,
                         "output_tokens_details": {"thinking_tokens": 1}}}
    return {"type": "assistant", "timestamp": ts, "sessionId": "sess-1", "gitBranch": branch,
            "cwd": "/repo", "message": message, **extra}


def make_claude_session(root):
    main_path = root / "sess-1.jsonl"
    write_jsonl(main_path, [
        {"type": "user", "timestamp": "2026-10-20T10:00:00Z", "sessionId": "sess-1", "gitBranch": "feat/x",
         "cwd": "/repo", "message": {"role": "user", "content": "Fix the paged list bug"}},
        claude_turn("m1", "2026-10-20T10:00:05Z", 2, 1000, 500, 40, tools=[("toolu_a", "Agent")]),
        # the same message streamed as a second block: must not be counted twice
        claude_turn("m1", "2026-10-20T10:00:05Z", 2, 1000, 500, 40),
        claude_turn("m2", "2026-10-20T10:05:00Z", 3, 2000, 0, 60, tools=[("toolu_b", "Bash")]),
    ])
    subs = root / "sess-1" / "subagents"
    write_jsonl(subs / "agent-aaaaaaa111.jsonl", [
        {"type": "user", "timestamp": "2026-10-20T10:00:06Z", "isSidechain": True, "agentId": "aaaaaaa111",
         "sessionId": "sess-1", "gitBranch": "feat/x", "message": {"role": "user", "content": "Find the cause"}},
        claude_turn("s1", "2026-10-20T10:01:00Z", 1, 300, 100, 20, tools=[("toolu_c", "Task")],
                    isSidechain=True, agentId="aaaaaaa111"),
    ])
    (subs / "agent-aaaaaaa111.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "Find where items are lost", "toolUseId": "toolu_a",
         "spawnDepth": 1}), encoding="utf-8")
    write_jsonl(subs / "agent-bbbbbbb222.jsonl", [
        claude_turn("n1", "2026-10-20T10:02:00Z", 1, 50, 10, 5, branch="fix/nested",
                    isSidechain=True, agentId="bbbbbbb222"),
    ])
    (subs / "agent-bbbbbbb222.meta.json").write_text(json.dumps(
        {"agentType": "general-purpose", "description": "Write the regression test", "toolUseId": "toolu_c",
         "spawnDepth": 2}), encoding="utf-8")
    return main_path


class Roadmap(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "feature/roadmap"], cwd=self.project, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "init"], cwd=self.project, check=True)
        self.store = Store(self.root / "state")
        self.store.init(self.project)

    def nodes(self, tid):
        return {n["agent"]: n for n in roadmap(self.store, tid)["nodes"]}

    def test_start_makes_the_solver_the_orchestrator_on_the_current_branch(self):
        tid = start(self.store, title="T", goal="Fix paging", acceptance=["A"])["task"]
        node = self.nodes(tid)["solver"]
        self.assertEqual(node["kind"], "orchestrator")
        self.assertIsNone(node["parent"])
        self.assertEqual(node["branch"], "feature/roadmap")
        self.assertEqual(node["role"], "Fix paging")

    def test_assign_hangs_specialists_under_the_coordinator(self):
        tid = self.store.task_create("T", "G", ["A"])["id"]
        self.store.assign(tid, "backend", "api", "Fix the API", ["api"], "api", backend="lexical",
                          branch="wt/backend", agent_model="gpt-x")
        self.store.assign(tid, "tests", "api", "Write tests", ["tests"], "tests", backend="lexical",
                          parent="backend", role="Regression tests for the API fix")
        nodes = self.nodes(tid)
        self.assertEqual(nodes["coordinator"]["kind"], "orchestrator")
        self.assertEqual(nodes["backend"]["parent"], "coordinator")
        self.assertEqual(nodes["backend"]["branch"], "wt/backend")
        self.assertEqual(nodes["backend"]["model"], "gpt-x")
        self.assertEqual(nodes["tests"]["parent"], "backend")
        self.assertEqual(nodes["tests"]["role"], "Regression tests for the API fix")
        assigned = [e["data"] for e in self.store.events(tid) if e["type"] == "agent_assigned"]
        self.assertEqual(assigned[0]["parent"], "coordinator")

    def test_claude_transcript_usage_is_deduplicated_and_normalised(self):
        info = parse_claude_transcript(make_claude_session(self.root))
        self.assertEqual(info["usage"]["requests"], 2)
        self.assertEqual(info["usage"]["input_tokens"], (2 + 1000 + 500) + (3 + 2000))
        self.assertEqual(info["usage"]["cached_input_tokens"], 3000)
        self.assertEqual(info["usage"]["cache_write_tokens"], 500)
        self.assertEqual(info["usage"]["output_tokens"], 100)
        self.assertEqual(info["prompt"], "Fix the paged list bug")
        self.assertEqual(info["tool_calls"], 2)
        self.assertEqual(info["spawned"], ["toolu_a"])

    def test_claude_session_import_builds_the_bot_tree_with_tokens(self):
        tid = start(self.store, title="T", goal="Fix paging", acceptance=["A"])["task"]
        transcript = make_claude_session(self.root)
        result = import_transcript(self.store, tid, transcript, "solver")
        self.assertEqual(result["runtime"], "claude")
        nodes = self.nodes(tid)
        explore = nodes["explore-aaaaaaa"]
        nested = nodes["general-purpose-bbbbbbb"]
        self.assertEqual(nodes["solver"]["kind"], "orchestrator", "start's node keeps its kind")
        self.assertEqual(explore["parent"], "solver")
        self.assertEqual(explore["role"], "Find where items are lost")
        self.assertEqual(explore["agent_type"], "Explore")
        self.assertEqual(explore["kind"], "subagent")
        self.assertEqual(nested["parent"], "explore-aaaaaaa", "spawned by the Explore agent's Task call")
        self.assertEqual(nested["branch"], "fix/nested")
        self.assertEqual(explore["usage"]["input_tokens"], 401)
        self.assertEqual(nodes["solver"]["usage"]["output_tokens"], 100)
        totals = roadmap(self.store, tid)["totals"]
        import_transcript(self.store, tid, transcript, "solver")
        self.assertEqual(roadmap(self.store, tid)["totals"], totals, "re-import replaces, never doubles")
        self.assertIn("agents_imported", [e["type"] for e in self.store.events(tid)])

    def test_codex_rollout_and_exec_output(self):
        tid = self.store.task_create("T", "G", ["A"])["id"]
        rollout = self.root / "rollout.jsonl"
        write_jsonl(rollout, [
            {"timestamp": "2026-10-20T09:00:00Z", "type": "session_meta",
             "payload": {"id": "cx-1", "cwd": "/repo", "git": {"branch": "codex/fix"}}},
            {"timestamp": "2026-10-20T09:00:01Z", "type": "turn_context", "payload": {"model": "gpt-6"}},
            {"timestamp": "2026-10-20T09:00:02Z", "type": "event_msg", "payload": {"type": "user_message",
                                                                                 "message": "Fix it"}},
            {"timestamp": "2026-10-20T09:01:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 9,
                                      "reasoning_output_tokens": 4}}}},
            {"timestamp": "2026-10-20T09:02:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 20,
                                      "reasoning_output_tokens": 8}}}},
        ])
        self.assertEqual(detect_format(rollout), "codex")
        import_codex(self.store, tid, rollout, "codex-solver", parent="coordinator")
        node = self.nodes(tid)["codex-solver"]
        self.assertEqual((node["model"], node["branch"], node["role"]), ("gpt-6", "codex/fix", "Fix it"))
        self.assertEqual(node["usage"]["input_tokens"], 300, "cumulative total, not a sum of snapshots")
        self.assertEqual(node["usage"]["reasoning_tokens"], 8)
        exec_out = self.root / "exec.jsonl"
        write_jsonl(exec_out, [{"type": "thread.started", "thread_id": "th-1"},
                               {"type": "item.completed", "item": {"type": "command_execution"}},
                               {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 40,
                                                                    "output_tokens": 5}}])
        import_codex(self.store, tid, exec_out, "codex-exec")
        self.assertEqual(self.nodes(tid)["codex-exec"]["usage"]["input_tokens"], 50)
        self.assertEqual(self.nodes(tid)["codex-exec"]["tool_calls"], 1)

    def test_manual_usage_and_validation(self):
        tid = self.store.task_create("T", "G", ["A"])["id"]
        record_manual_usage(self.store, tid, "reviewer", {"input_tokens": 10, "cached_input_tokens": 4,
                                                          "output_tokens": 2})
        self.assertEqual(self.nodes(tid)["reviewer"]["usage"]["input_tokens"], 10)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            record_manual_usage(self.store, tid, "reviewer", {"input_tokens": 1, "cached_input_tokens": 5})
        with self.assertRaises(ValueError):
            import_transcript(self.store, tid, self.root / "missing.jsonl", "x")

    def test_cli_import_and_agents(self):
        def call(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = main(["--state", str(self.store.root), *args])
            return code, json.loads(out.getvalue()) if out.getvalue() else None
        tid = start(self.store, title="T", goal="G", acceptance=["A"])["task"]
        code, result = call("import-session", "--transcript", str(make_claude_session(self.root)))
        self.assertEqual(code, 0)
        self.assertEqual(result["task"], tid)
        self.assertIn("solver", result["agents"])
        code, result = call("usage", tid, "--agent", "solver", "--input", "5", "--output", "1", "--source", "extra")
        self.assertEqual(code, 0)
        code, result = call("agents")
        self.assertEqual(code, 0)
        self.assertEqual(len(result["nodes"]), 3)
        self.assertGreater(result["totals"]["input_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
