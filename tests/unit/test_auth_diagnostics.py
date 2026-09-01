import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from cfr.codex.diagnostics import (
    classify_login_output,
    gate_metadata,
    normalize_gate_origin,
    routing_decision,
    runtime_execution_context,
    safe_error,
    sanitize_endpoint,
    summarize_config,
)
from cfr.codex.launcher import CodexLauncher


class AuthDiagnosticsTests(unittest.TestCase):
    def test_login_classification_does_not_retain_key_material(self):
        self.assertEqual(classify_login_output('Logged in using an API key sk-secret-value'), 'API_KEY')
        self.assertEqual(classify_login_output('Logged in using ChatGPT'), 'CHATGPT')
        self.assertEqual(classify_login_output('Not logged in'), 'UNKNOWN')
        self.assertNotIn('secret', safe_error('Authorization: Bearer secret https://api.openai.com/v1/responses'))

    def test_routing_mismatch_is_explicit(self):
        expected, consistency = routing_decision('CHATGPT', {'OpenAiBaseUrl': 'https://api.openai.com/v1'})
        self.assertEqual(expected, 'CHATGPT_CODEX_BACKEND')
        self.assertEqual(consistency, 'MISMATCH')
        self.assertEqual(sanitize_endpoint('stream disconnected at https://api.openai.com/v1/responses?token=hidden'), 'https://api.openai.com/v1/responses')

    def test_config_override_is_process_local_command_argument(self):
        launcher = CodexLauncher(
            {'features.responses_websockets': False},
            executable='codex',
            environment={'PATH': ''},
        )
        command = launcher.build_app_server_command()
        self.assertIn('--config', command)
        self.assertIn('features.responses_websockets=False', command)
        self.assertEqual(summarize_config({'model_provider': 'openai/default', 'openai_base_url': 'https://api.openai.com/v1'})['OpenAiBaseUrl'], 'https://api.openai.com')

    def test_gate_origin_metadata_is_explicit_and_report_only(self):
        self.assertEqual(normalize_gate_origin(None), 'unknown')
        self.assertEqual(runtime_execution_context('desktop_agent'), 'CODEX_AGENT_SANDBOX')
        self.assertEqual(runtime_execution_context('host_manual'), 'HOST')
        self.assertEqual(gate_metadata('unknown'), {'GateOrigin': 'unknown', 'RuntimeExecutionContext': 'UNKNOWN'})
        with self.assertRaises(ValueError):
            normalize_gate_origin('automatic_host')


if __name__ == '__main__':
    unittest.main()
