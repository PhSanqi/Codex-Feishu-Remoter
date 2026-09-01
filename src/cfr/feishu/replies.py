from __future__ import annotations

import hashlib
from typing import Any

from .store import FeishuStore
from .transport import FeishuTransport


def deterministic_reply_uuid(message_id: str, phase: str, chunk_index: int = 0) -> str:
    digest = hashlib.sha256(f'{message_id}|{phase}|{chunk_index}'.encode()).hexdigest()[:32]
    return f'cfr-{digest}'


def chunk_text(text: str, limit=12000) -> list[str]:
    if not text:
        return ['']
    return [text[index:index + limit] for index in range(0, len(text), limit)]


class FeishuReplyClient:
    def __init__(self, transport: FeishuTransport, store: FeishuStore):
        self.transport = transport
        self.store = store

    def _send(self, message_id, phase, text, sender_open_id=None, chat_id=None, chunks=False):
        parts = chunk_text(text) if chunks else [text]
        response_ids = []
        for index, part in enumerate(parts):
            uuid = deterministic_reply_uuid(message_id, phase, index)
            if not self.store.reserve_reply(message_id, phase, index):
                record = self.store.get_reply_record(message_id, phase, index)
                if record and record['state'] == 'sent':
                    response_ids.append(record['response_message_id'])
                    continue
                if record and record['state'] == 'pending':
                    continue
            try:
                if sender_open_id:
                    response_id = self.transport.send_text(sender_open_id, part, uuid)
                else:
                    response_id = self.transport.reply_text(message_id, chat_id or message_id, part, uuid)
                self.store.set_reply_response(message_id, phase, index, response_id)
            except Exception:
                self.store.mark_reply_failed(message_id, phase, index)
                raise
            response_ids.append(response_id)
        return response_ids

    def reply_text(self, message_id, text, phase='final', chat_id=None):
        return self._send(message_id, phase, text, chat_id=chat_id, chunks=True)

    def send_private_text(self, message_id, open_id, text, phase='approval'):
        return self._send(message_id, phase, text, sender_open_id=open_id, chunks=True)

    def send_card(self, message_id, open_id, card, phase='approval'):
        uuid = deterministic_reply_uuid(message_id, phase, 0)
        if not self.store.reserve_reply(message_id, phase, 0):
            record = self.store.get_reply_record(message_id, phase, 0)
            if record and record['state'] == 'sent':
                return record['response_message_id']
            if record and record['state'] == 'pending':
                return None
        try:
            response_id = self.transport.send_card(open_id, card, uuid)
            self.store.set_reply_response(message_id, phase, 0, response_id)
            return response_id
        except Exception:
            self.store.mark_reply_failed(message_id, phase, 0)
            raise

    def update_card(self, message_id, card):
        """Update the original interactive message through the current transport."""
        return self.transport.update_card(message_id, card)
