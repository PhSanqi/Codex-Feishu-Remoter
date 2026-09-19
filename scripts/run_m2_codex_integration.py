"""Host-only M2 gate: real CodexAdapter plus deterministic Fake Feishu."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from cfr.codex.binding import CodexAdapter
from cfr.feishu.config import FeishuSettings
from cfr.feishu.daemon import FeishuDaemon
from cfr.feishu.gateway import FeishuGateway
from cfr.feishu.replies import deterministic_reply_uuid
from cfr.feishu.transport import FakeFeishuTransport
from cfr.storage.db import BindingStore


def event(message_id, text, chat_id='oc-m2-integration', sender='ou-host'):
    return {'event': {'sender': {'sender_id': {'open_id': sender}, 'sender_type': 'user'}, 'message': {'message_id': message_id, 'chat_id': chat_id, 'chat_type': 'p2p', 'message_type': 'text', 'content': json.dumps({'text': text})}}}


class CountingCodexAdapter:
    def __init__(self, inner):
        self.inner = inner
        self.registry = inner.registry
        self.create_calls = []
        self.send_calls = []
        self.stop_calls = []

    @property
    def last_resume(self):
        return self.inner.last_resume

    async def create_conversation(self, cwd, name, initial_message, *, on_server_request=None):
        self.create_calls.append((Path(cwd), initial_message))
        return await self.inner.create_conversation(cwd, name, initial_message, on_server_request=on_server_request)

    async def send_message(self, thread_id, message, *, on_server_request=None):
        self.send_calls.append((thread_id, message))
        return await self.inner.send_message(thread_id, message, on_server_request=on_server_request)

    async def stop(self, thread_id):
        self.stop_calls.append(thread_id)
        return await self.inner.stop(thread_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--gate-origin', choices=('desktop_agent', 'host_manual', 'unknown'), default='unknown')
    args = parser.parse_args()
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = ROOT / '.tmp' / 'm2-codex-integration' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    result = {
        'RunId': run_id,
        'GateOrigin': args.gate_origin,
        'RuntimeExecutionContext': 'HOST' if args.gate_origin == 'host_manual' else 'CODEX_AGENT_SANDBOX' if args.gate_origin == 'desktop_agent' else 'UNKNOWN',
        'Verdict': 'HOST_REQUIRED' if args.gate_origin != 'host_manual' else 'FAIL',
        'RealCodexFakeFeishuNewPending': 'NOT_RUN_HOST_REQUIRED' if args.gate_origin != 'host_manual' else 'NOT_RUN',
        'RealCodexFakeFeishuCreate': 'NOT_RUN_HOST_REQUIRED' if args.gate_origin != 'host_manual' else 'NOT_RUN',
        'RealCodexFakeFeishuResume': 'NOT_RUN_HOST_REQUIRED' if args.gate_origin != 'host_manual' else 'NOT_RUN',
        'RealCodexFakeFeishuRestartRecovery': 'NOT_RUN_HOST_REQUIRED' if args.gate_origin != 'host_manual' else 'NOT_RUN',
        'RealCodexFakeFeishuDedupe': 'NOT_RUN_HOST_REQUIRED' if args.gate_origin != 'host_manual' else 'NOT_RUN',
        'RealCodexFakeFeishuStatus': 'NOT_RUN_HOST_REQUIRED' if args.gate_origin != 'host_manual' else 'NOT_RUN',
        'RealCodexFakeFeishuUnbound': 'NOT_RUN_HOST_REQUIRED' if args.gate_origin != 'host_manual' else 'NOT_RUN',
        'RealCodexFakeFeishuBoundary': 'NOT_RUN_HOST_REQUIRED' if args.gate_origin != 'host_manual' else 'NOT_RUN',
        'M1Regression': 'HOST_RUN_REQUIRED',
    }
    events = []

    def check(condition):
        return 'PASS' if condition else 'FAIL'

    if args.gate_origin == 'host_manual':
        daemon = None
        try:
            with tempfile.TemporaryDirectory(prefix='cfr-m2-codex-') as directory:
                base = Path(directory)
                allowed = base / 'allowed'
                workspace = allowed / 'workspace'
                outside = base / 'outside'
                workspace.mkdir(parents=True)
                outside.mkdir()
                database = base / 'cfr.sqlite3'
                settings = FeishuSettings('cli-m2-host', 'secret-not-recorded', ('ou-host',), (allowed,), database=database)

                transport = FakeFeishuTransport()
                binding_store = BindingStore(database)
                adapter = CountingCodexAdapter(CodexAdapter(store=binding_store, timeout=args.timeout))
                daemon = FeishuDaemon(settings, transport, adapter=adapter, binding_store=binding_store)
                gateway = FeishuGateway(settings, daemon.store, daemon)
                daemon.start(background_workers=False)

                def deliver(message_id, text, chat_id='oc-m2-integration'):
                    received = gateway.handle_message_event(event(message_id, text, chat_id=chat_id))
                    processed = daemon.process_pending()
                    events.append({'message_id': message_id, 'text': text, 'received': received, 'processed': bool(processed)})
                    return received

                initial = deliver('m-new', f'/cfr new {workspace}')
                session = daemon.store.get_session('oc-m2-integration')
                accepted = any(item.uuid == deterministic_reply_uuid('m-new', 'accepted', 0) for item in transport.messages)
                result['RealCodexFakeFeishuNewPending'] = check(initial['status'] == 'queued' and session.state == 'pending_initial' and not adapter.create_calls and accepted)

                first_prompt = deliver('m-create', 'CFR_M2_CODEX_A: Reply exactly CFR_M2_CODEX_A_ACK.')
                session = daemon.store.get_session('oc-m2-integration')
                binding = binding_store.get_binding(session.thread_id)
                first_final = next((item for item in transport.messages if item.uuid == deterministic_reply_uuid('m-create', 'final', 0)), None)
                result['RealCodexFakeFeishuCreate'] = check(
                    first_prompt['status'] == 'queued' and len(adapter.create_calls) == 1 and session.state == 'bound'
                    and session.thread_id and binding and binding.rollout_path and binding.rollout_path.exists()
                    and first_final and first_final.content == 'CFR_M2_CODEX_A_ACK'
                )

                second_prompt = deliver('m-send', 'CFR_M2_CODEX_B: Reply exactly CFR_M2_CODEX_B_ACK.')
                resumed = adapter.last_resume.get(session.thread_id)
                second_final = next((item for item in transport.messages if item.uuid == deterministic_reply_uuid('m-send', 'final', 0)), None)
                result['RealCodexFakeFeishuResume'] = check(
                    second_prompt['status'] == 'queued' and adapter.send_calls == [(session.thread_id, 'CFR_M2_CODEX_B: Reply exactly CFR_M2_CODEX_B_ACK.')]
                    and resumed and resumed.thread_id == session.thread_id and second_final and second_final.content == 'CFR_M2_CODEX_B_ACK'
                )

                daemon.stop()
                daemon = None
                transport = FakeFeishuTransport()
                restarted_store = BindingStore(database)
                restarted_adapter = CountingCodexAdapter(CodexAdapter(store=restarted_store, timeout=args.timeout))
                restarted = FeishuDaemon(settings, transport, adapter=restarted_adapter, binding_store=restarted_store)
                restarted_gateway = FeishuGateway(settings, restarted.store, restarted)
                restarted.start(background_workers=False)
                try:
                    restart_received = restarted_gateway.handle_message_event(event('m-restart', 'CFR_M2_CODEX_RESTART: Reply exactly CFR_M2_CODEX_RESTART_ACK.'))
                    restarted.process_pending()
                    restarted_session = restarted.store.get_session('oc-m2-integration')
                    restart_final = next((item for item in transport.messages if item.uuid == deterministic_reply_uuid('m-restart', 'final', 0)), None)
                    result['RealCodexFakeFeishuRestartRecovery'] = check(
                        restart_received['status'] == 'queued' and restarted_session.thread_id == session.thread_id
                        and len(restarted_adapter.create_calls) == 0 and restarted_adapter.send_calls == [(session.thread_id, 'CFR_M2_CODEX_RESTART: Reply exactly CFR_M2_CODEX_RESTART_ACK.')]
                        and restarted_adapter.inner.last_resume.get(session.thread_id).thread_id == session.thread_id
                        and restart_final and restart_final.content == 'CFR_M2_CODEX_RESTART_ACK'
                    )

                    before = len(restarted_adapter.send_calls)
                    first = restarted_gateway.handle_message_event(event('m-dup', 'CFR_M2_CODEX_DUP: Reply exactly CFR_M2_CODEX_DUP_ACK.'))
                    second = restarted_gateway.handle_message_event(event('m-dup', 'CFR_M2_CODEX_DUP: Reply exactly CFR_M2_CODEX_DUP_ACK.'))
                    restarted.process_pending()
                    dup_final = [item for item in transport.messages if item.uuid == deterministic_reply_uuid('m-dup', 'final', 0)]
                    dup_accepted = [item for item in transport.messages if item.uuid == deterministic_reply_uuid('m-dup', 'accepted', 0)]
                    result['RealCodexFakeFeishuDedupe'] = check(first['status'] == 'queued' and second['status'] == 'deduped' and len(restarted_adapter.send_calls) == before + 1 and len(dup_final) == 1 and len(dup_accepted) == 1)

                    before_model = (len(restarted_adapter.create_calls), len(restarted_adapter.send_calls))
                    status = restarted_gateway.handle_message_event(event('m-status', '/cfr status'))
                    restarted.process_pending()
                    result['RealCodexFakeFeishuStatus'] = check(status['status'] == 'queued' and before_model == (len(restarted_adapter.create_calls), len(restarted_adapter.send_calls)))

                    unbound = restarted_gateway.handle_message_event(event('m-unbound', 'ordinary text', chat_id='oc-unbound'))
                    restarted.process_pending()
                    result['RealCodexFakeFeishuUnbound'] = check(unbound['status'] == 'queued' and not restarted_adapter.create_calls and not any(item.uuid == deterministic_reply_uuid('m-unbound', 'final', 0) for item in transport.messages))

                    boundary = restarted_gateway.handle_message_event(event('m-outside', f'/cfr new {outside}', chat_id='oc-outside'))
                    restarted.process_pending()
                    outside_row = restarted.store.get_inbox('m-outside')
                    result['RealCodexFakeFeishuBoundary'] = check(boundary['status'] == 'queued' and outside_row.error_code == 'FEISHU_WORKSPACE_NOT_ALLOWED' and not restarted_adapter.create_calls)
                finally:
                    restarted.stop()
        except Exception as exc:
            result['ErrorClass'] = type(exc).__name__
            result['SafeError'] = str(exc)[:500]
            result['Verdict'] = 'FAIL'
        else:
            keys = ('RealCodexFakeFeishuNewPending', 'RealCodexFakeFeishuCreate', 'RealCodexFakeFeishuResume', 'RealCodexFakeFeishuRestartRecovery', 'RealCodexFakeFeishuDedupe', 'RealCodexFakeFeishuStatus', 'RealCodexFakeFeishuUnbound', 'RealCodexFakeFeishuBoundary')
            result['Verdict'] = 'PASS' if all(result[key] == 'PASS' for key in keys) else 'FAIL'
        finally:
            if daemon is not None:
                daemon.stop()

    (artifact_dir / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    (artifact_dir / 'events.jsonl').write_text('\n'.join(json.dumps(item) for item in events), encoding='utf-8')
    (artifact_dir / 'integration.log').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'ArtifactDir': str(artifact_dir), **result}))
    return 0 if result['Verdict'] in {'PASS', 'HOST_REQUIRED'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
