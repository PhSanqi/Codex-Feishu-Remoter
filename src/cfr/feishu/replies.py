from __future__ import annotations

import hashlib
from pathlib import Path
import time

from cfr.core.models import StructuredError

from .store import FeishuStore
from .transport import FeishuTransport


RETRYABLE_OUTBOUND_ERRORS = {'FEISHU_API_TIMEOUT', 'FEISHU_API_UNKNOWN'}


def deterministic_reply_uuid(message_id: str, phase: str, chunk_index: int = 0) -> str:
    digest = hashlib.sha256(f'{message_id}|{phase}|{chunk_index}'.encode()).hexdigest()[:32]
    return f'cfr-{digest}'


def chunk_text(text: str, limit=12000) -> list[str]:
    if not text:
        return ['']
    limit = int(limit)
    if limit <= 0:
        raise ValueError('chunk limit must be positive')
    parts = []
    current = []
    current_bytes = 0
    for character in text:
        encoded_size = len(character.encode('utf-8'))
        if encoded_size > limit:
            raise ValueError('chunk limit is smaller than one UTF-8 character')
        if current and current_bytes + encoded_size > limit:
            parts.append(''.join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += encoded_size
    if current:
        parts.append(''.join(current))
    return parts


class FeishuReplyClient:
    def __init__(self, transport: FeishuTransport, store: FeishuStore):
        self.transport = transport
        self.store = store

    @staticmethod
    def _deliver(operation):
        """Retry one idempotent outbound operation on transient transport failure."""
        for attempt in range(2):
            try:
                return operation()
            except StructuredError as exc:
                if attempt or exc.code not in RETRYABLE_OUTBOUND_ERRORS:
                    raise
                time.sleep(0.35)

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
                    response_id = self._deliver(
                        lambda sender=sender_open_id, payload=part, request_uuid=uuid:
                        self.transport.send_text(sender, payload, request_uuid)
                    )
                else:
                    response_id = self._deliver(
                        lambda source=message_id, target=chat_id or message_id, payload=part, request_uuid=uuid:
                        self.transport.reply_text(source, target, payload, request_uuid)
                    )
                self.store.set_reply_response(message_id, phase, index, response_id)
            except Exception:
                self.store.mark_reply_failed(message_id, phase, index)
                raise
            response_ids.append(response_id)
        return response_ids

    def reply_text(self, message_id, text, phase='final', chat_id=None):
        # Feishu rejects an empty text payload (provider code 230001).  A
        # completed Codex turn is allowed to have no textual final answer when
        # its real deliverable is an image/file, so treat that as "no text to
        # send" rather than turning a successful turn into a delivery failure.
        if not str(text or '').strip():
            return []
        return self._send(message_id, phase, text, chat_id=chat_id, chunks=True)

    def reply_image(self, message_id, image, phase='image', chat_id=None):
        uuid = deterministic_reply_uuid(message_id, phase, 0)
        if not self.store.reserve_reply(message_id, phase, 0):
            record = self.store.get_reply_record(message_id, phase, 0)
            if record and record['state'] == 'sent':
                return [record['response_message_id']]
            if record and record['state'] == 'pending':
                return []
        try:
            response_id = self._deliver(lambda: self.transport.reply_image(message_id, chat_id or message_id, bytes(image), uuid))
            self.store.set_reply_response(message_id, phase, 0, response_id)
        except Exception:
            self.store.mark_reply_failed(message_id, phase, 0)
            raise
        return [response_id]

    def reply_file(self, message_id, path, phase='file', chat_id=None):
        uuid = deterministic_reply_uuid(message_id, phase, 0)
        if not self.store.reserve_reply(message_id, phase, 0):
            record = self.store.get_reply_record(message_id, phase, 0)
            if record and record['state'] == 'sent':
                return [record['response_message_id']]
            if record and record['state'] == 'pending':
                return []
        try:
            response_id = self._deliver(lambda: self.transport.reply_file(message_id, chat_id or message_id, Path(path), uuid))
            self.store.set_reply_response(message_id, phase, 0, response_id)
        except Exception:
            self.store.mark_reply_failed(message_id, phase, 0)
            raise
        return [response_id]

    def reply_video(self, message_id, path, phase='video', chat_id=None):
        uuid = deterministic_reply_uuid(message_id, phase, 0)
        if not self.store.reserve_reply(message_id, phase, 0):
            record = self.store.get_reply_record(message_id, phase, 0)
            if record and record['state'] == 'sent':
                return [record['response_message_id']]
            if record and record['state'] == 'pending':
                return []
        try:
            response_id = self._deliver(lambda: self.transport.reply_video(message_id, chat_id or message_id, Path(path), uuid))
            self.store.set_reply_response(message_id, phase, 0, response_id)
        except Exception:
            self.store.mark_reply_failed(message_id, phase, 0)
            raise
        return [response_id]

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
            response_id = self._deliver(lambda: self.transport.send_card(open_id, card, uuid))
            self.store.set_reply_response(message_id, phase, 0, response_id)
            return response_id
        except Exception:
            self.store.mark_reply_failed(message_id, phase, 0)
            raise

    def update_card(self, message_id, card):
        """Update the original interactive message through the current transport."""
        return self.transport.update_card(message_id, card)
