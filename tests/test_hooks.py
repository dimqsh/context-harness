"""Plugin hooks: deny direct test runs and block unjustified test rewrites, only while a task is active."""
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from context_harness import hooks
from context_harness.home import default_state
from context_harness.session import start
from context_harness.store import Store

ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = ROOT / "plugins" / "context-harness" / "hooks" / "context_harness_hook.py"


class Detection(unittest.TestCase):
    def test_direct_test_and_check_commands_are_found(self):
        for command in ("npm test", "npm run check", "npx vitest run src/a.test.ts", "pytest -q",
                        "go test ./...", "cargo test", "python -m unittest discover -s tests",
                        "python3 -m pytest tests", "python3.11 -X dev -m unittest", "cd web && npm test",
                        "CI=1 npm test", "timeout 60 pytest", "bash -lc 'npm run check'", "uv run pytest",
                        "npm --prefix web run test:unit", "yarn test", "pnpm run check", "npx --yes vitest",
                        "./node_modules/.bin/vitest run", "cargo +nightly test", "env FOO=1 go test ./pkg",
                        "npm run build; npm test", "(cd api && pytest)", "npm t", "cd web\nnpm test",
                        "if pytest -q; then echo ok; fi", "{ cargo test; }", "npm test 2>&1 | tail -5"):
            with self.subTest(command=command):
                self.assertTrue(hooks.direct_test_commands(command), command)

    def test_harness_runs_and_unrelated_commands_pass(self):
        for command in ("python3 /x/scripts/ch.py run -- npm test",
                        'python3 "/x/skills/context-harness/scripts/ch.py" --project /p --compact run --full -- npm run check',
                        "context-harness run -- pytest", "python -m context_harness run -- go test ./...",
                        "python3 ch.py guard --run 'npx vitest run {files}'",
                        'echo "npm test"', "grep -r pytest setup.cfg", "npm install", "npm run build",
                        'git commit -m "npm test passes"', "cat pytest.ini", "python script.py",
                        'python -c "import pytest"', "go build ./...", "cargo build", "ls tests", "", None,
                        'git commit -m "Fix totals\n\npytest passes now\nnpm test too"',
                        "cat > notes.md <<'EOF'\npytest -q passes\nnpm test\nEOF",
                        'echo "unbalanced\npytest'):
            with self.subTest(command=command):
                self.assertEqual(hooks.direct_test_commands(command), [], command)

    def test_only_the_direct_part_of_a_mixed_line_counts(self):
        found = hooks.direct_test_commands("python3 ch.py run -- npm test && npm test")
        self.assertEqual(found, ["npm test"])

    def test_argv_list_commands_are_supported(self):
        self.assertEqual(hooks.direct_test_commands(["bash", "-lc", "pytest -x"]), ["pytest -x"])


class HookBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        (self.project / "tests").mkdir(parents=True)
        self.test_file = self.project / "tests" / "test_app.py"
        self.test_file.write_text("def test_value():\n    assert value() == 1\n", encoding="utf-8")
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "init"]):
            subprocess.run(cmd, cwd=self.project, check=True, capture_output=True)
        self.state = self.root / "state"
        env = mock.patch.dict(os.environ, {"CONTEXT_HARNESS_STATE": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("CONTEXT_HARNESS_HOOKS", "CONTEXT_HARNESS_HOOK_TTL_HOURS"):
            os.environ.pop(name, None)
        self.store = Store(self.state)
        self.store.init(self.project)

    def start_task(self):
        self.task = start(self.store, title="Fix value", goal="value() returns 1",
                          acceptance=["test_value passes"])["task"]
        return self.task

    def bash(self, command, cwd=None):
        return hooks.pre_tool_use({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                   "tool_input": {"command": command}, "cwd": str(cwd or self.project)})

    def stop(self):
        return hooks.stop({"hook_event_name": "Stop", "stop_hook_active": False, "cwd": str(self.project)})

    def report(self, status="completed", test_changes=()):
        return self.store.report(self.task, "solver", status, "done", [], ["check: ok"], list(test_changes))

    def rewrite_test(self):
        self.test_file.write_text("def test_value():\n    assert value() == 2\n", encoding="utf-8")


class PreToolUse(HookBase):
    def test_no_task_is_a_no_op(self):
        self.assertIsNone(self.bash("npm test"))

    def test_active_task_denies_direct_runs_and_explains_the_harness_call(self):
        self.start_task()
        out = self.bash("cd web && npm run check")["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(self.task, out["permissionDecisionReason"])
        self.assertIn("run [--full]", out["permissionDecisionReason"])
        self.assertIn("-- npm run check", out["permissionDecisionReason"])
        self.assertIsNone(self.bash("python3 ch.py run -- npm run check"))
        self.assertIsNone(self.bash("git status"))
        self.assertIsNotNone(self.bash("pytest", cwd=self.project / "tests"))

    def test_report_close_and_other_projects_end_the_rule(self):
        self.start_task()
        self.assertIsNone(self.bash("pytest", cwd=self.root))
        self.report()
        self.assertIsNone(self.bash("pytest"))
        self.store.close(self.task, "reported")
        self.start_task()
        self.assertIsNotNone(self.bash("pytest"))
        self.store.close(self.task, "abandoned")
        self.assertIsNone(self.bash("pytest"))

    def test_stale_task_does_not_block_later_sessions(self):
        self.start_task()
        later = datetime.now(timezone.utc) + timedelta(hours=hooks.DEFAULT_TTL_HOURS + 1)
        self.assertIsNone(hooks.current_task(self.project, now=later))
        with mock.patch.dict(os.environ, {"CONTEXT_HARNESS_HOOK_TTL_HOURS": "48"}):
            self.assertIsNotNone(hooks.current_task(self.project, now=later))

    def test_disabled_hooks_print_nothing(self):
        self.start_task()
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "pytest"}, "cwd": str(self.project)})
        out = io.StringIO()
        self.assertEqual(hooks.main(["pre-tool-use"], stdin=io.StringIO(payload), stdout=out), 0)
        self.assertIn('"deny"', out.getvalue())
        with mock.patch.dict(os.environ, {"CONTEXT_HARNESS_HOOKS": "off"}):
            out = io.StringIO()
            hooks.main(["pre-tool-use"], stdin=io.StringIO(payload), stdout=out)
            self.assertEqual(out.getvalue(), "")


class Stop(HookBase):
    def test_no_task_or_clean_tests_allow_stopping(self):
        self.assertIsNone(self.stop())
        self.start_task()
        self.assertIsNone(self.stop())
        with self.test_file.open("a", encoding="utf-8") as handle:
            handle.write("\n\ndef test_new():\n    assert True\n")
        self.assertIsNone(self.stop(), "adding tests to an old file is fine")

    def test_rewritten_test_blocks_until_justified(self):
        self.start_task()
        self.rewrite_test()
        out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn("tests/test_app.py", out["reason"])
        self.assertIn("--test-change", out["reason"])
        self.report(status="blocked")
        self.assertEqual(self.stop()["decision"], "block", "a report without a justification is not enough")
        self.report(status="completed", test_changes=["tests/test_app.py: value() now returns 2 by spec"])
        self.assertIsNone(self.stop())

    def test_deleted_test_blocks_and_closed_task_does_not(self):
        self.start_task()
        self.test_file.unlink()
        self.assertEqual(self.stop()["decision"], "block")
        self.store.close(self.task, "abandoned")
        self.assertIsNone(self.stop())

    def test_stop_gives_up_after_a_few_blocks(self):
        self.start_task()
        self.rewrite_test()
        for _ in range(hooks.MAX_STOP_BLOCKS):
            self.assertEqual(self.stop()["decision"], "block")
        out = self.stop()
        self.assertNotIn("decision", out)
        self.assertIn("gave up", out["systemMessage"])

    def test_internal_errors_fail_open(self):
        self.start_task()
        self.rewrite_test()
        out = io.StringIO()
        with mock.patch.object(hooks, "unjustified_test_changes", side_effect=RuntimeError("boom")):
            self.assertEqual(hooks.main(["stop"], stdin=io.StringIO("{}"), stdout=out), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(hooks.main(["stop"], stdin=io.StringIO("not json"), stdout=out), 0)
        self.assertEqual(hooks.main(["unknown-event"], stdin=io.StringIO("{}"), stdout=out), 0)
        self.assertEqual(out.getvalue(), "")


class PluginHookScript(unittest.TestCase):
    """The committed plugin script runs the bundled runtime with the default state location."""

    def run_hook(self, event, payload, env):
        return subprocess.run([sys.executable, str(HOOK_SCRIPT), event], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=60)

    def test_hook_script_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = root / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            env = {k: v for k, v in os.environ.items() if not k.startswith("CONTEXT_HARNESS")}
            env["CONTEXT_HARNESS_HOME"] = str(root / "home")
            payload = {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "cwd": str(project)}
            idle = self.run_hook("pre-tool-use", payload, env)
            self.assertEqual((idle.returncode, idle.stdout), (0, ""))
            with mock.patch.dict(os.environ, {"CONTEXT_HARNESS_HOME": str(root / "home")}):
                store = Store(default_state(project))
                store.init(project)
                start(store, title="t", goal="g", acceptance=["a"])
            denied = self.run_hook("pre-tool-use", payload, env)
            self.assertEqual(denied.returncode, 0, denied.stderr)
            decision = json.loads(denied.stdout)["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertIn("skills/context-harness/scripts/ch.py", decision["permissionDecisionReason"])
            stopped = self.run_hook("stop", {"cwd": str(project)}, env)
            self.assertEqual((stopped.returncode, stopped.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
