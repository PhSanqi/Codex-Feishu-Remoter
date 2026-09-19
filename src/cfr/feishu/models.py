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
    resources: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def is_user(self) -> bool:
        return self.sender_type == 'user' and not self.sender_is_bot

    @property
    def is_group(self) -> bool:
        return self.chat_type in {'group', 'topic'}

    @classmethod
    def from_event(cls, payload: dict[str, Any], event_id: str | None = None):
        if not isinstance(payload, dict):
            return None
        event = payload.get('event', payload)
        if not isinstance(event, dict):
            return None
        sender = event.get('sender') or {}
        sender = sender if isinstance(sender, dict) else {}
        sender_id = sender.get('sender_id') or {}
        sender_id = sender_id if isinstance(sender_id, dict) else {}
        message = event.get('message') or {}
        if not isinstance(message, dict):
            return None
        message_id = message.get('message_id') or event.get('message_id')
        if not message_id:
            return None
        content = message.get('content')
        text = None
        message_type = str(message.get('message_type') or 'unknown').strip().lower()
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                content = {'text': content} if message_type == 'text' else {}
        if message_type == 'text':
            if isinstance(content, dict):
                raw_text = content.get('text')
                text = raw_text if isinstance(raw_text, str) else None
        resources: tuple[dict[str, Any], ...] = ()
        if isinstance(content, dict):
            if message_type == 'image' and content.get('image_key'):
                resources = ({'type': 'image', 'file_key': str(content['image_key'])},)
            elif message_type == 'file' and content.get('file_key'):
                resources = ({
                    'type': 'file',
                    'file_key': str(content['file_key']),
                    'file_name': str(content.get('file_name') or content.get('fileName') or 'file'),
                },)
            elif message_type in {'media', 'video'} and content.get('file_key'):
                resources = ({
                    'type': 'video',
                    'file_key': str(content['file_key']),
                    'file_name': str(content.get('file_name') or content.get('fileName') or 'video.mp4'),
                },)
        mentions = tuple(item for item in (message.get('mentions') or ()) if isinstance(item, dict))
        mentioned_bot = bool(message.get('mentioned_bot', event.get('mentioned_bot', False)))
        if text is not None:
            for mention in mentions:
                key = mention.get('key') or mention.get('name')
                if key:
                    text = text.replace(str(key), '')
            text = re.sub(r'[ \t]+', ' ', text).strip()
        return cls(
            event_id=event_id or payload.get('event_id') or (
                payload.get('header', {}).get('event_id')
                if isinstance(payload.get('header'), dict)
                else None
            ),
            message_id=str(message_id),
            chat_id=str(message.get('chat_id') or ''),
            chat_type=str(message.get('chat_type') or 'unknown').strip().lower(),
            sender_open_id=str(sender_id.get('open_id') or sender.get('open_id') or ''),
            sender_type=str(sender.get('sender_type') or 'unknown').strip().lower(),
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
            resources=resources,
        )


@dataclass(frozen=True)
class FeishuSession:
    chat_id: str
    chat_type: str
    owner_open_id: str
    state: str
    thread_id: str | None
    pending_cwd: str | None
    pending_settings_json: str | None
    approval_mode: str
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
    resources_json: str | None
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
    cwd: str | None = None


@dataclass(frozen=True)
class FeishuCardAction:
    action: str
    approval_id: str | None
    operator_open_id: str | None
    raw: dict[str, Any] = field(default_factory=dict)
