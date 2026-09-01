import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.run_feishu_sdk_probe import build_result


class SdkProbeTests(unittest.TestCase):
    def test_missing_sdk_is_fail_and_nonzero_contract(self):
        surface = {
            'FeishuChannelImport': False,
            'SendResultImport': False,
            'EventsMessage': False,
            'EventsCardAction': False,
            'MissingRequiredApi': ['FeishuChannelImport'],
        }
        result = build_result(None, None, surface)
        self.assertEqual(result['FeishuSdkProbe'], 'FEISHU_SDK_NOT_INSTALLED')
        self.assertEqual(result['Verdict'], 'FAIL')
        self.assertEqual(result['BlockingIssues'], ['FEISHU_SDK_INSTALL_REQUIRED'])

    def test_required_surface_missing_is_fail(self):
        surface = {
            'FeishuChannelImport': True,
            'SendResultImport': True,
            'EventsMessage': True,
            'EventsCardAction': False,
            'MissingRequiredApi': ['EventsCardAction'],
        }
        result = build_result('1.0.0', '1.0.0', surface)
        self.assertEqual(result['FeishuSdkProbe'], 'MISSING_REQUIRED_API')
        self.assertEqual(result['Verdict'], 'FAIL')

    def test_complete_surface_is_pass(self):
        surface = {
            'FeishuChannelImport': True,
            'SendResultImport': True,
            'EventsMessage': True,
            'EventsCardAction': True,
            'MissingRequiredApi': [],
        }
        result = build_result('1.0.0', '1.0.0', surface)
        self.assertEqual(result['FeishuSdkProbe'], 'PASS')
        self.assertEqual(result['Verdict'], 'PASS')
        self.assertEqual(result['BlockingIssues'], [])


if __name__ == '__main__':
    unittest.main()
