from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

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

    def test_acquire_is_idempotent_for_the_same_connection_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = FeishuConnectionLease(Path(directory) / 'db.sqlite3', 'feishu:app', ttl=3, owner_instance_id='first')
            self.assertIs(lease.acquire(), lease.acquire())
            heartbeat = lease._heartbeat_thread
            self.assertIs(lease._heartbeat_thread, heartbeat)
            lease.release()

    def test_connection_heartbeat_keeps_lease_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = FeishuConnectionLease(Path(directory) / 'db.sqlite3', 'feishu:app', ttl=3, owner_instance_id='first').acquire()
            before = lease.store.inspect_daemon_lease('feishu:app')['heartbeat_at']
            time.sleep(1.1)
            after = lease.store.inspect_daemon_lease('feishu:app')['heartbeat_at']
            lease.release()
            self.assertGreater(after, before)

    def test_lease_lost_detects_replaced_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = FeishuConnectionLease(Path(directory) / 'db.sqlite3', 'feishu:app', ttl=30, owner_instance_id='first').acquire()
            with lease.store._connection() as connection:
                connection.execute(
                    'update feishu_daemon_lease set owner_instance_id=? where lease_key=?',
                    ('replacement', 'feishu:app'),
                )
            self.assertTrue(lease.lease_lost)
            lease.release()

    def test_transient_heartbeat_error_does_not_lose_confirmed_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = FeishuConnectionLease(Path(directory) / 'db.sqlite3', 'feishu:app', ttl=3, owner_instance_id='first').acquire()
            original = lease.store.heartbeat_daemon_lease
            calls = 0

            def heartbeat(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise sqlite3.OperationalError('database is busy')
                return original(*args, **kwargs)

            with patch.object(lease.store, 'heartbeat_daemon_lease', side_effect=heartbeat):
                time.sleep(1.2)
                self.assertFalse(lease.lease_lost)
            lease.release()

    def test_lease_inspection_error_fails_closed_after_confirmation_window(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = FeishuConnectionLease(Path(directory) / 'db.sqlite3', 'feishu:app', ttl=3, owner_instance_id='first').acquire()
            lease._last_confirmed = time.monotonic() - 3
            with patch.object(lease.store, 'inspect_daemon_lease', side_effect=sqlite3.OperationalError('busy')):
                self.assertTrue(lease.lease_lost)
            lease.release()


if __name__ == '__main__':
    unittest.main()
