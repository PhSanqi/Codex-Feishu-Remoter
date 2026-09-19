"""Read-only Feishu SDK metadata probe; never contacts Feishu or records secrets."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.metadata
import importlib
import json
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[1]


def version(distribution):
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def inspect_channel_sdk():
    result = {
        'FeishuChannelImport': False,
        'SendResultImport': False,
        'EventsMessage': False,
        'EventsCardAction': False,
        'MissingRequiredApi': [],
    }
    try:
        module = importlib.import_module('lark_channel')
        channel = getattr(module, 'FeishuChannel', None)
        events = getattr(module, 'Events', None)
        send_result = getattr(module, 'SendResult', None)
        result['FeishuChannelImport'] = channel is not None
        result['SendResultImport'] = send_result is not None
        result['EventsMessage'] = events is not None and hasattr(events, 'MESSAGE')
        result['EventsCardAction'] = events is not None and hasattr(events, 'CARD_ACTION')
    except Exception as exc:
        result['ImportError'] = type(exc).__name__
    required = {
        'FeishuChannelImport': result['FeishuChannelImport'],
        'SendResultImport': result['SendResultImport'],
        'EventsMessage': result['EventsMessage'],
        'EventsCardAction': result['EventsCardAction'],
    }
    result['MissingRequiredApi'] = [name for name, present in required.items() if not present]
    return result


def build_result(channel_version, oapi_version, surface):
    installed = channel_version is not None
    probe = 'PASS' if installed and not surface['MissingRequiredApi'] else 'FEISHU_SDK_NOT_INSTALLED' if not installed else 'MISSING_REQUIRED_API'
    blocking = [] if probe == 'PASS' else [
        'FEISHU_SDK_INSTALL_REQUIRED' if probe == 'FEISHU_SDK_NOT_INSTALLED' else 'FEISHU_SDK_REQUIRED_API_MISSING'
    ]
    return {
        'TransportBackend': 'lark-channel-sdk',
        'LarkChannelSdkInstalled': installed,
        'LarkChannelSdkVersion': channel_version,
        'LarkOapiInstalled': oapi_version is not None,
        'LarkOapiVersion': oapi_version,
        **{key: surface.get(key, False) for key in ('EventsMessage', 'EventsCardAction', 'FeishuChannelImport', 'SendResultImport')},
        'FeishuSdkProbe': probe,
        'Verdict': 'PASS' if probe == 'PASS' else 'FAIL',
        'BlockingIssues': blocking,
        'SecretsRecorded': False,
    }


def main():
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = ROOT / '.tmp' / 'm2-feishu-sdk' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    channel_version = version('lark-channel-sdk')
    oapi_version = version('lark-oapi')
    result = {'RunId': run_id, **build_result(channel_version, oapi_version, inspect_channel_sdk())}
    (artifact_dir / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'ArtifactDir': str(artifact_dir), **result}))
    return 0 if result['Verdict'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
