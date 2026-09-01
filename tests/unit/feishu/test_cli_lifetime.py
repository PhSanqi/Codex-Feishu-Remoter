from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.cli.main import main
from cfr.feishu.config import FeishuSettings


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


if __name__ == '__main__':
    unittest.main()
