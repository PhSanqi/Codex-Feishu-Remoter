import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

from cfr.control.read_model import ControlReadModel
from cfr.core.models import ThreadRef
from cfr.feishu.store import FeishuStore
from cfr.storage.db import BindingStore


class ControlReadModelTests(unittest.TestCase):
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

    def test_sessions_read_model_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            FeishuStore(database).create_pending_session('chat-1', 'p2p', 'owner-secret', 'C:/workspace')
            sessions = ControlReadModel(object(), database).sessions()
        self.assertNotIn('owner_open_id', sessions[0])
        self.assertNotIn('owner-secret', json.dumps(sessions))

    def test_bindings_read_model_uses_durable_binding_store_without_rollout_content(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'cfr.sqlite3'
            BindingStore(database).upsert_binding(ThreadRef('thread-1', 'native thread', Path(directory)))
            bindings = ControlReadModel(object(), database).bindings()
        self.assertEqual(bindings[0]['thread_id'], 'thread-1')
        self.assertEqual(bindings[0]['thread_name'], 'native thread')
        self.assertNotIn('rollout_path', bindings[0])
