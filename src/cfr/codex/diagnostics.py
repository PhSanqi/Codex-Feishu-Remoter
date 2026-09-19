from __future__ import annotations

import os
import hashlib
import json
import re
import subprocess
import tempfile
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .launcher import CodexLauncher
from .app_server import AppServerClient
from .approvals import CFR_ACCOUNTED_SERVER_REQUEST_METHODS, CFR_REQUIRED_SERVER_REQUEST_METHODS
from ..config import child_process_env, resolve_cfr_codex_home
from ..network import sanitized_proxy_url
from ..platform import hidden_subprocess_kwargs


_ROUTING_KEYS = {
    'profile', 'model', 'model_provider', 'modelProvider', 'openai_base_url',
    'openaiBaseUrl', 'chatgpt_base_url', 'chatgptBaseUrl', 'service_tier',
    'serviceTier', 'base_url', 'baseUrl', 'wire_api', 'wireApi',
    'requires_openai_auth', 'requiresOpenaiAuth', 'supports_websockets',
    'supportsWebsockets', 'responses_websockets', 'responses_websockets_v2',
}
_SENSITIVE_KEY_PARTS = ('token', 'secret', 'password', 'cookie', 'authorization', 'credential', 'api_key', 'apikey', 'email', 'name')
VALID_GATE_ORIGINS = {'desktop_agent', 'host_manual', 'unknown'}
VALID_RUNTIME_EXECUTION_CONTEXTS = {'HOST', 'CODEX_AGENT_SANDBOX', 'UNKNOWN'}


@dataclass(frozen=True)
class CodexInterfaceCapabilities:
    codex_version: str | None
    generated_schema: bool
    account_read: bool
    config_read: bool
    thread_start: bool
    thread_resume: bool
    thread_list: bool
    thread_loaded_list: bool
    thread_read: bool
    thread_settings_update: bool
    thread_settings_updated: bool
    thread_status_changed: bool
    thread_turns_list: bool
    thread_turns_list_experimental: bool
    turn_start: bool
    turn_interrupt: bool
    model_list: bool
    permission_profile_list: bool
    experimental_feature_list: bool
    collaboration_mode_list: bool
    thread_status_shape: tuple[str, ...]
    turn_status_shape: tuple[str, ...]
    server_request_methods: tuple[str, ...]


_REQUIRED_CFR_APP_SERVER_METHODS = {
    'account/read': 'account_read',
    'config/read': 'config_read',
    'thread/start': 'thread_start',
    'thread/resume': 'thread_resume',
    'thread/read': 'thread_read',
    'thread/settings/update': 'thread_settings_update',
    'turn/start': 'turn_start',
    'turn/interrupt': 'turn_interrupt',
    'model/list': 'model_list',
}


def codex_interface_capabilities(schema_path: str | Path, codex_version: str | None = None) -> CodexInterfaceCapabilities:
    """Parse generated local app-server schema; never probes or mutates a server."""
    path = Path(schema_path)
    schema_root = path if path.is_dir() else path.parent
    if path.is_dir():
        candidates = (
            path / 'codex_app_server_protocol.v2.schemas.json',
            path / 'codex_app_server_protocol.schemas.json',
            *sorted(path.glob('*.schemas.json')),
        )
        path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    try:
        schema = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError):
        schema = {}

    def literals(value):
        if isinstance(value, Mapping):
            if isinstance(value.get('const'), str):
                yield value['const']
            for item in value.get('enum', ()):
                if isinstance(item, str):
                    yield item
            for item in value.values():
                yield from literals(item)
        elif isinstance(value, list):
            for item in value:
                yield from literals(item)

    methods = set(literals(schema))
    definitions = schema.get('definitions', {}) if isinstance(schema, Mapping) else {}

    def server_request_methods():
        request_path = schema_root / 'ServerRequest.json'
        try:
            request_schema = json.loads(request_path.read_text(encoding='utf-8'))
        except (OSError, ValueError, json.JSONDecodeError):
            return ()
        methods = []
        for branch in request_schema.get('oneOf', ()) if isinstance(request_schema, Mapping) else ():
            if not isinstance(branch, Mapping):
                continue
            method_schema = (branch.get('properties') or {}).get('method') or {}
            values = []
            if isinstance(method_schema, Mapping):
                if isinstance(method_schema.get('const'), str):
                    values.append(method_schema['const'])
                values.extend(value for value in (method_schema.get('enum') or ()) if isinstance(value, str))
            methods.extend(values)
        return tuple(dict.fromkeys(methods))

    def status_values(name):
        definition = definitions.get(name, {}) if isinstance(definitions, Mapping) else {}
        values = []
        for branch in definition.get('oneOf', ()):
            for value in literals(branch.get('properties', {}).get('type', {})):
                values.append(value)
        values.extend(value for value in definition.get('enum', ()) if isinstance(value, str))
        return tuple(dict.fromkeys(values))

    return CodexInterfaceCapabilities(
        codex_version=codex_version,
        generated_schema=bool(schema),
        account_read='account/read' in methods,
        config_read='config/read' in methods,
        thread_start='thread/start' in methods,
        thread_resume='thread/resume' in methods,
        thread_list='thread/list' in methods,
        thread_loaded_list='thread/loaded/list' in methods,
        thread_read='thread/read' in methods,
        thread_settings_update='thread/settings/update' in methods,
        thread_settings_updated='thread/settings/updated' in methods,
        thread_status_changed='thread/status/changed' in methods,
        thread_turns_list='thread/turns/list' in methods,
        thread_turns_list_experimental=False,
        turn_start='turn/start' in methods,
        turn_interrupt='turn/interrupt' in methods,
        model_list='model/list' in methods,
        permission_profile_list='permissionProfile/list' in methods,
        experimental_feature_list='experimentalFeature/list' in methods,
        collaboration_mode_list='collaborationMode/list' in methods,
        thread_status_shape=status_values('ThreadStatus'),
        turn_status_shape=status_values('TurnStatus'),
        server_request_methods=server_request_methods(),
    )


def missing_required_app_server_methods(capabilities: CodexInterfaceCapabilities) -> tuple[str, ...]:
    if not capabilities.generated_schema:
        return ()
    return tuple(
        method
        for method, field in _REQUIRED_CFR_APP_SERVER_METHODS.items()
        if not getattr(capabilities, field)
    )


def missing_required_server_requests(capabilities: CodexInterfaceCapabilities) -> tuple[str, ...]:
    if not capabilities.generated_schema:
        return ()
    present = set(capabilities.server_request_methods)
    return tuple(sorted(CFR_REQUIRED_SERVER_REQUEST_METHODS - present))


def unaccounted_server_requests(capabilities: CodexInterfaceCapabilities) -> tuple[str, ...]:
    return tuple(sorted(set(capabilities.server_request_methods) - CFR_ACCOUNTED_SERVER_REQUEST_METHODS))


def _schema_fingerprint(directory: str | Path) -> str | None:
    root = Path(directory)
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    found = False
    for path in sorted(root.glob('*.json'), key=lambda value: value.name.casefold()):
        try:
            data = path.read_bytes()
        except OSError:
            return None
        found = True
        digest.update(path.name.encode('utf-8'))
        digest.update(b'\0')
        digest.update(data)
        digest.update(b'\0')
    return digest.hexdigest() if found else None


def normalize_gate_origin(value: str | None) -> str:
    """Normalize the declared gate source; this is metadata, not a pass/fail override."""
    origin = str(value or 'unknown').strip().lower()
    if origin not in VALID_GATE_ORIGINS:
        raise ValueError(f'unsupported gate origin: {origin}')
    return origin


def runtime_execution_context(gate_origin: str | None) -> str:
    """Map an explicit gate source to a report-only execution-context label."""
    origin = normalize_gate_origin(gate_origin)
    return {
        'host_manual': 'HOST',
        'desktop_agent': 'CODEX_AGENT_SANDBOX',
        'unknown': 'UNKNOWN',
    }[origin]


def gate_metadata(gate_origin: str | None) -> dict[str, str]:
    origin = normalize_gate_origin(gate_origin)
    return {
        'GateOrigin': origin,
        'RuntimeExecutionContext': runtime_execution_context(origin),
    }


def codex_home_readonly_snapshot(path: str | Path) -> dict[str, Any]:
    """Collect existence/stat metadata without opening SQLite or changing ACLs."""
    root = Path(path)

    def entry(candidate: Path) -> dict[str, Any]:
        item = {'Path': str(candidate), 'Exists': False, 'IsDirectory': False}
        try:
            stat = candidate.stat()
            item.update({
                'Exists': True,
                'IsDirectory': candidate.is_dir(),
                'Size': stat.st_size,
                'ModifiedAt': stat.st_mtime,
            })
        except (OSError, ValueError) as exc:
            item['ErrorClass'] = type(exc).__name__
        return item

    return {
        'Home': entry(root),
        'Parent': entry(root.parent),
        'Tmp': entry(root / 'tmp'),
        'Arg0': entry(root / 'tmp' / 'arg0'),
        'State5Sqlite': entry(root / 'state_5.sqlite'),
        'Logs2Sqlite': entry(root / 'logs_2.sqlite'),
    }


def _key_name(key: Any) -> str:
    return str(key)


def _find_values(value: Any, wanted: set[str], path: str = ''):
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = _key_name(key)
            child_path = f'{path}.{key_text}' if path else key_text
            if key_text in wanted:
                yield child_path, child
            yield from _find_values(child, wanted, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _find_values(child, wanted, f'{path}[{index}]')


def _first_value(value: Any, names: set[str]):
    for path, candidate in _find_values(value, names):
        return path, candidate
    return None, None


def _safe_scalar(value: Any):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _safe_url(value: Any):
    if not isinstance(value, str) or not value:
        return None
    return sanitized_proxy_url(value)


def sanitize_endpoint(value: Any):
    if not isinstance(value, str):
        return None
    match = re.search(r'https?://[^\s)]+', value)
    if not match:
        return None
    try:
        parsed = urllib.parse.urlsplit(match.group(0).rstrip('.,'))
        if not parsed.hostname:
            return None
        host = parsed.hostname
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        port = f':{parsed.port}' if parsed.port else ''
        path = parsed.path or '/'
        return f'{parsed.scheme}://{host}{port}{path}'
    except Exception:
        return None


def classify_login_output(output: str):
    text = (output or '').lower()
    if 'api key' in text or 'apikey' in text:
        return 'API_KEY'
    if 'chatgpt' in text or 'openai auth' in text or 'oauth' in text:
        return 'CHATGPT'
    if 'not logged' in text or 'not login' in text or 'logged out' in text:
        return 'UNKNOWN'
    return 'OTHER' if text.strip() else 'UNKNOWN'


def login_status(launcher: CodexLauncher, process_env: Mapping[str, str] | None = None):
    command = launcher.build_command('login', 'status')
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=15,
            env={**os.environ, **process_env} if process_env else None,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        output = f'{completed.stdout}\n{completed.stderr}'
        auth_mode = classify_login_output(output)
        login_state = 'NOT_LOGGED_IN' if 'not logged' in output.lower() or 'not login' in output.lower() else ('LOGGED_IN' if completed.returncode == 0 and auth_mode != 'UNKNOWN' else 'UNKNOWN')
        return {
            'LoginStatus': login_state,
            'AuthMode': auth_mode,
            'ExitCode': completed.returncode,
        }
    except Exception as exc:
        return {'LoginStatus': 'UNKNOWN', 'AuthMode': 'UNKNOWN', 'ErrorClass': type(exc).__name__}


def account_read(client):
    try:
        response = client.request('account/read', {'refreshToken': False})
        _, auth_value = _first_value(response, {'authMode', 'auth_mode', 'authType', 'auth_type', 'type'})
        _, plan_value = _first_value(response, {'planType', 'plan_type', 'plan'})
        _, requires_auth = _first_value(response, {'requiresOpenaiAuth', 'requires_openai_auth'})
        auth_mode = normalize_auth_mode(auth_value)
        return {
            'AccountReadSucceeded': True,
            'AuthMode': auth_mode,
            'PlanType': _safe_plan(plan_value),
            'RequiresOpenaiAuth': requires_auth if isinstance(requires_auth, bool) else None,
            'HasAccount': bool(response.get('account')) if isinstance(response, Mapping) else bool(response),
            'ResponseKeys': _safe_keys(response),
        }, response
    except Exception as exc:
        return {
            'AccountReadSucceeded': False,
            'AuthMode': 'UNKNOWN',
            'PlanType': None,
            'RequiresOpenaiAuth': None,
            'HasAccount': None,
            'ErrorClass': type(exc).__name__,
            'ErrorCode': getattr(exc, 'code', None),
        }, None


def normalize_auth_mode(value: Any):
    if isinstance(value, Mapping):
        value = value.get('type') or value.get('mode') or value.get('kind')
    text = str(value or '').lower().replace('_', '').replace('-', '')
    if 'apikey' in text or text in {'api', 'openai'}:
        return 'API_KEY'
    if 'chatgpt' in text or 'oauth' in text or 'personaltoken' in text or 'chatgptauthtokens' in text:
        return 'CHATGPT'
    return 'UNKNOWN'


def _safe_plan(value: Any):
    if isinstance(value, str):
        return value if len(value) <= 80 else value[:80]
    if isinstance(value, Mapping):
        for key in ('type', 'id', 'tier', 'planType'):
            if isinstance(value.get(key), str):
                return value[key][:80]
    return None


def _safe_keys(value: Any):
    if not isinstance(value, Mapping):
        return []
    return sorted(key for key in (_key_name(item) for item in value.keys()) if not any(part in key.lower() for part in _SENSITIVE_KEY_PARTS))


def config_read(client):
    try:
        response = client.request('config/read', {})
        return {'ConfigReadSucceeded': True, **summarize_config(response)}, response
    except Exception as exc:
        return {'ConfigReadSucceeded': False, 'ErrorClass': type(exc).__name__, 'ErrorCode': getattr(exc, 'code', None)}, None


def summarize_config(response: Any):
    def first(names):
        path, value = _first_value(response, names)
        return {'value': _safe_scalar(value), 'path': path} if path else {'value': None, 'path': None}

    model_provider = first({'model_provider', 'modelProvider'})
    model = first({'model'})
    profile = first({'profile'})
    service_tier = first({'service_tier', 'serviceTier'})
    openai_base = first({'openai_base_url', 'openaiBaseUrl'})
    chatgpt_base = first({'chatgpt_base_url', 'chatgptBaseUrl'})
    provider_base = first({'base_url', 'baseUrl'})
    wire_api = first({'wire_api', 'wireApi'})
    requires_auth = first({'requires_openai_auth', 'requiresOpenaiAuth'})
    supports_websockets = first({'supports_websockets', 'supportsWebsockets'})
    features = {}
    for path, value in _find_values(response, {'responses_websockets', 'responses_websockets_v2'}):
        if isinstance(value, bool):
            features[path] = value
    effective_base = openai_base['value'] or provider_base['value']
    return {
        'Profile': profile['value'],
        'Model': model['value'],
        'ModelProvider': model_provider['value'],
        'OpenAiBaseUrl': _safe_url(openai_base['value']),
        'ChatGptBaseUrl': _safe_url(chatgpt_base['value']),
        'ProviderBaseUrl': _safe_url(provider_base['value']),
        'EffectiveBaseUrlSanitized': _safe_url(effective_base),
        'ServiceTier': service_tier['value'],
        'ProviderWireApi': wire_api['value'],
        'ProviderRequiresOpenaiAuth': requires_auth['value'] if isinstance(requires_auth['value'], bool) else None,
        'ProviderSupportsWebsockets': supports_websockets['value'] if isinstance(supports_websockets['value'], bool) else None,
        'WebsocketFeatureCapabilities': features,
        'ConfigResponseKeys': _safe_keys(response),
    }


def config_schema_snapshot(launcher: CodexLauncher, codex_version: str | None = None):
    try:
        with tempfile.TemporaryDirectory(prefix='cfr-codex-schema-') as directory:
            completed = None
            mode = None
            for label, args in (
                ('experimental', ('app-server', 'generate-json-schema', '--experimental', '--out', directory)),
                ('standard', ('app-server', 'generate-json-schema', '--out', directory)),
            ):
                command = launcher.build_command(*args)
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=20,
                    check=False,
                    **hidden_subprocess_kwargs(),
                )
                if completed.returncode == 0:
                    mode = label
                    break
            capabilities = codex_interface_capabilities(directory, codex_version) if completed.returncode == 0 else None
            missing = missing_required_app_server_methods(capabilities) if capabilities is not None else ()
            missing_server_requests = missing_required_server_requests(capabilities) if capabilities is not None else ()
            unaccounted_requests = unaccounted_server_requests(capabilities) if capabilities is not None else ()
            inbound_compatible = bool(
                capabilities
                and capabilities.generated_schema
                and capabilities.server_request_methods
                and not missing_server_requests
                and not unaccounted_requests
            )
            fingerprint = _schema_fingerprint(directory) if completed.returncode == 0 else None
            return {
                'SchemaCommandSucceeded': completed.returncode == 0,
                'SchemaCommandMode': mode,
                'GeneratedSchema': bool(capabilities and capabilities.generated_schema),
                'SchemaFingerprint': fingerprint,
                'RequiredClientMethodsAvailable': bool(capabilities and capabilities.generated_schema and not missing),
                'InboundServerRequestsCompatible': inbound_compatible,
                'RequiredMethodsAvailable': bool(capabilities and capabilities.generated_schema and not missing and inbound_compatible),
                'MissingRequiredMethods': list(missing),
                'MissingRequiredServerRequests': list(missing_server_requests),
                'UnaccountedServerRequests': list(unaccounted_requests),
                'ServerRequestMethods': list(capabilities.server_request_methods) if capabilities is not None else [],
                'Capabilities': capabilities.__dict__ if capabilities is not None else None,
            }
    except Exception as exc:
        return {'SchemaCommandSucceeded': False, 'SchemaErrorClass': type(exc).__name__}


def routing_config_files(project_root: Path, codex_home=None, environment: Mapping[str, str] | None = None):
    environment = dict(os.environ if environment is None else environment)
    resolved_home = Path(codex_home) if codex_home else resolve_cfr_codex_home(environment=environment).path
    candidates = [('user', resolved_home / 'config.toml'), ('project', project_root / '.codex' / 'config.toml')]
    summaries = []
    for source, path in candidates:
        if not path.exists():
            continue
        try:
            with path.open('rb') as handle:
                parsed = tomllib.load(handle)
            summaries.append({'source': source, 'path': str(path), **summarize_config(parsed)})
        except Exception as exc:
            summaries.append({'source': source, 'path': str(path), 'ReadErrorClass': type(exc).__name__})
    return summaries


def environment_snapshot(environment: Mapping[str, str] | None = None):
    environment = dict(os.environ if environment is None else environment)
    result = {}
    for name in ('OPENAI_API_KEY', 'CODEX_API_KEY'):
        result[name] = {'present': name in environment}
    for name in ('OPENAI_BASE_URL', 'CODEX_HOME', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
        value = environment.get(name)
        should_sanitize = bool(value) and ('URL' in name or 'PROXY' in name.upper())
        result[name] = {'present': value is not None, 'sanitized': sanitized_proxy_url(value) if should_sanitize else None}
    return result


def routing_decision(auth_mode: str, config: Mapping[str, Any], account: Mapping[str, Any] | None = None):
    auth_mode = auth_mode if auth_mode in {'API_KEY', 'CHATGPT'} else 'UNKNOWN'
    explicit_override = bool(config.get('OpenAiBaseUrl') or config.get('ProviderBaseUrl'))
    if auth_mode == 'API_KEY':
        expected = 'OPENAI_RESPONSES_API'
        consistency = 'CONSISTENT'
    elif auth_mode == 'CHATGPT' and explicit_override:
        expected = 'CHATGPT_CODEX_BACKEND'
        consistency = 'MISMATCH'
    elif auth_mode == 'CHATGPT':
        expected = 'CHATGPT_CODEX_BACKEND'
        consistency = 'CONSISTENT'
    else:
        expected = 'UNKNOWN'
        consistency = 'UNKNOWN'
    return expected, consistency


def safe_error(value: Any):
    text = str(value or '')
    text = re.sub(r'(?i)bearer\s+[^\s,;]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)(authorization|cookie|api[-_ ]?key|token|secret)\s*[:=]\s*[^\s,;]+', r'\1=[REDACTED]', text)
    text = re.sub(r'https?://[^\s)]+', lambda match: sanitize_endpoint(match.group(0)) or '[REDACTED_URL]', text)
    return text[:500]


def inspect_runtime(project_root: Path, process_env: Mapping[str, str] | None = None, event_sink=None, timeout=30, codex_home=None, codex_bin=None):
    resolved_home = resolve_cfr_codex_home(codex_home)
    runtime_env = child_process_env(process_env, resolved_home)
    launcher = CodexLauncher(executable=codex_bin, environment=runtime_env)
    login = login_status(launcher, runtime_env)
    account = {'AccountReadSucceeded': False, 'AuthMode': 'UNKNOWN'}
    config = {'ConfigReadSucceeded': False}
    schema = None
    runtime_error = None
    runtime_stderr = []
    client = AppServerClient(launcher=launcher, timeout=timeout, process_env=runtime_env, codex_home=resolved_home.path)
    try:
        client.start()
        if event_sink:
            event_sink({'method': 'initialize', 'status': 'ok'})
        account, _ = account_read(client)
        if event_sink:
            event_sink({'method': 'account/read', 'status': 'ok' if account.get('AccountReadSucceeded') else 'error', 'error_class': account.get('ErrorClass')})
        config, _ = config_read(client)
        if event_sink:
            event_sink({'method': 'config/read', 'status': 'ok' if config.get('ConfigReadSucceeded') else 'error', 'error_class': config.get('ErrorClass')})
    except Exception as exc:
        runtime_error = safe_error(exc)
        runtime_stderr = [safe_error(line) for line in client.stderr_tail()][-10:]
        account.setdefault('ErrorClass', type(exc).__name__)
        config.setdefault('ErrorClass', type(exc).__name__)
    finally:
        client.close()
    if not config.get('ConfigReadSucceeded'):
        schema = config_schema_snapshot(launcher)
    file_configs = routing_config_files(project_root, codex_home=resolved_home.path, environment=runtime_env)
    auth_mode = account.get('AuthMode') if account.get('AuthMode') != 'UNKNOWN' else login.get('AuthMode', 'UNKNOWN')
    expected, consistency = routing_decision(auth_mode, config)
    config_source = 'UNKNOWN'
    if file_configs:
        config_source = file_configs[-1].get('source', 'UNKNOWN')
    return {
        'LoginStatus': login.get('LoginStatus', 'UNKNOWN'),
        'AuthMode': auth_mode,
        'LoginAuthMode': login.get('AuthMode', 'UNKNOWN'),
        'AccountReadSucceeded': account.get('AccountReadSucceeded', False),
        'AccountRead': {key: value for key, value in account.items() if key != 'ResponseKeys'} | {'ResponseKeys': account.get('ResponseKeys', [])},
        'PlanType': account.get('PlanType'),
        'RequiresOpenaiAuth': account.get('RequiresOpenaiAuth'),
        'HasAccount': account.get('HasAccount'),
        'ConfigReadSucceeded': config.get('ConfigReadSucceeded', False),
        'EffectiveConfig': config,
        'RoutingConfigFiles': file_configs,
        'RoutingConfigSource': config_source,
        'ExpectedBackendClass': expected,
        'RoutingConsistency': consistency,
        'SchemaProbe': schema,
        'RuntimeError': runtime_error,
        'RuntimeStderrTail': runtime_stderr,
        'Environment': environment_snapshot(runtime_env),
        'ResolvedCfrCodexHome': str(resolved_home.path),
        'CodexHomeSource': resolved_home.source,
        'ChildCodexHomeExplicit': True,
        'ConfigReadCodexHome': str(resolved_home.path),
        'ChildCodexHome': runtime_env.get('CODEX_HOME'),
        'CfrCodexHomeBoundary': 'PASS' if runtime_env.get('CODEX_HOME') == str(resolved_home.path) else 'FAIL',
        'CodexExecutable': _safe_executable_snapshot(launcher),
        'ReadOnlyCfrCodexHomeSnapshot': codex_home_readonly_snapshot(resolved_home.path),
    }


def _safe_executable_snapshot(launcher: CodexLauncher):
    try:
        resolved = launcher.resolve()
        return {
            'Path': str(resolved.path),
            'Source': resolved.source,
            'Kind': resolved.kind,
        }
    except Exception as exc:
        return {'Path': None, 'Source': None, 'Kind': None, 'ErrorClass': type(exc).__name__}
