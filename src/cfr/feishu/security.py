from __future__ import annotations

import os
from pathlib import Path

from cfr.codex.rollout import canonical_path_key
from cfr.core.models import StructuredError

from .config import FeishuSettings
from .models import FeishuInboundMessage


def authorize_sender(message: FeishuInboundMessage, settings: FeishuSettings) -> bool:
    return message.is_user and message.sender_open_id in settings.allowed_open_ids


def validate_message_scope(message: FeishuInboundMessage, settings: FeishuSettings) -> bool:
    chat_type = (message.chat_type or '').strip().lower()
    if chat_type == 'p2p':
        return True
    if chat_type in {'group', 'topic'}:
        return settings.enable_group_chats and message.mentioned_bot
    return False


def canonical_workspace(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise StructuredError('FEISHU_WORKSPACE_NOT_ALLOWED', 'Workspace path must be absolute')
    if not candidate.exists() or not candidate.is_dir():
        raise StructuredError('FEISHU_WORKSPACE_NOT_ALLOWED', 'Workspace must be an existing directory')
    return candidate.resolve(strict=True)


def validate_workspace(path: str | Path, settings: FeishuSettings) -> Path:
    candidate = canonical_workspace(path)
    roots = []
    for root in settings.allowed_workspace_roots:
        if root.exists() and root.is_dir():
            roots.append(root.resolve(strict=True))
    candidate_key = canonical_path_key(candidate)
    for root in roots:
        root_key = canonical_path_key(root)
        try:
            if os.path.commonpath([candidate_key, root_key]) == root_key:
                return candidate
        except ValueError:
            continue
    raise StructuredError('FEISHU_WORKSPACE_NOT_ALLOWED', f'Workspace is outside the configured allowlist: {candidate.name}')


def safe_identifier(value: str | None, keep=4) -> str:
    if not value:
        return '<none>'
    return value if len(value) <= keep * 2 else f'{value[:keep]}...{value[-keep:]}'
