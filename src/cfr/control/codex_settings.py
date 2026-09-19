"""Narrow, app-server-owned Codex defaults for newly created threads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cfr.codex.app_server import AppServerClient, AppServerRpcError

from .codex_catalog import model_catalog
from .model_registry import default_model, resolve_model


_TIMEOUT_SECONDS = 15
_KEYS = ('model', 'model_reasoning_effort', 'service_tier')


class CodexSettingsError(Exception):
    def __init__(self, error_code: str, message: str, status: int = 400):
        self.error_code = error_code
        self.message = message
        self.status = status
        super().__init__(message)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _source(origins: Mapping[str, Any], key: str, fallback: str = 'unknown') -> str:
    metadata = _mapping(origins.get(key))
    return _text(_mapping(metadata.get('name')).get('type')) or fallback


def _requirements(response: Mapping[str, Any] | None) -> dict[str, str | None]:
    new_thread = _mapping(_mapping(_mapping(response).get('requirements')).get('models')).get('newThread')
    return {
        'model': _text(_mapping(new_thread).get('model')),
        'reasoning_effort': _text(_mapping(new_thread).get('modelReasoningEffort')),
        'service_tier': _text(_mapping(new_thread).get('serviceTier')),
    }


def _project(config_response: Mapping[str, Any], requirements_response: Mapping[str, Any] | None, catalog: list[dict[str, Any]]) -> dict[str, Any]:
    config = _mapping(config_response.get('config'))
    origins = _mapping(config_response.get('origins'))
    runtime_default = default_model(catalog)
    configured_model = _text(config.get('model'))
    effective_model = configured_model or (runtime_default or {}).get('model')
    selected_model = resolve_model(catalog, effective_model, display_name=False)
    configured_reasoning = _text(config.get('model_reasoning_effort'))
    configured_tier = _text(config.get('service_tier'))
    return {
        'available': True,
        'error_code': None,
        'message': 'Installed Codex settings',
        'codex_model_defaults': {
            'applies_to': 'new_threads',
            'model': {
                'effective_value': effective_model,
                'source': _source(origins, 'model', 'runtimeDefault' if not configured_model and effective_model else 'unknown'),
            },
            'reasoning_effort': {
                'effective_value': configured_reasoning or (selected_model or {}).get('default_reasoning_effort'),
                'source': _source(origins, 'model_reasoning_effort', 'modelDefault' if not configured_reasoning and selected_model else 'unknown'),
            },
            'service_tier': {
                'effective_value': configured_tier or (selected_model or {}).get('default_service_tier'),
                'source': _source(origins, 'service_tier', 'modelDefault' if not configured_tier and selected_model else 'unknown'),
            },
            'managed_new_thread_defaults': _requirements(requirements_response),
        },
    }


def _method_missing(error: AppServerRpcError) -> bool:
    return error.code == -32601 or 'method not found' in error.message.lower()


def _read_with_client(client) -> dict[str, Any]:
    try:
        config_response = _mapping(client.request('config/read', {'includeLayers': False}, timeout=_TIMEOUT_SECONDS))
    except AppServerRpcError as error:
        code = 'CONTROL_CODEX_CONFIG_READ_UNAVAILABLE' if _method_missing(error) else 'CONTROL_CODEX_CONFIG_READ_FAILED'
        raise CodexSettingsError(code, 'Codex settings are unavailable.', 503) from error
    except Exception as error:
        raise CodexSettingsError('CONTROL_CODEX_CONFIG_READ_FAILED', 'Codex settings are unavailable.', 503) from error
    try:
        requirements_response = _mapping(client.request('configRequirements/read', {}, timeout=_TIMEOUT_SECONDS))
    except Exception:
        requirements_response = None
    try:
        catalog = model_catalog(client)
    except Exception:
        catalog = []
    return _project(config_response, requirements_response, catalog)


def read(client_factory=AppServerClient) -> dict[str, Any]:
    try:
        with client_factory(timeout=_TIMEOUT_SECONDS) as client:
            return _read_with_client(client)
    except CodexSettingsError as error:
        return {'available': False, 'error_code': error.error_code, 'message': error.message, 'codex_model_defaults': None}
    except Exception:
        return {'available': False, 'error_code': 'CONTROL_CODEX_UNAVAILABLE', 'message': 'Codex settings are unavailable.', 'codex_model_defaults': None}


def _validate(values: Any, catalog: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None]:
    if not isinstance(values, Mapping) or set(values) != {'model', 'reasoning_effort', 'service_tier'}:
        raise CodexSettingsError('CONTROL_INVALID_CODEX_SETTING', 'Model defaults require model, reasoning_effort, and service_tier.')
    model, reasoning, tier = (values[key] for key in ('model', 'reasoning_effort', 'service_tier'))
    if any(value is not None and not isinstance(value, str) for value in (model, reasoning, tier)):
        raise CodexSettingsError('CONTROL_INVALID_CODEX_SETTING', 'Model defaults must be strings or null.')
    selected = resolve_model(catalog, model, display_name=False) if model else default_model(catalog)
    if model and selected is None:
        raise CodexSettingsError('CONTROL_INVALID_CODEX_SETTING', 'Selected model is unavailable in the installed Codex catalog.')
    if selected is None and (reasoning or tier):
        raise CodexSettingsError('CONTROL_INVALID_CODEX_SETTING', 'Model default cannot be resolved for reasoning or service tier validation.')
    if reasoning and reasoning not in [item.get('reasoning_effort') for item in (selected or {}).get('supported_reasoning_efforts', [])]:
        raise CodexSettingsError('CONTROL_INVALID_CODEX_SETTING', 'Selected reasoning effort is unavailable for this model.')
    if tier and tier not in [item.get('id') for item in (selected or {}).get('service_tiers', [])]:
        raise CodexSettingsError('CONTROL_INVALID_CODEX_SETTING', 'Selected service tier is unavailable for this model.')
    return model, reasoning, tier


def _write_error(error: AppServerRpcError) -> CodexSettingsError:
    detail = f'{error.code} {error.message} {error.data}'.lower()
    if _method_missing(error):
        return CodexSettingsError('CONTROL_CODEX_CONFIG_WRITE_UNAVAILABLE', 'Codex config write is unavailable.', 503)
    if 'readonly' in detail:
        return CodexSettingsError('CONTROL_CODEX_SETTING_READONLY', 'Codex does not allow this setting to be changed.', 409)
    if 'versionconflict' in detail:
        return CodexSettingsError('CONTROL_CODEX_CONFIG_CONFLICT', 'Codex configuration changed; refresh and retry.', 409)
    if 'validation' in detail:
        return CodexSettingsError('CONTROL_INVALID_CODEX_SETTING', 'Codex rejected the requested model defaults.')
    return CodexSettingsError('CONTROL_CODEX_CONFIG_WRITE_FAILED', 'Codex could not save model defaults.', 503)


def write(values: Any, client_factory=AppServerClient) -> dict[str, Any]:
    try:
        with client_factory(timeout=_TIMEOUT_SECONDS) as client:
            catalog = model_catalog(client)
            model, reasoning, tier = _validate(values, catalog)
            response = _mapping(client.request('config/batchWrite', {
                'edits': [
                    {'keyPath': 'model', 'value': model, 'mergeStrategy': 'replace'},
                    {'keyPath': 'model_reasoning_effort', 'value': reasoning, 'mergeStrategy': 'replace'},
                    {'keyPath': 'service_tier', 'value': tier, 'mergeStrategy': 'replace'},
                ],
                'expectedVersion': None,
                'filePath': None,
                'reloadUserConfig': True,
            }, timeout=_TIMEOUT_SECONDS))
            state = _read_with_client(client)
    except CodexSettingsError:
        raise
    except AppServerRpcError as error:
        raise _write_error(error) from error
    except Exception as error:
        raise CodexSettingsError('CONTROL_CODEX_CONFIG_WRITE_FAILED', 'Codex could not save model defaults.', 503) from error
    overridden = response.get('status') == 'okOverridden'
    source = _text(_mapping(_mapping(_mapping(response.get('overriddenMetadata')).get('overridingLayer')).get('name')).get('type'))
    return {
        'status': 'ok',
        'error_code': None,
        'warning_code': 'CONTROL_CODEX_CONFIG_OVERRIDDEN' if overridden else None,
        'message': 'Codex model defaults saved.' if not overridden else 'Codex model defaults saved, but a higher-priority setting overrides them.',
        'current_state': state,
        'overriding_source_type': source if overridden else None,
    }
