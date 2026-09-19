from __future__ import annotations


APPROVAL_PRESETS = {
    'ask': {
        'label': '全部请求',
        'approval_policy': 'on-request',
        'approvals_reviewer': 'user',
        'sandbox': 'workspace-write',
    },
    'auto': {
        'label': '替我审批',
        'approval_policy': 'on-request',
        'approvals_reviewer': 'auto_review',
        'sandbox': 'workspace-write',
    },
    'full': {
        'label': '全部开放权限',
        'approval_policy': 'never',
        'approvals_reviewer': None,
        'sandbox': 'danger-full-access',
    },
}


def approval_preset(value: str | None):
    key = str(value or '').strip().lower()
    aliases = {
        '1': 'ask', 'ask': 'ask', 'all-request': 'ask', '全部请求': 'ask',
        '2': 'auto', 'auto': 'auto', 'approve-for-me': 'auto', '替我审批': 'auto',
        '3': 'full', 'full': 'full', 'full-access': 'full', '全部开放权限': 'full',
    }
    return aliases.get(key)


def native_permission_settings(mode: str) -> dict:
    preset = APPROVAL_PRESETS[mode]
    return {
        key: preset[key]
        for key in ('approval_policy', 'approvals_reviewer', 'sandbox')
        if preset.get(key) is not None
    }
