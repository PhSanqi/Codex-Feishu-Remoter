import unittest

from cfr.control.codex_settings import CodexSettingsError, read, write


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def request(self, method, params, timeout=None):
        self.requests.append((method, params, timeout))
        response = self.responses[method]
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _factory(client):
    return lambda **_kwargs: client


def _model():
    return {
        'id': 'runtime-id', 'model': 'runtime-model', 'displayName': 'Runtime model', 'description': '', 'isDefault': True,
        'defaultReasoningEffort': 'custom-a', 'supportedReasoningEfforts': [{'reasoningEffort': 'custom-a', 'description': ''}, {'reasoningEffort': 'custom-b', 'description': ''}],
        'serviceTiers': [{'id': 'runtime-tier', 'name': 'Runtime tier', 'description': ''}], 'defaultServiceTier': 'runtime-tier',
    }


def _config(model='runtime-model', reasoning='custom-a', tier='runtime-tier'):
    return {
        'config': {'model': model, 'model_reasoning_effort': reasoning, 'service_tier': tier},
        'origins': {
            'model': {'name': {'type': 'user', 'file': 'C:/secret/config.toml'}},
            'model_reasoning_effort': {'name': {'type': 'user'}},
            'service_tier': {'name': {'type': 'system', 'file': 'C:/managed.toml'}},
        },
    }


class CodexSettingsTests(unittest.TestCase):
    def test_future_runtime_model_and_new_reasoning_effort_need_no_cfr_model_patch(self):
        astra = {
            'id': 'gpt-6-astra', 'model': 'gpt-6-astra', 'displayName': 'GPT-6 Astra',
            'description': 'future runtime model', 'isDefault': True,
            'defaultReasoningEffort': 'high',
            'supportedReasoningEfforts': [
                {'reasoningEffort': value, 'description': value}
                for value in ('low', 'medium', 'high', 'xhigh', 'max')
            ],
            'serviceTiers': [],
        }
        client = _Client({
            'model/list': [
                {'data': [astra], 'nextCursor': None},
                {'data': [astra], 'nextCursor': None},
            ],
            'config/batchWrite': {'status': 'ok', 'version': 'v-next'},
            'config/read': _config(model='gpt-6-astra', reasoning='max', tier=None),
            'configRequirements/read': {'requirements': None},
        })
        result = write(
            {'model': 'gpt-6-astra', 'reasoning_effort': 'max', 'service_tier': None},
            _factory(client),
        )
        edits = next(params['edits'] for method, params, _ in client.requests if method == 'config/batchWrite')
        self.assertEqual([edit['value'] for edit in edits], ['gpt-6-astra', 'max', None])
        self.assertEqual(result['current_state']['codex_model_defaults']['model']['effective_value'], 'gpt-6-astra')
        self.assertEqual(result['current_state']['codex_model_defaults']['reasoning_effort']['effective_value'], 'max')

    def test_read_projects_effective_values_and_sanitized_sources(self):
        client = _Client({
            'config/read': _config(),
            'configRequirements/read': {'requirements': {'models': {'newThread': {'model': 'managed-model', 'modelReasoningEffort': 'managed-effort', 'serviceTier': 'managed-tier'}}}},
            'model/list': {'data': [_model()], 'nextCursor': None},
        })
        result = read(_factory(client))
        defaults = result['codex_model_defaults']
        self.assertTrue(result['available'])
        self.assertEqual(defaults['model'], {'effective_value': 'runtime-model', 'source': 'user'})
        self.assertEqual(defaults['managed_new_thread_defaults']['service_tier'], 'managed-tier')
        self.assertNotIn('C:/secret', str(result))

    def test_write_uses_exact_fixed_keys_and_reads_back(self):
        client = _Client({
            'model/list': [{'data': [_model()], 'nextCursor': None}, {'data': [_model()], 'nextCursor': None}],
            'config/batchWrite': {'status': 'ok', 'version': 'v2', 'filePath': 'C:/secret/config.toml'},
            'config/read': _config(),
            'configRequirements/read': {'requirements': None},
        })
        result = write({'model': 'runtime-model', 'reasoning_effort': 'custom-b', 'service_tier': 'runtime-tier'}, _factory(client))
        batch = next(params for method, params, _ in client.requests if method == 'config/batchWrite')
        self.assertEqual([edit['keyPath'] for edit in batch['edits']], ['model', 'model_reasoning_effort', 'service_tier'])
        self.assertTrue(all(edit['mergeStrategy'] == 'replace' for edit in batch['edits']))
        self.assertEqual(result['status'], 'ok')
        self.assertIsNone(result['warning_code'])
        self.assertEqual([method for method, _, _ in client.requests], ['model/list', 'config/batchWrite', 'config/read', 'configRequirements/read', 'model/list'])

    def test_write_rejects_invalid_model_without_config_write(self):
        client = _Client({'model/list': {'data': [_model()], 'nextCursor': None}})
        with self.assertRaises(CodexSettingsError) as raised:
            write({'model': 'not-runtime', 'reasoning_effort': None, 'service_tier': None}, _factory(client))
        self.assertEqual(raised.exception.error_code, 'CONTROL_INVALID_CODEX_SETTING')
        self.assertNotIn('config/batchWrite', [method for method, _, _ in client.requests])

    def test_write_rejects_arbitrary_keys_without_config_write(self):
        client = _Client({'model/list': {'data': [_model()], 'nextCursor': None}})
        with self.assertRaises(CodexSettingsError) as raised:
            write({'model': None, 'reasoning_effort': None, 'service_tier': None, 'keyPath': 'approval_policy'}, _factory(client))
        self.assertEqual(raised.exception.error_code, 'CONTROL_INVALID_CODEX_SETTING')
        self.assertNotIn('config/batchWrite', [method for method, _, _ in client.requests])

    def test_write_rejects_reasoning_and_tier_outside_selected_model(self):
        for values in (
            {'model': 'runtime-model', 'reasoning_effort': 'hardcoded-effort', 'service_tier': None},
            {'model': 'runtime-model', 'reasoning_effort': None, 'service_tier': 'hardcoded-tier'},
        ):
            client = _Client({'model/list': {'data': [_model()], 'nextCursor': None}})
            with self.assertRaises(CodexSettingsError):
                write(values, _factory(client))
            self.assertNotIn('config/batchWrite', [method for method, _, _ in client.requests])

    def test_ok_overridden_is_success_with_sanitized_warning(self):
        client = _Client({
            'model/list': [{'data': [_model()], 'nextCursor': None}, {'data': [_model()], 'nextCursor': None}],
            'config/batchWrite': {'status': 'okOverridden', 'version': 'v2', 'filePath': 'C:/secret/config.toml', 'overriddenMetadata': {'overridingLayer': {'name': {'type': 'enterpriseManaged', 'file': 'C:/managed.toml'}}}},
            'config/read': _config(),
            'configRequirements/read': {'requirements': None},
        })
        result = write({'model': 'runtime-model', 'reasoning_effort': None, 'service_tier': None}, _factory(client))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['warning_code'], 'CONTROL_CODEX_CONFIG_OVERRIDDEN')
        self.assertEqual(result['overriding_source_type'], 'enterpriseManaged')
        self.assertNotIn('C:/', str(result))


if __name__ == '__main__':
    unittest.main()
