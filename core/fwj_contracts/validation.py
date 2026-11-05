"""确定性数据校验：调用者提供 JSON 数据、时刻和历史，本模块无运行时守卫能力。"""

from datetime import datetime
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

_SCHEMA_ROOT = Path(__file__).resolve().parents[2] / 'schemas' / 'fwj' / 'v1.1'
_BASE = 'urn:fwj:v1.1:'
_FORMAT = FormatChecker()


def _time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00').replace('z', '+00:00'))


@_FORMAT.checks('date-time', raises=(ValueError, TypeError))
def _timestamp(value):
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})', value):
        return False
    if value[-1] not in ('Z', 'z') and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        return False
    # 固定到微秒，避免 TTL 在解析时被静默截断；不接收闰秒。
    fraction = re.search(r'\.(\d+)', value)
    if fraction and len(fraction[1]) > 6:
        return False
    try:
        _time(value)
    except ValueError:
        return False
    return True


def _issue(path, reason, code='E_CONTRACT'):
    return {'path': path, 'reason': reason, 'code': code}


def _result(errors=(), data=None):
    errors = list(errors)
    return {'Ok': not errors, 'Code': errors[0]['code'] if errors else 'OK',
            'Message': errors[0]['reason'] if errors else '',
            'Data': {'errors': errors} if errors else data}


def _pointer(parts):
    return '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in parts) if parts else ''


def _json_problem(value, path=''):
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                return _issue(path, 'JSON object keys must be strings', 'E_SCHEMA')
            error = _json_problem(v, path + _pointer([k]))
            if error:
                return error
    elif isinstance(value, list):
        for i, v in enumerate(value):
            error = _json_problem(v, path + '/' + str(i))
            if error:
                return error
    elif isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            return _issue(path, 'Expected valid UTF-8 text', 'E_SCHEMA')
    elif type(value) not in (str, int, float, bool, type(None)):
        return _issue(path, 'Expected a JSON value', 'E_SCHEMA')
    elif isinstance(value, float) and (value != value or abs(value) == float('inf')):
        return _issue(path, 'Non-finite numbers are not JSON', 'E_SCHEMA')
    return None


@lru_cache(maxsize=4)
def _validator(name):
    # 所有引用都在本地注册；未知引用抛出配置错误，绝不回退到网络。
    registry = Registry()
    schemas = {}
    for file in sorted(_SCHEMA_ROOT.glob('*.schema.json')):
        schema = json.loads(file.read_text(encoding='utf-8'))
        Draft202012Validator.check_schema(schema)
        schemas[schema['$id']] = schema
        registry = registry.with_resource(schema['$id'], Resource.from_contents(schema))
    return Draft202012Validator(schemas[_BASE + name], registry=registry, format_checker=_FORMAT)


def _errors(name, value):
    try:
        error = _json_problem(value)
    except RecursionError:
        return [_issue('', 'Cyclic or excessively nested JSON value', 'E_SCHEMA')]
    if error:
        return [error]
    errors = []
    for error in _validator(name).iter_errors(value):
        path = list(error.absolute_path)
        if error.validator == 'required':
            # 返回缺失字段自身的 JSON Pointer，便于下游定位。
            for field in error.validator_value:
                if field not in error.instance:
                    errors.append(_issue(_pointer(path + [field]), 'Required field is missing', 'E_SCHEMA'))
        else:
            errors.append(_issue(_pointer(path), error.message, 'E_SCHEMA'))
    return sorted(errors, key=lambda e: (e['path'], e['reason']))


def _duplicates(items, key, path):
    seen = set()
    errors = []
    for i, item in enumerate(items):
        if item[key] in seen:
            errors.append(_issue(f'{path}/{i}/{key}', 'Duplicate semantic identity'))
        seen.add(item[key])
    return errors


def validate_observation(value):
    """校验单帧；任务追踪缺失用 quest 的 null 字段与 unknown 表达。"""
    errors = _errors('observation', value)
    if not errors:
        errors += _duplicates(value['interactables'], 'semantic_id', '/interactables')
        errors += _duplicates(value['panels'], 'name', '/panels')
    return _result(errors)


def validate_task_state(value):
    """校验候选状态，不解析任务文案、不更新阶段。"""
    return _result(_errors('task-state', value))


def _intent_hash(session_id, attempt_no, kind, semantic_id, observation_id):
    payload = json.dumps([session_id, attempt_no, kind, semantic_id, observation_id],
                         ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def make_intent_id(session_id, attempt_no, kind, semantic_id, observation_id):
    """无分隔歧义的 UTF-8 JSON 数组哈希；会话作用域由调用者提供。"""
    checks = [('/session_id', isinstance(session_id, str) and bool(session_id)),
              ('/attempt_no', type(attempt_no) is int and attempt_no >= 0),
              ('/kind', kind in ('click_semantic', 'key', 'hover_probe')),
              ('/target/semantic_id', isinstance(semantic_id, str) and bool(semantic_id)),
              ('/origin_observation/observation_id', isinstance(observation_id, str) and
               bool(re.fullmatch(r'[0-7][0-9A-HJKMNP-TV-Z]{25}', observation_id)))]
    errors = [_issue(path, 'Invalid intent identity component', 'E_SCHEMA') for path, ok in checks if not ok]
    for path, value in (('/session_id', session_id), ('/target/semantic_id', semantic_id)):
        if isinstance(value, str):
            problem = _json_problem(value, path)
            if problem:
                errors.append(problem)
    return _result(errors, None if errors else _intent_hash(session_id, attempt_no, kind, semantic_id, observation_id))


def validate_action_intent(value, *, session_id=None):
    """结构及跨字段 TTL/相关性检查，不判断是否可真实执行。"""
    errors = _errors('action-intent', value)
    if not errors:
        origin = value['origin_observation']
        created = _time(value['created_at'])
        expires = _time(value['expires_at'])
        delta = expires - created
        if (delta.days * 86400000000 + delta.seconds * 1000000 + delta.microseconds) != value['ttl_ms'] * 1000:
            errors.append(_issue('/expires_at', 'Must equal created_at + ttl_ms'))
        if _time(origin['captured_at']) > created:
            errors.append(_issue('/origin_observation/captured_at', 'Origin cannot be captured after proposal'))
        if session_id is not None:
            correlation = make_intent_id(session_id, value['attempt_no'], value['kind'],
                                         value['target']['semantic_id'], origin['observation_id'])
            if not correlation['Ok']:
                errors += correlation['Data']['errors']
            elif value['intent_id'] != correlation['Data']:
                errors.append(_issue('/intent_id', 'Does not match deterministic attempt identity'))
    return _result(errors)


def validate_verification_result(value):
    """只校验所声明证据的自洽性；回执不是判定依据，也不应用 patch。"""
    errors = _errors('verification-result', value)
    if not errors and value['verdict'] in ('confirmed', 'rejected'):
        if value['before_observation_id'] == value['after_observation_id']:
            errors.append(_issue('/after_observation_id', 'Requires two independent observations'))
    return _result(errors)


def _collect(*results):
    return [error for result in results if not result['Ok'] for error in result['Data']['errors']]


def validate_observation_pair(before, after):
    """相邻采集事件恰好 +1；截图内容不参与序号分配。"""
    errors = _collect(validate_observation(before), validate_observation(after))
    if not errors:
        if after['observation_seq'] != before['observation_seq'] + 1:
            errors.append(_issue('/observation_seq', 'Adjacent captures must increment by one'))
        if after['observation_id'] == before['observation_id']:
            errors.append(_issue('/observation_id', 'New capture must have a fresh id'))
        if _time(after['captured_at']) < _time(before['captured_at']):
            errors.append(_issue('/captured_at', 'Capture time must not go backwards'))
    return _result(errors)


def _semantic(state, obs):
    dialog = obs['dialog'] or {}
    # 证据与操作资格也是语义状态；未决问题按集合比较，保守地全部计入。
    return (state['stage'], state['stage_source'],
            state['objective']['text'], state['objective']['evidence'],
            json.dumps(state['pending_decision'], sort_keys=True, ensure_ascii=False),
            sorted(state['open_questions']), obs['ui_mode'],
            sorted(p['name'] for p in obs['panels']), dialog.get('text_hash'),
            sorted(dialog.get('options', [])),
            sorted((i['semantic_id'], i['label']) for i in obs['interactables']),
            {k: (json.dumps(v['value'], sort_keys=True, ensure_ascii=False), v['evidence'])
             for k, v in state['flags'].items()})


def validate_revision(before_state, after_state, before_observation, after_observation):
    """校验已被调用者采纳的候选状态版本；不执行 M2 状态回放。"""
    errors = _collect(validate_task_state(before_state), validate_task_state(after_state),
                      validate_observation_pair(before_observation, after_observation))
    if not errors:
        for key in ('session_id', 'task_id'):
            if before_state[key] != after_state[key]:
                errors.append(_issue('/' + key, 'Cannot compare different task/session histories'))
        for state, obs in ((before_state, before_observation), (after_state, after_observation)):
            if state['observation_seq'] != obs['observation_seq']:
                errors.append(_issue('/observation_seq', 'State must consume the corresponding capture'))
        quest = after_observation['quest']
        observed_objective = quest['confidence'] == 'observed' and quest['objective_text'] is not None
        if after_state['stage_source'] == 'observed':
            if not observed_objective:
                errors.append(_issue('/stage_source', 'Observed stage requires current observed objective_text'))
            if (after_state['objective']['text'] != quest['objective_text'] or
                    after_state['objective']['evidence'] != 'observed'):
                errors.append(_issue('/objective', 'Observed stage must cite the current observed objective_text'))
        if after_state['stage'] != before_state['stage']:
            if not observed_objective or after_state['stage_source'] != 'observed':
                errors.append(_issue('/stage', 'Stage migration requires current observed objective_text'))
        if quest['confidence'] == 'unknown':
            if after_state['stage'] != before_state['stage']:
                errors.append(_issue('/stage', 'Missing tracker cannot establish a new stage'))
            expected_source = 'unknown' if after_state['stage'] == 'unknown' else 'inferred'
            if after_state['stage_source'] != expected_source:
                errors.append(_issue('/stage_source', 'Missing tracker cannot establish observed stage'))
            if not after_state['open_questions']:
                errors.append(_issue('/open_questions', 'Missing tracker must remain an explicit uncertainty'))
        changed = _semantic(before_state, before_observation) != _semantic(after_state, after_observation)
        if after_state['revision'] != before_state['revision'] + int(changed):
            errors.append(_issue('/revision', 'Must increment exactly once iff accepted semantics changed'))
    return _result(errors)


def _approval_valid(approval, intent, now):
    # 批准只作为调用者提供的数据；本模块不认证人类身份，也不签发批准。
    if not isinstance(approval, dict):
        return False
    if approval.get('intent_id') != intent['intent_id'] or approval.get('decision') != 'approved':
        return False
    if not isinstance(approval.get('approved_by'), str) or not approval['approved_by'].strip():
        return False
    for field in ('approved_at', 'expires_at'):
        if not isinstance(approval.get(field), str) or not _timestamp(approval[field]):
            return False
    return _time(intent['created_at']) <= _time(approval['approved_at']) <= now <= _time(approval['expires_at'])


def validate_attempt(intent, state, origin, guard, *, history, now,
                     guard_stage, preconditions_hold, approval=None):
    """离线尝试一致性分类，不截图、不重解析坐标、不执行、不记录历史。

    history 是本会话此前所有已提交尝试（含丢弃尝试）的 intent 列表。
    preconditions_hold 是上游对新帧重新解析后的布尔结果，非执行授权。
    """
    errors = _collect(validate_task_state(state), validate_action_intent(intent, session_id=state.get('session_id') if isinstance(state, dict) else None),
                      validate_observation(origin), validate_observation(guard))
    if not isinstance(history, list):
        errors.append(_issue('/history', 'Expected session-scoped list', 'E_SCHEMA'))
    else:
        for previous in history:
            errors += _collect(validate_action_intent(previous, session_id=state.get('session_id') if isinstance(state, dict) else None))
    if not isinstance(now, str) or not _timestamp(now):
        errors.append(_issue('/now', 'Expected RFC3339 timestamp', 'E_SCHEMA'))
    if not isinstance(guard_stage, str) or not guard_stage:
        errors.append(_issue('/guard_stage', 'Expected re-parsed stage or unknown', 'E_SCHEMA'))
    if type(preconditions_hold) is not bool:
        errors.append(_issue('/preconditions_hold', 'Expected explicit boolean', 'E_SCHEMA'))
    if errors:
        return _result(errors)
    if any(p['intent_id'] == intent['intent_id'] for p in history):
        return _result([_issue('/intent_id', 'Attempt was already submitted', 'E_DUPLICATE_ATTEMPT')])
    if history and intent['attempt_no'] != max(p['attempt_no'] for p in history) + 1:
        return _result([_issue('/attempt_no', 'Next proposal must increment attempt_no by one')])
    clock = _time(now)
    stale = []
    def require(ok, path, reason):
        if not ok:
            stale.append(_issue(path, reason, 'E_STALE_INTENT'))
    if history:
        require(origin['observation_seq'] > max(p['origin_observation']['observation_seq'] for p in history) and
                all(origin['observation_id'] != p['origin_observation']['observation_id'] for p in history),
                '/origin_observation', 'New attempt requires a fresh origin capture')
    require(_time(intent['created_at']) <= clock <= _time(intent['expires_at']), '/expires_at', 'Outside intent lifetime')
    require(intent['state_revision'] == state['revision'], '/state_revision', 'Semantic state revision changed')
    origin_ref = {k: origin[k] for k in ('observation_id', 'observation_seq', 'captured_at')}
    origin_ref['window'] = {k: origin['window'][k] for k in ('hwnd', 'pid', 'title_hash', 'rect')}
    require(intent['origin_observation'] == origin_ref, '/origin_observation', 'Origin binding differs from supplied observation')
    require(guard['observation_seq'] > origin['observation_seq'] and guard['observation_id'] != origin['observation_id'], '/observation_seq', 'Guard requires a fresh capture')
    require(_time(origin['captured_at']) <= _time(guard['captured_at']) <= clock, '/captured_at', 'Guard capture time is inconsistent')
    require(state['observation_seq'] >= origin['observation_seq'] and state['observation_seq'] <= guard['observation_seq'], '/observation_seq', 'State is outside origin/guard capture interval')
    for key in ('hwnd', 'pid', 'title_hash'):
        require(guard['window'][key] == origin['window'][key] == state['window'][key], '/window/' + key, 'Window identity changed')
    require(guard['window']['rect'] == origin['window']['rect'], '/window/rect', 'Window geometry changed')
    expected_stages = [p['expected'] for p in intent['preconditions'] if p['path'] == '/stage']
    require(expected_stages == [state['stage']] and guard_stage == state['stage'] and guard_stage != 'unknown', '/preconditions/stage', 'Guard stage differs from proposal stage or is unknown')
    require(preconditions_hold, '/preconditions', 'Re-evaluated preconditions do not hold')
    if stale:
        return _result(stale)
    if intent['requires_human_approval'] and not _approval_valid(approval, intent, clock):
        return _result([_issue('/approval', 'Valid attempt-bound approval is required', 'E_APPROVAL_REQUIRED')])
    return _result()
