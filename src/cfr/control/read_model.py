from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any

from .codex_catalog import capabilities as codex_capabilities
from .codex_catalog import models as codex_models
from .codex_settings import read as codex_settings
from .codex_settings import write as write_codex_settings
from cfr.feishu.config import load_settings
from cfr.feishu.store import FeishuStore
from cfr.storage.db import BindingStore


@dataclass
class ControlReadModel:
    supervisor: Any
    database: Path

    def sessions(self) -> list[dict[str, Any]]:
        """Expose the durable session view without management-only identity data."""
        return [
            {
                'chat_id': session.chat_id,
                'chat_type': session.chat_type,
                'state': session.state,
                'thread_id': session.thread_id or None,
                'pending_cwd': session.pending_cwd or None,
                'updated_at': session.updated_at,
            }
            for session in FeishuStore(self.database).list_sessions()
        ]

    def bindings(self) -> list[dict[str, Any]]:
        """Expose durable CFR/Codex thread bindings without rollout contents."""
        return [
            {
                'thread_id': binding.thread_id,
                'thread_name': binding.thread_name,
                'cwd': str(binding.cwd),
                'last_seen_turn_id': binding.last_seen_turn_id,
                'desktop_sync_state': binding.desktop_sync_state,
                'writer_state': binding.writer_state,
                'active_turn_id': binding.active_turn_id,
                'created_at': binding.created_at,
                'updated_at': binding.updated_at,
            }
            for binding in BindingStore(self.database).list_bindings()
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

    def jobs(self) -> list[dict[str, Any]]:
        """Read-only projection of CFR's active and current-process terminal turns."""
        daemon = getattr(self.supervisor, '_daemon', None)
        adapter = getattr(daemon, 'adapter', None)
        registry = getattr(adapter, 'registry', None)
        if registry is not None and hasattr(registry, 'runtime_snapshot'):
            snapshot = registry.runtime_snapshot()
        else:
            snapshot = {'active': (), 'results': ()}
        bindings = {binding.thread_id: binding for binding in BindingStore(self.database).list_bindings()}
        approvals = FeishuStore(self.database)

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
            pending = bool(
                thread_id and turn_id
                and any(item.get('state') == 'pending' for item in approvals.list_approvals_for_turn(thread_id, turn_id))
            )
            timestamps = runtime.get('timestamps') or {}
            terminal = runtime.get('status') in {'completed', 'failed', 'stopped', 'timed_out'}
            telemetry_jobs.append({
                'id': turn_id or observed['job_id'],
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
            pending = any(item.get('state') == 'pending' for item in approvals.list_approvals_for_turn(turn.thread_id, turn.turn_id))
            active_jobs.append({
                'id': turn.turn_id,
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
        return telemetry_jobs + active_jobs + terminal_jobs

    def models(self) -> dict[str, Any]:
        return codex_models()

    def capabilities(self) -> dict[str, Any]:
        return codex_capabilities()

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
        } for row in FeishuStore(self.database).list_recent_approvals()]

    def settings(self) -> dict[str, Any]:
        return codex_settings()

    def write_model_defaults(self, values: Any) -> dict[str, Any]:
        return write_codex_settings(values)

    def build(self) -> dict[str, Any]:
        settings = self.supervisor._load_settings() if hasattr(self.supervisor, '_load_settings') else load_settings(database=self.database)
        state = self.supervisor.current_state()
        codex_path = shutil.which('codex')
        workspace_roots = [str(root) for root in settings.allowed_workspace_roots]
        invalid_workspace_roots = [str(root) for root in settings.allowed_workspace_roots if not root.exists() or not root.is_dir()]
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
            'feishu': {
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
                'pairing': self.supervisor.pairing_state(),
                'activity': self.supervisor.activity() if hasattr(self.supervisor, 'activity') else [],
                'app_id_source': settings.app_id_source,
                'app_secret_configured': bool(settings.app_secret),
                'app_secret_source': settings.app_secret_source,
                'last_error_code': state['feishu']['last_error_code'],
                'last_error_message': state['feishu']['last_error_message'],
            },
            'doctor': state['doctor'],
        }
