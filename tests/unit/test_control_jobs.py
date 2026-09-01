from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from cfr.codex.diagnostics import codex_interface_capabilities, missing_required_app_server_methods
from cfr.codex.turns import ActiveTurnRegistry
from cfr.control.read_model import ControlReadModel
from cfr.core.models import ActiveTurn, ThreadRef, TurnResult
from cfr.storage.db import BindingStore


class _ForbiddenClient:
    def request(self, *_args, **_kwargs):
        raise AssertionError('Jobs read model must not call app-server')


class JobsReadModelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / 'cfr.sqlite3'
        self.registry = ActiveTurnRegistry()
        daemon = SimpleNamespace(adapter=SimpleNamespace(registry=self.registry))
        self.model = ControlReadModel(SimpleNamespace(_daemon=daemon), self.database)
        bindings = BindingStore(self.database)
        for thread_id in ('thread-active', 'thread-completed', 'thread-failed', 'thread-interrupted', 'thread-future'):
            bindings.upsert_binding(ThreadRef(thread_id, None, Path(f'C:/safe/{thread_id}')))

    def tearDown(self):
        self.directory.cleanup()

    def test_active_native_turn_projects_running_job_without_app_server_read(self):
        self.registry.register(ActiveTurn('thread-active', 'turn-active', _ForbiddenClient()))
        jobs = self.model.jobs()
        self.assertEqual(jobs[0]['status'], 'running')
        self.assertTrue(jobs[0]['active'])
        self.assertEqual(jobs[0]['workspace'], 'thread-active')
        self.assertTrue(jobs[0]['can_interrupt'])

    def test_terminal_turn_results_project_completed_failed_and_interrupted(self):
        self.registry.finish(TurnResult('thread-completed', 'turn-completed', 'completed', 'secret output', 1, 2))
        self.registry.finish(TurnResult('thread-failed', 'turn-failed', 'failed', 'secret output', 1, 3, 'secret error'))
        self.registry.finish(TurnResult('thread-interrupted', 'turn-interrupted', 'interrupted', 'secret output', 1, 4))
        jobs = {job['turn_id']: job for job in self.model.jobs()}
        self.assertEqual(jobs['turn-completed']['status'], 'completed')
        self.assertEqual(jobs['turn-failed']['status'], 'failed')
        self.assertEqual(jobs['turn-interrupted']['status'], 'interrupted')
        self.assertNotIn('secret output', json.dumps(jobs))
        self.assertNotIn('secret error', json.dumps(jobs))

    def test_unknown_future_turn_status_is_safe_unknown(self):
        self.registry.finish(TurnResult('thread-future', 'turn-future', 'future_status', '', 1, 2))
        self.assertEqual(self.model.jobs()[0]['status'], 'unknown')

    def test_runtime_telemetry_is_sanitized_and_keeps_missing_values_null(self):
        telemetry = self.registry.begin('private-message-id', thread_id='thread-active', received_at=10.0, queued_at=10.0, execution_started_at=10.1)
        telemetry.set_identity(turn_id='turn-active')
        telemetry.set_runtime_settings({'model': 'gpt-test', 'reasoningEffort': 'medium', 'serviceTier': 'default'})
        telemetry.mark('turn_started_at', at=1.0, wall_at=10.2)
        telemetry.set_stage('running', status='running')
        self.registry.register(ActiveTurn('thread-active', 'turn-active', _ForbiddenClient(), telemetry=telemetry))

        job = self.model.jobs()[0]
        self.assertEqual(job['status'], 'running')
        self.assertTrue(job['can_interrupt'])
        self.assertEqual(job['runtime']['model'], 'gpt-test')
        self.assertIsNone(job['runtime']['metrics']['ttft_ms'])
        self.assertNotIn('correlation_id', job['runtime'])
        self.assertNotIn('private-message-id', json.dumps(job))

    def test_failed_preturn_observation_is_not_left_active(self):
        telemetry = self.registry.begin('private-message-id', received_at=10, queued_at=10, execution_started_at=10)
        telemetry.set_stage('failed', status='failed')
        job = self.model.jobs()[0]
        self.assertEqual(job['status'], 'failed')
        self.assertFalse(job['active'])
        self.assertFalse(job['can_interrupt'])

    def test_missing_optional_turns_capability_does_not_block_snapshot(self):
        schema = Path(self.directory.name) / 'schema.json'
        schema.write_text(json.dumps({'definitions': {'ThreadStatus': {'oneOf': []}, 'TurnStatus': {'enum': ['completed']}}}), encoding='utf-8')
        capabilities = codex_interface_capabilities(schema, 'test')
        self.assertTrue(capabilities.generated_schema)
        self.assertFalse(capabilities.thread_turns_list)
        self.assertFalse(capabilities.thread_turns_list_experimental)

    def test_required_interface_check_is_capability_based_not_version_based(self):
        schema = Path(self.directory.name) / 'required-schema.json'
        methods = [
            'account/read', 'config/read', 'thread/start', 'thread/resume', 'thread/read',
            'thread/settings/update', 'turn/start', 'turn/interrupt', 'model/list',
        ]
        schema.write_text(json.dumps({'methods': [{'const': method} for method in methods]}), encoding='utf-8')
        capabilities = codex_interface_capabilities(schema, 'future-version')
        self.assertEqual(missing_required_app_server_methods(capabilities), ())
