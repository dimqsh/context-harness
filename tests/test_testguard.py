"""v0.5 acceptance: pre-existing tests are contracts; the guard always restores agent files."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from context_harness.runner import Runner
from context_harness.session import lesson_candidates, start
from context_harness.store import Store
from context_harness.testguard import TestGuard

PY = sys.executable
ORIGINAL = ("import unittest\nfrom app import handler\n\nclass T(unittest.TestCase):\n"
            "    def test_mouse(self):\n        self.assertEqual(handler('mousedown'), 'closed')\n")


class Guard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.project = root / "project"
        (self.project / "tests").mkdir(parents=True)
        self.app = self.project / "app.py"
        self.app.write_text("def handler(event):\n    return 'closed' if event == 'mousedown' else 'open'\n",
                            encoding="utf-8")
        self.test = self.project / "tests" / "test_app.py"
        self.test.write_text(ORIGINAL, encoding="utf-8")
        (self.project / "tests" / "__init__.py").write_text("", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        self.store = Store(root / "state")
        self.store.init(self.project)
        self.task = start(self.store, title="Pointer events", goal="Use pointer events",
                          acceptance=["outside pointerdown closes"])["task"]
        self.guard = TestGuard(self.store, self.task)

    def rewrite_old_test(self):
        self.test.write_text(ORIGINAL.replace("'mousedown'", "'pointerdown'"), encoding="utf-8")
        self.app.write_text("def handler(event):\n    return 'closed' if event == 'pointerdown' else 'open'\n",
                            encoding="utf-8")

    def test_start_snapshots_pre_existing_tests(self):
        manifest = self.guard.manifest()
        self.assertIn("tests/test_app.py", manifest["files"])
        self.assertNotIn("app.py", manifest["files"])

    def test_additions_only_are_fine(self):
        self.test.write_text(ORIGINAL + "\n    def test_more(self):\n        self.assertTrue(True)\n",
                             encoding="utf-8")
        result = self.guard.check()
        self.assertTrue(result["ok"])
        self.assertEqual(result["extended_only"][0]["path"], "tests/test_app.py")

    def test_rewritten_assertion_is_flagged_and_original_fails_against_new_code(self):
        self.rewrite_old_test()
        result = self.guard.check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["needs_justification"], ["tests/test_app.py"])
        self.assertIn("self.assertEqual(handler('mousedown'), 'closed')",
                      result["modified"][0]["removed_assertions"])
        before = self.test.read_bytes()
        out = self.guard.run_original(f'"{PY}" -m unittest {{files}}', Runner(self.store))
        self.assertTrue(out["ran"])
        self.assertEqual(out["run"]["exit_code"], 1)
        self.assertIn("FAIL", out["verdict"])
        self.assertEqual(self.test.read_bytes(), before, "agent version must be restored")

    def test_deleted_test_is_flagged_and_restored_after_guard_run(self):
        self.test.unlink()
        result = self.guard.check()
        self.assertEqual(result["deleted"], ["tests/test_app.py"])
        self.guard.run_original(f'"{PY}" -m unittest {{files}}', Runner(self.store))
        self.assertFalse(self.test.exists(), "deleted file stays deleted after the guard run")

    def test_interrupted_guard_is_repaired(self):
        self.rewrite_old_test()
        agent_version = self.test.read_bytes()
        backup = self.guard.folder / "guard-backup"
        backup.mkdir(parents=True, exist_ok=True)
        import hashlib
        name = hashlib.sha256(agent_version).hexdigest()
        (backup / name).write_bytes(agent_version)
        self.guard.active_path.write_text(json.dumps({"backups": {"tests/test_app.py": name}}), encoding="utf-8")
        self.test.write_text(ORIGINAL, encoding="utf-8")  # as if the swap happened and then crashed
        self.guard.check()
        self.assertEqual(self.test.read_bytes(), agent_version)
        self.assertFalse(self.guard.active_path.exists())

    def test_template_must_name_files(self):
        self.rewrite_old_test()
        with self.assertRaisesRegex(ValueError, "{files}"):
            self.guard.run_original("python -m unittest", Runner(self.store))

    def test_report_requires_justification_and_records_harness_evidence(self):
        self.rewrite_old_test()
        with self.assertRaisesRegex(ValueError, "without justification: tests/test_app.py"):
            self.store.report(self.task, "solver", "completed", "done", ["app.py"], [])
        self.store.report(self.task, "solver", "blocked", "partial", ["app.py"], [])
        self.store.report(self.task, "solver", "completed", "done", ["app.py"], ["ran tests"],
                          ["tests/test_app.py: mousedown no longer fires on touch devices"])
        report = self.store.summary(self.task)["agents"][0]["report"]
        joined = "\n".join(report["evidence"])
        self.assertIn("harness test-guard: 2 pre-existing test files; rewritten/deleted: tests/test_app.py", joined)
        self.assertIn("harness test-change: tests/test_app.py: mousedown", joined)

    def test_lesson_candidates_capture_the_contract(self):
        self.rewrite_old_test()
        drafts = lesson_candidates(self.store, self.task)["drafts"]
        self.assertEqual(drafts[0]["title"], "Contract in tests/test_app.py")
        self.assertIn("handler('mousedown')", drafts[0]["content"])
        applied = lesson_candidates(self.store, self.task, apply=True)
        with self.store.connect() as con:
            status = con.execute("SELECT status FROM memories WHERE id=?", (applied["added_proposed"][0],)).fetchone()[0]
        self.assertEqual(status, "proposed")


if __name__ == "__main__":
    unittest.main()
