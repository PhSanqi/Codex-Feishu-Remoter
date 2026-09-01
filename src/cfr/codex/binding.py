import asyncio
import logging
from pathlib import Path
import queue
import time
import uuid

from cfr.config import child_process_env, resolve_cfr_codex_home
from cfr.core.models import ConversationResult, StructuredError, ThreadRef, TurnResult, TurnTelemetry
from cfr.network import proxy_child_env, resolve_proxy

from .app_server import AppServerClient, AppServerRpcError
from .lease import LeaseState, WriterLeaseManager
from .runtime_lease import CfrThreadRuntimeLeaseManager
from .threads import ThreadManager
from .turns import ActiveTurnRegistry, TurnManager


UNSET = object()
LOGGER = logging.getLogger(__name__)


def _settings_timing(phase, started=None, elapsed_ms=None):
    elapsed = elapsed_ms if elapsed_ms is not None else int((time.monotonic() - started) * 1000)
    LOGGER.info('CFR_THREAD_SETTINGS_MUTATION_TIMING phase=%s elapsed_ms=%s', phase, elapsed)


class CodexAdapter:
    def __init__(self, store=None, launcher=None, timeout=30, turn_timeout=1800, process_env=None, config_overrides=None, codex_home=None):
        self.store = store
        self.launcher = launcher
        self.timeout = timeout
        self.turn_timeout = turn_timeout
        self.process_env = dict(process_env) if process_env is not None else dict(proxy_child_env(resolve_proxy()) or {})
        self.config_overrides = config_overrides
        self.resolved_cfr_codex_home = resolve_cfr_codex_home(codex_home)
        self.leases = WriterLeaseManager()
        self.runtime_leases = CfrThreadRuntimeLeaseManager(store.db_path) if store is not None and hasattr(store, 'db_path') else None
        self.registry = ActiveTurnRegistry()
        self.last_resume: dict[str, ThreadRef] = {}
        self.last_resume_payload: dict[str, dict] = {}
        self.last_client_lifecycle = None

    def _turn_telemetry(self, telemetry=None, *, thread_id=None):
        if telemetry is None:
            now = time.time()
            telemetry = self.registry.begin(
                f'local-{uuid.uuid4().hex}', thread_id=thread_id,
                received_at=now, queued_at=now, execution_started_at=now,
            )
        elif thread_id:
            telemetry.set_identity(thread_id=thread_id)
        self.registry.observe(telemetry)
        return telemetry

    @staticmethod
    def _observe_client_start(telemetry, client):
        now_mono = time.monotonic()
        now_wall = time.time()
        for name in (
            'app_server_start_started_at', 'app_server_started_at',
            'initialize_started_at', 'initialize_completed_at',
        ):
            value = getattr(client, name, None)
            if value is not None:
                telemetry.mark(name, at=value, wall_at=now_wall - (now_mono - value))

    def _client(self, on_server_request=None):
        options = dict(
            launcher=self.launcher,
            timeout=self.timeout,
            process_env=child_process_env(self.process_env, self.resolved_cfr_codex_home),
            config_overrides=self.config_overrides,
            codex_home=self.resolved_cfr_codex_home.path,
        )
        if on_server_request is not None:
            options['on_server_request'] = on_server_request
        return AppServerClient(**options)

    @staticmethod
    def _thread_from_read(result, fallback):
        return ThreadManager.ref_from_result(result, fallback.cwd, fallback.name)

    @staticmethod
    def _confirmed_settings(response):
        response = response or {}
        settings = response.get('threadSettings') if isinstance(response.get('threadSettings'), dict) else response
        fields = {}
        for local, native in (('model', 'model'), ('effort', 'effort'), ('service_tier', 'serviceTier')):
            if native in settings:
                fields[local] = settings[native]
        if 'effort' not in fields and 'reasoningEffort' in settings:
            fields['effort'] = settings['reasoningEffort']
        return fields

    def _observe_settings(self, thread_id, response):
        settings = self._confirmed_settings(response)
        if settings and self.store:
            self.store.update_observed_settings(thread_id, settings)
        return settings

    def _projected_settings(self, thread_id):
        if not self.store:
            raise StructuredError('BINDING_STORE_REQUIRED', 'thread settings require a BindingStore')
        binding = self.store.get_binding(thread_id)
        if not binding:
            raise StructuredError('BINDING_NOT_FOUND', f'No binding for {thread_id}')
        return {
            'thread_id': thread_id,
            'settings': {
                'model': binding.observed_model,
                'effort': binding.observed_reasoning_effort,
                'service_tier': binding.observed_service_tier,
                'collaboration_mode': None,
            },
            'observed_at': binding.observed_settings_at,
            'confirmed': binding.observed_settings_at is not None,
        }

    @staticmethod
    def _ensure_completed(turn: TurnResult, operation: str):
        if turn.status != 'completed':
            raise StructuredError(f'{operation.upper()}_TURN_{turn.status.upper()}', f'{operation} turn ended with {turn.status}', turn)

    async def create_conversation(self, cwd: Path, name: str, initial_message: str, *, on_server_request=None, on_progress=None, telemetry=None):
        telemetry = self._turn_telemetry(telemetry)
        def run():
            client = None
            try:
                telemetry.mark('task_execution_started_at')
                telemetry.mark('runtime_acquire_started_at')
                telemetry.mark('runtime_acquired_at')
                client = self._client(on_server_request=on_server_request)
                client.start()
                self._observe_client_start(telemetry, client)
                manager = ThreadManager(client)
                initial = manager.create_thread(Path(cwd), name)
                telemetry.set_identity(thread_id=initial.thread_id)
                telemetry.set_runtime_settings(manager.last_create_result)
                turn = TurnManager(client, self.registry).run_turn(initial.thread_id, initial_message, turn_timeout=self.turn_timeout, on_progress=on_progress, telemetry=telemetry)
                telemetry.mark('runtime_cleanup_started_at')
                self._ensure_completed(turn, 'initial')
                read = manager.read_thread(initial.thread_id)
                real_thread = self._thread_from_read(read, initial)
                if not real_thread.rollout_path or not real_thread.rollout_path.exists():
                    raise StructuredError('ROLLOUT_NOT_FOUND', f'No rollout file for {real_thread.thread_id}', real_thread)
                manager.name_thread(real_thread.thread_id, name)
                final_read = manager.read_thread(real_thread.thread_id)
                final = self._thread_from_read(final_read, real_thread)
                if self.store:
                    self.store.upsert_binding(final)
                    self._observe_settings(final.thread_id, manager.last_create_result)
                    self._observe_settings(final.thread_id, final_read)
                    telemetry.set_runtime_settings(final_read)
                    self.store.update_last_seen_turn(final.thread_id, turn.turn_id)
                return ConversationResult(final, turn)
            except Exception:
                if telemetry.status not in {'completed', 'stopped', 'timed_out', 'failed'}:
                    telemetry.set_stage('failed', status='failed', event='CFR runtime failed')
                raise
            finally:
                telemetry.mark('runtime_cleanup_started_at')
                if client is not None:
                    client.close()
                    self.last_client_lifecycle = client.lifecycle_snapshot()
                telemetry.mark('runtime_cleanup_completed_at')
                self.registry.observe(telemetry)

        return await asyncio.to_thread(run)

    async def send_message(self, thread_id: str, message: str, *, on_server_request=None, on_progress=None, telemetry=None):
        telemetry = self._turn_telemetry(telemetry, thread_id=thread_id)
        def run():
            telemetry.mark('task_execution_started_at')
            telemetry.mark('runtime_acquire_started_at')
            if not self.store:
                raise StructuredError('BINDING_STORE_REQUIRED', 'send_message requires a BindingStore')
            binding = self.store.get_binding(thread_id)
            if not binding:
                raise StructuredError('BINDING_NOT_FOUND', f'No binding for {thread_id}')
            durable_lease = None
            if self.runtime_leases:
                durable_lease = self.runtime_leases.acquire(thread_id)
                try:
                    self.runtime_leases.start_heartbeat(durable_lease)
                except Exception:
                    self.runtime_leases.release(durable_lease)
                    raise
            if self.leases.state_for(thread_id) is not LeaseState.EXTERNAL_ACTIVE:
                try:
                    self.leases.acquire(thread_id)
                except Exception:
                    if durable_lease and self.runtime_leases:
                        self.runtime_leases.release(durable_lease)
                    raise
            telemetry.mark('runtime_acquired_at')
            client = None
            try:
                client = self._client(on_server_request=on_server_request)
                client.start()
                self._observe_client_start(telemetry, client)
                manager = ThreadManager(client)
                try:
                    telemetry.mark('thread_resume_started_at')
                    resumed = manager.resume_thread(thread_id)
                    telemetry.mark('thread_resume_completed_at')
                except AppServerRpcError as exc:
                    if 'active writer' in exc.message.lower():
                        self.leases.mark_external(thread_id)
                        self.store.set_writer_state(thread_id, LeaseState.EXTERNAL_ACTIVE.value)
                        raise StructuredError('EXTERNAL_WRITER_ACTIVE', exc.message, {'method': exc.method, 'code': exc.code}) from exc
                    raise
                resumed_thread = ThreadManager.ref_from_result(resumed, binding.cwd, binding.thread_name)
                if resumed_thread.thread_id != thread_id:
                    raise StructuredError('THREAD_ID_MISMATCH', f'Resumed {resumed_thread.thread_id}, expected {thread_id}')
                if resumed_thread.rollout_path:
                    self.store.update_rollout_path(thread_id, resumed_thread.rollout_path)
                self.last_resume[thread_id] = resumed_thread
                self.last_resume_payload[thread_id] = resumed
                self._observe_settings(thread_id, resumed)
                telemetry.set_runtime_settings(resumed)
                self.leases.recover(thread_id)
                if durable_lease and durable_lease.lease_lost:
                    raise StructuredError('CFR_RUNTIME_LEASE_LOST', f'Lease lost for {thread_id}')
                turn = TurnManager(client, self.registry).run_turn(thread_id, message, turn_timeout=self.turn_timeout, on_progress=on_progress, telemetry=telemetry)
                telemetry.mark('runtime_cleanup_started_at')
                if durable_lease and durable_lease.lease_lost:
                    raise StructuredError('CFR_RUNTIME_LEASE_LOST', f'Lease lost for {thread_id}')
                self.store.update_last_seen_turn(thread_id, turn.turn_id)
                self.store.update_desktop_sync_state(thread_id, 'refresh_required')
                return turn
            except Exception:
                if telemetry.status not in {'completed', 'stopped', 'timed_out', 'failed'}:
                    telemetry.set_stage('failed', status='failed', event='CFR runtime failed')
                raise
            finally:
                telemetry.mark('runtime_cleanup_started_at')
                state = self.leases.state_for(thread_id)
                if client is not None:
                    client.close()
                    self.last_client_lifecycle = client.lifecycle_snapshot()
                self.store.clear_active_turn(thread_id, state.value if state is not LeaseState.CFR_ACTIVE else LeaseState.IDLE.value)
                if state is LeaseState.CFR_ACTIVE:
                    self.leases.release(thread_id)
                if durable_lease and self.runtime_leases:
                    self.runtime_leases.release(durable_lease)
                telemetry.mark('runtime_cleanup_completed_at')
                self.registry.observe(telemetry)

        return await asyncio.to_thread(run)

    async def stop(self, thread_id: str):
        def run():
            active = self.registry.get(thread_id)
            if not active:
                return {'status': 'CFR_DAEMON_REQUIRED_FOR_STOP', 'thread_id': thread_id}
            result = TurnManager(active.client, self.registry).interrupt_turn(thread_id, active.turn_id)
            terminal = self.registry.wait(thread_id, self.timeout)
            return {
                'status': 'STOP_REQUESTED',
                'thread_id': thread_id,
                'turn_id': active.turn_id,
                'interrupt_result': result,
                'terminal': terminal,
            }

        return await asyncio.to_thread(run)

    stop_turn = stop

    async def update_thread_settings(self, thread_id: str, *, model=UNSET, effort=UNSET, service_tier=UNSET, collaboration_mode=UNSET):
        """Persist only native Codex thread settings under existing CFR ownership."""
        def run():
            total_started = time.monotonic()
            if not self.store:
                raise StructuredError('BINDING_STORE_REQUIRED', 'thread settings require a BindingStore')
            binding = self.store.get_binding(thread_id)
            if not binding:
                raise StructuredError('BINDING_NOT_FOUND', f'No binding for {thread_id}')
            if self.leases.state_for(thread_id) is LeaseState.EXTERNAL_ACTIVE:
                raise StructuredError('EXTERNAL_WRITER_ACTIVE', f'Native thread {thread_id} has an external writer')
            durable_lease = None
            if self.runtime_leases:
                durable_lease = self.runtime_leases.acquire(thread_id)
                try:
                    self.runtime_leases.start_heartbeat(durable_lease)
                except Exception:
                    self.runtime_leases.release(durable_lease)
                    raise
            self.leases.acquire(thread_id)
            client = None
            try:
                client = self._client()
                client.start()
                _settings_timing('APP_SERVER_START', elapsed_ms=getattr(client, 'app_server_start_elapsed_ms', 0) or 0)
                _settings_timing('INITIALIZE', elapsed_ms=getattr(client, 'initialize_elapsed_ms', 0) or 0)
                manager = ThreadManager(client)
                try:
                    phase_started = time.monotonic()
                    resumed = manager.resume_thread(thread_id)
                    _settings_timing('THREAD_RESUME', phase_started)
                except AppServerRpcError as exc:
                    if 'active writer' in exc.message.lower():
                        self.leases.mark_external(thread_id)
                        self.store.set_writer_state(thread_id, LeaseState.EXTERNAL_ACTIVE.value)
                        raise StructuredError('EXTERNAL_WRITER_ACTIVE', exc.message, {'method': exc.method, 'code': exc.code}) from exc
                    raise
                resumed_thread = ThreadManager.ref_from_result(resumed, binding.cwd, binding.thread_name)
                if resumed_thread.thread_id != thread_id:
                    raise StructuredError('THREAD_ID_MISMATCH', f'Resumed {resumed_thread.thread_id}, expected {thread_id}')
                self._observe_settings(thread_id, resumed)
                self.leases.recover(thread_id)
                if durable_lease and durable_lease.lease_lost:
                    raise StructuredError('CFR_RUNTIME_LEASE_LOST', f'Lease lost for {thread_id}')
                payload = {'threadId': thread_id}
                for field, value in (('model', model), ('effort', effort), ('serviceTier', service_tier), ('collaborationMode', collaboration_mode)):
                    if value is not UNSET:
                        payload[field] = value
                subscription = client.subscribe(
                    lambda message: message.get('method') == 'thread/settings/updated'
                    and message.get('params', {}).get('threadId') == thread_id
                ) if len(payload) > 1 and hasattr(client, 'subscribe') else None
                phase_started = time.monotonic()
                result = client.request('thread/settings/update', payload) if len(payload) > 1 else None
                _settings_timing('SETTINGS_UPDATE_RPC', phase_started)
                phase_started = time.monotonic()
                confirmation = resumed if result is None else None
                if subscription is not None:
                    try:
                        confirmation = subscription.get(timeout=0.5).get('params')
                    except queue.Empty:
                        pass
                    finally:
                        subscription.close()
                if result is not None and confirmation is None:
                    confirmation = manager.resume_thread(thread_id)
                    confirmed_thread = ThreadManager.ref_from_result(confirmation, binding.cwd, binding.thread_name)
                    if confirmed_thread.thread_id != thread_id:
                        raise StructuredError('THREAD_ID_MISMATCH', f'Resumed {confirmed_thread.thread_id}, expected {thread_id}')
                if not self._observe_settings(thread_id, confirmation):
                    raise StructuredError('THREAD_SETTINGS_CONFIRMATION_UNAVAILABLE', f'Native settings confirmation unavailable for {thread_id}')
                _settings_timing('SETTINGS_CONFIRMATION', phase_started)
                if durable_lease and durable_lease.lease_lost:
                    raise StructuredError('CFR_RUNTIME_LEASE_LOST', f'Lease lost for {thread_id}')
                self.last_resume[thread_id] = resumed_thread
                self.last_resume_payload[thread_id] = confirmation
                if result is not None:
                    self.store.update_desktop_sync_state(thread_id, 'refresh_required')
                projection = self._projected_settings(thread_id)
                projection['result'] = result
                return projection
            finally:
                state = self.leases.state_for(thread_id)
                try:
                    if client is not None:
                        phase_started = time.monotonic()
                        try:
                            client.close()
                        finally:
                            _settings_timing('APP_SERVER_CLOSE', phase_started)
                            self.last_client_lifecycle = client.lifecycle_snapshot()
                finally:
                    self.store.clear_active_turn(thread_id, state.value if state is not LeaseState.CFR_ACTIVE else LeaseState.IDLE.value)
                    if state is LeaseState.CFR_ACTIVE:
                        self.leases.release(thread_id)
                    if durable_lease and self.runtime_leases:
                        self.runtime_leases.release(durable_lease)
                    _settings_timing('TOTAL', total_started)

        return await asyncio.to_thread(run)

    async def read_thread_settings(self, thread_id: str):
        return self._projected_settings(thread_id)

    async def steer(self, thread_id: str, text: str):
        def run():
            active = self.registry.get(thread_id)
            if active is None:
                return None
            return TurnManager(active.client, self.registry).steer_turn(thread_id, active.turn_id, text)
        return await asyncio.to_thread(run)
