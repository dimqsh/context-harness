"""Portable exports are inert; the loopback server exposes only task-scoped reads."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from context_harness.dashboard import build_snapshot, export_dashboard, make_server, render_html
from context_harness.store import Store


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / 'project'
        self.project.mkdir()
        self.store = Store(self.base / 'state')
        self.store.init(self.project)
        self.task = self.store.task_create('Контекст команды', 'Найти нужные факты', ['Проверить результат'])['id']

    def embedded(self, html):
        return json.loads(re.search(r'<script id="harness-data" type="application/json">(.*?)</script>',
                                    html, flags=re.S).group(1))

    def test_snapshot_is_task_scoped_and_counts_current_memory(self):
        other = self.store.task_create('Другая задача', 'Не показывать её', ['Скрыто'])['id']
        self.store.memory_add('Общее правило', 'Контекст', 'general', 'notes',
                              'verified', 'Проверено вручную')
        self.store.memory_add('Предложение', 'Ещё не проверено', 'general', 'notes')
        self.store.search('Контекст', backend='lexical', task_id=other, agent='other')
        self.store.search('Контекст', backend='lexical', task_id=self.task, agent='worker')
        # Counting memory must not hash each source or execute another retrieval.
        with patch.object(self.store, 'source_hash', side_effect=AssertionError('No source scan')):
            snapshot = build_snapshot(self.store, self.task)
        self.assertEqual(snapshot['memory_stats'], {'total': 2, 'verified': 1, 'proposed': 1})
        self.assertTrue(all(e['task_id'] == self.task for e in snapshot['events']))
        self.assertNotIn('other', [e['data'].get('agent') for e in snapshot['events']])
        self.assertEqual(snapshot['project'], str(self.project))
        self.assertEqual(snapshot['state'], str(self.store.root))
        self.assertIn('updated_at', snapshot)
        self.assertFalse(snapshot['events'][-1]['data']['api_called'])

    def test_script_breakout_and_html_payload_are_inert(self):
        attack = '</script><script>alert("unsafe")</script><img src=x onerror=alert(1)>\u2028&'
        snapshot = build_snapshot(self.store, self.task)
        snapshot['task']['title'] = attack
        snapshot['events'][0]['data']['title'] = attack
        html = render_html(snapshot)
        self.assertNotIn(attack, html)
        self.assertNotIn('<script>alert(', html)
        self.assertNotIn('<img src=x', html)
        self.assertEqual(self.embedded(html)['snapshot']['task']['title'], attack)
        self.assertNotIn('.innerHTML', html)
        self.assertNotIn('src="https://', html)
        self.assertNotIn('href="https://', html)

    def test_export_keeps_every_event_and_disables_polling(self):
        for i in range(170):
            with self.store.connect() as con:
                self.store.emit(con, 'agent_progress', {'agent': 'worker', 'stage': 'running',
                                                       'summary': str(i)}, self.task)
        output = self.base / 'nested' / 'dashboard.html'
        result = export_dashboard(self.store, self.task, output)
        embedded = self.embedded(output.read_text(encoding='utf-8'))
        self.assertEqual(result['events'], 171)
        self.assertEqual(len(embedded['snapshot']['events']), 171)
        self.assertFalse(embedded['live'])
        self.assertFalse(result['live'])
        self.assertEqual(result['output'], str(output))

    def test_server_is_loopback_read_only_and_updates_snapshot(self):
        server = make_server(self.store, self.task)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(server.server_address[0], '127.0.0.1')
        base_url = 'http://127.0.0.1:' + str(server.server_port)
        with urlopen(base_url + '/', timeout=3) as response:
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn('text/html', response.headers['Content-Type'])
            page = self.embedded(response.read().decode())
        self.assertTrue(page['live'])
        with self.store.connect() as con:
            self.store.emit(con, 'agent_progress', {'agent': 'worker', 'stage': 'running',
                                                   'summary': 'Новая запись'}, self.task)
        with urlopen(base_url + '/api/snapshot', timeout=3) as response:
            self.assertIn('application/json', response.headers['Content-Type'])
            snapshot = json.load(response)
        self.assertEqual(snapshot['events'][-1]['data']['summary'], 'Новая запись')
        before = self.store.events(self.task)
        for route in ('/state.sqlite3', '/../../etc/passwd', '/api/snapshot?task=other', '/missing'):
            with self.subTest(route=route), self.assertRaises(HTTPError) as error:
                urlopen(base_url + route, timeout=3)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        self.assertEqual(before, self.store.events(self.task))
        for host in ('untrusted.example:8765', '127.0.0.1:1', 'localhost.evil.test'):
            with self.subTest(host=host), self.assertRaises(HTTPError) as error:
                urlopen(Request(base_url + '/api/snapshot', headers={'Host': host}), timeout=3)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()

    @unittest.skipUnless(shutil.which('node'), 'Node.js optional for replay-state checks')
    def test_replay_never_invents_running_and_invalidates_old_verification(self):
        html = render_html(build_snapshot(self.store, self.task))
        code = html.split("const labels = ", 1)[1].split("function render(){", 1)[0]
        code = "const labels = " + code
        assertions = r"""
const assert = require('node:assert/strict');
const assigned = {type:'agent_assigned',data:{agent:'worker',domain:'backend',backend:'lexical'}};
const reported = {type:'agent_reported',data:{agent:'worker',status:'completed',summary:'Done'}};
const verified = {type:'coordinator_verified',data:{passed:true,summary:'Checked'}};
assert.equal(derive([]).agents.size,0);
assert.equal(derive([assigned]).agents.get('worker').stage,'assigned');
assert.equal(derive([assigned,reported]).agents.get('worker').stage,'completed');
assert.equal(derive([assigned,reported]).verification,null);
assert.equal(derive([assigned,reported]).apiCalls,0);
assert.equal(derive([assigned,reported,verified,reported]).verification.stale,true);
const search = {type:'search_started',data:{agent:'worker',query:'refund'}};
assert.equal(derive([search]).agents.get('worker').stage,'searching');
const running = {type:'agent_progress',data:{agent:'worker',stage:'running',summary:'Started'}};
assert.equal(derive([assigned,running]).agents.get('worker').stage,'running');
assert.equal(derive([assigned,running,reported]).received,1);
const shortlisted = {type:'context_shortlisted',data:{agent:'worker',candidates:[]}};
const failure = {type:'search_failed',data:{agent:'worker',error_type:'MissingAPIKeyError'}};
assert.equal(derive([search,shortlisted,failure]).agents.get('worker').stage,'blocked');
assert.equal(derive([search,shortlisted,failure]).memory.type,'search_failed');
assert.equal(derive([search,shortlisted,failure]).apiCalls,0);
assert.equal(derive([search,shortlisted,failure]).apiUnconfirmed,false);
const transportFailure = {type:'search_failed',data:{agent:'worker',backend:'decisions',error_type:'DecisionError'}};
assert.equal(derive([search,transportFailure]).apiUnconfirmed,true);
const localFailure = {type:'search_failed',data:{agent:'worker',backend:'hybrid',error_type:'SemanticError',phase:'retrieval'}};
assert.equal(derive([search,localFailure]).apiUnconfirmed,false);
const candidateFailure = {...localFailure,data:{...localFailure.data,backend:'decisions'}};
assert.equal(derive([search,candidateFailure]).apiUnconfirmed,false);
const snap = {type:'tests_snapshot',data:{files:3}};
const failRun = {type:'check_run',data:{agent:'worker',kind:'test',command:'npm test',exit_code:1,duration_ms:40,failed_tests:['a > b'],override:null}};
const fullRun = {type:'check_run',data:{agent:'worker',kind:'full',command:'npm run check',exit_code:0,duration_ms:900,failed_tests:[],override:'new idea'}};
const refused = {type:'check_refused',data:{agent:'worker',kind:'test',command:'npm test',reasons:['Stop rule']}};
const checked = derive([assigned,snap,failRun,fullRun,refused]);
assert.equal(checked.snapshotFiles,3);
assert.deepEqual(checked.checks.map(c=>c.status),['fail','pass','refused']);
assert.equal(checked.green,2);
assert.equal(checked.refused,1);
assert.equal(checked.overrides,1);
assert.equal(checked.agents.get('worker').checks,2);
assert.equal(checked.agents.get('worker').stage,'assigned');
assert.equal(eventTone(failRun),'fail');
assert.equal(eventTone(refused),'warn');
assert.equal(eventTitle(fullRun),'Check passed · full · override');
assert.match(eventText(refused),/Stop rule/);
const timeout = {type:'check_run',data:{kind:'test',command:'x',exit_code:124}};
assert.equal(derive([timeout]).checks[0].status,'timeout');
"""
        result = subprocess.run([shutil.which('node'), '-e', code + assertions],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_snapshot_carries_run_budget_limits_when_set(self):
        self.assertEqual(build_snapshot(self.store, self.task)['budgets'], {})
        self.store.assign(self.task, 'worker', 'backend', 'Goal', ['src'], 'Контекст', backend='lexical',
                          budget={'max_runs': 5})
        budgets = build_snapshot(self.store, self.task)['budgets']
        self.assertEqual(budgets['worker']['max_runs'], 5)
        self.assertEqual(budgets['worker']['max_after_green'], 1)

    def test_unknown_task_rejected_before_server_binds(self):
        with self.assertRaises(ValueError):
            make_server(self.store, 'unknown')


if __name__ == '__main__':
    unittest.main()
