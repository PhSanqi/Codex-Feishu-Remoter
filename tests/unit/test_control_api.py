from http.cookiejar import CookieJar
from pathlib import Path
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from urllib.request import HTTPCookieProcessor, Request, build_opener
from urllib.error import HTTPError
from urllib.parse import quote

from cfr.control.api import LocalControlServer, MAX_CONTROL_REQUEST_BYTES
from cfr.control.supervisor import CfrSupervisor, ControlCommandResult
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
    def __init__(self, settings, transport):
        self.settings = settings
        self.transport = transport
        self.store = object()
        self._started = False

    def start(self, **_kwargs):
        self._started = True

    def stop(self):
        self._started = False
        self.transport.is_running = False

    def update_workspace_roots(self, roots):
        self.settings.allowed_workspace_roots = tuple(Path(root).resolve() for root in roots)
        return self.settings


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
        operational = self._request('/api/v1/operational')['current_state']
        self.assertEqual(set(operational), {'feishu', 'sessions', 'bindings', 'jobs', 'surfaces', 'activity', 'storage'})
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

    def test_chat_surface_selection_explicitly_starts_browser_bridge(self):
        class ChatAdapter:
            def __init__(self):
                self.started = 0

            def health_snapshot(self):
                return {
                    'available': self.started > 0,
                    'status': 'ready' if self.started else 'not_connected',
                    'description': 'test chat',
                }

            def start(self):
                self.started += 1
                return self.health_snapshot()

            def close(self):
                pass

        chat = ChatAdapter()
        self.supervisor._chat_adapter = chat
        FeishuStore(self.supervisor.database).set_selected_surface('chat-1', 'code')
        result = self._request('/api/v1/surfaces/select', {'surface': 'chat'})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(chat.started, 1)
        self.assertEqual(FeishuStore(self.supervisor.database).get_selected_surface('chat-1'), 'chat')

    def test_feishu_poll_uses_lightweight_projection_not_full_status_build(self):
        with patch.object(self.server.read_model, 'build', side_effect=AssertionError('full status build')):
            response = self._request('/api/v1/feishu')
        self.assertEqual(response['status'], 'ok')
        self.assertEqual(response['current_state']['state'], 'stopped')

    def test_control_api_rejects_oversized_request_before_reading_json_body(self):
        csrf = next(cookie.value for cookie in self.jar if cookie.name == 'cfr_control_csrf')
        request = Request(
            f'{self.server.url}/api/v1/runtime/remote-execution',
            data=b'{' + b' ' * MAX_CONTROL_REQUEST_BYTES,
            headers={'X-CFR-CSRF': csrf, 'Content-Type': 'application/json'},
            method='POST',
        )
        with self.assertRaises(HTTPError) as raised:
            self.opener.open(request)
        self.assertEqual(raised.exception.code, 400)
        payload = json.loads(raised.exception.read())
        self.assertEqual(payload['error_code'], 'CONTROL_INVALID_REQUEST')
        raised.exception.close()

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
        self.assertEqual(launch.call_args_list, [call('thread/registered', launcher='auto'), call('thread/registered', launcher='auto')])
        self.assertEqual(bindings.get_binding('thread/registered'), before)

    def test_setup_preferences_persist_browser_and_codexhost_launcher(self):
        result = self._request('/api/v1/setup/preferences', {
            'default_surface': 'chat',
            'network_mode': 'direct',
            'proxy_url': None,
            'chat_browser_backend': 'embedded',
            'codex_desktop_launcher': 'codexhost',
        })
        self.assertEqual(result['status'], 'ok')
        config = self.supervisor._config_store
        self.assertEqual(config.get_chat_browser_backend(), 'embedded')
        self.assertEqual(config.get_codex_desktop_launcher(), 'codexhost')

    def test_show_embedded_chat_endpoint_only_reveals_login_flow(self):
        host = SimpleNamespace(available=True, show=Mock())
        self.supervisor.attach_embedded_chat_host(host)
        hidden = self._request('/api/v1/setup/chat/show', method='POST')
        self.assertEqual(hidden['error_code'], 'CONTROL_CHAT_AUTOMATION_HIDDEN')
        self.supervisor._chat_adapter = SimpleNamespace(health_snapshot=lambda: {'status': 'waiting_user'})
        result = self._request('/api/v1/setup/chat/show', method='POST')
        self.assertEqual(result['status'], 'ok')
        host.show.assert_called_once_with()

    def test_manual_chat_login_endpoints_are_exposed(self):
        started = ControlCommandResult('ok', None, 'manual started', {})
        verified = ControlCommandResult('ok', None, 'manual verified', {})
        with patch.object(self.supervisor, 'start_manual_chat_login', return_value=started) as start, patch.object(
            self.supervisor, 'finish_manual_chat_login', return_value=verified
        ) as verify:
            start_result = self._request('/api/v1/setup/chat/manual-login/start', method='POST')
            verify_result = self._request('/api/v1/setup/chat/manual-login/verify', method='POST')
        self.assertEqual(start_result['message'], 'manual started')
        self.assertEqual(verify_result['message'], 'manual verified')
        start.assert_called_once_with()
        verify.assert_called_once_with()

    def test_open_desktop_restart_is_explicit_and_uses_scoped_restart_handoff(self):
        bindings = BindingStore(self.supervisor.database)
        bindings.upsert_binding(ThreadRef('thread-restart', 'native thread', Path(self.directory.name)))
        path = '/api/v1/threads/thread-restart/open-desktop'
        with patch('cfr.control.api.restart_codex_desktop_thread', return_value='codex://threads/thread-restart') as restart, patch('cfr.control.api.open_codex_desktop_thread') as launch:
            response = self._request(path, {'restart': True})
        self.assertEqual(response['status'], 'ok')
        restart.assert_called_once_with('thread-restart', launcher='auto')
        launch.assert_not_called()

    def test_desktop_restart_is_blocked_by_other_busy_cfr_binding_but_plain_open_is_not(self):
        bindings = BindingStore(self.supervisor.database)
        bindings.upsert_binding(ThreadRef('thread-target', 'target', Path(self.directory.name)))
        bindings.upsert_binding(ThreadRef('thread-other', 'other', Path(self.directory.name)))
        bindings.set_writer_state('thread-other', 'external_active')
        path = '/api/v1/threads/thread-target/open-desktop'
        with patch('cfr.control.api.restart_codex_desktop_thread') as restart, patch(
            'cfr.control.api.open_codex_desktop_thread', return_value='codex://threads/thread-target'
        ) as launch:
            blocked = self._request(path, {'restart': True})
            opened = self._request(path, method='POST')
        self.assertEqual(blocked['error_code'], 'CONTROL_DESKTOP_RESTART_BUSY')
        self.assertEqual(opened['status'], 'ok')
        restart.assert_not_called()
        launch.assert_called_once_with('thread-target', launcher='auto')

    def test_desktop_restart_is_blocked_by_live_registry_state_not_yet_persisted(self):
        bindings = BindingStore(self.supervisor.database)
        bindings.upsert_binding(ThreadRef('thread-target-live', 'target', Path(self.directory.name)))
        adapter = CodexAdapter(store=bindings)
        self.supervisor._daemon = SimpleNamespace(adapter=adapter)
        adapter.registry.register(ActiveTurn('thread-other-live', 'turn-live', object()))
        with patch('cfr.control.api.restart_codex_desktop_thread') as restart:
            blocked = self._request('/api/v1/threads/thread-target-live/open-desktop', {'restart': True})
        self.assertEqual(blocked['error_code'], 'CONTROL_DESKTOP_RESTART_BUSY')
        restart.assert_not_called()

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

    def test_control_center_source_uses_one_serial_operational_poll(self):
        source = (ROOT / 'm3_control' / 'src' / 'main.tsx').read_text(encoding='utf-8')
        self.assertIn("api<{ current_state: Operational }>('/api/v1/operational')", source)
        self.assertIn('function pollSerially', source)
        self.assertIn('onError?: (error: unknown) => void', source)
        self.assertIn('window.setTimeout(run, intervalMs)', source)
        self.assertIn('model?.feishu.running ? 1200 : 3000', source)
        self.assertIn('控制 API 连接中断；控制中心正在自动重试。', source)
        self.assertIn('控制 API 已恢复连接。', source)
        self.assertNotIn('setInterval(refreshOperationalState', source)
        self.assertIn('当前飞书绑定', source)
        self.assertIn('可用 CFR 会话 / 持久化 Codex 线程', source)
        operational_poll = source.split('const refreshOperationalState = ', 1)[1].split('return pollSerially', 1)[0]
        self.assertNotIn('/api/v1/sessions', operational_poll)
        self.assertNotIn('/api/v1/jobs', operational_poll)
        self.assertNotIn('/api/v1/bindings', operational_poll)
        self.assertNotIn('/api/v1/surfaces', operational_poll)
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
        self.assertIn('CFR Runtime', source)
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

    def test_models_capabilities_and_surfaces_api_require_control_auth(self):
        for path in ('/api/v1/models', '/api/v1/capabilities', '/api/v1/surfaces'):
            with self.assertRaises(HTTPError) as raised:
                build_opener().open(f'{self.server.url}{path}')
            self.assertEqual(raised.exception.code, 403)
            raised.exception.close()

    def test_models_and_capabilities_api_return_read_model_envelopes(self):
        self.server.read_model.models = lambda: {'available': True, 'error_code': None, 'message': 'catalog', 'data': [{'id': 'runtime'}]}
        self.server.read_model.capabilities = lambda: {'context': 'default', 'permission_profiles': {'available': True, 'data': []}, 'experimental_features': {'available': True, 'data': []}}
        self.assertEqual(self._request('/api/v1/models')['current_state']['data'][0]['id'], 'runtime')
        self.assertEqual(self._request('/api/v1/capabilities')['current_state']['context'], 'default')

    def test_surfaces_api_defaults_to_code_without_remote_chat_state(self):
        response = self._request('/api/v1/surfaces')['current_state']
        self.assertEqual(response['selected'], 'code')
        states = {item['id']: item['available'] for item in response['data']}
        self.assertEqual(states, {'chat': False, 'work': False, 'code': True})

    def test_surfaces_api_reads_and_writes_most_recent_feishu_chat_selection(self):
        class ChatAdapter:
            def health(self):
                return {'available': True, 'status': 'ready', 'description': 'test chat'}

        self.supervisor._chat_adapter = ChatAdapter()
        store = FeishuStore(self.supervisor.database)
        store.enqueue_message(FeishuInboundMessage(
            event_id='event-surface', message_id='surface-message', chat_id='chat-surface', chat_type='p2p',
            sender_open_id='user-1', sender_type='user', message_type='text', text='/surface chat',
        ))
        store.set_selected_surface('chat-surface', 'chat')
        self.assertEqual(self._request('/api/v1/surfaces')['current_state']['selected'], 'chat')
        response = self._request('/api/v1/surfaces/select', {'surface': 'code'})
        self.assertEqual(response['status'], 'ok')
        self.assertEqual(response['current_state']['selected'], 'code')
        self.assertEqual(store.get_selected_surface('chat-surface'), 'code')

    def test_surface_selection_rejects_unavailable_work_surface(self):
        store = FeishuStore(self.supervisor.database)
        store.enqueue_message(FeishuInboundMessage(
            event_id='event-surface', message_id='surface-message', chat_id='chat-surface', chat_type='p2p',
            sender_open_id='user-1', sender_type='user', message_type='text', text='hello',
        ))
        with self.assertRaises(HTTPError) as raised:
            self._request('/api/v1/surfaces/select', {'surface': 'work'})
        self.assertEqual(raised.exception.code, 409)
        raised.exception.close()

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
        self.assertEqual(response['current_state'][0]['approval_mode'], 'ask')
        self.assertNotIn('owner_open_id', response['current_state'][0])
        self.assertNotIn('owner-secret', json.dumps(response))

    def test_session_approval_mode_api_persists_native_presets(self):
        store = FeishuStore(self.supervisor.database)
        store.create_pending_session('chat/1', 'p2p', 'owner-1', 'C:/workspace')
        path = f'/api/v1/sessions/{quote("chat/1", safe="")}/approval-mode'
        for mode in ('ask', 'auto', 'full'):
            response = self._request(path, {'mode': mode})
            self.assertEqual(response['status'], 'ok')
            self.assertEqual(store.get_approval_mode('chat/1'), mode)
            session = next(item for item in response['current_state'] if item['chat_id'] == 'chat/1')
            self.assertEqual(session['approval_mode'], mode)

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

    def test_active_session_cannot_be_unbound_or_switch_surface_mid_turn(self):
        store = FeishuStore(self.supervisor.database)
        store.create_pending_session('chat-1', 'p2p', 'owner-1', 'C:/workspace')
        store.bind_session('chat-1', 'thread-1')
        bindings = BindingStore(self.supervisor.database)
        bindings.upsert_binding(ThreadRef('thread-1', 'native thread', Path('C:/workspace')))
        registry = ActiveTurnRegistry()
        telemetry = registry.begin('message-private', thread_id='thread-1', received_at=1, queued_at=1, execution_started_at=1)
        telemetry.set_identity(turn_id='turn-1')
        telemetry.set_stage('running', status='running')
        registry.register(ActiveTurn('thread-1', 'turn-1', object(), telemetry=telemetry))
        self.supervisor._daemon = SimpleNamespace(adapter=SimpleNamespace(registry=registry))

        unbind = self._request('/api/v1/sessions/chat-1/unbind', {})
        self.assertEqual(unbind['error_code'], 'CONTROL_SESSION_ACTIVE')
        self.assertIsNotNone(store.get_session('chat-1'))

        with self.assertRaises(HTTPError) as raised:
            self._request('/api/v1/surfaces/select', {'surface': 'chat', 'chat_id': 'chat-1'})
        payload = json.loads(raised.exception.read())
        self.assertEqual(payload['error_code'], 'CONTROL_SURFACE_CHANGE_BUSY')
        raised.exception.close()

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

    def test_workspace_changes_hot_reload_while_running_and_fail_closed_while_pairing(self):
        self.assertEqual(self._request('/api/v1/feishu/start', {})['status'], 'ok')
        running = self._request('/api/v1/feishu/workspaces/add', {'workspace_root': self.directory.name})
        self.assertEqual(running['status'], 'ok')
        self.assertEqual(self.supervisor._daemon.settings.allowed_workspace_roots, (Path(self.directory.name).resolve(),))
        removed = self._request('/api/v1/feishu/workspaces/remove', {'workspace_root': self.directory.name})
        self.assertEqual(removed['error_code'], 'CONTROL_FEISHU_STOP_REQUIRED_FOR_WORKSPACE_CHANGE')
        self.assertEqual(self._request('/api/v1/feishu/stop', {})['status'], 'ok')
        self.assertEqual(self._request('/api/v1/feishu/pairing/start', {})['status'], 'ok')
        pairing = self._request('/api/v1/feishu/workspaces/add', {'workspace_root': self.directory.name})
        self.assertEqual(pairing['error_code'], 'CONTROL_FEISHU_PAIRING_ACTIVE')
