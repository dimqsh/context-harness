"""Retrieval contracts with a deterministic injected encoder; no network/model download.

These tests check wiring, lifecycle and ranking, not learned model quality. The
real multilingual model is measured separately on the frozen repository cases.
"""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from context_harness.semantic import OnnxEncoder, SemanticError, SemanticIndex, normalize_code
from context_harness.store import Store

HAS_SEMANTIC_DEPS = all(importlib.util.find_spec(name) is not None for name in ("numpy", "snowballstemmer"))


class FixtureEncoder:
    model_name = "fixture-only"
    fingerprint = "fixture-v1"
    dimensions = 4

    def __init__(self):
        self.calls = []

    def encode(self, texts, *, kind="passage"):
        self.calls.append((kind, list(texts)))
        out = []
        for text in texts:
            lowered = text.lower()
            # Deterministic semantic fixture: unrelated strings share a vector.
            if any(word in lowered for word in ("refund", "возврат", "reimburse")):
                out.append([1, 0, 0, 0])
            elif "invoice" in lowered:
                out.append([0, 1, 0, 0])
            else:
                out.append([0, 0, 1, 0])
        return out


@unittest.skipUnless(HAS_SEMANTIC_DEPS, "optional semantic dependencies are not installed")
class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "source.txt").write_text("source original")
        self.store = Store(self.root / "state")
        self.store.init(self.project)
        self.encoder = FixtureEncoder()
        self.index = SemanticIndex(self.store, encoder=self.encoder)

    def add(self, title, content="body", domain="backend", status="verified", source="external:test"):
        return self.store.memory_add(title, content, domain, source, status,
                                     "fixture source checked" if status == "verified" else "")["id"]

    def test_cross_language_retrieval_reaches_beyond_empty_lexical_shortlist(self):
        refund = self.add("Refund workflow", "Refund payments safely")
        self.add("Invoice rendering")
        self.index.build()
        self.assertEqual(self.store.shortlist("возврат денег"), [])
        self.assertEqual(self.index.search("возврат денег", limit=1)[0]["id"], refund)
        self.assertEqual(self.index.search("reimburse", limit=1)[0]["id"], refund)

    def test_store_reports_local_model_and_never_calls_ranker(self):
        refund = self.add("Refund workflow")
        self.index.build()
        client = unittest.mock.Mock()
        result = self.store.search("возврат", backend="hybrid", semantic_index=self.index, client=client)
        self.assertEqual(result["records"][0]["id"], refund)
        self.assertEqual(result["retrieval_model"], "fixture-only")
        self.assertEqual(result["retrieval"], "hybrid")
        self.assertFalse(result["api_called"])
        client.rank.assert_not_called()

    def test_semantic_retrieval_failure_is_recorded_before_api_ranking(self):
        with self.assertRaises(SemanticError):
            self.store.search("возврат", backend="decisions", retrieval="hybrid", semantic_index=self.index)
        event = self.store.events()[-1]
        self.assertEqual(event["type"], "search_failed")
        self.assertEqual(event["data"]["phase"], "retrieval")

    def test_exact_identifier_is_preserved_even_when_semantic_signal_disagrees(self):
        exact = self.add("PaymentService.process_refund", "invoice")
        for _ in range(8):
            self.add("Refund payment", "refund")
        self.index.build()
        self.assertEqual(self.index.search("PaymentService.process_refund", limit=1)[0]["id"], exact)

    def test_proposed_and_other_domains_are_excluded_general_is_included(self):
        backend = self.add("Refund backend")
        general = self.add("Refund general", domain="general")
        self.add("Refund frontend", domain="frontend")
        self.add("Refund proposal", status="proposed")
        self.index.build()
        ids = {row["id"] for row in self.index.search("возврат", domain="backend")}
        self.assertEqual(ids, {backend, general})

    def test_changed_and_deleted_sources_are_excluded_without_reembedding(self):
        first = self.add("Refund tracked", source="source.txt:1")
        self.index.build()
        self.assertEqual(self.index.search("возврат")[0]["id"], first)
        (self.project / "source.txt").write_text("source modified")
        self.assertEqual(self.index.search("возврат"), [])
        (self.project / "source.txt").unlink()
        self.assertEqual(self.index.search("возврат"), [])

    def test_changed_memory_fails_until_explicit_incremental_build(self):
        first = self.add("Refund first")
        self.add("Invoice second")
        initial = self.index.build()
        self.assertEqual(initial["embedded"], 2)
        self.assertEqual(self.index.build()["reused"], 2)
        with self.store.connect() as con:
            con.execute("UPDATE memories SET content='updated text' WHERE id=?", (first,))
        with self.assertRaisesRegex(SemanticError, "stale"):
            self.index.search("возврат")
        rebuilt = self.index.build()
        self.assertEqual(rebuilt["embedded"], 1)
        self.assertEqual(rebuilt["reused"], 1)
        self.assertTrue(self.index.status()["current"])

    def test_added_or_verified_memory_invalidates_index(self):
        self.add("Refund first")
        self.index.build()
        proposal = self.add("Refund proposal", status="proposed")
        self.assertEqual(len(self.index.search("возврат")), 1)
        self.store.memory_verify(proposal, "proposal checked")
        with self.assertRaisesRegex(SemanticError, "stale"):
            self.index.search("возврат")
        self.index.build()
        self.add("Invoice new")
        with self.assertRaisesRegex(SemanticError, "stale"):
            self.index.search("возврат")

    def test_model_change_requires_rebuild_and_reembeds_every_record(self):
        self.add("Refund one")
        self.index.build()
        self.encoder.fingerprint = "fixture-v2"
        with self.assertRaisesRegex(SemanticError, "model changed"):
            self.index.search("возврат")
        self.assertEqual(self.index.build()["embedded"], 1)
        self.assertEqual(len(self.index.search("возврат")), 1)

    def test_no_query_embedding_cache_and_index_survives_new_instance(self):
        self.add("Refund one")
        self.index.build()
        other = SemanticIndex(self.store, encoder=self.encoder)
        other.search("возврат")
        other.search("возврат")
        self.assertEqual([kind for kind, _ in self.encoder.calls].count("query"), 2)
        self.assertEqual([kind for kind, _ in self.encoder.calls].count("passage"), 1)

    def test_deterministic_ties_use_memory_id(self):
        ids = [self.add("Refund identical") for _ in range(5)]
        self.index.build()
        actual = [row["id"] for row in self.index.search("возврат")]
        self.assertEqual(actual, sorted(ids))
        self.assertEqual(actual, [row["id"] for row in self.index.search("возврат")])

    def test_corrupt_vector_is_rejected_even_after_in_process_cache_loaded(self):
        self.add("Refund one")
        self.index.build()
        self.index.search("возврат")
        with sqlite3.connect(self.index.db) as con:
            con.execute("UPDATE embeddings SET vector=?", (bytes(16),))
        with self.assertRaisesRegex(SemanticError, "corrupt"):
            self.index.search("возврат")

    def test_corrupt_sqlite_is_explicit_error(self):
        self.index.db.write_bytes(b"not a database")
        with self.assertRaisesRegex(SemanticError, "corrupt"):
            self.index.search("возврат")

    def test_unbuilt_or_unconfigured_model_does_not_fall_back(self):
        with self.assertRaisesRegex(SemanticError, "not built"):
            self.index.search("возврат")
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(SemanticError, "model directory"):
                SemanticIndex(self.store).build()

    def test_bad_query_embedding_is_rejected(self):
        self.add("Refund one")
        self.index.build()
        self.encoder.encode = lambda texts, **kwargs: [[0, 0, 0, 0]]
        with self.assertRaisesRegex(SemanticError, "zero embedding"):
            self.index.search("возврат")

    def test_empty_memory_index_is_explicitly_built_and_returns_no_records(self):
        stats = self.index.build()
        self.assertEqual(stats["records"], 0)
        self.assertEqual(self.index.search("возврат"), [])
        self.assertEqual(self.encoder.calls[-1][0], "query")

    def test_code_split_and_english_morphology_match_plain_queries(self):
        wanted = self.add("HTTPPaymentEvents.process_invoice", "calculate_order_totals")
        self.add("Unrelated document", "refund")
        self.index.build()
        self.assertEqual(self.index.lexical_search("invoices")[0]["id"], wanted)
        self.assertEqual(self.index.lexical_search("payment events")[0]["id"], wanted)
        self.assertEqual(self.index.lexical_search("orders totals")[0]["id"], wanted)
        self.assertEqual(normalize_code("HTTPPaymentGateway"), "http payment gateway")

    def test_russian_morphology_matches_comments_without_translation_dictionary(self):
        wanted = self.add("Обработка возвратов", "Проверка платежей")
        self.index.build()
        self.assertEqual(self.index.lexical_search("возврат платежами")[0]["id"], wanted)
        self.assertEqual(normalize_code("возврат платежей"), normalize_code("возвраты платежами"))

    def test_title_is_weighted_above_same_content_occurrence(self):
        title = self.add("Invoices", "generic body")
        self.add("Generic title", "invoices")
        self.index.build()
        self.assertEqual(self.index.lexical_search("invoice")[0]["id"], title)

    def test_tracked_source_diversity_caps_two_chunks_per_file(self):
        for line in range(1, 7):
            self.add("Refund chunk", source=f"source.txt:{line}")
        (self.project / "another.txt").write_text("another source")
        self.add("Refund other source", source="another.txt:1")
        external = [self.add("Refund external") for _ in range(3)]
        self.index.build()
        rows = self.index.search("возврат", limit=12)
        self.assertEqual(sum(row["source"].startswith("source.txt:") for row in rows), 2)
        self.assertEqual(sum(row["source"].startswith("another.txt:") for row in rows), 1)
        self.assertTrue(set(external).issubset({row["id"] for row in rows}))
        self.assertEqual(len(rows), 6)

    def test_legacy_embedding_cache_upgrade_reuses_all_embeddings(self):
        self.add("Refund one")
        self.index.build()
        with sqlite3.connect(self.index.db) as con:
            manifest = json.loads(con.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()[0])
            manifest.pop("lexical_version")
            manifest.pop("lexical_digest")
            con.execute("UPDATE meta SET value=? WHERE key='manifest'", (json.dumps(manifest),))
            con.execute("DROP TABLE code_fts")
        with self.assertRaisesRegex(SemanticError, "Code-aware index is not built"):
            self.index.search("возврат")
        stats = self.index.build()
        self.assertEqual(stats["embedded"], 0)
        self.assertEqual(stats["reused"], 1)
        self.assertEqual([kind for kind, _ in self.encoder.calls].count("passage"), 1)
        self.assertEqual(len(self.index.search("возврат")), 1)

    def test_corrupt_derived_lexical_content_is_rejected(self):
        self.add("Refund one")
        self.index.build()
        with sqlite3.connect(self.index.db) as con:
            con.execute("UPDATE code_fts SET title='corrupted'")
        with self.assertRaisesRegex(SemanticError, "lexical cache is corrupt"):
            self.index.search("возврат")

    def test_explicit_translation_variant_fills_a_lexical_gap(self):
        wanted = self.add("Invoice document")
        distractor = self.add("Status control")
        self.index.build()
        query = "счёт на оплату"
        self.assertEqual(self.index.lexical_search(query), [])
        self.assertEqual(self.index.search(query, limit=1)[0]["id"], distractor)
        self.assertEqual(self.index.search(query, variants=["invoice"], limit=1)[0]["id"], wanted)

    def test_query_variants_share_one_embedding_batch_and_are_not_cached(self):
        self.add("Invoice document")
        self.index.build()
        variants = ["invoice", "billing document"]
        self.index.search("счёт на оплату", variants=variants)
        self.index.search("счёт на оплату", variants=variants)
        queries = [texts for kind, texts in self.encoder.calls if kind == "query"]
        self.assertEqual(queries, [["счёт на оплату", *variants], ["счёт на оплату", *variants]])

    def test_variants_preserve_domain_proposal_and_source_freshness_checks(self):
        self.add("Invoice stale", source="source.txt:1")
        wanted = self.add("Invoice backend")
        self.add("Invoice frontend", domain="frontend")
        self.add("Invoice proposal", status="proposed")
        self.index.build()
        (self.project / "source.txt").write_text("modified source")
        rows = self.index.search("счёт", domain="backend", variants=["invoice"])
        self.assertEqual([row["id"] for row in rows], [wanted])

    def test_variant_bounds_are_explicit_validation_errors(self):
        bad = ["invoice", ["one", "two", "three"], [""], ["   "], [None], [42], ["a" * 501]]
        for variants in bad:
            with self.subTest(variants=variants):
                with self.assertRaisesRegex(SemanticError, "variant"):
                    self.index.search("query", variants=variants)

    def test_empty_variants_preserve_default_results_exactly(self):
        self.add("Invoice document")
        self.add("Refund payments")
        self.index.build()
        default = self.index.search("invoice")
        self.assertEqual(default, self.index.search("invoice", variants=[]))
        self.assertEqual(default, self.index.search("invoice", variants=None))

    def test_exact_original_identifier_keeps_priority_with_different_variant(self):
        wanted = self.add("RefundService.process_refund")
        self.add("InvoiceService.generate_invoice")
        self.index.build()
        rows = self.index.search("RefundService.process_refund", variants=["InvoiceService.generate_invoice"])
        self.assertEqual(rows[0]["id"], wanted)

    def test_repeated_variant_cannot_multiply_its_rank_contribution(self):
        self.add("Invoice document")
        self.add("Status control")
        self.index.build()
        once = self.index.search("счёт", variants=["invoice"])
        twice = self.index.search("счёт", variants=["invoice", "invoice"])
        self.assertEqual(once, twice)

    def test_wrong_query_dimensions_are_rejected(self):
        self.add("Refund one")
        self.index.build()
        self.encoder.encode = lambda texts, **kwargs: [[1, 0]]
        with self.assertRaisesRegex(SemanticError, "dimensions"):
            self.index.search("возврат")


class ModelArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "onnx").mkdir()
        (self.root / "onnx/model_quantized.onnx").write_bytes(b"test model bytes")
        (self.root / "tokenizer.json").write_text("{}")

    def test_artifact_hash_change_changes_encoder_fingerprint_without_loading_model(self):
        encoder = OnnxEncoder(self.root)
        original = encoder.fingerprint
        (self.root / "tokenizer.json").write_text('{"modified":true}')
        self.assertNotEqual(encoder.fingerprint, original)

    def test_declared_hash_mismatch_is_rejected(self):
        (self.root / "manifest.json").write_text(json.dumps({"files": {"tokenizer.json": "0" * 64}}))
        with self.assertRaisesRegex(SemanticError, "hash mismatch"):
            OnnxEncoder(self.root)

    def test_missing_artifact_is_explicit_error_without_network(self):
        (self.root / "tokenizer.json").unlink()
        with self.assertRaisesRegex(SemanticError, "Missing local model"):
            OnnxEncoder(self.root)


if __name__ == "__main__":
    unittest.main()
