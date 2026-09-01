"""Small, feature-detected compatibility boundary for the Channel SDK runtime."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import concurrent.futures
import importlib
import importlib.metadata
import inspect
import threading
import time
from types import ModuleType
from typing import Any, Iterable


@dataclass(frozen=True)
class ChannelSdkLoopDiagnostic:
    module_name: str
    loop_present: bool
    loop_running: bool
    loop_closed: bool
    compatibility_mode: str


@dataclass(frozen=True)
class SdkTaskSnapshot:
    total: int = 0
    ws: int = 0
    cache: int = 0
    device_flow: int = 0
    tasks: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeviceFlowPreCloseResult:
    observation_available: bool
    attempted: bool
    scheduled: bool
    completed: bool
    timed_out: bool
    cancelled_after_timeout: bool
    cancellation_observed: bool
    http_present_before: bool | None
    http_present_after: bool | None
    http_closed_before: bool | None
    http_closed_after: bool | None
    elapsed_ms: int | None
    error_code: str | None
    compatibility_mode: str
    future: object | None = None


@dataclass(frozen=True)
class BgPreStopDrainResult:
    attempted: bool = False
    mechanism: str = 'NOT_RUN'
    loop_running: bool | None = None
    thread_alive: bool | None = None
    tracked_future_count: int = 0
    tracked_futures_cancelled: int = 0
    tasks_before: tuple[str, ...] = ()
    tasks_cancelled: int = 0
    tasks_remaining: tuple[str, ...] = ()
    completed: bool = False
    timed_out: bool = False
    terminal_evidence: str = 'NOT_RUN'
    loop_stop_allowed: bool = False
    error_code: str | None = None


@dataclass
class ChannelBgOwnershipHandoff:
    """CFR's terminal ownership of the SDK background loop.

    The installed SDK keeps start-worker cleanup authority on the channel
    object.  CFR must retain strong references while removing those fields
    from the channel before releasing the blocking start worker.
    """

    attempted: bool = False
    completed: bool = False
    scheduling_blocked_before_detach: bool = False
    detached_from_sdk: bool = False
    bg_loop: asyncio.AbstractEventLoop | None = None
    bg_thread: threading.Thread | None = None
    tracked_bg_futures: tuple[object, ...] = ()
    bot_identity_retry_future: object | None = None
    start_future: object | None = None
    ws_client: object | None = None
    shape_version: str = 'NOT_OBSERVED'
    start_future_wrapper_cancelled: bool = False
    start_worker_terminal_wait_attempted: bool = False
    start_worker_exited: bool = False
    start_worker_terminal_timed_out: bool = False
    start_worker_terminal_evidence: str = 'NOT_RUN'
    producer_quiescence_contract: str = 'NOT_RUN'
    late_bg_task_created_after_handoff: bool = False
    start_worker_terminal_before_bg_drain: bool = False
    final_drain: BgPreStopDrainResult | None = None
    captured_loop_stop_allowed: bool = False
    captured_thread_exited: bool | None = None
    captured_loop_closed: bool | None = None


@dataclass(frozen=True)
class ChannelSdkShutdownCapture:
    ws_client: object | None = None
    start_future: object | None = None
    ws_loop: asyncio.AbstractEventLoop | None = None
    cache_cron_task: asyncio.Task | None = None
    cache_loop: asyncio.AbstractEventLoop | None = None
    reconnect_task: asyncio.Task | None = None
    captured_ws_tasks: tuple[asyncio.Task, ...] = ()
    captured_at: float = 0.0
    ws_client_captured: bool = False
    ws_loop_captured: bool = False
    cache_cron_captured: bool = False
    cache_cron_task_done_before_shutdown: bool | None = None
    cache_loop_running_before_shutdown: bool | None = None
    cache_loop_closed_before_shutdown: bool | None = None
    cache_loop_same_as_ws_loop: bool | None = None
    ws_loop_running_before_public_stop: bool | None = None
    ws_loop_closed_before_public_stop: bool | None = None
    ws_tasks_before_public_stop: tuple[str, ...] = ()
    cache_tasks_before_public_stop: tuple[str, ...] = ()
    bg_loop: asyncio.AbstractEventLoop | None = None
    bg_thread: threading.Thread | None = None
    captured_bg_tasks: tuple[asyncio.Task, ...] = ()
    captured_bg_retry_future: object | None = None
    bg_loop_captured: bool = False
    bg_thread_captured: bool = False
    bg_loop_running_before_public_stop: bool | None = None
    bg_loop_closed_before_public_stop: bool | None = None
    bg_thread_alive_before_public_stop: bool | None = None
    bg_tasks_before_public_stop: tuple[str, ...] = ()
    device_flow: object | None = None
    device_flow_captured: bool = False
    device_flow_http_present_before: bool | None = None
    device_flow_http_owned_before: bool | None = None
    device_flow_http_closed_before: bool | None = None


@dataclass(frozen=True)
class SdkShutdownDiagnostic:
    compatibility_mode: str = 'NOT_REQUIRED'
    device_flow_close_scheduled: bool = False
    device_flow_close_completed: str = 'NOT_OBSERVABLE'
    sdk_ws_tasks_before: int | None = 0
    sdk_ws_tasks_drained: int | None = 0
    sdk_ws_tasks_remaining: int | None = 0
    sdk_cache_tasks_before: int | None = 0
    sdk_cache_tasks_drained: int | None = 0
    sdk_cache_tasks_remaining: int | None = 0
    sdk_device_flow_tasks_before: int = 0
    sdk_device_flow_tasks_drained: int = 0
    sdk_device_flow_tasks_remaining: int = 0
    remaining_task_names: tuple[str, ...] = ()
    async_generators_shutdown: bool = False
    blocking_issues: tuple[str, ...] = ()
    pre_shutdown_capture: str = 'NOT_RUN'
    start_future_captured: bool = False
    start_future_exited: bool = False
    ws_client_captured: bool = False
    ws_loop_captured: bool = False
    ws_loop_running_before_public_stop: bool | None = None
    ws_loop_closed_before_public_stop: bool | None = None
    ws_tasks_before_public_stop: tuple[str, ...] = ()
    ws_tasks_after_public_stop: tuple[str, ...] = ()
    cache_cron_captured: bool = False
    cache_loop_running_before: bool | None = None
    cache_loop_closed_before: bool | None = None
    cache_loop_same_as_ws_loop: bool | None = None
    cache_cron_task_done_before: bool | None = None
    cache_loop_closed_by_cfr: bool = False
    ws_task_observation_available: bool = True
    cache_task_observation_available: bool = True
    bg_loop_captured: bool = False
    bg_thread_captured: bool = False
    bg_loop_running_before_public_stop: bool | None = None
    bg_loop_closed_before_public_stop: bool | None = None
    bg_thread_alive_before_public_stop: bool | None = None
    bg_thread_exited: bool | None = None
    bg_task_observation_available: bool = True
    bg_tasks_before_public_stop: tuple[str, ...] = ()
    bg_tasks_after_public_stop: tuple[str, ...] = ()
    bg_tasks_drained: int | None = 0
    bg_tasks_remaining: int | None = 0
    bg_remaining_task_names: tuple[str, ...] = ()
    bg_async_generators_shutdown: str = 'NOT_REQUIRED'
    bg_default_executor_shutdown: str = 'NOT_REQUIRED'
    bg_loop_closed_by_cfr: bool = False
    bg_loop_closed_after_public_stop: bool | None = None
    bg_loop_closure_state: str = 'NOT_CAPTURED'
    bg_task_observation_status: str = 'NOT_REQUIRED'
    bg_task_terminal_evidence: str = 'NOT_YET_EVALUATED'
    device_flow_captured: bool = False
    device_flow_http_present_before: bool | None = None
    device_flow_http_owned_before: bool | None = None
    device_flow_http_closed_before: bool | None = None
    device_flow_preclose_attempted: bool = False
    device_flow_preclose_scheduled: bool = False
    device_flow_preclose_completed: bool = False
    device_flow_preclose_timed_out: bool = False
    device_flow_preclose_cancelled_after_timeout: bool = False
    device_flow_preclose_cancellation_observed: bool = False
    device_flow_preclose_elapsed_ms: int | None = None
    device_flow_http_present_after: bool | None = None
    device_flow_http_closed_after: bool | None = None
    device_flow_preclose_error_code: str | None = None
    device_flow_shutdown_compatibility_mode: str = 'NOT_REQUIRED'
    device_flow_object_observed: bool = False
    device_flow_object_same_across_preclose_and_public_stop: bool | None = None
    device_flow_close_invocation_count_observed: bool = False
    device_flow_preclose_invocation_count: int | None = None
    device_flow_public_stop_close_invocation_count: int | None = None
    device_flow_close_kind: str = 'NOT_OBSERVED'
    device_flow_close_coroutine_created: bool = False
    device_flow_close_coroutine_scheduled: bool = False
    device_flow_close_task_captured: bool = False
    device_flow_close_task_done: bool = False
    device_flow_close_task_cancelled: bool = False
    device_flow_close_awaited_to_terminal: bool = False
    device_flow_raw_coroutine_leak: bool | None = None
    device_flow_close_owner_loop_observed: bool = False
    device_flow_close_owner_loop_running: bool | None = None
    device_flow_close_owner_loop_closed: bool | None = None
    device_flow_close_awaited: bool = False
    device_flow_close_done: bool = False
    device_flow_close_cancelled: bool = False
    device_flow_close_timed_out: bool = False
    device_flow_close_terminal_evidence: str = 'NOT_OBSERVED'
    device_flow_close_cleanup_mechanism: str = 'NOT_OBSERVED'
    device_flow_owner_loop_is_ws_loop: bool = False
    device_flow_owner_loop_is_bg_loop: bool = False
    device_flow_owner_loop_is_cache_loop: bool = False
    device_flow_owner_loop_is_transport_loop: bool = False
    bg_pre_stop_drain_attempted: bool = False
    bg_pre_stop_drain_mechanism: str = 'NOT_RUN'
    bg_pre_stop_loop_running: bool | None = None
    bg_pre_stop_thread_alive: bool | None = None
    bg_pre_stop_tracked_future_count: int = 0
    bg_pre_stop_tracked_futures_cancelled: int = 0
    bg_pre_stop_tasks_before: tuple[str, ...] = ()
    bg_pre_stop_tasks_cancelled: int = 0
    bg_pre_stop_tasks_remaining: tuple[str, ...] = ()
    bg_pre_stop_drain_completed: bool = False
    bg_pre_stop_drain_timed_out: bool = False
    bg_pre_stop_terminal_evidence: str = 'NOT_RUN'
    bg_pre_stop_loop_stop_allowed: bool = False
    bg_pre_stop_error_code: str | None = None
    start_future_wrapper_cancelled: bool = False
    start_worker_terminal_authority: str = 'NOT_OBSERVED'
    start_worker_terminal_wait_attempted: bool = False
    start_worker_exited: bool = False
    start_worker_terminal_timed_out: bool = False
    start_worker_terminal_evidence: str = 'NOT_RUN'
    bg_ownership_handoff_attempted: bool = False
    bg_scheduling_blocked_before_detach: bool = False
    bg_ownership_handoff_completed: bool = False
    bg_ownership_detached_from_sdk: bool = False
    bg_producer_quiescence_contract: str = 'NOT_RUN'
    late_bg_task_created_after_handoff: bool = False
    start_worker_terminal_before_bg_drain: bool = False
    bg_final_drain_attempted: bool = False
    bg_final_drain_tasks_before: tuple[str, ...] = ()
    bg_final_drain_tasks_remaining: tuple[str, ...] = ()
    bg_final_drain_terminal_evidence: str = 'NOT_RUN'
    bg_captured_loop_stop_allowed: bool = False
    host_072914_late_sleep_race_regression: str = 'NOT_RUN'


_CONFLICT_MARKERS = (
    'this event loop is already running',
    'cannot run the event loop while another loop is running',
)


def classify_channel_error(error: BaseException) -> str | None:
    text = str(error).lower()
    if any(marker in text for marker in _CONFLICT_MARKERS):
        return 'FEISHU_SDK_EVENT_LOOP_CONFLICT'
    return None


def inspect_channel_sdk_loop(module: ModuleType | None = None) -> ChannelSdkLoopDiagnostic:
    module = module or importlib.import_module('lark_channel.ws.client')
    legacy_loop = getattr(module, 'loop', None)
    if legacy_loop is None:
        return ChannelSdkLoopDiagnostic(module.__name__, False, False, False, 'NOT_REQUIRED')
    return ChannelSdkLoopDiagnostic(
        module_name=module.__name__,
        loop_present=True,
        loop_running=bool(legacy_loop.is_running()),
        loop_closed=bool(legacy_loop.is_closed()),
        compatibility_mode='LEGACY_GLOBAL_LOOP_REBIND' if legacy_loop.is_running() or legacy_loop.is_closed() else 'PREIMPORT_ONLY',
    )


def prepare_channel_sdk_runtime() -> ChannelSdkLoopDiagnostic:
    """Prepare only the SDK's private global reference; never replace CFR's loop."""
    module = importlib.import_module('lark_channel.ws.client')
    diagnostic = inspect_channel_sdk_loop(module)
    legacy_loop = getattr(module, 'loop', None)
    if legacy_loop is not None and (legacy_loop.is_running() or legacy_loop.is_closed()):
        module.loop = asyncio.new_event_loop()
        diagnostic = ChannelSdkLoopDiagnostic(
            module_name=module.__name__,
            loop_present=True,
            loop_running=False,
            loop_closed=False,
            compatibility_mode='LEGACY_GLOBAL_LOOP_REBIND',
        )
    return diagnostic


def _coroutine_origin(coro) -> tuple[str, str, str]:
    """Resolve a real coroutine's defining module, qualname, and filename."""
    frame = getattr(coro, 'cr_frame', None)
    if frame is None:
        frame = getattr(coro, 'ag_frame', None)
    module_name = ''
    if frame is not None:
        module_name = str(frame.f_globals.get('__name__') or '')
    if not module_name:
        module_name = str(getattr(coro, '__module__', '') or '')
    code = getattr(coro, 'cr_code', None) or getattr(coro, 'ag_code', None)
    qualname = (
        getattr(coro, '__qualname__', None)
        or getattr(code, 'co_qualname', None)
        or ''
    )
    filename = str(getattr(code, 'co_filename', '') or '')
    return module_name, str(qualname), filename


def _sdk_owned_task(task: asyncio.Task) -> bool:
    """Recognize only real pending tasks defined by the lark_channel package."""
    try:
        module_name, _qualname, filename = _coroutine_origin(task.get_coro())
        if module_name == 'lark_channel' or module_name.startswith('lark_channel.'):
            return True
        normalized = filename.replace('\\', '/').lower()
        return '/site-packages/lark_channel/' in normalized or normalized.endswith('/lark_channel')
    except Exception:
        return False


def _task_name(task: asyncio.Task) -> str:
    module_name, qualname, _filename = _coroutine_origin(task.get_coro())
    return f'{module_name}.{qualname}'.strip('.')


def _task_category(task: asyncio.Task) -> str:
    name = _task_name(task).lower()
    if 'cache' in name or 'cron' in name or 'clear' in name:
        return 'cache'
    if 'deviceflow' in name or 'device_flow' in name or '.auth' in name or 'token' in name:
        return 'device_flow'
    return 'ws'


def snapshot_sdk_tasks(loop: asyncio.AbstractEventLoop, extra_tasks: Iterable[asyncio.Task] = ()) -> SdkTaskSnapshot:
    """Return pending SDK tasks plus explicitly captured lifecycle task references."""
    tasks = [task for task in asyncio.all_tasks(loop) if not task.done() and _sdk_owned_task(task)]
    for task in extra_tasks:
        if not isinstance(task, asyncio.Task) or task.done() or task.get_loop() is not loop:
            continue
        if all(task is not existing for existing in tasks):
            tasks.append(task)
    names = tuple(sorted(_task_name(task) for task in tasks))
    counts = {'ws': 0, 'cache': 0, 'device_flow': 0}
    for task in tasks:
        counts[_task_category(task)] += 1
    return SdkTaskSnapshot(
        total=len(tasks),
        ws=counts['ws'],
        cache=counts['cache'],
        device_flow=counts['device_flow'],
        tasks=names,
    )


def _safe_snapshot(loop: asyncio.AbstractEventLoop | None, extra_tasks: Iterable[asyncio.Task] = ()) -> SdkTaskSnapshot:
    if loop is None or loop.is_closed():
        return SdkTaskSnapshot()
    try:
        return snapshot_sdk_tasks(loop, extra_tasks)
    except Exception:
        return SdkTaskSnapshot()


def _safe_task_refs(loop: asyncio.AbstractEventLoop | None) -> tuple[asyncio.Task, ...]:
    if loop is None or loop.is_closed():
        return ()
    try:
        return tuple(task for task in asyncio.all_tasks(loop) if not task.done() and _sdk_owned_task(task))
    except Exception:
        return ()


def _safe_all_pending_task_refs(loop: asyncio.AbstractEventLoop | None) -> tuple[asyncio.Task, ...]:
    """Capture every pending task only when the loop is a dedicated Channel loop."""
    if loop is None or loop.is_closed():
        return ()
    try:
        return tuple(task for task in asyncio.all_tasks(loop) if not task.done())
    except Exception:
        return ()


def _safe_http_closed(http: object | None) -> bool | None:
    if http is None:
        return None
    try:
        value = getattr(http, 'is_closed', None)
        value = value() if callable(value) else value
        return value if isinstance(value, bool) else None
    except Exception:
        return None


def capture_channel_sdk_shutdown_targets(channel: Any | None) -> ChannelSdkShutdownCapture:
    """Capture SDK ownership references before FeishuChannel.stop clears them."""
    if channel is None:
        return ChannelSdkShutdownCapture(captured_at=time.monotonic())
    try:
        ws = getattr(channel, 'ws_client', None)
    except Exception:
        ws = getattr(channel, '_ws_client', None)
    start_future = getattr(channel, '_start_future', None)
    ws_loop = getattr(ws, '_loop', None) if ws is not None else None
    module_loop = None
    try:
        module = importlib.import_module('lark_channel.ws.client')
        module_loop = getattr(module, 'loop', None)
    except Exception:
        pass
    if ws_loop is None:
        ws_loop = module_loop
    cache = getattr(ws, '_cache', None) if ws is not None else None
    cache_cron = getattr(cache, '_cron', None) if cache is not None else None
    cache_loop = None
    if isinstance(cache_cron, asyncio.Task):
        try:
            cache_loop = cache_cron.get_loop()
        except Exception:
            cache_loop = None
    reconnect_task = getattr(ws, '_reconnect_task', None) if ws is not None else None
    bg_loop = getattr(channel, '_bg_loop', None)
    bg_thread = getattr(channel, '_bg_thread', None)
    ws_snapshot = _safe_snapshot(ws_loop)
    cache_snapshot = _safe_snapshot(
        cache_loop,
        (cache_cron,) if isinstance(cache_cron, asyncio.Task) else (),
    )
    bg_tasks = _safe_all_pending_task_refs(bg_loop)
    device_flow = getattr(channel, '_device_flow', None)
    device_flow_http = getattr(device_flow, '_http', None) if device_flow is not None else None
    device_flow_owned = getattr(device_flow, '_owns_http', None) if device_flow is not None else None
    device_flow_closed = _safe_http_closed(device_flow_http)
    return ChannelSdkShutdownCapture(
        ws_client=ws,
        start_future=start_future,
        ws_loop=ws_loop,
        cache_cron_task=cache_cron if isinstance(cache_cron, asyncio.Task) else None,
        cache_loop=cache_loop,
        reconnect_task=reconnect_task if isinstance(reconnect_task, asyncio.Task) else None,
        captured_ws_tasks=_safe_task_refs(ws_loop),
        captured_at=time.monotonic(),
        ws_client_captured=ws is not None,
        ws_loop_captured=ws_loop is not None,
        cache_cron_captured=isinstance(cache_cron, asyncio.Task),
        cache_cron_task_done_before_shutdown=cache_cron.done() if isinstance(cache_cron, asyncio.Task) else None,
        cache_loop_running_before_shutdown=cache_loop.is_running() if cache_loop is not None else None,
        cache_loop_closed_before_shutdown=cache_loop.is_closed() if cache_loop is not None else None,
        cache_loop_same_as_ws_loop=cache_loop is ws_loop if cache_loop is not None and ws_loop is not None else None,
        ws_loop_running_before_public_stop=ws_loop.is_running() if ws_loop is not None else None,
        ws_loop_closed_before_public_stop=ws_loop.is_closed() if ws_loop is not None else None,
        ws_tasks_before_public_stop=ws_snapshot.tasks,
        cache_tasks_before_public_stop=cache_snapshot.tasks,
        bg_loop=bg_loop,
        bg_thread=bg_thread if isinstance(bg_thread, threading.Thread) else None,
        captured_bg_tasks=bg_tasks,
        captured_bg_retry_future=getattr(channel, '_bot_identity_retry_future', None),
        bg_loop_captured=bg_loop is not None,
        bg_thread_captured=bg_thread is not None,
        bg_loop_running_before_public_stop=bg_loop.is_running() if bg_loop is not None else None,
        bg_loop_closed_before_public_stop=bg_loop.is_closed() if bg_loop is not None else None,
        bg_thread_alive_before_public_stop=bg_thread.is_alive() if isinstance(bg_thread, threading.Thread) else None,
        bg_tasks_before_public_stop=tuple(sorted(_task_name(task) for task in bg_tasks)),
        device_flow=device_flow,
        device_flow_captured=device_flow is not None,
        device_flow_http_present_before=device_flow_http is not None if device_flow is not None else None,
        device_flow_http_owned_before=device_flow_owned if isinstance(device_flow_owned, bool) else None,
        device_flow_http_closed_before=device_flow_closed,
    )


async def _cancel_sdk_tasks(loop: asyncio.AbstractEventLoop, captured_tasks: Iterable[asyncio.Task] = ()) -> tuple[SdkTaskSnapshot, SdkTaskSnapshot, bool]:
    current = asyncio.current_task(loop=loop)
    before = snapshot_sdk_tasks(loop, captured_tasks)
    tasks = [task for task in asyncio.all_tasks(loop) if task is not current and not task.done() and _sdk_owned_task(task)]
    for task in captured_tasks:
        if not isinstance(task, asyncio.Task) or task is current or task.done() or task.get_loop() is not loop:
            continue
        if all(task is not existing for existing in tasks):
            tasks.append(task)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    async_generators_shutdown = True
    try:
        await loop.shutdown_asyncgens()
    except Exception:
        async_generators_shutdown = False
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    after = snapshot_sdk_tasks(loop, captured_tasks)
    return before, after, async_generators_shutdown


def _loop_tasks(
    loop: asyncio.AbstractEventLoop | None,
    timeout: float,
    captured_tasks: Iterable[asyncio.Task] = (),
) -> tuple[SdkTaskSnapshot, SdkTaskSnapshot, bool, str | None]:
    empty = SdkTaskSnapshot()
    captured_tasks = tuple(captured_tasks)

    def post_stop_loop_closed_during_drain() -> tuple[SdkTaskSnapshot, SdkTaskSnapshot, bool, str | None]:
        if any(not task.done() for task in captured_tasks):
            return empty, empty, False, 'SDK_LOOP_CLOSED_WITH_CAPTURED_PENDING_TASKS'
        # POST_STOP_LOOP_CLOSED_DURING_DRAIN: the loop is terminal and every
        # captured task is terminal, so there is nothing left for CFR to drain.
        return empty, empty, True, None

    if loop is None:
        return empty, empty, True, None
    if loop.is_closed():
        return empty, empty, False, 'SDK_LOOP_ALREADY_CLOSED_BEFORE_TASK_DRAIN'
    if loop.is_running():
        try:
            running_here = asyncio.get_running_loop() is loop
        except RuntimeError:
            running_here = False
        if running_here:
            return empty, empty, False, 'SDK_TASK_DRAIN_RUNNING_LOOP_CURRENT_THREAD'
        drain_coro = _cancel_sdk_tasks(loop, captured_tasks)
        try:
            future = asyncio.run_coroutine_threadsafe(drain_coro, loop)
        except RuntimeError as exc:
            drain_coro.close()
            if loop.is_closed():
                return post_stop_loop_closed_during_drain()
            return empty, empty, False, f'SDK_TASK_DRAIN_ERROR:{type(exc).__name__}'
        try:
            before, after, async_generators = future.result(timeout=timeout)
            return before, after, async_generators, None
        except (concurrent.futures.TimeoutError, TimeoutError):
            return empty, empty, False, 'SDK_TASK_DRAIN_TIMEOUT'
        except RuntimeError as exc:
            # run_coroutine_threadsafe() already transferred ownership to the
            # owner loop. Do not close the raw coroutine after this point.
            if loop.is_closed():
                return post_stop_loop_closed_during_drain()
            return empty, empty, False, f'SDK_TASK_DRAIN_ERROR:{type(exc).__name__}'

    drain_coro = _cancel_sdk_tasks(loop, captured_tasks)
    try:
        before, after, async_generators = loop.run_until_complete(drain_coro)
        return before, after, async_generators, None
    except (concurrent.futures.TimeoutError, TimeoutError):
        return empty, empty, False, 'SDK_TASK_DRAIN_TIMEOUT'
    except RuntimeError as exc:
        # run_until_complete() can reject a raw coroutine when the loop closes
        # between the earlier state check and ownership transfer.
        if not drain_coro.cr_running and drain_coro.cr_frame is not None:
            drain_coro.close()
        if loop.is_closed():
            return post_stop_loop_closed_during_drain()
        return empty, empty, False, f'SDK_TASK_DRAIN_ERROR:{type(exc).__name__}'


@dataclass(frozen=True)
class DedicatedLoopDrainResult:
    observation_available: bool
    tasks_before: tuple[str, ...]
    tasks_cancelled: int
    tasks_remaining: tuple[str, ...]
    async_generators_shutdown: bool
    loop_closed_by_cfr: bool
    error: str | None
    default_executor_shutdown: str = 'NOT_REQUIRED'


def _pending_task_refs(loop: asyncio.AbstractEventLoop, captured_tasks: Iterable[asyncio.Task] = ()) -> tuple[asyncio.Task, ...]:
    refs: list[asyncio.Task] = list(captured_tasks)
    try:
        refs.extend(asyncio.all_tasks(loop))
    except Exception:
        pass
    unique: list[asyncio.Task] = []
    for task in refs:
        if not task.done() and all(task is not existing for existing in unique):
            unique.append(task)
    return tuple(unique)


async def _cancel_all_dedicated_loop_tasks(loop: asyncio.AbstractEventLoop, captured_tasks: Iterable[asyncio.Task] = (), timeout: float = 5.0) -> tuple[tuple[str, ...], int, tuple[str, ...], bool]:
    current = asyncio.current_task(loop=loop)
    pending = [task for task in _pending_task_refs(loop, captured_tasks) if task is not current]
    before_names = tuple(sorted(_task_name(task) for task in pending))
    try:
        async with asyncio.timeout(timeout):
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            async_generators_shutdown = True
            try:
                await loop.shutdown_asyncgens()
            except Exception:
                async_generators_shutdown = False
    except TimeoutError:
        async_generators_shutdown = False
    remaining_names = tuple(sorted(_task_name(task) for task in _pending_task_refs(loop, captured_tasks) if task is not current))
    return before_names, len(pending), remaining_names, async_generators_shutdown


def drain_dedicated_channel_bg_loop(
    loop: asyncio.AbstractEventLoop | None,
    *,
    timeout: float,
    captured_tasks: Iterable[asyncio.Task] = (),
) -> DedicatedLoopDrainResult:
    """Drain all tasks on a captured, Channel-owned loop after its thread exits."""
    if loop is None:
        return DedicatedLoopDrainResult(False, (), 0, (), False, False, 'CHANNEL_BG_LOOP_NOT_CAPTURED')
    try:
        if loop.is_closed():
            pending_names = tuple(sorted(_task_name(task) for task in _pending_task_refs(loop, captured_tasks)))
            return DedicatedLoopDrainResult(False, pending_names, 0, pending_names, False, False, 'CHANNEL_BG_LOOP_ALREADY_CLOSED')
        if loop.is_running():
            return DedicatedLoopDrainResult(False, (), 0, (), False, False, 'CHANNEL_BG_LOOP_STILL_RUNNING')
        before_names, cancelled, remaining_names, async_generators = loop.run_until_complete(
            _cancel_all_dedicated_loop_tasks(loop, captured_tasks, timeout)
        )
        default_executor_shutdown = 'NOT_REQUIRED'
        if getattr(loop, '_default_executor', None) is not None:
            shutdown_executor = getattr(loop, 'shutdown_default_executor', None)
            if shutdown_executor is None:
                default_executor_shutdown = 'NOT_AVAILABLE'
            else:
                try:
                    loop.run_until_complete(asyncio.wait_for(shutdown_executor(), timeout=timeout))
                    default_executor_shutdown = 'PASS'
                except Exception:
                    default_executor_shutdown = 'FAIL'
        loop_closed = False
        if not remaining_names and async_generators and default_executor_shutdown != 'FAIL':
            loop.close()
            loop_closed = True
        return DedicatedLoopDrainResult(
            True,
            before_names,
            cancelled,
            remaining_names,
            async_generators,
            loop_closed,
            None if not remaining_names and async_generators and default_executor_shutdown != 'FAIL' else 'CHANNEL_BG_LOOP_TASKS_REMAINING',
            default_executor_shutdown,
        )
    except (concurrent.futures.TimeoutError, TimeoutError):
        return DedicatedLoopDrainResult(False, (), 0, (), False, False, 'CHANNEL_BG_LOOP_DRAIN_TIMEOUT')
    except RuntimeError as exc:
        return DedicatedLoopDrainResult(False, (), 0, (), False, False, f'CHANNEL_BG_LOOP_DRAIN_ERROR:{type(exc).__name__}')


def _wait_start_future(future: object | None, timeout: float) -> bool:
    if future is None:
        return False
    deadline = time.monotonic() + timeout
    while True:
        try:
            if bool(future.done()):
                return True
        except Exception:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


async def _settle_cancelled_device_flow_task() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def preclose_device_flow_before_public_shutdown(
    capture: ChannelSdkShutdownCapture | None,
    *,
    timeout: float,
) -> DeviceFlowPreCloseResult:
    """Best-effort DeviceFlow close on the captured Channel BG loop."""
    if capture is None or not capture.device_flow_captured or capture.device_flow is None:
        return DeviceFlowPreCloseResult(False, False, False, False, False, False, False, None, None, None, None, None, None, 'NOT_REQUIRED')
    device_flow = capture.device_flow
    close = getattr(device_flow, 'close', None)
    http_before = capture.device_flow_http_present_before
    http_closed_before = capture.device_flow_http_closed_before
    if not callable(close):
        return DeviceFlowPreCloseResult(True, False, False, False, False, False, False, http_before, http_before, http_closed_before, http_closed_before, 0, 'DEVICE_FLOW_PRECLOSE_UNAVAILABLE', 'UNAVAILABLE')
    loop = capture.bg_loop
    thread_alive = capture.bg_thread_alive_before_public_stop is True
    if loop is None or loop.is_closed() or not capture.bg_loop_captured or not capture.bg_thread_captured:
        return DeviceFlowPreCloseResult(True, False, False, False, False, False, False, http_before, http_before, http_closed_before, http_closed_before, 0, 'DEVICE_FLOW_PRECLOSE_LOOP_UNAVAILABLE', 'UNAVAILABLE')
    if loop.is_running() and not thread_alive:
        return DeviceFlowPreCloseResult(True, False, False, False, False, False, False, http_before, http_before, http_closed_before, http_closed_before, 0, 'DEVICE_FLOW_PRECLOSE_LOOP_UNAVAILABLE', 'UNAVAILABLE')
    if not loop.is_running() and thread_alive and capture.bg_thread is not threading.current_thread():
        return DeviceFlowPreCloseResult(True, False, False, False, False, False, False, http_before, http_before, http_closed_before, http_closed_before, 0, 'DEVICE_FLOW_PRECLOSE_LOOP_UNAVAILABLE', 'UNAVAILABLE')
    started = time.monotonic()
    future = None
    try:
        coroutine = close()
        if not inspect.isawaitable(coroutine):
            elapsed = int((time.monotonic() - started) * 1000)
            return DeviceFlowPreCloseResult(True, True, True, True, False, False, False, http_before, getattr(device_flow, '_http', None) is not None, http_closed_before, _safe_http_closed(getattr(device_flow, '_http', None)), elapsed, None, 'PRECLOSE_NO_HTTP_NOOP' if not http_before else 'PRECLOSE_OWNED_HTTP')
        if not loop.is_running():
            try:
                loop.run_until_complete(asyncio.wait_for(coroutine, timeout=timeout))
                elapsed = int((time.monotonic() - started) * 1000)
                http_after = getattr(device_flow, '_http', None)
                return DeviceFlowPreCloseResult(True, True, True, True, False, False, True, http_before, http_after is not None, http_closed_before, _safe_http_closed(http_after), elapsed, None, 'PRECLOSE_DRIVE_NONRUNNING_LOOP')
            except (asyncio.TimeoutError, TimeoutError):
                elapsed = int((time.monotonic() - started) * 1000)
                http_after = getattr(device_flow, '_http', None)
                return DeviceFlowPreCloseResult(True, True, True, False, True, True, True, http_before, http_after is not None, http_closed_before, _safe_http_closed(http_after), elapsed, 'DEVICE_FLOW_PRECLOSE_TIMEOUT', 'PRECLOSE_DRIVE_NONRUNNING_LOOP')
            except Exception as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                http_after = getattr(device_flow, '_http', None)
                return DeviceFlowPreCloseResult(True, True, True, False, False, False, False, http_before, http_after is not None, http_closed_before, _safe_http_closed(http_after), elapsed, f'DEVICE_FLOW_PRECLOSE_FAILED:{type(exc).__name__}', 'PRECLOSE_DRIVE_NONRUNNING_LOOP')
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except BaseException:
            coroutine.close()
            raise
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return DeviceFlowPreCloseResult(True, True, False, False, False, False, False, http_before, getattr(device_flow, '_http', None) is not None, http_closed_before, _safe_http_closed(getattr(device_flow, '_http', None)), elapsed, f'DEVICE_FLOW_PRECLOSE_SCHEDULE_FAILED:{type(exc).__name__}', 'PRECLOSE_TIMEOUT')
    try:
        future.result(timeout=timeout)
        completed = future.done() and not future.cancelled()
        elapsed = int((time.monotonic() - started) * 1000)
        http_after = getattr(device_flow, '_http', None)
        return DeviceFlowPreCloseResult(True, True, True, completed, False, False, False, http_before, http_after is not None, http_closed_before, _safe_http_closed(http_after), elapsed, None if completed else 'DEVICE_FLOW_PRECLOSE_INCOMPLETE', 'PRECLOSE_NO_HTTP_NOOP' if not http_before else 'PRECLOSE_OWNED_HTTP', future)
    except concurrent.futures.TimeoutError:
        cancelled = bool(future.cancel())
        cancellation_observed = False
        try:
            settle = asyncio.run_coroutine_threadsafe(_settle_cancelled_device_flow_task(), loop)
            settle.result(timeout=min(1.0, max(0.1, timeout)))
            cancellation_observed = True
        except concurrent.futures.CancelledError:
            cancellation_observed = True
        except concurrent.futures.TimeoutError:
            cancellation_observed = bool(future.done())
        except Exception:
            cancellation_observed = bool(future.done())
        elapsed = int((time.monotonic() - started) * 1000)
        http_after = getattr(device_flow, '_http', None)
        return DeviceFlowPreCloseResult(True, True, True, False, True, cancelled, cancellation_observed, http_before, http_after is not None, http_closed_before, _safe_http_closed(http_after), elapsed, 'DEVICE_FLOW_PRECLOSE_TIMEOUT', 'PRECLOSE_TIMEOUT', future)
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        http_after = getattr(device_flow, '_http', None)
        return DeviceFlowPreCloseResult(True, True, True, False, False, False, False, http_before, http_after is not None, http_closed_before, _safe_http_closed(http_after), elapsed, f'DEVICE_FLOW_PRECLOSE_FAILED:{type(exc).__name__}', 'PRECLOSE_TIMEOUT', future)


def _bg_pre_stop_shape_guard(channel: Any) -> None:
    required = (
        '_bg_loop', '_bg_thread', '_bg_tasks', '_bg_tasks_lock',
        '_bot_identity_retry_future', '_stop_bg_loop',
    )
    if any(not hasattr(channel, name) for name in required):
        raise RuntimeError('FEISHU_SDK_BG_DRAIN_SHAPE_UNSUPPORTED')
    if not callable(getattr(channel, '_stop_bg_loop', None)):
        raise RuntimeError('FEISHU_SDK_BG_DRAIN_SHAPE_UNSUPPORTED')
    if not callable(getattr(getattr(channel, '_bg_tasks_lock', None), '__enter__', None)):
        raise RuntimeError('FEISHU_SDK_BG_DRAIN_SHAPE_UNSUPPORTED')
    if not hasattr(getattr(channel, '_bg_tasks', None), '__iter__'):
        raise RuntimeError('FEISHU_SDK_BG_DRAIN_SHAPE_UNSUPPORTED')


def _start_worker_handoff_shape_guard(channel: Any) -> None:
    required = (
        '_shutdown', '_stop_requested', '_lifecycle_lock', '_bg_lock',
        '_bg_tasks_lock', '_bg_loop', '_bg_thread', '_bg_tasks',
        '_bot_identity_retry_future', '_start_future', '_ws_client',
    )
    if any(not hasattr(channel, name) for name in required):
        raise RuntimeError('FEISHU_SDK_START_WORKER_HANDOFF_SHAPE_UNSUPPORTED')
    for name in ('_lifecycle_lock', '_bg_lock', '_bg_tasks_lock'):
        lock = getattr(channel, name, None)
        if not callable(getattr(lock, '__enter__', None)):
            raise RuntimeError('FEISHU_SDK_START_WORKER_HANDOFF_SHAPE_UNSUPPORTED')
    shutdown = getattr(channel, '_shutdown', None)
    if not callable(getattr(shutdown, 'set', None)):
        raise RuntimeError('FEISHU_SDK_START_WORKER_HANDOFF_SHAPE_UNSUPPORTED')
    tasks = getattr(channel, '_bg_tasks', None)
    if not callable(getattr(tasks, 'clear', None)):
        raise RuntimeError('FEISHU_SDK_START_WORKER_HANDOFF_SHAPE_UNSUPPORTED')
    try:
        empty_tasks = type(tasks)()
    except Exception as exc:
        raise RuntimeError('FEISHU_SDK_START_WORKER_HANDOFF_SHAPE_UNSUPPORTED') from exc
    if not hasattr(empty_tasks, '__iter__'):
        raise RuntimeError('FEISHU_SDK_START_WORKER_HANDOFF_SHAPE_UNSUPPORTED')


def handoff_channel_bg_ownership(channel: Any) -> ChannelBgOwnershipHandoff:
    """Atomically block SDK producers and detach CFR-owned BG resources.

    This is intentionally a data/ownership transition at the compatibility
    boundary.  No SDK method is patched.  The captured loop/thread/futures
    remain strongly referenced by the returned handoff while late SDK cleanup
    sees an empty, detached channel state.
    """
    _start_worker_handoff_shape_guard(channel)
    handoff = ChannelBgOwnershipHandoff(attempted=True)
    version = 'UNKNOWN'
    try:
        version = importlib.metadata.version('lark-channel-sdk')
    except Exception:
        pass
    handoff.shape_version = f'lark-channel-sdk={version}'
    stop_watchdog = getattr(channel, '_stop_keepalive_watchdog', None)
    if callable(stop_watchdog):
        stop_watchdog()

    # The SDK's scheduling path consults _shutdown only when it has to create
    # a loop.  Set the terminal flag before publishing detached fields, under
    # the lifecycle/bg locks, so a post-handoff _ensure_bg_loop() fails closed.
    with channel._lifecycle_lock:
        channel._shutdown.set()
        if hasattr(channel, '_background_generation'):
            channel._background_generation += 1
        if hasattr(channel, '_lifecycle_generation'):
            channel._lifecycle_generation += 1
        stop_requested = getattr(channel, '_stop_requested')
        if callable(getattr(stop_requested, 'set', None)):
            stop_requested.set()
        else:
            channel._stop_requested = True
        with channel._bg_lock:
            with channel._bg_tasks_lock:
                current_tasks = getattr(channel, '_bg_tasks')
                tracked = tuple(current_tasks)
                retry_future = getattr(channel, '_bot_identity_retry_future')
                if retry_future is not None and all(retry_future is not item for item in tracked):
                    tracked = tracked + (retry_future,)
                channel._bg_tasks = type(current_tasks)()
                channel._bot_identity_retry_future = None
            handoff.bg_loop = getattr(channel, '_bg_loop')
            handoff.bg_thread = getattr(channel, '_bg_thread')
            handoff.tracked_bg_futures = tracked
            handoff.bot_identity_retry_future = retry_future
            handoff.start_future = getattr(channel, '_start_future')
            handoff.ws_client = getattr(channel, '_ws_client')
            handoff.scheduling_blocked_before_detach = True
            channel._bg_loop = None
            channel._bg_thread = None
            handoff.detached_from_sdk = True
    handoff.completed = True
    handoff.producer_quiescence_contract = 'PENDING_START_WORKER_TERMINAL'
    handoff.start_future_wrapper_cancelled = bool(
        handoff.start_future is not None and getattr(handoff.start_future, 'cancelled', lambda: False)()
    )
    channel._cfr_bg_ownership_handoff = handoff
    return handoff


def _stop_ws_client_after_bg_handoff(channel: Any, ws: object | None) -> None:
    if ws is None:
        return
    stopped = False
    for method_name in ('stop', 'close', 'disconnect'):
        method = getattr(ws, method_name, None)
        if not callable(method):
            continue
        try:
            method()
        except Exception:
            pass
        stopped = True
        break
    if not stopped:
        private_stop = getattr(channel, '_stop_private_ws_client', None)
        if callable(private_stop):
            private_stop(ws)


async def _await_start_worker_terminal(handoff: ChannelBgOwnershipHandoff, *, timeout: float) -> None:
    handoff.start_worker_terminal_wait_attempted = handoff.start_future is not None
    if handoff.start_future is None:
        handoff.start_worker_exited = True
        handoff.start_worker_terminal_evidence = 'NO_START_WORKER_CAPTURED'
        handoff.producer_quiescence_contract = 'PASS'
        return
    if handoff.start_future_wrapper_cancelled:
        handoff.start_worker_terminal_evidence = 'START_FUTURE_CANCELLED_NOT_WORKER_TERMINAL'
        handoff.producer_quiescence_contract = 'FAIL'
        return
    future = handoff.start_future
    try:
        # shield is required: timeout/caller cancellation must not cancel the
        # SDK Future wrapper, and only natural completion proves the worker.
        await asyncio.wait_for(asyncio.shield(future), timeout=max(0.01, float(timeout)))
    except asyncio.TimeoutError:
        handoff.start_worker_terminal_timed_out = True
        handoff.start_worker_terminal_evidence = 'START_WORKER_TERMINAL_TIMEOUT'
        handoff.producer_quiescence_contract = 'FAIL'
        return
    except asyncio.CancelledError:
        handoff.start_worker_terminal_evidence = 'START_FUTURE_CANCELLED_NOT_WORKER_TERMINAL'
        handoff.producer_quiescence_contract = 'FAIL'
        return
    except BaseException:
        # An exception from channel.start still proves that the executor
        # callable returned.  Startup failure is classified by its caller.
        pass
    handoff.start_worker_exited = bool(getattr(future, 'done', lambda: False)()) and not bool(
        getattr(future, 'cancelled', lambda: False)()
    )
    handoff.start_worker_terminal_evidence = 'START_FUTURE_NATURAL_COMPLETION' if handoff.start_worker_exited else 'START_WORKER_TERMINAL_NOT_PROVEN'
    handoff.producer_quiescence_contract = 'PASS' if handoff.start_worker_exited and handoff.detached_from_sdk else 'FAIL'


def drain_captured_bg_loop_after_worker(
    handoff: ChannelBgOwnershipHandoff,
    *,
    timeout: float,
) -> BgPreStopDrainResult:
    """Drain the captured SDK BG loop only after producer quiescence."""
    if handoff.producer_quiescence_contract != 'PASS':
        return BgPreStopDrainResult(
            attempted=False,
            mechanism='CFR_FINAL_DRAIN_BLOCKED',
            terminal_evidence='CFR_FINAL_DRAIN_PRODUCER_NOT_QUIESCENT',
            error_code='FEISHU_SDK_BG_PRODUCER_NOT_QUIESCENT',
        )
    loop = handoff.bg_loop
    thread = handoff.bg_thread
    if loop is None:
        result = BgPreStopDrainResult(
            attempted=True, mechanism='NO_BG_LOOP', completed=True,
            terminal_evidence='NO_BG_LOOP', loop_stop_allowed=True,
        )
        handoff.final_drain = result
        handoff.start_worker_terminal_before_bg_drain = True
        return result
    if loop.is_closed():
        result = BgPreStopDrainResult(
            attempted=True, mechanism='CFR_FINAL_DRAIN_LOOP_CLOSED',
            terminal_evidence='FEISHU_SDK_BG_DRAIN_LOOP_CLOSED',
            error_code='FEISHU_SDK_BG_DRAIN_LOOP_CLOSED',
        )
        handoff.final_drain = result
        return result
    if loop.is_running():
        result = _running_bg_loop_callback_drain(loop, timeout=timeout)
        result = BgPreStopDrainResult(
            **{**result.__dict__, 'mechanism': 'CFR_FINAL_CALLBACK_TERMINAL_DRAIN'}
        )
    else:
        drained = drain_dedicated_channel_bg_loop(
            loop, timeout=timeout, captured_tasks=()
        )
        result = BgPreStopDrainResult(
            attempted=True,
            mechanism='CFR_FINAL_NONRUNNING_LOOP_TERMINAL_DRAIN',
            loop_running=False,
            thread_alive=bool(thread and thread.is_alive()),
            tasks_before=drained.tasks_before,
            tasks_cancelled=drained.tasks_cancelled,
            tasks_remaining=drained.tasks_remaining,
            completed=bool(drained.observation_available and not drained.tasks_remaining),
            terminal_evidence='CFR_FINAL_NONRUNNING_LOOP_TERMINAL_DRAIN' if drained.observation_available and not drained.tasks_remaining else 'CFR_FINAL_NONRUNNING_LOOP_DRAIN_FAILED',
            loop_stop_allowed=bool(drained.observation_available and not drained.tasks_remaining),
            error_code=drained.error,
        )
    handoff.final_drain = result
    handoff.start_worker_terminal_before_bg_drain = handoff.start_worker_exited
    return result


def stop_captured_bg_loop(handoff: ChannelBgOwnershipHandoff, *, join_timeout: float) -> bool:
    """Stop/join the captured loop directly; never call SDK _stop_bg_loop."""
    result = handoff.final_drain
    if result is None or not result.loop_stop_allowed:
        return False
    loop = handoff.bg_loop
    thread = handoff.bg_thread
    if loop is None and thread is None:
        handoff.captured_thread_exited = True
        handoff.captured_loop_closed = True
        handoff.captured_loop_stop_allowed = True
        return True
    if loop is not None and not loop.is_closed():
        try:
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            return False
    if isinstance(thread, threading.Thread) and thread is not threading.current_thread() and thread.is_alive():
        thread.join(timeout=max(0.01, float(join_timeout)))
    handoff.captured_thread_exited = not thread.is_alive() if isinstance(thread, threading.Thread) else True
    handoff.captured_loop_closed = bool(loop is not None and loop.is_closed())
    handoff.captured_loop_stop_allowed = bool(handoff.captured_thread_exited and handoff.captured_loop_closed)
    return handoff.captured_loop_stop_allowed


def _snapshot_and_cancel_bg_futures(channel: Any) -> tuple[int, int]:
    tasks = getattr(channel, '_bg_tasks')
    lock = getattr(channel, '_bg_tasks_lock')
    with lock:
        futures = list(tasks)
        retry_future = getattr(channel, '_bot_identity_retry_future')
        if retry_future is not None and all(retry_future is not item for item in futures):
            futures.append(retry_future)
    cancelled = 0
    for future in futures:
        try:
            if not future.done() and bool(future.cancel()):
                cancelled += 1
        except Exception:
            try:
                if bool(future.cancel()):
                    cancelled += 1
            except Exception:
                pass
    return len(futures), cancelled


def _bg_task_names(tasks: Iterable[asyncio.Task]) -> tuple[str, ...]:
    return tuple(sorted(_task_name(task) for task in tasks))


def _running_bg_loop_callback_drain(
    loop: asyncio.AbstractEventLoop,
    *,
    timeout: float,
) -> BgPreStopDrainResult:
    """Cancel a running SDK owner loop using callbacks only.

    The SDK's cancellation helper inserts ``asyncio.sleep(0)`` through
    ``run_coroutine_threadsafe``. That is precisely the race this boundary
    owns, so this drain uses only loop callbacks and repeated all-task
    observations until the owner loop is terminal.
    """
    completed = threading.Event()
    state: dict[str, Any] = {
        'tasks_before': (),
        'tasks_cancelled': set(),
        'tasks_remaining': (),
        'completed': False,
        'timed_out': False,
        'terminal_evidence': 'NOT_RUN',
        'abandoned': False,
    }
    deadline = time.monotonic() + max(0.01, float(timeout))

    def finish(*, timed_out: bool, remaining: tuple[asyncio.Task, ...], evidence: str) -> None:
        if state['completed'] or state['abandoned']:
            return
        state['tasks_remaining'] = _bg_task_names(remaining)
        state['timed_out'] = timed_out
        state['completed'] = not timed_out and not remaining
        state['terminal_evidence'] = evidence
        completed.set()

    def check_again() -> None:
        if state['completed'] or state['timed_out'] or state['abandoned']:
            return
        try:
            pending = tuple(task for task in asyncio.all_tasks(loop) if not task.done())
        except Exception:
            pending = ()
        if not state['tasks_before']:
            state['tasks_before'] = _bg_task_names(pending)
        for task in pending:
            try:
                if task.cancel():
                    state['tasks_cancelled'].add(id(task))
            except Exception:
                pass
        if not pending:
            finish(timed_out=False, remaining=(), evidence='CFR_LOOP_CALLBACK_TERMINAL_DRAIN')
            return
        if time.monotonic() >= deadline:
            finish(timed_out=True, remaining=pending, evidence='CFR_LOOP_CALLBACK_DRAIN_TIMEOUT')
            return
        loop.call_later(min(0.01, max(0.001, (deadline - time.monotonic()) / 2)), check_again)

    try:
        loop.call_soon_threadsafe(check_again)
    except Exception as exc:
        return BgPreStopDrainResult(
            attempted=True,
            mechanism='CFR_LOOP_CALLBACK_TERMINAL_DRAIN',
            loop_running=True,
            tasks_remaining=(),
            terminal_evidence='CFR_LOOP_CALLBACK_SCHEDULE_FAILED',
            error_code=f'FEISHU_SDK_BG_DRAIN_CALLBACK_FAILED:{type(exc).__name__}',
        )
    if not completed.wait(max(0.01, float(timeout)) + 0.05):
        state['abandoned'] = True
        return BgPreStopDrainResult(
            attempted=True,
            mechanism='CFR_LOOP_CALLBACK_TERMINAL_DRAIN',
            loop_running=True,
            tasks_before=tuple(state['tasks_before']),
            tasks_cancelled=len(state['tasks_cancelled']),
            tasks_remaining=tuple(state['tasks_remaining']),
            timed_out=True,
            terminal_evidence='CFR_LOOP_CALLBACK_DRAIN_TIMEOUT',
            error_code='FEISHU_SDK_BG_DRAIN_TIMEOUT',
        )
    return BgPreStopDrainResult(
        attempted=True,
        mechanism='CFR_LOOP_CALLBACK_TERMINAL_DRAIN',
        loop_running=True,
        tasks_before=tuple(state['tasks_before']),
        tasks_cancelled=len(state['tasks_cancelled']),
        tasks_remaining=tuple(state['tasks_remaining']),
        completed=bool(state['completed']),
        timed_out=bool(state['timed_out']),
        terminal_evidence=str(state['terminal_evidence']),
        loop_stop_allowed=bool(state['completed']) and not state['tasks_remaining'],
        error_code=None if state['completed'] else 'FEISHU_SDK_BG_DRAIN_TIMEOUT',
    )


def drain_bg_loop_before_sdk_stop(channel: Any, *, timeout: float = 5.0) -> BgPreStopDrainResult:
    """Reach terminal state on the SDK BG owner loop before stopping it."""
    _bg_pre_stop_shape_guard(channel)
    loop = getattr(channel, '_bg_loop')
    thread = getattr(channel, '_bg_thread')
    tracked_count, tracked_cancelled = _snapshot_and_cancel_bg_futures(channel)
    loop_running = bool(loop is not None and loop.is_running())
    thread_alive = bool(thread is not None and getattr(thread, 'is_alive', lambda: False)())
    if loop is None:
        return BgPreStopDrainResult(
            attempted=True, mechanism='NO_BG_LOOP', loop_running=False,
            thread_alive=thread_alive, tracked_future_count=tracked_count,
            tracked_futures_cancelled=tracked_cancelled, completed=True,
            terminal_evidence='NO_BG_LOOP', loop_stop_allowed=True,
        )
    if loop.is_closed():
        pending = _safe_all_pending_task_refs(loop)
        return BgPreStopDrainResult(
            attempted=True, mechanism='CFR_LOOP_CALLBACK_TERMINAL_DRAIN',
            loop_running=False, thread_alive=thread_alive,
            tracked_future_count=tracked_count,
            tracked_futures_cancelled=tracked_cancelled,
            tasks_before=_bg_task_names(pending), tasks_remaining=_bg_task_names(pending),
            terminal_evidence='FEISHU_SDK_BG_DRAIN_LOOP_CLOSED',
            error_code='FEISHU_SDK_BG_DRAIN_LOOP_CLOSED',
        )
    if loop_running:
        if not thread_alive:
            return BgPreStopDrainResult(
                attempted=True, mechanism='CFR_LOOP_CALLBACK_TERMINAL_DRAIN',
                loop_running=True, thread_alive=False,
                tracked_future_count=tracked_count,
                tracked_futures_cancelled=tracked_cancelled,
                terminal_evidence='FEISHU_SDK_BG_DRAIN_OWNER_THREAD_MISSING',
                error_code='FEISHU_SDK_BG_DRAIN_OWNER_THREAD_MISSING',
            )
        result = _running_bg_loop_callback_drain(loop, timeout=timeout)
        return BgPreStopDrainResult(
            **{**result.__dict__, 'thread_alive': thread_alive,
               'tracked_future_count': tracked_count,
               'tracked_futures_cancelled': tracked_cancelled}
        )
    if thread_alive:
        return BgPreStopDrainResult(
            attempted=True, mechanism='CFR_LOOP_CALLBACK_TERMINAL_DRAIN',
            loop_running=False, thread_alive=True,
            tracked_future_count=tracked_count,
            tracked_futures_cancelled=tracked_cancelled,
            terminal_evidence='FEISHU_SDK_BG_DRAIN_THREAD_LIVE_LOOP_STOPPED',
            error_code='FEISHU_SDK_BG_DRAIN_THREAD_LIVE_LOOP_STOPPED',
        )
    result = drain_dedicated_channel_bg_loop(
        loop, timeout=timeout, captured_tasks=_safe_all_pending_task_refs(loop)
    )
    return BgPreStopDrainResult(
        attempted=True,
        mechanism='CFR_NONRUNNING_LOOP_TERMINAL_DRAIN',
        loop_running=False,
        thread_alive=False,
        tracked_future_count=tracked_count,
        tracked_futures_cancelled=tracked_cancelled,
        tasks_before=result.tasks_before,
        tasks_cancelled=result.tasks_cancelled,
        tasks_remaining=result.tasks_remaining,
        completed=bool(result.observation_available and not result.tasks_remaining),
        terminal_evidence='CFR_NONRUNNING_LOOP_TERMINAL_DRAIN' if result.observation_available and not result.tasks_remaining else 'CFR_NONRUNNING_LOOP_DRAIN_FAILED',
        loop_stop_allowed=bool(result.observation_available and not result.tasks_remaining),
        error_code=result.error,
    )


async def shutdown_channel_without_device_flow_close(
    channel: Any,
    *,
    join_timeout: float = 5.0,
) -> str:
    """Serialize SDK start-worker and BG-loop shutdown on the owner loop."""
    shutdown = getattr(channel, '_shutdown', None)
    if shutdown is None or not callable(getattr(shutdown, 'is_set', None)):
        raise RuntimeError('FEISHU_SDK_STOP_SHAPE_UNSUPPORTED')
    if shutdown.is_set():
        return 'ALREADY_TERMINAL'
    try:
        handoff = handoff_channel_bg_ownership(channel)
    except RuntimeError as exc:
        channel._cfr_bg_pre_stop_result = BgPreStopDrainResult(
            attempted=True,
            mechanism='SHAPE_GUARD',
            terminal_evidence='FEISHU_SDK_START_WORKER_HANDOFF_SHAPE_UNSUPPORTED',
            error_code=str(exc),
        )
        raise

    # Detach first.  A late _cleanup_failed_start() can now run, but its
    # _cancel_bg_tasks/_stop_bg_loop calls see empty/None ownership fields.
    _stop_ws_client_after_bg_handoff(channel, handoff.ws_client)
    await _await_start_worker_terminal(handoff, timeout=join_timeout)
    if not handoff.start_worker_exited:
        handoff.producer_quiescence_contract = 'FAIL'
        channel._cfr_bg_ownership_handoff = handoff
        raise RuntimeError('FEISHU_SDK_START_WORKER_TERMINAL_NOT_PROVEN')

    final_drain = drain_captured_bg_loop_after_worker(handoff, timeout=join_timeout)
    channel._cfr_bg_pre_stop_result = final_drain
    if not final_drain.loop_stop_allowed:
        raise RuntimeError(final_drain.error_code or 'FEISHU_SDK_BG_FINAL_DRAIN_NOT_TERMINAL')
    if not stop_captured_bg_loop(handoff, join_timeout=join_timeout):
        raise RuntimeError('FEISHU_SDK_BG_CAPTURED_LOOP_STOP_NOT_PROVEN')
    handoff.producer_quiescence_contract = 'PASS'
    channel._cfr_bg_ownership_handoff = handoff

    channel._ws_client = None
    channel._start_future = None
    channel._started = False
    channel._ready_flag = False
    channel._connection_state = 'idle'
    channel._connection_last_disconnected_at = time.time()
    clear_shutdown = getattr(shutdown, 'clear', None)
    if callable(clear_shutdown):
        clear_shutdown()
    ready_event = getattr(channel, '_ready_event', None)
    if ready_event is not None:
        try:
            ready_event.clear()
        except Exception:
            pass
    return 'PUBLIC_STOP_IDEMPOTENT_NOOP'


def stop_channel_without_device_flow_close(channel: Any, *, join_timeout: float = 5.0) -> str:
    """Synchronous bridge for callers that do not own an asyncio loop.

    Production transport calls the async variant so the SDK Future's owner
    loop remains alive.  This bridge is retained for simple synchronous
    fixtures and rejects a live worker rather than cancelling its wrapper.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            shutdown_channel_without_device_flow_close(channel, join_timeout=join_timeout)
        )
    raise RuntimeError('FEISHU_SDK_START_WORKER_WAIT_REQUIRES_OWNER_LOOP')


def _name_counts(names: Iterable[str]) -> tuple[int, int, int]:
    counts = {'ws': 0, 'cache': 0, 'device_flow': 0}
    for name in names:
        lower = name.lower()
        if 'cache' in lower or 'cron' in lower or 'clear' in lower:
            counts['cache'] += 1
        elif 'deviceflow' in lower or 'device_flow' in lower or '.auth' in lower or 'token' in lower:
            counts['device_flow'] += 1
        else:
            counts['ws'] += 1
    return counts['ws'], counts['cache'], counts['device_flow']


def drain_sdk_tasks(channel: Any | None = None, timeout: float = 5.0, loops: Iterable[asyncio.AbstractEventLoop] | None = None, capture: ChannelSdkShutdownCapture | None = None, device_flow_preclose: DeviceFlowPreCloseResult | None = None) -> SdkShutdownDiagnostic:
    """Drain only real SDK tasks, including hidden owner loops captured pre-stop."""
    issues: list[str] = []
    candidate_loops: list[asyncio.AbstractEventLoop] = list(loops or ())
    device_flow_scheduled = False
    device_flow_completed = 'NOT_OBSERVABLE'
    handoff = getattr(channel, '_cfr_bg_ownership_handoff', None) if channel is not None else None
    start_future_exited = (
        bool(getattr(handoff, 'start_worker_exited', False))
        if handoff is not None
        else (_wait_start_future(capture.start_future, timeout) if capture else False)
    )
    bg_thread_exited: bool | None = None
    bg_task_observation_available = True
    bg_tasks_after_public: tuple[str, ...] = ()
    bg_tasks_drained: int | None = 0
    bg_tasks_remaining: tuple[str, ...] = ()
    bg_async_generators = 'NOT_REQUIRED'
    bg_default_executor_shutdown = 'NOT_REQUIRED'
    bg_loop_closed_by_cfr = False
    bg_loop_closed_after_public_stop: bool | None = None
    bg_loop_closure_state = 'NOT_CAPTURED'
    bg_task_observation_status = 'NOT_REQUIRED'
    bg_task_terminal_evidence = 'NOT_YET_EVALUATED'
    df_capture = bool(capture and capture.device_flow_captured)
    df_http_before = capture.device_flow_http_present_before if capture else None
    df_http_owned_before = capture.device_flow_http_owned_before if capture else None
    df_http_closed_before = capture.device_flow_http_closed_before if capture else None
    df_preclose = device_flow_preclose
    bg_pre_stop = getattr(channel, '_cfr_bg_pre_stop_result', None) if channel is not None else None
    if isinstance(bg_pre_stop, BgPreStopDrainResult) and bg_pre_stop.error_code:
        issues.append(bg_pre_stop.error_code)
    if df_preclose is not None and df_preclose.error_code:
        issues.append(df_preclose.error_code)
    if capture is not None:
        for loop in (capture.ws_loop, capture.cache_loop):
            if loop is not None:
                candidate_loops.append(loop)
        for task in capture.captured_ws_tasks + tuple(item for item in (capture.cache_cron_task, capture.reconnect_task) if isinstance(item, asyncio.Task)):
            try:
                candidate_loops.append(task.get_loop())
            except Exception:
                pass
        if capture.cache_loop_closed_before_shutdown and capture.cache_cron_captured and capture.cache_cron_task_done_before_shutdown is False:
            issues.append('SDK_CACHE_LOOP_CLOSED_WITH_PENDING_TASK')
        if capture.ws_loop_closed_before_public_stop and capture.ws_tasks_before_public_stop:
            issues.append('SDK_WS_LOOP_CLOSED_WITH_PENDING_TASK')
    if channel is not None:
        device_flow = getattr(channel, '_device_flow', None)
        if device_flow is not None:
            device_flow_scheduled = True
            # The installed SDK does not retain the close future or expose a
            # stable closed/disposed flag. A stopped bg loop is not proof.
            for name in ('closed', 'is_closed', 'disposed', 'is_disposed'):
                if hasattr(device_flow, name):
                    value = getattr(device_flow, name)
                    value = value() if callable(value) else value
                    if value is True:
                        device_flow_completed = 'PASS'
                        break
        bg_loop = getattr(channel, '_bg_loop', None)
        if bg_loop is not None:
            candidate_loops.append(bg_loop)
        ws = getattr(channel, '_ws_client', None)
        ws_loop = getattr(ws, '_loop', None) if ws is not None else None
        if ws_loop is not None:
            candidate_loops.append(ws_loop)
    if capture is not None and capture.bg_loop_captured:
        bg_thread = capture.bg_thread
        if isinstance(bg_thread, threading.Thread):
            if bg_thread is not threading.current_thread() and bg_thread.is_alive():
                bg_thread.join(timeout=timeout)
            bg_thread_exited = not bg_thread.is_alive()
        else:
            bg_thread_exited = False
        bg_task_observation_available = bool(
            capture.bg_thread_captured
            and bg_thread_exited
            and capture.bg_loop is not None
        )
        bg_loop_closed_after_public_stop = capture.bg_loop.is_closed() if capture.bg_loop is not None else None
        if capture.bg_loop_closed_before_public_stop:
            bg_loop_closure_state = 'CLOSED_UNCLEANLY'
            bg_task_observation_status = 'UNAVAILABLE'
            bg_task_observation_available = False
            issues.append('CHANNEL_BG_LOOP_CLOSED_BEFORE_PUBLIC_STOP')
        elif not bg_thread_exited:
            bg_loop_closure_state = 'CLOSED_UNCLEANLY' if bg_loop_closed_after_public_stop else 'OPEN_AFTER_PUBLIC_STOP'
            bg_task_observation_status = 'UNAVAILABLE'
            if bg_loop_closed_after_public_stop:
                issues.append('CHANNEL_BG_LOOP_CLOSED_WITH_LIVE_THREAD')
            else:
                issues.append('CHANNEL_BG_LOOP_OWNER_NOT_PROVEN')
            bg_task_observation_available = False
        elif bg_loop_closed_after_public_stop:
            bg_loop_closure_state = 'CLOSED_BY_UPSTREAM_CLEANLY'
            bg_task_observation_status = 'CLOSED_CLEANLY_BY_UPSTREAM'
            bg_task_observation_available = False
            bg_async_generators = 'NOT_OBSERVABLE'
            bg_default_executor_shutdown = 'NOT_OBSERVABLE'
            bg_tasks_drained = None
            bg_tasks_remaining = ()
            bg_task_terminal_evidence = 'POST_EXIT_PROCESS_PENDING'
        elif not bg_task_observation_available:
            issues.append('CHANNEL_BG_LOOP_OWNER_NOT_PROVEN')
        else:
            bg_result = drain_dedicated_channel_bg_loop(capture.bg_loop, timeout=timeout, captured_tasks=capture.captured_bg_tasks)
            bg_tasks_after_public = bg_result.tasks_before
            bg_tasks_drained = bg_result.tasks_cancelled
            bg_tasks_remaining = bg_result.tasks_remaining
            bg_async_generators = 'PASS' if bg_result.async_generators_shutdown else 'FAIL'
            bg_default_executor_shutdown = bg_result.default_executor_shutdown
            bg_loop_closed_by_cfr = bg_result.loop_closed_by_cfr
            if not bg_result.observation_available:
                bg_task_observation_available = False
            if bg_result.error == 'CHANNEL_BG_LOOP_ALREADY_CLOSED' and capture.captured_bg_tasks:
                issues.append('CHANNEL_BG_LOOP_CLOSED_WITH_PENDING_TASKS')
            if bg_result.error:
                issues.append(bg_result.error)
            if bg_tasks_remaining:
                issues.append('CHANNEL_BG_TASKS_REMAINING_AFTER_DRAIN')
            bg_loop_closed_after_public_stop = capture.bg_loop.is_closed()
            bg_loop_closure_state = 'CLOSED_BY_CFR' if bg_loop_closed_by_cfr else ('OPEN_AFTER_PUBLIC_STOP' if not bg_loop_closed_after_public_stop else 'CLOSED_UNCLEANLY')
            bg_task_observation_status = 'OBSERVED' if bg_result.observation_available else 'UNAVAILABLE'
    sdk_global_loop = None
    if loops is None:
        try:
            sdk_module = importlib.import_module('lark_channel.ws.client')
            sdk_global_loop = getattr(sdk_module, 'loop', None)
            if sdk_global_loop is not None:
                candidate_loops.append(sdk_global_loop)
        except Exception:
            pass
    unique: list[asyncio.AbstractEventLoop] = []
    for loop in candidate_loops:
        if all(loop is not existing for existing in unique):
            unique.append(loop)
    after_public_names: list[str] = []
    after_names: list[str] = []
    ws_after_public = cache_after_public = device_after_public = 0
    ws_after = cache_after = device_after = 0
    asyncgens = True
    cache_loop_closed_by_cfr = False
    ws_task_observation_available = True
    cache_task_observation_available = True
    if capture is not None:
        ws_task_observation_available = not (
            capture.ws_loop_closed_before_public_stop and capture.ws_tasks_before_public_stop
        )
        cache_task_observation_available = not (
            capture.cache_loop_closed_before_shutdown
            and capture.cache_cron_captured
            and capture.cache_cron_task_done_before_shutdown is False
        )
    for loop in unique:
        captured_tasks: tuple[asyncio.Task, ...] = ()
        if capture is not None:
            if loop is capture.cache_loop and isinstance(capture.cache_cron_task, asyncio.Task):
                captured_tasks = (capture.cache_cron_task,)
            elif loop is capture.ws_loop:
                captured_tasks = tuple(capture.captured_ws_tasks)
        before, after, generators, issue = _loop_tasks(loop, timeout, captured_tasks)
        after_public_names.extend(before.tasks)
        after_names.extend(after.tasks)
        ws_b, cache_b, device_b = _name_counts(before.tasks)
        ws_a, cache_a, device_a = _name_counts(after.tasks)
        ws_after_public += ws_b
        cache_after_public += cache_b
        device_after_public += device_b
        ws_after += ws_a
        cache_after += cache_a
        device_after += device_a
        asyncgens = asyncgens and generators
        if issue:
            issues.append(issue)
        if after.tasks:
            issues.append('SDK_TASKS_REMAINING_AFTER_DRAIN')
        if capture is not None and loop is capture.cache_loop and loop is not capture.ws_loop and not loop.is_running() and not loop.is_closed() and after.total == 0:
            loop.close()
            cache_loop_closed_by_cfr = True
    remaining_names = sorted(set(after_names))
    if remaining_names:
        issues.append('SDK_TASKS_REMAINING_AFTER_DRAIN')
    if bg_tasks_remaining:
        remaining_names = sorted(set(remaining_names).union(bg_tasks_remaining))
        issues.append('CHANNEL_BG_TASKS_REMAINING_AFTER_DRAIN')
    pre_ws_names = capture.ws_tasks_before_public_stop if capture else tuple(after_public_names)
    pre_cache_names = capture.cache_tasks_before_public_stop if capture else tuple(after_public_names)
    ws_before, _unused_cache, _unused_device = _name_counts(pre_ws_names)
    _unused_ws, cache_before, device_before = _name_counts(pre_cache_names)
    if capture is None:
        ws_before = ws_after_public
        cache_before = cache_after_public
        device_before = device_after_public
    if capture and capture.start_future is not None and not start_future_exited:
        issues.append('FEISHU_CHANNEL_START_FUTURE_STILL_RUNNING')
    if not ws_task_observation_available:
        ws_before = ws_after = None
    if not cache_task_observation_available:
        cache_before = cache_after = None
    before_total = (ws_before or 0) + (cache_before or 0) + device_before
    after_total = (ws_after or 0) + (cache_after or 0) + device_after
    if before_total == 0 and after_total == 0 and not issues:
        mode = 'PUBLIC_LIFECYCLE_ONLY'
    elif after_total == 0 and not issues:
        mode = 'SDK_TASK_DRAIN_REQUIRED'
    else:
        mode = 'SDK_TASK_DRAIN_REQUIRED' if before_total > 0 or after_total > 0 or issues else 'NOT_REQUIRED'
    if not unique and not device_flow_scheduled and capture is None:
        mode = 'NOT_REQUIRED'
    device_flow_attempted = bool(df_preclose and df_preclose.attempted)
    device_flow_invocation_observed = bool(
        df_preclose and df_preclose.attempted and (df_preclose.scheduled or df_preclose.completed)
    )
    device_flow_preclose_count = 1 if device_flow_invocation_observed else (0 if df_capture else None)
    device_flow_public_stop_count = 0 if df_preclose and df_preclose.completed else None
    device_flow_close_kind = ('TASK' if df_preclose and df_preclose.compatibility_mode == 'PRECLOSE_DRIVE_NONRUNNING_LOOP' else 'FUTURE') if df_preclose and df_preclose.scheduled else (
        'NOT_OBSERVED' if not device_flow_attempted else 'COROUTINE'
    )
    device_flow_close_terminal = 'PASS' if df_preclose and df_preclose.completed else (
        'FAIL' if device_flow_attempted else 'NOT_OBSERVED'
    )
    device_flow_cleanup = 'PUBLIC_STOP_IDEMPOTENT_NOOP' if df_preclose and df_preclose.completed else (
        'TASK_CANCEL_AND_GATHER' if df_preclose and df_preclose.timed_out else (
            'RAW_COROUTINE_DISPOSED_UNSCHEDULED' if df_preclose and df_preclose.error_code and not df_preclose.scheduled else 'NOT_OBSERVED'
        )
    )
    return SdkShutdownDiagnostic(
        compatibility_mode=mode,
        device_flow_close_scheduled=device_flow_scheduled,
        device_flow_close_completed=device_flow_completed,
        sdk_ws_tasks_before=ws_before,
        sdk_ws_tasks_drained=(max(0, ws_before - ws_after) if ws_before is not None and ws_after is not None else None),
        sdk_ws_tasks_remaining=ws_after,
        sdk_cache_tasks_before=cache_before,
        sdk_cache_tasks_drained=(max(0, cache_before - cache_after) if cache_before is not None and cache_after is not None else None),
        sdk_cache_tasks_remaining=cache_after,
        sdk_device_flow_tasks_before=device_before,
        sdk_device_flow_tasks_drained=max(0, device_before - device_after),
        sdk_device_flow_tasks_remaining=device_after,
        remaining_task_names=tuple(remaining_names),
        async_generators_shutdown=asyncgens,
        blocking_issues=tuple(sorted(set(issues))),
        pre_shutdown_capture='PASS' if capture is not None else 'NOT_RUN',
        start_future_captured=bool(capture and capture.start_future is not None),
        start_future_exited=start_future_exited,
        ws_client_captured=bool(capture and capture.ws_client_captured),
        ws_loop_captured=bool(capture and capture.ws_loop_captured),
        ws_loop_running_before_public_stop=capture.ws_loop_running_before_public_stop if capture else None,
        ws_loop_closed_before_public_stop=capture.ws_loop_closed_before_public_stop if capture else None,
        ws_tasks_before_public_stop=tuple(pre_ws_names),
        ws_tasks_after_public_stop=tuple(sorted(set(after_public_names))),
        cache_cron_captured=bool(capture and capture.cache_cron_captured),
        cache_loop_running_before=capture.cache_loop_running_before_shutdown if capture else None,
        cache_loop_closed_before=capture.cache_loop_closed_before_shutdown if capture else None,
        cache_loop_same_as_ws_loop=capture.cache_loop_same_as_ws_loop if capture else None,
        cache_cron_task_done_before=capture.cache_cron_task_done_before_shutdown if capture else None,
        cache_loop_closed_by_cfr=cache_loop_closed_by_cfr,
        ws_task_observation_available=ws_task_observation_available,
        cache_task_observation_available=cache_task_observation_available,
        bg_loop_captured=bool(capture and capture.bg_loop_captured),
        bg_thread_captured=bool(capture and capture.bg_thread_captured),
        bg_loop_running_before_public_stop=capture.bg_loop_running_before_public_stop if capture else None,
        bg_loop_closed_before_public_stop=capture.bg_loop_closed_before_public_stop if capture else None,
        bg_thread_alive_before_public_stop=capture.bg_thread_alive_before_public_stop if capture else None,
        bg_thread_exited=bg_thread_exited,
        bg_task_observation_available=bg_task_observation_available,
        bg_tasks_before_public_stop=capture.bg_tasks_before_public_stop if capture else (),
        bg_tasks_after_public_stop=bg_tasks_after_public,
        bg_tasks_drained=bg_tasks_drained,
        bg_tasks_remaining=len(bg_tasks_remaining) if bg_task_observation_available else None,
        bg_remaining_task_names=tuple(bg_tasks_remaining),
        bg_async_generators_shutdown=bg_async_generators,
        bg_default_executor_shutdown=bg_default_executor_shutdown,
        bg_loop_closed_by_cfr=bg_loop_closed_by_cfr,
        bg_loop_closed_after_public_stop=bg_loop_closed_after_public_stop,
        bg_loop_closure_state=bg_loop_closure_state,
        bg_task_observation_status=bg_task_observation_status,
        bg_task_terminal_evidence=bg_task_terminal_evidence,
        device_flow_captured=df_capture,
        device_flow_http_present_before=df_http_before,
        device_flow_http_owned_before=df_http_owned_before,
        device_flow_http_closed_before=df_http_closed_before,
        device_flow_preclose_attempted=df_preclose.attempted if df_preclose else False,
        device_flow_preclose_scheduled=df_preclose.scheduled if df_preclose else False,
        device_flow_preclose_completed=df_preclose.completed if df_preclose else False,
        device_flow_preclose_timed_out=df_preclose.timed_out if df_preclose else False,
        device_flow_preclose_cancelled_after_timeout=df_preclose.cancelled_after_timeout if df_preclose else False,
        device_flow_preclose_cancellation_observed=df_preclose.cancellation_observed if df_preclose else False,
        device_flow_preclose_elapsed_ms=df_preclose.elapsed_ms if df_preclose else None,
        device_flow_http_present_after=df_preclose.http_present_after if df_preclose else None,
        device_flow_http_closed_after=df_preclose.http_closed_after if df_preclose else None,
        device_flow_preclose_error_code=df_preclose.error_code if df_preclose else None,
        device_flow_shutdown_compatibility_mode=df_preclose.compatibility_mode if df_preclose else 'NOT_REQUIRED',
        device_flow_object_observed=df_capture,
        device_flow_object_same_across_preclose_and_public_stop=True if df_preclose and df_preclose.completed else None,
        device_flow_close_invocation_count_observed=device_flow_invocation_observed,
        device_flow_preclose_invocation_count=device_flow_preclose_count,
        device_flow_public_stop_close_invocation_count=device_flow_public_stop_count,
        device_flow_close_kind=device_flow_close_kind,
        device_flow_close_coroutine_created=device_flow_invocation_observed,
        device_flow_close_coroutine_scheduled=bool(df_preclose and df_preclose.scheduled),
        device_flow_close_task_captured=False,
        device_flow_close_task_done=bool(df_preclose and df_preclose.completed),
        device_flow_close_task_cancelled=bool(df_preclose and df_preclose.cancelled_after_timeout),
        device_flow_close_awaited_to_terminal=bool(df_preclose and df_preclose.completed),
        device_flow_raw_coroutine_leak=False if device_flow_invocation_observed else None,
        device_flow_close_owner_loop_observed=bool(capture and capture.bg_loop_captured),
        device_flow_close_owner_loop_running=capture.bg_loop_running_before_public_stop if capture else None,
        device_flow_close_owner_loop_closed=capture.bg_loop_closed_before_public_stop if capture else None,
        device_flow_close_awaited=bool(df_preclose and df_preclose.completed),
        device_flow_close_done=bool(df_preclose and df_preclose.completed),
        device_flow_close_cancelled=bool(df_preclose and df_preclose.cancelled_after_timeout),
        device_flow_close_timed_out=bool(df_preclose and df_preclose.timed_out),
        device_flow_close_terminal_evidence=device_flow_close_terminal,
        device_flow_close_cleanup_mechanism=device_flow_cleanup,
        device_flow_owner_loop_is_ws_loop=bool(capture and capture.bg_loop is not None and capture.bg_loop is capture.ws_loop),
        device_flow_owner_loop_is_bg_loop=bool(capture and capture.bg_loop_captured),
        device_flow_owner_loop_is_cache_loop=bool(capture and capture.bg_loop is not None and capture.bg_loop is capture.cache_loop),
        device_flow_owner_loop_is_transport_loop=False,
        bg_pre_stop_drain_attempted=bg_pre_stop.attempted if isinstance(bg_pre_stop, BgPreStopDrainResult) else False,
        bg_pre_stop_drain_mechanism=bg_pre_stop.mechanism if isinstance(bg_pre_stop, BgPreStopDrainResult) else 'NOT_RUN',
        bg_pre_stop_loop_running=bg_pre_stop.loop_running if isinstance(bg_pre_stop, BgPreStopDrainResult) else None,
        bg_pre_stop_thread_alive=bg_pre_stop.thread_alive if isinstance(bg_pre_stop, BgPreStopDrainResult) else None,
        bg_pre_stop_tracked_future_count=bg_pre_stop.tracked_future_count if isinstance(bg_pre_stop, BgPreStopDrainResult) else 0,
        bg_pre_stop_tracked_futures_cancelled=bg_pre_stop.tracked_futures_cancelled if isinstance(bg_pre_stop, BgPreStopDrainResult) else 0,
        bg_pre_stop_tasks_before=bg_pre_stop.tasks_before if isinstance(bg_pre_stop, BgPreStopDrainResult) else (),
        bg_pre_stop_tasks_cancelled=bg_pre_stop.tasks_cancelled if isinstance(bg_pre_stop, BgPreStopDrainResult) else 0,
        bg_pre_stop_tasks_remaining=bg_pre_stop.tasks_remaining if isinstance(bg_pre_stop, BgPreStopDrainResult) else (),
        bg_pre_stop_drain_completed=bg_pre_stop.completed if isinstance(bg_pre_stop, BgPreStopDrainResult) else False,
        bg_pre_stop_drain_timed_out=bg_pre_stop.timed_out if isinstance(bg_pre_stop, BgPreStopDrainResult) else False,
        bg_pre_stop_terminal_evidence=bg_pre_stop.terminal_evidence if isinstance(bg_pre_stop, BgPreStopDrainResult) else 'NOT_RUN',
        bg_pre_stop_loop_stop_allowed=bg_pre_stop.loop_stop_allowed if isinstance(bg_pre_stop, BgPreStopDrainResult) else False,
        bg_pre_stop_error_code=bg_pre_stop.error_code if isinstance(bg_pre_stop, BgPreStopDrainResult) else None,
        start_future_wrapper_cancelled=bool(getattr(handoff, 'start_future_wrapper_cancelled', False)),
        start_worker_terminal_authority=(
            'START_FUTURE_NATURAL_COMPLETION'
            if handoff is not None and getattr(handoff, 'start_future', None) is not None
            else ('NO_START_WORKER_CAPTURED' if handoff is not None else 'NOT_OBSERVED')
        ),
        start_worker_terminal_wait_attempted=bool(getattr(handoff, 'start_worker_terminal_wait_attempted', False)),
        start_worker_exited=bool(getattr(handoff, 'start_worker_exited', False)),
        start_worker_terminal_timed_out=bool(getattr(handoff, 'start_worker_terminal_timed_out', False)),
        start_worker_terminal_evidence=str(getattr(handoff, 'start_worker_terminal_evidence', 'NOT_RUN')),
        bg_ownership_handoff_attempted=bool(getattr(handoff, 'attempted', False)),
        bg_scheduling_blocked_before_detach=bool(getattr(handoff, 'scheduling_blocked_before_detach', False)),
        bg_ownership_handoff_completed=bool(getattr(handoff, 'completed', False)),
        bg_ownership_detached_from_sdk=bool(getattr(handoff, 'detached_from_sdk', False)),
        bg_producer_quiescence_contract=str(getattr(handoff, 'producer_quiescence_contract', 'NOT_RUN')),
        late_bg_task_created_after_handoff=bool(getattr(handoff, 'late_bg_task_created_after_handoff', False)),
        start_worker_terminal_before_bg_drain=bool(getattr(handoff, 'start_worker_terminal_before_bg_drain', False)),
        bg_final_drain_attempted=bool(getattr(getattr(handoff, 'final_drain', None), 'attempted', False)),
        bg_final_drain_tasks_before=tuple(getattr(getattr(handoff, 'final_drain', None), 'tasks_before', ())),
        bg_final_drain_tasks_remaining=tuple(getattr(getattr(handoff, 'final_drain', None), 'tasks_remaining', ())),
        bg_final_drain_terminal_evidence=str(getattr(getattr(handoff, 'final_drain', None), 'terminal_evidence', 'NOT_RUN')),
        bg_captured_loop_stop_allowed=bool(getattr(handoff, 'captured_loop_stop_allowed', False)),
        host_072914_late_sleep_race_regression=(
            'PASS' if handoff is not None and getattr(handoff, 'late_bg_task_created_after_handoff', False) is False and getattr(handoff, 'start_worker_exited', False) else 'NOT_RUN'
        ),
    )
