import json
import ntpath
import os
from pathlib import Path
import re
from collections import deque

from cfr.core.events import CfrEvent, EventSource


DEFAULT_HISTORY_TAIL_BYTES = 4 * 1024 * 1024
MAX_HISTORY_TAIL_BYTES = 32 * 1024 * 1024
DEFAULT_WATCH_POLL_BYTES = 8 * 1024 * 1024


def _strip_windows_extended_prefix(text):
    """Strip Windows extended prefixes before any host-specific path parsing."""
    if len(text) < 4 or text[:2] != '\\\\' or text[2] != '?' or text[3] not in ('\\', '/'):
        return text
    remainder = text[4:]
    if len(remainder) >= 4 and remainder[:3].lower() == 'unc' and remainder[3] in ('\\', '/'):
        return '\\\\' + remainder[4:]
    return remainder


def _looks_like_windows_path(text):
    return bool(re.match(r'^[A-Za-z]:[\\/]', text)) or text.startswith('\\\\')


def canonical_path_key(value):
    """Return a comparison-only path key without rewriting stored rollout paths."""
    if value is None:
        return None
    text = os.fspath(value)
    text = _strip_windows_extended_prefix(text)
    if _looks_like_windows_path(text):
        return ntpath.normcase(ntpath.normpath(text)).rstrip('\\/')
    return os.path.normcase(str(Path(text).resolve(strict=False))).rstrip('\\/')


class RolloutWatcher:
    def __init__(self, thread_id=None, path=None, byte_offset=0, offset=None, max_poll_bytes=DEFAULT_WATCH_POLL_BYTES):
        # Keep the old positional RolloutWatcher(path, offset) shape as a safe compatibility path.
        if path is None and isinstance(thread_id, (str, Path)) and str(thread_id).lower().endswith('.jsonl'):
            path, thread_id = thread_id, None
        self.thread_id = thread_id
        self.path = Path(path) if path is not None else Path('')
        self.byte_offset = byte_offset if offset is None else offset
        self.max_poll_bytes = max(4096, int(max_poll_bytes))
        self._discarding_oversized_line = False

    @property
    def offset(self):
        return self.byte_offset

    def poll(self):
        events = []
        if not self.path.exists():
            return events
        try:
            size = self.path.stat().st_size
        except OSError:
            return events
        if self.byte_offset > size:
            self.byte_offset = 0
            self._discarding_oversized_line = False
        with self.path.open('rb') as handle:
            handle.seek(self.byte_offset)
            data = handle.read(self.max_poll_bytes)
        if self._discarding_oversized_line:
            newline = data.find(b'\n')
            if newline < 0:
                self.byte_offset += len(data)
                return events
            self.byte_offset += newline + 1
            data = data[newline + 1:]
            self._discarding_oversized_line = False
        if data and b'\n' not in data and len(data) >= self.max_poll_bytes:
            self.byte_offset += len(data)
            self._discarding_oversized_line = True
            return events
        consumed = 0
        for line in data.splitlines(keepends=True):
            if not line.endswith((b'\n', b'\r')):
                break
            consumed += len(line)
            try:
                record = json.loads(line.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            event = self._event(record)
            if event:
                events.append(event)
        self.byte_offset += consumed
        return events

    def _event(self, record):
        payload = record.get('payload') or {}
        event_type = payload.get('type')
        text = payload.get('message')
        if event_type == 'message' and payload.get('content'):
            text = ' '.join(item.get('text', '') for item in payload['content'] if isinstance(item, dict))
            event_type = f"{payload.get('role', 'unknown')}_message"
        if event_type not in ('user_message', 'agent_message'):
            return None
        source_name = payload.get('source') or record.get('source')
        source = EventSource.DESKTOP if source_name == 'desktop' else EventSource.UNKNOWN
        metadata = payload.get('internal_chat_message_metadata_passthrough') or {}
        return CfrEvent(
            self.thread_id or payload.get('thread_id'),
            payload.get('turn_id') or metadata.get('turn_id'),
            payload.get('id'),
            event_type,
            source,
            text,
            None,
            record.get('type'),
        )


def _message_text(payload):
    value = payload.get('message')
    if isinstance(value, str):
        return value
    content = payload.get('content') or ()
    if not isinstance(content, list):
        return ''
    return '\n'.join(
        str(item.get('text') or '')
        for item in content
        if isinstance(item, dict) and item.get('type') in {'input_text', 'output_text', 'text'}
    ).strip()


def _history_message(record):
    payload = record.get('payload') or {}
    kind = payload.get('type')
    role = None
    if kind == 'user_message':
        role = 'user'
    elif kind == 'agent_message':
        role = 'assistant'
    elif kind == 'message' and payload.get('role') in {'user', 'assistant'}:
        role = payload.get('role')
    if role is None:
        return None
    text = _message_text(payload).strip()
    if not text:
        return None
    metadata = payload.get('internal_chat_message_metadata_passthrough') or {}
    return {
        'role': role,
        'text': text,
        'timestamp': record.get('timestamp'),
        'turn_id': payload.get('turn_id') or metadata.get('turn_id'),
    }


def recent_rollout_messages(path, limit=10, max_bytes=DEFAULT_HISTORY_TAIL_BYTES, max_total_bytes=MAX_HISTORY_TAIL_BYTES):
    """Read a bounded tail of a native Codex rollout and return recent dialogue.

    Native rollouts can contain multi-megabyte image/tool records.  History
    views must therefore never rescan the complete file as a thread grows.
    Starting in the middle of a large JSON line is safe: the partial first line
    is discarded and subsequent complete records are parsed normally.
    """
    path = Path(path)
    if not path.is_file():
        return []
    limit = min(max(int(limit), 1), 50)
    max_bytes = max(int(max_bytes), 4096)
    max_total_bytes = max(max_bytes, int(max_total_bytes))
    size = path.stat().st_size
    window = min(size, max_bytes)
    while True:
        start = max(0, size - window)
        with path.open('rb') as handle:
            handle.seek(start)
            data = handle.read(window)
        if start:
            _, separator, data = data.partition(b'\n')
            if not separator:
                data = b''
        messages = deque(maxlen=limit)
        previous = None
        for line in data.splitlines():
            try:
                record = json.loads(line.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            item = _history_message(record)
            if item is None:
                continue
            # Codex rollouts frequently persist the same visible message once as an
            # event_msg and once as a response_item.  Consecutive duplicate visible
            # messages are one history item, not two turns.
            # Native event_msg and response_item copies can disagree on whether a
            # turn_id is populated, so visible-message identity must not depend on
            # that metadata.  Only consecutive identical visible messages collapse;
            # a user intentionally repeating the same text in a later turn remains.
            signature = (item['role'], item['text'])
            if signature == previous:
                continue
            previous = signature
            messages.append(item)
        if len(messages) >= limit or start == 0 or window >= max_total_bytes:
            return list(messages)
        window = min(size, max_total_bytes, window * 2)
