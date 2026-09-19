import argparse
import json
import platform
import re
import shutil
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from cfr.codex.app_server import AppServerClient
from cfr.codex.diagnostics import (
    gate_metadata,
    inspect_runtime,
    normalize_gate_origin,
    routing_decision,
    safe_error,
    sanitize_endpoint,
)
from cfr.codex.launcher import CodexLauncher
from cfr.codex.threads import ThreadManager
from cfr.codex.turns import TurnManager
from cfr.config import child_process_env, resolve_cfr_codex_home
from cfr.network import proxy_child_env, resolve_proxy, sanitized_proxy_url


def now():
    return datetime.now(timezone.utc).isoformat()


def append_event(path, event):
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'at': now(), **event}, ensure_ascii=False, default=str) + '\n')


def executable_snapshot():
    paths = {name: shutil.which(name) for name in ('codex', 'codex.cmd')}
    try:
        selected = CodexLauncher().discover()
        command = CodexLauncher().build_command('--version')
        import subprocess
        completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
        return {
            'CodexExecutable': str(selected),
            'CodexVersion': (completed.stdout or completed.stderr).strip(),
            'Where': paths,
        }
    except Exception as exc:
        return {'CodexExecutable': None, 'CodexVersion': None, 'Where': paths, 'ErrorClass': type(exc).__name__}


def transport_error(result):
    return safe_error(result.get('TurnError'))


def run_model_probe(project_root, timeout, process_env, events_path, marker, config_overrides=None, codex_home=None):
    result = {
        'AppServerHandshake': 'NO',
        'ThreadStart': 'NO',
        'TurnStart': 'NO',
        'TurnStatus': 'NOT_RUN',
        'FinalAck': 'NO',
        'EndpointFromError': None,
        'Error': None,
        'ThreadId': None,
        'ConfigOverrides': list(config_overrides or []),
    }
    client = AppServerClient(timeout=timeout, process_env=process_env, config_overrides=config_overrides, codex_home=codex_home)
    try:
        client.start()
        result['AppServerHandshake'] = 'YES'
        append_event(events_path, {'method': 'initialize', 'status': 'ok', 'probe': marker})
        started = client.request('thread/start', {
            'cwd': str(project_root),
            'ephemeral': True,
            'threadSource': 'user',
            'historyMode': 'legacy',
            'sessionStartSource': 'startup',
        })
        thread = ThreadManager.ref_from_result(started, project_root)
        result['ThreadStart'] = 'YES'
        result['ThreadId'] = thread.thread_id
        append_event(events_path, {'method': 'thread/start', 'status': 'ok', 'thread_id': thread.thread_id, 'probe': marker})
        turn = TurnManager(client).run_turn(
            thread.thread_id,
            f'{marker}_TEST: Do not call tools or modify files. Reply exactly {marker}.',
            timeout,
        )
        result['TurnStart'] = 'YES'
        result['TurnStatus'] = turn.status
        result['FinalAck'] = 'YES' if marker in turn.final_agent_message else 'NO'
        result['Error'] = safe_error(turn.error_message)
        result['EndpointFromError'] = sanitize_endpoint(turn.error_message)
        append_event(events_path, {
            'method': 'turn/completed',
            'status': turn.status,
            'final_ack': result['FinalAck'],
            'endpoint': result['EndpointFromError'],
            'probe': marker,
        })
        return result
    except Exception as exc:
        result['TurnStatus'] = 'failed'
        result['Error'] = safe_error(exc)
        result['EndpointFromError'] = sanitize_endpoint(str(exc))
        append_event(events_path, {'method': 'probe', 'status': 'error', 'error_class': type(exc).__name__, 'endpoint': result['EndpointFromError'], 'probe': marker})
        return result
    finally:
        client.close()


def proxy_reachability(urls, resolution):
    if resolution.mode != 'proxy':
        return {'ChatGptBackendReachable': False, 'Checks': [], 'Reason': 'no_proxy'}
    proxy_url = resolution.selected_proxy
    checks = []
    handler = urllib.request.ProxyHandler({'http': resolution.http_proxy or proxy_url, 'https': resolution.https_proxy or proxy_url})
    opener = urllib.request.build_opener(handler)
    for url in urls:
        check = {'Url': sanitized_proxy_url(url), 'Path': url.split('://', 1)[-1].split('/', 1)[-1] if '/' in url.split('://', 1)[-1] else '/'}
        try:
            request = urllib.request.Request(url, method='GET')
            with opener.open(request, timeout=8) as response:
                check['HttpStatus'] = response.status
                check['Reachable'] = response.status in (200, 401, 403, 404, 405)
        except urllib.error.HTTPError as exc:
            check['HttpStatus'] = exc.code
            check['Reachable'] = exc.code in (200, 401, 403, 404, 405)
        except Exception as exc:
            check['Reachable'] = False
            check['ErrorClass'] = type(exc).__name__
        checks.append(check)
    return {'ChatGptBackendReachable': bool(checks) and all(item.get('Reachable') for item in checks), 'Checks': checks}


def websocket_overrides(config):
    capabilities = config.get('WebsocketFeatureCapabilities') or {}
    overrides = []
    for path, value in capabilities.items():
        if not isinstance(value, bool):
            continue
        match = re.search(r'(features\.(?:responses_websockets|responses_websockets_v2))$', path)
        if match:
            overrides.append(f'{match.group(1)}=false')
    return overrides


def classify_result(runtime, default_probe=None, http_probe=None, chatgpt_reachable=False):
    auth_mode = runtime.get('AuthMode')
    consistency = runtime.get('RoutingConsistency')
    if auth_mode == 'API_KEY':
        return 'AUTH_MODE_DECISION_REQUIRED', 'API_KEY_MODE'
    if auth_mode == 'UNKNOWN':
        return 'AUTH_STATE_UNKNOWN', 'UNKNOWN'
    if consistency == 'MISMATCH':
        return 'CHATGPT_AUTH_PROVIDER_ROUTE_MISMATCH', 'CHATGPT_MODE'
    if not chatgpt_reachable:
        return 'CHATGPT_ROUTE_PROXY_FAILURE', 'CHATGPT_MODE'
    if default_probe and default_probe.get('TurnStatus') == 'completed' and default_probe.get('FinalAck') == 'YES':
        return 'PASS', 'CHATGPT_MODE'
    if http_probe and http_probe.get('TurnStatus') == 'completed' and http_probe.get('FinalAck') == 'YES':
        return 'HTTP_FALLBACK_WORKAROUND_CONFIRMED', 'CHATGPT_MODE'
    return 'CODEX_TRANSPORT_PROXY_FAILURE', 'CHATGPT_MODE'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--codex-home', default=None)
    parser.add_argument('--gate-origin', choices=('desktop_agent', 'host_manual', 'unknown'), default='unknown')
    args = parser.parse_args()
    gate_origin = normalize_gate_origin(args.gate_origin)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = ROOT / '.tmp' / 'auth-routing-diagnostic' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    events_path = artifact_dir / 'app_server_events.jsonl'
    events_path.touch()
    log_path = artifact_dir / 'diagnostic.log'
    resolution = resolve_proxy()
    resolved_home = resolve_cfr_codex_home(args.codex_home)
    process_env = child_process_env(proxy_child_env(resolution), resolved_home)
    executable = executable_snapshot()
    runtime = inspect_runtime(ROOT, process_env=process_env, event_sink=lambda event: append_event(events_path, event), timeout=args.timeout, codex_home=args.codex_home)
    effective_config = runtime.get('EffectiveConfig') or {}
    expected, consistency = routing_decision(runtime.get('AuthMode', 'UNKNOWN'), effective_config)
    runtime['ExpectedBackendClass'] = expected
    runtime['RoutingConsistency'] = consistency
    result = {
        'RunId': run_id,
        **gate_metadata(gate_origin),
        'started_at': now(),
        'OS': platform.platform(),
        'PythonVersion': platform.python_version(),
        'CodexExecutable': executable.get('CodexExecutable'),
        'CodexVersion': executable.get('CodexVersion'),
        'CODEX_HOME': runtime.get('ResolvedCfrCodexHome'),
        'ResolvedCfrCodexHome': runtime.get('ResolvedCfrCodexHome'),
        'CodexHomeSource': runtime.get('CodexHomeSource'),
        'ChildCodexHomeExplicit': runtime.get('ChildCodexHomeExplicit'),
        'ConfigReadCodexHome': runtime.get('ConfigReadCodexHome'),
        'ChildCodexHome': runtime.get('ChildCodexHome'),
        'CfrCodexHomeBoundary': runtime.get('CfrCodexHomeBoundary'),
        'LoginStatus': runtime.get('LoginStatus'),
        'AuthMode': runtime.get('AuthMode'),
        'PlanType': runtime.get('PlanType'),
        'AccountReadSucceeded': runtime.get('AccountReadSucceeded'),
        'HasAccount': runtime.get('HasAccount'),
        'RequiresOpenaiAuth': runtime.get('RequiresOpenaiAuth'),
        'ModelProvider': effective_config.get('ModelProvider'),
        'OpenAiBaseUrl': effective_config.get('OpenAiBaseUrl'),
        'ChatGptBaseUrl': effective_config.get('ChatGptBaseUrl'),
        'ProviderBaseUrl': effective_config.get('ProviderBaseUrl'),
        'EffectiveBaseUrlSanitized': effective_config.get('EffectiveBaseUrlSanitized'),
        'RoutingConfigSource': runtime.get('RoutingConfigSource'),
        'RoutingConfigFiles': runtime.get('RoutingConfigFiles'),
        'ExpectedBackendClass': expected,
        'ObservedErrorEndpoint': None,
        'RoutingConsistency': consistency,
        'Proxy': {
            'Detected': resolution.mode == 'proxy',
            'Source': resolution.source,
            'HostPort': sanitized_proxy_url(resolution.selected_proxy),
        },
        'Environment': runtime.get('Environment'),
        'AccountRead': runtime.get('AccountRead'),
        'ConfigReadSucceeded': runtime.get('ConfigReadSucceeded'),
        'EffectiveConfig': effective_config,
        'SchemaProbe': runtime.get('SchemaProbe'),
        'RuntimeError': runtime.get('RuntimeError'),
        'RuntimeStderrTail': runtime.get('RuntimeStderrTail'),
        'ReadOnlyCfrCodexHomeSnapshot': runtime.get('ReadOnlyCfrCodexHomeSnapshot'),
        'ChatGptBackendReachable': False,
        'DefaultTransportProbe': 'NOT_RUN',
        'HttpOnlyTransportProbe': 'NOT_RUN',
        'AuthRoutingVerdict': 'UNKNOWN',
        'Verdict': 'AUTH_STATE_UNKNOWN',
        'UserActionRequired': 'NO',
        'UserAction': None,
        'AuthDecisionRequired': 'NO',
        'HostValidationRequired': 'NO',
        'GateInterpretation': 'NOT_REQUIRED',
    }
    if not runtime.get('AccountReadSucceeded') or not runtime.get('ConfigReadSucceeded'):
        result['AuthRoutingVerdict'] = 'UNKNOWN'
        result['Verdict'] = 'AUTH_STATE_UNKNOWN'
        runtime_text = ' '.join([str(runtime.get('RuntimeError') or ''), *runtime.get('RuntimeStderrTail', [])])
        runtime_home_failure = bool(re.search(r'(?i)(access denied|stdout eof|initialize sqlite state runtime)', runtime_text))
        if runtime_home_failure and gate_origin != 'host_manual':
            result['GateInterpretation'] = 'HOST_RUNTIME_VALIDATION_REQUIRED'
            result['HostValidationRequired'] = 'YES'
            result['UserAction'] = 'Run the same gate from a normal Host PowerShell; this Desktop-agent runtime cannot establish the host CFR CODEX_HOME result.'
        else:
            result['GateInterpretation'] = 'HOST_CODEX_HOME_RUNTIME_INIT_FAILURE' if runtime_home_failure else 'AUTH_RUNTIME_READ_FAILED'
            result['UserAction'] = 'Resolve the runtime prerequisite before changing auth or provider configuration.'
    elif runtime.get('AuthMode') == 'API_KEY':
        result['AuthRoutingVerdict'] = 'API_KEY_MODE'
        result['Verdict'] = 'AUTH_MODE_DECISION_REQUIRED'
        result['AuthDecisionRequired'] = 'YES'
        result['UserActionRequired'] = 'YES'
        result['UserAction'] = 'Decide whether CFR should use API-key billing or the existing ChatGPT/Codex account semantics before changing auth mode.'
    elif runtime.get('AuthMode') == 'UNKNOWN':
        result['AuthRoutingVerdict'] = 'UNKNOWN'
        result['Verdict'] = 'AUTH_STATE_UNKNOWN'
        result['AuthDecisionRequired'] = 'YES'
        result['UserActionRequired'] = 'YES'
        result['UserAction'] = 'Authenticate the Codex runtime or explicitly choose the intended auth mode; no login or auth change was performed by this diagnostic.'
    elif consistency == 'MISMATCH':
        result['AuthRoutingVerdict'] = 'CHATGPT_MODE'
        result['Verdict'] = 'CHATGPT_AUTH_PROVIDER_ROUTE_MISMATCH'
        result['AuthDecisionRequired'] = 'YES'
        result['UserActionRequired'] = 'YES'
        result['UserAction'] = 'Confirm whether the effective provider/base_url override should be removed; no configuration was changed.'
    else:
        result['AuthRoutingVerdict'] = 'CHATGPT_MODE'
        reachability = proxy_reachability([
            'https://chatgpt.com/backend-api/codex/models',
            'https://chatgpt.com/backend-api/codex/responses',
        ], resolution)
        result['ChatGptBackendReachable'] = reachability.get('ChatGptBackendReachable', False)
        result['ChatGptBackendChecks'] = reachability.get('Checks', [])
        if result['ChatGptBackendReachable']:
            default_probe = run_model_probe(ROOT, args.timeout, process_env, events_path, 'CFR_DEFAULT_TRANSPORT_ACK_001', codex_home=resolved_home.path)
            result['DefaultTransportProbe'] = default_probe
            result['ObservedErrorEndpoint'] = default_probe.get('EndpointFromError')
            overrides = websocket_overrides(effective_config)
            if not (default_probe.get('TurnStatus') == 'completed' and default_probe.get('FinalAck') == 'YES') and overrides:
                http_probe = run_model_probe(ROOT, args.timeout, process_env, events_path, 'CFR_HTTP_ONLY_ACK_001', overrides, resolved_home.path)
                result['HttpOnlyTransportProbe'] = http_probe
                result['ObservedErrorEndpoint'] = result['ObservedErrorEndpoint'] or http_probe.get('EndpointFromError')
            elif not overrides:
                result['HttpOnlyTransportProbe'] = {'Status': 'NOT_AVAILABLE', 'Reason': 'websocket feature capability not confirmed'}
            verdict, _ = classify_result(runtime, result.get('DefaultTransportProbe'), result.get('HttpOnlyTransportProbe') if isinstance(result.get('HttpOnlyTransportProbe'), dict) else None, True)
            result['Verdict'] = verdict
        else:
            result['Verdict'] = 'CHATGPT_ROUTE_PROXY_FAILURE'
    result['completed_at'] = now()
    safe_log = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    log_path.write_text(safe_log, encoding='utf-8')
    (artifact_dir / 'result.json').write_text(safe_log, encoding='utf-8')
    print(json.dumps({'RunId': run_id, 'ArtifactDir': str(artifact_dir), 'Verdict': result['Verdict'], 'result': result}, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
