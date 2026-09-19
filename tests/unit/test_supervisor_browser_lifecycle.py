import threading
import time
import unittest

from cfr.control.supervisor import CfrSupervisor


class _SlowChatAdapter:
    def __init__(self):
        self._guard = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.closed = 0

    def start(self):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.03)
        with self._guard:
            self.active -= 1
        return {'available': True, 'status': 'ready', 'description': 'ready'}

    def close(self):
        self.closed += 1

    def start_manual_login(self):
        return {'available': False, 'status': 'waiting_user', 'description': 'manual'}

    def finish_manual_login(self):
        return {'available': True, 'status': 'ready', 'description': 'ready'}


class SupervisorBrowserLifecycleTests(unittest.TestCase):
    def _supervisor(self, adapter):
        supervisor = CfrSupervisor.__new__(CfrSupervisor)
        supervisor._lock = threading.RLock()
        supervisor._browser_lifecycle_lock = threading.RLock()
        supervisor._chat_adapter = adapter
        supervisor._chat_backend = 'dedicated'
        supervisor._chat_login_monitor_stop = threading.Event()
        supervisor._activity = []
        supervisor._desired_chat_backend = lambda force_backend=None: force_backend or supervisor._chat_backend
        supervisor._shared_chat_adapter = lambda force_backend=None: supervisor._chat_adapter
        supervisor._record_activity = lambda *_args, **_kwargs: None
        return supervisor

    def test_concurrent_browser_start_calls_are_serialized(self):
        adapter = _SlowChatAdapter()
        supervisor = self._supervisor(adapter)
        threads = [threading.Thread(target=supervisor.start_browser_bridge) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(adapter.max_active, 1)

    def test_close_detaches_adapter_once(self):
        adapter = _SlowChatAdapter()
        supervisor = self._supervisor(adapter)
        supervisor.close_browser_bridge()
        supervisor.close_browser_bridge()
        self.assertEqual(adapter.closed, 1)

    def test_manual_login_and_verify_are_serialized_through_same_adapter(self):
        adapter = _SlowChatAdapter()
        supervisor = self._supervisor(adapter)
        supervisor._setup = type('Setup', (), {'invalidate': lambda self: None})()
        supervisor.current_state = lambda: {}
        started = supervisor.start_manual_chat_login()
        verified = supervisor.finish_manual_chat_login()
        self.assertEqual(started.status, 'ok')
        self.assertEqual(verified.status, 'ok')


if __name__ == '__main__':
    unittest.main()
