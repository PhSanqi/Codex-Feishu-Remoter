import asyncio
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from cfr.codex.app_server import AppServerRpcError, NotificationDispatcher
from cfr.codex.binding import CodexAdapter
from cfr.codex.lease import LeaseState, WriterLeaseManager
from cfr.codex.rollout import RolloutWatcher, canonical_path_key
from cfr.codex.turns import TurnManager
from cfr.core.events import CfrEvent, EventSource
from cfr.core.projector import EventProjector
from cfr.core.models import StructuredError
from cfr.storage.db import BindingStore


class FakeClient:
    rollout_path = None

    def __init__(self, launcher=None, timeout=30, process_env=None, config_overrides=None, codex_home=None):
        self.dispatcher = NotificationDispatcher()

    def start(self):
        return self

    def close(self):
        pass

    def lifecycle_snapshot(self):
        return {'started': True, 'close_started': True, 'exited': True, 'exit_code': 0}

    def request(self, method, params, timeout=None):
        if method == 'thread/start':
            return {'thread': {'id': 'thread-1', 'cwd': params['cwd'], 'path': str(self.rollout_path)}}
        if method in ('thread/read', 'thread/resume'):
            return {'thread': {'id': 'thread-1', 'cwd': str(self.rollout_path.parent), 'path': str(self.rollout_path), 'name': 'name'}}
        if method == 'turn/start':
            self.dispatcher.publish({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1'}}})
            self.dispatcher.publish({'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item-1', 'delta': 'ACK'}})
            self.dispatcher.publish({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1', 'status': 'completed', 'items': [{'id': 'item-1', 'type': 'agentMessage', 'text': 'ACK'}]}}})
            return {'turn': {'id': 'turn-1', 'status': 'inProgress'}}
        if method == 'turn/interrupt':
            return {}
        return {}

    def subscribe(self, predicate=None):
        return self.dispatcher.subscribe(predicate)


class ActiveWriterClient(FakeClient):
    def request(self, method, params, timeout=None):
        if method == 'thread/resume':
            raise AppServerRpcError(method, -32600, 'thread already has an active writer')
        return super().request(method, params, timeout)


class BlockingClient(FakeClient):
    def request(self, method, params, timeout=None):
        if method in ('thread/resume',):
            return {'thread': {'id': 'thread-1', 'cwd': str(self.rollout_path.parent), 'path': str(self.rollout_path), 'name': 'name'}}
        if method == 'turn/start':
            return {'turn': {'id': 'turn-1', 'status': 'inProgress'}}
        if method == 'turn/interrupt':
            self.dispatcher.publish({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1', 'status': 'interrupted'}}})
            return {'ok': True}
        return super().request(method, params, timeout)


class ExplodingClient(FakeClient):
    def request(self, method, params, timeout=None):
        if method == 'turn/start':
            return {'turn': {'id': 'turn-1', 'status': 'inProgress'}}
        return super().request(method, params, timeout)

    def subscribe(self, predicate=None):
        class ExplodingSubscription:
            def get(self, timeout=None):
                raise RuntimeError('event stream failed')

            def close(self):
                pass

        return ExplodingSubscription()


class M1CompletionTests(unittest.TestCase):
    def test_binding_migration_and_cursor_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            import sqlite3
            conn = sqlite3.connect(database)
            try:
                conn.executescript('''create table codex_bindings(
                    id integer primary key, thread_id text unique, thread_name text, cwd text,
                    rollout_path text, last_rollout_offset integer default 0,
                    last_seen_turn_id text, desktop_sync_state text, created_at real, updated_at real
                ); insert into codex_bindings(thread_id,cwd,last_rollout_offset,last_seen_turn_id)
                values('thread-1','C:\\workspace',42,'turn-old');''')
            finally:
                conn.close()
            store = BindingStore(database)
            binding = store.get_binding('thread-1')
            self.assertEqual(binding.last_rollout_byte_offset, 42)
            self.assertEqual(binding.last_seen_turn_id, 'turn-old')
            store.close()

    def test_upsert_preserves_cursor_and_returns_model(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            ref = type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': Path(directory) / 'x.jsonl'})()
            store.upsert_binding(ref)
            store.update_rollout_offset('thread-1', 42)
            store.update_last_seen_turn('thread-1', 'turn-old')
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'new-name', 'cwd': Path(directory), 'rollout_path': None})())
            binding = store.get_binding('thread-1')
            self.assertEqual(binding.thread_name, 'new-name')
            self.assertEqual(binding.last_rollout_byte_offset, 42)
            self.assertEqual(binding.last_seen_turn_id, 'turn-old')
            store.close()

    def test_native_confirmed_settings_projection_persists_across_store_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            store = BindingStore(database)
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': None})())
            store.update_observed_settings('thread-1', {'model': 'model-one', 'effort': 'high', 'service_tier': 'priority'}, observed_at=123.0)
            store.close()
            reopened = BindingStore(database)
            binding = reopened.get_binding('thread-1')
            self.assertEqual((binding.observed_model, binding.observed_reasoning_effort, binding.observed_service_tier, binding.observed_settings_at), ('model-one', 'high', 'priority', 123.0))
            reopened.close()

    def test_create_lifecycle_runs_turn_before_name_and_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            FakeClient.rollout_path = rollout
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            with patch('cfr.codex.binding.AppServerClient', FakeClient):
                result = asyncio.run(CodexAdapter(store=store).create_conversation(Path(directory), 'name', 'hello'))
            self.assertEqual(result.thread_id, 'thread-1')
            self.assertEqual(result.initial_turn.final_agent_message, 'ACK')
            self.assertIsNotNone(store.get_binding('thread-1'))
            store.close()

    def test_create_projects_native_confirmed_start_settings(self):
        class SettingsStartClient(FakeClient):
            def request(self, method, params, timeout=None):
                response = super().request(method, params, timeout)
                if method == 'thread/start':
                    response.update({'model': 'model-one', 'reasoningEffort': 'high', 'serviceTier': 'priority'})
                return response

        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            SettingsStartClient.rollout_path = rollout
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            with patch('cfr.codex.binding.AppServerClient', SettingsStartClient):
                result = asyncio.run(CodexAdapter(store=store).create_conversation(Path(directory), 'name', 'hello'))
            binding = store.get_binding(result.thread_id)
            self.assertEqual((binding.observed_model, binding.observed_reasoning_effort, binding.observed_service_tier), ('model-one', 'high', 'priority'))
            store.close()

    def test_send_uses_binding_and_updates_refresh_state(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            FakeClient.rollout_path = rollout
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            ref = type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': rollout})()
            store.upsert_binding(ref)
            with patch('cfr.codex.binding.AppServerClient', FakeClient):
                result = asyncio.run(CodexAdapter(store=store).send_message('thread-1', 'hello'))
            binding = store.get_binding('thread-1')
            self.assertEqual(result.status, 'completed')
            self.assertEqual(binding.desktop_sync_state, 'refresh_required')
            self.assertEqual(binding.writer_state, 'idle')
            store.close()

    def test_active_writer_maps_to_structured_error_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            FakeClient.rollout_path = rollout
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            ref = type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': rollout})()
            store.upsert_binding(ref)
            adapter = CodexAdapter(store=store)
            with patch('cfr.codex.binding.AppServerClient', ActiveWriterClient):
                with self.assertRaises(StructuredError) as caught:
                    asyncio.run(adapter.send_message('thread-1', 'hello'))
            self.assertEqual(caught.exception.code, 'EXTERNAL_WRITER_ACTIVE')
            self.assertEqual(store.get_binding('thread-1').writer_state, 'external_active')
            with patch('cfr.codex.binding.AppServerClient', FakeClient):
                result = asyncio.run(adapter.send_message('thread-1', 'hello'))
            self.assertEqual(result.status, 'completed')
            self.assertEqual(store.get_binding('thread-1').writer_state, 'idle')
            store.close()

    def test_native_thread_settings_resume_update_and_release_without_turn(self):
        events = []

        class SettingsClient(FakeClient):
            requests = []
            closed = False
            state = {'model': 'model-one', 'reasoningEffort': 'medium', 'serviceTier': None}
            def request(self, method, params, timeout=None):
                self.requests.append((method, params))
                if method == 'thread/resume':
                    return {
                        'thread': {'id': 'thread-1', 'cwd': str(self.rollout_path.parent), 'path': str(self.rollout_path), 'name': 'name'},
                        **type(self).state,
                    }
                if method == 'thread/settings/update':
                    type(self).state.update({
                        'model': params.get('model', type(self).state['model']),
                        'reasoningEffort': params.get('effort', type(self).state['reasoningEffort']),
                        'serviceTier': params.get('serviceTier', type(self).state['serviceTier']),
                    })
                    return {}
                return super().request(method, params, timeout)
            def close(self):
                events.append('client_close')
                type(self).closed = True

        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            FakeClient.rollout_path = rollout
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': rollout})())
            SettingsClient.requests = []
            SettingsClient.closed = False
            SettingsClient.state = {'model': 'model-one', 'reasoningEffort': 'medium', 'serviceTier': None}
            adapter = CodexAdapter(store=store)
            release = adapter.leases.release
            with patch('cfr.codex.binding.AppServerClient', SettingsClient), patch.object(adapter.leases, 'release', side_effect=lambda thread_id: events.append('writer_release') or release(thread_id)), self.assertLogs('cfr.codex.binding', 'INFO') as logs:
                asyncio.run(adapter.update_thread_settings('thread-1', model='runtime-model', effort='high'))
            update = next(params for method, params in SettingsClient.requests if method == 'thread/settings/update')
            self.assertEqual(update, {'threadId': 'thread-1', 'model': 'runtime-model', 'effort': 'high'})
            self.assertNotIn('turn/start', [method for method, _ in SettingsClient.requests])
            self.assertTrue(SettingsClient.closed)
            self.assertLess(events.index('client_close'), events.index('writer_release'))
            for phase in ('APP_SERVER_START', 'INITIALIZE', 'THREAD_RESUME', 'SETTINGS_UPDATE_RPC', 'SETTINGS_CONFIRMATION', 'APP_SERVER_CLOSE', 'TOTAL'):
                self.assertTrue(any(f'phase={phase}' in line for line in logs.output))
            self.assertEqual(store.get_binding('thread-1').writer_state, 'idle')
            store.close()

    def test_normal_resume_populates_fast_thread_settings_projection(self):
        class ResumeSettingsClient(FakeClient):
            def request(self, method, params, timeout=None):
                if method == 'thread/resume':
                    return {
                        'thread': {'id': 'thread-1', 'cwd': str(self.rollout_path.parent), 'path': str(self.rollout_path), 'name': 'name'},
                        'model': 'gpt-5.6-terra', 'reasoningEffort': 'high', 'serviceTier': 'priority',
                    }
                return super().request(method, params, timeout)

        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            FakeClient.rollout_path = rollout
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': rollout})())
            adapter = CodexAdapter(store=store)
            with patch('cfr.codex.binding.AppServerClient', ResumeSettingsClient):
                asyncio.run(adapter.send_message('thread-1', 'hello'))
            with patch('cfr.codex.binding.AppServerClient', side_effect=AssertionError('projection read spawned app-server')):
                settings = asyncio.run(adapter.read_thread_settings('thread-1'))['settings']
            self.assertEqual(settings['model'], 'gpt-5.6-terra')
            self.assertEqual(settings['effort'], 'high')
            self.assertEqual(settings['service_tier'], 'priority')
            store.close()

    def test_settings_close_failure_still_releases_writer_and_runtime_lease(self):
        class CloseFailureSettingsClient(FakeClient):
            def request(self, method, params, timeout=None):
                if method == 'thread/resume':
                    return {
                        'thread': {'id': 'thread-1', 'cwd': str(self.rollout_path.parent), 'path': str(self.rollout_path), 'name': 'name'},
                        'model': 'model-one', 'reasoningEffort': 'medium', 'serviceTier': None,
                    }
                if method == 'thread/settings/update':
                    self.dispatcher.publish({'method': 'thread/settings/updated', 'params': {
                        'threadId': 'thread-1', 'threadSettings': {'model': 'model-two', 'effort': 'medium', 'serviceTier': None},
                    }})
                    return {}
                return super().request(method, params, timeout)

            def close(self):
                raise RuntimeError('close failed')

        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            CloseFailureSettingsClient.rollout_path = rollout
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': rollout})())
            adapter = CodexAdapter(store=store)
            with patch('cfr.codex.binding.AppServerClient', CloseFailureSettingsClient), self.assertRaisesRegex(RuntimeError, 'close failed'):
                asyncio.run(adapter.update_thread_settings('thread-1', model='model-two'))
            self.assertEqual(adapter.leases.state_for('thread-1'), LeaseState.IDLE)
            self.assertEqual(adapter.runtime_leases.inspect(), [])
            self.assertEqual(store.get_binding('thread-1').writer_state, 'idle')
            store.close()

    def test_settings_mutation_is_observed_by_fresh_resume(self):
        class StatefulSettingsClient(FakeClient):
            state = {'model': 'model-one', 'reasoningEffort': 'low', 'serviceTier': None}
            requests = []

            def request(self, method, params, timeout=None):
                type(self).requests.append((method, params))
                if method == 'thread/resume':
                    return {
                        'thread': {'id': 'thread-1', 'cwd': str(self.rollout_path.parent), 'path': str(self.rollout_path), 'name': 'name'},
                        **type(self).state,
                    }
                if method == 'thread/settings/update':
                    type(self).state.update({
                        'model': params.get('model', type(self).state['model']),
                        'reasoningEffort': params.get('effort', type(self).state['reasoningEffort']),
                        'serviceTier': params.get('serviceTier', type(self).state['serviceTier']),
                    })
                    return {}
                return super().request(method, params, timeout)

        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            FakeClient.rollout_path = rollout
            StatefulSettingsClient.state = {'model': 'model-one', 'reasoningEffort': 'low', 'serviceTier': None}
            StatefulSettingsClient.requests = []
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': rollout})())
            adapter = CodexAdapter(store=store)
            with patch('cfr.codex.binding.AppServerClient', StatefulSettingsClient):
                asyncio.run(adapter.update_thread_settings('thread-1', model='model-two'))
                self.assertEqual(asyncio.run(adapter.read_thread_settings('thread-1'))['settings']['model'], 'model-two')
                asyncio.run(adapter.update_thread_settings('thread-1', effort='medium'))
                self.assertEqual(asyncio.run(adapter.read_thread_settings('thread-1'))['settings']['effort'], 'medium')
            self.assertNotIn('turn/start', [method for method, _ in StatefulSettingsClient.requests])
            store.close()

    def test_native_settings_updated_notification_refreshes_projection(self):
        class NotificationSettingsClient(FakeClient):
            def request(self, method, params, timeout=None):
                if method == 'thread/resume':
                    return {
                        'thread': {'id': 'thread-1', 'cwd': str(self.rollout_path.parent), 'path': str(self.rollout_path), 'name': 'name'},
                        'model': 'model-one', 'reasoningEffort': 'medium', 'serviceTier': None,
                    }
                if method == 'thread/settings/update':
                    self.dispatcher.publish({'method': 'thread/settings/updated', 'params': {
                        'threadId': 'thread-1',
                        'threadSettings': {'model': 'model-two', 'effort': 'high', 'serviceTier': 'priority'},
                    }})
                    return {}
                return super().request(method, params, timeout)

        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            FakeClient.rollout_path = rollout
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': rollout})())
            adapter = CodexAdapter(store=store)
            with patch('cfr.codex.binding.AppServerClient', NotificationSettingsClient):
                asyncio.run(adapter.update_thread_settings('thread-1', model='model-two', effort='high'))
            projected = asyncio.run(adapter.read_thread_settings('thread-1'))['settings']
            self.assertEqual((projected['model'], projected['effort'], projected['service_tier']), ('model-two', 'high', 'priority'))
            store.close()

    def test_delayed_native_settings_notification_refreshes_projection_without_second_owner(self):
        class DelayedSettingsClient(FakeClient):
            constructions = 0
            resumes = 0

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                type(self).constructions += 1

            def request(self, method, params, timeout=None):
                if method == 'thread/resume':
                    type(self).resumes += 1
                    return {
                        'thread': {'id': 'thread-1', 'cwd': str(self.rollout_path.parent), 'path': str(self.rollout_path), 'name': 'name'},
                        'model': 'model-one', 'reasoningEffort': 'medium', 'serviceTier': None,
                    }
                if method == 'thread/settings/update':
                    threading.Timer(0.2, self.dispatcher.publish, args=({'method': 'thread/settings/updated', 'params': {
                        'threadId': 'thread-1',
                        'threadSettings': {'model': 'model-two', 'effort': 'high', 'serviceTier': 'priority'},
                    }},)).start()
                    return {}
                return super().request(method, params, timeout)

        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            DelayedSettingsClient.rollout_path = rollout
            DelayedSettingsClient.constructions = 0
            DelayedSettingsClient.resumes = 0
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': rollout})())
            adapter = CodexAdapter(store=store)
            with patch('cfr.codex.binding.AppServerClient', DelayedSettingsClient), patch.object(adapter.runtime_leases, 'acquire', wraps=adapter.runtime_leases.acquire) as runtime_acquire, patch.object(adapter.leases, 'acquire', wraps=adapter.leases.acquire) as writer_acquire:
                projected = asyncio.run(adapter.update_thread_settings('thread-1', model='model-two', effort='high', service_tier='priority'))['settings']
            self.assertEqual((projected['model'], projected['effort'], projected['service_tier']), ('model-two', 'high', 'priority'))
            self.assertEqual((DelayedSettingsClient.constructions, DelayedSettingsClient.resumes), (1, 1))
            self.assertEqual((runtime_acquire.call_count, writer_acquire.call_count), (1, 1))
            store.close()

    def test_same_client_fallback_uses_native_effective_value_and_confirms_noop(self):
        class FallbackSettingsClient(FakeClient):
            state = {'model': 'model-one', 'reasoningEffort': 'medium', 'serviceTier': 'priority'}
            constructions = 0
            resumes = 0

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                type(self).constructions += 1

            def request(self, method, params, timeout=None):
                if method == 'thread/resume':
                    type(self).resumes += 1
                    return {
                        'thread': {'id': 'thread-1', 'cwd': str(self.rollout_path.parent), 'path': str(self.rollout_path), 'name': 'name'},
                        **type(self).state,
                    }
                if method == 'thread/settings/update':
                    if 'serviceTier' in params:
                        type(self).state['serviceTier'] = 'default' if params['serviceTier'] is None else params['serviceTier']
                    if 'effort' in params:
                        type(self).state['reasoningEffort'] = params['effort']
                    return {}
                return super().request(method, params, timeout)

        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            FallbackSettingsClient.rollout_path = rollout
            FallbackSettingsClient.state = {'model': 'model-one', 'reasoningEffort': 'medium', 'serviceTier': 'priority'}
            FallbackSettingsClient.constructions = 0
            FallbackSettingsClient.resumes = 0
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': rollout})())
            adapter = CodexAdapter(store=store)
            with patch('cfr.codex.binding.AppServerClient', FallbackSettingsClient):
                normalized = asyncio.run(adapter.update_thread_settings('thread-1', service_tier=None))['settings']
                noop = asyncio.run(adapter.update_thread_settings('thread-1', effort='medium'))['settings']
            self.assertEqual(normalized['service_tier'], 'default')
            self.assertEqual(noop['effort'], 'medium')
            self.assertEqual((FallbackSettingsClient.constructions, FallbackSettingsClient.resumes), (2, 4))
            store.close()

    def test_native_thread_settings_fail_closed_for_external_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            FakeClient.rollout_path = rollout
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': rollout})())
            with patch('cfr.codex.binding.AppServerClient', ActiveWriterClient):
                with self.assertRaises(StructuredError) as caught:
                    asyncio.run(CodexAdapter(store=store).update_thread_settings('thread-1', model='runtime-model'))
            self.assertEqual(caught.exception.code, 'EXTERNAL_WRITER_ACTIVE')
            self.assertEqual(store.get_binding('thread-1').writer_state, 'external_active')
            store.close()

    def test_dispatcher_fanout_and_per_thread_lease(self):
        dispatcher = NotificationDispatcher()
        a = dispatcher.subscribe(lambda message: message.get('params', {}).get('threadId') == 'a')
        b = dispatcher.subscribe(lambda message: message.get('params', {}).get('threadId') == 'b')
        dispatcher.publish({'method': 'turn/completed', 'params': {'threadId': 'a'}})
        self.assertEqual(a.get(timeout=0.1)['params']['threadId'], 'a')
        self.assertTrue(b.queue.empty())
        leases = WriterLeaseManager()
        leases.acquire('a')
        leases.acquire('b')
        self.assertEqual(leases.state_for('a'), LeaseState.CFR_ACTIVE)
        self.assertEqual(leases.state_for('b'), LeaseState.CFR_ACTIVE)

    def test_rollout_explicit_thread_and_partial_utf8_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollout.jsonl'
            complete = json.dumps({'payload': {'type': 'agent_message', 'turn_id': 'turn-1', 'id': 'item-1', 'message': '中文'}}).encode('utf-8') + b'\n'
            path.write_bytes(complete + b'{"payload":')
            watcher = RolloutWatcher(thread_id='thread-1', path=path, byte_offset=0)
            events = watcher.poll()
            self.assertEqual(events[0].thread_id, 'thread-1')
            self.assertEqual(events[0].source, EventSource.UNKNOWN)
            self.assertEqual(watcher.byte_offset, len(complete))
            self.assertEqual(watcher.poll(), [])

    def test_rollout_cursor_persists_across_store_and_watcher_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'cfr.sqlite3'
            rollout = root / 'rollout.jsonl'
            def append_event(event_id, turn_id):
                with rollout.open('ab') as handle:
                    handle.write((json.dumps({'payload': {
                        'type': 'agent_message',
                        'id': event_id,
                        'turn_id': turn_id,
                        'message': event_id,
                    }}) + '\n').encode('utf-8'))

            append_event('item-a', 'turn-a')
            store = BindingStore(database)
            ref = type('Ref', (), {
                'thread_id': 'thread-1',
                'name': 'name',
                'cwd': root,
                'rollout_path': rollout,
            })()
            store.upsert_binding(ref)
            watcher = RolloutWatcher(thread_id='thread-1', path=rollout, byte_offset=0)
            self.assertEqual(len(watcher.poll()), 1)
            store.update_rollout_offset('thread-1', watcher.byte_offset)
            first_offset = watcher.byte_offset
            store.close()

            store = BindingStore(database)
            recovered = store.get_binding('thread-1')
            self.assertEqual(recovered.last_rollout_byte_offset, first_offset)
            restarted = RolloutWatcher(thread_id='thread-1', path=recovered.rollout_path, byte_offset=recovered.last_rollout_byte_offset)
            self.assertEqual(restarted.poll(), [])
            append_event('item-b', 'turn-b')
            self.assertEqual(len(restarted.poll()), 1)
            store.update_rollout_offset('thread-1', restarted.byte_offset)
            final_offset = restarted.byte_offset
            store.close()

            store = BindingStore(database)
            final_binding = store.get_binding('thread-1')
            self.assertEqual(final_binding.last_rollout_byte_offset, final_offset)
            final_watcher = RolloutWatcher(thread_id='thread-1', path=final_binding.rollout_path, byte_offset=final_binding.last_rollout_byte_offset)
            self.assertEqual(final_watcher.poll(), [])
            store.close()

    def test_rollout_path_key_normalizes_extended_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollout.jsonl'
            extended = '\\\\?\\' + str(path)
            self.assertEqual(canonical_path_key(path), canonical_path_key(extended))

    def test_rollout_path_key_is_host_independent_for_posix_fixture(self):
        self.assertEqual(canonical_path_key('/tmp/example'), canonical_path_key(r'\\?\/tmp/example'))

    def test_rollout_path_key_normalizes_windows_drive_case(self):
        self.assertEqual(canonical_path_key(r'C:\Users\Test'), canonical_path_key(r'c:\users\test'))
        self.assertEqual(canonical_path_key(r'C:\A\B'), canonical_path_key(r'\\?\C:\A\B'))

    def test_rollout_path_key_normalizes_windows_unc_and_extended_unc(self):
        self.assertEqual(canonical_path_key(r'\\server\share\x'), canonical_path_key(r'\\?\UNC\server\share\x'))

    def test_projector_suppresses_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            event = CfrEvent('thread', 'turn', 'item', 'agent_message', EventSource.UNKNOWN, 'x', None, None)
            projector = EventProjector(store)
            self.assertIsNotNone(projector.project_event(event))
            self.assertIsNone(projector.project_event(event))
            store.close()

    def test_stop_without_runtime_exposes_daemon_boundary(self):
        result = asyncio.run(CodexAdapter().stop('missing'))
        self.assertEqual(result['status'], 'CFR_DAEMON_REQUIRED_FOR_STOP')

    def test_active_registry_does_not_hold_client_after_terminal_exception(self):
        client = ExplodingClient()
        client.start()
        manager = TurnManager(client)
        with self.assertRaises(RuntimeError):
            manager.run_turn('thread-1', 'hello', timeout=0.2)
        self.assertIsNone(manager.registry.get('thread-1'))

    def test_same_process_stop_interrupts_and_finishes_turn(self):
        async def scenario(adapter):
            task = asyncio.create_task(adapter.send_message('thread-1', 'long'))
            for _ in range(100):
                if adapter.registry.get('thread-1'):
                    break
                await asyncio.sleep(0.01)
            stop = await adapter.stop('thread-1')
            result = await task
            return stop, result

        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            rollout.write_text('', encoding='utf-8')
            FakeClient.rollout_path = rollout
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            ref = type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': rollout})()
            store.upsert_binding(ref)
            with patch('cfr.codex.binding.AppServerClient', BlockingClient):
                stop, result = asyncio.run(scenario(CodexAdapter(store=store, timeout=2)))
            self.assertEqual(stop['status'], 'STOP_REQUESTED')
            self.assertEqual(result.status, 'interrupted')
            store.close()


if __name__ == '__main__':
    unittest.main()
