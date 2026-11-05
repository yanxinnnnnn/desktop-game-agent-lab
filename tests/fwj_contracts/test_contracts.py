"""全部数据为 synthetic；observed 是合成字段的证据标签，不是视觉实验结论。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

try:
    from fwj_contracts import (
        make_intent_id, validate_action_intent, validate_attempt, validate_observation,
        validate_observation_pair, validate_revision, validate_task_state,
        validate_verification_result,
    )
except ModuleNotFoundError as exc:
    if exc.name in {'jsonschema', 'referencing'}:
        raise unittest.SkipTest('缺少离线依赖，请安装 requirements-fwj.txt') from exc
    raise

EXAMPLES = json.loads(Path(__file__).with_name('examples.json').read_text(encoding='utf-8'))


class Contracts(unittest.TestCase):
    def setUp(self):
        self.obs = deepcopy(EXAMPLES['observation'])
        self.state = deepcopy(EXAMPLES['task_state'])
        self.intent = deepcopy(EXAMPLES['action_intent'])
        self.verification = deepcopy(EXAMPLES['verification_result'])
        self.guard = deepcopy(self.obs)
        self.guard.update(observation_seq=2, observation_id='01ARZ3NDEKTSV4RRFFQ69G5FAW',
                          captured_at='2026-01-01T00:00:01Z')

    def assertOk(self, result):
        self.assertTrue(result['Ok'], result)
        self.assertEqual(result['Code'], 'OK')

    def assertCode(self, result, code, path=None):
        self.assertFalse(result['Ok'], result)
        self.assertEqual(result['Code'], code, result)
        if path is not None:
            self.assertIn(path, [e['path'] for e in result['Data']['errors']], result)

    def attempt(self, **overrides):
        arguments = dict(intent=self.intent, state=self.state, origin=self.obs, guard=self.guard,
                         history=[], now='2026-01-01T00:00:02Z',
                         guard_stage=self.state['stage'], preconditions_hold=True)
        arguments.update(overrides)
        return validate_attempt(**arguments)

    def rehash(self, intent):
        intent['intent_id'] = make_intent_id(self.state['session_id'], intent['attempt_no'],
            intent['kind'], intent['target']['semantic_id'],
            intent['origin_observation']['observation_id'])['Data']

    def revision(self, after_obs=None, after_state=None):
        obs = self.guard if after_obs is None else after_obs
        state = deepcopy(self.state) if after_state is None else after_state
        state['observation_seq'] = obs['observation_seq']
        return validate_revision(self.state, state, self.obs, obs)

    def test_all_examples_and_schemas(self):
        self.assertEqual(EXAMPLES['source'], 'synthetic')
        self.assertEqual(EXAMPLES['label_confidence'], 'synthetic')
        for name, validator in [('observation', validate_observation), ('task_state', validate_task_state),
                                ('action_intent', validate_action_intent), ('verification_result', validate_verification_result)]:
            with self.subTest(name=name):
                self.assertOk(validator(EXAMPLES[name]))

    def test_schema_versions_required_fields_and_extra_fields(self):
        for name, validator in [('observation', validate_observation), ('task_state', validate_task_state),
                                ('action_intent', validate_action_intent), ('verification_result', validate_verification_result)]:
            for field, value in [('schema_version', '1.0'), ('unexpected', True)]:
                data = deepcopy(EXAMPLES[name]); data[field] = value
                with self.subTest(name=name, field=field):
                    self.assertCode(validator(data), 'E_SCHEMA')
            data = deepcopy(EXAMPLES[name]); del data['schema_version']
            self.assertCode(validator(data), 'E_SCHEMA', '/schema_version')

    def test_invalid_enum_nested_type_and_non_json(self):
        for path, value in [('ui_mode', 'other'), ('observation_quality', 'perfect'), ('window', [])]:
            data = deepcopy(self.obs); data[path] = value
            self.assertCode(validate_observation(data), 'E_SCHEMA', '/' + path)
        for value in [float('nan'), float('inf'), object(), {1: 'invalid'}, ('tuple',)]:
            self.assertCode(validate_observation(value), 'E_SCHEMA')

    def test_negative_sequences_and_invalid_identifiers(self):
        for data, field, validator in [(self.obs, 'observation_seq', validate_observation),
                                      (self.state, 'revision', validate_task_state),
                                      (self.state, 'observation_seq', validate_task_state),
                                      (self.intent, 'attempt_no', validate_action_intent),
                                      (self.intent, 'state_revision', validate_action_intent)]:
            invalid = deepcopy(data); invalid[field] = -1
            self.assertCode(validator(invalid), 'E_SCHEMA', '/' + field)
        for bad in ['obs-1', '81ARZ3NDEKTSV4RRFFQ69G5FAV', '01ARZ3NDEKTSV4RRFFQ69G5FAI', self.obs['observation_id'] + '\n']:
            data = deepcopy(self.obs); data['observation_id'] = bad
            self.assertCode(validate_observation(data), 'E_SCHEMA', '/observation_id')
        invalid = deepcopy(self.obs); invalid['screenshot']['sha256'] += '\n'
        self.assertCode(validate_observation(invalid), 'E_SCHEMA', '/screenshot/sha256')
        self.obs['window']['pid'] = 0
        self.assertCode(validate_observation(self.obs), 'E_SCHEMA', '/window/pid')

    def test_timestamp_validation_is_active_without_optional_extras(self):
        for bad in ['yesterday', '2026-02-30T00:00:00Z', '2026-01-01T00:00:00',
                    '2026-01-01T25:00:00Z', '2026-01-01T00:00:00+25:00', '2026-01-01T00:00:00+00:99']:
            self.obs['captured_at'] = bad
            self.assertCode(validate_observation(self.obs), 'E_SCHEMA', '/captured_at')
        self.obs['captured_at'] = '2026-01-01T08:00:00+08:00'
        self.assertOk(validate_observation(self.obs))

    def test_missing_tracker_and_unknown_fields(self):
        self.assertOk(validate_observation(self.obs))
        self.obs['dialog'] = None
        self.obs['ui_mode'] = 'unknown'
        self.obs['observation_quality'] = 'unusable'
        self.assertOk(validate_observation(self.obs))
        self.assertIsNone(self.obs['quest']['objective_text'])
        self.obs['quest']['confidence'] = 'observed'
        self.assertCode(validate_observation(self.obs), 'E_SCHEMA')

    def test_unknown_stage_and_evidence_provenance(self):
        self.state.update(stage='unknown', stage_source='unknown')
        self.assertOk(validate_task_state(self.state))
        self.state['stage_source'] = 'observed'
        self.assertCode(validate_task_state(self.state), 'E_SCHEMA', '/stage_source')
        self.state = deepcopy(EXAMPLES['task_state'])
        flag = self.state['flags']['example_flag']
        flag.update(value=True, evidence='inferred')
        self.assertOk(validate_task_state(self.state))
        flag['evidence'] = 'observed'
        self.assertCode(validate_task_state(self.state), 'E_SCHEMA')
        flag['observations'] = [self.obs['observation_id']]
        self.assertOk(validate_task_state(self.state))

    def test_duplicate_semantic_entities(self):
        self.obs['interactables'].append(deepcopy(self.obs['interactables'][0]))
        self.obs['interactables'][1]['label'] = 'different'
        self.assertCode(validate_observation(self.obs), 'E_CONTRACT', '/interactables/1/semantic_id')

    def test_local_reference_is_optional_not_example_data(self):
        self.assertNotIn('local_ref', self.obs['screenshot'])
        self.obs['screenshot']['local_ref'] = None
        self.assertOk(validate_observation(self.obs))

    def test_key_and_hover_shapes(self):
        for kind, key in [('key', 'Alt+P'), ('hover_probe', None)]:
            intent = deepcopy(self.intent); intent.update(kind=kind, key=key)
            self.rehash(intent)
            self.assertOk(validate_action_intent(intent, session_id=self.state['session_id']))
        self.intent.update(kind='key', key=None)
        self.assertCode(validate_action_intent(self.intent), 'E_SCHEMA', '/key')

    def test_precondition_pointer_and_unknown_interactable(self):
        self.intent['preconditions'][0]['path'] = '/bad~escape'
        self.assertCode(validate_action_intent(self.intent), 'E_SCHEMA')
        self.obs['interactables'][0]['label'] = None
        self.assertCode(validate_observation(self.obs), 'E_SCHEMA')
        self.obs['interactables'][0]['confidence'] = 'unknown'
        self.assertOk(validate_observation(self.obs))

    def test_expiry_is_exact_and_origin_precedes_proposal(self):
        self.intent['ttl_ms'] = 4999
        self.assertCode(validate_action_intent(self.intent), 'E_CONTRACT', '/expires_at')
        self.intent['ttl_ms'] = 10**100
        self.assertCode(validate_action_intent(self.intent), 'E_CONTRACT', '/expires_at')
        self.intent = deepcopy(EXAMPLES['action_intent'])
        self.intent['origin_observation']['captured_at'] = '2026-01-01T00:00:01Z'
        self.assertCode(validate_action_intent(self.intent), 'E_CONTRACT', '/origin_observation/captured_at')

    def test_irreversible_requires_approval_declaration(self):
        self.intent['risk'] = 'irreversible'
        self.assertCode(validate_action_intent(self.intent), 'E_SCHEMA', '/requires_human_approval')

    def test_hash_is_deterministic_and_components_are_unambiguous(self):
        result = make_intent_id('synthetic-session', 1, 'click_semantic', 'continue', self.obs['observation_id'])
        self.assertEqual(result['Data'], self.intent['intent_id'])
        self.assertOk(validate_action_intent(self.intent, session_id='synthetic-session'))
        self.assertCode(validate_action_intent(self.intent, session_id='other'), 'E_CONTRACT', '/intent_id')
        self.assertCode(make_intent_id('s', -1, 'key', 'target', self.obs['observation_id']), 'E_SCHEMA')
        a = make_intent_id('a,b', 1, 'key', 'c', self.obs['observation_id'])
        b = make_intent_id('a', 1, 'key', 'b,c', self.obs['observation_id'])
        self.assertNotEqual(a['Data'], b['Data'])

    def test_adjacent_capture_and_repeated_screenshot(self):
        self.assertOk(validate_observation_pair(self.obs, self.guard))
        self.assertOk(self.revision())
        for field, value in [('observation_seq', 1), ('observation_seq', 3),
                             ('observation_id', self.obs['observation_id']),
                             ('captured_at', '2025-12-31T23:59:59Z')]:
            after = deepcopy(self.guard); after[field] = value
            self.assertCode(validate_observation_pair(self.obs, after), 'E_CONTRACT', '/' + field)

    def test_repeated_frame_must_not_increment_revision(self):
        after = deepcopy(self.state); after['revision'] = 1
        self.assertCode(self.revision(after_state=after), 'E_CONTRACT', '/revision')

    def test_dialog_progress_preserves_stage(self):
        for i in range(4):
            before_stage = self.state['stage']
            self.guard['dialog']['text'] = f'合成正文 {i}'
            self.guard['dialog']['text_hash'] = hashlib.sha256(self.guard['dialog']['text'].encode()).hexdigest()
            after = deepcopy(self.state); after['revision'] += 1
            self.assertOk(self.revision(after_state=after))
            self.assertEqual(after['stage'], before_stage)
            self.state = after; self.state['observation_seq'] = self.guard['observation_seq']
            self.obs = deepcopy(self.guard)
            self.guard['observation_seq'] += 1
            self.guard['observation_id'] = f'01ARZ3NDEKTSV4RRFFQ69G5FA{i}'
            self.assertOk(self.revision())

    def test_each_semantic_field_requires_one_revision(self):
        changes = [lambda s,o: s.update(stage='new-stage'),
                   lambda s,o: s['objective'].update(text='合成任务文案'),
                   lambda s,o: o.update(ui_mode='panel'),
                   lambda s,o: o['panels'].append({'name':'合成面板','confidence':'inferred'}),
                   lambda s,o: o['dialog']['options'].append('合成选项'),
                   lambda s,o: o['interactables'][0].update(label='学习 tooltip'),
                   lambda s,o: s['flags']['example_flag'].update(value=True, evidence='inferred')]
        for change in changes:
            state, obs = deepcopy(self.state), deepcopy(self.guard)
            change(state, obs)
            if state['stage'] != self.state['stage']:
                obs['quest'].update(quest_id='synthetic-task', title='合成任务',
                                    objective_text='合成新阶段', target_name='合成 NPC', confidence='observed')
                state['stage_source'] = 'observed'
            self.assertCode(self.revision(obs, state), 'E_CONTRACT', '/revision')
            state['revision'] += 1
            self.assertOk(self.revision(obs, state))

    def test_evidence_change_counts_but_reference_only_change_does_not(self):
        self.state['flags']['example_flag'].update(value=True, evidence='inferred')
        after = deepcopy(self.state)
        after['flags']['example_flag'].update(evidence='observed', observations=[self.obs['observation_id']])
        after['revision'] = 1
        self.assertOk(self.revision(after_state=after))
        after = deepcopy(self.state)
        after['flags']['example_flag']['observations'] = [self.obs['observation_id']]
        self.assertOk(self.revision(after_state=after))

    def test_sets_ignore_order_and_geometry_is_not_semantic_revision(self):
        self.obs['panels'] = [{'name':'a','confidence':'observed'},{'name':'b','confidence':'observed'}]
        self.guard['panels'] = list(reversed(self.obs['panels']))
        self.guard['interactables'][0]['region']['x'] += 1
        self.assertOk(self.revision())

    def test_revision_context_mismatch(self):
        after = deepcopy(self.state); after['session_id'] = 'other'
        self.assertCode(self.revision(after_state=after), 'E_CONTRACT', '/session_id')
        after = deepcopy(self.state); after['observation_seq'] = 9
        self.assertCode(validate_revision(self.state, after, self.obs, self.guard), 'E_CONTRACT', '/observation_seq')

    def test_valid_offline_attempt_and_no_input_mutation(self):
        values = deepcopy((self.intent, self.state, self.obs, self.guard))
        self.assertOk(self.attempt())
        self.assertEqual(values, (self.intent, self.state, self.obs, self.guard))

    def test_duplicate_has_priority_even_if_expired(self):
        self.assertCode(self.attempt(history=[self.intent], now='2026-01-01T00:01:00Z'),
                        'E_DUPLICATE_ATTEMPT', '/intent_id')

    def test_new_attempt_same_target_fresh_observation(self):
        previous = deepcopy(self.intent)
        self.obs = deepcopy(self.guard)
        self.guard.update(observation_seq=3, observation_id='01ARZ3NDEKTSV4RRFFQ69G5FAX', captured_at='2026-01-01T00:00:02Z')
        self.state['observation_seq'] = 2
        self.intent.update(attempt_no=2, created_at='2026-01-01T00:00:01Z', expires_at='2026-01-01T00:00:06Z')
        self.intent['origin_observation'].update({k:self.obs[k] for k in ('observation_id','observation_seq','captured_at')})
        self.rehash(self.intent)
        self.assertOk(self.attempt(history=[previous]))
        self.intent['attempt_no'] = 4; self.rehash(self.intent)
        self.assertCode(self.attempt(history=[previous]), 'E_CONTRACT', '/attempt_no')

    def test_stale_time_revision_preconditions(self):
        self.assertCode(self.attempt(now='2026-01-01T00:00:05.001Z'), 'E_STALE_INTENT', '/expires_at')
        self.assertOk(self.attempt(now='2026-01-01T00:00:05Z'))
        self.assertCode(self.attempt(preconditions_hold=False), 'E_STALE_INTENT', '/preconditions')
        self.state['revision'] += 1
        self.assertCode(self.attempt(), 'E_STALE_INTENT', '/state_revision')

    def test_stale_window_identity_and_exact_geometry(self):
        for key, value in [('hwnd', 102), ('pid', 202), ('title_hash', 'a'*64),
                           ('rect', {'x':1,'y':0,'w':800,'h':600})]:
            guard = deepcopy(self.guard); guard['window'][key] = value
            self.assertCode(self.attempt(guard=guard), 'E_STALE_INTENT', '/window/' + key)

    def test_stale_stage_and_origin_binding(self):
        self.assertCode(self.attempt(guard_stage='other'), 'E_STALE_INTENT')
        self.assertCode(self.attempt(guard_stage='unknown'), 'E_STALE_INTENT')
        self.intent['origin_observation']['observation_seq'] = 0
        self.assertCode(self.attempt(), 'E_STALE_INTENT', '/origin_observation')

    def test_guard_cannot_reuse_origin_or_come_from_future(self):
        self.assertCode(self.attempt(guard=self.obs), 'E_STALE_INTENT', '/observation_seq')
        self.guard['captured_at'] = '2026-01-01T00:00:03Z'
        self.assertCode(self.attempt(), 'E_STALE_INTENT', '/captured_at')

    def test_approval_missing_wrong_attempt_expired_and_valid(self):
        self.intent.update(risk='irreversible', requires_human_approval=True)
        self.assertCode(self.attempt(), 'E_APPROVAL_REQUIRED', '/approval')
        approval = {'intent_id':self.intent['intent_id'],'decision':'approved','approved_by':'synthetic-operator',
                    'approved_at':'2026-01-01T00:00:01Z','expires_at':'2026-01-01T00:00:04Z'}
        self.assertOk(self.attempt(approval=approval))
        for key, value in [('intent_id','a'*64), ('decision','rejected'), ('approved_by',''),
                           ('expires_at','2026-01-01T00:00:01Z'), ('approved_at','2026-02-30T00:00:00Z')]:
            bad = deepcopy(approval); bad[key] = value
            self.assertCode(self.attempt(approval=bad), 'E_APPROVAL_REQUIRED')
        self.assertCode(self.attempt(preconditions_hold=False), 'E_STALE_INTENT')
        self.assertCode(self.attempt(history=[self.intent]), 'E_DUPLICATE_ATTEMPT')

    def test_malformed_context_is_not_approval_or_stale(self):
        for kwargs in [{'history':None}, {'now':'bad'}, {'preconditions_hold':None}, {'guard_stage':None}]:
            self.assertCode(self.attempt(**kwargs), 'E_SCHEMA')

    def test_confirmed_needs_two_frames_quality_and_checks(self):
        for field, value in [('observation_quality','degraded'), ('before_observation_id',None),
                             ('checks',[]), ('error_code','E_NO_CHANGE')]:
            data = deepcopy(self.verification); data[field] = value
            self.assertCode(validate_verification_result(data), 'E_SCHEMA')
        self.verification['after_observation_id'] = self.verification['before_observation_id']
        self.assertCode(validate_verification_result(self.verification), 'E_CONTRACT', '/after_observation_id')

    def test_rejected_and_ambiguous_cannot_apply_patch(self):
        self.verification.update(verdict='rejected', error_code='E_NO_CHANGE')
        self.verification['checks'][0]['pass'] = False
        self.assertOk(validate_verification_result(self.verification))
        self.verification['state_patch'] = {'stage':'other'}
        self.assertCode(validate_verification_result(self.verification), 'E_SCHEMA', '/state_patch')
        self.verification.update(verdict='ambiguous', state_patch={}, observation_quality='unusable',
                                 after_observation_id=None, checks=[], error_code='E_OCCLUDED')
        self.assertOk(validate_verification_result(self.verification))

    def test_receipt_cannot_make_confirmation(self):
        self.verification.update(executor_receipt={'operator_said':'done'}, checks=[])
        self.assertCode(validate_verification_result(self.verification), 'E_SCHEMA')
        self.verification['checks'] = [{'name':'change','expected':True,'actual':False,'pass':False}]
        self.assertCode(validate_verification_result(self.verification), 'E_SCHEMA')
        self.verification.update(verdict='ambiguous', observation_quality='unusable')
        self.assertOk(validate_verification_result(self.verification))

    def test_missing_tracker_cannot_promote_or_invent_stage(self):
        after = deepcopy(self.state)
        after['stage_source'] = 'observed'
        self.assertCode(self.revision(after_state=after), 'E_CONTRACT', '/stage_source')
        after = deepcopy(self.state); after.update(stage='invented', revision=1)
        self.assertCode(self.revision(after_state=after), 'E_CONTRACT', '/stage')
        after = deepcopy(self.state); after['open_questions'] = []
        self.assertCode(self.revision(after_state=after), 'E_CONTRACT', '/open_questions')

    def test_new_attempt_requires_new_origin(self):
        previous = deepcopy(self.intent)
        self.intent['attempt_no'] += 1
        self.rehash(self.intent)
        self.assertCode(self.attempt(history=[previous]), 'E_STALE_INTENT', '/origin_observation')

    def test_invalid_unicode_and_cyclic_non_json_are_rejected(self):
        self.obs['scene']['name'] = '\ud800'
        self.assertCode(validate_observation(self.obs), 'E_SCHEMA', '/scene/name')
        self.assertCode(make_intent_id('\ud800', 1, 'key', 'x', self.guard['observation_id']), 'E_SCHEMA')
        cyclic = {}; cyclic['loop'] = cyclic
        self.assertCode(validate_observation(cyclic), 'E_SCHEMA')

    def test_json_boolean_and_number_flag_values_are_distinct(self):
        self.state['flags']['example_flag'].update(value=True, evidence='inferred')
        after = deepcopy(self.state)
        after['flags']['example_flag']['value'] = 1
        after['revision'] = 1
        self.assertOk(self.revision(after_state=after))

    def test_offline_validation_never_opens_a_socket(self):
        with patch.object(socket, 'socket', side_effect=AssertionError('Network forbidden')):
            self.assertOk(self.attempt())
            self.assertOk(validate_verification_result(self.verification))


if __name__ == '__main__':
    unittest.main()
