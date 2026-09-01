from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
import uuid
from typing import Any


TURN_TIMELINE_MAX_EVENTS = 20


def _elapsed_ms(start, end):
    if start is None or end is None:
        return None
    return max(0, round((end - start) * 1000))


@dataclass
class TurnTelemetry:
    """Bounded, process-local observation of one CFR/native Turn lifecycle."""

    correlation_id: str
    job_id: str = field(default_factory=lambda: f'job-{uuid.uuid4().hex[:12]}')
    thread_id: str | None = None
    turn_id: str | None = None
    status: str = 'queued'
    stage: str = 'queued'
    model: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    timestamps: dict[str, float] = field(default_factory=dict)
    wall_timestamps: dict[str, float] = field(default_factory=dict)
    token_usage: dict[str, int] = field(default_factory=dict)
    last_token_usage: dict[str, int] = field(default_factory=dict)
    model_context_window: int | None = None
    transport_state: str | None = None
    transport_retry_count: int = 0
    transport_fallback_count: int = 0
    current_tool: dict[str, Any] | None = None
    recent_tool: dict[str, Any] | None = None
    timeline: deque = field(default_factory=lambda: deque(maxlen=TURN_TIMELINE_MAX_EVENTS))
    _tool_started: OrderedDict = field(default_factory=OrderedDict, repr=False)
    _tool_total_ms: int = field(default=0, repr=False)
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    TIMESTAMP_NAMES = (
        'task_received_at', 'task_queued_at', 'task_execution_started_at',
        'runtime_acquire_started_at', 'runtime_acquired_at',
        'app_server_start_started_at', 'app_server_started_at',
        'initialize_started_at', 'initialize_completed_at',
        'thread_resume_started_at', 'thread_resume_completed_at',
        'turn_start_requested_at', 'turn_started_at', 'first_native_activity_at',
        'model_activity_started_at', 'transport_retry_started_at', 'transport_fallback_at',
        'transport_recovered_at', 'reasoning_started_at', 'planning_started_at', 'agent_message_started_at',
        'first_answer_delta_at', 'turn_completed_at',
        'runtime_cleanup_started_at', 'runtime_cleanup_completed_at',
        'final_reply_started_at', 'final_reply_completed_at',
    )

    def mark(self, name, *, at=None, wall_at=None, overwrite=False):
        at = time.monotonic() if at is None else at
        wall_at = time.time() if wall_at is None else wall_at
        with self._lock:
            if overwrite or name not in self.timestamps:
                self.timestamps[name] = at
                self.wall_timestamps[name] = wall_at
            return self.timestamps.get(name)

    def mark_wall(self, name, wall_at):
        if wall_at is None:
            return None
        with self._lock:
            self.wall_timestamps.setdefault(name, wall_at)
            return self.wall_timestamps[name]

    def set_identity(self, *, thread_id=None, turn_id=None):
        with self._lock:
            if thread_id:
                self.thread_id = thread_id
            if turn_id:
                self.turn_id = turn_id

    def set_runtime_settings(self, values):
        values = values or {}
        with self._lock:
            self.model = values.get('model') or self.model
            self.reasoning_effort = values.get('reasoningEffort') or values.get('effort') or self.reasoning_effort
            self.service_tier = values.get('serviceTier') or self.service_tier

    def add_event(self, label, *, category='lifecycle', status=None, at=None, wall_at=None):
        at = time.monotonic() if at is None else at
        wall_at = time.time() if wall_at is None else wall_at
        event = {'at': wall_at, 'category': category, 'label': label}
        if status:
            event['status'] = status
        with self._lock:
            self.timeline.append(event)
        return at

    def set_stage(self, stage, *, status=None, event=None, at=None, wall_at=None):
        with self._lock:
            self.stage = stage
            if status:
                self.status = status
        if event:
            self.add_event(event, status=status, at=at, wall_at=wall_at)

    def observe_native_activity(self, *, at=None, wall_at=None, count_for_ttfn=True):
        at = time.monotonic() if at is None else at
        wall_at = time.time() if wall_at is None else wall_at
        with self._lock:
            if count_for_ttfn and 'first_native_activity_at' not in self.timestamps:
                self.timestamps['first_native_activity_at'] = at
                self.wall_timestamps['first_native_activity_at'] = wall_at
            self.timestamps['last_native_activity_at'] = at
            self.wall_timestamps['last_native_activity_at'] = wall_at

    def observe_transport_retry(self, *, at=None, wall_at=None):
        at = time.monotonic() if at is None else at
        wall_at = time.time() if wall_at is None else wall_at
        with self._lock:
            if 'transport_retry_started_at' not in self.timestamps:
                self.timestamps['transport_retry_started_at'] = at
                self.wall_timestamps['transport_retry_started_at'] = wall_at
            self.transport_retry_count += 1
            self.transport_state = 'retrying'
            self.timeline.append({'at': wall_at, 'category': 'transport', 'label': 'Codex transport reconnecting', 'status': 'retrying'})

    def observe_transport_fallback(self, *, at=None, wall_at=None):
        at = time.monotonic() if at is None else at
        wall_at = time.time() if wall_at is None else wall_at
        with self._lock:
            if 'transport_fallback_at' not in self.timestamps:
                self.timestamps['transport_fallback_at'] = at
                self.wall_timestamps['transport_fallback_at'] = wall_at
            self.transport_fallback_count += 1
            self.transport_state = 'https_fallback'
            self.timeline.append({'at': wall_at, 'category': 'transport', 'label': 'Codex switched to HTTPS', 'status': 'fallback'})

    def observe_model_activity(self, *, at=None, wall_at=None):
        at = time.monotonic() if at is None else at
        wall_at = time.time() if wall_at is None else wall_at
        with self._lock:
            if 'model_activity_started_at' not in self.timestamps:
                self.timestamps['model_activity_started_at'] = at
                self.wall_timestamps['model_activity_started_at'] = wall_at
            if self.transport_retry_count and 'transport_recovered_at' not in self.timestamps:
                self.timestamps['transport_recovered_at'] = at
                self.wall_timestamps['transport_recovered_at'] = wall_at
                if self.transport_fallback_count == 0:
                    self.transport_state = 'websocket_recovered'
                self.timeline.append({'at': wall_at, 'category': 'transport', 'label': 'Codex response stream resumed', 'status': self.transport_state or 'recovered'})

    @staticmethod
    def _safe_tool(item):
        item_type = item.get('type')
        if item_type == 'mcpToolCall':
            context = item.get('appContext') or {}
            server = context.get('appName') or item.get('server') or 'MCP'
            tool = context.get('actionName') or item.get('tool') or 'tool'
            return 'mcp', f'{server} · {tool}'
        if item_type == 'dynamicToolCall':
            namespace = item.get('namespace') or 'Dynamic tool'
            return 'dynamic', f'{namespace} · {item.get("tool") or "tool"}'
        if item_type == 'commandExecution':
            return 'command', 'Command execution'
        if item_type == 'fileChange':
            return 'file_change', 'File change'
        if item_type in {'collabAgentToolCall', 'subAgentActivity'}:
            return 'multi_agent', 'Sub-agent activity'
        return None

    def observe_tool(self, item, *, completed=False, at=None, wall_at=None):
        safe = self._safe_tool(item)
        if safe is None:
            return
        at = time.monotonic() if at is None else at
        wall_at = time.time() if wall_at is None else wall_at
        category, name = safe
        item_id = str(item.get('id') or f'{category}:{name}')
        native_status = item.get('status')
        failed = native_status == 'failed' or item.get('success') is False or item.get('error') is not None
        status = 'failed' if failed else ('completed' if completed else 'running')
        with self._lock:
            if not completed:
                self._tool_started[item_id] = at
                while len(self._tool_started) > TURN_TIMELINE_MAX_EVENTS:
                    self._tool_started.popitem(last=False)
            started = self._tool_started.pop(item_id, None) if completed else at
            elapsed = item.get('durationMs') if completed else None
            if elapsed is None and completed:
                elapsed = _elapsed_ms(started, at)
            tool = {'category': category, 'name': name, 'status': status, 'elapsed_ms': elapsed}
            if completed:
                self.recent_tool = tool
                self._tool_total_ms += elapsed or 0
                if self.current_tool and self.current_tool.get('name') == name:
                    self.current_tool = None
            else:
                self.current_tool = tool
            self.timeline.append({'at': wall_at, 'category': 'tool', 'label': name, 'status': status})

    def update_token_usage(self, payload):
        usage = (payload or {}).get('tokenUsage') or {}
        total = usage.get('total') or {}
        last = usage.get('last') or {}

        def safe(values):
            mapping = {
                'input_tokens': 'inputTokens',
                'cached_input_tokens': 'cachedInputTokens',
                'cache_write_input_tokens': 'cacheWriteInputTokens',
                'output_tokens': 'outputTokens',
                'reasoning_output_tokens': 'reasoningOutputTokens',
                'total_tokens': 'totalTokens',
            }
            return {local: values[native] for local, native in mapping.items() if isinstance(values.get(native), int)}

        with self._lock:
            self.token_usage = safe(total)
            self.last_token_usage = safe(last)
            window = usage.get('modelContextWindow')
            self.model_context_window = window if isinstance(window, int) and window > 0 else None

    def _metrics(self, now, wall_now):
        t = self.timestamps
        w = self.wall_timestamps
        transport_retry_ms = None
        if t.get('transport_retry_started_at') is not None:
            transport_end = t.get('transport_fallback_at') or t.get('transport_recovered_at') or t.get('model_activity_started_at') or t.get('turn_completed_at') or now
            transport_retry_ms = _elapsed_ms(t.get('transport_retry_started_at'), transport_end)
        raw_response_wait_ms = _elapsed_ms(t.get('turn_started_at'), t.get('model_activity_started_at'))
        model_response_wait_ms = None if raw_response_wait_ms is None else max(0, raw_response_wait_ms - (transport_retry_ms or 0))
        metrics = {
            'queue_ms': _elapsed_ms(w.get('task_queued_at'), w.get('task_execution_started_at')),
            'runtime_prep_ms': _elapsed_ms(t.get('task_execution_started_at'), t.get('initialize_completed_at')),
            'app_server_start_ms': _elapsed_ms(t.get('app_server_start_started_at'), t.get('app_server_started_at')),
            'initialize_ms': _elapsed_ms(t.get('initialize_started_at'), t.get('initialize_completed_at')),
            'thread_resume_ms': _elapsed_ms(t.get('thread_resume_started_at'), t.get('thread_resume_completed_at')),
            'turn_start_ms': _elapsed_ms(t.get('turn_start_requested_at'), t.get('turn_started_at')),
            'ttfn_ms': _elapsed_ms(t.get('turn_started_at'), t.get('first_native_activity_at')),
            'ttft_ms': _elapsed_ms(t.get('turn_started_at'), t.get('first_answer_delta_at')),
            'transport_retry_ms': transport_retry_ms,
            'model_response_wait_ms': model_response_wait_ms,
            'model_to_generation_ms': _elapsed_ms(t.get('turn_started_at'), t.get('agent_message_started_at')),
            'generation_ms': _elapsed_ms(t.get('first_answer_delta_at'), t.get('turn_completed_at')),
            'tool_execution_ms': self._tool_total_ms or None,
            'cleanup_ms': _elapsed_ms(t.get('runtime_cleanup_started_at'), t.get('runtime_cleanup_completed_at')),
            'final_delivery_ms': _elapsed_ms(t.get('final_reply_started_at'), t.get('final_reply_completed_at')),
            'cfr_pre_turn_ms': _elapsed_ms(t.get('task_execution_started_at'), t.get('turn_started_at')),
            'cfr_post_turn_ms': _elapsed_ms(t.get('turn_completed_at'), t.get('final_reply_completed_at')),
            'total_ms': _elapsed_ms(w.get('task_received_at'), w.get('final_reply_completed_at')),
            'elapsed_ms': _elapsed_ms(w.get('task_received_at'), wall_now),
        }
        pre = metrics['cfr_pre_turn_ms']
        post = metrics['cfr_post_turn_ms']
        metrics['cfr_controlled_overhead_ms'] = pre + (post or 0) if pre is not None else None
        return metrics

    def _dominant_owner(self, metrics):
        t = self.timestamps
        phases = {
            'CFR_QUEUE': metrics['queue_ms'],
            'CFR_RUNTIME_STARTUP': metrics['runtime_prep_ms'],
            'CFR_THREAD_RESUME': metrics['thread_resume_ms'],
            'CFR_TURN_START': metrics['turn_start_ms'],
            'CODEX_TRANSPORT_RETRY': metrics['transport_retry_ms'],
            'CODEX_RESPONSE_WAIT': metrics['model_response_wait_ms'],
            'MODEL_REASONING': _elapsed_ms(t.get('reasoning_started_at'), t.get('agent_message_started_at')),
            'TOOL_EXECUTION': metrics['tool_execution_ms'],
            'MODEL_GENERATION': metrics['generation_ms'],
            'CFR_RUNTIME_CLEANUP': metrics['cleanup_ms'],
            'FEISHU_FINAL_DELIVERY': metrics['final_delivery_ms'],
        }
        available = {name: value for name, value in phases.items() if value is not None}
        return max(available, key=available.get) if available else 'UNKNOWN'

    def snapshot(self, *, now=None, wall_now=None):
        now = time.monotonic() if now is None else now
        wall_now = time.time() if wall_now is None else wall_now
        with self._lock:
            metrics = self._metrics(now, wall_now)
            context_used = self.last_token_usage.get('total_tokens')
            context_percent = None
            if context_used is not None and self.model_context_window:
                context_percent = round(context_used * 100 / self.model_context_window, 1)
            return {
                'job_id': self.job_id,
                'thread_id': self.thread_id,
                'turn_id': self.turn_id,
                'status': self.status,
                'stage': self.stage,
                'model': self.model,
                'reasoning_effort': self.reasoning_effort,
                'service_tier': self.service_tier,
                'timestamps': {name: self.wall_timestamps.get(name) for name in self.TIMESTAMP_NAMES},
                'metrics': metrics,
                'dominant_owner': self._dominant_owner(metrics),
                'last_native_activity_at': self.wall_timestamps.get('last_native_activity_at'),
                'last_native_activity_age_ms': _elapsed_ms(self.timestamps.get('last_native_activity_at'), now),
                'transport_state': self.transport_state,
                'transport_retry_count': self.transport_retry_count,
                'transport_fallback_count': self.transport_fallback_count,
                'token_usage': dict(self.token_usage),
                'model_context_window': self.model_context_window,
                'context_used_tokens': context_used,
                'context_usage_percent': context_percent,
                'current_tool': dict(self.current_tool) if self.current_tool else None,
                'recent_tool': dict(self.recent_tool) if self.recent_tool else None,
                'timeline': list(self.timeline),
            }


@dataclass(frozen=True)
class ThreadRef:
    thread_id: str
    name: str | None
    cwd: Path
    rollout_path: Path | None = None


@dataclass(frozen=True)
class TurnResult:
    thread_id: str
    turn_id: str
    status: str
    final_agent_message: str = ''
    started_at: int | None = None
    completed_at: int | None = None
    error_message: str | None = None
    telemetry: TurnTelemetry | None = None


@dataclass(frozen=True)
class BindingRecord:
    thread_id: str
    thread_name: str | None
    cwd: Path
    rollout_path: Path | None
    last_rollout_byte_offset: int = 0
    last_seen_turn_id: str | None = None
    desktop_sync_state: str = 'up_to_date'
    active_turn_id: str | None = None
    writer_state: str = 'idle'
    observed_model: str | None = None
    observed_reasoning_effort: str | None = None
    observed_service_tier: str | None = None
    observed_settings_at: float | None = None
    created_at: float | None = None
    updated_at: float | None = None


@dataclass(frozen=True)
class ConversationResult:
    thread: ThreadRef
    initial_turn: TurnResult

    @property
    def thread_id(self) -> str:
        return self.thread.thread_id

    @property
    def name(self) -> str | None:
        return self.thread.name

    @property
    def cwd(self) -> Path:
        return self.thread.cwd

    @property
    def rollout_path(self) -> Path | None:
        return self.thread.rollout_path


@dataclass(frozen=True)
class ActiveTurn:
    thread_id: str
    turn_id: str
    client: Any
    state: str = 'cfr_active'
    telemetry: TurnTelemetry | None = None


@dataclass(frozen=True)
class StructuredError(Exception):
    code: str
    message: str
    data: Any = None

    def __str__(self) -> str:
        return f'{self.code}: {self.message}'
