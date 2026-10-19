"""Telemetry must reflect recorded actions, not invent completed agent executions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_harness.store import Store
from context_harness.decision import MissingAPIKeyError


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.root / "state")
        self.store.init(self.project)
        self.store.memory_add("refund contract", "A refund requires an idempotency key.",
                              "backend", "manual source", "verified", "Fixture fact")
        self.task = self.store.task_create("Refund", "Investigate", ["Read source"])["id"]

    def test_retrieval_belongs_to_assignment_and_execution_requires_progress(self):
        packet = self.store.assign(self.task, "worker", "backend", "Investigate", [],
                                   "refund", backend="lexical")
        events = self.store.events(self.task)
        self.assertEqual([e["type"] for e in events], ["task_created", "search_started",
                         "context_shortlisted", "search", "agent_assigned"])
        self.assertTrue(all(e["data"]["agent"] == "worker" for e in events[1:]))
        self.assertFalse(events[-2]["data"]["api_called"])
        self.assertEqual(self.store.summary(self.task)["agents"][0]["status"], "assigned")
        self.store.progress(self.task, "worker", "running", "Opened source file")
        self.store.report(self.task, "worker", "completed", "Inspected", [], ["Read source"])
        self.assertEqual([e["type"] for e in self.store.events(self.task)][-2:],
                         ["agent_progress", "agent_reported"])
        self.assertEqual(self.store.events(self.task)[-2]["data"]["stage"], "running")
        self.assertEqual(json.loads(Path(packet["packet"]).read_text())["context_backend"], "lexical")

    def test_rank_failure_is_visible_and_does_not_pretend_success(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(MissingAPIKeyError):
            self.store.search("refund", backend="decisions", task_id=self.task, agent="worker")
        events = self.store.events(self.task)
        self.assertEqual(events[-1]["type"], "search_failed")
        self.assertEqual(events[-1]["data"]["error_type"], "MissingAPIKeyError")
        self.assertNotIn("search", [e["type"] for e in events])

    def test_unassigned_or_closed_agents_cannot_claim_progress(self):
        with self.assertRaises(ValueError):
            self.store.progress(self.task, "unknown", "running", "Work")
        self.store.assign(self.task, "worker", "backend", "Read", [], "refund", "lexical")
        self.store.close(self.task, "Cancelled")
        with self.assertRaises(ValueError):
            self.store.progress(self.task, "worker", "running", "Work")


if __name__ == "__main__":
    unittest.main()
