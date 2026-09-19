from __future__ import annotations


_SURFACES = (
    {
        'id': 'chat',
        'name': 'Chat',
        'authority': 'chatgpt_chat',
        'available': False,
        'status': 'not_connected',
        'description': 'Real ChatGPT Chat execution surface; not connected yet.',
    },
    {
        'id': 'work',
        'name': 'Work',
        'authority': 'chatgpt_work',
        'available': False,
        'status': 'not_connected',
        'description': 'Real ChatGPT Work execution surface; not connected yet.',
    },
    {
        'id': 'code',
        'name': 'Code',
        'authority': 'codex',
        'available': True,
        'status': 'ready',
        'description': 'Native Codex execution surface currently used by CFR.',
    },
)


def execution_surfaces(*, selected='code', chat_status=None) -> dict:
    """Return the top-level execution-surface contract.

    This is intentionally separate from Codex collaboration modes. Until a
    real ChatGPT Chat or ChatGPT Work authority is connected, Code remains the
    only selectable execution surface and existing task routing is unchanged.
    """
    data = [dict(item) for item in _SURFACES]
    if chat_status:
        chat = next(item for item in data if item['id'] == 'chat')
        chat['available'] = bool(chat_status.get('available'))
        chat['status'] = chat_status.get('status') or ('ready' if chat['available'] else 'not_connected')
        if chat_status.get('description'):
            chat['description'] = chat_status['description']
    if selected not in {item['id'] for item in data}:
        selected = 'code'
    return {'selected': selected, 'data': data}


def find_execution_surface(value: str | None) -> dict | None:
    if not value:
        return None
    requested = value.strip().lower()
    for item in _SURFACES:
        if requested in {str(item['id']), str(item['name']).lower()}:
            return dict(item)
    return None
