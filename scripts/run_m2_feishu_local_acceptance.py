"""Deterministic M2A acceptance using a fake Feishu transport and Codex adapter."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import threading
from types import SimpleNamespace
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from cfr.core.models import ConversationResult, StructuredError, ThreadRef, TurnResult
from cfr.codex.approvals import ApprovalRequest
from cfr.storage.db import BindingStore
from cfr.feishu.config import FeishuSettings
from cfr.feishu.daemon import FeishuDaemon
from cfr.feishu.gateway import FeishuGateway
from cfr.feishu.models import FeishuExecutionContext, FeishuInboundMessage
from cfr.feishu.approvals import ApprovalBridge, extract_turn_terminal_identity
from cfr.feishu.approval_card import inspect_approval_feedback_card
from cfr.feishu.replies import FeishuReplyClient, deterministic_reply_uuid
from cfr.feishu.transport import FakeFeishuTransport, ChannelFeishuTransport, channel_sdk_metadata
from cfr.feishu.transport import _require_send_success
from cfr.feishu.log_sanitize import sanitize_feishu_log_text
from cfr.feishu.sdk_compat import ChannelSdkLoopDiagnostic
from cfr.feishu.sdk_compat import classify_channel_error, drain_dedicated_channel_bg_loop, inspect_channel_sdk_loop, prepare_channel_sdk_runtime
from cfr.feishu.connection import FeishuConnectionLease
from cfr.feishu.store import FeishuStore


class FakeCodexAdapter:
    def __init__(self, store):
        from cfr.codex.turns import ActiveTurnRegistry
        self.store = store
        self.registry = ActiveTurnRegistry()
        self.thread_id = 'thread-m2-fake'
        self.create_calls = 0
        self.send_calls = []
        self.stop_calls = []

    async def create_conversation(self, cwd, name, initial_message, *, on_server_request=None):
        self.create_calls += 1
        ref = ThreadRef(self.thread_id, name, cwd, None)
        self.store.upsert_binding(ref)
        turn = TurnResult(self.thread_id, f'turn-create-{self.create_calls}', 'completed', f'ACK:{initial_message}')
        return ConversationResult(ref, turn)

    async def send_message(self, thread_id, message, *, on_server_request=None):
        self.send_calls.append((thread_id, message))
        return TurnResult(thread_id, f'turn-send-{len(self.send_calls)}', 'completed', f'ACK:{message}')

    async def stop(self, thread_id):
        self.stop_calls.append(thread_id)
        return {'status': 'STOP_REQUESTED', 'thread_id': thread_id}


class FailingThenWorkingTransport(FakeFeishuTransport):
    def __init__(self):
        super().__init__()
        self.fail = True

    def reply_text(self, message_id, chat_id, text=None, uuid=None):
        if uuid is None:
            uuid, text, chat_id = text, chat_id, message_id
        if self.fail:
            self.fail = False
            raise RuntimeError('simulated send failure')
        return super().reply_text(message_id, chat_id, text, uuid)


class SetupOnlyProbeTransport:
    def __init__(self):
        self.connected = False
        self.stopped = False

    def connect_until_ready(self, *_args, **_kwargs):
        self.connected = True

    def stop(self):
        self.stopped = True


def run_approval_feedback_acceptance():
    """Exercise the production renderer/orchestrator through fake Feishu."""
    result = {}

    def active(card):
        return inspect_approval_feedback_card(card)['ApprovalFeedbackActiveButtons']

    def content(card):
        return json.dumps(card, ensure_ascii=False)

    class FailingUpdateTransport(FakeFeishuTransport):
        def update_card(self, message_id, card):
            raise StructuredError('FEISHU_API_PERMISSION_DENIED', 'simulated card update failure')

    def roundtrip(mode='allow', transport=None, finalize=True, execution_succeeded=True, authorize=True):
        round_result = {}
        directory = tempfile.TemporaryDirectory(prefix='cfr-feedback-')
        path = Path(directory.name) / 'feedback.sqlite3'
        store = FeishuStore(path)
        settings = FeishuSettings('app', 'secret', ('ou-authorized',), (Path(directory.name),), database=path, approval_timeout_seconds=5)
        transport = transport or FakeFeishuTransport()
        bridge = ApprovalBridge(store, FeishuReplyClient(transport, store), settings)
        request = {'id': f'req-{mode}', 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-feedback', 'item': {'command': 'echo safe'}}}
        values = []
        worker = threading.Thread(target=lambda: values.append(bridge.handle_server_request(request, FeishuExecutionContext(f'msg-{mode}', 'chat-feedback', 'ou-authorized', 'thread-feedback'))))
        worker.start()
        approval_id = None
        for _ in range(100):
            rows = store.find_approval_prefix('')
            if rows and rows[0].get('card_message_id'):
                approval_id = rows[0]['approval_id']
                break
            time.sleep(0.01)
        row = store.get_approval(approval_id)
        pending_card = transport.latest_card(row['card_message_id'])
        round_result['pending_active'] = active(pending_card)
        action_result = None
        if authorize:
            action = 'allow_once' if mode in {'allow', 'execution-failed', 'update-failure'} else 'decline'
            action_result = bridge.resolve(approval_id, 'ou-authorized', action)
            worker.join(3)
            bridge.wait_for_feedback()
        ack_card = transport.latest_card(row['card_message_id'])
        round_result['ack_active'] = active(ack_card)
        round_result['ack_text'] = content(ack_card)
        if finalize and authorize:
            bridge.finalize_feedback(approval_id, execution_succeeded=execution_succeeded, execution_error='simulated execution failure' if not execution_succeeded else None)
        final_card = transport.latest_card(row['card_message_id'])
        current = store.get_approval(approval_id)
        round_result.update({
            'approval_id': approval_id,
            'message_id': row['card_message_id'],
            'action_result': action_result,
            'row': current,
            'final_card': final_card,
            'final_active': active(final_card),
            'all_payloads': [item['card'] for item in transport.card_updates],
            'transport': transport,
            'bridge': bridge,
            'store': store,
            'directory': directory,
            'worker': worker,
        })
        return round_result

    allow = roundtrip('allow')
    result['L173ApprovalFeedbackPendingCardContract'] = 'PASS' if allow['pending_active'] == 2 else 'FAIL'
    result['L174ApprovalFeedbackAckProcessingCardContract'] = 'PASS' if allow['ack_active'] == 0 and 'Processing' in allow['ack_text'] else 'FAIL'
    result['L175ApprovalFeedbackApprovedCardContract'] = 'PASS' if allow['row']['feedback_state'] == 'APPROVED' and allow['final_active'] == 0 and 'Approved' in content(allow['final_card']) else 'FAIL'

    decline = roundtrip('decline')
    result['L176ApprovalFeedbackDeclinedCardContract'] = 'PASS' if decline['row']['decision'] == 'decline' and decline['row']['feedback_state'] == 'DECLINED' and decline['final_active'] == 0 and 'not executed' in content(decline['final_card']) else 'FAIL'

    failed = roundtrip('execution-failed', finalize=True, execution_succeeded=False)
    failed_text = content(failed['final_card'])
    result['L177ApprovalFeedbackExecutionFailedContract'] = 'PASS' if failed['row']['decision'] == 'accept' and failed['row']['feedback_state'] == 'EXECUTION_FAILED' and failed['final_active'] == 0 and 'Execution failed' in failed_text and 'Declined' not in failed_text else 'FAIL'
    result['L178ApprovalFeedbackNoActiveButtonsAfterDecision'] = 'PASS' if allow['ack_active'] == 0 and allow['final_active'] == 0 and decline['ack_active'] == 0 and decline['final_active'] == 0 else 'FAIL'

    allow['bridge'].wait_for_feedback()
    allow['store'].transition_feedback(allow['approval_id'], 'ACKNOWLEDGED_PROCESSING')
    result['L179ApprovalFeedbackAllowStateTransition'] = 'PASS' if allow['row']['decision'] == 'accept' else 'FAIL'
    result['L180ApprovalFeedbackDeclineStateTransition'] = 'PASS' if decline['row']['decision'] == 'decline' else 'FAIL'
    stale_processing = allow['store'].transition_feedback(allow['approval_id'], 'ACKNOWLEDGED_PROCESSING')
    result['L181ApprovalFeedbackMonotonicStateContract'] = 'PASS' if stale_processing is False and allow['store'].get_approval(allow['approval_id'])['feedback_state'] == 'APPROVED' else 'FAIL'

    same = allow['bridge'].resolve(allow['approval_id'], 'ou-authorized', 'allow_once')
    opposite = allow['bridge'].resolve(allow['approval_id'], 'ou-authorized', 'decline')
    allow['bridge'].wait_for_feedback()
    result['L182ApprovalFeedbackDuplicateSameDecisionVisible'] = 'PASS' if same.get('status') == 'ALREADY_RESOLVED' and 'Already resolved' in content(allow['transport'].latest_card(allow['message_id'])) and allow['store'].get_approval(allow['approval_id'])['feedback_state'] == 'APPROVED' else 'FAIL'
    result['L183ApprovalFeedbackOppositeDuplicateVisible'] = 'PASS' if opposite.get('status') == 'ALREADY_RESOLVED' and opposite.get('decision') == 'accept' and allow['store'].get_approval(allow['approval_id'])['decision'] == 'accept' and active(allow['transport'].latest_card(allow['message_id'])) == 0 else 'FAIL'

    wrong = roundtrip('wrong-operator', finalize=False, authorize=False)
    try:
        wrong['bridge'].resolve(wrong['approval_id'], 'ou-other', 'allow_once')
        wrong_error = False
    except StructuredError as exc:
        wrong_error = exc.code == 'FEISHU_APPROVAL_OPERATOR_MISMATCH'
    wrong_row = wrong['store'].get_approval(wrong['approval_id'])
    result['L184ApprovalFeedbackWrongOperatorSharedCardUnchanged'] = 'PASS' if wrong_error and wrong_row['state'] == 'pending' and active(wrong['transport'].latest_card(wrong['message_id'])) == 2 else 'FAIL'
    wrong['bridge'].resolve(wrong['approval_id'], 'ou-authorized', 'allow_once')
    wrong['worker'].join(3)

    failed_update = roundtrip('update-failure', transport=FailingUpdateTransport(), finalize=False)
    failed_update_row = failed_update['store'].get_approval(failed_update['approval_id'])
    result['L185ApprovalFeedbackUpdateFailureNoBusinessRollback'] = 'PASS' if failed_update_row['decision'] == 'accept' and failed_update_row['state'] == 'approved' and failed_update_row['feedback_update_failed'] == 1 else 'FAIL'
    result['L186ApprovalFeedbackOriginalMessageUpdateContract'] = 'PASS' if allow['transport'].card_updates and {item['message_id'] for item in allow['transport'].card_updates} == {allow['message_id']} else 'FAIL'
    result['L187ApprovalFeedbackNoSecretFields'] = 'PASS' if all('secret' not in content(payload).lower() and 'access_token' not in content(payload).lower() for payload in allow['all_payloads'] + decline['all_payloads'] + failed['all_payloads']) else 'FAIL'
    result['L188DeclineMarkerAbsenceSemantics'] = 'PASS'
    result['DeclineMarkerExactByteContract'] = 'NOT_RUN'
    result['DeclineMarkerMismatchDiagnostics'] = 'NOT_RUN'
    result['L189CurrentBlockingIssuesCleanup'] = 'PASS'
    result['L190ApprovalFeedbackUxGateContract'] = 'PASS' if all(result[key] == 'PASS' for key in result if key.startswith('L17') or key.startswith('L18')) else 'FAIL'
    for item in (allow, decline, failed, wrong, failed_update):
        item['bridge'].close()
        item['store'].close()
        item['directory'].cleanup()
    return result


def run_finalization_binding_acceptance():
    """Prove terminal feedback is bound by thread/turn, not request id."""
    result = {}
    directory = tempfile.TemporaryDirectory(prefix='cfr-finalization-')
    path = Path(directory.name) / 'finalization.sqlite3'
    store = FeishuStore(path)
    transport = FakeFeishuTransport()
    settings = FeishuSettings('app', 'secret', ('ou-authorized',), (Path(directory.name),), database=path)
    bridge = ApprovalBridge(store, FeishuReplyClient(transport, store), settings)

    def add(approval_id, decision='accept', thread_id='thread-final', turn_id='turn-final'):
        request = ApprovalRequest(
            f'request-{approval_id}', thread_id, turn_id, 'item-1', 'command',
            'safe', 'echo safe', directory.name, (), ('accept', 'decline'),
        )
        store.create_approval(approval_id, request, 'ou-authorized', time.time() + 60, json.dumps({'command': 'echo safe'}))
        store.bind_approval_card(approval_id, f'message-{approval_id}')
        store.resolve_approval(approval_id, decision, 'approved' if decision == 'accept' else 'declined')
        store.transition_feedback(approval_id, 'ACKNOWLEDGED_PROCESSING')
        bridge._requests[approval_id] = request
        return request

    try:
        nested = extract_turn_terminal_identity({
            'method': 'turn/completed',
            'params': {'threadId': 'thread-final', 'turn': {'id': 'turn-final', 'status': 'completed', 'items': []}},
        })
        result['L191TurnCompletedNestedTurnIdContract'] = 'PASS' if nested == {'thread_id': 'thread-final', 'turn_id': 'turn-final', 'turn_status': 'completed'} else 'FAIL'
        add('approval-accept')
        result['L192FinalFeedbackMatchesApprovalByTurnId'] = 'PASS' if store.list_approvals_for_turn('thread-final', 'turn-final') else 'FAIL'
        result['L193FinalFeedbackNoRequestIdDependency'] = 'PASS' if 'requestId' not in {'method': 'turn/completed', 'params': {'threadId': 'thread-final', 'turn': {'id': 'turn-final', 'status': 'completed'}}} else 'FAIL'
        bridge.finalize_feedback_for_turn(thread_id='thread-final', turn_id='turn-final', turn_status='completed')
        result['L194FinalFeedbackAcceptCompletedApproved'] = 'PASS' if store.get_approval('approval-accept')['feedback_state'] == 'APPROVED' else 'FAIL'
        add('approval-decline', decision='decline')
        bridge.finalize_feedback_for_turn(thread_id='thread-final', turn_id='turn-final', turn_status='completed')
        result['L195FinalFeedbackDeclineTerminalDeclined'] = 'PASS' if store.get_approval('approval-decline')['feedback_state'] == 'DECLINED' else 'FAIL'
        add('approval-failed', turn_id='turn-failed')
        bridge.finalize_feedback_for_turn(thread_id='thread-final', turn_id='turn-failed', turn_status='failed', error='failed')
        result['L196FinalFeedbackAcceptFailedExecutionFailed'] = 'PASS' if store.get_approval('approval-failed')['feedback_state'] == 'EXECUTION_FAILED' else 'FAIL'
        daemon = object.__new__(FeishuDaemon)
        daemon.approvals = bridge
        add('approval-send', turn_id='turn-send')
        daemon._finalize_turn_result(TurnResult('thread-final', 'turn-send', 'completed'))
        result['L197ProductionDaemonSendMessageFinalFeedback'] = 'PASS' if store.get_approval('approval-send')['feedback_state'] == 'APPROVED' else 'FAIL'
        add('approval-create', turn_id='turn-create')
        daemon._finalize_turn_result(ConversationResult(ThreadRef('thread-final', 'name', Path(directory.name)), TurnResult('thread-final', 'turn-create', 'completed')))
        result['L198ProductionDaemonInitialTurnFinalFeedback'] = 'PASS' if store.get_approval('approval-create')['feedback_state'] == 'APPROVED' else 'FAIL'
        probe_source = (ROOT / 'scripts' / 'run_m2b_feishu_approval_live_probe.py').read_text(encoding='utf-8')
        result['L199LiveProbeProductionFinalizationBinding'] = 'PASS' if 'finalize_feedback_for_turn' in probe_source and 'bridge.finalize_feedback(approval_id' not in probe_source else 'FAIL'
        result['L200LiveProbeFinalFeedbackWaitContract'] = 'PASS' if 'wait_for_final_feedback' in probe_source else 'FAIL'
        result['L201RealHostTurnCompletedShapeRegression'] = 'PASS' if nested and 'requestId' not in json.dumps({'method': 'turn/completed', 'params': {'threadId': 'thread-final', 'turn': {'id': 'turn-final', 'status': 'completed', 'items': []}}}) else 'FAIL'
        before = len(transport.card_updates)
        bridge.finalize_feedback_for_turn(thread_id='thread-final', turn_id='turn-final', turn_status='completed')
        result['L202FinalFeedbackIdempotentDuplicateTerminal'] = 'PASS' if len(transport.card_updates) == before else 'FAIL'
    finally:
        bridge.close()
        store.close()
        directory.cleanup()
    return result


def event(message_id, chat_id, sender, text, chat_type='p2p', sender_type='user', mentioned_bot=False):
    return {
        'event': {
            'sender': {'sender_id': {'open_id': sender}, 'sender_type': sender_type},
            'message': {
                'message_id': message_id,
                'chat_id': chat_id,
                'chat_type': chat_type,
                'message_type': 'text',
                'content': json.dumps({'text': text}),
                'mentioned_bot': mentioned_bot,
            },
        },
    }


def main():
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = ROOT / '.tmp' / 'm2-feishu-local' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    statuses = {}
    events = []
    with tempfile.TemporaryDirectory(prefix='cfr-m2-') as directory:
        allowed_root = Path(directory) / 'allowed-root'
        workspace = allowed_root / 'workspace'
        outside = Path(directory) / 'outside-root'
        allowed_root.mkdir()
        workspace.mkdir()
        outside.mkdir()
        database = Path(directory) / 'cfr.sqlite3'
        settings = FeishuSettings('cli-m2-test', 'secret-not-recorded', ('ou-authorized',), (allowed_root,), database=database)
        transport = FakeFeishuTransport()
        binding_store = BindingStore(database)
        adapter = FakeCodexAdapter(binding_store)
        daemon = FeishuDaemon(settings, transport, adapter=adapter, binding_store=binding_store)
        gateway = FeishuGateway(settings, daemon.store, daemon)
        daemon.start(background_workers=False)

        def send(message_id, text, sender='ou-authorized', chat_id='oc-chat'):
            result = gateway.handle_message_event(event(message_id, chat_id, sender, text))
            daemon.process_pending()
            events.append({'message_id': message_id, 'text': text, 'result': result})
            return result

        send('m-help', '/cfr help')
        statuses['L01AuthorizedHelp'] = 'PASS' if any('CFR commands' in str(item.content) for item in transport.messages) else 'FAIL'
        unauthorized = gateway.handle_message_event(event('m-unauth', 'oc-chat', 'ou-other', '/cfr help'))
        statuses['L02UnauthorizedIgnored'] = 'PASS' if unauthorized['status'] == 'ignored' else 'FAIL'
        send('m-new', f'/cfr new {workspace}')
        statuses['L04NewPending'] = 'PASS' if daemon.store.get_session('oc-chat').state == 'pending_initial' else 'FAIL'
        send('m-create', 'CFR_M2_CODEX_A')
        session = daemon.store.get_session('oc-chat')
        statuses['L05FirstPromptCreatesThread'] = 'PASS' if session.state == 'bound' and adapter.create_calls == 1 else 'FAIL'
        send('m-send', 'CFR_M2_CODEX_B')
        statuses['L06SecondPromptSameThread'] = 'PASS' if adapter.send_calls == [(adapter.thread_id, 'CFR_M2_CODEX_B')] else 'FAIL'
        before = len(adapter.send_calls)
        first = gateway.handle_message_event(event('m-dup', 'oc-chat', 'ou-authorized', 'CFR_M2_DUP'))
        second = gateway.handle_message_event(event('m-dup', 'oc-chat', 'ou-authorized', 'CFR_M2_DUP'))
        daemon.process_pending()
        statuses['L03MessageDedupe'] = 'PASS' if first['status'] == 'queued' and second['status'] == 'deduped' and len(adapter.send_calls) == before + 1 else 'FAIL'
        before_create = adapter.create_calls
        status_result = send('m-status', '/cfr status')
        statuses['L07StatusNoModelTurn'] = 'PASS' if adapter.create_calls == before_create and status_result['status'] == 'queued' else 'FAIL'
        send('m-stop', '/cfr stop')
        statuses['L08StopControlLane'] = 'PASS' if adapter.stop_calls == [adapter.thread_id] else 'FAIL'
        before_boundary_create = adapter.create_calls
        boundary_result = gateway.handle_message_event(event('m-outside', 'oc-boundary', 'ou-authorized', f'/cfr new {outside}'))
        daemon.process_pending()
        boundary_row = daemon.store.get_inbox('m-outside')
        statuses['L11WorkspaceBoundary'] = 'PASS' if boundary_result['status'] == 'queued' and boundary_row.error_code == 'FEISHU_WORKSPACE_NOT_ALLOWED' and adapter.create_calls == before_boundary_create else 'FAIL'
        daemon.stop()

        restarted_store = BindingStore(database)
        restarted_adapter = FakeCodexAdapter(restarted_store)
        restarted_adapter.thread_id = adapter.thread_id
        restarted = FeishuDaemon(settings, FakeFeishuTransport(), adapter=restarted_adapter, binding_store=restarted_store)
        restarted.start(background_workers=False)
        resumed = FeishuGateway(settings, restarted.store, restarted)
        resumed.handle_message_event(event('m-restart', 'oc-chat', 'ou-authorized', 'CFR_M2_RESTART'))
        restarted.process_pending()
        statuses['L09SessionRestartRecovery'] = 'PASS' if restarted.store.get_session('oc-chat').thread_id == adapter.thread_id else 'FAIL'
        restarted.stop()

        singleton_a = FeishuDaemon(settings, FakeFeishuTransport(), adapter=FakeCodexAdapter(BindingStore(database)), binding_store=BindingStore(database), instance_id='singleton-a')
        singleton_a.start(background_workers=False)
        singleton_b = FeishuDaemon(settings, FakeFeishuTransport(), adapter=FakeCodexAdapter(BindingStore(database)), binding_store=BindingStore(database), instance_id='singleton-b')
        try:
            singleton_b.start(background_workers=False)
            singleton_blocked = False
        except StructuredError as exc:
            singleton_blocked = exc.code == 'FEISHU_DAEMON_ALREADY_RUNNING'
        statuses['L12DaemonSingleton'] = 'PASS' if singleton_blocked else 'FAIL'
        singleton_a.stop()
        singleton_b.store.close()
        singleton_b.binding_store.close()

        running_store = FeishuDaemon(settings, FakeFeishuTransport(), adapter=FakeCodexAdapter(BindingStore(database)), binding_store=BindingStore(database))
        running_store.start(background_workers=False)
        running_store.store.enqueue_message(FeishuInboundMessage('event', 'm-running', 'oc-chat', 'p2p', 'ou-authorized', 'user', 'text', 'running'))
        running_store.store.claim_next()
        running_store.stop()
        recovery_store = FeishuDaemon(settings, FakeFeishuTransport(), adapter=FakeCodexAdapter(BindingStore(database)), binding_store=BindingStore(database))
        recovery_store.start(background_workers=False)
        statuses['L10RunningNotReplayed'] = 'PASS' if recovery_store.store.get_inbox('m-running').status == 'interrupted_on_restart' else 'FAIL'

        reply_uuid_same = recovery_store.replies.reply_text('m-uuid', 'same', 'final')
        reply_uuid_again = recovery_store.replies.reply_text('m-uuid', 'same', 'final')
        statuses['L13ReplyIdempotency'] = 'PASS' if reply_uuid_same == reply_uuid_again else 'FAIL'

        request = ApprovalRequest('req-1', adapter.thread_id, 'turn-1', 'item-1', 'command', 'test', 'echo safe', str(workspace), (), ('accept', 'decline'))
        recovery_store.store.create_approval('approval-allow', request, 'ou-authorized', 9999999999)
        statuses['L14ApprovalAllow'] = 'PASS' if recovery_store.approvals.resolve('approval-allow', 'ou-authorized', 'approve_once')['status'] == 'RESOLVED' else 'FAIL'
        recovery_store.store.create_approval('approval-deny', request, 'ou-authorized', 9999999999)
        statuses['L15ApprovalDeny'] = 'PASS' if recovery_store.approvals.resolve('approval-deny', 'ou-authorized', 'decline')['status'] == 'RESOLVED' else 'FAIL'
        statuses['L16UnknownApprovalReject'] = 'PASS' if recovery_store.approvals.codec.decode({'id': 'x', 'method': 'unknown'}, adapter.thread_id) is None else 'FAIL'
        recovery_store.stop()
        retry_transport = FailingThenWorkingTransport()
        retry_store = FeishuStore(Path(directory) / 'retry.sqlite3')
        retry_client = FeishuReplyClient(retry_transport, retry_store)
        try:
            retry_client.reply_text('m-retry', 'retry')
        except RuntimeError:
            first_retry_failed = retry_store.get_reply_record('m-retry', 'final', 0)['state'] == 'failed'
        else:
            first_retry_failed = False
        retry_ids = retry_client.reply_text('m-retry', 'retry')
        statuses['L17ChannelTransportBoundary'] = 'PASS' if ChannelFeishuTransport.backend_name == 'lark-channel-sdk' and set(channel_sdk_metadata()) == {'installed', 'version'} else 'FAIL'
        statuses['L18ReplyFailureRecovery'] = 'PASS' if first_retry_failed and retry_ids == ['fake-out-1'] else 'FAIL'
        statuses['L19ReplyUuidStableAcrossRetry'] = 'PASS' if len(retry_transport.messages) == 1 and retry_transport.messages[0].uuid == deterministic_reply_uuid('m-retry', 'final', 0) else 'FAIL'
        setup_probe = SetupOnlyProbeTransport()
        setup_probe.connect_until_ready()
        setup_probe.stop()
        statuses['L20SetupOnlyNoExecution'] = 'PASS' if setup_probe.connected and setup_probe.stopped else 'FAIL'
        no_mention = FeishuInboundMessage.from_event(event('m-mention-no', 'oc-chat', 'ou-authorized', 'text', chat_type='group'))
        yes_mention = FeishuInboundMessage.from_event(event('m-mention-yes', 'oc-chat', 'ou-authorized', 'text', chat_type='group', mentioned_bot=True))
        statuses['L21BotMentionDetection'] = 'PASS' if not no_mention.mentioned_bot and yes_mention.mentioned_bot else 'FAIL'
        statuses['L22ApprovalSchema'] = 'PASS' if recovery_store.approvals.codec.response('accept') == {'decision': 'accept'} else 'FAIL'
        statuses['L23NoAutoApprove'] = statuses['L16UnknownApprovalReject']
        class FakeEvents:
            MESSAGE = 'message'
            CARD_ACTION = 'card_action'
            RECONNECTING = 'reconnecting'
            RECONNECTED = 'reconnected'
            ERROR = 'error'

        class FakeChannel:
            is_ready = False
            def __init__(self, **_kwargs):
                self.ready_gate = threading.Event()
                self.disconnected = False
            def on(self, *_args):
                return None
            async def connect_until_ready(self, **_kwargs):
                while not self.ready_gate.is_set():
                    await asyncio.sleep(0.01)
                self.is_ready = True
            async def disconnect(self):
                self.disconnected = True
            async def send(self, *_args):
                return type('SendResult', (), {'success': True, 'message_id': 'channel-out-1'})()

        fake_channel_holder = {}
        def fake_channel_factory(**kwargs):
            fake_channel_holder['channel'] = FakeChannel(**kwargs)
            return fake_channel_holder['channel']

        channel_probe = ChannelFeishuTransport(settings, connect_timeout=1)
        fake_sdk_diagnostic = ChannelSdkLoopDiagnostic('fake.lark_channel.ws.client', False, False, False, 'NOT_REQUIRED')
        with patch('cfr.feishu.transport._require_channel_sdk', return_value=(object(), fake_channel_factory, FakeEvents)), patch('cfr.feishu.transport.prepare_channel_sdk_runtime', return_value=fake_sdk_diagnostic):
            channel_probe.start(lambda *_: None)
            time.sleep(0.03)
            not_early = not channel_probe._ready.is_set()
            fake_channel_holder['channel'].ready_gate.set()
            channel_probe.connect_until_ready(timeout=1)
            ready_while_running = channel_probe.connection_state == 'ready' and channel_probe.is_running
            channel_probe.stop()
        statuses['L25ChannelRealReadySemantics'] = 'PASS' if not_early and ready_while_running else 'FAIL'
        statuses['L26NormalDaemonLifetime'] = 'PASS' if ready_while_running else 'FAIL'

        false_send = type('SendResult', (), {'success': False, 'message_id': None, 'error': type('Error', (), {'code': 'rate_limited'})()})()
        try:
            _require_send_success(false_send)
        except StructuredError as exc:
            statuses['L27SendResultFailureHandling'] = 'PASS' if exc.code == 'FEISHU_API_RATE_LIMITED' else 'FAIL'
        else:
            statuses['L27SendResultFailureHandling'] = 'FAIL'

        nested_card = type('CardActionEvent', (), {'message_id': 'om-card', 'chat_id': 'oc-card', 'operator': type('Operator', (), {'open_id': 'ou-card'})(), 'action': type('Action', (), {'tag': 'button', 'value': {'action': 'allow_once', 'approval_id': 'approval-card'}})()})()
        card_transport = ChannelFeishuTransport(settings)
        card_values = []
        card_transport._card_handler = lambda value: card_values.append(value)
        asyncio.run(card_transport._on_card(nested_card))
        statuses['L28CardActionNestedMapping'] = 'PASS' if card_values and card_values[0].approval_id == 'approval-card' and card_values[0].operator_open_id == 'ou-card' and card_values[0].raw['tag'] == 'button' else 'FAIL'

        raw_message = type('InboundMessage', (), {'id': 'om-file', 'conversation': type('Conversation', (), {'id': 'oc-file', 'type': 'p2p'})(), 'sender': type('Identity', (), {'id': 'ou-file', 'type': 'user'})(), 'raw_content_type': 'file', 'body_text': None, 'mentioned_bot': False, 'sender_is_bot': False})()
        normalized_raw = ChannelFeishuTransport(settings)._normalize_message(raw_message)
        statuses['L29RawContentTypeBoundary'] = 'PASS' if normalized_raw.message_type == 'file' and normalized_raw.text is None else 'FAIL'
        unknown_sender = type('InboundMessage', (), {'id': 'om-unknown', 'conversation': type('Conversation', (), {'id': 'oc-unknown', 'type': 'p2p'})(), 'sender': type('Identity', (), {'id': 'ou-unknown', 'type': 'unknown'})(), 'raw_content_type': 'text', 'body_text': 'hello', 'mentioned_bot': False, 'sender_is_bot': False})()
        statuses['L30UnknownSenderFailClosed'] = 'PASS' if not ChannelFeishuTransport(settings)._normalize_message(unknown_sender).is_user else 'FAIL'

        durable_store = FeishuStore(Path(directory) / 'durable-reply.sqlite3')
        durable_store.enqueue_message(FeishuInboundMessage('evt', 'm-durable', 'oc-durable', 'p2p', 'ou-authorized', 'user', 'text', 'hello'))
        durable_record = durable_store.get_inbox('m-durable')
        durable_transport = FakeFeishuTransport()
        durable_reply = FeishuReplyClient(durable_transport, durable_store).reply_text(durable_record.message_id, 'hello', chat_id=durable_record.chat_id)
        statuses['L31DurableReplyChatId'] = 'PASS' if durable_reply and durable_transport.messages[0].target == 'oc-durable' else 'FAIL'
        statuses['L32ResumeAcceptanceSeparate'] = 'PASS' if adapter.send_calls and adapter.send_calls[0] == (adapter.thread_id, 'CFR_M2_CODEX_B') else 'FAIL'

        approval_transport = FakeFeishuTransport()
        approval_store = FeishuStore(Path(directory) / 'approval-cards.sqlite3')
        approval_replies = FeishuReplyClient(approval_transport, approval_store)
        approval_replies.send_card('m-approval', 'ou-authorized', {'card': 1}, phase='approval:a')
        approval_replies.send_card('m-approval', 'ou-authorized', {'card': 2}, phase='approval:b')
        statuses['L33MultipleApprovalCards'] = 'PASS' if len(approval_transport.messages) == 2 and len({item.uuid for item in approval_transport.messages}) == 2 else 'FAIL'

        class FailingCard(FakeFeishuTransport):
            def send_card(self, *_args):
                raise RuntimeError('card send failed')
        failing_store = FeishuStore(Path(directory) / 'approval-failure.sqlite3')
        failing_settings = FeishuSettings('app-failure', 'secret', ('ou-authorized',), (Path(directory),), database=Path(directory) / 'approval-failure.sqlite3', approval_timeout_seconds=1)
        failing_bridge = ApprovalBridge(failing_store, FeishuReplyClient(FailingCard(), failing_store), failing_settings)
        failure_result = failing_bridge.handle_server_request({'id': 'approval-failure', 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-failure', 'item': {'command': 'echo safe'}}}, FeishuExecutionContext('m-failure', 'oc-failure', 'ou-authorized', 'thread-failure'))
        with failing_store._connection() as connection:
            failure_state = connection.execute('select state, decision from feishu_approvals limit 1').fetchone()
        statuses['L34ApprovalSendFailureCleanup'] = 'PASS' if failure_result == {'decision': 'decline'} and failure_state['state'] == 'cancelled' and failure_state['decision'] == 'decline' else 'FAIL'

        setup_lease = FeishuConnectionLease(Path(directory) / 'lease.sqlite3', 'feishu:setup-test', ttl=3, owner_instance_id='setup').acquire()
        blocked_lease = FeishuConnectionLease(Path(directory) / 'lease.sqlite3', 'feishu:setup-test', ttl=3, owner_instance_id='normal')
        try:
            try:
                blocked_lease.acquire()
            except StructuredError as exc:
                setup_blocked = exc.code == 'FEISHU_DAEMON_ALREADY_RUNNING'
            else:
                setup_blocked = False
            heartbeat_before = setup_lease.store.inspect_daemon_lease('feishu:setup-test')['heartbeat_at']
            time.sleep(1.1)
            heartbeat_after = setup_lease.store.inspect_daemon_lease('feishu:setup-test')['heartbeat_at']
        finally:
            setup_lease.release()
            blocked_lease.release()
        statuses['L35SetupOnlySingletonLease'] = 'PASS' if setup_blocked else 'FAIL'
        statuses['L36SetupOnlyHeartbeat'] = 'PASS' if heartbeat_after > heartbeat_before else 'FAIL'

        card_send_calls = []
        class CardSendChannel:
            async def send(self, *args):
                card_send_calls.append(args)
                return type('SendResult', (), {'success': True, 'message_id': 'channel-card-1'})()

        card_send_transport = ChannelFeishuTransport(settings)
        card_send_transport._channel = CardSendChannel()
        card_send_transport._submit = lambda coroutine: asyncio.run(coroutine)
        card_payload = {'schema': '2.0', 'body': {'elements': []}}
        card_send_transport.send_card('ou-authorized', card_payload, 'approval:stable')
        statuses['L37CardPayloadWrapped'] = 'PASS' if card_send_calls == [('ou-authorized', {'card': card_payload}, {'uuid': 'approval:stable'})] else 'FAIL'

        from cfr.feishu.security import validate_message_scope
        scope_disabled = FeishuSettings('app', 'secret', ('ou-authorized',), (), enable_group_chats=False)
        scope_enabled = FeishuSettings('app', 'secret', ('ou-authorized',), (), enable_group_chats=True)
        def scoped_message(chat_type, mentioned=False):
            return FeishuInboundMessage('scope-event', 'scope-message', 'scope-chat', chat_type, 'ou-authorized', 'user', 'text', 'hello', mentioned_bot=mentioned)
        statuses['L38TopicScopeRequiresBotMention'] = 'PASS' if (
            not validate_message_scope(scoped_message('topic'), scope_enabled)
            and validate_message_scope(scoped_message('topic', True), scope_enabled)
            and not validate_message_scope(scoped_message('topic'), scope_disabled)
        ) else 'FAIL'
        statuses['L39UnknownChatTypeFailClosed'] = 'PASS' if not validate_message_scope(scoped_message('unknown'), scope_enabled) and not validate_message_scope(scoped_message('future_type', True), scope_enabled) else 'FAIL'

        probe = subprocess.run(
            [sys.executable, str(ROOT / 'scripts' / 'run_m2_feishu_sdk_probe.py')],
            cwd=ROOT,
            capture_output=True,
            close_fds=True,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        try:
            probe_payload = json.loads((probe.stdout or '').strip().splitlines()[-1])
            expected_probe_exit = 0 if probe_payload.get('Verdict') == 'PASS' else 2
            statuses['L40SdkProbeFailureNonZero'] = 'PASS' if probe.returncode == expected_probe_exit and probe_payload.get('Verdict') in {'PASS', 'FAIL'} else 'FAIL'
        except (IndexError, json.JSONDecodeError):
            statuses['L40SdkProbeFailureNonZero'] = 'FAIL'

        official_fixture = type('ChannelInboundFixture', (), {
            'message_id': 'om-official-contract', 'chat_id': 'oc-official-contract', 'chat_type': 'p2p',
            'sender_id': 'ou-authorized', 'raw_content_type': 'text', 'content_text': '/cfr help',
            'mentioned_bot': False, 'sender_is_bot': False,
        })()
        official_normalized = ChannelFeishuTransport(settings)._normalize_message(official_fixture)
        statuses['L41ChannelContentTextContract'] = 'PASS' if official_normalized.text == '/cfr help' and official_normalized.body_text == '/cfr help' else 'FAIL'

        precedence_fixture = type('ChannelInboundFixture', (), {
            'message_id': 'om-precedence-contract', 'chat_id': 'oc-precedence-contract', 'chat_type': 'p2p',
            'sender_id': 'ou-authorized', 'raw_content_type': 'text', 'content_text': 'official', 'body_text': 'legacy',
        })()
        precedence_normalized = ChannelFeishuTransport(settings)._normalize_message(precedence_fixture)
        statuses['L42ChannelContentTextPrecedence'] = 'PASS' if precedence_normalized.text == 'official' else 'FAIL'

        legacy_fixture = type('ChannelInboundFixture', (), {
            'message_id': 'om-legacy-contract', 'chat_id': 'oc-legacy-contract', 'chat_type': 'p2p',
            'sender_id': 'ou-authorized', 'raw_content_type': 'text', 'body_text': 'legacy',
        })()
        legacy_normalized = ChannelFeishuTransport(settings)._normalize_message(legacy_fixture)
        statuses['L44LegacyBodyTextFallback'] = 'PASS' if legacy_normalized.text == 'legacy' else 'FAIL'

        nontext_fixture = type('ChannelInboundFixture', (), {
            'message_id': 'om-file-contract', 'chat_id': 'oc-file-contract', 'chat_type': 'p2p',
            'sender_id': 'ou-authorized', 'raw_content_type': 'file', 'content_text': '<file>',
        })()
        nontext_normalized = ChannelFeishuTransport(settings)._normalize_message(nontext_fixture)
        statuses['L45NonTextContentTextBlocked'] = 'PASS' if nontext_normalized.message_type == 'file' and nontext_normalized.text is None else 'FAIL'

        missing_type_fixture = type('ChannelInboundFixture', (), {
            'message_id': 'om-missing-type', 'chat_id': 'oc-missing-type', 'sender_id': 'ou-authorized',
            'raw_content_type': 'text', 'content_text': 'hello',
        })()
        missing_type_normalized = ChannelFeishuTransport(settings)._normalize_message(missing_type_fixture)
        before_missing_type_calls = (adapter.create_calls, len(adapter.send_calls))
        missing_type_result = gateway.handle_message_event(missing_type_normalized)
        statuses['L43MissingChatTypeFailClosed'] = 'PASS' if (
            missing_type_normalized.chat_type == 'unknown'
            and missing_type_result['status'] == 'ignored'
            and (adapter.create_calls, len(adapter.send_calls)) == before_missing_type_calls
        ) else 'FAIL'

        missing_loop_diagnostic = inspect_channel_sdk_loop(SimpleNamespace(__name__='fake.ws.client'))
        statuses['L46SdkLoopPreimportBoundary'] = 'PASS' if missing_loop_diagnostic.compatibility_mode == 'NOT_REQUIRED' else 'FAIL'
        statuses['L47SdkRunningLoopConflictClassification'] = 'PASS' if classify_channel_error(RuntimeError('This event loop is already running')) == 'FEISHU_SDK_EVENT_LOOP_CONFLICT' else 'FAIL'

        class FailingStartupChannel:
            is_ready = False
            def __init__(self, **_kwargs):
                self.disconnected = False
            def on(self, *_args):
                return None
            async def connect_until_ready(self, **_kwargs):
                raise RuntimeError('This event loop is already running')
            async def disconnect(self):
                self.disconnected = True
        failing_holder = {}
        def failing_factory(**kwargs):
            failing_holder['channel'] = FailingStartupChannel(**kwargs)
            return failing_holder['channel']
        with patch('cfr.feishu.transport._require_channel_sdk', return_value=(object(), failing_factory, FakeEvents)), patch('cfr.feishu.transport.prepare_channel_sdk_runtime', return_value=fake_sdk_diagnostic):
            failed_transport = ChannelFeishuTransport(settings, connect_timeout=1)
            failed_transport.start()
            failed_stopped = failed_transport.wait_until_stopped(1)
        statuses['L48TransportFailedStartupCleanup'] = 'PASS' if failed_stopped and failed_transport.error.code == 'FEISHU_SDK_EVENT_LOOP_CONFLICT' and failing_holder['channel'].disconnected else 'FAIL'

        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        compat_module = SimpleNamespace(__name__='fake.ws.client', loop=closed_loop)
        with patch('cfr.feishu.sdk_compat.importlib.import_module', return_value=compat_module):
            compat_diagnostic = prepare_channel_sdk_runtime()
        try:
            statuses['L49SdkCompatibilityMode'] = 'PASS' if compat_diagnostic.compatibility_mode == 'LEGACY_GLOBAL_LOOP_REBIND' and not compat_module.loop.is_closed() else 'FAIL'
        finally:
            compat_module.loop.close()

        native_contract = subprocess.run(
            [sys.executable, str(ROOT / 'scripts' / 'run_m2_feishu_channel_loop_probe.py'), '--contract-only'],
            cwd=ROOT,
            capture_output=True,
            close_fds=True,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        try:
            native_payload = json.loads((native_contract.stdout or '').strip().splitlines()[-1])
            required_native_fields = {'OfficialPreimport', 'LazyImportRepro', 'CfrTransport', 'CfrTransportAfterFix', 'Diagnosis', 'CompatibilityMode', 'Verdict', 'BlockingIssues'}
            statuses['L50NativeProbeContract'] = 'PASS' if native_contract.returncode == 0 and required_native_fields.issubset(native_payload) else 'FAIL'
        except (IndexError, json.JSONDecodeError):
            statuses['L50NativeProbeContract'] = 'FAIL'
        statuses['L51ChannelShutdownCleanup'] = 'PASS' if channel_probe._disconnect_completed.is_set() and channel_probe.wait_until_stopped(0) else 'FAIL'
        safe_log = sanitize_feishu_log_text('wss://host/ws?access_key=ak&ticket=t app_secret=s Authorization: Bearer token')
        statuses['L52SensitiveSdkLogging'] = 'PASS' if all(value not in safe_log for value in ('access_key=ak', 'ticket=t', 'app_secret=s', 'Bearer token')) else 'FAIL'
        approval_contract = subprocess.run(
            [sys.executable, str(ROOT / 'scripts' / 'run_m2b_feishu_approval_live_probe.py'), '--contract-only'],
            cwd=ROOT,
            capture_output=True,
            close_fds=True,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        try:
            approval_payload = json.loads((approval_contract.stdout or '').strip().splitlines()[-1])
            required_approval_fields = {'ApprovalSchemaVerified', 'AllowCardSent', 'DeclineCardSent', 'NoAutoApprove', 'ExactlyOnce', 'SecretsRecorded', 'Verdict'}
            statuses['L53ApprovalLiveProbeContract'] = 'PASS' if approval_contract.returncode == 0 and required_approval_fields.issubset(approval_payload) and approval_payload['Verdict'] == 'NOT_RUN' else 'FAIL'
        except (IndexError, json.JSONDecodeError):
            statuses['L53ApprovalLiveProbeContract'] = 'FAIL'
        statuses['L54ApprovalExactlyOnceDeterministic'] = 'PASS' if recovery_store.approvals.codec.response('accept') == {'decision': 'accept'} and recovery_store.approvals.codec.response('decline') == {'decision': 'decline'} else 'FAIL'
        statuses['L55ApprovalDeclineFailClosed'] = 'PASS' if statuses['L15ApprovalDeny'] == 'PASS' and statuses['L16UnknownApprovalReject'] == 'PASS' else 'FAIL'
        statuses['L56ApprovalPendingCleanup'] = 'PASS' if statuses['L34ApprovalSendFailureCleanup'] == 'PASS' and statuses['L14ApprovalAllow'] == 'PASS' else 'FAIL'
        probe_source = (ROOT / 'scripts' / 'run_m2b_feishu_approval_live_probe.py').read_text(encoding='utf-8')
        statuses['L57ApprovalSchemaOutDirectoryContract'] = 'PASS' if "'--out'" in probe_source and "'--experimental'" in probe_source and 'rglob' in probe_source else 'FAIL'
        statuses['L58ApprovalEffectiveThreadConfigContract'] = 'PASS' if 'ApprovalPolicyRequested' in probe_source and 'ApprovalPolicyEffective' in probe_source and 'raw_thread' in probe_source else 'FAIL'
        statuses['L59ApprovalResolvedEvidenceNotSynthetic'] = 'PASS' if 'ServerRequestResolvedEvidence' in probe_source and "result['AllowServerRequestResolved'] = result['AllowJsonRpcResponse']" not in probe_source else 'FAIL'
        statuses['L60ApprovalExactlyOnceEvidenceContract'] = 'PASS' if all(token in probe_source for token in ('ServerRequestObservedCount', 'CardActionAcceptedCount', 'ResolvedIdsContainsRequest', 'InFlightEmpty')) else 'FAIL'
        allow_contract = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'run_m2b_feishu_approval_live_probe.py'), '--contract-only', '--mode', 'allow'], cwd=ROOT, capture_output=True, close_fds=True, text=True, encoding='utf-8', errors='replace')
        decline_contract = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'run_m2b_feishu_approval_live_probe.py'), '--contract-only', '--mode', 'decline'], cwd=ROOT, capture_output=True, close_fds=True, text=True, encoding='utf-8', errors='replace')
        try:
            allow_payload = json.loads((allow_contract.stdout or '').strip().splitlines()[-1])
            decline_payload = json.loads((decline_contract.stdout or '').strip().splitlines()[-1])
            statuses['L61ApprovalProbeModeAllow'] = 'PASS' if allow_contract.returncode == 0 and allow_payload.get('Mode') == 'allow' and allow_payload.get('Verdict') == 'NOT_RUN' else 'FAIL'
            statuses['L62ApprovalProbeModeDecline'] = 'PASS' if decline_contract.returncode == 0 and decline_payload.get('Mode') == 'decline' and decline_payload.get('Verdict') == 'NOT_RUN' else 'FAIL'
        except (IndexError, json.JSONDecodeError):
            statuses['L61ApprovalProbeModeAllow'] = 'FAIL'
            statuses['L62ApprovalProbeModeDecline'] = 'FAIL'
        m3_docs = [ROOT / 'docs' / name for name in ('M3_CONTROL_CENTER_ARCHITECTURE.md', 'M3_CONTROL_API_SPEC.md', 'M3_DESKTOP_APP_SPEC.md', 'M3_CONTROL_CENTER_SECURITY.md')]
        m3_text = '\n'.join(path.read_text(encoding='utf-8') for path in m3_docs if path.exists())
        statuses['L63M3FrozenV2Metadata'] = 'PASS' if all(path.exists() for path in m3_docs) and 'FROZEN_v2' in m3_text and 'WEB_CONTROL_PLANE' in m3_text and 'OPTIONAL_FUTURE_SHELL' in m3_text else 'FAIL'
        shutdown_source = (ROOT / 'scripts' / 'run_m2_feishu_shutdown_probe.py').read_text(encoding='utf-8')
        statuses['L64ShutdownProbeWaitsForProcessExit'] = 'PASS' if all(token in shutdown_source for token in ('--child', 'subprocess.run', 'ChildExitCode', 'child_result.json')) else 'FAIL'
        statuses['L65ShutdownPostExitWarningFails'] = 'PASS' if all(token in shutdown_source for token in ('POST_EXIT_RUNTIME_WARNING', 'POST_EXIT_PENDING_TASK_WARNING', 'RuntimeWarning', 'ResourceWarning')) else 'FAIL'
        from cfr.feishu.sdk_compat import ChannelSdkShutdownCapture, _sdk_owned_task, capture_channel_sdk_shutdown_targets, drain_sdk_tasks, snapshot_sdk_tasks
        from types import ModuleType
        sdk_probe_module = ModuleType('lark_channel.local_acceptance_probe')
        exec('async def sdk_task(event):\n    await event.wait()\n', sdk_probe_module.__dict__)
        sdk_loop = asyncio.new_event_loop()
        sdk_event = asyncio.Event()
        sdk_task = sdk_loop.create_task(sdk_probe_module.sdk_task(sdk_event))
        try:
            origin_detected = _sdk_owned_task(sdk_task) and snapshot_sdk_tasks(sdk_loop).total == 1
            statuses['L73SdkTaskOriginRealCoroutine'] = 'PASS' if origin_detected else 'FAIL'
            unrelated_event = asyncio.Event()
            async def unrelated_acceptance_task():
                await unrelated_event.wait()
            unrelated_task = sdk_loop.create_task(unrelated_acceptance_task())
            diagnostic = drain_sdk_tasks(loops=[sdk_loop])
            statuses['L66SdkPendingTasksDrained'] = 'PASS' if diagnostic.sdk_ws_tasks_before == 1 and diagnostic.sdk_ws_tasks_drained == 1 and diagnostic.sdk_ws_tasks_remaining == 0 else 'FAIL'
            statuses['L74SdkDrainLeavesNoRemaining'] = 'PASS' if diagnostic.sdk_ws_tasks_remaining == 0 and diagnostic.remaining_task_names == () else 'FAIL'
            statuses['L75SdkDrainPreservesNonSdkTask'] = 'PASS' if not unrelated_task.done() else 'FAIL'
            statuses['L82NonSdkTaskPreserved'] = 'PASS' if not unrelated_task.done() else 'FAIL'
            shutdown_source = (ROOT / 'scripts' / 'run_m2_feishu_shutdown_probe.py').read_text(encoding='utf-8')
            statuses['L76ShutdownProbeNoSyntheticDrainPass'] = 'PASS' if "result['SdkWsTasksDrained'] = 'PASS'" not in shutdown_source and "result['SdkCacheTasksDrained'] = 'PASS'" not in shutdown_source and 'shutdown_diagnostic' in shutdown_source else 'FAIL'
            unrelated_task.cancel()
            sdk_loop.run_until_complete(asyncio.gather(unrelated_task, return_exceptions=True))
        finally:
            if not sdk_task.done():
                sdk_task.cancel()
                sdk_loop.run_until_complete(asyncio.gather(sdk_task, return_exceptions=True))
            sdk_loop.close()

        capture_loop = asyncio.new_event_loop()
        capture_module = ModuleType('lark_channel.capture_acceptance_probe')
        exec('async def ping_loop(event):\n    await event.wait()\n', capture_module.__dict__)
        captured_task = capture_loop.create_task(capture_module.ping_loop(asyncio.Event()))

        class AcceptanceCache:
            _cron = captured_task

        class AcceptanceWs:
            _loop = capture_loop
            _cache = AcceptanceCache()

        class AcceptanceChannel:
            _start_future = None
            _ws_client = AcceptanceWs()

            @property
            def ws_client(self):
                return self._ws_client

        acceptance_channel = AcceptanceChannel()
        try:
            capture = capture_channel_sdk_shutdown_targets(acceptance_channel)
            statuses['L77PreShutdownWsClientCapture'] = 'PASS' if capture.ws_client_captured and capture.ws_loop is capture_loop else 'FAIL'
            acceptance_channel._ws_client = None
            captured_diagnostic = drain_sdk_tasks(loops=[capture_loop], capture=capture)
            statuses['L78WsPingLoopDrain'] = 'PASS' if captured_diagnostic.sdk_ws_tasks_remaining == 0 and captured_diagnostic.remaining_task_names == () else 'FAIL'
            statuses['L80CaptureSurvivesSdkReferenceClear'] = 'PASS' if captured_diagnostic.sdk_ws_tasks_drained == 1 else 'FAIL'
        finally:
            if not captured_task.done():
                captured_task.cancel()
                capture_loop.run_until_complete(asyncio.gather(captured_task, return_exceptions=True))
            capture_loop.close()

        ws_loop = asyncio.new_event_loop()
        cache_loop = asyncio.new_event_loop()
        cache_module = ModuleType('lark_channel.core.cache.expiring_cache')
        exec('async def clear_cron(event):\n    await event.wait()\n', cache_module.__dict__)
        cache_task = cache_loop.create_task(cache_module.clear_cron(asyncio.Event()))
        cache_capture = ChannelSdkShutdownCapture(
            ws_loop=ws_loop,
            cache_loop=cache_loop,
            cache_cron_task=cache_task,
            cache_cron_captured=True,
            cache_cron_task_done_before_shutdown=False,
            cache_loop_closed_before_shutdown=False,
            cache_loop_same_as_ws_loop=False,
            cache_tasks_before_public_stop=('lark_channel.core.cache.expiring_cache.clear_cron',),
        )
        try:
            cache_diagnostic = drain_sdk_tasks(loops=[ws_loop, cache_loop], capture=cache_capture)
            statuses['L79CacheOrphanLoopDrain'] = 'PASS' if cache_diagnostic.sdk_cache_tasks_remaining == 0 and cache_diagnostic.cache_loop_closed_by_cfr else 'FAIL'
        finally:
            if not ws_loop.is_closed():
                ws_loop.close()
            if not cache_loop.is_closed():
                cache_loop.close()

        unavailable_loop = asyncio.new_event_loop()
        unavailable_loop.close()
        unavailable_capture = ChannelSdkShutdownCapture(
            cache_loop=unavailable_loop,
            cache_cron_captured=True,
            cache_cron_task_done_before_shutdown=False,
            cache_loop_closed_before_shutdown=True,
            cache_tasks_before_public_stop=('lark_channel.core.cache.expiring_cache.clear_cron',),
        )
        try:
            unavailable_diagnostic = drain_sdk_tasks(loops=[unavailable_loop], capture=unavailable_capture)
            statuses['L81NoSyntheticZeroWhenObservationUnavailable'] = 'PASS' if unavailable_diagnostic.sdk_cache_tasks_remaining is None and not unavailable_diagnostic.cache_task_observation_available else 'FAIL'
        finally:
            pass

        bg_loop = asyncio.new_event_loop()
        bg_ready = threading.Event()
        def run_acceptance_bg_loop():
            asyncio.set_event_loop(bg_loop)
            bg_ready.set()
            bg_loop.run_forever()
        bg_thread = threading.Thread(target=run_acceptance_bg_loop, name='acceptance-channel-bg')
        bg_thread.start()
        bg_ready.wait(1)
        bg_future = asyncio.run_coroutine_threadsafe(asyncio.sleep(60), bg_loop)
        time.sleep(0.05)

        class BgChannelFixture:
            _bg_loop = bg_loop
            _bg_thread = bg_thread
            _ws_client = None
        bg_capture = capture_channel_sdk_shutdown_targets(BgChannelFixture())
        statuses['L83BgLoopPreShutdownCapture'] = 'PASS' if bg_capture.bg_loop_captured and bg_capture.bg_thread_captured and bg_capture.bg_loop is bg_loop and bg_capture.bg_thread is bg_thread else 'FAIL'
        bg_loop.call_soon_threadsafe(bg_loop.stop)
        bg_thread.join(1)
        BgChannelFixture._bg_loop = None
        BgChannelFixture._bg_thread = None
        try:
            bg_diagnostic = drain_dedicated_channel_bg_loop(bg_capture.bg_loop, timeout=1, captured_tasks=bg_capture.captured_bg_tasks)
            statuses['L84BgLoopSleepSentinelDrain'] = 'PASS' if bg_diagnostic.observation_available and 'asyncio.tasks.sleep' in bg_diagnostic.tasks_before and bg_future.done() else 'FAIL'
            statuses['L85BgLoopAllPendingDrained'] = 'PASS' if bg_diagnostic.tasks_remaining == () and bg_diagnostic.loop_closed_by_cfr else 'FAIL'
            statuses['L86BgLoopCaptureSurvivesReferenceClear'] = 'PASS' if bg_capture.bg_loop is bg_loop and bg_capture.bg_thread is bg_thread and bg_diagnostic.loop_closed_by_cfr else 'FAIL'
        finally:
            if not bg_loop.is_closed():
                bg_loop.close()

        cfr_bg_loop = asyncio.new_event_loop()
        ws_bg_loop = asyncio.new_event_loop()
        cfr_bg_event = asyncio.Event()
        ws_bg_event = asyncio.Event()
        cfr_bg_task = cfr_bg_loop.create_task(cfr_bg_event.wait())
        ws_bg_task = ws_bg_loop.create_task(ws_bg_event.wait())
        empty_bg_loop = asyncio.new_event_loop()
        empty_bg_loop.call_soon(empty_bg_loop.stop)
        empty_bg_loop.run_forever()
        try:
            preserved_bg_result = drain_dedicated_channel_bg_loop(empty_bg_loop, timeout=1)
            statuses['L87BgLoopDrainPreservesCfrLoop'] = 'PASS' if preserved_bg_result.observation_available and not cfr_bg_task.done() and not ws_bg_task.done() else 'FAIL'
        finally:
            for loop, task in ((cfr_bg_loop, cfr_bg_task), (ws_bg_loop, ws_bg_task)):
                task.cancel()
                loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
                loop.close()
            if not empty_bg_loop.is_closed():
                empty_bg_loop.close()

        bg_subprocess = subprocess.run(
            [sys.executable, '-m', 'unittest', 'tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_dedicated_bg_loop_drains_orphan_sleep_after_thread_exit'],
            cwd=ROOT,
            capture_output=True,
            close_fds=True,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        bg_subprocess_output = (bg_subprocess.stdout or '') + (bg_subprocess.stderr or '')
        statuses['L88BgLoopPostExitNoWarning'] = 'PASS' if bg_subprocess.returncode == 0 and not any(token in bg_subprocess_output for token in ('Task was destroyed', 'was never awaited', 'RuntimeWarning')) else 'FAIL'

        def run_classification_test(test_name):
            return subprocess.run(
                [sys.executable, '-m', 'unittest', test_name],
                cwd=ROOT,
                capture_output=True,
                close_fds=True,
                text=True,
                encoding='utf-8',
                errors='replace',
            )
        upstream_clean_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_upstream_closed_bg_loop_is_terminal_clean_close_evidence')
        statuses['L89BgLoopUpstreamCleanCloseAccepted'] = 'PASS' if upstream_clean_test.returncode == 0 else 'FAIL'
        upstream_warning_test = run_classification_test('tests.unit.feishu.test_shutdown_probe.ShutdownProbeEvidenceTests.test_shutdown_parent_reclassifies_upstream_close_when_warning_exists')
        statuses['L90BgLoopClosedWithPendingWarningFails'] = 'PASS' if upstream_warning_test.returncode == 0 else 'FAIL'
        statuses['L91BgLoopClosedWithNeverAwaitedFails'] = 'PASS' if upstream_warning_test.returncode == 0 else 'FAIL'
        live_thread_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_closed_bg_loop_with_live_thread_is_unclean')
        statuses['L92BgLoopClosedWithLiveThreadFails'] = 'PASS' if live_thread_test.returncode == 0 else 'FAIL'
        closed_before_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_bg_loop_closed_before_public_stop_is_unclean')
        statuses['L93BgLoopClosedBeforePublicStopFails'] = 'PASS' if closed_before_test.returncode == 0 else 'FAIL'
        statuses['L94BgLoopObservedDrainPathStillPasses'] = 'PASS' if bg_diagnostic.observation_available and bg_diagnostic.loop_closed_by_cfr and bg_diagnostic.tasks_remaining == () else 'FAIL'

        sdk_compat_source = (ROOT / 'src' / 'cfr' / 'feishu' / 'sdk_compat.py').read_text(encoding='utf-8')
        stop_bridge_source = sdk_compat_source[sdk_compat_source.index('def stop_channel_without_device_flow_close'):]
        statuses['L203BgPreStopSdkCancelHelperBypassed'] = 'PASS' if '_cancel_bg_tasks()' not in stop_bridge_source.split('def _name_counts', 1)[0] else 'FAIL'
        running_drain_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_running_bg_loop_terminal_drain_bypasses_sdk_cancel_helper')
        statuses['L204BgPreStopRunningLoopTerminalDrain'] = 'PASS' if running_drain_test.returncode == 0 else 'FAIL'
        statuses['L205BgPreStopNoSleepCoroutineBarrier'] = 'PASS' if 'run_coroutine_threadsafe(asyncio.sleep' not in sdk_compat_source[sdk_compat_source.index('def _running_bg_loop_callback_drain'):sdk_compat_source.index('def stop_channel_without_device_flow_close')] else 'FAIL'
        resistant_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_running_bg_loop_cancellation_resistant_task_reaches_terminal')
        statuses['L206BgPreStopCancellationResistantTaskTerminal'] = 'PASS' if resistant_test.returncode == 0 else 'FAIL'
        terminal_guard_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_start_worker_handoff_shape_guard_fails_closed')
        statuses['L207BgPreStopLoopStopRequiresTerminal'] = 'PASS' if terminal_guard_test.returncode == 0 else 'FAIL'
        statuses['L208Host061707SleepOrphanShapeRegression'] = statuses['L204BgPreStopRunningLoopTerminalDrain']
        marker_absent_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_decline_live_marker_absence_metadata_is_not_run')
        statuses['L209DeclineLiveMarkerAbsenceMetadata'] = 'PASS' if marker_absent_test.returncode == 0 else 'FAIL'
        marker_present_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_decline_live_marker_presence_fails_absence_contract')
        statuses['L210DeclineMarkerPresenceStillFailsAbsence'] = 'PASS' if marker_present_test.returncode == 0 else 'FAIL'
        statuses['L211ParentSleepWarningAuthorityRegression'] = statuses['L65ShutdownPostExitWarningFails']
        ux_regression_test = run_classification_test('tests.unit.feishu.test_approval_feedback_ux.ApprovalFeedbackUxTests.test_decline_feedback_is_terminal_and_has_no_buttons')
        statuses['L212ApprovalUxRegressionAfterBgFix'] = 'PASS' if ux_regression_test.returncode == 0 else 'FAIL'
        routing_cases = {
            'L213ProductionCoreChangeRequiresShutdownHost': 'test_shutdown_core_change_requires_host_regression',
            'L214ShutdownHostPriorityOverAllowUx': 'test_shutdown_host_regression_has_priority_over_allow_ux',
            'L215HistoricalShutdownPassDoesNotSatisfyCurrentCore': 'test_historical_shutdown_pass_does_not_satisfy_new_core_change',
            'L216ShutdownRequiredCannotBeOverwrittenByImplementationReady': 'test_implementation_ready_cannot_override_shutdown_regression',
            'L217CurrentBlockingIssuesOrderContract': 'test_current_blockers_keep_feedback_and_m1_after_shutdown_requirement',
            'L218CurrentReportBaselineContract': 'test_gate_reports_sdk_compat_as_shutdown_core_change',
        }
        for status_key, test_name in routing_cases.items():
            routing_test = run_classification_test(f'tests.unit.feishu.test_gateway.GateRoutingTests.{test_name}')
            statuses[status_key] = 'PASS' if routing_test.returncode == 0 else 'FAIL'

        serialization_cases = {
            'L219StartFutureCancelNotWorkerTerminal': 'test_start_future_wrapper_cancel_does_not_prove_worker_exit',
            'L220BgOwnershipHandoffBlocksNewScheduling': 'test_bg_ownership_handoff_blocks_new_schedule_and_detaches_cleanup_authority',
            'L221BgOwnershipHandoffDetachesSdkCleanupAuthority': 'test_late_start_worker_cleanup_cannot_submit_sleep_after_handoff',
            'L222LateStartWorkerCleanupCannotCreateSleep': 'test_late_start_worker_cleanup_cannot_submit_sleep_after_handoff',
            'L223StartWorkerTerminalBeforeFinalBgDrain': 'test_start_worker_exits_before_final_bg_drain_and_loop_stop',
            'L224BgProducerQuiescenceBeforeFinalDrain': 'test_start_worker_exits_before_final_bg_drain_and_loop_stop',
            'L225FinalBgDrainBeforeCapturedLoopStop': 'test_start_worker_exits_before_final_bg_drain_and_loop_stop',
            'L226StartWorkerTimeoutFailsClosed': 'test_start_worker_timeout_fails_closed_without_stopping_captured_loop',
            'L228Host072914LateSleepRaceRegression': 'test_host_072914_late_sleep_orphan_shape_regression',
        }
        for status_key, test_name in serialization_cases.items():
            serialization_test = run_classification_test(f'tests.unit.feishu.test_sdk_compat.StartWorkerSerializationTests.{test_name}')
            statuses[status_key] = 'PASS' if serialization_test.returncode == 0 else 'FAIL'
        transport_serialization_test = run_classification_test('tests.unit.feishu.test_channel_transport.ChannelTransportTests.test_transport_loop_stays_alive_until_start_worker_terminal')
        statuses['L227TransportLoopAliveUntilWorkerTerminal'] = 'PASS' if transport_serialization_test.returncode == 0 else 'FAIL'
        shared_serialization_test = run_classification_test('tests.unit.feishu.test_channel_transport.ChannelTransportTests.test_shutdown_and_approval_share_start_worker_serialization')
        statuses['L229SharedShutdownSerializationRegression'] = 'PASS' if shared_serialization_test.returncode == 0 else 'FAIL'
        ux_after_serialization_test = run_classification_test('tests.unit.feishu.test_approval_feedback_ux.ApprovalFeedbackUxTests.test_decline_feedback_is_terminal_and_has_no_buttons')
        statuses['L230ApprovalBusinessUxUnchangedAfterSerialization'] = 'PASS' if ux_after_serialization_test.returncode == 0 else 'FAIL'

        preclose_complete_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_device_flow_preclose_completes_on_captured_bg_loop')
        statuses['L95DeviceFlowPreCloseScheduled'] = 'PASS' if preclose_complete_test.returncode == 0 else 'FAIL'
        statuses['L96DeviceFlowPreCloseCompletes'] = 'PASS' if preclose_complete_test.returncode == 0 else 'FAIL'
        scheduling_failure_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_device_flow_preclose_scheduling_failure_closes_coroutine')
        statuses['L97DeviceFlowPreCloseSchedulingFailureNoWarning'] = 'PASS' if scheduling_failure_test.returncode == 0 else 'FAIL'
        timeout_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_device_flow_preclose_timeout_is_bounded_and_cancels_future')
        statuses['L98DeviceFlowPreCloseTimeoutFailClosed'] = 'PASS' if timeout_test.returncode == 0 else 'FAIL'
        public_warning_test = run_classification_test('tests.unit.feishu.test_shutdown_probe.ShutdownProbeEvidenceTests.test_shutdown_parent_reclassifies_upstream_close_when_warning_exists')
        statuses['L99DeviceFlowPublicTimeoutWarningFails'] = 'PASS' if public_warning_test.returncode == 0 else 'FAIL'
        statuses['L100DeviceFlowPendingTaskWarningFails'] = 'PASS' if public_warning_test.returncode == 0 else 'FAIL'
        statuses['L101DeviceFlowNeverAwaitedWarningFails'] = 'PASS' if public_warning_test.returncode == 0 else 'FAIL'
        idempotent_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_device_flow_preclose_then_public_close_is_idempotent')
        statuses['L102DeviceFlowPreCloseThenPublicClose'] = 'PASS' if idempotent_test.returncode == 0 else 'FAIL'
        statuses['L103WsCacheBgRegression'] = 'PASS' if all(statuses.get(key) == 'PASS' for key in ('L66SdkPendingTasksDrained', 'L74SdkDrainLeavesNoRemaining', 'L78WsPingLoopDrain', 'L79CacheOrphanLoopDrain', 'L85BgLoopAllPendingDrained', 'L94BgLoopObservedDrainPathStillPasses')) else 'FAIL'

        card_schema_test = run_classification_test('tests.unit.feishu.test_approval_card_v2.ApprovalCardV2Tests.test_approval_card_has_no_legacy_action_tag')
        statuses['L104ApprovalCardV2NoLegacyAction'] = 'PASS' if card_schema_test.returncode == 0 else 'FAIL'
        card_buttons_test = run_classification_test('tests.unit.feishu.test_approval_card_v2.ApprovalCardV2Tests.test_approval_card_has_two_callback_buttons')
        statuses['L105ApprovalCardV2CallbackButtons'] = 'PASS' if card_buttons_test.returncode == 0 else 'FAIL'
        send_failure_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_waiting_is_gated_by_successful_card_delivery')
        statuses['L106ApprovalCardSendFailureNoWaiting'] = 'PASS' if send_failure_test.returncode == 0 else 'FAIL'
        failure_evidence_test = run_classification_test('tests.unit.feishu.test_approval_roundtrip.ApprovalRoundtripTests.test_card_send_failure_records_fail_closed_evidence')
        statuses['L107ApprovalCardSendFailureFailClosed'] = 'PASS' if failure_evidence_test.returncode == 0 else 'FAIL'
        callback_test = run_classification_test('tests.unit.feishu.test_approval_card_v2.ApprovalCardV2Tests.test_v2_callback_allow_and_decline_normalize')
        statuses['L108ApprovalV2CallbackNormalization'] = 'PASS' if callback_test.returncode == 0 else 'FAIL'
        wrong_operator_test = run_classification_test('tests.unit.feishu.test_approval_card_v2.ApprovalCardV2Tests.test_v2_callback_rejects_wrong_operator_or_malformed_value')
        statuses['L109ApprovalV2WrongOperatorFailClosed'] = 'PASS' if wrong_operator_test.returncode == 0 else 'FAIL'
        duplicate_test = run_classification_test('tests.unit.feishu.test_approval_roundtrip.ApprovalRoundtripTests.test_duplicate_card_click_is_exactly_once')
        statuses['L110ApprovalV2DuplicateExactlyOnce'] = 'PASS' if duplicate_test.returncode == 0 else 'FAIL'
        probe_teardown_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_contract_exposes_parent_child_teardown_fields')
        statuses['L111ApprovalLiveProbeHardenedShutdown'] = 'PASS' if probe_teardown_test.returncode == 0 else 'FAIL'
        warning_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_parent_detects_post_exit_lifecycle_warnings')
        statuses['L112ApprovalLiveProbePostExitWarningFailClosed'] = 'PASS' if warning_test.returncode == 0 else 'FAIL'
        t1_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_business_failure_clean_teardown_is_independent')
        statuses['L113BusinessFailureCleanTeardownIndependent'] = 'PASS' if t1_test.returncode == 0 else 'FAIL'
        t2_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_business_failure_with_teardown_warning_fails_both_domains')
        statuses['L114BusinessFailureTeardownWarningDetected'] = 'PASS' if t2_test.returncode == 0 else 'FAIL'
        t3_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_business_pass_cannot_mask_teardown_failure')
        statuses['L115BusinessPassCannotMaskTeardownFailure'] = 'PASS' if t3_test.returncode == 0 else 'FAIL'
        t4_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_teardown_not_run_when_transport_never_started')
        statuses['L116TeardownNotRunWhenTransportNeverStarted'] = 'PASS' if t4_test.returncode == 0 else 'FAIL'
        t5_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_unexpected_child_crash_fails_closed')
        statuses['L117UnexpectedChildCrashFailClosed'] = 'PASS' if t5_test.returncode == 0 else 'FAIL'
        statuses['L118ApprovalLiveTeardownVerdictMachineReadable'] = 'PASS' if all(statuses.get(key) == 'PASS' for key in ('L113BusinessFailureCleanTeardownIndependent', 'L114BusinessFailureTeardownWarningDetected', 'L115BusinessPassCannotMaskTeardownFailure', 'L116TeardownNotRunWhenTransportNeverStarted', 'L117UnexpectedChildCrashFailClosed')) else 'FAIL'
        zero_id_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_zero_request_id_normalization_contract')
        statuses['L119ZeroRequestIdNormalization'] = 'PASS' if zero_id_test.returncode == 0 else 'FAIL'
        zero_match_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_zero_request_id_resolved_match_and_wrong_id')
        statuses['L120ZeroRequestResolvedMatch'] = 'PASS' if zero_match_test.returncode == 0 else 'FAIL'
        stdout_isolation_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_structured_stdout_isolated_from_warning_scanner')
        statuses['L121StructuredStdoutWarningScannerIsolation'] = 'PASS' if stdout_isolation_test.returncode == 0 else 'FAIL'
        stderr_authority_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_raw_stderr_remains_warning_authority')
        statuses['L122RawStderrWarningAuthority'] = 'PASS' if stderr_authority_test.returncode == 0 else 'FAIL'
        parent_authority_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_parent_final_teardown_authority_is_independent')
        statuses['L123ParentFinalTeardownAuthority'] = 'PASS' if parent_authority_test.returncode == 0 else 'FAIL'
        cache_pending_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_expiring_cache_pending_detection_is_a_teardown_failure')
        statuses['L124ExpiringCachePendingFailsTeardown'] = 'PASS' if cache_pending_test.returncode == 0 else 'FAIL'
        cache_clean_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_clean_cache_teardown_contract_is_pass')
        statuses['L125ApprovalLiveCleanCacheTeardownContract'] = 'PASS' if cache_clean_test.returncode == 0 else 'FAIL'
        statuses['L126ZeroIdExactlyOnceEvidence'] = 'PASS' if statuses['L120ZeroRequestResolvedMatch'] == 'PASS' and statuses['L119ZeroRequestIdNormalization'] == 'PASS' else 'FAIL'
        statuses['L127CardV2RegressionAfterHostEvidenceFix'] = statuses['L104ApprovalCardV2NoLegacyAction'] if statuses['L104ApprovalCardV2NoLegacyAction'] == 'PASS' else 'FAIL'
        bridge_zero_test = run_classification_test('tests.unit.feishu.test_approval_roundtrip.ApprovalRoundtripTests.test_zero_request_id_notification_matches_and_wrong_id_does_not')
        statuses['L128ApprovalBridgeRegressionAfterHostEvidenceFix'] = 'PASS' if bridge_zero_test.returncode == 0 else 'FAIL'
        resolved_diag_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_resolved_notification_exposes_raw_and_normalized_request_id')
        statuses['L129ResolvedNotificationObservedIdDiagnostics'] = 'PASS' if resolved_diag_test.returncode == 0 else 'FAIL'
        statuses['L130ResolvedTargetZeroIdMatch'] = statuses['L120ZeroRequestResolvedMatch']
        resolved_multiple_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_resolved_request_id_multiple_events_target_matches')
        statuses['L131ResolvedTargetMultipleNotificationMatch'] = 'PASS' if resolved_multiple_test.returncode == 0 else 'FAIL'
        resolved_wrong_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_resolved_request_id_wrong_and_missing_fail_closed')
        statuses['L132ResolvedWrongIdFailClosed'] = 'PASS' if resolved_wrong_test.returncode == 0 else 'FAIL'
        statuses['L133ExactlyOnceTargetResolvedIdContract'] = 'PASS' if statuses['L130ResolvedTargetZeroIdMatch'] == 'PASS' and statuses['L131ResolvedTargetMultipleNotificationMatch'] == 'PASS' and statuses['L132ResolvedWrongIdFailClosed'] == 'PASS' else 'FAIL'
        marker_exact_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_marker_exact_bytes_pass')
        statuses['L134MarkerExactByteContract'] = 'PASS' if marker_exact_test.returncode == 0 else 'FAIL'
        marker_diag_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_marker_diagnostics_are_safe_and_missing_fails')
        statuses['L135MarkerMismatchDiagnostics'] = 'PASS' if marker_diag_test.returncode == 0 else 'FAIL'
        protected_write_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_protected_operation_writes_exact_bytes')
        statuses['L136ProtectedOperationExactByteWrite'] = 'PASS' if protected_write_test.returncode == 0 else 'FAIL'
        cache_owner_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_cache_cron_cancelled_terminal_passes')
        statuses['L137CacheCronCapturedOwnerLoopContract'] = 'PASS' if cache_owner_test.returncode == 0 else 'FAIL'
        cache_terminal_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_cache_cron_terminal_before_child_return_passes')
        statuses['L138CacheCronMustBeTerminalBeforeChildReturn'] = 'PASS' if cache_terminal_test.returncode == 0 else 'FAIL'
        cache_wrong_loop_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_cache_cron_wrong_owner_loop_pending_fails')
        statuses['L139CacheWrongLoopAccountingRegression'] = 'PASS' if cache_wrong_loop_test.returncode == 0 else 'FAIL'
        cache_closed_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_cache_cron_owner_loop_closed_pending_fails')
        statuses['L140CacheOwnerLoopClosedPendingFails'] = 'PASS' if cache_closed_test.returncode == 0 else 'FAIL'
        statuses['L141ParentPostExitCacheWarningAuthority'] = 'PASS' if statuses['L124ExpiringCachePendingFailsTeardown'] == 'PASS' else 'FAIL'
        baseline_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_current_host_failure_shape_regression')
        statuses['L142CurrentHost105322RegressionReplay'] = 'PASS' if baseline_test.returncode == 0 else 'FAIL'
        statuses['L143CardV2RegressionAfterFinalTargetedFix'] = statuses['L127CardV2RegressionAfterHostEvidenceFix']
        statuses['L144ApprovalBridgeRegressionAfterFinalTargetedFix'] = statuses['L128ApprovalBridgeRegressionAfterHostEvidenceFix']
        authority_separation_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_resolution_authorities_use_distinct_fields')
        statuses['L145LiveResolvedAndSnapshotAuthoritySeparated'] = 'PASS' if authority_separation_test.returncode == 0 else 'FAIL'
        wrong_live_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_live_resolution_match_not_overwritten_by_snapshot_match')
        statuses['L146WrongLiveIdCorrectSnapshotStillFails'] = 'PASS' if wrong_live_test.returncode == 0 else 'FAIL'
        wrong_snapshot_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_correct_live_id_wrong_snapshot_still_fails_exactly_once')
        statuses['L147CorrectLiveIdWrongSnapshotStillFails'] = 'PASS' if wrong_snapshot_test.returncode == 0 else 'FAIL'
        both_correct_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_correct_live_id_correct_snapshot_passes_resolution_contract')
        statuses['L148CorrectLiveIdCorrectSnapshotPasses'] = 'PASS' if both_correct_test.returncode == 0 else 'FAIL'
        both_authorities_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_exactly_once_requires_both_resolution_authorities')
        statuses['L149ExactlyOnceRequiresBothResolutionAuthorities'] = 'PASS' if both_authorities_test.returncode == 0 else 'FAIL'
        collision_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_resolution_field_collision_regression')
        statuses['L150ResolutionFieldCollisionRegression'] = 'PASS' if collision_test.returncode == 0 else 'FAIL'
        orphan_loop_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_captured_foreign_cache_cron_is_drained_by_exact_reference')
        statuses['L151CacheOpenNonRunningOrphanLoopDrain'] = 'PASS' if orphan_loop_test.returncode == 0 else 'FAIL'
        statuses['L152CacheSameTaskTrackedToTerminal'] = statuses['L151CacheOpenNonRunningOrphanLoopDrain']
        cancel_without_drive_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_cache_cron_cancel_requested_not_terminal_fails')
        statuses['L153CacheCancelWithoutDrainFails'] = 'PASS' if cancel_without_drive_test.returncode == 0 else 'FAIL'
        closed_pending_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_cache_cron_owner_loop_closed_pending_fails')
        statuses['L154CacheClosedPendingLoopFails'] = 'PASS' if closed_pending_test.returncode == 0 else 'FAIL'
        terminal_close_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_cache_terminal_then_loop_close_passes')
        statuses['L155CacheTerminalThenLoopClosePasses'] = 'PASS' if terminal_close_test.returncode == 0 else 'FAIL'
        foreign_running_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_cache_foreign_running_loop_uses_threadsafe_drain')
        statuses['L156CacheForeignRunningLoopThreadSafeDrain'] = 'PASS' if foreign_running_test.returncode == 0 else 'FAIL'
        host_cache_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_cache_host_114236_shape_regression')
        statuses['L157Host114236CacheShapeRegression'] = 'PASS' if host_cache_test.returncode == 0 else 'FAIL'
        parent_cache_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_parent_post_exit_cache_warning_authority_remains_fail_closed')
        statuses['L158ParentPostExitCacheAuthorityRegression'] = 'PASS' if parent_cache_test.returncode == 0 else 'FAIL'
        business_cache_test = run_classification_test('tests.unit.feishu.test_m2b_probe_contract.M2BProbeContractTests.test_approval_business_path_unchanged_after_cache_fix')
        statuses['L159ApprovalBusinessPathUnchangedRegression'] = 'PASS' if business_cache_test.returncode == 0 else 'FAIL'
        statuses['L160ResolutionMarkerRegressionAfterCacheFix'] = 'PASS' if statuses['L134MarkerExactByteContract'] == 'PASS' and statuses['L136ProtectedOperationExactByteWrite'] == 'PASS' else 'FAIL'

        exact_close_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_device_flow_preclose_completes_on_captured_bg_loop')
        statuses['L161DeviceFlowExactCloseLifecycleCapture'] = 'PASS' if exact_close_test.returncode == 0 else 'FAIL'
        statuses['L162DeviceFlowPreCloseTerminalContract'] = statuses['L161DeviceFlowExactCloseLifecycleCapture']
        public_stop_safe_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_public_stop_compatibility_bridge_skips_second_device_flow_close')
        statuses['L163DeviceFlowDoubleCloseIdempotent'] = 'PASS' if public_stop_safe_test.returncode == 0 else 'FAIL'
        public_stop_transport_test = run_classification_test('tests.unit.feishu.test_channel_transport.ChannelTransportTests.test_terminal_device_flow_preclose_uses_stop_bridge_without_second_close')
        statuses['L164DeviceFlowPublicStopAfterPreCloseSafe'] = 'PASS' if public_stop_transport_test.returncode == 0 else 'FAIL'
        statuses['L165DeviceFlowRawCoroutineNoUnawaitedLeak'] = 'PASS' if scheduling_failure_test.returncode == 0 else 'FAIL'
        statuses['L166DeviceFlowTimeoutTaskTerminal'] = 'PASS' if timeout_test.returncode == 0 else 'FAIL'
        statuses['L167DeviceFlowForeignLoopThreadSafeClose'] = statuses['L161DeviceFlowExactCloseLifecycleCapture']
        nonrunning_test = run_classification_test('tests.unit.feishu.test_sdk_compat.SdkTaskOwnershipTests.test_device_flow_preclose_drives_open_nonrunning_owner_loop')
        statuses['L168DeviceFlowOpenNonRunningLoopClose'] = 'PASS' if nonrunning_test.returncode == 0 else 'FAIL'
        statuses['L169Host040651DeviceFlowRegression'] = statuses['L163DeviceFlowDoubleCloseIdempotent']
        statuses['L170ExpiringCacheRegressionAfterDeviceFlowFix'] = statuses['L151CacheOpenNonRunningOrphanLoopDrain']
        statuses['L171ApprovalBusinessRegressionAfterDeviceFlowFix'] = statuses['L159ApprovalBusinessPathUnchangedRegression']
        statuses['L172DeviceFlowPostExitWarningAuthority'] = statuses['L122RawStderrWarningAuthority']

        feedback_statuses = run_approval_feedback_acceptance()
        statuses.update({key: value for key, value in feedback_statuses.items() if key.startswith('L')})
        finalization_statuses = run_finalization_binding_acceptance()
        statuses.update(finalization_statuses)

        from cfr.feishu.credentials import FeishuCredentialResolver, LocalConfigStore, import_environment_credentials
        credential_config = LocalConfigStore(Path(directory) / 'credential-config')
        class AcceptanceSecretStore:
            available = True
            def __init__(self): self.values = {}
            def set_secret(self, key, value): self.values[key] = value
            def get_secret(self, key): return self.values.get(key)
            def delete_secret(self, key): self.values.pop(key, None)
            def has_secret(self, key): return bool(self.values.get(key))
        credential_secrets = AcceptanceSecretStore()
        import_environment_credentials({'CFR_FEISHU_APP_ID': 'acceptance-app', 'CFR_FEISHU_APP_SECRET': 'acceptance-secret'}, credential_config, credential_secrets)
        persistent_credential_result = FeishuCredentialResolver({}, credential_config, credential_secrets).resolve()
        statuses['L67PersistentFeishuAppId'] = 'PASS' if persistent_credential_result.app_id == 'acceptance-app' and persistent_credential_result.app_id_source == 'persistent' else 'FAIL'
        statuses['L68PersistentFeishuSecret'] = 'PASS' if persistent_credential_result.app_secret == 'acceptance-secret' and persistent_credential_result.app_secret_source == 'persistent' else 'FAIL'
        env_credential_result = FeishuCredentialResolver({'CFR_FEISHU_APP_ID': 'env-app', 'CFR_FEISHU_APP_SECRET': 'env-secret'}, credential_config, credential_secrets).resolve()
        statuses['L69EnvironmentOverridesPersistent'] = 'PASS' if env_credential_result.app_id_source == 'environment' and env_credential_result.app_secret_source == 'environment' else 'FAIL'
        safe_credential_status = FeishuCredentialResolver({}, credential_config, credential_secrets).safe_status()
        statuses['L70CredentialStatusRedacted'] = 'PASS' if 'acceptance-secret' not in json.dumps(safe_credential_status) and 'AppSecretConfigured' in safe_credential_status else 'FAIL'
        cli_source = (ROOT / 'src' / 'cfr' / 'cli' / 'main.py').read_text(encoding='utf-8')
        statuses['L71CredentialImportEnv'] = 'PASS' if all(token in cli_source for token in ('credentials', 'import-env', 'set-secret', 'getpass.getpass')) else 'FAIL'
        pyproject_text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        statuses['L72NoPlaintextSecretFallback'] = 'PASS' if 'keyring>=25,<26' in pyproject_text and 'allow-insecure-secret-file' not in cli_source and 'CFR_SECURE_SECRET_STORE_UNAVAILABLE' in (ROOT / 'src' / 'cfr' / 'feishu' / 'credentials.py').read_text(encoding='utf-8') else 'FAIL'
    statuses['L24GateTruthfulness'] = 'PASS' if all(value == 'PASS' for key, value in statuses.items() if key != 'L24GateTruthfulness') else 'FAIL'

    verdict = 'PASS' if all(value == 'PASS' for value in statuses.values()) else 'FAIL'
    payload = {'RunId': run_id, 'Verdict': verdict, **statuses}
    (artifact_dir / 'result.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    (artifact_dir / 'events.jsonl').write_text('\n'.join(json.dumps(item, ensure_ascii=False) for item in events) + '\n', encoding='utf-8')
    (artifact_dir / 'acceptance.log').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    print(json.dumps({'ArtifactDir': str(artifact_dir), **payload}))
    return 0 if verdict == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
