import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import time
import re
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from cfr.codex.app_server import AppServerClient, AppServerRpcError
from cfr.codex.diagnostics import gate_metadata, inspect_runtime, normalize_gate_origin, routing_decision, safe_error
from cfr.codex.launcher import CodexLauncher
from cfr.codex.threads import ThreadManager
from cfr.codex.turns import TurnManager
from cfr.config import child_process_env, resolve_cfr_codex_home
from cfr.network import proxy_child_env, proxy_endpoint, resolve_proxy, sanitized_proxy_url

HOST = 'api.openai.com'
PORT = 443


def now():
    return datetime.now(timezone.utc).isoformat()


def proxy_snapshot(resolution):
    names = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')
    environment = {}
    for name in names:
        value = os.environ.get(name)
        environment[name] = {'present': value is not None, 'sanitized': sanitized_proxy_url(value) if value else None}
    return {
        'source': resolution.source,
        'environment': environment,
        'http_proxy': sanitized_proxy_url(resolution.http_proxy),
        'https_proxy': sanitized_proxy_url(resolution.https_proxy),
        'no_proxy_present': bool(resolution.no_proxy),
    }


def windows_proxy_snapshot():
    if os.name != 'nt':
        return {'available': False, 'reason': 'not_windows'}
    try:
        completed = subprocess.run(['netsh', 'winhttp', 'show', 'proxy'], capture_output=True, text=True, timeout=10)
        return {'available': True, 'exit_code': completed.returncode, 'summary': (completed.stdout or completed.stderr)[-2000:]}
    except Exception as exc:
        return {'available': False, 'error': str(exc)}


def executable_snapshot():
    paths = {name: shutil.which(name) for name in ('codex', 'codex.cmd')}
    try:
        selected = CodexLauncher().discover()
        command = [str(selected), '--version']
        if selected.suffix.lower() in ('.cmd', '.bat'):
            command = [os.environ.get('COMSPEC', r'C:\Windows\System32\cmd.exe'), '/d', '/s', '/c', str(selected), '--version']
        version = subprocess.run(command, capture_output=True, text=True, timeout=10)
        return {'where': paths, 'selected': str(selected), 'version': (version.stdout or version.stderr).strip()}
    except Exception as exc:
        return {'where': paths, 'selected': None, 'error': str(exc)}


def direct_path_probe():
    result = {'DirectDnsResolved': False, 'DirectTcp443Connected': False, 'DirectTlsHandshake': 'FAIL'}
    try:
        result['DirectDnsResolved'] = bool(socket.getaddrinfo(HOST, PORT, type=socket.SOCK_STREAM))
    except Exception as exc:
        result['DirectDnsError'] = str(exc)
        return result
    try:
        with socket.create_connection((HOST, PORT), timeout=5) as raw:
            result['DirectTcp443Connected'] = True
            context = ssl.create_default_context()
            with context.wrap_socket(raw, server_hostname=HOST) as tls:
                tls.getpeercert()
                result['DirectTlsHandshake'] = 'PASS'
    except Exception as exc:
        result['DirectTcpError'] = str(exc)
        if result['DirectTcp443Connected']:
            result['DirectTlsError'] = str(exc)
    return result


def proxy_listener_probe(resolution):
    result = {'ProxyListenerReachable': False, 'ProxyConnectTunnel': 'NOT_RUN', 'ProxyHttpsReachable': False, 'ProxyHttpStatus': None}
    proxy_url = resolution.selected_proxy
    if not proxy_url:
        return result
    try:
        host, port = proxy_endpoint(proxy_url)
    except Exception as exc:
        result['ProxyError'] = str(exc)
        return result
    result['ProxyHost'] = host
    result['ProxyPort'] = port
    try:
        with socket.create_connection((host, port), timeout=5):
            result['ProxyListenerReachable'] = True
    except Exception as exc:
        result['ProxyListenerError'] = str(exc)
        return result
    handlers = urllib.request.ProxyHandler({'http': resolution.http_proxy or proxy_url, 'https': resolution.https_proxy or proxy_url})
    opener = urllib.request.build_opener(handlers)
    try:
        request = urllib.request.Request(f'https://{HOST}/v1/models', method='GET')
        with opener.open(request, timeout=8) as response:
            result['ProxyHttpStatus'] = response.status
            result['ProxyHttpsReachable'] = response.status in (200, 401, 403, 404)
            result['ProxyConnectTunnel'] = 'PASS' if result['ProxyHttpsReachable'] else 'FAIL'
            return result
    except urllib.error.HTTPError as exc:
        result['ProxyHttpStatus'] = exc.code
        result['ProxyHttpsReachable'] = exc.code in (200, 401, 403, 404)
        result['ProxyConnectTunnel'] = 'PASS' if result['ProxyHttpsReachable'] else 'FAIL'
        return result
    except Exception as exc:
        result['ProxyHttpsError'] = str(exc)
    if proxy_url.lower().startswith('http://'):
        try:
            host, port = proxy_endpoint(proxy_url)
            with socket.create_connection((host, port), timeout=5) as connection:
                connection.sendall(f'CONNECT {HOST}:{PORT} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nConnection: close\r\n\r\n'.encode('ascii'))
                status_line = connection.recv(4096).splitlines()[0].decode('latin1', 'replace')
                result['ProxyConnectStatusLine'] = status_line
                if ' 200 ' in status_line:
                    result['ProxyConnectTunnel'] = 'PASS'
                    context = ssl.create_default_context()
                    with context.wrap_socket(connection, server_hostname=HOST):
                        result['ProxyHttpsReachable'] = True
                else:
                    result['ProxyConnectTunnel'] = 'FAIL'
        except Exception as exc:
            result['ProxyConnectError'] = str(exc)
    return result


def classify_transport(error):
    text = str(error).lower()
    if any(word in text for word in ('401', '403', 'unauthorized', 'authentication', 'auth')):
        return 'CODEX_AUTH_FAILURE'
    if any(word in text for word in ('stream disconnected', 'error sending request', 'connection', 'timed out', 'timeout', 'dns', 'tls', 'certificate')):
        return 'CODEX_MODEL_NETWORK_FAILURE'
    if isinstance(error, AppServerRpcError):
        return 'CODEX_PROTOCOL_FAILURE'
    return 'UNKNOWN'


def websocket_config_overrides(config):
    overrides = []
    for path, value in (config.get('WebsocketFeatureCapabilities') or {}).items():
        if isinstance(value, bool):
            match = re.search(r'(features\.(?:responses_websockets|responses_websockets_v2))$', path)
            if match:
                overrides.append(f'{match.group(1)}=false')
    return overrides


def append_event(path, event):
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')


def model_probe(workspace, timeout, events_path, process_env=None, marker='CFR_NETWORK_PREFLIGHT_ACK_001', config_overrides=None, codex_home=None):
    result = {'AppServerHandshake': 'NO', 'ThreadStart': 'NO', 'TurnStart': 'NO', 'TurnCompleted': 'NO', 'FinalAck': 'NO', 'TurnError': None, 'ThreadId': None}
    client = AppServerClient(timeout=timeout, process_env=process_env, config_overrides=config_overrides, codex_home=codex_home)
    try:
        client.start()
        result['AppServerHandshake'] = 'YES'
        append_event(events_path, {'at': now(), 'method': 'initialize', 'status': 'ok'})
        started = client.request('thread/start', {'cwd': str(workspace), 'ephemeral': True, 'threadSource': 'user', 'historyMode': 'legacy', 'sessionStartSource': 'startup'})
        thread = ThreadManager.ref_from_result(started, workspace)
        result['ThreadStart'] = 'YES'
        result['ThreadId'] = thread.thread_id
        append_event(events_path, {'at': now(), 'method': 'thread/start', 'status': 'ok', 'thread_id': thread.thread_id})
        prompt_marker = marker.replace('_ACK_', '_TEST_')
        turn = TurnManager(client).run_turn(thread.thread_id, f'{prompt_marker}: Do not call tools or modify files. Reply exactly {marker}.', timeout)
        result['TurnStart'] = 'YES'
        result['TurnCompleted'] = turn.status
        result['FinalAck'] = 'YES' if marker in turn.final_agent_message else 'NO'
        result['TurnError'] = turn.error_message
        append_event(events_path, {'at': now(), 'method': 'turn/completed', 'status': turn.status, 'turn_id': turn.turn_id, 'error': turn.error_message})
        return result, 'PASS' if turn.status == 'completed' and result['FinalAck'] == 'YES' else classify_transport(turn.error_message or turn.status)
    except Exception as exc:
        result['TurnError'] = str(exc)
        append_event(events_path, {'at': now(), 'method': 'probe', 'status': 'error', 'error': str(exc)})
        return result, classify_transport(exc)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--retry-interval', type=float, default=5)
    parser.add_argument('--codex-home', default=None)
    parser.add_argument('--gate-origin', choices=('desktop_agent', 'host_manual', 'unknown'), default='unknown')
    args = parser.parse_args()
    gate_origin = normalize_gate_origin(args.gate_origin)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = ROOT / '.tmp' / 'network-preflight' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    workspace = ROOT / '.tmp' / 'network_preflight_workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    log_path = artifact_dir / 'preflight.log'
    events_path = artifact_dir / 'app_server_events.jsonl'
    events_path.touch()
    resolution = resolve_proxy()
    resolved_home = resolve_cfr_codex_home(args.codex_home)
    direct = direct_path_probe()
    proxy = proxy_listener_probe(resolution) if resolution.mode == 'proxy' else {'ProxyListenerReachable': False, 'ProxyConnectTunnel': 'NOT_RUN', 'ProxyHttpsReachable': False, 'ProxyHttpStatus': None}
    process_env = child_process_env(proxy_child_env(resolution), resolved_home)
    runtime = inspect_runtime(ROOT, process_env=process_env, event_sink=lambda event: append_event(events_path, event), timeout=min(args.timeout, 30), codex_home=args.codex_home)
    effective_config = runtime.get('EffectiveConfig') or {}
    expected_backend, routing_consistency = routing_decision(runtime.get('AuthMode', 'UNKNOWN'), effective_config)
    result = {
        'RunId': run_id, 'started_at': now(), 'OS': platform.platform(), 'PythonVersion': platform.python_version(),
        **gate_metadata(gate_origin),
        'CODEX_HOME': 'present' if 'CODEX_HOME' in os.environ else 'absent', 'ProxyDiagnosis': proxy_snapshot(resolution),
        'WindowsProxy': windows_proxy_snapshot(), 'Codex': executable_snapshot(), **direct, **proxy,
        'SystemProxyDetected': resolution.mode == 'proxy', 'SystemProxySource': resolution.source,
        'SystemProxyUrl': sanitized_proxy_url(resolution.selected_proxy), 'CodexDirectProbe': 'NOT_RUN', 'CodexProxyProbe': 'NOT_RUN',
        'SelectedNetworkMode': 'none', 'Classification': 'UNKNOWN', 'Attempts': [],
        'LoginStatus': runtime.get('LoginStatus'), 'AuthMode': runtime.get('AuthMode'), 'PlanType': runtime.get('PlanType'),
        'ExpectedBackendClass': expected_backend, 'EffectiveProvider': {
            'model_provider': effective_config.get('ModelProvider'),
            'base_url': effective_config.get('ProviderBaseUrl'),
            'wire_api': effective_config.get('ProviderWireApi'),
            'requires_openai_auth': effective_config.get('ProviderRequiresOpenaiAuth'),
            'supports_websockets': effective_config.get('ProviderSupportsWebsockets'),
        },
        'EffectiveBaseUrlSanitized': effective_config.get('EffectiveBaseUrlSanitized'),
        'RoutingConfigSource': runtime.get('RoutingConfigSource'),
        'RoutingConsistency': routing_consistency,
        'ChatGptBackendReachable': False,
        'DefaultTransportProbe': 'NOT_RUN',
        'HttpOnlyTransportProbe': 'NOT_RUN',
        'TransportConfigOverrides': websocket_config_overrides(effective_config),
        'ResolvedCfrCodexHome': runtime.get('ResolvedCfrCodexHome'),
        'CodexHomeSource': runtime.get('CodexHomeSource'),
        'ChildCodexHomeExplicit': runtime.get('ChildCodexHomeExplicit'),
        'ConfigReadCodexHome': runtime.get('ConfigReadCodexHome'),
        'ChildCodexHome': runtime.get('ChildCodexHome'),
        'CfrCodexHomeBoundary': runtime.get('CfrCodexHomeBoundary'),
        'RuntimeError': runtime.get('RuntimeError'),
        'RuntimeStderrTail': runtime.get('RuntimeStderrTail'),
        'ReadOnlyCfrCodexHomeSnapshot': runtime.get('ReadOnlyCfrCodexHomeSnapshot'),
        'GateInterpretation': 'NOT_REQUIRED',
        'ProxyDecisionRequired': 'NO',
    }
    direct_transport = direct.get('DirectDnsResolved') and direct.get('DirectTcp443Connected') and direct.get('DirectTlsHandshake') == 'PASS'
    proxy_transport = proxy.get('ProxyListenerReachable') and proxy.get('ProxyHttpsReachable')
    auth_gate = runtime.get('AccountReadSucceeded') and runtime.get('ConfigReadSucceeded') and runtime.get('AuthMode') in ('CHATGPT',) and routing_consistency == 'CONSISTENT'
    if auth_gate and direct_transport:
        probe, classification = model_probe(workspace, args.timeout, events_path, None, 'CFR_DIRECT_NETWORK_ACK_001', codex_home=resolved_home.path)
        result['Attempts'].append({'mode': 'direct', 'probe': probe, 'classification': classification})
        result['CodexDirectProbe'] = classification
        result['DefaultTransportProbe'] = probe
        if classification == 'PASS':
            result['SelectedNetworkMode'] = 'direct'
            result['Classification'] = 'PASS_DIRECT'
    if auth_gate and result['Classification'] != 'PASS_DIRECT' and proxy_transport:
        chatgpt_urls = ('https://chatgpt.com/backend-api/codex/models', 'https://chatgpt.com/backend-api/codex/responses')
        handlers = urllib.request.ProxyHandler({'http': resolution.http_proxy or resolution.selected_proxy, 'https': resolution.https_proxy or resolution.selected_proxy})
        opener = urllib.request.build_opener(handlers)
        checks = []
        for url in chatgpt_urls:
            check = {'url': sanitized_proxy_url(url), 'reachable': False}
            try:
                with opener.open(urllib.request.Request(url, method='GET'), timeout=8) as response:
                    check['status'] = response.status
                    check['reachable'] = response.status in (200, 401, 403, 404, 405)
            except urllib.error.HTTPError as exc:
                check['status'] = exc.code
                check['reachable'] = exc.code in (200, 401, 403, 404, 405)
            except Exception as exc:
                check['error_class'] = type(exc).__name__
            checks.append(check)
        result['ChatGptBackendReachable'] = bool(checks) and all(item['reachable'] for item in checks)
        result['ChatGptBackendChecks'] = checks
    if auth_gate and result['Classification'] != 'PASS_DIRECT' and proxy_transport and result['ChatGptBackendReachable']:
        for attempt in range(1, 3):
            probe, classification = model_probe(workspace, args.timeout, events_path, process_env, 'CFR_PROXY_NETWORK_ACK_001', codex_home=resolved_home.path)
            result['Attempts'].append({'mode': 'proxy', 'attempt': attempt, 'probe': probe, 'classification': classification})
            result['CodexProxyProbe'] = classification
            if result['DefaultTransportProbe'] == 'NOT_RUN':
                result['DefaultTransportProbe'] = probe
            if classification == 'PASS':
                result['SelectedNetworkMode'] = 'proxy'
                result['Classification'] = 'PASS_PROXY'
                break
            if attempt == 1:
                time.sleep(args.retry_interval)
    if auth_gate and result['Classification'] not in ('PASS_DIRECT', 'PASS_PROXY') and result['TransportConfigOverrides'] and (resolution.mode != 'proxy' or result['ChatGptBackendReachable']):
        http_env = None if direct_transport and result['CodexDirectProbe'] != 'NOT_RUN' else process_env
        http_probe, http_classification = model_probe(
            workspace,
            args.timeout,
            events_path,
            http_env,
            'CFR_HTTP_ONLY_ACK_001',
            result['TransportConfigOverrides'],
            resolved_home.path,
        )
        result['HttpOnlyTransportProbe'] = http_probe
        result['HttpOnlyTransportClassification'] = http_classification
        if http_classification == 'PASS':
            result['Classification'] = 'HTTP_FALLBACK_WORKAROUND_CONFIRMED'
            result['SelectedNetworkMode'] = 'direct' if http_env is None else 'proxy'
    if result['Classification'] == 'UNKNOWN' and (not runtime.get('AccountReadSucceeded') or not runtime.get('ConfigReadSucceeded')):
        result['Classification'] = 'AUTH_STATE_UNKNOWN'
    elif result['Classification'] == 'UNKNOWN' and runtime.get('AuthMode') == 'API_KEY':
        result['Classification'] = 'AUTH_MODE_DECISION_REQUIRED'
    elif result['Classification'] == 'UNKNOWN' and runtime.get('AuthMode') == 'UNKNOWN':
        result['Classification'] = 'AUTH_STATE_UNKNOWN'
    elif result['Classification'] == 'UNKNOWN' and routing_consistency == 'MISMATCH':
        result['Classification'] = 'CHATGPT_AUTH_PROVIDER_ROUTE_MISMATCH'
    elif result['Classification'] == 'UNKNOWN':
        if resolution.mode == 'proxy' and not proxy.get('ProxyListenerReachable'):
            result['Classification'] = 'PROXY_DETECTED_BUT_LISTENER_DOWN'
        elif resolution.mode == 'proxy' and not proxy.get('ProxyHttpsReachable'):
            result['Classification'] = 'PROXY_CONNECT_FAILURE' if proxy.get('ProxyConnectTunnel') != 'PASS' else 'PROXY_TLS_FAILURE'
        elif resolution.mode == 'proxy' and proxy_transport:
            result['Classification'] = 'CODEX_PROXY_NETWORK_FAILURE'
        elif not direct.get('DirectDnsResolved'):
            result['Classification'] = 'DNS_FAILURE'
        elif not direct.get('DirectTcp443Connected'):
            result['Classification'] = 'DIRECT_TCP_BLOCKED'
        elif direct.get('DirectTlsHandshake') != 'PASS':
            result['Classification'] = 'PROXY_TLS_FAILURE'
        else:
            result['Classification'] = 'UNKNOWN'
    runtime_text = ' '.join([str(result.get('RuntimeError') or ''), *result.get('RuntimeStderrTail', [])])
    runtime_home_failure = bool(re.search(r'(?i)(access denied|stdout eof|initialize sqlite state runtime)', runtime_text))
    if runtime_home_failure:
        result['GateInterpretation'] = 'HOST_RUNTIME_VALIDATION_REQUIRED' if gate_origin != 'host_manual' else 'HOST_CODEX_HOME_RUNTIME_INIT_FAILURE'
        result['ProxyDecisionRequired'] = 'NO'
    result['DnsResolved'] = result['DirectDnsResolved']
    result['Tcp443Connected'] = result['DirectTcp443Connected']
    result['TlsHandshake'] = result['DirectTlsHandshake']
    result['completed_at'] = now()
    log_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    (artifact_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'RunId': run_id, 'ArtifactDir': str(artifact_dir), 'Preflight': result['Classification'], 'result': result}, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
