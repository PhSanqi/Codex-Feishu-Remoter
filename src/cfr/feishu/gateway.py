from __future__ import annotations

import logging
import time

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
        self.metrics = {'received': 0, 'deduped': 0, 'ignored': 0, 'callback_duration_ms': []}

    def handle_message_event(self, payload: dict, event_id=None):
        started = time.perf_counter()
        self.metrics['received'] += 1
        message = payload if isinstance(payload, FeishuInboundMessage) else FeishuInboundMessage.from_event(payload, event_id)
        if message is None:
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
        if message.message_type != 'text':
            inserted = self.store.enqueue_message(message)
            if inserted:
                self.store.mark_ignored(message.message_id, 'UNSUPPORTED_MESSAGE_TYPE', 'M2 supports text messages only')
            return {'status': 'ignored', 'reason': 'UNSUPPORTED_MESSAGE_TYPE', 'message_id': message.message_id}
        if (message.text or '').strip().startswith('/'):
            if self.daemon is None:
                return {'status': 'ignored', 'reason': 'CONTROL_RUNTIME_NOT_READY', 'message_id': message.message_id}
            self.daemon.handle_control_command(message)
            return {'status': 'command', 'message_id': message.message_id}
        inserted = self.store.enqueue_message(message)
        if not inserted:
            self.metrics['deduped'] += 1
            LOGGER.info('FEISHU_EVENT_DEDUPED message_id_suffix=%s', message.message_id[-8:])
            return {'status': 'deduped', 'message_id': message.message_id}
        if self.daemon is not None:
            self.daemon.enqueue(message.message_id)
        duration = (time.perf_counter() - started) * 1000
        self.metrics['callback_duration_ms'].append(duration)
        LOGGER.info('FEISHU_EVENT_RECEIVED message_id_suffix=%s', message.message_id[-8:])
        return {'status': 'queued', 'message_id': message.message_id, 'callback_duration_ms': duration}

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
