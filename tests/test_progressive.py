"""Independent v06 acceptance: preserve facts/contracts while disclosing less data."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from context_harness.cli import main
from context_harness.context import search_view
from context_harness.store import Store


def wire_bytes(value):
    return len((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


class ProgressiveAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.source = self.project / 'policy.txt'
        self.source.write_text('Refund means возврат; retry_refund must remain idempotent. 🚀', encoding='utf-8')
        self.store = Store(self.root / 'state')
        self.store.init(self.project)

    def add(self, title='Refund policy', content='Не возвращать деньги дважды. retry_refund 🚀',
            domain='payments', status='verified'):
        return self.store.memory_add(title, content, domain, 'policy.txt:1', status,
                                     'Read policy.txt' if status == 'verified' else '')['id']

    def task(self):
        return self.store.task_create('Исправить возврат', 'Не менять денежный контракт 🚀',
                                      ['Повторный запрос не создаёт платёж', 'Сохранить права кассира'])['id']

    def assign(self, tid, agent='worker', paths=None, **kwargs):
        return self.store.assign(tid, agent, 'payments', 'Проверить повторный возврат',
                                 ['src/payments'] if paths is None else paths, 'refund',
                                 backend='lexical', **kwargs)

    def call(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['--state', str(self.store.root), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def completed(self):
        tid = self.task()
        self.assign(tid)
        artifact = self.project / 'result.txt'
        artifact.write_text('Executed result', encoding='utf-8')
        self.store.report(tid, 'worker', 'completed', 'Fixture result ready', ['result.txt'], ['Fixture check executed'])
        return tid, artifact

    def test_index_to_selected_detail_preserves_exact_content(self):
        wanted = self.add()
        other = self.add('Other refund', content='Other body is not needed')
        code, out, _ = self.call('search', 'refund', '--backend', 'lexical', '--view', 'index')
        self.assertEqual(code, 0)
        index = json.loads(out)
        self.assertEqual(index['view'], 'index')
        self.assertEqual({r['id'] for r in index['records']}, {wanted, other})
        for row in index['records']:
            self.assertNotIn('content', row)
            self.assertNotIn('evidence', row)
            self.assertEqual(row['source'], 'policy.txt:1')
        code, out, _ = self.call('memory-read', wanted)
        self.assertEqual(code, 0)
        details = json.loads(out)
        self.assertEqual([r['id'] for r in details['records']], [wanted])
        self.assertEqual(details['records'][0]['content'], 'Не возвращать деньги дважды. retry_refund 🚀')
        self.assertEqual(details['records'][0]['evidence'], 'Read policy.txt')
        self.assertTrue(details['untrusted_data'])

    def test_selected_detail_batch_is_all_or_nothing_for_unavailable_records(self):
        good = self.add()
        proposed = self.add('Proposed refund', status='proposed')
        for bad in [proposed, 'm_missing']:
            code, out, err = self.call('memory-read', good, bad)
            self.assertEqual(code, 2)
            self.assertEqual(out, '')
            self.assertIn('not verified', err)
        self.source.write_text('Changed policy', encoding='utf-8')
        code, out, err = self.call('memory-read', good)
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('source changed', err)
        self.assertEqual(self.store.search('refund', backend='lexical')['records'], [])

    def test_selected_detail_keeps_domain_scope_and_project_state(self):
        wanted = self.add()
        with self.assertRaisesRegex(ValueError, 'domain'):
            self.store.memory_read([wanted], domain='frontend')
        general = self.add('General refund', domain='general')
        self.assertEqual(self.store.memory_read([general], domain='frontend')['records'][0]['id'], general)
        other = Store(self.root / 'other-state')
        other.init(self.project)
        with self.assertRaises(ValueError):
            other.memory_read([wanted])

    def test_cli_unicode_budget_includes_metadata_and_newline(self):
        for i in range(8):
            self.add('Возврат 🚀 ' * 8 + str(i), content=('Данные 🚀 ' * 70) + str(i))
        code, out, err = self.call('search', 'возврат', '--backend', 'lexical', '--view', 'index', '--limit', '8', '--budget-bytes', '1900')
        self.assertEqual((code, err), (0, ''))
        page = json.loads(out)
        self.assertLessEqual(len(out.encode('utf-8')), 1900)
        self.assertEqual(len(out.encode('utf-8')), wire_bytes(page))
        self.assertGreater(page['returned'], 0)
        self.assertLess(page['returned'], page['total'])
        self.assertIsNotNone(page['next_offset'])
        self.assertEqual(page['budget_unit'], 'UTF-8 bytes, not model tokens')
        seen = []
        offset = 0
        while offset is not None:
            result = search_view(self.store.search('возврат', backend='lexical', limit=8),
                                 budget_bytes=1900, offset=offset)
            self.assertLessEqual(wire_bytes(result), 1900)
            seen += [r['id'] for r in result['records']]
            offset = result['next_offset']
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 8)

    def test_full_compatibility_keeps_exact_fact_and_evidence(self):
        wanted = self.add()
        code, out, _ = self.call('search', 'refund', '--backend', 'lexical', '--view', 'full')
        self.assertEqual(code, 0)
        record = json.loads(out)['records'][0]
        self.assertEqual(record['id'], wanted)
        self.assertEqual(record['content'], self.store.memory_read([wanted])['records'][0]['content'])
        self.assertEqual(record['evidence'], 'Read policy.txt')

    def test_large_required_metadata_and_large_whole_record_fail_explicitly(self):
        self.add(title='Refund ' + 'я' * 490)
        code, out, err = self.call('search', 'refund', '--backend', 'lexical', '--budget-bytes', '600')
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('exceeds', err)
        self.add(title='Refund massive', content='🚀' * 1900)
        code, out, err = self.call('search', 'refund', '--backend', 'lexical', '--view', 'full', '--budget-bytes', '1100')
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('exceeds', err)

    def test_packet_budget_preserves_acceptance_scope_and_discloses_omission(self):
        for i in range(8):
            self.add('Refund ' + ('я🚀' * 80) + str(i))
        tid = self.task()
        assignment = self.assign(tid, budget_bytes=2400)
        content = Path(assignment['packet']).read_bytes()
        packet = json.loads(content)
        self.assertLessEqual(len(content), 2400)
        self.assertEqual(assignment['packet_bytes'], len(content))
        self.assertEqual(packet['task']['acceptance'], self.store.task(tid)['acceptance'])
        self.assertEqual(packet['task']['goal'], self.store.task(tid)['goal'])
        self.assertEqual(packet['allow_paths'], ['src/payments'])
        self.assertFalse(packet['read_only'])
        self.assertTrue(packet['protocol'])
        self.assertGreater(packet['context_omitted'], 0)
        self.assertEqual(packet['context_total'], len(packet['memory_evidence']) + packet['context_omitted'])
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            self.assign(tid, agent='conflict', paths=['src'])
        readonly = self.assign(tid, agent='reviewer', paths=[])
        self.assertTrue(json.loads(Path(readonly['packet']).read_text())['read_only'])

    def test_too_small_packet_never_acquires_ownership(self):
        tid = self.task()
        with self.assertRaises(ValueError):
            self.assign(tid, budget_bytes=512)
        self.assertEqual(self.store.summary(tid)['assigned'], 0)
        self.assertFalse((self.store.root / 'packets' / tid / 'worker.json').exists())

    def test_checkpoint_has_latest_report_without_replaying_history(self):
        tid, artifact = self.completed()
        for i in range(15):
            self.store.progress(tid, 'worker', 'running', 'HISTORIC_VERBOSE_EVENT_' + str(i))
        self.store.report(tid, 'worker', 'completed', 'Latest checked artifact', ['result.txt'], ['Final fixture'])
        self.store.verify(tid, True, 'Independent fixture pass', ['Executed independent acceptance'])
        checkpoint = self.store.checkpoint(tid, 6000)
        serialized = json.dumps(checkpoint, ensure_ascii=False)
        self.assertTrue(checkpoint['verified'])
        self.assertIn('Latest checked artifact', serialized)
        self.assertIn('Independent fixture pass', serialized)
        self.assertNotIn('HISTORIC_VERBOSE_EVENT_', serialized)
        self.assertEqual(checkpoint['agents'][0]['paths'], ['src/payments'])
        self.assertLessEqual(wire_bytes(checkpoint), 6000)
        artifact.write_text('Changed since acceptance')
        stale = self.store.checkpoint(tid)
        self.assertFalse(stale['verified'])
        self.assertTrue(stale['verification']['stale'])

    def test_refinement_requires_failed_current_report_and_is_bounded(self):
        tid, _ = self.completed()
        with self.assertRaisesRegex(ValueError, 'failed verification'):
            self.store.refine_plan(tid)
        self.store.verify(tid, False, 'Independent check found focus bug', ['test_focus failed'])
        plan = self.store.refine_plan(tid, max_probes=2, token_budget=1234)
        self.assertFalse(plan['model_started'])
        self.assertEqual(plan['declared_token_budget'], 1234)
        self.assertIn('focus bug', plan['failure'])
        self.assertFalse(self.store.summary(tid)['verified'])
        self.assertEqual(self.store.refine_plan(tid, max_probes=2)['probe'], 2)
        with self.assertRaisesRegex(ValueError, 'limit'):
            self.store.refine_plan(tid, max_probes=2)
        self.store.report(tid, 'worker', 'completed', 'Changed result', ['result.txt'], ['Another fixture'])
        with self.assertRaisesRegex(ValueError, 'Reports changed'):
            self.store.refine_plan(tid)
        for probes, tokens in [(3, 100), (0, 100), (1, 0), (1, 100001)]:
            with self.assertRaises(ValueError):
                self.store.refine_plan(tid, probes, tokens)

    def test_lesson_requires_current_acceptance_and_stays_proposed(self):
        tid, artifact = self.completed()
        with self.assertRaises(ValueError):
            self.store.lesson_propose(tid, 'Refund lesson', 'Preserve retry contract', 'payments', 'policy.txt')
        self.store.verify(tid, False, 'No pass yet', ['Check failed'])
        with self.assertRaises(ValueError):
            self.store.lesson_propose(tid, 'Refund lesson', 'Preserve retry contract', 'payments', 'policy.txt')
        self.store.verify(tid, True, 'Independent pass', ['Regression executed'])
        lesson = self.store.lesson_propose(tid, 'Refund lesson', 'Preserve retry contract', 'payments', 'policy.txt')
        self.assertEqual(lesson['status'], 'proposed')
        self.assertEqual(self.store.shortlist('Refund lesson'), [])
        with self.assertRaises(ValueError):
            self.store.refine_plan(tid)
        artifact.write_text('Changed result')
        with self.assertRaises(ValueError):
            self.store.lesson_propose(tid, 'Refund stale', 'Preserve retry contract', 'payments', 'policy.txt')


if __name__ == '__main__':
    unittest.main()
