from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.cli.main import _run_feishu_setup_only


class SetupTransport:
    def __init__(self):
        self.connected = False
        self.stopped = False

    def connect_until_ready(self, handler, **_kwargs):
        self.connected = True
        handler(SimpleNamespace(sender_open_id='ou', chat_id='oc', message_id='om'), 'evt')

    def stop(self):
        self.stopped = True


class SetupOnlyTests(unittest.TestCase):
    def test_setup_only_connects_transport_without_execution_daemon(self):
        transport = SetupTransport()
        with patch('cfr.cli.main.time.sleep', side_effect=KeyboardInterrupt):
            self.assertEqual(_run_feishu_setup_only(transport, show_identifiers=True), 0)
        self.assertTrue(transport.connected)
        self.assertTrue(transport.stopped)


if __name__ == '__main__':
    unittest.main()
