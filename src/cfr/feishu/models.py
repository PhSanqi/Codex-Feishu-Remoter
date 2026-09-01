from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any


@dataclass(frozen=True)
class FeishuInboundMessage:
    event_id: str | None
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    sender_type: str
    message_type: str
    text: str | None
    root_id: str | None = None
    parent_id: str | None = None
    create_time: str | None = None
    mentions: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    mentioned_bot: bool = False
    body_text: str | None = None
    sender_is_bot: bool = False
    reply_to_message_id: str | None = None

    @property
    def is_user(self) -> bool:
        return self.sender_type == 'user' and not self.sender_is_bot

    @property
    def is_group(self) -> bool:
        return self.chat_type in {'group', 'topic'}

    @classmethod
    def from_event(cls, payload: dict[str, Any], event_id: str | None = None):
        event = payload.get('event', payload)
        sender = event.get('sender') or {}
        sender_id = sender.get('sender_id') or {}
        message = event.get('message') or {}
        message_id = message.get('message_id') or event.get('message_id')
        if not message_id:
            return None
        content = message.get('content')
        text = None
        message_type = message.get('message_type') or 'unknown'
        if message_type == 'text':
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    content = {'text': content}
            if isinstance(content, dict):
                text = content.get('text')
        mentions = tuple(message.get('mentions') or ())
        mentioned_bot = bool(message.get('mentioned_bot', event.get('mentioned_bot', False)))
        if text is not None:
            for mention in mentions:
                key = mention.get('key') or mention.get('name')
                if key:
                    text = text.replace(str(key), '')
            text = re.sub(r'[ \t]+', ' ', text).strip()
        return cls(
            event_id=event_id or payload.get('event_id') or payload.get('header', {}).get('event_id'),
            message_id=str(message_id),
            chat_id=str(message.get('chat_id') or ''),
            chat_type=str(message.get('chat_type') or 'p2p'),
            sender_open_id=str(sender_id.get('open_id') or sender.get('open_id') or ''),
            sender_type=str(sender.get('sender_type') or 'unknown'),
            message_type=message_type,
            text=text,
            root_id=message.get('root_id'),
            parent_id=message.get('parent_id'),
            create_time=message.get('create_time'),
            mentions=mentions,
            mentioned_bot=mentioned_bot,
            body_text=text,
            sender_is_bot=bool(sender.get('sender_type') == 'bot' or sender.get('is_bot', False)),
            reply_to_message_id=message.get('reply_to_message_id') or message.get('parent_id'),
        )


@dataclass(frozen=True)
class FeishuSession:
    chat_id: str
    chat_type: str
    owner_open_id: str
    state: str
    thread_id: str | None
    pending_cwd: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class InboxRecord:
    message_id: str
    event_id: str | None
    chat_id: str
    chat_type: str
    sender_open_id: str
    message_type: str
    text_content: str | None
    status: str
    received_at: float
    started_at: float | None = None
    completed_at: float | None = None
    response_message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class FeishuExecutionContext:
    message_id: str
    chat_id: str
    sender_open_id: str
    thread_id: str | None = None


@dataclass(frozen=True)
class FeishuCardAction:
    action: str
    approval_id: str | None
    operator_open_id: str | None
    raw: dict[str, Any] = field(default_factory=dict)
