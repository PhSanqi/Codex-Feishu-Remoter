"""Feishu Approval Card JSON 2.0 builder and callback boundary."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from cfr.core.models import StructuredError

from .log_sanitize import sanitize_feishu_log_text


_CALLBACK_KEYS = {'action', 'approval_id', 'version', 'nonce', 'idempotency_key'}
_ALLOWED_ACTIONS = {'allow_once', 'decline'}
_APPROVAL_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
FEEDBACK_STATES = {
    'PENDING': 0,
    'ACKNOWLEDGED_PROCESSING': 2,
    'APPROVED': 3,
    'DECLINED': 3,
    'EXECUTION_FAILED': 3,
}


@dataclass(frozen=True)
class ApprovalCardPayload:
    schema_version: str
    payload: dict[str, Any]


def _bounded(value: Any, limit: int, fallback: str = '<not provided>') -> str:
    text = sanitize_feishu_log_text(str(value or '')).strip()
    return (text[:limit] if text else fallback)


def _workspace_name(cwd: Any) -> str:
    text = _bounded(cwd, 160, '<unknown workspace>')
    return text.replace('\\', '/').rstrip('/').split('/')[-1] or '<unknown workspace>'


def _operation_summary(request: Any) -> str:
    changed_paths = tuple(getattr(request, 'changed_paths', ()) or ())
    if changed_paths:
        return f'{getattr(request, "kind", "operation")} affecting {len(changed_paths)} path(s)'
    command = _bounded(getattr(request, 'command', None), 240)
    return f'{getattr(request, "kind", "operation")}: {command}'


def build_approval_feedback_card(
    request: Any,
    approval_id: str,
    state: str = 'PENDING',
    decision: str | None = None,
    feedback: str | None = None,
    execution_error: str | None = None,
) -> ApprovalCardPayload:
    """Render every approval-card state through one Card 2.0 builder."""
    if not isinstance(approval_id, str) or not _APPROVAL_ID.fullmatch(approval_id):
        raise StructuredError('FEISHU_APPROVAL_ID_INVALID', 'Approval card identity is invalid')
    if state not in FEEDBACK_STATES:
        raise StructuredError('FEISHU_APPROVAL_FEEDBACK_STATE_INVALID', 'Approval feedback state is invalid')
    if state == 'PENDING' and decision is not None:
        raise StructuredError('FEISHU_APPROVAL_FEEDBACK_DECISION_INVALID', 'Pending approval cannot have a terminal decision')
    callback_allow = {'action': 'allow_once', 'approval_id': approval_id}
    callback_decline = {'action': 'decline', 'approval_id': approval_id}
    if state == 'PENDING':
        status = 'Status: Waiting for decision'
        title = 'CFR Approval Required'
    elif state == 'ACKNOWLEDGED_PROCESSING':
        received = 'Allow once' if decision == 'accept' else 'Decline' if decision == 'decline' else 'Decision'
        status = f'✓ {received} received\nStatus: Processing…'
        title = 'CFR Approval Processing'
    elif state == 'APPROVED':
        status = '✓ Approved\nStatus: Completed'
        title = 'CFR Approval Approved'
    elif state == 'DECLINED':
        status = 'Declined\nThe protected operation was not executed.'
        title = 'CFR Approval Declined'
    else:
        safe_error = _bounded(execution_error, 240, 'The protected operation did not complete.')
        status = f'Approved\nExecution failed\n{safe_error}'
        title = 'CFR Approval — Execution Failed'
    if feedback:
        status = f'{status}\n\n{_bounded(feedback, 240)}'
    summary_content = (
        f'**Type:** {_bounded(getattr(request, "kind", None), 80)}\n'
        f'**Workspace:** `{_workspace_name(getattr(request, "cwd", None))}`\n'
        f'**Thread:** `{_bounded(getattr(request, "thread_id", None), 80)}`\n'
        f'**Operation:** {_operation_summary(request)}\n'
        f'**Risk:** {_bounded(getattr(request, "reason", None), 240)}'
    )
    elements = [{'tag': 'markdown', 'content': summary_content + (f'\n{status}' if state == 'PENDING' else '')}]
    if state != 'PENDING':
        elements.append({'tag': 'markdown', 'content': status})
    if state == 'PENDING':
        elements.append({
            'tag': 'column_set',
            'flex_mode': 'bisect',
            'columns': [
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'elements': [{
                        'tag': 'button',
                        'element_id': 'cfr_allow',
                        'text': {'tag': 'plain_text', 'content': 'Allow once'},
                        'type': 'primary',
                        'behaviors': [{'type': 'callback', 'value': callback_allow}],
                    }],
                },
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'elements': [{
                        'tag': 'button',
                        'element_id': 'cfr_decline',
                        'text': {'tag': 'plain_text', 'content': 'Decline'},
                        'type': 'danger',
                        'behaviors': [{'type': 'callback', 'value': callback_decline}],
                    }],
                },
            ],
        })
    payload = {
        'schema': '2.0',
        'header': {
            'title': {'tag': 'plain_text', 'content': title},
            'template': 'orange' if state == 'PENDING' else 'blue',
        },
        'body': {'elements': elements},
    }
    return ApprovalCardPayload('2.0', payload)


def build_approval_card_v2(request: Any, approval_id: str) -> ApprovalCardPayload:
    """Build the initial approval card through the shared renderer."""
    return build_approval_feedback_card(request, approval_id, 'PENDING')


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_dicts(child)


def inspect_approval_card_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """Return bounded machine-checkable evidence for the V2 card contract."""
    nodes = tuple(_walk_dicts(payload))
    buttons = tuple(node for node in nodes if node.get('tag') == 'button')
    legacy_action = any(node.get('tag') == 'action' for node in nodes)
    callbacks = []
    for button in buttons:
        for behavior in button.get('behaviors', ()) or ():
            if isinstance(behavior, dict) and behavior.get('type') == 'callback':
                callbacks.append((button, behavior.get('value')))
    values = [value for _button, value in callbacks if isinstance(value, dict)]
    allow = next((value for value in values if value.get('action') == 'allow_once'), None)
    decline = next((value for value in values if value.get('action') == 'decline'), None)
    ids = {value.get('approval_id') for value in values}
    serialized = repr(payload).lower()
    forbidden_secret = any(token in serialized for token in ('app_secret', 'access_token', 'authorization', 'raw_json_rpc'))
    return {
        'ApprovalCardSchemaVersion': payload.get('schema'),
        'ApprovalCardLegacyActionTagPresent': legacy_action,
        'ApprovalCardButtonCount': len(buttons),
        'ApprovalCardAllowCallback': 'PASS' if allow and set(allow) <= _CALLBACK_KEYS and len(ids) == 1 else 'FAIL',
        'ApprovalCardDeclineCallback': 'PASS' if decline and set(decline) <= _CALLBACK_KEYS and len(ids) == 1 else 'FAIL',
        'ApprovalCardNoSecretFields': not forbidden_secret,
        'ApprovalCardV2Contract': (
            'PASS' if payload.get('schema') == '2.0' and not legacy_action and len(buttons) == 2
            and len(callbacks) == 2 and allow and decline and len(ids) == 1 and not forbidden_secret else 'FAIL'
        ),
    }


def inspect_approval_feedback_card(payload: dict[str, Any]) -> dict[str, Any]:
    """Return state-independent evidence for active callbacks and safe payloads."""
    nodes = tuple(_walk_dicts(payload))
    callbacks = tuple(
        behavior
        for node in nodes if node.get('tag') == 'button'
        for behavior in (node.get('behaviors') or ())
        if isinstance(behavior, dict) and behavior.get('type') == 'callback'
    )
    serialized = repr(payload).lower()
    forbidden_secret = any(token in serialized for token in ('app_secret', 'access_token', 'authorization', 'raw_json_rpc'))
    return {
        'ApprovalCardSchemaVersion': payload.get('schema'),
        'ApprovalFeedbackActiveButtons': len(callbacks),
        'ApprovalFeedbackNoSecretFields': not forbidden_secret,
        'ApprovalFeedbackLegacyActionTagPresent': any(node.get('tag') == 'action' for node in nodes),
    }


def parse_v2_card_callback(value: Any, operator_open_id: Any, tag: Any = 'button') -> tuple[str, str, str]:
    """Validate a real Card 2.0 callback before it reaches ApprovalBridge."""
    if tag != 'button' or not operator_open_id or not isinstance(value, dict):
        raise StructuredError('FEISHU_CARD_ACTION_INVALID', 'Card V2 callback is malformed')
    if set(value) - _CALLBACK_KEYS:
        raise StructuredError('FEISHU_CARD_ACTION_INVALID', 'Card V2 callback contains unsupported fields')
    action = value.get('action')
    approval_id = value.get('approval_id')
    if action not in _ALLOWED_ACTIONS or not isinstance(approval_id, str) or not _APPROVAL_ID.fullmatch(approval_id):
        raise StructuredError('FEISHU_CARD_ACTION_INVALID', 'Card V2 callback action or approval id is invalid')
    return str(action), approval_id, str(operator_open_id)
