from __future__ import annotations

from collections import deque
import logging
import time
from typing import Any

from cfr.core.models import StructuredError

from .config import FeishuSettings
from .models import FeishuInboundMessage
from .security import authorize_sender, validate_message_scope
from .store import FeishuStore

LOGGER = logging.getLogger(__name__)


class FeishuGateway:
    """Fast callback boundary: parse, authorize, persist, enqueue, return."""

    def __init__(self, settings: FeishuSettings, store: FeishuStore, daemon=None):
        self.settings = settings
        self.store = store
        self.daemon = daemon
        self.metrics: dict[str, Any] = {
            'received': 0,
            'deduped': 0,
            'ignored': 0,
            'callback_duration_ms': deque(maxlen=200),
        }

    def handle_message_event(self, payload: dict, event_id=None):
        started = time.perf_counter()
        self.metrics['received'] += 1
        message = payload if isinstance(payload, FeishuInboundMessage) else FeishuInboundMessage.from_event(payload, event_id)
        if message is None:
            self.metrics['ignored'] += 1
            return {'status': 'ignored', 'reason': 'INVALID_MESSAGE'}
        if not message.message_id or not message.chat_id or not message.sender_open_id:
            self.metrics['ignored'] += 1
            return {'status': 'ignored', 'reason': 'INVALID_MESSAGE'}
        if not message.is_user:
            self.metrics['ignored'] += 1
            return {'status': 'ignored', 'reason': 'NON_USER_SENDER', 'message_id': message.message_id}
        if not authorize_sender(message, self.settings):
            self.metrics['ignored'] += 1
            LOGGER.info('FEISHU_UNAUTHORIZED_SENDER sender_suffix=%s', message.sender_open_id[-4:])
            return {'status': 'ignored', 'reason': 'UNAUTHORIZED_SENDER', 'message_id': message.message_id}
        if not validate_message_scope(message, self.settings):
            self.metrics['ignored'] += 1
            return {'status': 'ignored', 'reason': 'GROUPS_DISABLED', 'message_id': message.message_id}
        if message.message_type == 'text' and self.daemon is not None:
            consume_user_input = getattr(self.daemon, 'consume_user_input_message', None)
            if consume_user_input is not None and consume_user_input(message):
                duration = (time.perf_counter() - started) * 1000
                self.metrics['callback_duration_ms'].append(duration)
                return {
                    'status': 'codex_user_input',
                    'message_id': message.message_id,
                    'callback_duration_ms': duration,
                }
        if message.message_type == 'text' and not str(message.text or '').strip():
            inserted = self.store.enqueue_message(message)
            if not inserted:
                self.metrics['deduped'] += 1
                return {'status': 'deduped', 'message_id': message.message_id}
            self.store.mark_ignored(message.message_id, 'EMPTY_TEXT', 'Text message has no executable content')
            self.metrics['ignored'] += 1
            return {'status': 'ignored', 'reason': 'EMPTY_TEXT', 'message_id': message.message_id}
        attachment_type = {'image': 'image', 'file': 'file', 'media': 'video', 'video': 'video'}.get(message.message_type)
        if message.message_type != 'text' and attachment_type is None:
            inserted = self.store.enqueue_message(message)
            if inserted:
                self.store.mark_ignored(message.message_id, 'UNSUPPORTED_MESSAGE_TYPE', 'CFR currently supports text, image, file, and video messages')
            return {'status': 'ignored', 'reason': 'UNSUPPORTED_MESSAGE_TYPE', 'message_id': message.message_id}
        if attachment_type and not any(
            isinstance(item, dict)
            and item.get('type') == attachment_type
            and item.get('file_key')
            for item in message.resources
        ):
            inserted = self.store.enqueue_message(message)
            if inserted:
                self.store.mark_ignored(message.message_id, 'FEISHU_ATTACHMENT_RESOURCE_MISSING', 'Attachment message has no downloadable resource')
            return {'status': 'ignored', 'reason': 'FEISHU_ATTACHMENT_RESOURCE_MISSING', 'message_id': message.message_id}
        command = None
        if message.message_type == 'text' and (message.text or '').strip().startswith('/'):
            if self.daemon is None:
                return {'status': 'ignored', 'reason': 'CONTROL_RUNTIME_NOT_READY', 'message_id': message.message_id}
            parser = getattr(self.daemon, 'parser', None)
            immediate = getattr(self.daemon, 'IMMEDIATE_COMMANDS', None)
            if parser is None or immediate is None:
                if not self.store.enqueue_message(message):
                    self.metrics['deduped'] += 1
                    return {'status': 'deduped', 'message_id': message.message_id}
                if self.store.claim_next(message.message_id) is None:
                    raise StructuredError('FEISHU_COMMAND_CLAIM_FAILED', 'CFR could not claim the control command')
                try:
                    response_ids = self.daemon.handle_control_command(message) or []
                except Exception as exc:
                    self.store.mark_failed(message.message_id, type(exc).__name__, str(exc))
                    raise
                self.store.mark_completed(message.message_id, response_ids[-1] if response_ids else None)
                return {'status': 'command', 'message_id': message.message_id}
            command = parser.parse(message.text)
            if command is not None and command.name in immediate:
                if not self.store.enqueue_message(message):
                    self.metrics['deduped'] += 1
                    LOGGER.info('FEISHU_EVENT_DEDUPED message_id_suffix=%s', message.message_id[-8:])
                    return {'status': 'deduped', 'message_id': message.message_id}
                if self.store.claim_next(message.message_id) is None:
                    raise StructuredError('FEISHU_COMMAND_CLAIM_FAILED', 'CFR could not claim the control command')
                try:
                    response_ids = self.daemon.handle_control_command(message) or []
                except Exception as exc:
                    self.store.mark_failed(message.message_id, type(exc).__name__, str(exc))
                    raise
                # Redirect reuses this reserved row as the queued new direction
                # only after daemon rewrites its text. A rejected redirect has
                # already produced an error reply and must not execute again.
                rewritten_redirect = False
                if command.name == 'redirect':
                    reserved = self.store.get_inbox(message.message_id)
                    rewritten_redirect = bool(
                        reserved
                        and reserved.status == 'queued'
                        and reserved.text_content != message.text
                    )
                if not rewritten_redirect:
                    self.store.mark_completed(message.message_id, response_ids[-1] if response_ids else None)
                return {'status': 'command', 'message_id': message.message_id}
        inserted = self.store.enqueue_message(message)
        if not inserted:
            self.metrics['deduped'] += 1
            LOGGER.info('FEISHU_EVENT_DEDUPED message_id_suffix=%s', message.message_id[-8:])
            return {'status': 'deduped', 'message_id': message.message_id}
        if self.daemon is not None:
            enqueue = getattr(self.daemon, 'enqueue_for_chat', None)
            if enqueue is not None:
                enqueue(message.message_id, message.chat_id)
            else:
                self.daemon.enqueue(message.message_id)
        duration = (time.perf_counter() - started) * 1000
        self.metrics['callback_duration_ms'].append(duration)
        LOGGER.info('FEISHU_EVENT_RECEIVED message_id_suffix=%s', message.message_id[-8:])
        return {
            'status': 'queued_command' if command is not None else 'queued',
            'message_id': message.message_id,
            'callback_duration_ms': duration,
        }

    def handle_card_action(self, payload):
        if self.daemon is None:
            raise StructuredError('FEISHU_DAEMON_REQUIRED', 'Card actions require the running Feishu daemon')
        if hasattr(payload, 'approval_id'):
            if not payload.approval_id or not payload.operator_open_id or not payload.action:
                raise StructuredError('FEISHU_CARD_ACTION_INVALID', 'Card action is missing required fields')
            return self.daemon.handle_card_action({'action': {'value': {'approval_id': payload.approval_id, 'action': payload.action}}, 'operator_open_id': payload.operator_open_id})
        if not isinstance(payload, dict):
            raise StructuredError('FEISHU_CARD_ACTION_INVALID', 'Card action payload is invalid')
        return self.daemon.handle_card_action(payload)
