import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from cfr.codex.runtime_lease import CfrThreadRuntimeLeaseManager
from cfr.core.models import StructuredError


class RuntimeLeaseTests(unittest.TestCase):
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

    def test_different_threads_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            manager = CfrThreadRuntimeLeaseManager(database)
            one = manager.acquire('thread-a')
            two = manager.acquire('thread-b')
            self.assertNotEqual(one.thread_id, two.thread_id)
            manager.release(one)
            manager.release(two)


if __name__ == '__main__':
    unittest.main()
