import unittest

from cfr.surfaces import execution_surfaces, find_execution_surface


class ExecutionSurfaceTests(unittest.TestCase):
    def test_code_is_only_ready_surface_and_chat_work_are_not_emulated(self):
        catalog = execution_surfaces()
        self.assertEqual(catalog['selected'], 'code')
        by_id = {item['id']: item for item in catalog['data']}
        self.assertTrue(by_id['code']['available'])
        self.assertFalse(by_id['chat']['available'])
        self.assertFalse(by_id['work']['available'])
        self.assertEqual(by_id['code']['authority'], 'codex')
        self.assertEqual(by_id['chat']['authority'], 'chatgpt_chat')
        self.assertEqual(by_id['work']['authority'], 'chatgpt_work')

    def test_surface_resolution_accepts_id_or_name(self):
        self.assertEqual(find_execution_surface('Code')['id'], 'code')
        self.assertEqual(find_execution_surface('work')['name'], 'Work')
        self.assertIsNone(find_execution_surface('plan'))

    def test_runtime_chat_health_can_enable_chat_without_enabling_work(self):
        catalog = execution_surfaces(selected='chat', chat_status={'available': True, 'status': 'ready'})
        by_id = {item['id']: item for item in catalog['data']}
        self.assertEqual(catalog['selected'], 'chat')
        self.assertTrue(by_id['chat']['available'])
        self.assertFalse(by_id['work']['available'])


if __name__ == '__main__':
    unittest.main()
