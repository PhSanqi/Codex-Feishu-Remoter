from pathlib import Path
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.codex.approvals import ApprovalRequest
from cfr.core.models import StructuredError
from cfr.feishu.approval_card import build_approval_card_v2, inspect_approval_card_v2, parse_v2_card_callback


class ApprovalCardV2Tests(unittest.TestCase):
    def setUp(self):
        self.request = ApprovalRequest(
            'request-1', 'thread-1', 'turn-1', 'item-1', 'command',
            'run a bounded operation', 'echo safe', 'C:\\workspace', (), ('accept', 'decline'),
        )

    def test_approval_card_schema_is_2_0(self):
        card = build_approval_card_v2(self.request, 'approval-1').payload
        self.assertEqual(card['schema'], '2.0')

    def test_approval_card_has_no_legacy_action_tag(self):
        evidence = inspect_approval_card_v2(build_approval_card_v2(self.request, 'approval-1').payload)
        self.assertFalse(evidence['ApprovalCardLegacyActionTagPresent'])
        self.assertEqual(evidence['ApprovalCardV2Contract'], 'PASS')

    def test_approval_card_has_two_callback_buttons(self):
        evidence = inspect_approval_card_v2(build_approval_card_v2(self.request, 'approval-1').payload)
        self.assertEqual(evidence['ApprovalCardButtonCount'], 2)
        self.assertEqual(evidence['ApprovalCardAllowCallback'], 'PASS')
        self.assertEqual(evidence['ApprovalCardDeclineCallback'], 'PASS')

    def test_callback_values_share_durable_approval_id(self):
        payload = build_approval_card_v2(self.request, 'approval-1').payload
        values = []
        for column in payload['body']['elements'][1]['columns']:
            button = column['elements'][0]
            values.append(button['behaviors'][0]['value'])
        self.assertEqual({item['approval_id'] for item in values}, {'approval-1'})
        self.assertEqual({item['action'] for item in values}, {'allow_once', 'decline'})
        self.assertEqual({item['element_id'] for column in payload['body']['elements'][1]['columns'] for item in column['elements']}, {'cfr_allow', 'cfr_decline'})

    def test_card_contains_no_secret_fields(self):
        card = build_approval_card_v2(self.request, 'approval-1').payload
        self.assertTrue(inspect_approval_card_v2(card)['ApprovalCardNoSecretFields'])
        self.assertNotIn('raw_json_rpc', repr(card).lower())

    def test_v2_callback_allow_and_decline_normalize(self):
        self.assertEqual(parse_v2_card_callback({'action': 'allow_once', 'approval_id': 'approval-1'}, 'ou-1'), ('allow_once', 'approval-1', 'ou-1'))
        self.assertEqual(parse_v2_card_callback({'action': 'decline', 'approval_id': 'approval-1'}, 'ou-1'), ('decline', 'approval-1', 'ou-1'))

    def test_v2_callback_rejects_wrong_operator_or_malformed_value(self):
        for value, operator, tag in (
            ({'action': 'allow_once', 'approval_id': 'approval-1'}, '', 'button'),
            ({'action': 'unknown', 'approval_id': 'approval-1'}, 'ou-1', 'button'),
            ({'action': 'allow_once'}, 'ou-1', 'button'),
            ({'action': 'allow_once', 'approval_id': 'approval-1', 'secret': 'no'}, 'ou-1', 'button'),
            ({'action': 'allow_once', 'approval_id': 'approval-1'}, 'ou-1', 'action'),
        ):
            with self.assertRaises(StructuredError):
                parse_v2_card_callback(value, operator, tag)


if __name__ == '__main__':
    unittest.main()
