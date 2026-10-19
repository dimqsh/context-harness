"""Output vault contracts exercised with real SQLite FTS and exact UTF-8 budgets."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from context_harness.store import Store
from context_harness.vault import MAX_OUTPUT_BYTES, OutputVault


def size(value):
    return len((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.store = Store(self.root / 'state')
        self.store.init(self.project)
        self.vault = OutputVault(self.store)

    def write(self, text, name='build.log'):
        path = self.project / name
        path.write_bytes(text.encode('utf-8'))
        return path

    def test_error_tail_beyond_thousand_lines_exact_and_bounded(self):
        text = ''.join(f'normal progress {i}\n' for i in range(1200))
        text += 'ERROR PaymentService.retry_refund: повторный возврат 🚀\r\nlast evidence\n'
        receipt = self.vault.ingest(self.write(text), 'Build log')
        result = self.vault.search('PaymentService.retry_refund', budget_bytes=900)
        self.assertLessEqual(size(result), 900)
        hit = result['results'][0]
        self.assertEqual(hit['start_line'], 1201)
        self.assertIn('PaymentService.retry_refund: повторный возврат 🚀\r\n', hit['text'])
        self.assertEqual(receipt['sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertNotIn('normal progress', json.dumps(receipt))

    def test_pagination_reconstructs_exact_unicode_bytes(self):
        text = ''.join('Строка 🚀 %d\r\n' % i for i in range(100))
        receipt = self.vault.ingest(self.write(text))
        cursor, pieces = 1, []
        while cursor is not None:
            page = self.vault.read(receipt['output_id'], cursor, max_lines=9, budget_bytes=700)
            self.assertLessEqual(size(page), 700)
            pieces.append(page['text'])
            self.assertEqual(page['next_line'], page['end_line'] + 1 if page['truncated'] else None)
            cursor = page['next_line']
        self.assertEqual(''.join(pieces), text)

    def test_dedup_is_project_bound_and_retains_initial_snapshot(self):
        path = self.write('ERROR foo\n')
        first = self.vault.ingest(path)
        self.assertTrue(self.vault.ingest(path)['deduplicated'])
        with self.store.connect() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM vault_outputs').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM vault_chunks').fetchone()[0], 1)
        path.write_text('changed')
        page = self.vault.read(first['output_id'])
        self.assertEqual(page['text'], 'ERROR foo\n')
        self.assertEqual(page['source_status'], 'changed')
        other = Store(self.root / 'other-state')
        other.init(self.project)
        self.assertEqual(OutputVault(other).search('ERROR')['results'], [])
        with self.assertRaises(ValueError):
            OutputVault(other).read(first['output_id'])

    def test_missing_or_corrupt_blob_fails_in_both_read_and_search(self):
        receipt = self.vault.ingest(self.write('ERROR important\n'))
        blob = self.vault.blobs / receipt['sha256']
        original = blob.read_bytes()
        for bad in (b'corruption', None):
            if bad is None:
                blob.unlink()
            else:
                blob.write_bytes(bad)
            with self.assertRaises(ValueError):
                self.vault.read(receipt['output_id'])
            with self.assertRaises(ValueError):
                self.vault.search('ERROR')
            if bad is not None:
                with self.assertRaises(ValueError):
                    self.vault.ingest(self.project / 'build.log')
            blob.write_bytes(original)

    def test_rejects_symlink_secrets_binary_and_oversize(self):
        good = self.write('allowed')
        link = self.project / 'link.log'
        link.symlink_to(good)
        for path in [link, self.write('x', '.env'), self.write('x', '.env.example'),
                     self.write('x', 'credentials.json'), self.write('x', 'private.pem')]:
            with self.assertRaises(ValueError):
                self.vault.ingest(path)
        bad = self.project / 'binary.log'
        for data in (b'\xff', b'a\x00b'):
            bad.write_bytes(data)
            with self.assertRaises(ValueError):
                self.vault.ingest(bad)
        with bad.open('wb') as handle:
            handle.truncate(MAX_OUTPUT_BYTES + 1)
        with self.assertRaises(ValueError):
            self.vault.ingest(bad)

    def test_sql_fts_punctuation_and_nonmutating_queries(self):
        self.vault.ingest(self.write('important ERROR SELECT\n'))
        for query in ['" OR * - NEAR(', "'; DROP TABLE vault_outputs; --", '🚀', '']:
            result = self.vault.search(query, budget_bytes=700)
            self.assertLessEqual(size(result), 700)
        self.assertEqual(self.vault.search('important')['matched_chunks'], 1)

    def test_huge_lines_never_silently_shortened_and_minimum_budget(self):
        receipt = self.vault.ingest(self.write('ERROR ' + 'я' * 10000))
        with self.assertRaisesRegex(ValueError, 'line exceeds'):
            self.vault.read(receipt['output_id'], budget_bytes=700)
        with self.assertRaisesRegex(ValueError, 'lines exceed'):
            self.vault.search('ERROR', budget_bytes=700)
        for budget in [0, 511, True]:
            with self.assertRaises(ValueError):
                self.vault.search('ERROR', budget_bytes=budget)
        self.assertLessEqual(size(self.vault.read(receipt['output_id'], budget_bytes=22000)), 22000)

    def test_empty_output_and_read_bounds(self):
        receipt = self.vault.ingest(self.write(''))
        result = self.vault.read(receipt['output_id'])
        self.assertEqual(result['text'], '')
        self.assertIsNone(result['next_line'])
        self.assertFalse(result['truncated'])
        for start in [0, 2, True]:
            with self.assertRaises(ValueError):
                self.vault.read(receipt['output_id'], start_line=start)

    def test_search_shared_budget_omissions_and_unknown_ids(self):
        for i in range(5):
            self.vault.ingest(self.write('ERROR ' + str(i) + '\n', f'log-{i}.txt'))
        result = self.vault.search('ERROR', limit=5, budget_bytes=700)
        self.assertLessEqual(size(result), 700)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['omitted_matches'], 5 - len(result['results']))
        with self.assertRaises(ValueError):
            self.vault.read('../bad')


if __name__ == '__main__':
    unittest.main()
