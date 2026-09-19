import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import Mock, patch
import unittest

from cfr.control.read_model import ControlReadModel
from cfr.core.models import ThreadRef
from cfr.feishu.store import FeishuStore
from cfr.feishu.credentials import LocalConfigStore
from cfr.storage.db import BindingStore


class ControlReadModelTests(unittest.TestCase):
    def test_surfaces_uses_cached_chat_snapshot_without_invasive_health_call(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            snapshot = Mock(return_value={'available': True, 'status': 'ready', 'description': 'cached'})
            supervisor = SimpleNamespace(
                chat_status_snapshot=snapshot,
                _config_store=SimpleNamespace(get_default_surface=lambda: 'code'),
            )
            surfaces = ControlReadModel(supervisor, database).surfaces()
        snapshot.assert_called_once_with()
        self.assertIsNone(surfaces['chat_id'])
        self.assertTrue(next(item for item in surfaces['data'] if item['id'] == 'chat')['available'])

    def test_settings_cache_reloads_only_after_config_revision_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = LocalConfigStore(directory)
            config.set_default_surface('code')
            first, second = object(), object()
            loader = Mock(side_effect=[first, second])
            supervisor = SimpleNamespace(_config_store=config, _load_settings=loader)
            model = ControlReadModel(supervisor, Path(directory) / 'cfr.sqlite3')
            self.assertIs(model._settings(), first)
            self.assertIs(model._settings(), first)
            config.set_network_policy('direct')
            self.assertIs(model._settings(), second)
        self.assertEqual(loader.call_count, 2)

    def test_tracked_settings_cache_does_not_reload_only_because_time_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = LocalConfigStore(directory)
            config.set_default_surface('code')
            value = object()
            loader = Mock(return_value=value)
            supervisor = SimpleNamespace(_config_store=config, _load_settings=loader)
            model = ControlReadModel(supervisor, Path(directory) / 'cfr.sqlite3')
            with patch('cfr.control.read_model.time.monotonic', side_effect=[0.0, 60.0]):
                self.assertIs(model._settings(), value)
                self.assertIs(model._settings(), value)
        loader.assert_called_once_with()

    def test_untracked_settings_loader_keeps_short_ttl(self):
        first, second = object(), object()
        loader = Mock(side_effect=[first, second])
        supervisor = SimpleNamespace(_load_settings=loader)
        model = ControlReadModel(supervisor, Path(':memory:'))
        with patch('cfr.control.read_model.time.monotonic', side_effect=[0.0, 6.0]):
            self.assertIs(model._settings(), first)
            self.assertIs(model._settings(), second)
        self.assertEqual(loader.call_count, 2)

    def test_read_model_uses_runtime_state_and_excludes_secret_value(self):
        supervisor = type('Supervisor', (), {
            'database': Path('cfr.sqlite3'),
            'current_state': lambda self: {
                'remote_execution_enabled': True,
                'accept_new_tasks': False,
                'feishu': {'state': 'running', 'running': True, 'last_error_code': None, 'last_error_message': None},
                'doctor': {'status': 'PASS', 'last_result_available': True},
            },
            'pairing_state': lambda self: {'state': 'idle'},
        })()
        settings = type('Settings', (), {
            'credentials_present': True,
            'app_id_source': 'persistent',
            'app_secret': 'should-never-appear',
            'app_secret_source': 'keyring',
            'allowed_open_ids': (),
            'allowed_workspace_roots': (),
            'operator_policy_source': 'missing',
            'workspace_policy_source': 'missing',
        })()
        with patch('cfr.control.read_model.load_settings', return_value=settings), patch('cfr.control.read_model.shutil.which', return_value='C:\\Tools\\codex.exe'):
            model = ControlReadModel(supervisor, Path('cfr.sqlite3')).build()
        self.assertTrue(model['feishu']['running'])
        self.assertFalse(model['runtime']['accept_new_tasks'])
        self.assertEqual(model['codex']['availability'], 'available')
        self.assertNotIn('should-never-appear', json.dumps(model))

    def test_workspace_preflight_uses_startup_directory_semantics_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory).resolve()
            missing = valid / 'missing-root'
            settings = type('Settings', (), {
                'credentials_present': True,
                'app_id_source': 'persistent',
                'app_secret': 'should-never-appear',
                'app_secret_source': 'keyring',
                'allowed_open_ids': ('operator',),
                'allowed_workspace_roots': (valid, missing),
                'operator_policy_source': 'persistent',
                'workspace_policy_source': 'persistent',
            })()
            supervisor = type('Supervisor', (), {
                '_load_settings': lambda self: settings,
                'current_state': lambda self: {
                    'remote_execution_enabled': True,
                    'accept_new_tasks': True,
                    'feishu': {'state': 'stopped', 'running': False, 'last_error_code': None, 'last_error_message': None},
                    'doctor': {'status': 'NOT_RUN', 'last_result_available': False},
                },
                'pairing_state': lambda self: {'state': 'idle'},
                'activity': lambda self: [],
            })()
            with patch('cfr.control.read_model.shutil.which', return_value=None):
                feishu = ControlReadModel(supervisor, Path(directory) / 'cfr.sqlite3').build()['feishu']
        self.assertEqual(feishu['workspace_roots'], [str(valid), str(missing)])
        self.assertFalse(feishu['workspace_roots_valid'])
        self.assertEqual(feishu['invalid_workspace_roots'], [str(missing)])

    def test_sessions_read_model_uses_existing_store(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            FeishuStore(database).create_pending_session('chat-1', 'p2p', 'owner-1', 'C:/workspace')
            sessions = ControlReadModel(object(), database).sessions()
        self.assertEqual(sessions[0]['chat_id'], 'chat-1')
        self.assertEqual(sessions[0]['pending_cwd'], 'C:/workspace')
        self.assertIsNone(sessions[0]['thread_id'])
        self.assertEqual(sessions[0]['approval_mode'], 'ask')

    def test_sessions_read_model_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            FeishuStore(database).create_pending_session('chat-1', 'p2p', 'owner-secret', 'C:/workspace')
            sessions = ControlReadModel(object(), database).sessions()
        self.assertNotIn('owner_open_id', sessions[0])
        self.assertNotIn('owner-secret', json.dumps(sessions))

    def test_sessions_read_model_combines_code_chat_and_selected_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            store = FeishuStore(database)
            store.create_pending_session('chat-1', 'p2p', 'owner-secret', directory)
            store.update_pending_thread_settings('chat-1', model='gpt-test', reasoning_effort='medium')
            store.set_selected_surface('chat-1', 'chat')
            store.ensure_chat_binding('chat-1')
            store.update_chat_binding('chat-1', tab_id='5', url='https://chatgpt.com/g/g-p-aabbcc-project/c/conv-1')
            session = ControlReadModel(object(), database).sessions()[0]
        self.assertEqual(session['selected_surface'], 'chat')
        self.assertEqual(session['state'], 'pending_initial')
        self.assertEqual(session['pending_settings']['model'], 'gpt-test')
        self.assertEqual(session['chat_state'], 'conversation')
        self.assertEqual(session['chat_tab_id'], '5')
        self.assertEqual(session['chat_project_id'], 'g-p-aabbcc')
        self.assertEqual(session['chat_conversation_id'], 'conv-1')
        self.assertNotIn('owner-secret', json.dumps(session))

    def test_bindings_read_model_uses_durable_binding_store_without_rollout_content(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            BindingStore(database).upsert_binding(ThreadRef('thread-1', 'native thread', Path(directory)))
            bindings = ControlReadModel(object(), database).bindings()
        self.assertEqual(bindings[0]['thread_id'], 'thread-1')
        self.assertEqual(bindings[0]['thread_name'], 'native thread')
        self.assertNotIn('rollout_path', bindings[0])

    def test_expensive_codex_catalog_reads_are_short_lived_cached(self):
        model = ControlReadModel(object(), Path('unused.sqlite3'))
        with patch('cfr.control.read_model.codex_models', return_value={'available': True, 'data': []}) as models, patch(
            'cfr.control.read_model.codex_capabilities', return_value={'context': 'default'}
        ) as capabilities, patch(
            'cfr.control.read_model.codex_settings', return_value={'available': True}
        ) as settings:
            self.assertIs(model.models(), model.models())
            self.assertIs(model.capabilities(), model.capabilities())
            self.assertIs(model.settings(), model.settings())
        self.assertEqual(models.call_count, 1)
        self.assertEqual(capabilities.call_count, 1)
        self.assertEqual(settings.call_count, 1)

    def test_model_default_write_invalidates_only_settings_cache(self):
        model = ControlReadModel(object(), Path('unused.sqlite3'))
        with patch('cfr.control.read_model.codex_settings', side_effect=[{'revision': 1}, {'revision': 2}]) as settings, patch(
            'cfr.control.read_model.write_codex_settings', return_value={'status': 'ok'}
        ):
            self.assertEqual(model.settings()['revision'], 1)
            self.assertEqual(model.write_model_defaults({})['status'], 'ok')
            self.assertEqual(model.settings()['revision'], 2)
        self.assertEqual(settings.call_count, 2)

    def test_bindings_read_model_marks_current_feishu_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            BindingStore(database).upsert_binding(ThreadRef('thread-1', 'native thread', Path(directory)))
            store = FeishuStore(database)
            store.create_pending_session('chat-1', 'p2p', 'owner-1', directory)
            store.bind_session('chat-1', 'thread-1')
            binding = ControlReadModel(object(), database).bindings()[0]
        self.assertEqual(binding['bound_chat_id'], 'chat-1')
