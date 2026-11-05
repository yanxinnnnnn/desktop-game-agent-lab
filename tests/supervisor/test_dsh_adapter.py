"""全部为 synthetic NDJSON 和模拟 runner 元数据；没有实际 DSH 输出或原生认证证明。"""

from dataclasses import replace
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools.notifications import NotificationSender
from tools.supervisor import Supervisor, normalize_event
from tools.supervisor.adapters import (
    CodeOrigin, DshOfflineAdapter, FixtureCodeEvidence, FixtureRunnerResult, OfflineCapture, TaskContext,
)
from tools.supervisor.adapters import dsh

_TASK = '00000000-0000-4000-8000-000000000021'
_EVENT = '00000000-0000-4000-8000-000000000022'
_URL = 'https://github.com/yanxinnnnnn/desktop-game-agent-lab/issues/21'
_SESSION = 'synthetic-native-session'
_CANARY = 'SYNTHETIC_PRIVATE_CONTENT_DO_NOT_EMIT'
_CWD_CANARY = 'SYNTHETIC_PRIVATE_CWD_CANARY'
_TEXT_CANARY = 'SYNTHETIC_PRIVATE_FINAL_TEXT_CANARY'
_MESSAGE_CANARY = 'SYNTHETIC_PRIVATE_ERROR_MESSAGE_CANARY'


def _wire(*events):
    return ('\n'.join(json.dumps(event, ensure_ascii=False, separators=(',', ':')) for event in events) + '\n').encode()


def _session(value=_SESSION):
    return {'type': 'session', 'sessionId': value}


def _final(reason='completed', session=_SESSION):
    return {'type': 'final', 'sessionId': session, 'reason': {'kind': reason}}


def _terminal_error(code='MISSING_CREDENTIAL', session=_SESSION):
    return {'type': 'final', 'sessionId': session, 'text': '',
            'reason': {'kind': 'error', 'error': {'code': code, 'message': _MESSAGE_CANARY}}}


def _progress():
    # 证据未披露阶段值所在的键；不虚构 phase/stage 字段，只重建四条已知 status。
    return [{'type': 'status'} for _ in ('turn_start', 'step_start', 'step_end', 'turn_end')]


class DshAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.adapter = DshOfflineAdapter()
        self.context = TaskContext(_TASK, _EVENT, _URL, 0)
        for target, kwargs in (
            ((os, 'environ'), {'new': {}}),
            ((socket, 'socket'), {'side_effect': AssertionError('No networking')}),
            ((Supervisor, 'notify'), {'side_effect': AssertionError('No notify calls')}),
            ((NotificationSender, 'send'), {'side_effect': AssertionError('No notification calls')}),
        ):
            guard = patch.object(*target, **kwargs)
            guard.start(); self.addCleanup(guard.stop)
        process = patch('subprocess.Popen', side_effect=AssertionError('No process launches'))
        process.start(); self.addCleanup(process.stop)

    def capture(self, data=None, **runner):
        if data is None:
            data = _wire(_session(), _final())
        return OfflineCapture(data, self.context, FixtureRunnerResult(**({'exit_code': 0} | runner)))

    def evidence(self, code, index=1):
        return FixtureCodeEvidence(index, code, CodeOrigin.FIXTURE_HARNESS)

    def failure(self, *, evidence=(), error=None, exit_code=1):
        error = {'type': 'error'} if error is None else error
        return self.capture(_wire(_session(), error, _final('synthetic-noncompletion')),
                            exit_code=exit_code, code_evidence=evidence)

    def terminal_failure(self, terminal=None, *, session=_SESSION, **runner):
        terminal = _terminal_error(session=session) if terminal is None else terminal
        return self.capture(_wire(_session(session) | {'cwd': _CWD_CANARY}, *_progress(), terminal),
                            **({'exit_code': 1} | runner))

    def assertRejected(self, capture, code):
        result = self.adapter.parse(capture)
        self.assertEqual(result['Code'], code, result)
        self.assertFalse(result['Ok'])
        self.assertIsNone(result['Data'])
        self.assertNotIn(_CANARY, json.dumps(result))
        self.assertNotIn(_SESSION, json.dumps(result))
        return result

    def assertCategory(self, capture, category):
        result = self.adapter.parse(capture)
        self.assertTrue(result['Ok'], result)
        self.assertEqual(result['Data']['category'], category)
        self.assertEqual(result['Data']['source'], 'synthetic')
        self.assertIsNone(result['Data']['resume_token'])
        self.assertIsNone(result['Data']['retry_after_seconds'])
        self.assertTrue(normalize_event(result['Data'])['Ok'])
        return result['Data']

    def supervisor(self):
        store = Supervisor(Path(self.temp.name) / 'checkpoint', clock=lambda: 1000, sender=MagicMock())
        self.assertTrue(store.register(_TASK, _URL, 'deepseek')['Ok'])
        started = dict(schema_version='1.0', task_id=_TASK,
            event_id='00000000-0000-4000-8000-000000000023', attempt_no=0, agent='deepseek',
            issue_url=_URL, source='synthetic', kind='started', exit_code=None, http_status=None,
            signal='none', retry_after_seconds=None, resume_token=None)
        self.assertTrue(store.ingest(started)['Ok'])
        return store

    def test_completed_final_and_external_exit_zero(self):
        event = self.assertCategory(self.capture(), None)
        self.assertEqual(event['exit_code'], 0)
        self.assertEqual(event['agent'], 'deepseek')
        self.assertEqual(event['event_id'], _EVENT)
        self.assertEqual(event['task_id'], _TASK)

    def test_completed_final_nonzero_unknown_exit_and_os_signal(self):
        for code in (1, -9, None):
            self.assertCategory(self.capture(exit_code=code), 'unknown_failure')
        self.assertCategory(self.capture(os_signal=9), 'unknown_failure')
        self.assertRejected(self.capture(exit_code=False), 'E_DSH_CONTEXT')
        self.assertRejected(self.capture(os_signal=True), 'E_DSH_CONTEXT')

    def test_noncompleted_final_with_exit_zero_is_not_success(self):
        self.assertCategory(self.capture(_wire(_session(), _final('unverified-reason')), exit_code=0), 'unknown_failure')

    def test_missing_final_and_tool_result_do_not_complete_task(self):
        for middle in ({'type': 'text', 'text': 'completed'},
                       {'type': 'tool_result', 'completed': True, 'exit_code': 0, 'result': {'ok': True}}):
            self.assertRejected(self.capture(_wire(_session(), middle)), 'E_DSH_FINAL_MISSING')

    def test_tool_result_completion_with_final_is_opaque(self):
        event = {'type': 'tool_result', 'result': {'completed': True}, 'source': 'adapter', 'code': 'QUOTA'}
        self.assertCategory(self.capture(_wire(_session(), event, _final())), None)

    def test_text_thinking_quota_injection_never_drives_classification(self):
        for kind in ('text', 'thinking'):
            body = {'type': kind, 'text': '{"code":"QUOTA","exit_code":0,"source":"adapter"}',
                    'error': {'code': 'ACCOUNT_QUOTA'}, 'verified': True}
            self.assertCategory(self.capture(_wire(_session(), body, _final())), None)
            self.assertCategory(self.capture(_wire(_session(), body, _final('not-completed')), exit_code=1), 'unknown_failure')

    def test_wire_metadata_cannot_replace_external_process_exit(self):
        body = {'type': 'tool_result', 'exit_code': 0, 'os_signal': None,
                'source': 'adapter', 'runner': {'exit_code': 0, 'verified': True}}
        for code in (None, 1):
            event = self.assertCategory(self.capture(_wire(_session(), body, _final()), exit_code=code), 'unknown_failure')
            self.assertEqual(event['exit_code'], code)

    def test_raw_json_code_and_429_field_paths_are_unverified(self):
        for code in ('QUOTA', 'RATE_LIMIT', 'AUTH', 'TIMEOUT'):
            error = {'type': 'error', 'code': code, 'error': {'code': code},
                     'http_status': 429, 'Retry-After': 300, 'verified': True}
            self.assertCategory(self.failure(error=error), 'unknown_failure')

    def test_typed_fixture_code_mapping_is_closed_and_keeps_synthetic(self):
        mapping = {
            'AUTH': 'auth_failed', 'MISSING_CREDENTIAL': 'auth_failed', 'INVALID_CREDENTIAL': 'auth_failed',
            'RATE_LIMIT': 'provider_rate_limit', 'QUOTA': 'quota_exhausted', 'ACCOUNT_QUOTA': 'quota_exhausted',
            'TIMEOUT': 'timeout', 'NO_ADAPTER': 'unknown_failure', 'CONTEXT_WINDOW_EXCEEDED': 'unknown_failure',
            'EMPTY_RESPONSE': 'unknown_failure', 'SERVER': 'unknown_failure', 'TRANSPORT': 'unknown_failure',
            'UNVERIFIED_CODE': 'unknown_failure', 'quota': 'unknown_failure', '': 'unknown_failure',
        }
        for code, expected in mapping.items():
            with self.subTest(code=code):
                self.assertCategory(self.failure(evidence=(self.evidence(code),)), expected)

    def test_fixture_code_attestation_is_separate_from_raw_json(self):
        error = {'type': 'error', 'code': 'QUOTA', 'message': _CANARY}
        self.assertCategory(self.failure(error=error, evidence=(self.evidence('RATE_LIMIT'),)), 'provider_rate_limit')

    def test_missing_or_mixed_error_evidence_is_unknown(self):
        raw = _wire(_session(), {'type': 'error'}, {'type': 'error'}, _final('noncompletion'))
        self.assertCategory(self.capture(raw, exit_code=1), 'unknown_failure')
        self.assertCategory(self.capture(raw, exit_code=1, code_evidence=(self.evidence('QUOTA'),)), 'unknown_failure')
        self.assertCategory(self.capture(raw, exit_code=1,
            code_evidence=(self.evidence('QUOTA'), self.evidence('RATE_LIMIT', 2))), 'unknown_failure')
        self.assertCategory(self.capture(raw, exit_code=1,
            code_evidence=(self.evidence('AUTH'), self.evidence('INVALID_CREDENTIAL', 2))), 'auth_failed')

    def test_untyped_unbound_and_duplicate_code_proof_rejected(self):
        for evidence in (({'event_index': 1, 'code': 'QUOTA', 'origin': 'fixture_harness'},),
                         (FixtureCodeEvidence(1, 'QUOTA', 'fixture_harness'),),
                         (self.evidence('QUOTA', 0),), (self.evidence('QUOTA', 99),),
                         (self.evidence('QUOTA'), self.evidence('AUTH'))):
            self.assertRejected(self.failure(evidence=evidence), 'E_DSH_EVIDENCE')
        raw = _wire(_session(), {'type': 'text', 'text': 'QUOTA'}, _final('noncompletion'))
        self.assertRejected(self.capture(raw, exit_code=1, code_evidence=(self.evidence('QUOTA'),)), 'E_DSH_EVIDENCE')

    def test_completed_final_conflicts_with_prior_error(self):
        raw = _wire(_session(), {'type': 'error'}, _final())
        self.assertRejected(self.capture(raw), 'E_DSH_CONFLICT')
        self.assertRejected(self.capture(raw, code_evidence=(self.evidence('QUOTA'),)), 'E_DSH_CONFLICT')

    def test_strict_session_and_final_order(self):
        sequences = [({'type': 'text', 'text': 'before'}, _session(), _final()),
                     (_final(),), (_session(), _session(), _final()),
                     (_session(), _final(), _final()),
                     (_session(), _final(), _final('conflicting')),
                     (_session(), _final(), {'type': 'text', 'text': 'after'})]
        for events in sequences:
            self.assertRejected(self.capture(_wire(*events)), 'E_DSH_ORDER')

    def test_session_mismatch_in_final_body_and_runner(self):
        self.assertRejected(self.capture(_wire(_session(), _final(session='different-native'))), 'E_DSH_SESSION')
        self.assertRejected(self.capture(expected_session_id='different-runner'), 'E_DSH_SESSION')
        raw = _wire(_session(), {'type': 'text', 'text': 'hello', 'sessionId': 'different'}, _final())
        self.assertRejected(self.capture(raw), 'E_DSH_SESSION')
        self.assertCategory(self.capture(expected_session_id=_SESSION), None)

    def test_native_uuid4_session_is_never_resume_token(self):
        native = '00000000-0000-4000-8000-000000000099'
        event = self.assertCategory(self.capture(_wire(_session(native), _final(session=native)),
                                               expected_session_id=native), None)
        self.assertIsNone(event['resume_token'])
        self.assertNotIn(native, json.dumps(event))

    def test_runner_truncation_and_top_level_truncation_fail_closed(self):
        self.assertRejected(self.capture(truncated=True), 'E_DSH_TRUNCATED')
        self.assertRejected(self.capture(_wire(_session(), {'type': 'text', 'text': 'short', 'truncated': True}, _final())), 'E_DSH_TRUNCATED')
        self.assertRejected(self.capture(_wire(_session(), _final() | {'truncated': 1})), 'E_DSH_SCHEMA')

    def test_invalid_json_duplicates_constants_and_top_level_values(self):
        for line in (b'{broken}', b'{"type":"session","sessionId":"a","sessionId":"b"}',
                     b'{"type":"tool_result","value":NaN}', b'{"type":"tool_result","value":1e999}',
                     b'[1,2]', b'null', b'"QUOTA"'):
            result = self.adapter.parse(self.capture(line + b'\n'))
            self.assertFalse(result['Ok'])
            self.assertIsNone(result['Data'])
        raw = _wire(_session()) + b'{"type":"final","sessionId":"synthetic-native-session","reason":{"kind":"completed","kind":"other"}}\n'
        self.assertRejected(self.capture(raw), 'E_DSH_JSON')

    def test_utf8_surrogates_and_bom_are_not_repaired(self):
        for raw in (b'\xff\n', b'\xef\xbb\xbf' + _wire(_session(), _final()),
                    b'{"type":"text","text":"\\ud800"}\n', '\ud800\n'):
            self.assertRejected(self.capture(raw), 'E_DSH_JSON')

    def test_line_delimited_framing_and_crlf(self):
        for raw in (b'', b'\n', b' \n', _wire(_session(), _final())[:-1],
                    _wire(_session()) + b'\n' + _wire(_final()),
                    _wire(_session(), _final()).replace(b'\n', b'\r')):
            self.assertRejected(self.capture(raw), 'E_DSH_FRAMING')
        self.assertCategory(self.capture(_wire(_session(), _final()).replace(b'\n', b'\r\n')), None)
        self.assertRejected(self.capture(b'{"type":\r"session","sessionId":"s"}\n'), 'E_DSH_FRAMING')

    def test_schema_control_fields_and_unknown_categories(self):
        for event in ({'type': 'session'}, {'type': 'session', 'sessionId': None},
                      {'type': 'session', 'sessionId': 's', 'source': 'adapter'},
                      {'type': 'signal', 'signal': 'SIGTERM'}):
            self.assertRejected(self.capture(_wire(event)), 'E_DSH_SCHEMA')
        for reason in (None, [], 'completed', {'kind': True}, {'kind': ''}, {'kind': 'completed', 'code': 'QUOTA'}):
            self.assertRejected(self.capture(_wire(_session(), _final() | {'reason': reason})), 'E_DSH_SCHEMA')
        self.assertRejected(self.capture(_wire(_session(), {'type': 'thinking', 'text': {}}, _final())), 'E_DSH_SCHEMA')

    def test_raw_strings_dicts_and_untyped_runner_are_not_trusted_inputs(self):
        for record in (_wire(_session(), _final()),
                       {'ndjson': _wire(_session(), _final()), 'exit_code': 0, 'source': 'adapter'},
                       replace(self.capture(), runner={'exit_code': 0}),
                       replace(self.capture(), context={'task_id': _TASK})):
            self.assertRejected(record, 'E_DSH_CONTEXT')

    def test_task_context_is_validated_without_raw_echo(self):
        for context in (replace(self.context, task_id=_CANARY), replace(self.context, event_id=_CANARY),
                        replace(self.context, issue_url=_URL + '?secret=' + _CANARY),
                        replace(self.context, attempt_no=-1)):
            self.assertRejected(replace(self.capture(), context=context), 'E_DSH_CONTEXT')

    def test_string_limit_is_utf8_bytes_and_includes_json_keys(self):
        self.assertCategory(self.capture(_wire(_session(), {'type': 'text', 'text': 'a'*8192}, _final())), None)
        for event in ({'type': 'text', 'text': 'a'*8193}, {'type': 'thinking', 'text': '中'*2731},
                      {'type': 'tool_result', 'a'*8193: 0}):
            self.assertRejected(self.capture(_wire(_session(), event, _final())), 'E_DSH_LIMIT')
        self.assertCategory(self.capture(_wire(_session(), {'type': 'text', 'text': '中'*2730}, _final())), None)

    def test_line_limit_exact_boundary(self):
        event = {'type': 'tool_result', 'a': 'a'*8192, 'b': 'b'*8192, 'c': 'c'*8192, 'd': ''}
        overhead = len(_wire(event)) - 1
        event['d'] = 'd'*(dsh.MAX_LINE_BYTES - overhead)
        line = _wire(event)
        self.assertEqual(len(line) - 1, dsh.MAX_LINE_BYTES)
        self.assertCategory(self.capture(_wire(_session()) + line + _wire(_final())), None)
        self.assertRejected(self.capture(_wire(_session()) + line[:-1] + b' \n' + _wire(_final())), 'E_DSH_LIMIT')

    def test_total_bytes_and_event_count_limits(self):
        self.assertRejected(self.capture(b'x'*(dsh.MAX_TOTAL_BYTES + 1)), 'E_DSH_LIMIT')
        text = {'type': 'text', 'text': ''}
        self.assertCategory(self.capture(_wire(_session(), *([text]*(dsh.MAX_EVENTS-2)), _final())), None)
        self.assertRejected(self.capture(_wire(_session(), *([text]*(dsh.MAX_EVENTS-1)), _final())), 'E_DSH_LIMIT')

    def test_depth_and_total_node_limits(self):
        nested = None
        for _ in range(dsh.MAX_DEPTH + 1):
            nested = [nested]
        self.assertRejected(self.capture(_wire(_session(), {'type': 'tool_result', 'data': nested}, _final())), 'E_DSH_LIMIT')
        item = {'type': 'tool_result', 'data': [0]*9000}
        self.assertLess(len(_wire(item)), dsh.MAX_LINE_BYTES)
        self.assertRejected(self.capture(_wire(_session(), item, item, _final())), 'E_DSH_LIMIT')

    def test_private_content_and_session_are_not_emitted_or_persisted(self):
        native = _CANARY + '-session'
        error = {'type': 'error', 'message': _CANARY, 'path': 'synthetic-private-location',
                 'error': {'code': 'QUOTA', 'token': _CANARY}}
        capture = self.capture(_wire(_session(native), error, _final('failed', native)), exit_code=1)
        output = self.assertCategory(capture, 'unknown_failure')
        self.assertNotIn(_CANARY, json.dumps(output))
        self.assertNotIn(_CANARY, repr(capture))
        store = self.supervisor()
        self.assertTrue(store.ingest(capture, adapter=self.adapter)['Ok'])
        checkpoint = store.check()['Data']
        self.assertNotIn(_CANARY, json.dumps(checkpoint))
        self.assertNotIn(b'SYNTHETIC_PRIVATE', (Path(self.temp.name)/'checkpoint'/'supervisor.sqlite3').read_bytes())
        self.assertEqual(checkpoint['tasks'][0]['state'], 'BLOCKED_EXECUTION')
        self.assertEqual(checkpoint['events'][-1]['source'], 'synthetic')
        self.assertEqual(checkpoint['deliveries'][0]['reservations'], 0)

    def test_supervisor_success_and_synthetic_quota_integration_without_notify(self):
        for quota in (False, True):
            with self.subTest(quota=quota):
                # 每个用例使用新临时数据库，退出时清理，不触碰已有用户检查点。
                with tempfile.TemporaryDirectory(dir=self.temp.name) as base:
                    store = Supervisor(base, clock=lambda: 1000)
                    self.assertTrue(store.register(_TASK, _URL, 'deepseek')['Ok'])
                    started = normalize_event(self.assertCategory(self.capture(), None) | {
                        'event_id': '00000000-0000-4000-8000-000000000024', 'kind': 'started',
                        'exit_code': None, 'category': None, 'signal': 'none'})['Data']
                    self.assertTrue(store.ingest(started)['Ok'])
                    capture = self.failure(evidence=(self.evidence('QUOTA'),)) if quota else self.capture()
                    self.assertTrue(store.ingest(capture, adapter=self.adapter)['Ok'])
                    saved = store.check()['Data']
                    self.assertEqual(saved['tasks'][0]['state'], 'BLOCKED_QUOTA' if quota else 'SUCCEEDED')
                    self.assertTrue(all(row['source'] == 'synthetic' for row in saved['events']))
                    self.assertIsNone(saved['tasks'][0]['resume_token'])

    def test_rejected_capture_leaves_supervisor_state_unchanged(self):
        store = self.supervisor()
        before = store.check()['Data']
        result = store.ingest(self.capture(_wire(_session())), adapter=self.adapter)
        self.assertEqual(result['Code'], 'E_EVENT_INVALID')
        self.assertEqual(store.check()['Data'], before)

    def test_observed_shaped_terminal_authentication_failure(self):
        # 仅按 CTO 脱敏字段摘要重建，未复制真实 NDJSON、路径、Session ID 或错误正文。
        capture = self.terminal_failure()
        self.assertEqual(len(capture.ndjson.splitlines()), 6)
        self.assertEqual(capture.runner.exit_code, 1)
        self.assertEqual(capture.runner.code_evidence, ())
        event = self.assertCategory(capture, 'auth_failed')
        self.assertEqual(event['signal'], 'auth_failed')
        self.assertEqual(event['exit_code'], 1)

    def test_terminal_error_missing_or_nonstring_code_rejected(self):
        for error in ({'message': _MESSAGE_CANARY}, {}, None, [],
                      *({'code': code, 'message': _MESSAGE_CANARY} for code in
                        (None, True, 0, 1.0, [], {'code': 'MISSING_CREDENTIAL'}, ''))):
            terminal = _final('error')
            terminal['reason']['error'] = error
            self.assertRejected(self.terminal_failure(terminal), 'E_DSH_SCHEMA')
        self.assertRejected(self.terminal_failure(_final('error')), 'E_DSH_SCHEMA')

    def test_unobserved_terminal_codes_remain_unknown(self):
        for code in ('QUOTA', 'ACCOUNT_QUOTA', 'RATE_LIMIT', 'AUTH', 'INVALID_CREDENTIAL',
                     'TIMEOUT', 'SERVER', 'NO_ADAPTER', 'CONTEXT_WINDOW_EXCEEDED',
                     'missing_credential', 'MISSING_CREDENTIAL_SUFFIX', 'UNVERIFIED_CODE'):
            with self.subTest(code=code):
                self.assertCategory(self.terminal_failure(_terminal_error(code)), 'unknown_failure')

    def test_status_is_progress_without_field_path_assumptions(self):
        statuses = _progress()
        # 故意加入未核验的状态内容；不得搜索这些字段并建立完成/额度/认证结论。
        statuses[0] |= {'fixture_extra': {'code': 'QUOTA', 'completed': True},
                        'text': 'MISSING_CREDENTIAL', 'error': {'code': 'MISSING_CREDENTIAL'}}
        self.assertRejected(self.capture(_wire(_session(), *statuses,
            {'type': 'tool_result', 'completed': True})), 'E_DSH_FINAL_MISSING')
        self.assertCategory(self.capture(_wire(_session(), *statuses, _final('unknown-final')), exit_code=1),
                            'unknown_failure')

    def test_status_still_obeys_session_order_and_limits(self):
        self.assertRejected(self.capture(_wire({'type': 'status'}, _session(), _final())), 'E_DSH_ORDER')
        self.assertRejected(self.capture(_wire(_session(), _final(), {'type': 'status'})), 'E_DSH_ORDER')
        self.assertRejected(self.capture(_wire(_session(), {'type': 'status', 'sessionId': 'other'}, _final())),
                            'E_DSH_SESSION')
        self.assertRejected(self.capture(_wire(_session(), {'type': 'status', 'fixture_extra': 'x'*8193}, _final())),
                            'E_DSH_LIMIT')
        self.assertRejected(self.capture(_wire(_session(), *([{'type': 'status'}]*dsh.MAX_EVENTS), _final())),
                            'E_DSH_LIMIT')

    def test_private_observed_fields_dropped_in_results_repr_logs_and_sqlite(self):
        terminal = _terminal_error() | {'text': _TEXT_CANARY}
        capture = self.terminal_failure(terminal)
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), patch('logging.Logger._log') as logger:
            event = self.assertCategory(capture, 'auth_failed')
            store = self.supervisor()
            self.assertTrue(store.ingest(capture, adapter=self.adapter)['Ok'])
            checkpoint = store.check()['Data']
            logger.assert_not_called()
        self.assertEqual(output.getvalue() + errors.getvalue(), '')
        self.assertEqual(checkpoint['tasks'][0]['state'], 'BLOCKED_AUTH')
        self.assertTrue(all(row['source'] == 'synthetic' for row in checkpoint['events']))
        self.assertEqual(checkpoint['deliveries'][0]['reservations'], 0)
        self.assertIsNone(checkpoint['tasks'][0]['resume_token'])
        serialized = json.dumps(event) + json.dumps(checkpoint) + repr(capture) + repr(capture.runner) + repr(self.adapter)
        raw_db = (Path(self.temp.name)/'checkpoint'/'supervisor.sqlite3').read_bytes()
        for canary in (_CWD_CANARY, _TEXT_CANARY, _MESSAGE_CANARY, _SESSION):
            self.assertNotIn(canary, serialized)
            self.assertNotIn(canary.encode(), raw_db)

    def test_private_fields_type_and_size_fail_closed(self):
        for value in ({'private': _CANARY}, 42):
            self.assertRejected(self.capture(_wire(_session() | {'cwd': value}, _final())), 'E_DSH_SCHEMA')
            self.assertRejected(self.capture(_wire(_session(), _final() | {'text': value})), 'E_DSH_SCHEMA')
            terminal = _terminal_error()
            terminal['reason']['error']['message'] = value
            self.assertRejected(self.terminal_failure(terminal), 'E_DSH_SCHEMA')
        for field in ('cwd', 'text', 'message'):
            session, terminal = _session(), _terminal_error()
            if field == 'cwd':
                session['cwd'] = 'x'*8193
            elif field == 'text':
                terminal['text'] = 'x'*8193
            else:
                terminal['reason']['error']['message'] = 'x'*8193
            self.assertRejected(self.capture(_wire(session, terminal), exit_code=1), 'E_DSH_LIMIT')

    def test_terminal_code_is_independent_of_all_prose_and_other_json_paths(self):
        for code, category in (('MISSING_CREDENTIAL', 'auth_failed'), ('UNVERIFIED_CODE', 'unknown_failure')):
            terminal = _terminal_error(code) | {'text': 'QUOTA MISSING_CREDENTIAL ACCOUNT_QUOTA'}
            terminal['reason']['error']['message'] = 'QUOTA MISSING_CREDENTIAL'
            model = [ {'type': kind, 'text': 'QUOTA MISSING_CREDENTIAL', 'code': 'QUOTA',
                       'error': {'code': 'MISSING_CREDENTIAL'}} for kind in ('text', 'thinking', 'tool_result', 'status') ]
            self.assertCategory(self.capture(_wire(_session(), *model, terminal), exit_code=1), category)

    def test_terminal_unknown_cannot_be_upgraded_by_fixture_code(self):
        raw = _wire(_session(), {'type': 'error'}, _terminal_error('QUOTA'))
        self.assertCategory(self.capture(raw, exit_code=1, code_evidence=(self.evidence('QUOTA'),)), 'unknown_failure')
        contradictory = _wire(_session(), {'type': 'error'}, _terminal_error())
        self.assertRejected(self.capture(contradictory, exit_code=1,
            code_evidence=(self.evidence('QUOTA'),)), 'E_DSH_CONFLICT')
        self.assertRejected(self.capture(_wire(_session(), _final() | {
            'reason': {'kind': 'completed', 'error': {'code': 'MISSING_CREDENTIAL'}}})), 'E_DSH_SCHEMA')

    def test_terminal_error_exit_zero_or_one_never_success(self):
        cases = (
            ('MISSING_CREDENTIAL', 1, None, 'auth_failed'),
            ('MISSING_CREDENTIAL', 0, None, 'unknown_failure'),
            ('MISSING_CREDENTIAL', None, None, 'unknown_failure'),
            ('MISSING_CREDENTIAL', 2, None, 'unknown_failure'),
            ('MISSING_CREDENTIAL', -9, None, 'unknown_failure'),
            ('MISSING_CREDENTIAL', 1, 9, 'unknown_failure'),
            ('UNKNOWN', 1, None, 'unknown_failure'),
            ('UNKNOWN', 0, None, 'unknown_failure'),
        )
        for code, exit_code, os_signal, expected in cases:
            with self.subTest(code=code, exit_code=exit_code, os_signal=os_signal):
                event = self.assertCategory(self.terminal_failure(_terminal_error(code),
                    exit_code=exit_code, os_signal=os_signal), expected)
                self.assertIsNotNone(event['category'])
                self.assertNotEqual(event['signal'], 'none')
                self.assertEqual(event['exit_code'], exit_code)

    def test_nonzero_exit_cannot_succeed_after_progress_and_tools(self):
        raw = _wire(_session(), *_progress(), {'type': 'tool_result', 'completed': True}, _final())
        self.assertCategory(self.capture(raw, exit_code=1), 'unknown_failure')
        # 成功形状仅为 documented-only 的 synthetic 测试，不代表 Windows 已成功运行。
        self.assertCategory(self.capture(raw, exit_code=0), None)

    def test_terminal_auth_native_uuid_is_not_resume_token(self):
        native = '00000000-0000-4000-8000-000000000098'
        event = self.assertCategory(self.terminal_failure(session=native, expected_session_id=native), 'auth_failed')
        self.assertNotIn(native, json.dumps(event))
        self.assertIsNone(event['resume_token'])


if __name__ == '__main__':
    unittest.main()
