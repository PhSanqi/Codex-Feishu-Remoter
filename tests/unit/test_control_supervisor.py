from types import SimpleNamespace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cfr.control.supervisor import CfrSupervisor
from cfr.core.models import StructuredError
from cfr.feishu.config import load_settings
from cfr.feishu.daemon import FeishuDaemon
from cfr.feishu.credentials import LocalConfigStore
from cfr.feishu.models import FeishuInboundMessage


class _Settings:
    def validate_execution(self):
        return None


class _Transport:
    def __init__(self, _settings):
        self.is_running = False
        self.connection_state = 'stopped'

    def connect_until_ready(self, *_args, **_kwargs):
        self.is_running = True
        self.connection_state = 'ready'


class _Daemon:
    def __init__(self, _settings, transport):
        self.transport = transport
        self.store = object()
        self._started = False
        self.stop_calls = 0

    def start(self, **_kwargs):
        self._started = True
        return self

    def stop(self):
        self.stop_calls += 1
        self._started = False
        self.transport.is_running = False
        self.transport.connection_state = 'stopped'


class _StopFailureDaemon(_Daemon):
    def stop(self):
        self.stop_calls += 1
        raise RuntimeError('stop failed')


class _SharedBrowserDaemon(FeishuDaemon):
    def __init__(self, _settings, transport):
        self.transport = transport
        self.store = object()
        self.chat_adapter = object()
        self._owns_chat_adapter = True
        self._started = False
        self.stop_calls = 0

    def start(self, **_kwargs):
        self._started = True
        return self

    def stop(self):
        self.stop_calls += 1
        self._started = False
        self.transport.is_running = False
        self.transport.connection_state = 'stopped'


class _ConnectFailureTransport(_Transport):
    def connect_until_ready(self, *_args, **_kwargs):
        raise RuntimeError('connect failed')


class _Gateway:
    def __init__(self, *_args):
        self.handle_message_event = lambda *_args: None
        self.handle_card_action = lambda *_args: None


class _PairingSettings:
    database = ':memory:'
    app_namespace = 'pairing-test'

    def __init__(self):
        self.connection_validated = False
        self.execution_validated = False

    def validate_connection(self):
        self.connection_validated = True

    def validate_execution(self):
        self.execution_validated = True


class _PairingTransport:
    def __init__(self, _settings):
        self.message_handler = None
        self.stop_calls = 0
        self.stopped = threading.Event()
        self.is_running = False

    def connect_until_ready(self, message_handler, *_args, **_kwargs):
        self.message_handler = message_handler
        self.is_running = True

    def stop(self):
        self.stop_calls += 1
        self.is_running = False
        self.stopped.set()


class _Lease:
    def __init__(self, *_args):
        self.release_calls = 0
        self.released = threading.Event()

    def acquire(self):
        return self

    def release(self):
        self.release_calls += 1
        self.released.set()


class SupervisorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.daemons = []

        def daemon_factory(*args):
            daemon = _Daemon(*args)
            self.daemons.append(daemon)
            return daemon

        self.supervisor = CfrSupervisor(
            settings_loader=lambda **_kwargs: _Settings(),
            transport_factory=_Transport,
            daemon_factory=daemon_factory,
            gateway_factory=_Gateway,
            doctor_runner=lambda **_kwargs: {'Verdict': 'PASS'},
        )

    def test_start_stop_and_reconnect_are_idempotent_and_single_owner(self):
        self.assertEqual(self.supervisor.start_feishu().status, 'ok')
        self.assertEqual(len(self.daemons), 1)
        self.assertEqual(self.supervisor.start_feishu().message, 'Feishu is already running')
        self.assertEqual(len(self.daemons), 1)
        self.assertEqual(self.supervisor.reconnect_feishu().status, 'ok')
        self.assertEqual(len(self.daemons), 2)
        self.assertEqual(self.daemons[0].stop_calls, 1)
        self.assertTrue(self.supervisor.current_state()['feishu']['running'])
        self.assertEqual(self.supervisor.stop_feishu().status, 'ok')
        self.assertEqual(self.supervisor.stop_feishu().message, 'Feishu is already stopped')

    def test_runtime_controls_and_doctor_return_current_state(self):
        self.assertFalse(self.supervisor.set_remote_execution(False).current_state['remote_execution_enabled'])
        self.assertFalse(self.supervisor.set_accept_new_tasks(False).current_state['accept_new_tasks'])
        result = self.supervisor.run_doctor()
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.current_state['doctor']['status'], 'PASS')

    def test_default_settings_loader_is_short_lived_cached_and_explicitly_invalidatable(self):
        supervisor = CfrSupervisor(settings_loader=load_settings)
        calls = []

        def loader(**_kwargs):
            calls.append(1)
            return _Settings()

        supervisor._settings_loader = loader
        self.assertIs(supervisor._load_settings(), supervisor._load_settings())
        self.assertEqual(len(calls), 1)
        supervisor._invalidate_settings()
        supervisor._load_settings()
        self.assertEqual(len(calls), 2)

    def test_stop_failure_retains_runtime_ownership_and_blocks_second_start(self):
        daemons = []

        def daemon_factory(*args):
            daemon = _StopFailureDaemon(*args)
            daemons.append(daemon)
            return daemon

        supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings(), transport_factory=_Transport, daemon_factory=daemon_factory, gateway_factory=_Gateway)
        self.assertEqual(supervisor.start_feishu().status, 'ok')
        self.assertEqual(supervisor.stop_feishu().error_code, 'CONTROL_FEISHU_STOP_FAILED')
        self.assertIs(supervisor._daemon, daemons[0])
        self.assertEqual(supervisor.current_state()['feishu']['state'], 'degraded')
        self.assertEqual(supervisor.start_feishu().error_code, 'CONTROL_FEISHU_RUNTIME_OWNERSHIP_UNRESOLVED')
        self.assertEqual(len(daemons), 1)

    def test_reconnect_does_not_start_after_stop_failure(self):
        daemons = []

        def daemon_factory(*args):
            daemon = _StopFailureDaemon(*args)
            daemons.append(daemon)
            return daemon

        supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings(), transport_factory=_Transport, daemon_factory=daemon_factory, gateway_factory=_Gateway)
        self.assertEqual(supervisor.start_feishu().status, 'ok')
        self.assertEqual(supervisor.reconnect_feishu().error_code, 'CONTROL_FEISHU_STOP_FAILED')
        self.assertEqual(len(daemons), 1)

    def test_partial_start_cleanup_failure_retains_ownership(self):
        daemons = []

        def daemon_factory(*args):
            daemon = _StopFailureDaemon(*args)
            daemons.append(daemon)
            return daemon

        supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings(), transport_factory=_ConnectFailureTransport, daemon_factory=daemon_factory, gateway_factory=_Gateway)
        result = supervisor.start_feishu()
        self.assertEqual(result.error_code, 'CONTROL_FEISHU_STOP_FAILED')
        self.assertIs(supervisor._daemon, daemons[0])
        self.assertEqual(supervisor.current_state()['feishu']['state'], 'degraded')

    def test_successful_stop_releases_ownership(self):
        self.assertEqual(self.supervisor.start_feishu().status, 'ok')
        self.assertEqual(self.supervisor.stop_feishu().status, 'ok')
        self.assertIsNone(self.supervisor._daemon)
        self.assertIsNone(self.supervisor._transport)
        self.assertEqual(self.supervisor.current_state()['feishu']['state'], 'stopped')

    def test_successful_reconnect_maintains_single_owner(self):
        self.assertEqual(self.supervisor.start_feishu().status, 'ok')
        old_daemon = self.supervisor._daemon
        self.assertEqual(self.supervisor.reconnect_feishu().status, 'ok')
        self.assertIsNot(self.supervisor._daemon, old_daemon)
        self.assertEqual(old_daemon.stop_calls, 1)
        self.assertTrue(self.supervisor.current_state()['feishu']['running'])

    def test_browser_bridge_survives_feishu_reconnect_and_closes_with_control_plane(self):
        class Browser:
            def __init__(self, *_args, **_kwargs): self.close_calls = 0
            def close(self): self.close_calls += 1

        daemons = []

        def daemon_factory(*args):
            daemon = _SharedBrowserDaemon(*args)
            daemons.append(daemon)
            return daemon

        with patch('cfr.chat.ChromeChatAdapter', Browser):
            supervisor = CfrSupervisor(
                settings_loader=lambda **_kwargs: _Settings(),
                transport_factory=_Transport,
                daemon_factory=daemon_factory,
                gateway_factory=_Gateway,
            )
            self.assertEqual(supervisor.start_feishu().status, 'ok')
            browser = supervisor.chat_adapter
            self.assertIs(daemons[0].chat_adapter, browser)
            self.assertFalse(daemons[0]._owns_chat_adapter)
            self.assertEqual(supervisor.reconnect_feishu().status, 'ok')
            self.assertIs(supervisor.chat_adapter, browser)
            self.assertIs(daemons[1].chat_adapter, browser)
            self.assertEqual(browser.close_calls, 0)
            supervisor.stop_feishu()
            self.assertEqual(browser.close_calls, 0)
            supervisor.close_browser_bridge()
            self.assertEqual(browser.close_calls, 1)

    def test_chat_selected_runtime_confirms_browser_before_starting_feishu(self):
        class ChatSettings(_Settings):
            default_surface = 'chat'

        daemons = []

        def daemon_factory(*args):
            daemon = _SharedBrowserDaemon(*args)
            daemons.append(daemon)
            return daemon

        supervisor = CfrSupervisor(
            settings_loader=lambda **_kwargs: ChatSettings(),
            transport_factory=_Transport,
            daemon_factory=daemon_factory,
            gateway_factory=_Gateway,
        )
        browser = Mock()
        with patch.object(
            supervisor,
            'start_browser_bridge',
            return_value={'available': True, 'status': 'ready'},
        ) as start_browser, patch.object(supervisor, '_shared_chat_adapter', return_value=browser):
            result = supervisor.start_feishu()
        self.assertEqual(result.status, 'ok')
        start_browser.assert_called_once_with()
        self.assertIs(daemons[0].chat_adapter, browser)
        self.assertTrue(daemons[0]._started)

    def test_chat_selected_runtime_fails_closed_when_browser_is_not_ready(self):
        class ChatSettings(_Settings):
            default_surface = 'chat'

        daemon = _SharedBrowserDaemon(ChatSettings(), _Transport(ChatSettings()))
        supervisor = CfrSupervisor(
            settings_loader=lambda **_kwargs: ChatSettings(),
            transport_factory=_Transport,
            daemon_factory=lambda *_args: daemon,
            gateway_factory=_Gateway,
        )
        with patch.object(
            supervisor,
            'start_browser_bridge',
            return_value={
                'available': False,
                'status': 'waiting_user',
                'error_code': 'CHATGPT_LOGIN_REQUIRED',
                'description': 'login required',
            },
        ):
            result = supervisor.start_feishu()
        self.assertEqual(result.error_code, 'CHATGPT_LOGIN_REQUIRED')
        self.assertFalse(daemon._started)
        self.assertIsNone(supervisor._daemon)

    def test_persisted_chat_surface_requires_browser_even_when_default_is_code(self):
        settings = SimpleNamespace(default_surface='code')
        daemon = SimpleNamespace(
            store=SimpleNamespace(list_surface_states=lambda: [
                {'chat_id': 'chat-1', 'selected_surface': 'code'},
                {'chat_id': 'chat-2', 'selected_surface': 'chat'},
            ])
        )
        self.assertTrue(CfrSupervisor._runtime_requires_chat(settings, daemon))

    def test_browser_bridge_reports_waiting_user_without_marking_ready(self):
        class Browser:
            def __init__(self, *_args, **_kwargs): pass
            def start(self): return {'available': False, 'status': 'waiting_user'}
            def close(self): pass

        with patch('cfr.chat.ChromeChatAdapter', Browser):
            supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings())
            state = supervisor.start_browser_bridge()
        self.assertEqual(state['status'], 'waiting_user')
        self.assertEqual(supervisor.activity()[-1]['event'], 'CHAT_BROWSER_WAITING_USER')

    def test_attached_embedded_host_creates_embedded_adapter_without_touching_legacy_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            config = LocalConfigStore(Path(directory) / 'config')
            config.set_chat_browser_backend('embedded')
            supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings(), config_store=config)
            host = SimpleNamespace(
                available=True,
                browser_url='http://127.0.0.1:9223',
                state_dir=Path(directory) / 'browser' / 'embedded',
                show=Mock(),
            )
            supervisor.attach_embedded_chat_host(host)
            mcp = Mock()
            browser = Mock()
            browser.start.return_value = {'available': True, 'status': 'ready'}
            with patch('cfr.chat.linux_shared_tab_mode', return_value=False), patch('cfr.chat.ChromeDevToolsMcp', return_value=mcp) as mcp_factory, patch(
                'cfr.chat.ChromeChatAdapter', return_value=browser
            ) as browser_factory:
                state = supervisor.start_browser_bridge()
            self.assertTrue(state['available'])
            mcp_factory.assert_called_once_with(
                mode='embedded',
                browser_url='http://127.0.0.1:9223',
                user_data_dir=host.state_dir / 'profile',
            )
            browser_factory.assert_called_once_with(mcp, show_browser=host.show)
            self.assertEqual(supervisor._chat_backend, 'embedded')
            self.assertEqual(supervisor.setup_state()['chat']['runtime_backend'], 'embedded')

    def test_auto_backend_stays_embedded_and_does_not_pop_retained_dedicated_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            config = LocalConfigStore(Path(directory) / 'config')
            supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings(), config_store=config)
            host = SimpleNamespace(
                available=True,
                browser_url='http://127.0.0.1:9223',
                state_dir=Path(directory) / 'browser' / 'embedded',
                show=Mock(),
            )
            supervisor.attach_embedded_chat_host(host)
            embedded = Mock()
            embedded.start.return_value = {
                'available': False,
                'status': 'waiting_user',
                'error_code': 'CHATGPT_EMBEDDED_LOGIN_REQUIRED',
            }
            embedded.close = Mock()

            def setup_snapshot(*, browser_backend, embedded_available):
                self.assertEqual(browser_backend, 'auto')
                self.assertTrue(embedded_available)
                return {'effective_backend': 'embedded'}

            with patch('cfr.chat.linux_shared_tab_mode', return_value=False), patch('cfr.chat.chat_setup_snapshot', side_effect=setup_snapshot), patch(
                'cfr.chat.ChromeDevToolsMcp', return_value=Mock()
            ), patch('cfr.chat.ChromeChatAdapter', return_value=embedded) as browser_factory:
                state = supervisor.start_browser_bridge()

            self.assertFalse(state['available'])
            self.assertEqual(state['status'], 'waiting_user')
            self.assertEqual(supervisor._chat_backend, 'embedded')
            self.assertEqual(supervisor.setup_state()['chat']['runtime_backend'], 'embedded')
            browser_factory.assert_called_once()
            embedded.close.assert_not_called()
            self.assertNotIn('CHAT_BROWSER_FALLBACK_DEDICATED', [item['event'] for item in supervisor.activity()])

    def test_explicit_embedded_backend_never_silently_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            config = LocalConfigStore(Path(directory) / 'config')
            config.set_chat_browser_backend('embedded')
            supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings(), config_store=config)
            host = SimpleNamespace(
                available=True,
                browser_url='http://127.0.0.1:9223',
                state_dir=Path(directory) / 'browser' / 'embedded',
                show=Mock(),
            )
            supervisor.attach_embedded_chat_host(host)
            embedded = Mock()
            embedded.start.return_value = {
                'available': False,
                'status': 'waiting_user',
                'error_code': 'CHATGPT_EMBEDDED_LOGIN_REQUIRED',
            }
            with patch('cfr.chat.linux_shared_tab_mode', return_value=False), patch('cfr.chat.ChromeDevToolsMcp', return_value=Mock()), patch(
                'cfr.chat.ChromeChatAdapter', return_value=embedded
            ) as browser_factory:
                state = supervisor.start_browser_bridge()
            self.assertFalse(state['available'])
            self.assertEqual(supervisor._chat_backend, 'embedded')
            browser_factory.assert_called_once()

    def test_auto_chat_setup_forces_embedded_migration_when_host_is_available(self):
        with tempfile.TemporaryDirectory() as directory:
            config = LocalConfigStore(Path(directory) / 'config')
            supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings(), config_store=config)
            host = SimpleNamespace(available=True, show=Mock())
            supervisor.attach_embedded_chat_host(host)
            with patch.object(supervisor, 'start_browser_bridge', return_value={
                'available': False,
                'status': 'waiting_user',
                'description': 'login',
            }) as start, patch.object(supervisor, '_start_chat_login_monitor') as monitor:
                result = supervisor.start_chat_setup()
            self.assertEqual(result.status, 'ok')
            start.assert_called_once_with(force_backend='embedded')
            monitor.assert_called_once_with()

    def test_show_embedded_chat_does_not_start_or_replace_browser_backend(self):
        supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings())
        host = SimpleNamespace(available=True, show=Mock())
        supervisor.attach_embedded_chat_host(host)
        hidden = supervisor.show_embedded_chat()
        self.assertEqual(hidden.error_code, 'CONTROL_CHAT_AUTOMATION_HIDDEN')
        supervisor._chat_adapter = SimpleNamespace(health_snapshot=lambda: {'status': 'waiting_user'})
        result = supervisor.show_embedded_chat()
        self.assertEqual(result.status, 'ok')
        host.show.assert_called_once_with()
        self.assertIsNotNone(supervisor.chat_adapter)

    def test_runtime_setup_change_is_persisted_and_reconnects_feishu_while_running(self):
        with tempfile.TemporaryDirectory() as directory:
            config = LocalConfigStore(Path(directory) / 'config')
            supervisor = CfrSupervisor(
                settings_loader=lambda **_kwargs: _Settings(),
                transport_factory=_Transport,
                daemon_factory=_Daemon,
                gateway_factory=_Gateway,
                config_store=config,
            )
            self.assertEqual(supervisor.start_feishu().status, 'ok')
            old_daemon = supervisor._daemon
            result = supervisor.save_setup_preferences(
                'code', 'auto', None,
                chat_browser_backend='embedded',
                codex_desktop_launcher='auto',
            )
            self.assertEqual(result.status, 'ok')
            self.assertIn('自动重新连接', result.message)
            self.assertEqual(config.get_chat_browser_backend(), 'embedded')
            self.assertEqual(old_daemon.stop_calls, 1)
            self.assertIsNot(supervisor._daemon, old_daemon)
            self.assertTrue(supervisor.current_state()['feishu']['running'])

    def test_launcher_only_setup_change_does_not_restart_feishu(self):
        with tempfile.TemporaryDirectory() as directory:
            config = LocalConfigStore(Path(directory) / 'config')
            supervisor = CfrSupervisor(
                settings_loader=lambda **_kwargs: _Settings(),
                transport_factory=_Transport,
                daemon_factory=_Daemon,
                gateway_factory=_Gateway,
                config_store=config,
            )
            self.assertEqual(supervisor.start_feishu().status, 'ok')
            old_daemon = supervisor._daemon
            result = supervisor.save_setup_preferences(
                'code', 'auto', None,
                chat_browser_backend='auto',
                codex_desktop_launcher='stock',
            )
            self.assertEqual(result.status, 'ok')
            self.assertIs(supervisor._daemon, old_daemon)
            self.assertEqual(old_daemon.stop_calls, 0)

    def test_feishu_credential_change_reconnects_running_runtime(self):
        supervisor = CfrSupervisor(
            settings_loader=lambda **_kwargs: _Settings(),
            transport_factory=_Transport,
            daemon_factory=_Daemon,
            gateway_factory=_Gateway,
        )
        self.assertEqual(supervisor.start_feishu().status, 'ok')
        old_daemon = supervisor._daemon
        with patch.object(supervisor._setup, 'save_feishu_credentials') as save:
            result = supervisor.save_feishu_credentials('cli_test', 'secret')
        self.assertEqual(result.status, 'ok')
        self.assertIn('自动重新连接', result.message)
        save.assert_called_once_with(app_id='cli_test', app_secret='secret')
        self.assertEqual(old_daemon.stop_calls, 1)
        self.assertIsNot(supervisor._daemon, old_daemon)

    def test_shutdown_stops_runtime_then_browser_and_disables_new_tasks(self):
        supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings())
        observed = []
        with patch.object(
            supervisor, 'stop_feishu', side_effect=lambda: (observed.append('feishu') or SimpleNamespace(status='ok', error_code=None))
        ), patch.object(
            supervisor, 'close_browser_bridge', side_effect=lambda: observed.append('browser')
        ):
            result = supervisor.shutdown()
        self.assertEqual(result, {'status': 'ok', 'errors': []})
        self.assertEqual(observed, ['feishu', 'browser'])
        self.assertFalse(supervisor.current_state()['accept_new_tasks'])

    def test_start_records_meaningful_stages_and_running_event(self):
        self.assertEqual(self.supervisor.start_feishu().status, 'ok')
        activity = self.supervisor.activity()
        self.assertEqual(
            [(item['event'], item.get('stage')) for item in activity],
            [
                ('FEISHU_STARTING', 'settings_load'),
                ('FEISHU_SETTINGS_VALIDATING', 'settings_validate'),
                ('FEISHU_TRANSPORT_CREATING', 'transport_create'),
                ('FEISHU_DAEMON_CREATING', 'daemon_create'),
                ('FEISHU_DAEMON_STARTING', 'daemon_start'),
                ('FEISHU_CHANNEL_CONNECTING', 'channel_connect'),
                ('FEISHU_RUNNING', 'running'),
            ],
        )

    def test_start_validation_failure_records_safe_stage_and_code(self):
        class InvalidSettings:
            def validate_execution(self):
                raise StructuredError('FEISHU_WORKSPACE_ROOT_INVALID', 'All configured workspace roots must be existing directories')

        supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: InvalidSettings())
        result = supervisor.start_feishu()
        self.assertEqual(result.error_code, 'FEISHU_WORKSPACE_ROOT_INVALID')
        event = supervisor.activity()[-1]
        self.assertEqual((event['event'], event['level'], event['stage'], event['code']), ('FEISHU_ERROR', 'error', 'settings_validate', 'FEISHU_WORKSPACE_ROOT_INVALID'))

    def test_start_unexpected_exception_records_no_exception_text(self):
        def transport_factory(_settings):
            raise RuntimeError('app-secret=do-not-expose')

        supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings(), transport_factory=transport_factory)
        result = supervisor.start_feishu()
        self.assertEqual(result.error_code, 'CONTROL_FEISHU_START_FAILED')
        rendered = str(supervisor.activity())
        self.assertNotIn('do-not-expose', rendered)
        self.assertNotIn('Traceback', rendered)

    def test_activity_is_readable_while_startup_is_blocked_in_channel_connect(self):
        entered = threading.Event()
        release = threading.Event()
        result = []

        class BlockingTransport(_Transport):
            def connect_until_ready(self, *_args, **_kwargs):
                entered.set()
                release.wait(1)
                super().connect_until_ready(*_args, **_kwargs)

        supervisor = CfrSupervisor(settings_loader=lambda **_kwargs: _Settings(), transport_factory=BlockingTransport, daemon_factory=_Daemon, gateway_factory=_Gateway)
        starter = threading.Thread(target=lambda: result.append(supervisor.start_feishu()))
        starter.start()
        self.assertTrue(entered.wait(1))
        observed = []
        reader = threading.Thread(target=lambda: observed.extend(supervisor.activity()))
        reader.start()
        reader.join(.2)
        self.assertFalse(reader.is_alive())
        self.assertIn(('FEISHU_CHANNEL_CONNECTING', 'channel_connect'), [(item['event'], item.get('stage')) for item in observed])
        states = []
        state_reader = threading.Thread(target=lambda: states.append(supervisor.current_state()))
        state_reader.start()
        state_reader.join(.2)
        self.assertFalse(state_reader.is_alive())
        self.assertEqual(states[0]['feishu']['state'], 'starting')
        release.set()
        starter.join(1)
        self.assertFalse(starter.is_alive())
        self.assertEqual(result[0].status, 'ok')

    def test_doctor_does_not_block_state_reads_and_rejects_concurrent_run(self):
        entered = threading.Event()
        release = threading.Event()
        results = []

        def doctor(**_kwargs):
            entered.set()
            release.wait(1)
            return {'Verdict': 'PASS'}

        supervisor = CfrSupervisor(doctor_runner=doctor)
        runner = threading.Thread(target=lambda: results.append(supervisor.run_doctor()))
        runner.start()
        self.assertTrue(entered.wait(1))
        state = []
        reader = threading.Thread(target=lambda: state.append(supervisor.current_state()))
        reader.start()
        reader.join(.2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(state[0]['doctor']['status'], 'NOT_RUN')
        self.assertEqual(supervisor.run_doctor().error_code, 'CONTROL_DOCTOR_BUSY')
        release.set()
        runner.join(1)
        self.assertFalse(runner.is_alive())
        self.assertEqual(results[0].status, 'ok')


class SupervisorPairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = _PairingSettings()
        self.transports = []
        self.leases = []

        def transport_factory(*args):
            transport = _PairingTransport(*args)
            self.transports.append(transport)
            return transport

        def lease_factory(*args):
            lease = _Lease(*args)
            self.leases.append(lease)
            return lease

        self.supervisor = CfrSupervisor(
            database=Path(self.temp.name) / 'cfr.sqlite3',
            settings_loader=lambda **_kwargs: self.settings,
            transport_factory=transport_factory,
            daemon_factory=_Daemon,
            gateway_factory=_Gateway,
            connection_lease_factory=lease_factory,
            config_store=LocalConfigStore(self.temp.name),
            pairing_code_factory=lambda: '583271',
        )

    def tearDown(self):
        self.supervisor.cancel_feishu_pairing()
        self.temp.cleanup()

    @staticmethod
    def _message(sender='ou-pair-user', chat_type='p2p', text='绑定 583271'):
        return FeishuInboundMessage(
            event_id='pair-event', message_id='pair-message', chat_id='chat-1', chat_type=chat_type,
            sender_open_id=sender, sender_type='user', message_type='text', text=text,
        )

    def test_pairing_uses_connection_only_without_execution_allowlists(self):
        result = self.supervisor.start_feishu_pairing()
        self.assertEqual(result.status, 'ok')
        self.assertTrue(self.settings.connection_validated)
        self.assertFalse(self.settings.execution_validated)
        self.assertEqual(self.supervisor.pairing_state()['state'], 'waiting')
        self.assertEqual(self.supervisor.pairing_state()['code'], '583271')

    def test_pairing_message_arriving_during_connect_is_not_lost_or_overwritten(self):
        message = self._message()

        class ImmediateMessageTransport(_PairingTransport):
            def connect_until_ready(self, message_handler, *_args, **_kwargs):
                self.message_handler = message_handler
                self.is_running = True
                message_handler(message)

        supervisor = CfrSupervisor(
            database=Path(self.temp.name) / 'immediate.sqlite3',
            settings_loader=lambda **_kwargs: self.settings,
            transport_factory=ImmediateMessageTransport,
            daemon_factory=_Daemon,
            gateway_factory=_Gateway,
            connection_lease_factory=lambda *args: _Lease(*args),
            config_store=LocalConfigStore(Path(self.temp.name) / 'immediate-config'),
            pairing_code_factory=lambda: '583271',
        )
        try:
            result = supervisor.start_feishu_pairing()
            self.assertEqual(result.status, 'ok')
            self.assertEqual(supervisor.pairing_state()['state'], 'pending_confirmation')
            self.assertIsNotNone(supervisor.pairing_state()['candidate'])
        finally:
            supervisor.cancel_feishu_pairing()

    def test_private_correct_code_captures_one_masked_candidate_without_gateway(self):
        self.supervisor.start_feishu_pairing()
        transport, lease = self.transports[0], self.leases[0]
        self.assertEqual(transport.message_handler(self._message())['status'], 'pending_confirmation')
        self.assertTrue(transport.stopped.wait(1))
        self.assertTrue(lease.released.wait(1))
        state = self.supervisor.pairing_state()
        self.assertEqual(state['state'], 'pending_confirmation')
        self.assertNotEqual(state['candidate'], 'ou-pair-user')
        self.assertIsNone(state['code'])
        self.assertEqual(transport.message_handler(self._message('ou-second'))['status'], 'ignored')

    def test_invalid_or_group_pairing_messages_are_silent(self):
        self.supervisor.start_feishu_pairing()
        handler = self.transports[0].message_handler
        self.assertEqual(handler(self._message(text='绑定 111111'))['status'], 'ignored')
        self.assertEqual(handler(self._message(chat_type='group'))['status'], 'ignored')
        self.assertEqual(self.supervisor.pairing_state()['state'], 'waiting')

    def test_confirm_persists_server_candidate_and_preserves_existing_policy(self):
        root = Path(self.temp.name).resolve()
        self.supervisor._config_store.set_feishu_security(['ou-existing'], [str(root)])
        self.supervisor.start_feishu_pairing()
        self.transports[0].message_handler(self._message())
        result = self.supervisor.confirm_feishu_pairing(str(root))
        self.assertEqual(result.status, 'ok')
        self.assertEqual(self.supervisor.pairing_state()['state'], 'idle')
        self.assertEqual(self.supervisor._config_store.get_allowed_open_ids(), ('ou-existing', 'ou-pair-user'))
        self.assertEqual(self.supervisor._config_store.get_allowed_workspace_roots(), (str(root),))
        self.assertTrue(self.supervisor.current_state()['feishu']['running'])
        self.assertEqual([event['event'] for event in self.supervisor.activity()][-1], 'FEISHU_RUNNING')

    def test_confirm_timeout_preserves_policy_without_second_runtime(self):
        released = threading.Event()

        class BlockingTransport(_PairingTransport):
            def stop(self):
                released.wait(1)
                super().stop()

        def transport_factory(*args):
            transport = BlockingTransport(*args)
            self.transports.append(transport)
            return transport
        self.supervisor._transport_factory = transport_factory
        self.supervisor._pairing_cleanup_timeout = 0.01
        root = Path(self.temp.name).resolve()
        self.supervisor.start_feishu_pairing()
        self.transports[0].message_handler(self._message())
        result = self.supervisor.confirm_feishu_pairing(str(root))
        self.assertEqual(result.error_code, 'CONTROL_FEISHU_PAIRING_HANDOFF_TIMEOUT')
        self.assertEqual(self.supervisor._config_store.get_allowed_open_ids(), ('ou-pair-user',))
        self.assertIsNone(self.supervisor._daemon)
        released.set()

    def test_confirm_start_failure_preserves_policy(self):
        root = Path(self.temp.name).resolve()

        class FailingRuntimeTransport(_PairingTransport):
            def connect_until_ready(self, message_handler, *args, **kwargs):
                if args:
                    raise RuntimeError('normal start failed')
                super().connect_until_ready(message_handler, *args, **kwargs)

        calls = []
        def transport_factory(*args):
            calls.append(args)
            transport = _PairingTransport(*args) if len(calls) == 1 else FailingRuntimeTransport(*args)
            self.transports.append(transport)
            return transport
        self.supervisor._transport_factory = transport_factory
        self.supervisor.start_feishu_pairing()
        self.transports[0].message_handler(self._message())
        result = self.supervisor.confirm_feishu_pairing(str(root))
        self.assertEqual(result.error_code, 'CONTROL_FEISHU_START_FAILED')
        self.assertEqual(self.supervisor._config_store.get_allowed_open_ids(), ('ou-pair-user',))

    def test_activity_is_bounded_and_masks_pairing_candidate(self):
        for index in range(101):
            self.supervisor._record_activity('PAIRING_MESSAGE_IGNORED', '配对期间收到不匹配的私聊文本。')
        self.assertEqual(len(self.supervisor.activity()), 50)
        self.supervisor.start_feishu_pairing()
        self.transports[0].message_handler(self._message('ou-sensitive-user'))
        rendered = str(self.supervisor.activity())
        self.assertNotIn('ou-sensitive-user', rendered)

    def test_pairing_and_normal_runtime_are_mutually_exclusive(self):
        self.supervisor.start_feishu_pairing()
        self.assertEqual(self.supervisor.start_feishu().error_code, 'CONTROL_FEISHU_PAIRING_ACTIVE')
        self.assertEqual(self.supervisor.reconnect_feishu().error_code, 'CONTROL_FEISHU_PAIRING_ACTIVE')
        self.assertEqual(self.supervisor.cancel_feishu_pairing().status, 'ok')
        self.assertEqual(self.supervisor.cancel_feishu_pairing().status, 'ok')

    def test_expiry_releases_connection_and_clears_code(self):
        self.supervisor.start_feishu_pairing()
        transport, lease = self.transports[0], self.leases[0]
        self.supervisor._expire_feishu_pairing()
        self.assertTrue(transport.stopped.wait(1))
        self.assertTrue(lease.released.wait(1))
        state = self.supervisor.pairing_state()
        self.assertEqual(state['state'], 'expired')
        self.assertIsNone(state['code'])
        self.assertIsNone(state['candidate'])
