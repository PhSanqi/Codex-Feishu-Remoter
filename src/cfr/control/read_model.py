from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import json
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any
from urllib.parse import urlparse

from .codex_catalog import capabilities as codex_capabilities
from .codex_catalog import models as codex_models
from .codex_settings import read as codex_settings
from .codex_settings import write as write_codex_settings
from cfr.feishu.config import load_settings
from cfr.feishu.store import FeishuStore
from cfr.surfaces import execution_surfaces
from cfr.storage.db import BindingStore


CONTROL_CHAT_STATE_LIMIT = 200


@dataclass
class ControlReadModel:
    supervisor: Any
    database: Path

    @cached_property
    def _feishu_store(self) -> FeishuStore:
        return FeishuStore(self.database)

    @cached_property
    def _binding_store(self) -> BindingStore:
        return BindingStore(self.database)

    @cached_property
    def _settings_cache(self) -> dict[str, Any]:
        return {'revision': None, 'checked_at': 0.0, 'value': None}

    @cached_property
    def _codex_path_cache(self) -> dict[str, Any]:
        return {'checked_at': 0.0, 'value': None}

    @cached_property
    def _storage_cache(self) -> dict[str, Any]:
        return {'checked_at': 0.0, 'value': None}

    @cached_property
    def _read_cache_lock(self) -> threading.RLock:
        return threading.RLock()

    @cached_property
    def _catalog_cache(self) -> dict[str, tuple[float, Any]]:
        return {}

    def _cached_read(self, key, operation, ttl):
        now = time.monotonic()
        with self._read_cache_lock:
            cached = self._catalog_cache.get(key)
            if cached and now - cached[0] < ttl:
                return cached[1]
        value = operation()
        with self._read_cache_lock:
            self._catalog_cache[key] = (time.monotonic(), value)
        return value

    def close(self):
        self._feishu_store.close()
        self._binding_store.close()

    def _settings(self):
        config_store = getattr(self.supervisor, '_config_store', None)
        path = getattr(config_store, 'path', None)
        revision: tuple[str, int | None, int | None, int | None, int | None] | None = None
        if path is not None:
            try:
                stat = Path(path).stat()
                revision = (str(Path(path).resolve()), stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
            except OSError:
                revision = (str(Path(path).resolve()), None, None, None, None)
        with self._read_cache_lock:
            cache = self._settings_cache
            now = time.monotonic()
            expired_untracked = revision is None and now - cache['checked_at'] >= 5.0
            if (
                cache['value'] is None
                or cache['revision'] != revision
                or expired_untracked
            ):
                cache['value'] = (
                    self.supervisor._load_settings()
                    if hasattr(self.supervisor, '_load_settings')
                    else load_settings(database=self.database)
                )
                cache['revision'] = revision
                cache['checked_at'] = now
            return cache['value']

    def _codex_path(self):
        with self._read_cache_lock:
            cache = self._codex_path_cache
            now = time.monotonic()
            if now - cache['checked_at'] >= 5.0:
                cache['value'] = shutil.which('codex')
                cache['checked_at'] = now
            return cache['value']

    def sessions(self, code_sessions=None, chat_bindings=None, surface_states=None) -> list[dict[str, Any]]:
        """Expose one composite Code/Chat state per Feishu chat."""
        store = self._feishu_store
        if code_sessions is None and chat_bindings is None and surface_states is None:
            state = store.recent_control_chat_state(CONTROL_CHAT_STATE_LIMIT)
            code_sessions = state['sessions']
            chat_bindings = state['chat_bindings']
            surface_states = state['surface_states']
        code = {
            item.chat_id: item
            for item in (code_sessions or ())
        }
        chat = {
            item['chat_id']: item
            for item in (chat_bindings or ())
        }
        surfaces = {
            item['chat_id']: item
            for item in (surface_states or ())
        }
        config_store = getattr(self.supervisor, '_config_store', None)
        default_surface = config_store.get_default_surface() if config_store is not None else 'code'
        chat_ids = set(code) | set(chat) | set(surfaces)
        rows = []
        for chat_id in chat_ids:
            session = code.get(chat_id)
            browser = chat.get(chat_id)
            surface = surfaces.get(chat_id)
            url = (browser or {}).get('url')
            try:
                path = urlparse(str(url or '')).path
            except ValueError:
                path = ''
            conversation = re.search(r'/c/([^/?#]+)', path, re.IGNORECASE)
            project = re.search(r'/g/(g-p-[0-9a-f]+)(?:-[^/]+)?(?:/|$)', path, re.IGNORECASE)
            pending_settings = None
            if session and session.pending_settings_json:
                try:
                    pending_settings = json.loads(session.pending_settings_json)
                except (TypeError, json.JSONDecodeError):
                    pending_settings = None
            updated_at = max(
                session.updated_at if session else 0,
                (browser or {}).get('updated_at') or 0,
                (surface or {}).get('updated_at') or 0,
            )
            rows.append({
                'chat_id': chat_id,
                'chat_type': session.chat_type if session else 'unknown',
                'state': session.state if session else 'unbound',
                'selected_surface': (surface or {}).get('selected_surface') or default_surface,
                'thread_id': session.thread_id if session else None,
                'pending_cwd': session.pending_cwd if session else None,
                'pending_settings': pending_settings,
                'approval_mode': session.approval_mode if session else 'ask',
                'chat_state': 'conversation' if conversation else ('new_pending' if browser else 'unbound'),
                'chat_tab_id': (browser or {}).get('tab_id'),
                'chat_url': url,
                'chat_project_id': project.group(1) if project else None,
                'chat_conversation_id': conversation.group(1) if conversation else None,
                'updated_at': updated_at,
            })
        rows.sort(key=lambda item: item['updated_at'], reverse=True)
        return rows

    def bindings(self, binding_records=None, code_sessions=None, bound_chats=None) -> list[dict[str, Any]]:
        """Expose durable CFR/Codex thread bindings without rollout contents."""
        if bound_chats is None:
            bound_chats = {
                session.thread_id: session.chat_id
                for session in (code_sessions if code_sessions is not None else self._feishu_store.list_sessions())
                if session.thread_id
            }
        return [
            {
                'thread_id': binding.thread_id,
                'thread_name': binding.thread_name,
                'cwd': str(binding.cwd),
                'last_seen_turn_id': binding.last_seen_turn_id,
                'desktop_sync_state': binding.desktop_sync_state,
                'writer_state': binding.writer_state,
                'active_turn_id': binding.active_turn_id,
                'bound_chat_id': bound_chats.get(binding.thread_id),
                'created_at': binding.created_at,
                'updated_at': binding.updated_at,
            }
            for binding in (binding_records if binding_records is not None else self._binding_store.list_bindings(limit=200))
        ]

    @staticmethod
    def _job_status(value: Any) -> str:
        return {
            'completed': 'completed',
            'failed': 'failed',
            'interrupted': 'interrupted',
            'timeout': 'failed',
            'inProgress': 'running',
            'running': 'running',
            'queued': 'queued',
            'starting': 'running',
            'stopped': 'stopped',
            'timed_out': 'timed_out',
        }.get(str(value), 'unknown')

    def jobs(self, binding_records=None, pending_approvals=None) -> list[dict[str, Any]]:
        """Read-only projection of CFR's active and current-process terminal turns."""
        daemon = getattr(self.supervisor, '_daemon', None)
        adapter = getattr(daemon, 'adapter', None)
        registry = getattr(adapter, 'registry', None)
        if registry is not None and hasattr(registry, 'runtime_snapshot'):
            snapshot = registry.runtime_snapshot()
        else:
            snapshot = {'active': (), 'results': ()}
        if binding_records is None:
            thread_ids = {
                str(thread_id)
                for thread_id in (
                    [getattr(turn, 'thread_id', None) for turn in snapshot.get('active', ())]
                    + [getattr(turn, 'thread_id', None) for turn in snapshot.get('results', ())]
                    + [item.get('thread_id') for item in snapshot.get('telemetry', ()) if isinstance(item, dict)]
                )
                if thread_id
            }
            bindings = {
                thread_id: binding
                for thread_id in thread_ids
                if (binding := self._binding_store.get_binding(thread_id)) is not None
            }
        else:
            bindings = {binding.thread_id: binding for binding in binding_records}
        if pending_approvals is None:
            pending_approvals = self._feishu_store.list_pending_approval_turns()

        def workspace(thread_id):
            binding = bindings.get(thread_id)
            return binding.cwd.name if binding and binding.cwd else None

        active_turns = {(turn.thread_id, turn.turn_id) for turn in snapshot['active']}
        observed_turns = set()
        telemetry_jobs = []
        for observed in snapshot.get('telemetry', ()):
            runtime = {key: value for key, value in observed.items() if key != 'job_id'}
            thread_id = runtime.get('thread_id')
            turn_id = runtime.get('turn_id')
            if turn_id:
                observed_turns.add((thread_id, turn_id))
            pending = bool(thread_id and turn_id and (thread_id, turn_id) in pending_approvals)
            timestamps = runtime.get('timestamps') or {}
            terminal = runtime.get('status') in {'completed', 'failed', 'stopped', 'timed_out'}
            telemetry_jobs.append({
                'id': turn_id or observed['job_id'],
                'surface': 'code',
                'thread_id': thread_id,
                'turn_id': turn_id,
                'status': 'waiting_approval' if pending else self._job_status(runtime.get('status')),
                'active': (thread_id, turn_id) in active_turns or (not turn_id and not terminal),
                'origin': 'feishu' if daemon is not None else 'unknown',
                'workspace': workspace(thread_id),
                'started_at': timestamps.get('task_execution_started_at'),
                'completed_at': timestamps.get('final_reply_completed_at') or timestamps.get('turn_completed_at'),
                'last_activity_at': runtime.get('last_native_activity_at'),
                'can_interrupt': (thread_id, turn_id) in active_turns,
                'runtime': runtime,
            })

        active_jobs = []
        for turn in snapshot['active']:
            if (turn.thread_id, turn.turn_id) in observed_turns:
                continue
            pending = (turn.thread_id, turn.turn_id) in pending_approvals
            active_jobs.append({
                'id': turn.turn_id,
                'surface': 'code',
                'thread_id': turn.thread_id,
                'turn_id': turn.turn_id,
                'status': 'waiting_approval' if pending else 'running',
                'active': True,
                'origin': 'feishu' if daemon is not None else 'unknown',
                'workspace': workspace(turn.thread_id),
                'started_at': None,
                'completed_at': None,
                'last_activity_at': None,
                'can_interrupt': True,
            })
        terminal_jobs = [{
            'id': turn.turn_id,
            'surface': 'code',
            'thread_id': turn.thread_id,
            'turn_id': turn.turn_id,
            'status': self._job_status(turn.status),
            'active': False,
            'origin': 'feishu' if daemon is not None else 'unknown',
            'workspace': workspace(turn.thread_id),
            'started_at': turn.started_at,
            'completed_at': turn.completed_at,
            'last_activity_at': None,
            'can_interrupt': False,
        } for turn in snapshot['results'] if (turn.thread_id, turn.turn_id) not in observed_turns]
        terminal_jobs.sort(key=lambda item: item['completed_at'] or 0, reverse=True)
        telemetry_jobs.sort(key=lambda item: (not item['active'], -(item['completed_at'] or item['started_at'] or 0)))
        chat_snapshot = getattr(daemon, 'chat_runtime_snapshot', lambda: ())() if daemon is not None else ()
        chat_jobs = [{
            'id': item.get('id'),
            'surface': 'chat',
            'thread_id': None,
            'turn_id': None,
            'status': item.get('status') or 'unknown',
            'active': bool(item.get('active')),
            'origin': 'feishu',
            'workspace': None,
            'started_at': item.get('started_at'),
            'completed_at': item.get('completed_at'),
            'last_activity_at': item.get('last_activity_at'),
            'can_interrupt': bool(item.get('active')),
            'chat_id': item.get('chat_id'),
            'conversation_id': item.get('conversation_id'),
            'url': item.get('url'),
            'stage': item.get('stage'),
            'attachment_count': item.get('attachment_count'),
            'output_count': item.get('output_count'),
            'error_code': item.get('error_code'),
            'queue_ms': item.get('queue_ms'),
            'total_ms': item.get('total_ms'),
            'current_owner': item.get('current_owner'),
            'timings_ms': item.get('timings_ms') or {},
            'owner_timings_ms': item.get('owner_timings_ms') or {},
            'timeline': item.get('timeline') or [],
            'artifact_failures': item.get('artifact_failures') or [],
        } for item in chat_snapshot]
        return chat_jobs + telemetry_jobs + active_jobs + terminal_jobs

    def models(self) -> dict[str, Any]:
        return self._cached_read('models', codex_models, 60.0)

    def capabilities(self) -> dict[str, Any]:
        return self._cached_read('capabilities', codex_capabilities, 60.0)

    def surfaces(self, chat_id=None) -> dict[str, Any]:
        snapshot = getattr(self.supervisor, 'chat_status_snapshot', None)
        chat_status = snapshot() if snapshot is not None else None
        if chat_id:
            selected = self._feishu_store.get_selected_surface_or_none(chat_id) or self.supervisor._config_store.get_default_surface()
            current_chat_id = str(chat_id)
        else:
            surface_state = self._feishu_store.get_current_surface_state()
            selected = surface_state['selected_surface'] if surface_state else self.supervisor._config_store.get_default_surface()
            current_chat_id = surface_state['chat_id'] if surface_state else None
        return {**execution_surfaces(selected=selected, chat_status=chat_status), 'chat_id': current_chat_id}

    def approvals(self) -> list[dict[str, Any]]:
        """Sanitized read-only projection of CFR's durable approval authority."""
        return [{
            'approval_id': row.get('approval_id'),
            'thread_id': row.get('thread_id'),
            'turn_id': row.get('turn_id'),
            'state': row.get('state'),
            'decision': row.get('decision'),
            'feedback_state': row.get('feedback_state'),
            'request_kind': row.get('kind'),
            'created_at': row.get('created_at'),
            'updated_at': row.get('feedback_updated_at') or row.get('resolved_at'),
        } for row in self._feishu_store.list_recent_approvals()]

    def settings(self) -> dict[str, Any]:
        return self._cached_read('settings', codex_settings, 15.0)

    def write_model_defaults(self, values: Any) -> dict[str, Any]:
        result = write_codex_settings(values)
        with self._read_cache_lock:
            self._catalog_cache.pop('settings', None)
        return result

    def operational(self) -> dict[str, Any]:
        """Return one coherent, side-effect-free dashboard snapshot."""
        chat_state = self._feishu_store.recent_control_chat_state(CONTROL_CHAT_STATE_LIMIT)
        code_sessions = chat_state['sessions']
        chat_bindings = chat_state['chat_bindings']
        surface_states = chat_state['surface_states']
        binding_records = self._binding_store.list_bindings(limit=200)
        bound_chats = self._feishu_store.bound_chats_for_threads(binding.thread_id for binding in binding_records)
        pending_approvals = self._feishu_store.list_pending_approval_turns()
        feishu = self.feishu()
        return {
            'feishu': feishu,
            'sessions': self.sessions(code_sessions, chat_bindings, surface_states),
            'bindings': self.bindings(binding_records, bound_chats=bound_chats),
            'jobs': self.jobs(pending_approvals=pending_approvals),
            'surfaces': self.surfaces(),
            'activity': feishu['activity'],
            'storage': self.storage(),
        }

    def storage(self, binding_records=None) -> dict[str, Any]:
        """Return bounded filesystem-only storage telemetry, cached for 30s."""
        cache = self._storage_cache
        now = time.monotonic()
        if cache['value'] is not None and now - cache['checked_at'] < 30.0:
            return dict(cache['value'])

        def size(path):
            try:
                return Path(path).stat().st_size
            except OSError:
                return 0

        database = Path(self.database)
        database_bytes = size(database)
        wal_bytes = size(f'{database}-wal')
        shm_bytes = size(f'{database}-shm')
        rollout_total = 0
        rollout_largest = 0
        rollout_count = 0
        rollout_paths = (
            [binding.rollout_path for binding in binding_records if binding.rollout_path]
            if binding_records is not None
            else self._binding_store.list_rollout_paths()
        )
        for rollout_path in rollout_paths:
            bytes_used = size(rollout_path)
            rollout_total += bytes_used
            rollout_largest = max(rollout_largest, bytes_used)
            rollout_count += 1
        value = {
            'database_bytes': database_bytes,
            'wal_bytes': wal_bytes,
            'shm_bytes': shm_bytes,
            'cfr_storage_bytes': database_bytes + wal_bytes + shm_bytes,
            'codex_rollout_bytes': rollout_total,
            'codex_rollout_count': rollout_count,
            'largest_codex_rollout_bytes': rollout_largest,
        }
        cache['checked_at'] = now
        cache['value'] = value
        return dict(value)

    @staticmethod
    def _feishu_projection(supervisor, settings, state) -> dict[str, Any]:
        workspace_roots = [str(root) for root in settings.allowed_workspace_roots]
        invalid_workspace_roots = [str(root) for root in settings.allowed_workspace_roots if not root.exists() or not root.is_dir()]
        daemon = getattr(supervisor, '_daemon', None)
        transport = getattr(supervisor, '_transport', None)
        runtime = getattr(daemon, 'runtime_snapshot', lambda: None)() if daemon is not None else None
        transport_state = getattr(transport, 'connection_state', None) if transport is not None else None
        transport_event_error = getattr(transport, 'last_event_error', None) if transport is not None else None
        return {
            'state': state['feishu']['state'],
            'running': state['feishu']['running'],
            'credentials_configured': settings.credentials_present,
            'operator_authorized': bool(settings.allowed_open_ids),
            'operator_count': len(settings.allowed_open_ids),
            'workspace_configured': bool(settings.allowed_workspace_roots),
            'workspace_root_count': len(settings.allowed_workspace_roots),
            'workspace_roots': workspace_roots,
            'workspace_roots_valid': not invalid_workspace_roots,
            'invalid_workspace_roots': invalid_workspace_roots,
            'operator_policy_source': getattr(settings, 'operator_policy_source', 'missing'),
            'workspace_policy_source': getattr(settings, 'workspace_policy_source', 'missing'),
            'pairing': supervisor.pairing_state(),
            'activity': supervisor.activity() if hasattr(supervisor, 'activity') else [],
            'runtime': runtime,
            'transport_state': transport_state,
            'transport_last_event_error': transport_event_error,
            'app_id_source': settings.app_id_source,
            'app_secret_configured': bool(settings.app_secret),
            'app_secret_source': settings.app_secret_source,
            'last_error_code': state['feishu']['last_error_code'],
            'last_error_message': state['feishu']['last_error_message'],
        }

    def feishu(self) -> dict[str, Any]:
        settings = self._settings()
        return self._feishu_projection(self.supervisor, settings, self.supervisor.current_state())

    def build(self) -> dict[str, Any]:
        settings = self._settings()
        state = self.supervisor.current_state()
        codex_path = self._codex_path()
        feishu = self._feishu_projection(self.supervisor, settings, state)
        return {
            'overall_status': 'healthy' if state['feishu']['last_error_code'] is None and state['feishu']['state'] in {'stopped', 'running'} else 'degraded',
            'runtime': {
                'remote_execution_enabled': state['remote_execution_enabled'],
                'accept_new_tasks': state['accept_new_tasks'],
            },
            'codex': {
                'availability': 'available' if codex_path else 'unavailable',
                'executable': codex_path,
                'doctor_status': state['doctor']['status'],
            },
            'feishu': feishu,
            'doctor': state['doctor'],
        }
