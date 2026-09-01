"""Verify persistent Feishu credentials from a fresh process.

The parent removes both Feishu environment variables before spawning the
child. The child uses the same resolver as every formal Feishu entrypoint.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cfr.feishu.config import load_settings
from cfr.feishu.connection import FeishuConnectionLease
from cfr.feishu.log_sanitize import sanitize_feishu_log_text
from cfr.feishu.transport import ChannelFeishuTransport


def contract(run_id):
    return {
        'RunId': run_id,
        'PersistentAppIdResolved': False,
        'PersistentAppSecretResolved': False,
        'EnvironmentRemoved': False,
        'FeishuConnectionReady': 'NOT_RUN',
        'SecretsRecorded': False,
        'Verdict': 'NOT_RUN',
        'BlockingIssues': ['CREDENTIAL_STORE_PROBE_NOT_RUN'],
    }


def child_main(run_id, artifact_dir, live, timeout):
    result = contract(run_id)
    result['EnvironmentRemoved'] = not os.environ.get('CFR_FEISHU_APP_ID') and not os.environ.get('CFR_FEISHU_APP_SECRET')
    try:
        settings = load_settings(database=ROOT / 'cfr.sqlite3')
        result['PersistentAppIdResolved'] = settings.app_id_source == 'persistent' and bool(settings.app_id)
        result['PersistentAppSecretResolved'] = settings.app_secret_source == 'persistent' and bool(settings.app_secret)
        if not result['EnvironmentRemoved']:
            result['BlockingIssues'] = ['FEISHU_ENVIRONMENT_NOT_REMOVED']
        elif not result['PersistentAppIdResolved'] or not result['PersistentAppSecretResolved']:
            result['BlockingIssues'] = ['PERSISTENT_FEISHU_CREDENTIALS_REQUIRED']
        elif live:
            lease = FeishuConnectionLease(settings.database, f'feishu:{settings.app_namespace}', ttl=30, owner_instance_id=f'credential-probe-{run_id}').acquire()
            transport = ChannelFeishuTransport(settings, connect_timeout=timeout, disconnect_timeout=5.0)
            try:
                transport.connect_until_ready(timeout=timeout)
                result['FeishuConnectionReady'] = 'PASS' if transport.connection_state == 'ready' else 'FAIL'
            finally:
                transport.stop()
                lease.release()
        else:
            result['FeishuConnectionReady'] = 'NOT_RUN'
        if not result['BlockingIssues'] or result['BlockingIssues'] == ['CREDENTIAL_STORE_PROBE_NOT_RUN']:
            result['BlockingIssues'] = []
    except Exception as exc:
        result['BlockingIssues'] = [getattr(exc, 'code', type(exc).__name__)]
    result['Verdict'] = 'PASS' if result['EnvironmentRemoved'] and result['PersistentAppIdResolved'] and result['PersistentAppSecretResolved'] and (not live or result['FeishuConnectionReady'] == 'PASS') and not result['SecretsRecorded'] and not result['BlockingIssues'] else 'FAIL'
    safe = sanitize_feishu_log_text(json.dumps(result, indent=2, ensure_ascii=False))
    (artifact_dir / 'child_result.json').write_text(safe, encoding='utf-8')
    print(json.dumps({'ChildResultPath': str(artifact_dir / 'child_result.json')}))
    return 0 if result['Verdict'] == 'PASS' else 2


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--contract-only', action='store_true')
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--run-id')
    parser.add_argument('--artifact-dir')
    args = parser.parse_args(argv)
    run_id = args.run_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = Path(args.artifact_dir) if args.artifact_dir else ROOT / '.tmp' / 'feishu-credential-store' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if args.contract_only:
        result = contract(run_id)
        (artifact_dir / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps({'ArtifactDir': str(artifact_dir), **result}))
        return 0
    if args.child:
        return child_main(run_id, artifact_dir, args.live, args.timeout)
    child_env = dict(os.environ)
    child_env.pop('CFR_FEISHU_APP_ID', None)
    child_env.pop('CFR_FEISHU_APP_SECRET', None)
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), '--child', '--run-id', run_id, '--artifact-dir', str(artifact_dir), '--timeout', str(args.timeout)] + (['--live'] if args.live else []),
        cwd=ROOT, env=child_env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=max(args.timeout * 2, args.timeout + 10),
    )
    try:
        result = json.loads((artifact_dir / 'child_result.json').read_text(encoding='utf-8'))
    except Exception:
        result = contract(run_id)
        result['BlockingIssues'] = ['CHILD_RESULT_MISSING']
    result['ChildExitCode'] = completed.returncode
    if completed.returncode != 0:
        result.setdefault('BlockingIssues', []).append(f'CHILD_EXIT_CODE:{completed.returncode}')
    result['Verdict'] = 'PASS' if completed.returncode == 0 and not result.get('BlockingIssues') else 'FAIL'
    (artifact_dir / 'probe.log').write_text(sanitize_feishu_log_text((completed.stdout or '') + '\n' + (completed.stderr or '')), encoding='utf-8')
    (artifact_dir / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'ArtifactDir': str(artifact_dir), **result}, ensure_ascii=False))
    return 0 if result['Verdict'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
