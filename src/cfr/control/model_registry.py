"""Model metadata normalization shared by Control and Feishu surfaces.

The installed Codex runtime is the authority for model availability.  This
module deliberately contains no model slugs: a newly deployed model becomes
usable as soon as `model/list` advertises it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CATALOG_SCHEMA_VERSION = 1

_PROJECTED_NATIVE_KEYS = frozenset({
    'id', 'model', 'displayName', 'description', 'isDefault',
    'defaultReasoningEffort', 'supportedReasoningEfforts',
    'serviceTiers', 'defaultServiceTier', 'inputModalities',
    'supportsPersonality', 'modelSpecialty', 'multiAgentVersion',
    'upgrade', 'upgradeInfo', 'availabilityNux', 'hidden',
    'additionalSpeedTiers',
})


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def project_runtime_model(item: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one native model record without losing future metadata."""
    upgrade_info = _mapping(item.get('upgradeInfo'))
    availability = _mapping(item.get('availabilityNux'))
    return {
        'catalog_schema_version': CATALOG_SCHEMA_VERSION,
        'source': 'codex_runtime',
        'id': _text(item.get('id')),
        'model': _text(item.get('model')),
        'display_name': _text(item.get('displayName')),
        'description': _text(item.get('description')),
        'is_default': item.get('isDefault') is True,
        'default_reasoning_effort': _text(item.get('defaultReasoningEffort')),
        'supported_reasoning_efforts': [
            {'reasoning_effort': _text(effort.get('reasoningEffort')), 'description': _text(effort.get('description'))}
            for effort in _items(item.get('supportedReasoningEfforts')) if isinstance(effort, Mapping)
        ],
        'service_tiers': [
            {'id': _text(tier.get('id')), 'name': _text(tier.get('name')), 'description': _text(tier.get('description'))}
            for tier in _items(item.get('serviceTiers')) if isinstance(tier, Mapping)
        ],
        'default_service_tier': _text(item.get('defaultServiceTier')),
        'input_modalities': [value for value in _items(item.get('inputModalities')) if isinstance(value, str)],
        'supports_personality': item.get('supportsPersonality') is True,
        'model_specialty': _text(item.get('modelSpecialty')),
        'multi_agent_version': _text(item.get('multiAgentVersion')),
        'availability_message': _text(availability.get('message')),
        'upgrade_model': _text(item.get('upgrade')) or _text(upgrade_info.get('model')),
        'upgrade_info': {
            'model': _text(upgrade_info.get('model')),
            'copy': _text(upgrade_info.get('upgradeCopy')),
            'model_link': _text(upgrade_info.get('modelLink')),
            'migration_markdown': _text(upgrade_info.get('migrationMarkdown')),
            'retirement_at': upgrade_info.get('retirementAt') if isinstance(upgrade_info.get('retirementAt'), int) else None,
        } if upgrade_info else None,
        # Preserve newly introduced app-server fields for forward-compatible
        # diagnostics/UI without treating them as supported CFR behavior yet.
        'extensions': {str(key): value for key, value in item.items() if key not in _PROJECTED_NATIVE_KEYS},
    }


def default_model(catalog: list[dict[str, Any]]) -> dict[str, Any] | None:
    defaults = [item for item in catalog if item.get('is_default') and item.get('model')]
    return defaults[0] if len(defaults) == 1 else None


def resolve_model(catalog: list[dict[str, Any]], identity: str | None, *, display_name=True) -> dict[str, Any] | None:
    """Resolve one unambiguous runtime model by native id/slug/display name."""
    identity = (identity or '').strip()
    if not identity:
        return None
    fields = ('model', 'id', 'display_name') if display_name else ('model', 'id')
    matches = [
        item for item in catalog
        if isinstance(item, dict) and identity in {
            str(item.get(field) or '').strip() for field in fields
        } - {''}
    ]
    return matches[0] if len(matches) == 1 else None
