"""v0.5 acceptance: code memory becomes pointers to current files; facts arrive whole once."""
import json
from pathlib import Path
import tempfile
import unittest

from context_harness.context import memory_view
from context_harness.session import start
from context_harness.store import Store

CODE = "".join(f"line {i}\n" for i in range(1, 41))


class Inline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.project = root / "project"
        (self.project / "src").mkdir(parents=True)
        self.file = self.project / "src" / "dismiss.ts"
        self.file.write_text(CODE, encoding="utf-8")
        self.store = Store(root / "state")
        self.store.init(self.project)
        chunk = "".join(f"line {i}\n" for i in range(10, 16))
        self.code_id = self.store.memory_add("src/dismiss.ts | lines 10-15 | useDismiss", chunk, "frontend",
                                             "src/dismiss.ts:10", "verified", "indexed")["id"]
        self.fact_id = self.store.memory_add(
            "Dismiss contract", "Hidden fixtures dispatch mousedown/touchstart; keep that event family.",
            "frontend", "review of task t_1", "verified", "independent acceptance")["id"]

    def record(self, mid):
        with self.store.connect() as con:
            return dict(con.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone())

    def test_code_chunk_becomes_pointer(self):
        view = memory_view(self.project, self.record(self.code_id))
        self.assertEqual(view, {"id": self.code_id, "kind": "code", "path": "src/dismiss.ts",
                                "lines": "10-15", "symbols": "useDismiss"})

    def test_inline_mode_reads_current_text(self):
        view = memory_view(self.project, self.record(self.code_id), "inline")
        self.assertEqual(view["text"].splitlines()[0], "line 10")
        self.assertEqual(len(view["text"].splitlines()), 6)

    def test_fact_is_delivered_in_full(self):
        view = memory_view(self.project, self.record(self.fact_id))
        self.assertEqual(view["kind"], "fact")
        self.assertIn("mousedown/touchstart", view["content"])
        self.assertEqual(view["evidence"], "independent acceptance")

    def test_start_returns_context_inline_without_second_hop(self):
        out = start(self.store, title="Escape closes parent", goal="dismiss only the top layer",
                    acceptance=["one Escape closes one layer"], query="useDismiss mousedown")
        kinds = {c["kind"] for c in out["context"]}
        self.assertEqual(kinds, {"code", "fact"})
        self.assertNotIn("memory-read", json.dumps(out))
        self.assertEqual(out["context_retrieval"], "lexical")
        self.assertIn("auto: lexical", out["context_retrieval_note"])

    def test_auto_backend_explains_missing_semantic_index_for_cross_language_query(self):
        backend, note = self.store.auto_backend("Escape закрывает родительское окно фильтров")
        self.assertEqual(backend, "lexical")
        self.assertIn("different scripts", note)
        self.assertIn("no semantic index", note)

    def test_same_language_query_stays_lexical_whatever_the_corpus_size(self):
        backend, note = self.store.auto_backend("Escape closes the parent dialog")
        self.assertEqual(backend, "lexical")
        self.assertIn("same language", note)

    def add_code(self, path, start, lines, symbols=""):
        file = self.project / path
        file.parent.mkdir(parents=True, exist_ok=True)
        if not file.exists():
            file.write_text("".join(f"{path} filler {i}\n" for i in range(1, 80)), encoding="utf-8")
        text = file.read_text(encoding="utf-8").splitlines(keepends=True)
        for i, line in enumerate(lines):
            text[start - 1 + i] = line + "\n"
        file.write_text("".join(text), encoding="utf-8")
        title = f"{path} | lines {start}-{start + len(lines) - 1} | {symbols}"
        return self.store.memory_add(title, "".join(l + "\n" for l in lines), "frontend", f"{path}:{start}",
                                     "verified", "indexed")["id"]

    def test_identifier_in_query_pins_file_and_merges_its_chunks(self):
        lines = ["export function useSyncQueue() {", "  const itemsRef = 1", "  applyItems()", "}"]
        (self.project / "src" / "use-sync-queue.ts").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for start_line, chunk, symbols in ((1, lines[:2], "useSyncQueue"), (3, lines[2:], "applyItems")):
            self.store.memory_add(f"src/use-sync-queue.ts | lines {start_line}-{start_line + 1} | {symbols}",
                                  "\n".join(chunk) + "\n", "frontend", f"src/use-sync-queue.ts:{start_line}",
                                  "verified", "indexed")
        self.add_code("backend/reconciliation.py", 5, ["stale response total"], "reconcile")
        out = start(self.store, title="T", goal="G", acceptance=["A"], query="local updates lost in use-sync-queue")
        code = [c for c in out["context"] if c["kind"] == "code"]
        self.assertEqual(len(code), 1)
        self.assertEqual(code[0]["path"], "src/use-sync-queue.ts")
        self.assertEqual(code[0]["lines"], "1-4")
        self.assertEqual(len(code[0]["ids"]), 2)
        self.assertIn("applyItems", code[0]["symbols"])

    def test_vague_query_drops_code_pointers_without_rare_word_coverage(self):
        for i in range(40):
            self.add_code(f"src/common{i}.ts", 1, [f"export const total{i} = render(list)"])
        unrelated = self.add_code("backend/recon.py", 1, ["counter of stale refunds"], "reconcile")
        related = self.add_code("src/paged.ts", 1, ["paged counter grows when updates are lost"], "paged")
        out = start(self.store, title="T", goal="G", acceptance=["A"],
                    query="paged counter grows when updates are lost")
        ids = [c.get("id") for c in out["context"] if c["kind"] == "code"]
        self.assertIn(related, ids)
        self.assertNotIn(unrelated, ids)
        packet = json.loads((self.store.root / "packets" / out["task"] / "solver.json").read_text())
        self.assertGreaterEqual(packet["context_dropped_irrelevant"], 1)
        self.assertIn("rare query words", packet["context_relevance"])

    def test_facts_survive_the_relevance_gate(self):
        out = start(self.store, title="T", goal="G", acceptance=["A"], query="event family fixtures mousedown")
        self.assertIn(self.fact_id, [c.get("id") for c in out["context"]])

    def test_explicit_legacy_views_still_work(self):
        tid = self.store.task_create("T", "G", ["A"])["id"]
        out = self.store.assign(tid, "worker", "frontend", "G", ["src"], "dismiss", backend="lexical",
                                context_view="index")
        packet = json.loads(Path(out["packet"]).read_text(encoding="utf-8"))
        self.assertEqual(packet["context_detail_command"], "memory-read ID [ID ...]")
        self.assertNotIn("kind", packet["memory_evidence"][0])


if __name__ == "__main__":
    unittest.main()
