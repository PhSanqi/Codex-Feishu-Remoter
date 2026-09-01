from http.cookiejar import CookieJar
from pathlib import Path
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import call, patch
from urllib.request import HTTPCookieProcessor, Request, build_opener
from urllib.error import HTTPError
from urllib.parse import quote

from cfr.control.api import LocalControlServer
from cfr.control.supervisor import CfrSupervisor
from cfr.core.models import ActiveTurn, ThreadRef, TurnResult
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.store import FeishuStore
from cfr.feishu.credentials import LocalConfigStore
from cfr.storage.db import BindingStore
from cfr.codex.turns import ActiveTurnRegistry
from cfr.codex.binding import CodexAdapter


ROOT = Path(__file__).resolve().parents[2]


class _Settings:
    def __init__(self, database):
        self.database = database
        self.app_namespace = 'api-pairing-test'
        self.credentials_present = True
        self.allowed_workspace_roots = ()
    app_id_source = 'missing'
    app_secret = 'configured-secret'
    app_secret_source = 'missing'
    allowed_open_ids = {'user-1'}
    enable_group_chats = False

    def validate_connection(self):
        return None

    def validate_execution(self):
        return None


class _Transport:
    def __init__(self, _settings):
        self.is_running = False
        self.connection_state = 'stopped'
        self.message_handler = None
        self.card_handler = None

    def connect_until_ready(self, message_handler, card_handler=None, **_kwargs):
        self.message_handler = message_handler
        self.card_handler = card_handler
        self.is_running = True
        self.connection_state = 'ready'

    def stop(self):
        self.is_running = False
        self.connection_state = 'stopped'


class _Daemon:
    def __init__(self, _settings, transport):
        self.transport = transport
        self.store = object()
        self._started = False

    def start(self, **_kwargs):
        self._started = True

    def stop(self):
        self._started = False
        self.transport.is_running = False


class _Gateway:
    def __init__(self, settings, *_args):
        self.settings = settings
        self.messages = []
        self.handle_message_event = lambda message, event_id=None: self.messages.append((message, event_id)) or {'status': 'queued'}
        self.handle_card_action = lambda *_args: None


class LocalControlApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        web_root = ROOT / 'm3_control' / 'dist'
        if not (web_root / 'index.html').is_file():
            self.skipTest('build the M3 Control Center before the localhost smoke')
        database = Path(self.directory.name) / 'cfr.sqlite3'
        self.settings = _Settings(database)
        self.supervisor = CfrSupervisor(
            database=database,
            settings_loader=lambda **_kwargs: self.settings,
            transport_factory=_Transport,
            daemon_factory=_Daemon,
            gateway_factory=_Gateway,
            doctor_runner=lambda **_kwargs: {'Verdict': 'PASS'},
            config_store=LocalConfigStore(Path(self.directory.name) / 'config'),
            pairing_code_factory=lambda: '583271',
        )
        self.server = LocalControlServer(self.supervisor, port=0, web_root=web_root).start()
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.index = self.opener.open(self.server.bootstrap_url).read().decode('utf-8')

    def tearDown(self):
        self.server.stop()
        self.directory.cleanup()

    def _request(self, path, body=None, method=None):
        headers = {}
        request_method = method or ('POST' if body is not None else 'GET')
        if request_method != 'GET':
            csrf = next(cookie.value for cookie in self.jar if cookie.name == 'cfr_control_csrf')
            headers['X-CFR-CSRF'] = csrf
            headers['Content-Type'] = 'application/json'
        request = Request(f'{self.server.url}{path}', data=json.dumps(body).encode('utf-8') if body is not None else None, headers=headers, method=request_method)
        return json.loads(self.opener.open(request).read())

    def test_local_api_reads_state_and_controls_real_supervisor(self):
        self.assertIn('<div id="root">', self.index)
        self.assertEqual(self._request('/api/v1/status')['status'], 'ok')
        self.assertEqual(self._request('/api/v1/feishu/start', {})['status'], 'ok')
        self.assertEqual(self._request('/api/v1/feishu/stop', {})['status'], 'ok')
        self.assertEqual(self._request('/api/v1/feishu/reconnect', {})['status'], 'ok')
        self.assertEqual(self._request('/api/v1/doctor', {})['status'], 'ok')

    def test_feishu_activity_api_requires_auth_and_reads_only_bounded_activity(self):
        with self.assertRaises(HTTPError) as raised:
            build_opener().open(f'{self.server.url}/api/v1/feishu/activity')
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        self.supervisor._record_activity('FEISHU_STARTING', '正在启动飞书运行时。', stage='settings_load')
        response = self._request('/api/v1/feishu/activity')
        self.assertEqual((response['status'], response['message']), ('ok', 'Feishu activity'))
        self.assertEqual(response['current_state'][-1]['event'], 'FEISHU_STARTING')
        self.assertIsNone(self.supervisor._daemon)

    def test_localhost_smoke_applies_admission_policy_and_lifecycle(self):
        self.assertEqual(self._request('/api/v1/status')['status'], 'ok')
        self.assertEqual(self._request('/api/v1/feishu/start', {})['status'], 'ok')
        self.assertEqual(self._request('/api/v1/runtime/drain', {})['status'], 'ok')
        message = FeishuInboundMessage(
            event_id='event-1', message_id='smoke-1', chat_id='chat-1', chat_type='p2p',
            sender_open_id='user-1', sender_type='user', message_type='text', text='new work',
        )
        rejected = self.supervisor._transport.message_handler(message, 'event-1')
        self.assertEqual(rejected['reason'], 'CONTROL_NOT_ACCEPTING_NEW_TASKS')
        self.assertEqual(self._request('/api/v1/runtime/accept-new-work', {'enabled': True})['status'], 'ok')
        self.assertEqual(self._request('/api/v1/feishu/stop', {})['status'], 'ok')
        self.assertEqual(self._request('/api/v1/feishu/reconnect', {})['status'], 'ok')
        self.assertEqual(self._request('/api/v1/doctor', {})['status'], 'ok')

    def test_drain_is_idempotent_and_does_not_start_another_runtime(self):
        self.assertEqual(self._request('/api/v1/feishu/start', {})['status'], 'ok')
        daemon = self.supervisor._daemon
        first = self._request('/api/v1/runtime/drain', {})
        second = self._request('/api/v1/runtime/drain', {})
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(second['status'], 'ok')
        self.assertFalse(second['current_state']['accept_new_tasks'])
        self.assertIs(self.supervisor._daemon, daemon)

    def test_sessions_api_requires_control_auth(self):
        with self.assertRaises(HTTPError) as raised:
            build_opener().open(f'{self.server.url}/api/v1/sessions')
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_bindings_api_requires_control_auth_and_returns_sanitized_durable_records(self):
        with self.assertRaises(HTTPError) as raised:
            build_opener().open(f'{self.server.url}/api/v1/bindings')
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        bindings = BindingStore(self.supervisor.database)
        bindings.upsert_binding(ThreadRef('thread-1', 'native thread', Path(self.directory.name)))
        response = self._request('/api/v1/bindings')
        self.assertEqual(response['current_state'][0]['thread_id'], 'thread-1')
        self.assertNotIn('rollout_path', response['current_state'][0])
        self.assertIsNotNone(bindings.get_binding('thread-1'))

    def test_open_desktop_requires_auth_and_existing_idle_binding(self):
        path = '/api/v1/threads/thread-1/open-desktop'
        with self.assertRaises(HTTPError) as raised:
            build_opener().open(Request(f'{self.server.url}{path}', data=b'', method='POST'))
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

        with patch('cfr.control.api.open_codex_desktop_thread') as launch:
            missing = self._request(path, method='POST')
        self.assertEqual(missing['error_code'], 'CONTROL_THREAD_NOT_FOUND')
        launch.assert_not_called()

    def test_open_desktop_uses_registered_id_without_mutating_binding(self):
        bindings = BindingStore(self.supervisor.database)
        bindings.upsert_binding(ThreadRef('thread/registered', 'native thread', Path(self.directory.name)))
        before = bindings.get_binding('thread/registered')
        path = f'/api/v1/threads/{quote("thread/registered", safe="")}/open-desktop'
        with patch('cfr.control.api.open_codex_desktop_thread', return_value='codex://threads/thread/registered') as launch:
            first = self._request(path, {'uri': 'codex://threads/new?path=C:/wrong'})
            second = self._request(path, method='POST')
        self.assertEqual((first['status'], second['status']), ('ok', 'ok'))
        self.assertEqual(launch.call_args_list, [call('thread/registered'), call('thread/registered')])
        self.assertEqual(bindings.get_binding('thread/registered'), before)

    def test_open_desktop_rejects_real_live_registry_and_writer_when_binding_is_idle(self):
        bindings = BindingStore(self.supervisor.database)
        bindings.upsert_binding(ThreadRef('thread-live', None, Path(self.directory.name)))
        adapter = CodexAdapter(store=bindings)
        self.supervisor._daemon = SimpleNamespace(adapter=adapter)
        adapter.registry.register(ActiveTurn('thread-live', 'turn-live', object()))
        with patch('cfr.control.api.open_codex_desktop_thread') as launch:
            active = self._request('/api/v1/threads/thread-live/open-desktop', method='POST')
            adapter.registry.finish(TurnResult('thread-live', 'turn-live', 'completed'))
            adapter.leases.acquire('thread-live')
            busy = self._request('/api/v1/threads/thread-live/open-desktop', method='POST')
        self.assertEqual(active['error_code'], 'CONTROL_THREAD_ACTIVE')
        self.assertEqual(busy['error_code'], 'CONTROL_THREAD_WRITER_BUSY')
        self.assertEqual((bindings.get_binding('thread-live').active_turn_id, bindings.get_binding('thread-live').writer_state), (None, 'idle'))
        launch.assert_not_called()

    def test_open_desktop_rejects_active_and_busy_bindings(self):
        bindings = BindingStore(self.supervisor.database)
        bindings.upsert_binding(ThreadRef('thread-active', None, Path(self.directory.name)))
        bindings.set_active_turn('thread-active', 'turn-1')
        bindings.upsert_binding(ThreadRef('thread-busy', None, Path(self.directory.name)))
        bindings.set_writer_state('thread-busy', 'external_active')
        with patch('cfr.control.api.open_codex_desktop_thread') as launch:
            active = self._request('/api/v1/threads/thread-active/open-desktop', method='POST')
            busy = self._request('/api/v1/threads/thread-busy/open-desktop', method='POST')
        self.assertEqual(active['error_code'], 'CONTROL_THREAD_ACTIVE')
        self.assertEqual(busy['error_code'], 'CONTROL_THREAD_WRITER_BUSY')
        launch.assert_not_called()

    def test_open_desktop_maps_platform_failures(self):
        bindings = BindingStore(self.supervisor.database)
        bindings.upsert_binding(ThreadRef('thread-1', None, Path(self.directory.name)))
        path = '/api/v1/threads/thread-1/open-desktop'
        with patch('cfr.control.api.open_codex_desktop_thread', side_effect=NotImplementedError):
            unsupported = self._request(path, method='POST')
        with patch('cfr.control.api.open_codex_desktop_thread', side_effect=RuntimeError('failed')):
            failed = self._request(path, method='POST')
        self.assertEqual(unsupported['error_code'], 'CODEX_DESKTOP_OPEN_UNSUPPORTED')
        self.assertEqual(failed['error_code'], 'CODEX_DESKTOP_OPEN_FAILED')

    def test_control_center_source_polls_jobs_and_sessions_only_while_feishu_runs(self):
        source = (ROOT / 'm3_control' / 'src' / 'main.tsx').read_text(encoding='utf-8')
        self.assertIn("if (!model?.feishu.running) return", source)
        self.assertIn("api<{ current_state: Session[] }>('/api/v1/sessions')", source)
        self.assertIn("api<{ current_state: Job[] }>('/api/v1/jobs')", source)
        self.assertIn('setInterval(refreshOperationalState, 1200)', source)
        self.assertIn('当前飞书绑定', source)
        self.assertIn('可用 CFR 会话', source)
        operational_poll = source.split('const refreshOperationalState = ', 1)[1].split('const timer', 1)[0]
        self.assertNotIn('/api/v1/models', operational_poll)
        self.assertNotIn('/api/v1/settings', operational_poll)
        self.assertNotIn('/api/v1/capabilities', operational_poll)
        self.assertIn('在 Codex Desktop 中打开', source)
        self.assertIn('复制完整 Thread ID', source)
        self.assertIn('encodeURIComponent(threadId)', source)
        self.assertNotIn('codex://threads/', source)
        self.assertIn("if (result.status !== 'ok')", source)
        desktop_action = source.split('const openDesktop = ', 1)[1].split('const copyPairingMessage', 1)[0]
        self.assertNotIn('refresh()', desktop_action)
        self.assertIn("jobs.some((job) => job.active && job.thread_id === binding.thread_id)", source)
        self.assertIn('Codex Runtime', source)
        self.assertIn('Runtime breakdown', source)
        self.assertIn('Tokens and context', source)
        self.assertIn('Tool activity', source)
        self.assertIn('Activity timeline', source)
        self.assertIn("value == null ? '—'", source)
        self.assertIn('runtime.timeline.slice(-20)', source)
        self.assertIn('last_native_activity_age_ms', source)
        self.assertIn('最近 Codex 活动', source)
        self.assertIn("if (value < 1000) return '刚刚'", source)
        self.assertIn('cache_write_input_tokens', source)
        self.assertIn('Cache write', source)
        self.assertIn('cfr_controlled_overhead_ms', source)
        self.assertIn('CFR 开销', source)
        styles = (ROOT / 'm3_control' / 'src' / 'styles.css').read_text(encoding='utf-8')
        self.assertIn('minmax(170px, .8fr)', styles)
        self.assertIn('overflow-wrap: anywhere', styles)

    def test_approvals_api_requires_control_auth(self):
        with self.assertRaises(HTTPError) as raised:
            build_opener().open(f'{self.server.url}/api/v1/approvals')
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_approvals_api_returns_read_model(self):
        self.server.read_model.approvals = lambda: [{'approval_id': 'approval-1', 'decision': 'accept', 'feedback_state': 'EXECUTION_FAILED'}]
        response = self._request('/api/v1/approvals')
        self.assertEqual(response['status'], 'ok')
        self.assertEqual(response['current_state'][0]['feedback_state'], 'EXECUTION_FAILED')

    def test_models_and_capabilities_api_require_control_auth(self):
        for path in ('/api/v1/models', '/api/v1/capabilities'):
            with self.assertRaises(HTTPError) as raised:
                build_opener().open(f'{self.server.url}{path}')
            self.assertEqual(raised.exception.code, 403)
            raised.exception.close()

    def test_models_and_capabilities_api_return_read_model_envelopes(self):
        self.server.read_model.models = lambda: {'available': True, 'error_code': None, 'message': 'catalog', 'data': [{'id': 'runtime'}]}
        self.server.read_model.capabilities = lambda: {'context': 'default', 'permission_profiles': {'available': True, 'data': []}, 'experimental_features': {'available': True, 'data': []}}
        self.assertEqual(self._request('/api/v1/models')['current_state']['data'][0]['id'], 'runtime')
        self.assertEqual(self._request('/api/v1/capabilities')['current_state']['context'], 'default')

    def test_settings_read_and_model_defaults_write_use_control_auth(self):
        self.server.read_model.settings = lambda: {'available': True, 'codex_model_defaults': {'applies_to': 'new_threads'}}
        self.server.read_model.write_model_defaults = lambda values: {'status': 'ok', 'error_code': None, 'warning_code': None, 'message': 'saved', 'current_state': {'values': values}, 'overriding_source_type': None}
        self.assertTrue(self._request('/api/v1/settings')['current_state']['available'])
        response = self._request('/api/v1/settings/codex/model-defaults', {'model': None, 'reasoning_effort': None, 'service_tier': None}, method='PUT')
        self.assertEqual(response['status'], 'ok')
        with self.assertRaises(HTTPError) as raised:
            build_opener().open(Request(f'{self.server.url}/api/v1/settings/codex/model-defaults', data=b'{}', method='PUT'))
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        with self.assertRaises(HTTPError) as raised:
            self.opener.open(Request(f'{self.server.url}/api/v1/settings/codex/model-defaults', data=b'{}', method='PUT'))
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_sessions_api_returns_sanitized_read_model(self):
        FeishuStore(self.supervisor.database).create_pending_session('chat-1', 'p2p', 'owner-secret', 'C:/workspace')
        response = self._request('/api/v1/sessions')
        self.assertEqual(response['status'], 'ok')
        self.assertEqual(response['current_state'][0]['chat_id'], 'chat-1')
        self.assertNotIn('owner_open_id', response['current_state'][0])
        self.assertNotIn('owner-secret', json.dumps(response))

    def test_session_unbind_api_removes_session(self):
        store = FeishuStore(self.supervisor.database)
        store.create_pending_session('chat/1', 'p2p', 'owner-1', 'C:/workspace')
        response = self._request(f'/api/v1/sessions/{quote("chat/1", safe="")}/unbind', {})
        self.assertEqual(response['status'], 'ok')
        self.assertEqual(response['message'], 'Session unbound')
        self.assertEqual(self._request('/api/v1/sessions')['current_state'], [])

    def test_session_unbind_does_not_delete_codex_binding(self):
        store = FeishuStore(self.supervisor.database)
        store.create_pending_session('chat-1', 'p2p', 'owner-1', 'C:/workspace')
        store.bind_session('chat-1', 'thread-1')
        bindings = BindingStore(self.supervisor.database)
        bindings.upsert_binding(ThreadRef('thread-1', 'native thread', Path('C:/workspace')))
        self.assertEqual(self._request('/api/v1/sessions/chat-1/unbind', {})['status'], 'ok')
        self.assertIsNone(store.get_session('chat-1'))
        self.assertIsNotNone(bindings.get_binding('thread-1'))

    def test_session_unbind_is_idempotent(self):
        first = self._request('/api/v1/sessions/missing-chat/unbind', {})
        second = self._request('/api/v1/sessions/missing-chat/unbind', {})
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(second['status'], 'ok')

    def test_jobs_api_returns_existing_read_model_envelope(self):
        registry = ActiveTurnRegistry()
        telemetry = registry.begin('private-message-id', thread_id='thread-1', received_at=1, queued_at=1, execution_started_at=1)
        telemetry.set_identity(turn_id='turn-1')
        telemetry.set_stage('running', status='running')
        registry.register(ActiveTurn('thread-1', 'turn-1', object(), telemetry=telemetry))
        self.supervisor._daemon = SimpleNamespace(adapter=SimpleNamespace(registry=registry))
        response = self._request('/api/v1/jobs')
        self.assertEqual(response['status'], 'ok')
        self.assertEqual(response['error_code'], None)
        self.assertEqual(response['message'], 'Read model')
        self.assertEqual(response['current_state'][0]['turn_id'], 'turn-1')
        self.assertEqual(response['current_state'][0]['runtime']['stage'], 'running')
        self.assertNotIn('private-message-id', json.dumps(response))

    def test_pairing_mutations_require_control_auth_and_csrf(self):
        for path in ('/api/v1/feishu/pairing/start', '/api/v1/feishu/pairing/cancel', '/api/v1/feishu/pairing/confirm'):
            with self.assertRaises(HTTPError) as raised:
                build_opener().open(Request(f'{self.server.url}{path}', data=b'{}', method='POST'))
            self.assertEqual(raised.exception.code, 403)
            raised.exception.close()
        with self.assertRaises(HTTPError) as raised:
            self.opener.open(Request(f'{self.server.url}/api/v1/feishu/pairing/start', data=b'{}', method='POST'))
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_pairing_api_returns_waiting_state_and_never_exposes_raw_candidate(self):
        started = self._request('/api/v1/feishu/pairing/start', {})
        self.assertEqual(started['status'], 'ok')
        self.assertEqual(started['current_state']['feishu']['pairing']['state'], 'waiting')
        code = started['current_state']['feishu']['pairing']['code']
        message = FeishuInboundMessage(
            event_id='pair-event', message_id='pair-message', chat_id='chat-pair', chat_type='p2p',
            sender_open_id='ou-internal-user', sender_type='user', message_type='text', text=f'绑定 {code}',
        )
        self.supervisor._pairing_transport.message_handler(message)
        result = self._request('/api/v1/feishu')
        pairing = result['current_state']['pairing']
        self.assertEqual(pairing['state'], 'pending_confirmation')
        self.assertNotEqual(pairing['candidate'], 'ou-internal-user')
        self.assertNotIn('ou-internal-user', json.dumps(result))

    def test_pairing_confirm_accepts_workspace_only_and_uses_server_candidate(self):
        store = FeishuStore(self.supervisor.database)
        store.create_pending_session('existing-chat', 'p2p', 'owner-existing', self.directory.name)
        store.bind_session('existing-chat', 'thread-existing')
        self._request('/api/v1/feishu/pairing/start', {})
        self.supervisor._pairing_transport.message_handler(FeishuInboundMessage(
            event_id='pair-event', message_id='pair-message', chat_id='chat-pair', chat_type='p2p',
            sender_open_id='ou-paired-user', sender_type='user', message_type='text', text='绑定 583271',
        ))
        with self.assertRaises(HTTPError) as raised:
            self._request('/api/v1/feishu/pairing/confirm', {'workspace_root': self.directory.name, 'open_id': 'ou-attacker'})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()
        confirmed = self._request('/api/v1/feishu/pairing/confirm', {'workspace_root': self.directory.name})
        self.assertEqual(confirmed['status'], 'ok')
        self.assertEqual(self.supervisor._config_store.get_allowed_open_ids(), ('ou-paired-user',))
        preserved = store.get_session('existing-chat')
        self.assertEqual((preserved.state, preserved.thread_id), ('bound', 'thread-existing'))

    def test_workspace_api_requires_auth_and_returns_persistent_roots(self):
        with self.assertRaises(HTTPError) as raised:
            build_opener().open(Request(f'{self.server.url}/api/v1/feishu/workspaces/add', data=b'{}', method='POST'))
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        added = self._request('/api/v1/feishu/workspaces/add', {'workspace_root': self.directory.name})
        self.assertEqual(added['status'], 'ok')
        self.settings.allowed_workspace_roots = (Path(self.directory.name).resolve(),)
        self.assertEqual(self._request('/api/v1/feishu')['current_state']['workspace_roots'], [str(Path(self.directory.name).resolve())])
        self.assertEqual(self._request('/api/v1/feishu/workspaces/remove', {'workspace_root': self.directory.name})['status'], 'ok')
        self.assertEqual(self._request('/api/v1/feishu/workspaces/remove', {'workspace_root': self.directory.name})['status'], 'ok')

    def test_workspace_changes_fail_closed_while_running_or_pairing(self):
        self.assertEqual(self._request('/api/v1/feishu/start', {})['status'], 'ok')
        running = self._request('/api/v1/feishu/workspaces/add', {'workspace_root': self.directory.name})
        self.assertEqual(running['error_code'], 'CONTROL_FEISHU_STOP_REQUIRED_FOR_WORKSPACE_CHANGE')
        self.assertEqual(self._request('/api/v1/feishu/stop', {})['status'], 'ok')
        self.assertEqual(self._request('/api/v1/feishu/pairing/start', {})['status'], 'ok')
        pairing = self._request('/api/v1/feishu/workspaces/add', {'workspace_root': self.directory.name})
        self.assertEqual(pairing['error_code'], 'CONTROL_FEISHU_PAIRING_ACTIVE')
