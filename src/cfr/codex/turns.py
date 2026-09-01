from datetime import datetime, timezone
from collections import OrderedDict
import threading
import time
import uuid

from cfr.core.models import ActiveTurn, TurnResult, TurnTelemetry


RECENT_TURN_LIMIT = 20


class ActiveTurnRegistry:
    def __init__(self):
        self._active: dict[str, ActiveTurn] = {}
        self._results: OrderedDict[str, TurnResult] = OrderedDict()
        self._pending: OrderedDict[str, TurnTelemetry] = OrderedDict()
        self._telemetry: OrderedDict[str, TurnTelemetry] = OrderedDict()
        self._done: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def begin(self, correlation_id, *, thread_id=None, received_at=None, queued_at=None, execution_started_at=None):
        telemetry = TurnTelemetry(correlation_id, thread_id=thread_id)
        telemetry.mark_wall('task_received_at', received_at)
        telemetry.mark_wall('task_queued_at', queued_at if queued_at is not None else received_at)
        if execution_started_at is not None:
            telemetry.mark('task_execution_started_at', wall_at=execution_started_at)
        telemetry.set_stage('starting', status='running', event='Task execution started')
        with self._lock:
            self._pending[correlation_id] = telemetry
            self._telemetry[correlation_id] = telemetry
            while len(self._telemetry) > RECENT_TURN_LIMIT:
                oldest, _ = self._telemetry.popitem(last=False)
                self._pending.pop(oldest, None)
        return telemetry

    def observe(self, telemetry):
        if telemetry is None:
            return
        with self._lock:
            self._telemetry.pop(telemetry.correlation_id, None)
            self._telemetry[telemetry.correlation_id] = telemetry
            while len(self._telemetry) > RECENT_TURN_LIMIT:
                oldest, _ = self._telemetry.popitem(last=False)
                self._pending.pop(oldest, None)

    def register(self, active: ActiveTurn):
        with self._lock:
            self._active[active.thread_id] = active
            self._done[active.thread_id] = threading.Event()
            if active.telemetry is not None:
                self._pending.pop(active.telemetry.correlation_id, None)
                self._telemetry.pop(active.telemetry.correlation_id, None)
                self._telemetry[active.telemetry.correlation_id] = active.telemetry

    def get(self, thread_id):
        with self._lock:
            return self._active.get(thread_id)

    def telemetry_for(self, correlation_id):
        with self._lock:
            return self._telemetry.get(correlation_id)

    def finish(self, result: TurnResult):
        with self._lock:
            self._results.pop(result.thread_id, None)
            self._results[result.thread_id] = result
            while len(self._results) > RECENT_TURN_LIMIT:
                self._results.popitem(last=False)
            self._active.pop(result.thread_id, None)
            if result.telemetry is not None:
                self._pending.pop(result.telemetry.correlation_id, None)
                self._telemetry.pop(result.telemetry.correlation_id, None)
                self._telemetry[result.telemetry.correlation_id] = result.telemetry
            event = self._done.get(result.thread_id)
            if event:
                event.set()

    def wait(self, thread_id, timeout=None):
        with self._lock:
            event = self._done.get(thread_id)
        if event:
            event.wait(timeout)
        with self._lock:
            return self._results.get(thread_id)

    def telemetry_snapshots(self):
        with self._lock:
            telemetry = tuple(self._telemetry.values())
        return [item.snapshot() for item in reversed(telemetry)]

    def runtime_snapshot(self):
        with self._lock:
            active = tuple(self._active.values())
            results = tuple(self._results.values())
            telemetry = tuple(self._telemetry.values())
        return {
            'active': active,
            'results': results,
            'telemetry': [item.snapshot() for item in reversed(telemetry)],
        }


class TurnManager:
    def __init__(self, client, registry=None):
        self.client = client
        self.registry = registry or ActiveTurnRegistry()

    @staticmethod
    def _turn_id(result):
        turn = (result or {}).get('turn', {})
        turn_id = turn.get('id') or (result or {}).get('turnId')
        if not turn_id:
            raise RuntimeError('turn/start returned no turn id')
        return turn_id

    @staticmethod
    def _timestamp(value):
        if value is None:
            return None
        return int(value)

    @staticmethod
    def _message_from_item(item):
        if item.get('type') == 'agentMessage':
            return item.get('text') or ''
        return ''

    def start_turn(self, thread_id, text):
        return self.client.request('turn/start', {
            'threadId': thread_id,
            'input': [{'type': 'text', 'text': text}],
        })

    @staticmethod
    def _progress(callback, **values):
        if callback is not None:
            try:
                callback(values)
            except Exception:
                pass

    def run_turn(self, thread_id, text, timeout=None, *, turn_timeout=None, on_progress=None, telemetry=None):
        timeout = turn_timeout if turn_timeout is not None else timeout or self.client.timeout
        started_at = int(time.time())
        telemetry = telemetry or self.registry.begin(
            f'local-{uuid.uuid4().hex}', thread_id=thread_id,
            received_at=time.time(), queued_at=time.time(), execution_started_at=time.time(),
        )
        telemetry.set_identity(thread_id=thread_id)
        self.registry.observe(telemetry)
        subscription = self.client.subscribe(lambda message: message.get('params', {}).get('threadId') == thread_id)
        turn_id = None
        deltas = {}
        summary = ''
        preview = ''
        try:
            telemetry.mark('turn_start_requested_at')
            response = self.start_turn(thread_id, text)
            turn_id = self._turn_id(response)
            telemetry.mark('turn_started_at')
            telemetry.set_identity(turn_id=turn_id)
            telemetry.set_stage('running', status='running', event='Turn started')
            self.registry.register(ActiveTurn(thread_id, turn_id, self.client, telemetry=telemetry))
            self._progress(on_progress, state='running', thread_id=thread_id, turn_id=turn_id, steer_available=True, stage='running')
            deadline = time.monotonic() + timeout
            completed = None
            while completed is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    telemetry.mark('turn_completed_at')
                    telemetry.set_stage('timed_out', status='timed_out', event='Turn timed out')
                    result = TurnResult(thread_id, turn_id, 'timeout', ''.join(deltas.values()), started_at, int(time.time()), telemetry=telemetry)
                    self.registry.finish(result)
                    self._progress(on_progress, state='timed_out', thread_id=thread_id, turn_id=turn_id, steer_available=False, stage='timed_out')
                    return result
                try:
                    message = subscription.get(timeout=remaining)
                except Exception as exc:
                    if isinstance(exc, TimeoutError):
                        raise
                    from queue import Empty
                    if isinstance(exc, Empty):
                        telemetry.mark('turn_completed_at')
                        telemetry.set_stage('timed_out', status='timed_out', event='Turn timed out')
                        result = TurnResult(thread_id, turn_id, 'timeout', ''.join(deltas.values()), started_at, int(time.time()), telemetry=telemetry)
                        self.registry.finish(result)
                        self._progress(on_progress, state='timed_out', thread_id=thread_id, turn_id=turn_id, steer_available=False, stage='timed_out')
                        return result
                    raise
                method = message.get('method')
                params = message.get('params', {})
                candidate_turn_id = params.get('turnId') or params.get('turn', {}).get('id')
                if candidate_turn_id and candidate_turn_id != turn_id:
                    continue
                native_at = time.monotonic()
                telemetry.observe_native_activity(at=native_at, count_for_ttfn=method != 'turn/started')
                self._progress(on_progress, last_native_activity_at=native_at)
                if method == 'turn/started':
                    self._progress(on_progress, state='running', thread_id=thread_id, turn_id=turn_id, steer_available=True, stage='running')
                elif method == 'error':
                    error = params.get('error') or {}
                    message = str(error.get('message') or '')
                    if params.get('willRetry') and message.startswith('Reconnecting'):
                        telemetry.observe_transport_retry(at=native_at)
                        self._progress(on_progress, state='running', thread_id=thread_id, turn_id=turn_id, steer_available=True, stage='transport_retry')
                elif method == 'warning':
                    message = str(params.get('message') or '')
                    if message.startswith('Falling back from WebSockets to HTTPS transport'):
                        telemetry.observe_transport_fallback(at=native_at)
                        self._progress(on_progress, state='running', thread_id=thread_id, turn_id=turn_id, steer_available=True, stage='transport_fallback')
                elif method == 'item/agentMessage/delta':
                    telemetry.observe_model_activity(at=native_at)
                    if 'agent_message_started_at' not in telemetry.timestamps:
                        telemetry.mark('agent_message_started_at', at=native_at)
                        telemetry.set_stage('generating', event='Generating reply', at=native_at)
                    if 'first_answer_delta_at' not in telemetry.timestamps:
                        telemetry.mark('first_answer_delta_at', at=native_at)
                        telemetry.add_event('First output', at=native_at)
                    item_id = params.get('itemId', '')
                    delta = params.get('delta') or ''
                    deltas[item_id] = deltas.get(item_id, '') + delta
                    preview = (preview + delta)[-2000:]
                    self._progress(on_progress, state='generating', thread_id=thread_id, turn_id=turn_id, steer_available=True, stage='generating', answer_preview=preview)
                elif method == 'item/reasoning/summaryTextDelta':
                    telemetry.observe_model_activity(at=native_at)
                    if 'reasoning_started_at' not in telemetry.timestamps:
                        telemetry.mark('reasoning_started_at', at=native_at)
                        telemetry.set_stage('reasoning', event='Reasoning started', at=native_at)
                    summary = (summary + (params.get('delta') or ''))[-1200:]
                    self._progress(on_progress, state='running', thread_id=thread_id, turn_id=turn_id, steer_available=True, stage='reasoning', reasoning_summary=summary)
                elif method == 'item/plan/delta':
                    telemetry.observe_model_activity(at=native_at)
                    if 'planning_started_at' not in telemetry.timestamps:
                        telemetry.mark('planning_started_at', at=native_at)
                        telemetry.set_stage('planning', event='Planning started', at=native_at)
                    self._progress(on_progress, state='running', thread_id=thread_id, turn_id=turn_id, steer_available=True, stage='planning')
                elif method == 'thread/tokenUsage/updated':
                    telemetry.update_token_usage(params)
                elif method == 'item/started':
                    item = params.get('item') or {}
                    item_type = item.get('type')
                    if item_type and item_type != 'userMessage':
                        telemetry.observe_model_activity(at=native_at)
                    if item_type == 'userMessage' and str(item.get('clientId') or '').startswith('cfr-steer-'):
                        self._progress(on_progress, thread_id=thread_id, turn_id=turn_id, steer_available=True, steer_received=True)
                    if item_type == 'reasoning' and 'reasoning_started_at' not in telemetry.timestamps:
                        telemetry.mark('reasoning_started_at', at=native_at)
                        telemetry.set_stage('reasoning', event='Reasoning started', at=native_at)
                    elif item_type == 'plan' and 'planning_started_at' not in telemetry.timestamps:
                        telemetry.mark('planning_started_at', at=native_at)
                        telemetry.set_stage('planning', event='Planning started', at=native_at)
                    elif item_type == 'agentMessage' and 'agent_message_started_at' not in telemetry.timestamps:
                        telemetry.mark('agent_message_started_at', at=native_at)
                        telemetry.set_stage('generating', event='Generating reply', at=native_at)
                    telemetry.observe_tool(item, at=native_at)
                    stage = {
                        'reasoning': 'reasoning', 'plan': 'planning', 'agentMessage': 'generating',
                        'commandExecution': 'tool', 'mcpToolCall': 'tool', 'dynamicToolCall': 'tool',
                        'fileChange': 'file_change', 'collabAgentToolCall': 'multi_agent', 'subAgentActivity': 'multi_agent',
                    }.get(item_type)
                    if stage:
                        if stage in {'tool', 'file_change', 'multi_agent'}:
                            telemetry.set_stage(stage)
                        self._progress(on_progress, state='generating' if stage == 'generating' else 'running', thread_id=thread_id, turn_id=turn_id, steer_available=True, stage=stage)
                elif method == 'item/completed':
                    item = params.get('item') or {}
                    telemetry.observe_tool(item, completed=True, at=native_at)
                elif method == 'turn/completed':
                    completed = params.get('turn') or {}
                    status = completed.get('status', 'completed')
                    stage = {
                        'completed': 'completed', 'interrupted': 'stopped',
                        'timeout': 'timed_out', 'timed_out': 'timed_out',
                    }.get(status, 'failed')
                    telemetry.mark('turn_completed_at', at=native_at)
                    telemetry.set_stage(stage, status=stage, event=f'Turn {stage}', at=native_at)
            final_message = ''.join(deltas.values())
            if not final_message:
                final_message = ''.join(self._message_from_item(item) for item in completed.get('items', []))
            result = TurnResult(
                thread_id,
                turn_id,
                completed.get('status', 'completed'),
                final_message,
                self._timestamp(completed.get('startedAt')) or started_at,
                self._timestamp(completed.get('completedAt')) or int(time.time()),
                (completed.get('error') or {}).get('message'),
                telemetry,
            )
            self.registry.finish(result)
            state = {
                'completed': 'completed',
                'interrupted': 'stopped',
                'timeout': 'timed_out',
                'timed_out': 'timed_out',
            }.get(result.status, 'failed')
            self._progress(on_progress, state=state, thread_id=thread_id, turn_id=turn_id, steer_available=False, stage=state)
            return result
        except Exception as exc:
            if turn_id and self.registry.get(thread_id):
                telemetry.mark('turn_completed_at')
                telemetry.set_stage('failed', status='failed', event='Turn failed')
                result = TurnResult(
                    thread_id,
                    turn_id,
                    'failed',
                    ''.join(deltas.values()),
                    started_at,
                    int(time.time()),
                    str(exc),
                    telemetry,
                )
                self.registry.finish(result)
                self._progress(on_progress, state='failed', thread_id=thread_id, turn_id=turn_id, steer_available=False, stage='failed')
            raise
        finally:
            subscription.close()

    def interrupt_turn(self, thread_id, turn_id):
        return self.client.request('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id})

    def steer_turn(self, thread_id, expected_turn_id, text):
        return self.client.request('turn/steer', {
            'threadId': thread_id,
            'expectedTurnId': expected_turn_id,
            'clientUserMessageId': f'cfr-steer-{uuid.uuid4().hex}',
            'input': [{'type': 'text', 'text': text}],
        })
