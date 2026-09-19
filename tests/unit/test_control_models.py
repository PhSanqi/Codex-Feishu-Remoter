import unittest

from cfr.codex.app_server import AppServerRpcError
from cfr.control.codex_catalog import capabilities, collaboration_modes, models


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


class CodexCatalogTests(unittest.TestCase):
    def test_future_model_is_discovered_without_cfr_slug_changes_and_preserves_extensions(self):
        client = _Client({'model/list': {'data': [{
            'id': 'gpt-6-astra',
            'model': 'gpt-6-astra',
            'displayName': 'GPT-6 Astra',
            'description': 'future runtime model',
            'isDefault': False,
            'defaultReasoningEffort': 'high',
            'supportedReasoningEfforts': [
                {'reasoningEffort': value, 'description': value}
                for value in ('low', 'medium', 'high', 'xhigh', 'max')
            ],
            'serviceTiers': [],
            'inputModalities': ['text', 'image'],
            'multiAgentVersion': 'v2',
            'futureCapability': {'mode': 'async-tools'},
        }], 'nextCursor': None}})
        result = models(_factory(client))
        model = result['data'][0]
        self.assertEqual(result['source'], 'codex_runtime')
        self.assertEqual(result['catalog_schema_version'], 1)
        self.assertEqual(model['model'], 'gpt-6-astra')
        self.assertEqual(model['multi_agent_version'], 'v2')
        self.assertEqual(model['supported_reasoning_efforts'][-1]['reasoning_effort'], 'max')
        self.assertEqual(model['extensions']['futureCapability'], {'mode': 'async-tools'})

    def test_upgrade_and_availability_metadata_are_normalized(self):
        client = _Client({'model/list': {'data': [{
            'id': 'old', 'model': 'old', 'displayName': 'Old', 'description': '',
            'isDefault': False, 'defaultReasoningEffort': 'low', 'supportedReasoningEfforts': [],
            'upgrade': 'new',
            'upgradeInfo': {'model': 'new', 'migrationMarkdown': 'move', 'retirementAt': 123},
            'availabilityNux': {'message': 'limited rollout'},
        }], 'nextCursor': None}})
        model = models(_factory(client))['data'][0]
        self.assertEqual(model['upgrade_model'], 'new')
        self.assertEqual(model['upgrade_info']['retirement_at'], 123)
        self.assertEqual(model['availability_message'], 'limited rollout')

    def test_models_use_runtime_catalog_without_hardcoded_entries(self):
        client = _Client({'model/list': {'data': [
            {'id': 'runtime-a', 'model': 'runtime-slug-a', 'displayName': 'Runtime A', 'description': 'A', 'isDefault': True, 'defaultReasoningEffort': 'custom-a', 'supportedReasoningEfforts': []},
            {'id': 'runtime-b', 'model': 'runtime-slug-b', 'displayName': 'Runtime B', 'description': 'B', 'isDefault': False, 'defaultReasoningEffort': 'custom-b', 'supportedReasoningEfforts': []},
        ], 'nextCursor': None}})
        result = models(_factory(client))
        self.assertTrue(result['available'])
        self.assertEqual([item['id'] for item in result['data']], ['runtime-a', 'runtime-b'])
        self.assertEqual(client.requests[0][0], 'model/list')
        self.assertEqual(client.requests[0][1]['includeHidden'], False)

    def test_model_pagination_and_runtime_field_order_are_preserved(self):
        client = _Client({'model/list': [
            {'data': [{'id': 'a', 'model': 'a', 'displayName': 'A', 'description': '', 'isDefault': False, 'defaultReasoningEffort': 'custom-a', 'supportedReasoningEfforts': [{'reasoningEffort': 'custom-a', 'description': ''}, {'reasoningEffort': 'custom-z', 'description': ''}, {'reasoningEffort': 'custom-b', 'description': ''}], 'serviceTiers': [{'id': 'current', 'name': 'Current', 'description': ''}], 'additionalSpeedTiers': ['deprecated']}], 'nextCursor': 'next'},
            {'data': [{'id': 'b', 'model': 'b', 'displayName': 'B', 'description': '', 'isDefault': False, 'defaultReasoningEffort': 'custom-b', 'supportedReasoningEfforts': [], 'serviceTiers': [], 'defaultServiceTier': None}], 'nextCursor': None},
        ]})
        result = models(_factory(client))
        self.assertEqual([item['id'] for item in result['data']], ['a', 'b'])
        self.assertEqual([item['reasoning_effort'] for item in result['data'][0]['supported_reasoning_efforts']], ['custom-a', 'custom-z', 'custom-b'])
        self.assertEqual(result['data'][0]['service_tiers'], [{'id': 'current', 'name': 'Current', 'description': ''}])
        self.assertEqual(client.requests[1][1]['cursor'], 'next')

    def test_model_list_failure_is_structured(self):
        client = _Client({'model/list': TimeoutError('do not expose this')})
        result = models(_factory(client))
        self.assertFalse(result['available'])
        self.assertEqual(result['error_code'], 'CODEX_MODEL_LIST_FAILED')
        self.assertEqual(result['data'], [])
        self.assertNotIn('do not expose this', result['message'])

    def test_missing_optional_capability_does_not_block_other_capabilities(self):
        client = _Client({
            'permissionProfile/list': AppServerRpcError('permissionProfile/list', -32601, 'Method not found'),
            'experimentalFeature/list': {'data': [{'name': 'runtime-feature', 'stage': 'beta', 'enabled': True, 'defaultEnabled': False, 'displayName': 'Runtime feature', 'description': 'Feature'}], 'nextCursor': None},
        })
        result = capabilities(_factory(client))
        self.assertEqual(result['context'], 'default')
        self.assertFalse(result['permission_profiles']['available'])
        self.assertEqual(result['permission_profiles']['error_code'], 'CODEX_PERMISSION_PROFILE_LIST_UNAVAILABLE')
        self.assertTrue(result['experimental_features']['available'])
        self.assertEqual(result['experimental_features']['data'][0]['name'], 'runtime-feature')
        self.assertEqual([request[0] for request in client.requests], ['permissionProfile/list', 'experimentalFeature/list'])

    def test_collaboration_modes_use_installed_experimental_catalog(self):
        client = _Client({'collaborationMode/list': {'data': [
            {'name': 'Plan', 'mode': 'plan', 'model': None, 'reasoning_effort': 'medium'},
            {'name': 'Default', 'mode': 'default', 'model': None, 'reasoning_effort': None},
        ]}})
        result = collaboration_modes(_factory(client))
        self.assertTrue(result['available'])
        self.assertEqual([item['mode'] for item in result['data']], ['plan', 'default'])
        self.assertEqual(client.requests[0][0], 'collaborationMode/list')
        self.assertEqual(client.requests[0][1], {})


if __name__ == '__main__':
    unittest.main()
