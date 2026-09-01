"""Small read-only projections of the installed Codex catalog RPCs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cfr.codex.app_server import AppServerClient, AppServerRpcError


_PAGE_LIMIT = 100
_MAX_PAGES = 10
_MAX_RECORDS = 500
_TIMEOUT_SECONDS = 15


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _page_through(client, method: str, params: dict[str, Any]) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    cursor = None
    seen_cursors = set()
    for _ in range(_MAX_PAGES):
        response = client.request(method, {**params, 'cursor': cursor}, timeout=_TIMEOUT_SECONDS)
        if not isinstance(response, Mapping):
            break
        for item in _items(response.get('data')):
            if isinstance(item, Mapping):
                records.append(item)
                if len(records) == _MAX_RECORDS:
                    return records
        cursor = response.get('nextCursor')
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
    return records


def _method_missing(error: AppServerRpcError) -> bool:
    return error.code == -32601 or 'method not found' in error.message.lower()


def _unavailable(error_code: str, message: str) -> dict[str, Any]:
    return {'available': False, 'error_code': error_code, 'message': message, 'data': []}


def _project_model(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
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
    }


def model_catalog(client) -> list[dict[str, Any]]:
    """Project the installed runtime catalog for another Control read operation."""
    return [_project_model(item) for item in _page_through(client, 'model/list', {'includeHidden': False, 'limit': _PAGE_LIMIT})]


def models(client_factory=AppServerClient) -> dict[str, Any]:
    """Read the current installed model catalog; never invent a fallback."""
    try:
        with client_factory(timeout=_TIMEOUT_SECONDS) as client:
            data = model_catalog(client)
    except AppServerRpcError as error:
        code = 'CODEX_MODEL_LIST_UNAVAILABLE' if _method_missing(error) else 'CODEX_MODEL_LIST_FAILED'
        return _unavailable(code, 'Installed Codex model catalog is unavailable.')
    except Exception:
        return _unavailable('CODEX_MODEL_LIST_FAILED', 'Installed Codex model catalog is unavailable.')
    return {'available': True, 'error_code': None, 'message': 'Installed Codex model catalog', 'data': data}


def collaboration_modes(client_factory=AppServerClient) -> dict[str, Any]:
    """Read installed experimental collaboration modes without inventing presets."""
    try:
        with client_factory(timeout=_TIMEOUT_SECONDS) as client:
            response = client.request('collaborationMode/list', {}, timeout=_TIMEOUT_SECONDS)
            data = [
                {'name': _text(item.get('name')), 'mode': _text(item.get('mode')), 'model': _text(item.get('model')), 'reasoning_effort': _text(item.get('reasoning_effort'))}
                for item in _items((response or {}).get('data')) if isinstance(item, Mapping)
            ]
    except AppServerRpcError as error:
        code = 'CODEX_COLLABORATION_MODE_LIST_UNAVAILABLE' if _method_missing(error) else 'CODEX_COLLABORATION_MODE_LIST_FAILED'
        return _unavailable(code, 'Installed Codex collaboration modes are unavailable.')
    except Exception:
        return _unavailable('CODEX_COLLABORATION_MODE_LIST_FAILED', 'Installed Codex collaboration modes are unavailable.')
    return {'available': True, 'error_code': None, 'message': 'Installed Codex collaboration modes', 'data': data}


def _capability_section(client, method: str, unavailable_code: str, failed_code: str, projector) -> dict[str, Any]:
    try:
        return {'available': True, 'error_code': None, 'message': 'Installed Codex capability', 'data': [projector(item) for item in _page_through(client, method, {'limit': _PAGE_LIMIT})]}
    except AppServerRpcError as error:
        code = unavailable_code if _method_missing(error) else failed_code
        return _unavailable(code, 'Installed Codex capability is unavailable.')
    except Exception:
        return _unavailable(failed_code, 'Installed Codex capability is unavailable.')


def capabilities(client_factory=AppServerClient) -> dict[str, Any]:
    """Read global/default capability state without loading a CFR thread."""
    try:
        with client_factory(timeout=_TIMEOUT_SECONDS) as client:
            profiles = _capability_section(
                client, 'permissionProfile/list', 'CODEX_PERMISSION_PROFILE_LIST_UNAVAILABLE', 'CODEX_PERMISSION_PROFILE_LIST_FAILED',
                lambda item: {'id': _text(item.get('id')), 'allowed': item.get('allowed') is True, 'description': _text(item.get('description'))},
            )
            features = _capability_section(
                client, 'experimentalFeature/list', 'CODEX_FEATURE_LIST_UNAVAILABLE', 'CODEX_FEATURE_LIST_FAILED',
                lambda item: {
                    'name': _text(item.get('name')), 'stage': _text(item.get('stage')), 'enabled': item.get('enabled') is True,
                    'default_enabled': item.get('defaultEnabled') is True, 'display_name': _text(item.get('displayName')), 'description': _text(item.get('description')),
                },
            )
    except Exception:
        profiles = _unavailable('CODEX_PERMISSION_PROFILE_LIST_FAILED', 'Installed Codex capability is unavailable.')
        features = _unavailable('CODEX_FEATURE_LIST_FAILED', 'Installed Codex capability is unavailable.')
    return {'context': 'default', 'permission_profiles': profiles, 'experimental_features': features}
