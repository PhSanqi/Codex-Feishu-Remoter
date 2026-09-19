import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from cfr.codex.runtime_lease import CfrThreadRuntimeLeaseManager
from cfr.core.models import StructuredError


class RuntimeLeaseTests(unittest.TestCase):
    def test_in_memory_manager_keeps_one_shared_database_across_operations(self):
        manager = CfrThreadRuntimeLeaseManager(':memory:', instance_id='memory')
        try:
            lease = manager.acquire('thread-a')
            self.assertTrue(manager.heartbeat(lease))
            self.assertEqual(manager.inspect()[0]['thread_id'], 'thread-a')
            self.assertTrue(manager.release(lease))
            self.assertEqual(manager.inspect(), [])
        finally:
            manager.close()

    def test_acquire_conflict_release_handoff_and_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            first = CfrThreadRuntimeLeaseManager(database, instance_id='a', id_factory=lambda: 'lease-a')
            second = CfrThreadRuntimeLeaseManager(database, instance_id='b', id_factory=lambda: 'lease-b')
            lease_a = first.acquire('thread-a')
            with self.assertRaises(StructuredError) as caught:
                second.acquire('thread-a')
            self.assertEqual(caught.exception.code, 'CFR_RUNTIME_WRITER_ACTIVE')
            self.assertTrue(first.release(lease_a))
            lease_b = second.acquire('thread-a')
            self.assertEqual(lease_b.generation, 2)
            self.assertTrue(second.release(lease_b))

    def test_expired_reclaim_increments_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            first = CfrThreadRuntimeLeaseManager(database, instance_id='a', ttl=0.05)
            lease_a = first.acquire('thread-a')
            time.sleep(0.08)
            second = CfrThreadRuntimeLeaseManager(database, instance_id='b')
            lease_b = second.acquire('thread-a')
            self.assertEqual(lease_b.generation, lease_a.generation + 1)
            self.assertFalse(first.release(lease_a))
            self.assertTrue(second.inspect())
            second.release(lease_b)

    def test_dead_local_owner_is_reclaimed_without_waiting_for_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            first = CfrThreadRuntimeLeaseManager(database, instance_id='dead-owner', owner_pid=424242, ttl=60)
            lease_a = first.acquire('thread-a')
            second = CfrThreadRuntimeLeaseManager(database, instance_id='new-owner', owner_pid=434343, ttl=60)
            with patch.object(second, '_pid_is_alive', return_value=False):
                lease_b = second.acquire('thread-a')
            self.assertEqual(lease_b.generation, lease_a.generation + 1)
            self.assertFalse(first.release(lease_a))
            self.assertTrue(second.release(lease_b))

    def test_heartbeat_renews_and_lost_lease_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            manager = CfrThreadRuntimeLeaseManager(database, ttl=0.12, heartbeat_interval=0.03)
            lease = manager.acquire('thread-a')
            manager.start_heartbeat(lease, interval=0.03)
            time.sleep(0.3)
            self.assertFalse(lease.lease_lost)
            connection = sqlite3.connect(database)
            try:
                connection.execute('delete from cfr_thread_runtime_leases where thread_id=?', ('thread-a',))
                connection.commit()
            finally:
                connection.close()
            self.assertFalse(manager.heartbeat(lease))
            self.assertTrue(lease.lease_lost)
            manager.release(lease)

    def test_heartbeat_start_is_idempotent_for_same_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CfrThreadRuntimeLeaseManager(Path(directory) / 'cfr.sqlite3', heartbeat_interval=30)
            lease = manager.acquire('thread-a')
            first = manager.start_heartbeat(lease)
            second = manager.start_heartbeat(lease)
            self.assertIs(first, second)
            self.assertEqual(len(manager._heartbeat_threads), 1)
            manager.release(lease)

    def test_schema_setup_runs_once_per_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CfrThreadRuntimeLeaseManager(Path(directory) / 'cfr.sqlite3')
            calls = []
            original = manager._ensure_schema

            def counted(connection):
                calls.append(1)
                return original(connection)

            with patch.object(manager, '_ensure_schema', side_effect=counted):
                one = manager.acquire('thread-a')
                two = manager.acquire('thread-b')
                manager.inspect()
            self.assertEqual(len(calls), 1)
            manager.release(one)
            manager.release(two)

    def test_different_threads_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            manager = CfrThreadRuntimeLeaseManager(database)
            one = manager.acquire('thread-a')
            two = manager.acquire('thread-b')
            self.assertNotEqual(one.thread_id, two.thread_id)
            manager.release(one)
            manager.release(two)

    def test_transient_sqlite_error_does_not_immediately_lose_unexpired_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            manager = CfrThreadRuntimeLeaseManager(database, ttl=30)
            lease = manager.acquire('thread-a')
            original = manager._connect
            with patch.object(manager, '_connect', side_effect=sqlite3.OperationalError('database is locked')):
                self.assertTrue(manager.heartbeat(lease))
            self.assertFalse(lease.lease_lost)
            manager._connect = original
            self.assertTrue(manager.heartbeat(lease))
            manager.release(lease)

    def test_transient_sqlite_error_fails_closed_after_last_confirmed_expiry(self):
        clock = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            manager = CfrThreadRuntimeLeaseManager(database, ttl=10, clock=lambda: clock[0])
            lease = manager.acquire('thread-a')
            clock[0] = 111.0
            with patch.object(manager, '_connect', side_effect=sqlite3.OperationalError('database is locked')):
                self.assertFalse(manager.heartbeat(lease))
            self.assertTrue(lease.lease_lost)

    def test_release_retries_transient_sqlite_failure_before_leaving_stale_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CfrThreadRuntimeLeaseManager(Path(directory) / 'cfr.sqlite3')
            lease = manager.acquire('thread-a')
            original = manager._connect
            calls = []

            def flaky_connect():
                calls.append(1)
                if len(calls) < 3:
                    raise sqlite3.OperationalError('database is locked')
                return original()

            with patch.object(manager, '_connect', side_effect=flaky_connect), patch('cfr.codex.runtime_lease.time.sleep'):
                self.assertTrue(manager.release(lease))
            self.assertEqual(len(calls), 3)
            self.assertEqual(manager.inspect(), [])


if __name__ == '__main__':
    unittest.main()
