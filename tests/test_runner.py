"""v0.5 acceptance: budgets, stop-after-green, repeat-failure stop, refusal semantics."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from context_harness.cli import REFUSED_EXIT, main
from context_harness.runner import Runner
from context_harness.session import start
from context_harness.store import Store

PY = sys.executable


def fail_with(name):
    return f'"{PY}" -c "import sys; print(\'FAILED tests/test_a.py::{name} - boom\'); sys.exit(1)"'


class RunnerBase(unittest.TestCase):
    git = True

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        (self.project / "tests").mkdir(parents=True)
        (self.project / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.project / "tests" / "test_app.py").write_text(
            "import unittest\nfrom app import VALUE\n\nclass T(unittest.TestCase):\n"
            "    def test_value(self):\n        self.assertEqual(VALUE, 1)\n", encoding="utf-8")
        if self.git:
            for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "init"]):
                subprocess.run(cmd, cwd=self.project, check=True, capture_output=True)
        self.store = Store(self.root / "state")
        self.store.init(self.project)
        self.started = start(self.store, title="Change value", goal="Keep tests green",
                             acceptance=["value test passes"], budget={"max_runs": 6})
        self.runner = Runner(self.store)
        self.check = f'"{PY}" -m unittest discover -s tests'

    def edit(self, text):
        (self.project / "app.py").write_text(text, encoding="utf-8")


class Budgets(RunnerBase):
    def test_full_green_then_one_confirmation_then_stop(self):
        first = self.runner.run(self.check, kind="full")
        self.assertEqual(first["exit_code"], 0)
        self.assertTrue(first["budget"]["green"])
        self.assertIn("stop editing", first["notice"])
        self.edit("VALUE = 1  # tweak\n")
        second = self.runner.run(self.check, kind="test")
        self.assertEqual(second["exit_code"], 0)
        self.assertEqual(second["budget"]["after_green_left"], 0)
        self.edit("VALUE = 1  # another tweak\n")
        third = self.runner.run(self.check, kind="test")
        self.assertTrue(third["refused"])
        self.assertTrue(any("Stop rule" in r for r in third["reasons"]))
        self.assertEqual(len(list((self.store.root / "logs" / self.started["task"]).iterdir())), 2)

    def test_identical_rerun_is_refused_with_previous_result(self):
        self.runner.run(self.check, kind="test")
        again = self.runner.run(self.check, kind="test")
        self.assertTrue(again["refused"])
        self.assertEqual(again["previous"]["exit_code"], 0)
        self.assertIn("OK", again["previous"]["output"])
        self.edit("VALUE = 1\n# changed\n")
        self.assertNotIn("refused", self.runner.run(self.check, kind="test"))

    def test_cache_files_written_by_a_run_do_not_hide_an_identical_rerun(self):
        # CI writes bytecode and tool caches that are not gitignored in the project.
        script = self.project / "cachey.py"
        script.write_text(
            "import os, time\n"
            "for d in ('tests/__pycache__', '.pytest_cache/v', 'node_modules/.cache'):\n"
            "    os.makedirs(d, exist_ok=True)\n"
            "stamp = str(time.time_ns())\n"
            "for f in ('tests/__pycache__/test_app.cpython-3.pyc', '.pytest_cache/v/lastfailed',\n"
            "          'node_modules/.cache/x', '.coverage', 'app.pyc'):\n"
            "    open(f, 'w').write(stamp)\n", encoding="utf-8")
        subprocess.run(["git", "add", "cachey.py"], cwd=self.project, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "cachey"],
                       cwd=self.project, check=True)
        command = f'"{PY}" cachey.py'
        self.assertNotIn("refused", self.runner.run(command, kind="test"))
        self.assertTrue(self.runner.run(command, kind="test")["refused"])
        (self.project / "notes.txt").write_text("a real untracked change", encoding="utf-8")
        self.assertNotIn("refused", self.runner.run(command, kind="test"))

    def test_repeat_failure_stops_then_override_is_limited_and_recorded(self):
        for i in range(3):
            self.edit(f"VALUE = {i + 2}\n")
            result = self.runner.run(fail_with("test_x"), kind="test")
            self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["budget"]["repeat_failures"], {"tests/test_a.py::test_x": 3})
        self.edit("VALUE = 9\n")
        blocked = self.runner.run(fail_with("test_x"), kind="test")
        self.assertTrue(blocked["refused"])
        self.assertIn("failed 3 times", " ".join(blocked["reasons"]))
        allowed = self.runner.run(fail_with("test_x"), kind="test", override="new hypothesis: wrong fixture")
        self.assertNotIn("refused", allowed)
        self.assertEqual(allowed["budget"]["overrides_left"], 1)
        with self.store.connect() as con:
            row = con.execute("SELECT override FROM runs WHERE id=?", (allowed["run"],)).fetchone()
        self.assertEqual(row[0], "new hypothesis: wrong fixture")

    def test_override_is_not_consumed_without_a_refusal(self):
        result = self.runner.run(self.check, kind="test", override="not needed")
        self.assertNotIn("overrides_left", result["budget"])

    def test_run_budget_exhaustion(self):
        for i in range(6):
            self.edit(f"VALUE = 1\n# {i}\n")
            self.assertNotIn("refused", self.runner.run(self.check, kind="test"))
        self.edit("VALUE = 1\n# last\n")
        out = self.runner.run(self.check, kind="test")
        self.assertTrue(out["refused"])
        self.assertIn("budget is exhausted", " ".join(out["reasons"]))
        other = self.runner.run(f'"{PY}" -c "print(1)"', kind="other")
        self.assertEqual(other["exit_code"], 0)

    def test_missing_local_npx_binary_is_refused_not_installed(self):
        (self.project / "node_modules" / ".bin").mkdir(parents=True)
        out = self.runner.run("npx knip --no-progress", kind="other")
        self.assertTrue(out["refused"])
        self.assertIn("not installed locally", out["reasons"][0])

    def test_timeout_kills_and_reports(self):
        out = self.runner.run(f'"{PY}" -c "import time; time.sleep(30)"', kind="other", timeout=1)
        self.assertEqual(out["exit_code"], 124)
        self.assertTrue(out["timed_out"])

    def test_cli_exit_codes(self):
        def call(*args):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                code = main(["--state", str(self.store.root), *args])
            return code, stdout.getvalue()
        code, out = call("run", "--", PY, "-c", "import sys; sys.exit(3)")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["exit_code"], 3)
        code, out = call("run", "--", PY, "-c", "import sys; sys.exit(3)")
        self.assertEqual(code, REFUSED_EXIT)
        self.assertTrue(json.loads(out)["refused"])

    def test_long_run_reports_progress_heartbeats(self):
        events = []
        out = self.runner.run(f'"{PY}" -c "import time; time.sleep(1.3)"', kind="other",
                              progress=events.append, heartbeat_s=0.4)
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(events[0]["stage"], "started")
        self.assertIn("do not poll", events[0]["message"].lower())
        self.assertGreaterEqual(sum(e["stage"] == "running" for e in events), 2)

    def test_cli_heartbeat_goes_to_stderr_not_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"CONTEXT_HARNESS_HEARTBEAT": "0.3"}):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(["--state", str(self.store.root), "run", "--", PY, "-c", "import time; time.sleep(0.8)"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["exit_code"], 0)
        self.assertIn("[context-harness] run", stderr.getvalue())
        self.assertIn("still running", stderr.getvalue())

    def test_long_output_is_shaped_and_log_kept(self):
        script = "for i in range(400): print('noise line', i)\nprint('FAILED tests/t.py::test_big - x')\nraise SystemExit(1)"
        path = self.project / "noisy.py"
        path.write_text(script, encoding="utf-8")
        out = self.runner.run(f'"{PY}" noisy.py', kind="test")
        self.assertTrue(out["shaped"])
        self.assertEqual(out["failed_tests"], ["tests/t.py::test_big"])
        self.assertIn("noise line 399", Path(out["log"]).read_text(encoding="utf-8"))
        self.assertNotIn("output", out)


class NonGitProject(RunnerBase):
    git = False

    def test_no_fingerprint_means_no_redundancy_refusal(self):
        self.runner.run(self.check, kind="test")
        self.assertNotIn("refused", self.runner.run(self.check, kind="test"))


class DefaultState(unittest.TestCase):
    def test_project_selects_state_outside_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            project = root / "p"
            project.mkdir()
            with mock.patch.dict(os.environ, {"CONTEXT_HARNESS_HOME": str(root / "home")}):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main(["--project", str(project), "start", "--title", "T", "--goal", "G",
                                 "--acceptance", "A"])
                self.assertEqual(code, 0)
                states = list((root / "home" / "projects").iterdir())
                self.assertEqual(len(states), 1)
                self.assertTrue(states[0].name.startswith("p-"))
                self.assertFalse((project / ".context-harness").exists())


if __name__ == "__main__":
    unittest.main()
