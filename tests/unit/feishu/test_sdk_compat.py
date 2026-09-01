import asyncio
import threading
import time
from types import ModuleType
import unittest
from unittest.mock import patch

from cfr.feishu.sdk_compat import (
    BgPreStopDrainResult,
    ChannelSdkShutdownCapture,
    _loop_tasks,
    _sdk_owned_task,
    capture_channel_sdk_shutdown_targets,
    drain_bg_loop_before_sdk_stop,
    drain_captured_bg_loop_after_worker,
    drain_dedicated_channel_bg_loop,
    drain_sdk_tasks,
    handoff_channel_bg_ownership,
    shutdown_channel_without_device_flow_close,
    snapshot_sdk_tasks,
    stop_captured_bg_loop,
    stop_channel_without_device_flow_close,
)


def _sdk_module():
    module = ModuleType('lark_channel.test_probe')
    exec(
        'async def fake_sdk_ping(event):\n'
        '    await event.wait()\n',
        module.__dict__,
    )
    return module


class SdkTaskOwnershipTests(unittest.TestCase):
    def test_sdk_owned_task_detects_real_coroutine_module_via_cr_frame(self):
        loop = asyncio.new_event_loop()
        module = _sdk_module()
        event = asyncio.Event()
        task = loop.create_task(module.fake_sdk_ping(event))
        try:
            self.assertTrue(_sdk_owned_task(task))
            origin = task.get_coro().cr_frame.f_globals['__name__']
            self.assertEqual(origin, 'lark_channel.test_probe')
        finally:
            task.cancel()
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            loop.close()


class SdkTaskDrainLoopCloseRaceTests(unittest.TestCase):
    class _ClosingRunningLoop:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        def is_running(self):
            return True

    @staticmethod
    def _drain_coro():
        async def drain():
            return None

        return drain()

    @staticmethod
    def _captured_task(done: bool):
        class CapturedTask:
            def done(self):
                return done

        return CapturedTask()

    def test_post_transfer_runtime_error_with_closed_loop_and_terminal_tasks_is_benign(self):
        loop = self._ClosingRunningLoop()
        drain_coro = self._drain_coro()
        captured = self._captured_task(done=True)

        class Future:
            def result(self, *, timeout):
                loop.closed = True
                raise RuntimeError('Event loop is closed')

        try:
            with patch('cfr.feishu.sdk_compat._cancel_sdk_tasks', new=lambda _loop, _captured: drain_coro), patch(
                'cfr.feishu.sdk_compat.asyncio.run_coroutine_threadsafe', return_value=Future(),
            ):
                _before, _after, generators, issue = _loop_tasks(loop, timeout=1, captured_tasks=(captured,))
            self.assertTrue(generators)
            self.assertIsNone(issue)
            self.assertIsNotNone(drain_coro.cr_frame)
        finally:
            drain_coro.close()

    def test_post_transfer_runtime_error_with_closed_loop_and_pending_task_fails_closed(self):
        loop = self._ClosingRunningLoop()
        drain_coro = self._drain_coro()
        captured = self._captured_task(done=False)

        class Future:
            def result(self, *, timeout):
                loop.closed = True
                raise RuntimeError('Event loop is closed')

        try:
            with patch('cfr.feishu.sdk_compat._cancel_sdk_tasks', new=lambda _loop, _captured: drain_coro), patch(
                'cfr.feishu.sdk_compat.asyncio.run_coroutine_threadsafe', return_value=Future(),
            ):
                _before, _after, generators, issue = _loop_tasks(loop, timeout=1, captured_tasks=(captured,))
            self.assertFalse(generators)
            self.assertEqual(issue, 'SDK_LOOP_CLOSED_WITH_CAPTURED_PENDING_TASKS')
            self.assertIsNotNone(drain_coro.cr_frame)
        finally:
            drain_coro.close()

    def test_post_transfer_runtime_error_with_open_loop_remains_drain_error(self):
        loop = self._ClosingRunningLoop()
        drain_coro = self._drain_coro()
        captured = self._captured_task(done=True)

        class Future:
            def result(self, *, timeout):
                raise RuntimeError('unexpected owner-loop failure')

        try:
            with patch('cfr.feishu.sdk_compat._cancel_sdk_tasks', new=lambda _loop, _captured: drain_coro), patch(
                'cfr.feishu.sdk_compat.asyncio.run_coroutine_threadsafe', return_value=Future(),
            ):
                _before, _after, generators, issue = _loop_tasks(loop, timeout=1, captured_tasks=(captured,))
            self.assertFalse(generators)
            self.assertEqual(issue, 'SDK_TASK_DRAIN_ERROR:RuntimeError')
            self.assertIsNotNone(drain_coro.cr_frame)
        finally:
            drain_coro.close()


class StartWorkerSerializationTests(unittest.TestCase):
    @staticmethod
    def _loop_fixture():
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def runner():
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()
            loop.close()

        thread = threading.Thread(target=runner, name='test-start-worker-bg')
        thread.start()
        if not ready.wait(1):
            raise AssertionError('BG loop did not start')
        return loop, thread

    @staticmethod
    def _channel(loop, thread, start_future, release, cleanup_log=None):
        cleanup_log = cleanup_log if cleanup_log is not None else []

        class Ws:
            def stop(self):
                cleanup_log.append('ws_stop')
                release.set()

        class Channel:
            def __init__(self):
                self._shutdown = threading.Event()
                self._stop_requested = threading.Event()
                self._lifecycle_lock = threading.RLock()
                self._bg_lock = threading.RLock()
                self._bg_tasks_lock = threading.RLock()
                self._bg_loop = loop
                self._bg_thread = thread
                self._bg_tasks = set()
                self._bot_identity_retry_future = None
                self._start_future = start_future
                self._ws_client = Ws()
                self._background_generation = 0
                self._lifecycle_generation = 0
                self._started = True
                self._ready_flag = True
                self._connection_state = 'connected'
                self._connection_last_disconnected_at = None
                self._ready_event = None

            def _stop_keepalive_watchdog(self):
                cleanup_log.append('watchdog_stop')

            def _cancel_bg_tasks(self):
                cleanup_log.append(('late_cancel', self._bg_loop is not None))
                if self._bg_loop is not None:
                    asyncio.run_coroutine_threadsafe(asyncio.sleep(60), self._bg_loop)

            def _stop_bg_loop(self, *, join_timeout):
                cleanup_log.append(('late_stop', self._bg_loop is not None))
                if self._bg_loop is not None:
                    self._bg_loop.call_soon_threadsafe(self._bg_loop.stop)

            def _ensure_bg_loop(self):
                if self._bg_loop is not None:
                    return
                with self._bg_lock:
                    if self._bg_loop is not None:
                        return
                    if self._shutdown.is_set():
                        raise RuntimeError('channel is shutting down')

            def schedule(self, coro):
                try:
                    self._ensure_bg_loop()
                except Exception:
                    coro.close()
                    raise
                if self._bg_loop is None:
                    coro.close()
                    raise RuntimeError('channel BG loop detached')
                return asyncio.run_coroutine_threadsafe(coro, self._bg_loop)

            def _cleanup_failed_start(self):
                self._cancel_bg_tasks()
                self._stop_bg_loop(join_timeout=1)

        return Channel()

    def test_start_future_wrapper_cancel_does_not_prove_worker_exit(self):
        async def scenario():
            release = threading.Event()
            worker_exited = threading.Event()

            def worker():
                release.wait(1)
                worker_exited.set()

            future = asyncio.get_running_loop().run_in_executor(None, worker)
            future.cancel()
            self.assertTrue(future.done())
            self.assertTrue(future.cancelled())
            self.assertFalse(worker_exited.is_set())
            release.set()
            for _ in range(100):
                if worker_exited.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(worker_exited.is_set())

        asyncio.run(scenario())

    def test_bg_ownership_handoff_blocks_new_schedule_and_detaches_cleanup_authority(self):
        async def scenario():
            loop, thread = self._loop_fixture()
            release = threading.Event()
            worker_future = asyncio.get_running_loop().run_in_executor(None, release.wait, 1)
            channel = self._channel(loop, thread, worker_future, release)
            try:
                handoff = handoff_channel_bg_ownership(channel)
                self.assertTrue(handoff.completed)
                self.assertTrue(handoff.scheduling_blocked_before_detach)
                self.assertTrue(handoff.detached_from_sdk)
                self.assertIsNone(channel._bg_loop)
                self.assertIsNone(channel._bg_thread)
                with self.assertRaisesRegex(RuntimeError, 'shutting down|detached'):
                    channel.schedule(asyncio.sleep(0))
                channel._cleanup_failed_start()
                self.assertEqual(channel._cfr_bg_ownership_handoff.late_bg_task_created_after_handoff, False)
            finally:
                release.set()
                try:
                    await asyncio.wait_for(asyncio.shield(worker_future), timeout=1)
                except BaseException:
                    pass
                if thread.is_alive():
                    loop.call_soon_threadsafe(loop.stop)
                    thread.join(1)
                if not loop.is_closed():
                    loop.close()

        asyncio.run(scenario())

    def test_late_start_worker_cleanup_cannot_submit_sleep_after_handoff(self):
        async def scenario():
            loop, thread = self._loop_fixture()
            release = threading.Event()
            worker_future = asyncio.get_running_loop().run_in_executor(None, release.wait, 1)
            log = []
            channel = self._channel(loop, thread, worker_future, release, log)
            try:
                handoff = handoff_channel_bg_ownership(channel)
                channel._cleanup_failed_start()
                self.assertIn(('late_cancel', False), log)
                self.assertIn(('late_stop', False), log)
                self.assertEqual(handoff.tracked_bg_futures, ())
            finally:
                release.set()
                try:
                    await asyncio.wait_for(asyncio.shield(worker_future), timeout=1)
                except BaseException:
                    pass
                loop.call_soon_threadsafe(loop.stop)
                thread.join(1)
                if not loop.is_closed():
                    loop.close()

        asyncio.run(scenario())

    def test_start_worker_exits_before_final_bg_drain_and_loop_stop(self):
        async def scenario():
            loop, thread = self._loop_fixture()
            release = threading.Event()
            worker_done = threading.Event()
            cleanup_log = []

            def worker():
                release.wait(1)
                worker_done.set()
                cleanup_log.append('worker_exit')
                channel._cleanup_failed_start()

            worker_future = asyncio.get_running_loop().run_in_executor(None, worker)
            channel = self._channel(loop, thread, worker_future, release, cleanup_log)
            pending = asyncio.run_coroutine_threadsafe(asyncio.sleep(60), loop)
            try:
                result = await shutdown_channel_without_device_flow_close(channel, join_timeout=1)
                self.assertEqual(result, 'PUBLIC_STOP_IDEMPOTENT_NOOP')
                handoff = channel._cfr_bg_ownership_handoff
                self.assertTrue(worker_done.is_set())
                self.assertTrue(handoff.start_worker_exited)
                self.assertTrue(handoff.start_worker_terminal_before_bg_drain)
                self.assertTrue(handoff.final_drain.completed)
                self.assertEqual(handoff.final_drain.tasks_remaining, ())
                self.assertTrue(handoff.captured_loop_stop_allowed)
                self.assertTrue(pending.done())
            finally:
                if not thread.is_alive() and not loop.is_closed():
                    loop.close()

        asyncio.run(scenario())

    def test_start_worker_timeout_fails_closed_without_stopping_captured_loop(self):
        async def scenario():
            loop, thread = self._loop_fixture()
            release = threading.Event()
            worker_future = asyncio.get_running_loop().run_in_executor(None, release.wait, 5)
            channel = self._channel(loop, thread, worker_future, release)
            channel._ws_client.stop = lambda: None
            with self.assertRaisesRegex(RuntimeError, 'START_WORKER_TERMINAL_NOT_PROVEN'):
                await shutdown_channel_without_device_flow_close(channel, join_timeout=0.05)
            handoff = channel._cfr_bg_ownership_handoff
            self.assertTrue(handoff.start_worker_terminal_timed_out)
            self.assertEqual(handoff.producer_quiescence_contract, 'FAIL')
            self.assertTrue(thread.is_alive())
            release.set()
            try:
                await asyncio.wait_for(asyncio.shield(worker_future), timeout=1)
            except BaseException:
                pass
            loop.call_soon_threadsafe(loop.stop)
            thread.join(1)
            if not loop.is_closed():
                loop.close()

        asyncio.run(scenario())

    def test_host_072914_late_sleep_orphan_shape_regression(self):
        async def scenario():
            loop, thread = self._loop_fixture()
            release = threading.Event()
            late_future = []

            class Channel:
                _bg_loop = loop
                _bg_thread = thread
                _bg_tasks = set()
                _bg_tasks_lock = threading.RLock()
                _bot_identity_retry_future = None

                def _cancel_bg_tasks(self):
                    late_future.append(asyncio.run_coroutine_threadsafe(asyncio.sleep(60), self._bg_loop))

                def _stop_bg_loop(self, **_kwargs):
                    return None

            channel = Channel()
            first_drain = drain_bg_loop_before_sdk_stop(channel, timeout=0.2)
            self.assertTrue(first_drain.completed)
            release.set()
            channel._cancel_bg_tasks()
            self.assertTrue(late_future)
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=1)
            task_names = tuple(
                f"{task.get_coro().cr_code.co_filename}:{task.get_coro().cr_code.co_qualname}"
                for task in asyncio.all_tasks(loop)
                if not task.done() and getattr(task.get_coro(), 'cr_code', None) is not None
            )
            self.assertTrue(any(name.endswith(':sleep') and 'asyncio' in name for name in task_names), task_names)
            late_future[0].cancel()
            try:
                late_future[0].result(timeout=1)
            except BaseException:
                pass
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=1)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(1)
            if not loop.is_closed():
                loop.close()

        asyncio.run(scenario())

SdkTaskOwnershipBase = SdkTaskOwnershipTests


class SdkTaskOwnershipTests(SdkTaskOwnershipBase):
    def test_sdk_owned_task_rejects_non_lark_channel_task(self):
        loop = asyncio.new_event_loop()
        event = asyncio.Event()

        async def unrelated_task():
            await event.wait()

        task = loop.create_task(unrelated_task())
        try:
            self.assertFalse(_sdk_owned_task(task))
        finally:
            task.cancel()
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            loop.close()

    def test_running_bg_loop_terminal_drain_bypasses_sdk_cancel_helper(self):
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run_loop():
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=run_loop, name='test-cfr-bg-loop')
        thread.start()
        ready.wait(1)
        future = asyncio.run_coroutine_threadsafe(asyncio.sleep(30), loop)
        cancel_calls = []

        class Channel:
            _bg_loop = loop
            _bg_thread = thread
            _bg_tasks = {future}
            _bg_tasks_lock = threading.RLock()
            _bot_identity_retry_future = None
            _stop_bg_loop = staticmethod(lambda **_kwargs: cancel_calls.append('stop'))

        try:
            with patch('cfr.feishu.sdk_compat.asyncio.run_coroutine_threadsafe', side_effect=AssertionError('sleep barrier used')):
                result = drain_bg_loop_before_sdk_stop(Channel(), timeout=1)
            self.assertTrue(result.completed)
            self.assertEqual(result.mechanism, 'CFR_LOOP_CALLBACK_TERMINAL_DRAIN')
            self.assertEqual(result.tasks_remaining, ())
            self.assertTrue(future.cancelled() or future.done())
            self.assertEqual(cancel_calls, [])
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(1)
            loop.close()

    def test_running_bg_loop_cancellation_resistant_task_reaches_terminal(self):
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        started = threading.Event()
        cancellations = []

        async def resistant():
            started.set()
            while True:
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    cancellations.append(True)
                    if len(cancellations) >= 2:
                        raise

        def run_loop():
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=run_loop, name='test-cfr-bg-loop-resistant')
        thread.start()
        ready.wait(1)
        future = asyncio.run_coroutine_threadsafe(resistant(), loop)
        started.wait(1)

        class Channel:
            _bg_loop = loop
            _bg_thread = thread
            _bg_tasks = set()
            _bg_tasks_lock = threading.RLock()
            _bot_identity_retry_future = None
            _stop_bg_loop = staticmethod(lambda **_kwargs: None)

        try:
            result = drain_bg_loop_before_sdk_stop(Channel(), timeout=1)
            self.assertTrue(result.completed)
            self.assertTrue(result.loop_stop_allowed)
            self.assertGreaterEqual(len(cancellations), 2)
        finally:
            loop.call_soon_threadsafe(lambda: [task.cancel() for task in asyncio.all_tasks(loop)])
            loop.call_soon_threadsafe(lambda: loop.call_later(0.05, loop.stop))
            thread.join(1)
            loop.close()

    def test_start_worker_handoff_shape_guard_fails_closed(self):
        class Channel:
            _shutdown = threading.Event()
            _bg_loop = object()
            _bg_thread = None
            _bg_tasks = set()
            _bg_tasks_lock = threading.RLock()
            _bot_identity_retry_future = None
            _stop_bg_loop_calls = 0

            def _stop_bg_loop(self, **_kwargs):
                self._stop_bg_loop_calls += 1

        channel = Channel()
        with self.assertRaisesRegex(RuntimeError, 'FEISHU_SDK_START_WORKER_HANDOFF_SHAPE_UNSUPPORTED'):
            stop_channel_without_device_flow_close(channel)

    def test_sdk_task_snapshot_counts_sdk_task(self):
        loop = asyncio.new_event_loop()
        module = _sdk_module()
        task = loop.create_task(module.fake_sdk_ping(asyncio.Event()))
        try:
            snapshot = snapshot_sdk_tasks(loop)
            self.assertEqual(snapshot.total, 1)
            self.assertEqual(snapshot.ws, 1)
            self.assertEqual(snapshot.tasks, ('lark_channel.test_probe.fake_sdk_ping',))
        finally:
            task.cancel()
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            loop.close()

    def test_sdk_task_drain_cancels_sdk_task(self):
        loop = asyncio.new_event_loop()
        module = _sdk_module()
        task = loop.create_task(module.fake_sdk_ping(asyncio.Event()))
        try:
            diagnostic = drain_sdk_tasks(loops=[loop])
            self.assertEqual(diagnostic.sdk_ws_tasks_before, 1)
            self.assertEqual(diagnostic.sdk_ws_tasks_drained, 1)
            self.assertEqual(diagnostic.sdk_ws_tasks_remaining, 0)
            self.assertEqual(diagnostic.compatibility_mode, 'SDK_TASK_DRAIN_REQUIRED')
            self.assertTrue(task.done())
        finally:
            if not task.done():
                task.cancel()
                loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            loop.close()

    def test_sdk_task_drain_does_not_cancel_non_sdk_task(self):
        loop = asyncio.new_event_loop()
        module = _sdk_module()
        sdk_task = loop.create_task(module.fake_sdk_ping(asyncio.Event()))

        async def unrelated_task():
            await asyncio.Event().wait()

        unrelated = loop.create_task(unrelated_task())
        try:
            diagnostic = drain_sdk_tasks(loops=[loop])
            self.assertEqual(diagnostic.sdk_ws_tasks_remaining, 0)
            self.assertFalse(unrelated.done())
        finally:
            for task in (sdk_task, unrelated):
                if not task.done():
                    task.cancel()
            loop.run_until_complete(asyncio.gather(sdk_task, unrelated, return_exceptions=True))
            loop.close()

    def test_sdk_task_drain_reports_zero_remaining(self):
        loop = asyncio.new_event_loop()
        module = _sdk_module()
        task = loop.create_task(module.fake_sdk_ping(asyncio.Event()))
        try:
            diagnostic = drain_sdk_tasks(loops=[loop])
            self.assertEqual(diagnostic.remaining_task_names, ())
            self.assertFalse(diagnostic.blocking_issues)
        finally:
            if not task.done():
                task.cancel()
                loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            loop.close()

    def test_captured_ping_task_is_drained_on_its_owner_loop(self):
        loop = asyncio.new_event_loop()
        module = _sdk_module()
        task = loop.create_task(module.fake_sdk_ping(asyncio.Event()))
        capture = ChannelSdkShutdownCapture(
            ws_loop=loop,
            captured_ws_tasks=(task,),
            ws_client_captured=True,
            ws_loop_captured=True,
            ws_tasks_before_public_stop=('lark_channel.test_probe.fake_sdk_ping',),
        )
        try:
            diagnostic = drain_sdk_tasks(loops=[loop], capture=capture)
            self.assertEqual(diagnostic.sdk_ws_tasks_before, 1)
            self.assertEqual(diagnostic.sdk_ws_tasks_remaining, 0)
            self.assertEqual(diagnostic.ws_tasks_after_public_stop, ('lark_channel.test_probe.fake_sdk_ping',))
        finally:
            if not task.done():
                task.cancel()
                loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            loop.close()

    def test_cache_cron_is_drained_on_distinct_orphan_loop(self):
        ws_loop = asyncio.new_event_loop()
        cache_loop = asyncio.new_event_loop()
        module = ModuleType('lark_channel.core.cache.expiring_cache')
        exec(
            'async def clear_cron(event):\n'
            '    await event.wait()\n',
            module.__dict__,
        )
        cron = cache_loop.create_task(module.clear_cron(asyncio.Event()))
        capture = ChannelSdkShutdownCapture(
            ws_loop=ws_loop,
            cache_loop=cache_loop,
            cache_cron_task=cron,
            cache_cron_captured=True,
            cache_cron_task_done_before_shutdown=False,
            cache_loop_running_before_shutdown=False,
            cache_loop_closed_before_shutdown=False,
            cache_loop_same_as_ws_loop=False,
            cache_tasks_before_public_stop=('lark_channel.core.cache.expiring_cache.clear_cron',),
        )
        try:
            diagnostic = drain_sdk_tasks(loops=[ws_loop, cache_loop], capture=capture)
            self.assertEqual(diagnostic.sdk_cache_tasks_before, 1)
            self.assertEqual(diagnostic.sdk_cache_tasks_remaining, 0)
            self.assertTrue(diagnostic.cache_loop_closed_by_cfr)
            self.assertFalse(diagnostic.cache_task_observation_available is False)
        finally:
            if not ws_loop.is_closed():
                ws_loop.close()
            if not cache_loop.is_closed():
                cache_loop.close()

    def test_captured_foreign_cache_cron_is_drained_by_exact_reference(self):
        cache_loop = asyncio.new_event_loop()
        module = ModuleType('foreign.cache')
        exec(
            'async def cron(event):\n'
            '    await event.wait()\n',
            module.__dict__,
        )
        cron = cache_loop.create_task(module.cron(asyncio.Event()))
        capture = ChannelSdkShutdownCapture(
            cache_loop=cache_loop,
            cache_cron_task=cron,
            cache_cron_captured=True,
            cache_cron_task_done_before_shutdown=False,
            cache_loop_running_before_shutdown=False,
            cache_loop_closed_before_shutdown=False,
            cache_loop_same_as_ws_loop=False,
            cache_tasks_before_public_stop=('foreign.cache.cron',),
        )
        diagnostic = drain_sdk_tasks(loops=[cache_loop], capture=capture)
        self.assertTrue(cron.done())
        self.assertEqual(diagnostic.sdk_cache_tasks_before, 1)
        self.assertEqual(diagnostic.sdk_cache_tasks_drained, 1)
        self.assertEqual(diagnostic.sdk_cache_tasks_remaining, 0)
        self.assertTrue(diagnostic.cache_loop_closed_by_cfr)

    def test_cache_foreign_running_loop_uses_threadsafe_drain(self):
        cache_loop = asyncio.new_event_loop()
        ready = threading.Event()
        thread = None

        def run_loop():
            asyncio.set_event_loop(cache_loop)
            ready.set()
            cache_loop.run_forever()

        thread = threading.Thread(target=run_loop, name='foreign-cache-owner')
        thread.start()
        self.assertTrue(ready.wait(1))

        async def cron(event):
            await event.wait()

        async def create_task():
            return asyncio.create_task(cron(asyncio.Event()))

        task = asyncio.run_coroutine_threadsafe(create_task(), cache_loop).result(timeout=1)
        capture = ChannelSdkShutdownCapture(
            cache_loop=cache_loop,
            cache_cron_task=task,
            cache_cron_captured=True,
            cache_cron_task_done_before_shutdown=False,
            cache_loop_running_before_shutdown=True,
            cache_loop_closed_before_shutdown=False,
            cache_loop_same_as_ws_loop=False,
        )
        diagnostic = drain_sdk_tasks(loops=[cache_loop], capture=capture)
        self.assertTrue(task.done())
        self.assertEqual(diagnostic.sdk_cache_tasks_remaining, 0)
        cache_loop.call_soon_threadsafe(cache_loop.stop)
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        cache_loop.close()

    def test_capture_survives_channel_sdk_reference_clear(self):
        loop = asyncio.new_event_loop()
        module = _sdk_module()
        task = loop.create_task(module.fake_sdk_ping(asyncio.Event()))

        class Cache:
            _cron = task

        class Ws:
            _loop = loop
            _cache = Cache()

        class Channel:
            _start_future = None
            _ws_client = Ws()

            @property
            def ws_client(self):
                return self._ws_client

        channel = Channel()
        try:
            capture = capture_channel_sdk_shutdown_targets(channel)
            channel._ws_client = None
            diagnostic = drain_sdk_tasks(loops=[loop], capture=capture)
            self.assertTrue(capture.ws_client_captured)
            self.assertEqual(diagnostic.sdk_ws_tasks_remaining, 0)
        finally:
            if not task.done():
                task.cancel()
                loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            loop.close()

    def test_closed_cache_loop_reports_unavailable_instead_of_zero(self):
        cache_loop = asyncio.new_event_loop()
        cache_loop.close()
        capture = ChannelSdkShutdownCapture(
            cache_loop=cache_loop,
            cache_cron_captured=True,
            cache_cron_task_done_before_shutdown=False,
            cache_loop_closed_before_shutdown=True,
            cache_tasks_before_public_stop=('lark_channel.core.cache.expiring_cache.clear_cron',),
        )
        try:
            diagnostic = drain_sdk_tasks(capture=capture, loops=[cache_loop])
            self.assertFalse(diagnostic.cache_task_observation_available)
            self.assertIsNone(diagnostic.sdk_cache_tasks_remaining)
            self.assertIn('SDK_CACHE_LOOP_CLOSED_WITH_PENDING_TASK', diagnostic.blocking_issues)
        finally:
            pass

    def test_dedicated_bg_loop_drains_orphan_sleep_after_thread_exit(self):
        bg_loop = asyncio.new_event_loop()
        thread_ready = threading.Event()

        def run_bg_loop():
            asyncio.set_event_loop(bg_loop)
            thread_ready.set()
            bg_loop.run_forever()

        thread = threading.Thread(target=run_bg_loop, name='test-channel-bg')
        thread.start()
        self.assertTrue(thread_ready.wait(1))

        orphan_future = asyncio.run_coroutine_threadsafe(asyncio.sleep(60), bg_loop)
        time.sleep(0.05)
        captured_tasks = tuple(asyncio.all_tasks(bg_loop))
        bg_loop.call_soon_threadsafe(bg_loop.stop)
        thread.join(1)
        try:
            self.assertFalse(thread.is_alive())
            result = drain_dedicated_channel_bg_loop(bg_loop, timeout=1, captured_tasks=captured_tasks)
            self.assertTrue(result.observation_available)
            self.assertIn('asyncio.tasks.sleep', result.tasks_before)
            self.assertEqual(result.tasks_remaining, ())
            self.assertTrue(result.loop_closed_by_cfr)
            self.assertTrue(orphan_future.done())
        finally:
            if not bg_loop.is_closed():
                bg_loop.close()

    def test_dedicated_bg_loop_does_not_touch_cfr_or_ws_loops(self):
        cfr_loop = asyncio.new_event_loop()
        ws_loop = asyncio.new_event_loop()
        bg_loop = asyncio.new_event_loop()
        bg_loop.call_soon(bg_loop.stop)
        bg_loop.run_forever()
        async def cfr_task():
            await asyncio.Event().wait()
        cfr_pending = cfr_loop.create_task(cfr_task())
        async def ws_task():
            await asyncio.Event().wait()
        ws_pending = ws_loop.create_task(ws_task())
        try:
            result = drain_dedicated_channel_bg_loop(bg_loop, timeout=1)
            self.assertTrue(result.observation_available)
            self.assertFalse(cfr_pending.done())
            self.assertFalse(ws_pending.done())
        finally:
            for loop, task in ((cfr_loop, cfr_pending), (ws_loop, ws_pending)):
                task.cancel()
                loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
                loop.close()
            if not bg_loop.is_closed():
                bg_loop.close()

    def test_upstream_closed_bg_loop_is_terminal_clean_close_evidence(self):
        bg_loop = asyncio.new_event_loop()
        bg_loop.close()
        capture = ChannelSdkShutdownCapture(
            bg_loop=bg_loop,
            bg_thread=threading.Thread(name='channel-bg'),
            bg_loop_captured=True,
            bg_thread_captured=True,
            bg_loop_running_before_public_stop=True,
            bg_loop_closed_before_public_stop=False,
            bg_thread_alive_before_public_stop=True,
        )
        diagnostic = drain_sdk_tasks(loops=[], capture=capture)
        self.assertEqual(diagnostic.bg_loop_closure_state, 'CLOSED_BY_UPSTREAM_CLEANLY')
        self.assertEqual(diagnostic.bg_task_observation_status, 'CLOSED_CLEANLY_BY_UPSTREAM')
        self.assertIsNone(diagnostic.bg_tasks_remaining)
        self.assertFalse(diagnostic.blocking_issues)

    def test_closed_bg_loop_with_live_thread_is_unclean(self):
        class LiveThread:
            def is_alive(self):
                return True
        bg_loop = asyncio.new_event_loop()
        bg_loop.close()
        capture = ChannelSdkShutdownCapture(
            bg_loop=bg_loop,
            bg_thread=LiveThread(),
            bg_loop_captured=True,
            bg_thread_captured=True,
            bg_loop_closed_before_public_stop=False,
        )
        diagnostic = drain_sdk_tasks(loops=[], capture=capture)
        self.assertEqual(diagnostic.bg_loop_closure_state, 'CLOSED_UNCLEANLY')
        self.assertIn('CHANNEL_BG_LOOP_CLOSED_WITH_LIVE_THREAD', diagnostic.blocking_issues)

    def test_bg_loop_closed_before_public_stop_is_unclean(self):
        bg_loop = asyncio.new_event_loop()
        bg_loop.close()
        capture = ChannelSdkShutdownCapture(
            bg_loop=bg_loop,
            bg_thread=threading.Thread(name='channel-bg'),
            bg_loop_captured=True,
            bg_thread_captured=True,
            bg_loop_closed_before_public_stop=True,
        )
        diagnostic = drain_sdk_tasks(loops=[], capture=capture)
        self.assertEqual(diagnostic.bg_loop_closure_state, 'CLOSED_UNCLEANLY')
        self.assertIn('CHANNEL_BG_LOOP_CLOSED_BEFORE_PUBLIC_STOP', diagnostic.blocking_issues)

    def test_observed_bg_loop_drain_remains_cfr_close_path(self):
        bg_loop = asyncio.new_event_loop()
        bg_loop.call_soon(bg_loop.stop)
        bg_loop.run_forever()
        capture = ChannelSdkShutdownCapture(
            bg_loop=bg_loop,
            bg_thread=threading.Thread(name='channel-bg'),
            bg_loop_captured=True,
            bg_thread_captured=True,
            bg_loop_running_before_public_stop=True,
            bg_loop_closed_before_public_stop=False,
            bg_thread_alive_before_public_stop=True,
        )
        diagnostic = drain_sdk_tasks(loops=[], capture=capture)
        self.assertEqual(diagnostic.bg_loop_closure_state, 'CLOSED_BY_CFR')
        self.assertEqual(diagnostic.bg_task_observation_status, 'OBSERVED')
        self.assertEqual(diagnostic.bg_tasks_remaining, 0)

    def _device_flow_loop_fixture(self, device_flow):
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=run, name='device-flow-bg')
        thread.start()
        self.assertTrue(ready.wait(1))
        capture = ChannelSdkShutdownCapture(
            bg_loop=loop,
            bg_thread=thread,
            bg_loop_captured=True,
            bg_thread_captured=True,
            bg_loop_running_before_public_stop=True,
            bg_loop_closed_before_public_stop=False,
            bg_thread_alive_before_public_stop=True,
            device_flow=device_flow,
            device_flow_captured=True,
            device_flow_http_present_before=getattr(device_flow, '_http', None) is not None,
            device_flow_http_owned_before=getattr(device_flow, '_owns_http', None),
            device_flow_http_closed_before=None,
        )
        return loop, thread, capture

    def _stop_device_flow_loop(self, loop, thread):
        loop.call_soon_threadsafe(loop.stop)
        thread.join(1)
        if not loop.is_closed():
            loop.close()

    def test_device_flow_preclose_completes_on_captured_bg_loop(self):
        class Http:
            is_closed = False
        class DeviceFlow:
            _http = Http()
            _owns_http = True
            async def close(self):
                await asyncio.sleep(0.01)
                self._http = None
        device = DeviceFlow()
        loop, thread, capture = self._device_flow_loop_fixture(device)
        try:
            from cfr.feishu.sdk_compat import preclose_device_flow_before_public_shutdown
            result = preclose_device_flow_before_public_shutdown(capture, timeout=1)
            self.assertTrue(result.scheduled)
            self.assertTrue(result.completed)
            self.assertFalse(result.timed_out)
            self.assertIsNone(device._http)
            self.assertEqual(result.compatibility_mode, 'PRECLOSE_OWNED_HTTP')
        finally:
            self._stop_device_flow_loop(loop, thread)

    def test_device_flow_preclose_drives_open_nonrunning_owner_loop(self):
        class DeviceFlow:
            _http = object()
            _owns_http = True
            async def close(self): self._http = None

        loop = asyncio.new_event_loop()
        device = DeviceFlow()
        capture = ChannelSdkShutdownCapture(
            bg_loop=loop,
            bg_thread=threading.Thread(name='device-flow-nonrunning-owner'),
            bg_loop_captured=True,
            bg_thread_captured=True,
            bg_loop_running_before_public_stop=False,
            bg_thread_alive_before_public_stop=False,
            device_flow=device,
            device_flow_captured=True,
            device_flow_http_present_before=True,
            device_flow_http_owned_before=True,
        )
        try:
            from cfr.feishu.sdk_compat import preclose_device_flow_before_public_shutdown
            result = preclose_device_flow_before_public_shutdown(capture, timeout=1)
            self.assertTrue(result.completed)
            self.assertEqual(result.compatibility_mode, 'PRECLOSE_DRIVE_NONRUNNING_LOOP')
            self.assertIsNone(device._http)
        finally:
            if not loop.is_closed():
                loop.close()

    def test_device_flow_preclose_timeout_is_bounded_and_cancels_future(self):
        class DeviceFlow:
            _http = object()
            _owns_http = True
            async def close(self):
                await asyncio.Event().wait()
        device = DeviceFlow()
        loop, thread, capture = self._device_flow_loop_fixture(device)
        try:
            from cfr.feishu.sdk_compat import preclose_device_flow_before_public_shutdown
            result = preclose_device_flow_before_public_shutdown(capture, timeout=0.05)
            self.assertTrue(result.timed_out)
            self.assertTrue(result.cancelled_after_timeout)
            self.assertTrue(result.cancellation_observed)
            self.assertEqual(result.error_code, 'DEVICE_FLOW_PRECLOSE_TIMEOUT')
        finally:
            self._stop_device_flow_loop(loop, thread)

    def test_device_flow_preclose_scheduling_failure_closes_coroutine(self):
        class DeviceFlow:
            _http = object()
            _owns_http = True
            async def close(self):
                await asyncio.sleep(0)
        class RunningLoop:
            def is_closed(self): return False
            def is_running(self): return True
        class LiveThread:
            def is_alive(self): return True
        capture = ChannelSdkShutdownCapture(
            bg_loop=RunningLoop(), bg_thread=LiveThread(), bg_loop_captured=True, bg_thread_captured=True,
            bg_loop_running_before_public_stop=True, bg_thread_alive_before_public_stop=True,
            device_flow=DeviceFlow(), device_flow_captured=True, device_flow_http_present_before=True,
            device_flow_http_owned_before=True,
        )
        from cfr.feishu.sdk_compat import preclose_device_flow_before_public_shutdown
        with patch('cfr.feishu.sdk_compat.asyncio.run_coroutine_threadsafe', side_effect=RuntimeError('closed race')):
            result = preclose_device_flow_before_public_shutdown(capture, timeout=1)
        self.assertFalse(result.scheduled)
        self.assertIn('DEVICE_FLOW_PRECLOSE_SCHEDULE_FAILED', result.error_code)

    def test_device_flow_preclose_then_public_close_is_idempotent(self):
        class DeviceFlow:
            _http = None
            _owns_http = True
            def __init__(self): self.calls = 0
            async def close(self): self.calls += 1
        device = DeviceFlow()
        loop, thread, capture = self._device_flow_loop_fixture(device)
        try:
            from cfr.feishu.sdk_compat import preclose_device_flow_before_public_shutdown
            first = preclose_device_flow_before_public_shutdown(capture, timeout=1)
            second = preclose_device_flow_before_public_shutdown(capture, timeout=1)
            self.assertTrue(first.completed and second.completed)
            self.assertEqual(device.calls, 2)
        finally:
            self._stop_device_flow_loop(loop, thread)

    def test_public_stop_compatibility_bridge_skips_second_device_flow_close(self):
        class DeviceFlow:
            def __init__(self): self.calls = 0
            async def close(self): self.calls += 1

        class Ws:
            def __init__(self): self.calls = 0
            def stop(self): self.calls += 1

        class Channel:
            def __init__(self):
                self._shutdown = threading.Event()
                self._start_future = None
                self._lifecycle_lock = threading.RLock()
                self._background_generation = 0
                self._lifecycle_generation = 0
                self._stop_requested = threading.Event()
                self._ws_client = Ws()
                self._bg_tasks_lock = threading.RLock()
                self._bg_tasks = set()
                self._bg_lock = threading.RLock()
                self._bg_loop = None
                self._bg_thread = None
                self._bot_identity_retry_future = None
                self._started = True
                self._ready_flag = True
                self._connection_state = 'connected'
                self._connection_last_disconnected_at = None
                self._ready_event = None
                self.device_flow = DeviceFlow()
                self.bg_loop_stop_calls = 0
                self.watchdog_stop_calls = 0
                self.cancel_calls = 0

            def _stop_keepalive_watchdog(self): self.watchdog_stop_calls += 1
            def _cancel_bg_tasks(self): self.cancel_calls += 1
            def _stop_bg_loop(self, *, join_timeout): self.bg_loop_stop_calls += 1

        channel = Channel()
        result = stop_channel_without_device_flow_close(channel, join_timeout=1)
        self.assertEqual(result, 'PUBLIC_STOP_IDEMPOTENT_NOOP')
        self.assertEqual(channel.device_flow.calls, 0)
        self.assertEqual(channel._ws_client, None)
        self.assertEqual(channel.bg_loop_stop_calls, 0)
        self.assertEqual(channel.cancel_calls, 0)
        self.assertFalse(channel._shutdown.is_set())


if __name__ == '__main__':
    unittest.main()
