from pathlib import Path
import tempfile
import time
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.core.models import StructuredError
from cfr.feishu.connection import FeishuConnectionLease


class ConnectionLeaseTests(unittest.TestCase):
    def test_connection_owners_are_singleton_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            first = FeishuConnectionLease(path, 'feishu:app', ttl=3, owner_instance_id='first').acquire()
            second = FeishuConnectionLease(path, 'feishu:app', ttl=3, owner_instance_id='second')
            with self.assertRaises(StructuredError) as caught:
                second.acquire()
            self.assertEqual(caught.exception.code, 'FEISHU_DAEMON_ALREADY_RUNNING')
            first.release()
            second.acquire()
            second.release()

    def test_connection_heartbeat_keeps_lease_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = FeishuConnectionLease(Path(directory) / 'db.sqlite3', 'feishu:app', ttl=3, owner_instance_id='first').acquire()
            before = lease.store.inspect_daemon_lease('feishu:app')['heartbeat_at']
            time.sleep(1.1)
            after = lease.store.inspect_daemon_lease('feishu:app')['heartbeat_at']
            lease.release()
            self.assertGreater(after, before)


if __name__ == '__main__':
    unittest.main()
