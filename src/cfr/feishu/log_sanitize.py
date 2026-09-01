from __future__ import annotations

import re
import logging


_KEYS = (
    'access_key',
    'ticket',
    'app_secret',
    'tenant_access_token',
    'user_access_token',
    'authorization',
)
_KEY_PATTERN = '|'.join(re.escape(key) for key in _KEYS)
_ASSIGNMENT = re.compile(
    rf'(?P<key>\b(?:{_KEY_PATTERN})\b)(?P<separator>\s*(?:=|:)\s*)(?P<value>"[^"]*"|\'[^\']*\'|[^\s,;&}}]+)',
    re.IGNORECASE,
)
_QUERY = re.compile(
    rf'(?P<prefix>[?&](?:{_KEY_PATTERN})=)(?P<value>[^&#\s]+)',
    re.IGNORECASE,
)
_BEARER = re.compile(r'(?i)(\bauthorization\b\s*:\s*bearer\s+)[^\s,;]+')


def sanitize_feishu_log_text(value) -> str:
    """Redact Feishu credentials and transport tokens from captured text."""
    text = str(value)
    text = _QUERY.sub(lambda match: f'{match.group("prefix")}<redacted>', text)
    text = _BEARER.sub(r'\1<redacted>', text)
    text = _ASSIGNMENT.sub(lambda match: f'{match.group("key")}{match.group("separator")}<redacted>', text)
    return text


class FeishuLogSanitizer(logging.Filter):
    """Sanitize records emitted by the optional Feishu SDK before propagation."""

    def filter(self, record):
        record.msg = sanitize_feishu_log_text(record.getMessage())
        record.args = ()
        return True


def configure_feishu_sdk_logging(level_name: str = 'WARNING'):
    level = getattr(logging, level_name.upper(), logging.WARNING)
    sanitizer = FeishuLogSanitizer()
    names = {'lark_channel', 'lark_oapi'}
    names.update(name for name in logging.Logger.manager.loggerDict if name.startswith(('lark_channel.', 'lark_oapi.')))
    for name in names:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        if not any(isinstance(item, FeishuLogSanitizer) for item in logger.filters):
            logger.addFilter(sanitizer)
