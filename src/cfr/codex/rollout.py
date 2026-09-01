import json
import ntpath
import os
from pathlib import Path
import re

from cfr.core.events import CfrEvent, EventSource


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
    def __init__(self, thread_id=None, path=None, byte_offset=0, offset=None):
        # Keep the old positional RolloutWatcher(path, offset) shape as a safe compatibility path.
        if path is None and isinstance(thread_id, (str, Path)) and str(thread_id).lower().endswith('.jsonl'):
            path, thread_id = thread_id, None
        self.thread_id = thread_id
        self.path = Path(path) if path is not None else Path('')
        self.byte_offset = byte_offset if offset is None else offset

    @property
    def offset(self):
        return self.byte_offset

    def poll(self):
        events = []
        if not self.path.exists():
            return events
        size = self.path.stat().st_size
        if self.byte_offset > size:
            self.byte_offset = 0
        with self.path.open('rb') as handle:
            handle.seek(self.byte_offset)
            data = handle.read()
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
