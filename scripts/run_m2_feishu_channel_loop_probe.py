"""Fresh-process native Channel SDK event-loop diagnostic probe."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cfr.feishu.log_sanitize import sanitize_feishu_log_text


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def sanitize(text):
    return sanitize_feishu_log_text(text or '')[-20000:]


def safe_error(error):
    from cfr.feishu.sdk_compat import classify_channel_error
    return {
        'class': type(error).__name__,
        'code': getattr(error, 'code', None) or classify_channel_error(error) or 'UNKNOWN',
    }


async def connect_and_disconnect(channel, timeout):
    try:
        await channel.connect_until_ready(timeout=timeout)
        return {'ready': bool(getattr(channel, 'is_ready', False)), 'bot_identity_resolved': bool(getattr(channel, 'bot_identity', None))}
    finally:
        disconnect = getattr(channel, 'disconnect', None)
        if disconnect is not None:
            await disconnect()


def worker(mode, timeout):
    result = {'mode': mode, 'status': 'FAIL', 'error': None, 'ready': False, 'bot_identity_resolved': False}
    try:
        from cfr.feishu.config import load_settings
        settings = load_settings(database=ROOT / 'cfr.sqlite3')
        settings.validate_connection()
        app_id = settings.app_id
        app_secret = settings.app_secret
        if not app_id or not app_secret:
            result['error'] = {'class': 'ConfigurationError', 'code': 'FEISHU_LIVE_SETUP_REQUIRED'}
            print(json.dumps(result))
            return 2
        if mode == 'official-preimport':
            from lark_channel import FeishuChannel
            channel = FeishuChannel(app_id=app_id, app_secret=app_secret)
            result.update(asyncio.run(connect_and_disconnect(channel, timeout)))
        elif mode == 'lazy-import-repro':
            async def lazy_main():
                from lark_channel import FeishuChannel
                channel = FeishuChannel(app_id=app_id, app_secret=app_secret)
                return await connect_and_disconnect(channel, timeout)
            result.update(asyncio.run(lazy_main()))
        elif mode == 'official-preimport-with-compat':
            from lark_channel import FeishuChannel
            from cfr.feishu.sdk_compat import prepare_channel_sdk_runtime
            diagnostic = prepare_channel_sdk_runtime()
            result['compatibility_mode'] = diagnostic.compatibility_mode
            channel = FeishuChannel(app_id=app_id, app_secret=app_secret)
            result.update(asyncio.run(connect_and_disconnect(channel, timeout)))
        elif mode == 'cfr-transport':
            from cfr.feishu.transport import ChannelFeishuTransport
            transport = ChannelFeishuTransport(settings, connect_timeout=timeout)
            try:
                transport.connect_until_ready(timeout=timeout)
                result['ready'] = transport.connection_state == 'ready'
                result['bot_identity_resolved'] = bool(getattr(transport._channel, 'bot_identity', None))
                result['compatibility_mode'] = getattr(getattr(transport, '_sdk_loop_diagnostic', None), 'compatibility_mode', None)
            finally:
                transport.stop()
            result['status'] = 'PASS' if result['ready'] else 'FAIL'
            print(json.dumps(result))
            return 0 if result['status'] == 'PASS' else 2
        else:
            raise ValueError(f'unknown probe mode: {mode}')
        result['status'] = 'PASS' if result['ready'] else 'FAIL'
    except Exception as exc:
        result['error'] = safe_error(exc)
        if result['error']['code'] == 'FEISHU_SDK_EVENT_LOOP_CONFLICT':
            result['status'] = 'EVENT_LOOP_CONFLICT_REPRODUCED'
    print(json.dumps(result))
    return 0 if result['status'] in {'PASS', 'EVENT_LOOP_CONFLICT_REPRODUCED'} else 2


def parse_worker(stdout, stderr, mode, returncode):
    lines = (stdout or '').strip().splitlines()
    try:
        payload = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        payload = {'mode': mode, 'status': 'FAIL', 'error': {'class': 'ProbeOutputError', 'code': 'INVALID_WORKER_OUTPUT'}, 'ready': False}
    payload['returncode'] = returncode
    import re
    payload['warnings_detected'] = bool(re.search(r'RuntimeWarning|coroutine .* was never awaited|Task was destroyed', stderr or '', re.I))
    payload['stderr'] = sanitize(stderr)
    return payload


def contract_result(run_id):
    return {
        'RunId': run_id,
        'PythonVersion': platform.python_version(),
        'Platform': platform.platform(),
        'LarkChannelSdkVersion': package_version('lark-channel-sdk'),
        'LarkOapiVersion': package_version('lark-oapi'),
        'WebsocketsVersion': package_version('websockets'),
        'OfficialPreimport': 'NOT_RUN',
        'LazyImportRepro': 'NOT_RUN',
        'OfficialPreimportWithCompat': 'NOT_RUN',
        'CfrTransport': 'NOT_RUN',
        'CfrTransportAfterFix': 'NOT_RUN',
        'OfficialErrorClass': None,
        'LazyErrorClass': None,
        'CfrErrorClass': None,
        'BotIdentityResolved': False,
        'ChannelReady': False,
        'WarningsDetected': False,
        'Diagnosis': 'NOT_RUN',
        'CompatibilityMode': 'NOT_RUN',
        'Verdict': 'NOT_RUN',
        'BlockingIssues': ['NATIVE_PROBE_NOT_RUN'],
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('official-preimport', 'lazy-import-repro', 'cfr-transport', 'official-preimport-with-compat'))
    parser.add_argument('--worker-mode', dest='worker_mode', choices=('official-preimport', 'lazy-import-repro', 'cfr-transport', 'official-preimport-with-compat'))
    parser.add_argument('--contract-only', action='store_true')
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args(argv)
    if args.worker_mode:
        return worker(args.worker_mode, args.timeout)

    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = ROOT / '.tmp' / 'm2-feishu-channel-loop' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    result = contract_result(run_id)
    logs = {}
    if not args.contract_only:
        modes = ('official-preimport', 'lazy-import-repro', 'cfr-transport')
        for mode in modes:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), '--worker-mode', mode, '--timeout', str(args.timeout)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=max(10, args.timeout + 10),
            )
            payload = parse_worker(completed.stdout, completed.stderr, mode, completed.returncode)
            logs[mode] = payload
            key = {'official-preimport': 'OfficialPreimport', 'lazy-import-repro': 'LazyImportRepro', 'cfr-transport': 'CfrTransport'}[mode]
            result[key] = payload.get('status', 'FAIL')
            result[{'official-preimport': 'OfficialErrorClass', 'lazy-import-repro': 'LazyErrorClass', 'cfr-transport': 'CfrErrorClass'}[mode]] = (payload.get('error') or {}).get('code')
            result['WarningsDetected'] = result['WarningsDetected'] or payload.get('warnings_detected', False)
            result['BotIdentityResolved'] = result['BotIdentityResolved'] or payload.get('bot_identity_resolved', False)
            result['ChannelReady'] = result['ChannelReady'] or payload.get('ready', False)
            if mode == 'cfr-transport':
                result['CompatibilityMode'] = payload.get('compatibility_mode') or result['CompatibilityMode']
        if result['OfficialPreimport'] not in {'PASS'} and result['OfficialErrorClass'] != 'FEISHU_LIVE_SETUP_REQUIRED':
            mode = 'official-preimport-with-compat'
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), '--worker-mode', mode, '--timeout', str(args.timeout)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=max(10, args.timeout + 10),
            )
            payload = parse_worker(completed.stdout, completed.stderr, mode, completed.returncode)
            logs[mode] = payload
            result['OfficialPreimportWithCompat'] = payload.get('status', 'FAIL')
            result['CompatibilityMode'] = payload.get('compatibility_mode') or result['CompatibilityMode']
        if result['CfrTransport'] == 'PASS':
            result['CfrTransportAfterFix'] = 'PASS'
        else:
            result['CfrTransportAfterFix'] = 'FAIL'
        error_codes = {result['OfficialErrorClass'], result['LazyErrorClass'], result['CfrErrorClass']}
        if 'FEISHU_LIVE_SETUP_REQUIRED' in error_codes:
            result['Diagnosis'] = 'FEISHU_LIVE_SETUP_REQUIRED'
        elif result['OfficialPreimport'] == 'PASS' and result['CfrTransport'] != 'PASS':
            result['Diagnosis'] = 'OFFICIAL_PREIMPORT_PASS_CFR_FAIL'
        elif result['OfficialPreimport'] == 'EVENT_LOOP_CONFLICT_REPRODUCED':
            result['Diagnosis'] = 'UPSTREAM_NATIVE_EVENT_LOOP_FAILURE'
        elif result['CfrTransport'] == 'PASS':
            result['Diagnosis'] = 'CFR_EVENT_LOOP_OWNERSHIP_FIXED'
        elif all(value == 'FAIL' for value in (result['OfficialPreimport'], result['LazyImportRepro'], result['CfrTransport'])):
            result['Diagnosis'] = 'UNKNOWN'
        else:
            result['Diagnosis'] = 'UNKNOWN'
        if result['CfrTransportAfterFix'] == 'PASS':
            result['Verdict'] = 'PASS'
            result['BlockingIssues'] = []
        elif result['Diagnosis'] == 'FEISHU_LIVE_SETUP_REQUIRED':
            result['Verdict'] = 'FAIL'
            result['BlockingIssues'] = ['FEISHU_LIVE_SETUP_REQUIRED']
        else:
            result['Verdict'] = 'FAIL'
            result['BlockingIssues'] = ['FEISHU_CHANNEL_NATIVE_PROBE_REQUIRED']
    (artifact_dir / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    for mode, payload in logs.items():
        (artifact_dir / f'{mode.replace("-", "_")}.log').write_text(sanitize(json.dumps(payload, indent=2)), encoding='utf-8')
    (artifact_dir / 'probe.log').write_text(sanitize(json.dumps(result, indent=2)), encoding='utf-8')
    print(json.dumps({'ArtifactDir': str(artifact_dir), **result}))
    if args.contract_only:
        return 0
    return 0 if result['Verdict'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
