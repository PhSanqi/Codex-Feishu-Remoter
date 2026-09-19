import gc
import queue
import threading
import time
import unittest
import weakref

from cfr.feishu.daemon import FeishuDaemon


class _EmptyQueueStore:
    def next_queued_message_id(self, _chat_id):
        return None


class FeishuDaemonSchedulerTests(unittest.TestCase):
    def test_partial_worker_loss_is_reported_as_degraded_runtime(self):
        daemon = FeishuDaemon.__new__(FeishuDaemon)
        daemon._started = True
        daemon._fatal_error = None
        daemon.stop_event = threading.Event()
        daemon._heartbeat_thread = type('Thread', (), {'is_alive': lambda self: True})()
        daemon._workers = [
            type('Thread', (), {'is_alive': lambda self: True})(),
            type('Thread', (), {'is_alive': lambda self: False})(),
        ]
        self.assertEqual(daemon.fatal_error.code, 'FEISHU_WORKER_STOPPED')
        self.assertIn('1/2', daemon.fatal_error.message)

    def test_database_empty_state_clears_drifted_memory_count(self):
        daemon = FeishuDaemon.__new__(FeishuDaemon)
        daemon.stop_event = threading.Event()
        daemon.queue = queue.Queue()
        daemon.queue.put('chat-1')
        daemon.store = _EmptyQueueStore()
        daemon._queue_lock = threading.RLock()
        daemon._active_chats = set()
        daemon._scheduled_chats = {'chat-1'}
        daemon._chat_pending_counts = {'chat-1': 7}

        worker = threading.Thread(target=daemon._worker)
        worker.start()
        deadline = time.monotonic() + 2
        while daemon.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        daemon.stop_event.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertNotIn('chat-1', daemon._scheduled_chats)
        self.assertNotIn('chat-1', daemon._chat_pending_counts)

    def test_per_chat_locks_do_not_retain_historical_chat_ids(self):
        daemon = FeishuDaemon.__new__(FeishuDaemon)
        daemon._locks = weakref.WeakValueDictionary()
        daemon._locks_guard = threading.RLock()
        lock = daemon._lock_for('chat-1')
        self.assertIs(lock, daemon._lock_for('chat-1'))
        del lock
        gc.collect()
        self.assertEqual(len(daemon._locks), 0)


if __name__ == '__main__':
    unittest.main()
