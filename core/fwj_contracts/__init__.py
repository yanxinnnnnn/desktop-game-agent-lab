"""FWJ v1.1 纯离线契约；不访问窗口、不应用状态、不执行动作。"""

from .validation import (
    make_intent_id,
    validate_action_intent,
    validate_attempt,
    validate_observation,
    validate_observation_pair,
    validate_revision,
    validate_task_state,
    validate_verification_result,
)

__all__ = [
    'make_intent_id', 'validate_action_intent', 'validate_attempt',
    'validate_observation', 'validate_observation_pair', 'validate_revision',
    'validate_task_state', 'validate_verification_result',
]
