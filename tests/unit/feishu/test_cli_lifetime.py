from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.cli.main import main
from cfr.feishu.config import FeishuSettings
from cfr.feishu.daemon import FeishuDaemon
from cfr.core.models import StructuredError


class LifetimeTransport:
    def __init__(self, _settings):
        self.ready = False
        self.stopped = False
        self.error = None

    def connect_until_ready(self, *_args, **_kwargs):
        self.ready = True

    @property
    def is_running(self):
        return self.ready and not self.stopped

    def stop(self):
        self.stopped = True


class LifetimeDaemon:
    instances = []

    def __init__(self, _settings, transport):
        self.transport = transport
        self.store = None
        self.stop_event = threading.Event()
        self.waited = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self, background_workers=True):
        self.background_workers = background_workers
        return self

    def stop(self):
        self.stopped = True
        self.transport.stop()


class LifetimeGateway:
    def __init__(self, _settings, _store, _daemon):
        pass

    def handle_message_event(self, *_args):
        return None

    def handle_card_action(self, *_args):
        return None


class CliLifetimeTests(unittest.TestCase):
    def test_feishu_run_does_not_stop_immediately_after_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FeishuSettings('app', 'secret', ('ou',), (Path(directory),), database=Path(directory) / 'db.sqlite3')
            daemon_holder = {}
            original_wait = threading.Event.wait

            def wait(event, timeout=None):
                daemon_holder['daemon'].waited = True
                return True

            def daemon_factory(*args):
                daemon = LifetimeDaemon(*args)
                daemon_holder['daemon'] = daemon
                return daemon

            with patch('cfr.feishu.config.load_settings', return_value=settings), patch('cfr.feishu.transport.ChannelFeishuTransport', LifetimeTransport), patch('cfr.feishu.daemon.FeishuDaemon', side_effect=daemon_factory), patch('cfr.feishu.gateway.FeishuGateway', LifetimeGateway), patch('threading.Event.wait', wait):
                self.assertEqual(main(['--db', str(settings.database), 'feishu', 'run']), 0)
            self.assertTrue(daemon_holder['daemon'].waited)
            self.assertTrue(daemon_holder['daemon'].stopped)

    def test_daemon_stop_runs_all_cleanup_stages_even_when_optional_cleanup_fails(self):
        calls = []
        transport_attempts = []

        def stop_transport():
            transport_attempts.append(1)
            if len(transport_attempts) == 1:
                raise RuntimeError('transport close failed')
            calls.append('transport')

        daemon = FeishuDaemon.__new__(FeishuDaemon)
        daemon._started = True
        daemon.stop_event = threading.Event()
        daemon.approvals = SimpleNamespace(cancel_all_pending=lambda: None, close=lambda: calls.append('approvals'))
        daemon.adapter = SimpleNamespace(registry=SimpleNamespace(_active={}))
        daemon._workers = []
        daemon._heartbeat_thread = None
        daemon.transport = SimpleNamespace(stop=stop_transport)
        daemon.chat_adapter = SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError('chat close failed')))
        daemon._owns_chat_adapter = True
        daemon.lease_key = 'feishu:test'
        daemon.instance_id = 'instance'
        daemon.store = SimpleNamespace(
            release_daemon_lease=lambda *_args: calls.append('lease'),
            close=lambda: calls.append('store'),
        )
        daemon.binding_store = SimpleNamespace(close=lambda: calls.append('bindings'))
        with self.assertRaises(StructuredError) as caught:
            daemon.stop()
        self.assertEqual(caught.exception.code, 'FEISHU_DAEMON_STOP_CLEANUP_FAILED')
        self.assertEqual(calls, ['approvals', 'lease'])
        self.assertTrue(daemon._started)
        daemon.stop()
        self.assertFalse(daemon._started)
        self.assertEqual(len(transport_attempts), 2)
        self.assertEqual(calls, ['approvals', 'lease', 'approvals', 'transport', 'lease', 'store', 'bindings'])

    def test_daemon_stop_does_not_keep_dead_runtime_owned_for_optional_close_failure(self):
        calls = []
        daemon = FeishuDaemon.__new__(FeishuDaemon)
        daemon._started = True
        daemon.stop_event = threading.Event()
        daemon.approvals = SimpleNamespace(cancel_all_pending=lambda: None, close=lambda: calls.append('approvals'))
        daemon.adapter = SimpleNamespace(registry=SimpleNamespace(_active={}))
        daemon._workers = []
        daemon._heartbeat_thread = None
        daemon.transport = SimpleNamespace(stop=lambda: calls.append('transport'))
        daemon.chat_adapter = SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError('chat close failed')))
        daemon._owns_chat_adapter = True
        daemon.lease_key = 'feishu:test'
        daemon.instance_id = 'instance'
        daemon.store = SimpleNamespace(
            release_daemon_lease=lambda *_args: calls.append('lease') or True,
            close=lambda: (_ for _ in ()).throw(RuntimeError('store close failed')),
        )
        daemon.binding_store = SimpleNamespace(close=lambda: calls.append('bindings'))
        daemon.stop()
        self.assertEqual(calls, ['approvals', 'transport', 'lease', 'bindings'])
        self.assertFalse(daemon._started)


if __name__ == '__main__':
    unittest.main()
