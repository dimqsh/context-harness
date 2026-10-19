"""Behavioral checks with temporary projects and an explicit local API fixture."""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from context_harness.decision import DecisionClient, MissingAPIKeyError
from context_harness.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.source = self.project / "policy.txt"
        self.source.write_text("Refund retries must be idempotent. Возврат платежа.", encoding="utf-8")
        self.store = Store(self.base / "state")
        self.store.init(self.project)

    def memory(self, title="Refund policy", content="Refund retries must be idempotent.",
               domain="backend", status="verified", source="policy.txt", evidence="Read policy.txt"):
        return self.store.memory_add(title, content, domain, source, status, evidence)

    def task(self):
        return self.store.task_create("Fix refund", "Preserve refund idempotency",
                                      ["Repeated cancellation does not credit twice"])["id"]

    def assign(self, tid, agent="backend", paths=None):
        return self.store.assign(tid, agent, "backend", "Fix refund",
                                 ["src"] if paths is None else paths,
                                 "refund", backend="lexical")

    def report(self, tid, agent="backend", status="completed", artifacts=None):
        return self.store.report(tid, agent, status, "Refund check finished",
                                 artifacts or [], ["Executed fixture regression check"])

    def completed_task(self):
        tid = self.task()
        self.assign(tid)
        artifact = self.project / "result.txt"
        artifact.write_text("first result", encoding="utf-8")
        self.report(tid, artifacts=["result.txt"])
        return tid, artifact

    def test_state_cannot_be_rebound_and_projects_do_not_share_memories(self):
        self.memory()
        other = self.base / "other-project"
        other.mkdir()
        with self.assertRaises(ValueError):
            self.store.init(other)
        self.assertEqual(self.store.project, self.project)
        separate = Store(self.base / "other-state")
        separate.init(other)
        self.assertEqual(separate.shortlist("refund"), [])
        self.assertEqual(len(self.store.shortlist("refund")), 1)

    def test_verified_memory_requires_evidence(self):
        with self.assertRaises(ValueError):
            self.memory(evidence="  ")
        self.assertEqual(self.store.shortlist("refund"), [])
        proposal = self.memory(status="proposed", evidence="")
        with self.assertRaises(ValueError):
            self.store.memory_verify(proposal["id"], "")
        self.assertEqual(self.store.shortlist("refund"), [])

    def test_proposals_excluded_until_explicit_verification(self):
        proposal = self.memory(status="proposed", evidence="")
        self.assertEqual(self.store.shortlist("refund"), [])
        result = self.store.memory_verify(proposal["id"], "Checked policy.txt")
        self.assertTrue(result["source_tracked"])
        self.assertEqual([row["id"] for row in self.store.shortlist("refund")], [proposal["id"]])

    def test_domain_search_includes_general_but_excludes_other_specialists(self):
        backend = self.memory(domain="backend")["id"]
        frontend = self.memory(domain="frontend")["id"]
        general = self.memory(domain="general")["id"]
        matches = {row["id"] for row in self.store.shortlist("refund", domain="backend")}
        self.assertEqual(matches, {backend, general})
        self.assertNotIn(frontend, matches)
        self.assertEqual(len(self.store.shortlist("refund")), 3)

    def test_changed_or_deleted_source_excludes_old_memory_until_reverified(self):
        record = self.memory(source="policy.txt:1")
        self.assertTrue(record["source_tracked"])
        self.source.write_text("Refunds now require a new transaction contract.", encoding="utf-8")
        self.assertEqual(self.store.shortlist("refund"), [])
        self.store.memory_verify(record["id"], "Rechecked changed source")
        self.assertEqual(len(self.store.shortlist("refund")), 1)
        self.source.unlink()
        self.assertEqual(self.store.shortlist("refund"), [])

    def test_external_sources_are_explicitly_not_hash_tracked(self):
        result = self.memory(source="https://example.test/policy")
        self.assertFalse(result["source_tracked"])

    def test_quoted_cyrillic_and_sql_like_search_input_is_data(self):
        record = self.memory(content="Возврат платежа выполняется один раз.")["id"]
        for query in ('"Возврат"', "возврат", 'возврат\"; DROP TABLE memories; --'):
            with self.subTest(query=query):
                self.assertIn(record, [row["id"] for row in self.store.shortlist(query)])
        self.assertEqual(self.store.shortlist('"\" ; --'), [])
        self.assertEqual(len(self.store.shortlist("возврат")), 1)

    def test_lexical_search_is_explicit_and_does_not_require_credentials(self):
        record = self.memory()["id"]
        with patch.dict(os.environ, {}, clear=True):
            found = self.store.search("refund", backend="lexical")
        self.assertEqual(found["backend"], "lexical")
        self.assertIsNone(found["model"])
        self.assertEqual(found["records"][0]["id"], record)
        self.assertIsNone(found["records"][0]["decision_score"])
        self.assertTrue(any(e["type"] == "search" for e in self.store.events()))

    def test_native_search_does_not_fall_back_without_credentials(self):
        self.memory()
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(MissingAPIKeyError):
            self.store.search("refund", backend="decisions")

    def test_assignment_packet_contains_acceptance_sources_and_ownership(self):
        record = self.memory()["id"]
        tid = self.task()
        assignment = self.assign(tid, paths=["src/backend", "src/backend"])
        packet = json.loads(Path(assignment["packet"]).read_text(encoding="utf-8"))
        self.assertEqual(packet["task"]["id"], tid)
        self.assertTrue(packet["task"]["acceptance"])
        self.assertEqual(packet["allow_paths"], ["src/backend"])
        self.assertFalse(packet["read_only"])
        self.assertEqual(packet["context_backend"], "lexical")
        self.assertEqual(packet["memory_evidence"][0]["id"], record)
        self.assertEqual(packet["memory_evidence"][0]["source"], "policy.txt")
        readonly = self.assign(tid, "reviewer", paths=[])
        packet = json.loads(Path(readonly["packet"]).read_text(encoding="utf-8"))
        self.assertTrue(packet["read_only"])

    def test_parent_child_ownership_conflicts_but_sibling_and_readonly_do_not(self):
        tid = self.task()
        self.assign(tid, paths=["src/backend"])
        for agent, paths in (("parent", ["src"]), ("child", ["src/backend/payments.py"]),
                             ("same", ["src/backend"])):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.assign(tid, agent, paths)
        self.assign(tid, "frontend", ["src/frontend"])
        self.assign(tid, "reviewer", [])
        self.assertEqual(self.store.summary(tid)["assigned"], 3)

    def test_path_traversal_git_and_symlink_escape_rejected(self):
        tid = self.task()
        outside = self.base / "outside"
        outside.mkdir()
        (self.project / "escape").symlink_to(outside, target_is_directory=True)
        for paths in (["../outside"], [str(outside)], [".git/config"], ["escape/file.py"]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.assign(tid, "writer", paths)
        self.assertEqual(self.store.summary(tid)["assigned"], 0)

    def test_duplicate_or_path_like_agent_names_rejected(self):
        tid = self.task()
        self.assign(tid, "backend", [])
        with self.assertRaises(ValueError):
            self.assign(tid, "backend", [])
        for name in ("../agent", "agent/child", "", "x" * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.assign(tid, name, [])

    def test_concurrent_ownership_check_is_atomic(self):
        tid = self.task()
        start = threading.Barrier(2)

        def compete(agent):
            start.wait(timeout=5)
            try:
                self.assign(tid, agent, ["src/shared"])
                return "assigned"
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(compete, ["first", "second"]))
        self.assertCountEqual(outcomes, ["assigned", "rejected"])
        self.assertEqual(self.store.summary(tid)["assigned"], 1)

    def test_unknown_agent_cannot_submit_report(self):
        tid = self.task()
        with self.assertRaises(ValueError):
            self.report(tid, "unassigned")
        self.assertEqual(self.store.summary(tid)["report_version"], 0)

    def test_pass_requires_every_agent_to_have_latest_completed_report(self):
        tid = self.task()
        with self.assertRaises(ValueError):
            self.store.verify(tid, True, "Checked", ["check"])
        self.assign(tid)
        self.assign(tid, "reviewer", [])
        self.report(tid)
        for state in (None, "blocked", "failed"):
            if state:
                self.report(tid, "reviewer", state)
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.store.verify(tid, True, "Checked", ["check"])
        self.report(tid, "reviewer", "completed", ["policy.txt"])
        self.assertTrue(self.store.verify(tid, True, "Checked", ["independent check"])["verified"])

    def test_no_artifacts_cannot_establish_fresh_verification(self):
        tid = self.task()
        self.assign(tid)
        self.report(tid)
        with self.assertRaises(ValueError):
            self.store.verify(tid, True, "Checked", ["check"])

    def test_memory_admission_rejects_records_that_cannot_be_ranked(self):
        for values in ({"content": "x" * 2001}, {"title": "x" * 501},
                       {"domain": "x" * 201}, {"source": "x" * 1001}):
            with self.subTest(values=list(values)), self.assertRaises(ValueError):
                self.memory(**values)
        self.assertEqual(self.store.shortlist("x"), [])

    def test_writable_ownership_spans_active_tasks_and_releases_after_verification(self):
        first, artifact = self.completed_task()
        second = self.task()
        with self.assertRaises(ValueError):
            self.assign(second, "other-writer", ["src"])
        self.assign(second, "reader", [])
        self.store.verify(first, True, "Checked", ["check"])
        self.assign(second, "other-writer", ["src"])

    def test_closed_tasks_do_not_reacquire_paths_when_their_old_artifacts_change(self):
        tid, artifact = self.completed_task()
        self.store.verify(tid, True, "Checked", ["check"])
        artifact.write_text("subsequent task changes the old artifact")
        self.assertFalse(self.store.summary(tid)["verified"])
        self.assertTrue(self.store.summary(tid)["task"]["closed"])
        self.assign(self.task(), "new-writer", ["src"])
        with self.assertRaises(ValueError):
            self.assign(tid, "resumed-writer", ["new-path"])

    def test_explicit_closing_releases_ownership_without_passing(self):
        tid = self.task()
        self.assign(tid)
        closed = self.store.close(tid, "Cancelled by coordinator")
        self.assertFalse(closed["verified"])
        self.assign(self.task(), "replacement", ["src"])

    def test_simultaneous_tasks_cannot_both_acquire_the_same_path(self):
        tids = [self.task(), self.task()]
        start = threading.Barrier(2)
        def compete(tid):
            start.wait(timeout=5)
            try:
                self.assign(tid, "writer", ["src/shared"])
                return "assigned"
            except ValueError:
                return "rejected"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(compete, tids)), ["assigned", "rejected"])

    def test_completion_alone_is_not_verification(self):
        tid, _ = self.completed_task()
        before = self.store.summary(tid)
        self.assertEqual(before["completed"], 1)
        self.assertFalse(before["verified"])
        for summary, evidence in (("", ["check"]), ("Checked", [])):
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                self.store.verify(tid, True, summary, evidence)
        failed = self.store.verify(tid, False, "Regression found", ["failed regression check"])
        self.assertFalse(failed["verified"])

    def test_new_report_invalidates_existing_verification(self):
        tid, _ = self.completed_task()
        original = self.store.verify(tid, True, "Checked", ["independent check"])
        self.assertTrue(original["verified"])
        self.report(tid)
        after = self.store.summary(tid)
        self.assertFalse(after["verified"])
        self.assertTrue(after["verification"]["stale"])
        self.assertGreater(after["report_version"], original["report_version"])

    def test_artifact_edit_or_deletion_invalidates_verification(self):
        tid, artifact = self.completed_task()
        self.assertTrue(self.store.verify(tid, True, "Checked", ["check"])["verified"])
        artifact.write_text("modified result", encoding="utf-8")
        self.assertFalse(self.store.summary(tid)["verified"])
        self.assertTrue(self.store.summary(tid)["verification"]["stale"])
        self.assertTrue(self.store.verify(tid, True, "Rechecked", ["fresh check"])["verified"])
        artifact.unlink()
        self.assertFalse(self.store.summary(tid)["verified"])

    def test_repointed_artifact_symlink_invalidates_verification(self):
        tid = self.task()
        self.assign(tid)
        original = self.project / "original.txt"
        replacement = self.project / "replacement.txt"
        original.write_text("checked result")
        replacement.write_text("unreviewed replacement")
        alias = self.project / "result.txt"
        alias.symlink_to(original)
        self.report(tid, artifacts=["result.txt"])
        self.assertTrue(self.store.verify(tid, True, "Checked", ["check"])["verified"])
        alias.unlink()
        alias.symlink_to(replacement)
        self.assertFalse(self.store.summary(tid)["verified"])

    def test_nonexistent_and_outside_artifacts_cannot_be_verified(self):
        tid = self.task()
        self.assign(tid)
        outside = self.base / "private.txt"
        outside.write_text("outside")
        for artifact in ("missing.txt", str(outside)):
            self.report(tid, artifacts=[artifact])
            with self.subTest(artifact=artifact), self.assertRaises(ValueError):
                self.store.verify(tid, True, "Checked", ["check"])
            self.assertFalse(self.store.summary(tid)["verified"])

    def test_report_arriving_during_verification_rejects_stale_snapshot(self):
        tid, _ = self.completed_task()
        original_hashes = self.store._artifact_hashes

        def concurrent_report(reports):
            self.report(tid)
            return original_hashes(reports)

        with patch.object(self.store, "_artifact_hashes", side_effect=concurrent_report):
            with self.assertRaises(ValueError):
                self.store.verify(tid, True, "Checked", ["check"])
        self.assertIsNone(self.store.summary(tid)["verification"])

    def test_store_native_search_through_real_local_http_client(self):
        # Transport integration fixture, not a live model-quality evaluation.
        first = self.memory(content="Refund settlement is idempotent.")["id"]
        second = self.memory(content="Refund display changes only.")["id"]
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, payload))
                candidates = json.loads(payload["input"])["candidates"]
                answers = [{"type": "score", "name": question["name"],
                            "score": 0.95 if candidate["id"] == first else 0.1}
                           for question, candidate in zip(payload["questions"], candidates)]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"answers": answers}).encode())

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = DecisionClient(base_url=f"http://127.0.0.1:{server.server_port}/v1",
                                    api_key="local-fixture-only")
            found = self.store.search("refund", "backend", backend="decisions", limit=1, client=client)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
        self.assertEqual(found["backend"], "decisions")
        self.assertEqual(found["model"], "gpt-6-luna")
        self.assertEqual(found["shortlist_size"], 2)
        self.assertEqual(found["records"][0]["id"], first)
        self.assertEqual(found["records"][0]["decision_score"], 0.95)
        self.assertEqual(calls[0][0], "/v1/decisions")
        sent = json.loads(calls[0][1]["input"])["candidates"]
        self.assertEqual({row["id"] for row in sent}, {first, second})

    def test_cli_workflow_in_real_subprocesses(self):
        state = self.base / "cli-state"
        source_root = Path(__file__).resolve().parents[1] / "src"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(source_root)
        env.pop("OPENAI_API_KEY", None)

        def cli(*args, success=True):
            result = subprocess.run([sys.executable, "-m", "context_harness", "--state", str(state), *args],
                                    capture_output=True, text=True, env=env, timeout=15)
            if success:
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            return json.loads(result.stderr)

        cli("init", "--project", str(self.project))
        cli("memory-add", "--title", "Refund policy", "--content", "Refund idempotency",
            "--domain", "backend", "--source", "policy.txt", "--status", "verified",
            "--evidence", "Read policy.txt")
        task = cli("task-create", "--title", "Fix refund", "--goal", "Preserve idempotency",
                   "--acceptance", "Repeated refund does not double-credit")["id"]
        assigned = cli("assign", task, "--agent", "backend", "--domain", "backend", "--goal", "Fix refund",
                       "--query", "refund", "--allow-path", "src", "--backend", "lexical")
        self.assertTrue(Path(assigned["packet"]).is_file())
        self.assertFalse(cli("summary", task)["verified"])
        cli("report", task, "--agent", "unknown", "--status", "completed", "--summary", "done", success=False)
        self.source.write_text("Refund idempotency verified.", encoding="utf-8")
        cli("report", task, "--agent", "backend", "--status", "completed", "--summary", "done",
            "--artifact", "policy.txt", "--evidence", "fixture check")
        self.assertFalse(cli("summary", task)["verified"])
        checked = cli("verify", task, "--passed", "--summary", "Independent fixture verification",
                      "--evidence", "Read final artifact")
        self.assertTrue(checked["verified"])
        self.assertTrue(cli("summary", task)["verified"])


if __name__ == "__main__":
    unittest.main()
