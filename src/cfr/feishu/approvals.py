from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
import json
import re
import threading
import time
import uuid

from cfr.codex.approvals import ApprovalRequest, CodexApprovalCodec
from cfr.core.models import StructuredError

from .approval_card import (
    build_approval_feedback_card,
    inspect_approval_card_v2,
    inspect_approval_feedback_card,
)
from .config import FeishuSettings
from .log_sanitize import sanitize_feishu_log_text
from .models import FeishuExecutionContext
from .replies import FeishuReplyClient
from .store import FeishuStore


APPROVAL_EVIDENCE_LIMIT = 200


def first_present(mapping, *keys):
    """Return the first present, non-None value; numeric zero is valid."""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def extract_turn_terminal_identity(message):
    """Extract the provider's nested terminal turn identity without guessing."""
    if not isinstance(message, dict):
        return None
    params = message.get('params') or {}
    turn = params.get('turn') or {}
    thread_id = first_present(params, 'threadId', 'thread_id')
    turn_id = first_present(turn, 'id', 'turnId') or first_present(params, 'turnId', 'turn_id')
    turn_status = first_present(turn, 'status') or first_present(params, 'status')
    if thread_id is None or turn_id is None or turn_status is None:
        return None
    return {
        'thread_id': str(thread_id),
        'turn_id': str(turn_id),
        'turn_status': str(turn_status),
    }


@dataclass
class _PendingApproval:
    approval_id: str
    event: threading.Event
    request_id: str | None = None
    thread_id: str | None = None
    decision: str | None = None
    request: ApprovalRequest | None = None


@dataclass
class _PendingUserInput:
    request_id: str
    chat_id: str
    requester_open_id: str
    thread_id: str
    turn_id: str
    questions: tuple[dict, ...]
    event: threading.Event
    answers: dict | None = None
    cancelled: bool = False


class ApprovalBridge:
    def __init__(self, store: FeishuStore, replies: FeishuReplyClient, settings: FeishuSettings):
        self.store = store
        self.replies = replies
        self.settings = settings
        self.codec = CodexApprovalCodec()
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = threading.RLock()
        self._card_send_evidence: OrderedDict[str, dict] = OrderedDict()
        self._card_contract_evidence: OrderedDict[str, dict] = OrderedDict()
        self._feedback_evidence: OrderedDict[str, dict] = OrderedDict()
        self._requests: OrderedDict[str, ApprovalRequest] = OrderedDict()
        self._feedback_futures = set()
        self._feedback_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='cfr-feishu-feedback')
        self._pending_user_inputs: dict[str, _PendingUserInput] = {}

    @staticmethod
    def _trim(mapping):
        while len(mapping) > APPROVAL_EVIDENCE_LIMIT:
            mapping.popitem(last=False)

    def _card(self, request: ApprovalRequest, approval_id: str):
        return build_approval_feedback_card(request, approval_id, 'PENDING').payload

    def card_send_evidence(self, approval_id: str) -> dict:
        with self._lock:
            return dict(self._card_send_evidence.get(approval_id, {}))

    def card_contract_evidence(self, approval_id: str) -> dict:
        with self._lock:
            return dict(self._card_contract_evidence.get(approval_id, {}))

    def feedback_evidence(self, approval_id: str) -> dict:
        row = self.store.get_approval(approval_id) or {}
        with self._lock:
            evidence = dict(self._feedback_evidence.get(approval_id, {}))
        evidence.update({
            'ApprovalFeedbackStateInitial': 'PENDING',
            'ApprovalFeedbackStateAfterDecision': evidence.get('ApprovalFeedbackStateAfterDecision') or (row.get('feedback_state') if row.get('feedback_state') == 'ACKNOWLEDGED_PROCESSING' else None),
            'ApprovalFeedbackStateFinal': evidence.get('ApprovalFeedbackStateFinal') or (row.get('feedback_state') if row.get('feedback_state') in {'APPROVED', 'DECLINED', 'EXECUTION_FAILED'} else None),
            'ApprovalFeedbackMessageIdBound': bool(row.get('card_message_id')),
            'ApprovalFeedbackUpdateFailed': bool(row.get('feedback_update_failed')),
            'ApprovalFeedbackUpdateErrorCode': row.get('feedback_update_error_code'),
            'ApprovalFeedbackRevision': row.get('feedback_revision', 0),
        })
        return {key: value for key, value in evidence.items() if value is not None}

    def _request_for_row(self, row):
        approval_id = row.get('approval_id')
        with self._lock:
            request = self._requests.get(approval_id)
            if request is not None:
                self._requests.move_to_end(approval_id)
        if request is not None:
            return request
        try:
            summary = json.loads(row.get('summary_json') or '{}')
        except (TypeError, ValueError):
            summary = {}
        request = ApprovalRequest(
            str(row.get('codex_request_id') or ''), str(row.get('thread_id') or ''), row.get('turn_id'), None,
            str(row.get('kind') or 'operation'), summary.get('reason'), summary.get('command'), summary.get('cwd'),
            tuple(summary.get('changed_paths') or ()), ('accept', 'decline'),
            grant_root=summary.get('grant_root'),
            method=summary.get('method'),
            native_kind=summary.get('native_kind'),
            command_actions=tuple(item for item in (summary.get('command_actions') or ()) if isinstance(item, dict)),
            requested_permissions=summary.get('requested_permissions') if isinstance(summary.get('requested_permissions'), dict) else None,
            network_approval_context=summary.get('network_approval_context') if isinstance(summary.get('network_approval_context'), dict) else None,
            proposed_execpolicy_amendment=tuple(summary.get('proposed_execpolicy_amendment') or ()),
            proposed_network_policy_amendments=tuple(item for item in (summary.get('proposed_network_policy_amendments') or ()) if isinstance(item, dict)),
        )
        with self._lock:
            existing = self._requests.setdefault(row['approval_id'], request)
            self._requests.move_to_end(row['approval_id'])
            self._trim(self._requests)
            return existing

    def _record_feedback_attempt(self, approval_id, state, attempted, succeeded=None, latency_ms=None, payload=None, stale=False, feedback=None):
        with self._lock:
            evidence = self._feedback_evidence.setdefault(approval_id, {})
            self._feedback_evidence.move_to_end(approval_id)
            self._trim(self._feedback_evidence)
            card_evidence = inspect_approval_feedback_card(payload or {})
            evidence.update({
                'ApprovalFeedbackStateAttempted': state,
                'ApprovalFeedbackOriginalCardUpdated': bool(succeeded),
                'ApprovalFeedbackMonotonicStateContract': 'PASS',
                'ApprovalFeedbackUpdateFailed': attempted and succeeded is False,
                'ApprovalFeedbackUpdateStaleSkipped': stale,
                'ApprovalFeedbackLastFeedback': feedback,
                'ApprovalFeedbackCardSchemaVersion': card_evidence.get('ApprovalCardSchemaVersion'),
                'ApprovalFeedbackActiveButtonsLast': card_evidence.get('ApprovalFeedbackActiveButtons'),
            })
            if state == 'ACKNOWLEDGED_PROCESSING':
                evidence.update({
                    'ApprovalFeedbackAckAttempted': attempted,
                    'ApprovalFeedbackAckSucceeded': succeeded,
                    'ApprovalFeedbackAckLatencyMs': latency_ms,
                    'ApprovalFeedbackActiveButtonsAfterAck': card_evidence.get('ApprovalFeedbackActiveButtons') if succeeded else None,
                })
                if succeeded:
                    evidence['ApprovalFeedbackStateAfterDecision'] = state
            elif state in {'APPROVED', 'DECLINED', 'EXECUTION_FAILED'}:
                evidence.update({
                    'ApprovalFeedbackFinalUpdateAttempted': attempted,
                    'ApprovalFeedbackFinalUpdateSucceeded': succeeded,
                    'ApprovalFeedbackFinalLatencyMs': latency_ms,
                    'ApprovalFeedbackActiveButtonsFinal': card_evidence.get('ApprovalFeedbackActiveButtons') if succeeded else None,
                    'ApprovalFeedbackStateFinal': state if succeeded else evidence.get('ApprovalFeedbackStateFinal'),
                })

    def _update_feedback(self, approval_id, state, decision, feedback=None, execution_error=None):
        started = time.monotonic()
        row = self.store.get_approval(approval_id)
        if not row or not row.get('card_message_id'):
            self.store.mark_feedback_update_failed(approval_id, 'FEISHU_ORIGINAL_CARD_MESSAGE_ID_MISSING')
            self._record_feedback_attempt(approval_id, state, True, False, round((time.monotonic() - started) * 1000, 2), feedback=feedback)
            return False
        if not self.store.transition_feedback(approval_id, state):
            self._record_feedback_attempt(approval_id, state, False, True, 0, stale=True, feedback=feedback)
            return True
        request = self._request_for_row(row)
        card = build_approval_feedback_card(request, approval_id, state, decision, feedback, execution_error).payload
        try:
            self.replies.update_card(row['card_message_id'], card)
        except Exception as exc:
            code = getattr(exc, 'code', type(exc).__name__)
            self.store.mark_feedback_update_failed(approval_id, code)
            self._record_feedback_attempt(approval_id, state, True, False, round((time.monotonic() - started) * 1000, 2), payload=card, feedback=feedback)
            return False
        self.store.mark_feedback_update_succeeded(approval_id)
        self._record_feedback_attempt(approval_id, state, True, True, round((time.monotonic() - started) * 1000, 2), payload=card, feedback=feedback)
        return True

    def _schedule_feedback_update(self, approval_id, state, decision, feedback=None, execution_error=None):
        future = self._feedback_executor.submit(self._update_feedback, approval_id, state, decision, feedback, execution_error)
        with self._lock:
            self._feedback_futures.add(future)

        def done(completed):
            with self._lock:
                self._feedback_futures.discard(completed)

        future.add_done_callback(done)
        return future

    def wait_for_feedback(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                futures = tuple(self._feedback_futures)
            if not futures:
                return True
            for future in futures:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    future.result(timeout=remaining)
                except Exception:
                    pass

    def wait_for_final_feedback(self, approval_id, timeout=10.0):
        """Poll durable state until the original card reaches a terminal state."""
        terminal = {'APPROVED', 'DECLINED', 'EXECUTION_FAILED'}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.wait_for_feedback(timeout=min(0.25, max(0.0, deadline - time.monotonic())))
            row = self.store.get_approval(approval_id) or {}
            if row.get('feedback_state') in terminal:
                return True
            time.sleep(0.02)
        row = self.store.get_approval(approval_id) or {}
        return row.get('feedback_state') in terminal

    def finalize_feedback(self, approval_id, execution_succeeded=True, execution_error=None):
        row = self.store.get_approval(approval_id)
        if not row or row.get('decision') not in {'accept', 'decline'}:
            return False
        state = 'DECLINED' if row['decision'] == 'decline' else ('APPROVED' if execution_succeeded else 'EXECUTION_FAILED')
        self._schedule_feedback_update(approval_id, state, row['decision'], execution_error=execution_error)
        return self.wait_for_feedback(timeout=5.0)

    def finalize_feedback_for_turn(self, *, thread_id, turn_id, turn_status, error=None, trigger_source='TURN_RESULT'):
        """Finalize all decisions bound to one authoritative terminal turn."""
        rows = self.store.list_approvals_for_turn(thread_id, turn_id)
        status = '' if turn_status is None else str(turn_status).lower()
        execution_succeeded = status == 'completed'
        terminal_states = {'APPROVED', 'DECLINED', 'EXECUTION_FAILED'}
        eligible = [row for row in rows if row.get('decision') in {'accept', 'decline'}]
        triggered = False
        already_final = False
        for row in eligible:
            if row.get('feedback_state') in terminal_states:
                already_final = True
                continue
            triggered = True
            execution_error = error
            if row.get('decision') == 'accept' and not execution_succeeded and not execution_error:
                execution_error = f'Turn ended with status {turn_status}'
            self.finalize_feedback(
                row['approval_id'],
                execution_succeeded=execution_succeeded,
                execution_error=execution_error,
            )
        self.wait_for_feedback(timeout=5.0)
        observed = True if eligible else False
        wait_timed_out = False
        for row in eligible:
            current = self.store.get_approval(row['approval_id']) or {}
            if current.get('feedback_state') not in terminal_states:
                observed = False
                wait_timed_out = True
            with self._lock:
                evidence = self._feedback_evidence.setdefault(row['approval_id'], {})
                self._feedback_evidence.move_to_end(row['approval_id'])
                self._trim(self._feedback_evidence)
                evidence.update({
                    'ApprovalFeedbackFinalizationAuthority': (
                        'IDEMPOTENT_ALREADY_FINAL' if not triggered and already_final else trigger_source
                    ),
                    'ApprovalFeedbackTurnIdObserved': str(turn_id),
                    'ApprovalFeedbackThreadIdObserved': str(thread_id),
                    'ApprovalFeedbackTurnStatusObserved': str(turn_status),
                    'ApprovalFeedbackTurnBindingMatchedCount': len(rows),
                    'ApprovalFeedbackFinalizationTriggered': triggered,
                    'ApprovalFeedbackFinalizationTriggerSource': trigger_source,
                    'ApprovalFeedbackFinalizationObserved': observed,
                    'ApprovalFeedbackFinalizationWaitTimedOut': wait_timed_out,
                })
        return {
            'thread_id': str(thread_id),
            'turn_id': str(turn_id),
            'turn_status': str(turn_status),
            'matched_count': len(rows),
            'eligible_count': len(eligible),
            'triggered': triggered,
            'observed': observed,
            'wait_timed_out': wait_timed_out,
        }

    def close(self):
        self.cancel_all_pending()
        self.wait_for_feedback(timeout=5.0)
        self._feedback_executor.shutdown(wait=True, cancel_futures=False)

    @staticmethod
    def _user_input_prompt(questions):
        lines = ['Codex 需要你的输入。请直接回复这条会话中的下一条文本；CFR 会把它作为当前任务的回答，不会创建新任务。']
        if len(questions) > 1:
            lines.append('多问题请按“1: 回答”“2: 回答”的格式逐行回复。')
        for index, question in enumerate(questions, 1):
            header = str(question.get('header') or f'问题 {index}').strip()
            text = str(question.get('question') or '').strip()
            lines.append(f'\n{index}. {header}\n{text}')
            options = question.get('options') or ()
            for option_index, option in enumerate(options, 1):
                if not isinstance(option, dict):
                    continue
                label = str(option.get('label') or '').strip()
                description = str(option.get('description') or '').strip()
                letter = chr(ord('A') + option_index - 1) if option_index <= 26 else str(option_index)
                lines.append(f'   {letter}. {label}' + (f' — {description}' if description else ''))
            if question.get('isOther'):
                lines.append('   也可以直接输入其他答案。')
        return '\n'.join(lines).strip()

    @staticmethod
    def _normalize_question_answer(question, raw):
        value = str(raw or '').strip()
        options = [item for item in (question.get('options') or ()) if isinstance(item, dict)]
        if not options or not value:
            return value
        upper = value.upper()
        if len(upper) == 1 and 'A' <= upper <= 'Z':
            index = ord(upper) - ord('A')
            if index < len(options):
                return str(options[index].get('label') or value)
        if value.isdigit():
            index = int(value) - 1
            if 0 <= index < len(options):
                return str(options[index].get('label') or value)
        for option in options:
            label = str(option.get('label') or '').strip()
            if label and value.casefold() == label.casefold():
                return label
        return value

    @classmethod
    def _parse_user_input_answers(cls, questions, text):
        questions = tuple(questions or ())
        raw = str(text or '').strip()
        if not questions or not raw:
            return None
        indexed = {}
        if len(questions) > 1:
            for line in raw.splitlines():
                match = re.match(r'^\s*(\d+)\s*[:：.)、-]\s*(.+?)\s*$', line)
                if match:
                    indexed[int(match.group(1))] = match.group(2)
            if len(indexed) < len(questions):
                return None
        answers = {}
        for index, question in enumerate(questions, 1):
            value = raw if len(questions) == 1 else indexed.get(index)
            if value is None:
                return None
            answer = cls._normalize_question_answer(question, value)
            question_id = str(question.get('id') or index)
            answers[question_id] = {'answers': [answer]}
        return answers

    def handle_user_input_request(self, message, context: FeishuExecutionContext):
        params = message.get('params') or {}
        request_id = message.get('id')
        questions = tuple(item for item in (params.get('questions') or ()) if isinstance(item, dict))
        thread_id = str(params.get('threadId') or context.thread_id or '')
        turn_id = str(params.get('turnId') or '')
        if request_id is None or not thread_id or not turn_id or not questions:
            return {'error': {'code': -32602, 'message': 'CODEX_USER_INPUT_REQUEST_INVALID'}}
        if any(bool(question.get('isSecret')) for question in questions):
            try:
                self.replies.reply_text(
                    context.message_id,
                    'Codex 请求了 secret 输入。CFR 不会通过飞书收集密码、令牌或其他秘密值，因此已拒绝该输入请求。',
                    f'user-input-secret:{request_id}',
                    chat_id=context.chat_id,
                )
            except Exception:
                pass
            return {'error': {'code': -32010, 'message': 'CFR_SECRET_USER_INPUT_UNSUPPORTED'}}
        pending = _PendingUserInput(
            request_id=str(request_id),
            chat_id=context.chat_id,
            requester_open_id=context.sender_open_id,
            thread_id=thread_id,
            turn_id=turn_id,
            questions=questions,
            event=threading.Event(),
        )
        with self._lock:
            if context.chat_id in self._pending_user_inputs:
                return {'error': {'code': -32011, 'message': 'CFR_USER_INPUT_ALREADY_PENDING'}}
            self._pending_user_inputs[context.chat_id] = pending
        try:
            self.replies.reply_text(
                context.message_id,
                self._user_input_prompt(questions),
                f'user-input:{request_id}',
                chat_id=context.chat_id,
            )
        except Exception:
            with self._lock:
                self._pending_user_inputs.pop(context.chat_id, None)
            return {'error': {'code': -32012, 'message': 'CFR_USER_INPUT_PROMPT_DELIVERY_FAILED'}}
        timeout = min(float(self.settings.approval_timeout_seconds), 24 * 60 * 60)
        pending.event.wait(timeout)
        with self._lock:
            self._pending_user_inputs.pop(context.chat_id, None)
        if pending.cancelled:
            return {'error': {'code': -32013, 'message': 'CFR_USER_INPUT_CANCELLED'}}
        if pending.answers is None:
            return {'error': {'code': -32014, 'message': 'CFR_USER_INPUT_TIMEOUT'}}
        return {'answers': pending.answers}

    def consume_user_input_message(self, message) -> bool:
        text = str(getattr(message, 'text', '') or '').strip()
        if getattr(message, 'message_type', None) != 'text' or not text:
            return False
        # CFR control commands must remain available while Codex is waiting for
        # input, especially /stop. Do not swallow them as model answers.
        if text.startswith('/'):
            return False
        chat_id = str(getattr(message, 'chat_id', '') or '')
        sender = str(getattr(message, 'sender_open_id', '') or '')
        with self._lock:
            pending = self._pending_user_inputs.get(chat_id)
        if pending is None or sender != pending.requester_open_id:
            return False
        if not self.store.enqueue_message(message):
            return True
        answers = self._parse_user_input_answers(pending.questions, message.text)
        if answers is None:
            self.store.mark_ignored(message.message_id, 'CODEX_USER_INPUT_INVALID', 'Reply did not match the pending Codex user-input question')
            try:
                self.replies.reply_text(
                    message.message_id,
                    '这条回复无法匹配当前 Codex 输入问题。请按提示的编号格式重新回复。',
                    f'user-input-invalid:{pending.request_id}',
                    chat_id=chat_id,
                )
            except Exception:
                pass
            return True
        pending.answers = answers
        self.store.mark_completed(message.message_id, None)
        pending.event.set()
        return True

    def handle_server_request(self, message, context: FeishuExecutionContext):
        method = message.get('method') if isinstance(message, dict) else None
        if method == 'item/tool/requestUserInput':
            return self.handle_user_input_request(message, context)
        if method == 'currentTime/read':
            return {'currentTimeAt': int(time.time())}
        if method in {
            'item/tool/call',
            'mcpServer/elicitation/request',
            'account/chatgptAuthTokens/refresh',
            'attestation/generate',
        }:
            return {'error': {'code': -32601, 'message': f'{method} is not advertised by CFR'}}
        params = message.get('params') or {}
        thread_id = first_present(params, 'threadId', 'thread_id')
        if thread_id is None:
            thread_id = context.thread_id
        request = self.codec.decode(message, str(thread_id or ''))
        if request is None:
            raise StructuredError('UNSUPPORTED_CODEX_SERVER_REQUEST', 'Unknown Codex server request rejected')
        if not request.cwd and context.cwd:
            request = replace(request, cwd=context.cwd)
        approval_id = uuid.uuid4().hex
        expires = time.time() + self.settings.approval_timeout_seconds
        summary = json.dumps({
            'reason': request.reason,
            'cwd': request.cwd,
            'command': (request.command or '')[:1000],
            'changed_paths': request.changed_paths,
            'grant_root': request.grant_root,
            'method': request.method,
            'native_kind': request.native_kind,
            'command_actions': request.command_actions,
            'requested_permissions': request.requested_permissions,
            'network_approval_context': request.network_approval_context,
            'proposed_execpolicy_amendment': request.proposed_execpolicy_amendment,
            'proposed_network_policy_amendments': request.proposed_network_policy_amendments,
        })
        self.store.create_approval(approval_id, request, context.sender_open_id, expires, summary)
        with self._lock:
            pending = _PendingApproval(approval_id, threading.Event(), request.request_id, request.thread_id, request=request)
            self._pending[approval_id] = pending
            self._requests[approval_id] = request
            self._requests.move_to_end(approval_id)
            self._trim(self._requests)
        try:
            card = self._card(request, approval_id)
            with self._lock:
                self._card_contract_evidence[approval_id] = inspect_approval_card_v2(card)
                self._card_contract_evidence.move_to_end(approval_id)
                self._trim(self._card_contract_evidence)
            response_id = self.replies.send_card(context.message_id, context.sender_open_id, card, phase=f'approval:{approval_id}')
            if not response_id:
                raise StructuredError('FEISHU_APPROVAL_CARD_SEND_FAILED', 'Feishu approval card was not delivered')
            if not self.store.bind_approval_card(approval_id, response_id):
                bound = self.store.get_approval(approval_id)
                if not bound or bound.get('card_message_id') != str(response_id):
                    raise StructuredError('FEISHU_ORIGINAL_CARD_BINDING_FAILED', 'Original Feishu card message id was not persisted')
            with self._lock:
                self._card_send_evidence[approval_id] = {
                    'FeishuSendSuccess': True,
                    'FeishuMessageIdPresent': True,
                    'FeishuErrorCode': None,
                    'FeishuCardErrorCode': None,
                    'FeishuErrorMessageSanitized': None,
                    'FeishuMessageId': str(response_id),
                }
                self._card_send_evidence.move_to_end(approval_id)
                self._trim(self._card_send_evidence)
                self._feedback_evidence[approval_id] = {
                    'ApprovalFeedbackStateInitial': 'PENDING',
                    'ApprovalFeedbackMessageIdBound': True,
                    'ApprovalFeedbackOriginalCardUpdated': False,
                }
                self._feedback_evidence.move_to_end(approval_id)
                self._trim(self._feedback_evidence)
        except Exception as exc:
            data = exc.data if isinstance(exc, StructuredError) and isinstance(exc.data, dict) else {}
            with self._lock:
                self._card_send_evidence[approval_id] = {
                    'FeishuSendSuccess': False,
                    'FeishuMessageIdPresent': False,
                    'FeishuErrorCode': data.get('provider_code') or getattr(exc, 'code', type(exc).__name__),
                    'FeishuCardErrorCode': data.get('provider_ext_code'),
                    'FeishuErrorMessageSanitized': sanitize_feishu_log_text(str(getattr(exc, 'message', exc)))[:240],
                    'FailureDisposition': 'decline',
                    'FailureDispositionReason': 'FEISHU_CARD_DELIVERY_FAILED',
                }
                self._card_send_evidence.move_to_end(approval_id)
                self._trim(self._card_send_evidence)
            with self._lock:
                self._pending.pop(approval_id, None)
            self.store.resolve_approval(approval_id, 'decline', 'cancelled')
            return self.codec.response('decline', request)
        pending.event.wait(self.settings.approval_timeout_seconds)
        # The card callback itself remains fast: it only schedules the update.
        # Before the approval request is released to Codex, make the local
        # projection deterministic so the original card cannot race the
        # business continuation or a test teardown.
        self.wait_for_feedback(timeout=5.0)
        with self._lock:
            self._pending.pop(approval_id, None)
        if pending.decision is None:
            self.store.expire_approval(approval_id)
            return self.codec.response('decline', request)
        return self.codec.response(pending.decision, request)

    def cancel_all_pending(self):
        with self._lock:
            pending = list(self._pending.values())
            pending_user_inputs = list(self._pending_user_inputs.values())
        for item in pending:
            self.store.resolve_approval(item.approval_id, 'cancel', 'cancelled')
            item.decision = 'cancel'
            item.event.set()
        for item in pending_user_inputs:
            item.cancelled = True
            item.event.set()

    def handle_server_notification(self, message):
        """Close approvals when app-server reports a resolved/terminal request."""
        method = message.get('method') if isinstance(message, dict) else None
        params = (message.get('params') or {}) if isinstance(message, dict) else {}
        raw_request_id = first_present(params, 'requestId', 'request_id', 'id')
        raw_thread_id = first_present(params, 'threadId', 'thread_id')
        request_id = '' if raw_request_id is None else str(raw_request_id)
        thread_id = '' if raw_thread_id is None else str(raw_thread_id)
        if method not in {'serverRequest/resolved', 'turn/completed', 'turn/interrupted'}:
            return 0
        with self._lock:
            matches = [
                item for item in self._pending.values()
                if (request_id and item.request_id == request_id) or (thread_id and item.thread_id == thread_id)
            ]
            input_matches = [
                item for item in self._pending_user_inputs.values()
                if (request_id and item.request_id == request_id)
                or (
                    method in {'turn/completed', 'turn/interrupted'}
                    and thread_id and item.thread_id == thread_id
                    and (not params.get('turnId') or item.turn_id == str(params.get('turnId')))
                )
            ]
        for item in matches:
            # A real card decision may win this race before the notification
            # arrives. Never overwrite a terminal operator decision.
            if self.store.resolve_approval(item.approval_id, 'cancel', 'resolved'):
                item.decision = 'cancel'
                item.event.set()
        for item in input_matches:
            item.cancelled = True
            item.event.set()
        if method in {'turn/completed', 'turn/interrupted'}:
            identity = extract_turn_terminal_identity(message)
            if identity is not None:
                self.finalize_feedback_for_turn(
                    thread_id=identity['thread_id'],
                    turn_id=identity['turn_id'],
                    turn_status=identity['turn_status'],
                    trigger_source='TURN_NOTIFICATION_FALLBACK',
                )
        return len(matches) + len(input_matches)

    def resolve(self, approval_id, operator_open_id, action):
        row = self.store.get_approval(approval_id)
        if not row:
            raise StructuredError('FEISHU_APPROVAL_NOT_FOUND', 'Approval request not found')
        if operator_open_id != row['requester_open_id'] or operator_open_id not in self.settings.allowed_open_ids:
            raise StructuredError('FEISHU_APPROVAL_OPERATOR_MISMATCH', 'Only the requesting authorized operator can resolve approval')
        decision = {'allow_once': 'accept', 'approve_once': 'accept', 'decline': 'decline', 'cancel': 'cancel'}.get(action)
        if decision is None:
            raise StructuredError('FEISHU_APPROVAL_DECISION_INVALID', 'Unsupported approval action')
        if row['state'] != 'pending':
            resolved_as = 'Allow once' if row.get('decision') == 'accept' else 'Decline' if row.get('decision') == 'decline' else 'already resolved'
            visible = f'Already resolved as {resolved_as}'
            if row.get('decision') in {'accept', 'decline'} and row.get('feedback_state'):
                future = self._schedule_feedback_update(approval_id, row['feedback_state'], row['decision'], feedback=visible)
                try:
                    future.result(timeout=5.0)
                except Exception:
                    pass
            return {'status': 'ALREADY_RESOLVED', 'state': row['state'], 'decision': row.get('decision'), 'visible_feedback': visible}
        if not self.store.resolve_approval(approval_id, decision, 'approved' if decision == 'accept' else 'declined' if decision == 'decline' else 'cancelled'):
            current = self.store.get_approval(approval_id) or row
            resolved_as = 'Allow once' if current.get('decision') == 'accept' else 'Decline' if current.get('decision') == 'decline' else 'already resolved'
            visible = f'Already resolved as {resolved_as}'
            if current.get('decision') in {'accept', 'decline'} and current.get('feedback_state'):
                future = self._schedule_feedback_update(approval_id, current['feedback_state'], current['decision'], feedback=visible)
                try:
                    future.result(timeout=5.0)
                except Exception:
                    pass
            return {'status': 'ALREADY_RESOLVED', 'decision': current.get('decision'), 'visible_feedback': visible}
        feedback_future = None
        if decision in {'accept', 'decline'}:
            feedback_future = self._schedule_feedback_update(approval_id, 'ACKNOWLEDGED_PROCESSING', decision)
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending:
                pending.decision = decision
                pending.event.set()
        if feedback_future is not None and pending is None:
            try:
                feedback_future.result(timeout=5.0)
            except Exception:
                pass
        return {'status': 'RESOLVED', 'decision': decision}
