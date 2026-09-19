from pathlib import Path
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.codex.approvals import ApprovalRequest, CodexApprovalCodec
from cfr.core.models import StructuredError
from cfr.feishu.approval_card import build_approval_feedback_card


class ApprovalTests(unittest.TestCase):
    def test_known_command_approval_decodes(self):
        request = CodexApprovalCodec().decode({'id': 4, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 't', 'item': {'id': 'i', 'command': 'echo safe', 'cwd': 'C:\\tmp'}}}, 't')
        self.assertEqual(request.kind, 'command')
        self.assertEqual(request.command, 'echo safe')

    def test_current_command_and_permissions_protocols_preserve_native_risk_data(self):
        codec = CodexApprovalCodec()
        command = codec.decode({
            'id': 5,
            'method': 'item/commandExecution/requestApproval',
            'params': {'threadId': 't', 'item': {
                'kind': 'writeStdin', 'command': 'continue',
                'commandActions': [{'type': 'read', 'path': 'C:/workspace/input.txt'}],
                'additionalPermissions': {'network': {'enabled': True}},
            }},
        }, 't')
        self.assertEqual(command.kind, 'write_stdin')
        self.assertEqual(command.command_actions[0]['path'], 'C:/workspace/input.txt')
        self.assertEqual(command.requested_permissions, {'network': {'enabled': True}})

        permissions = codec.decode({
            'id': 6,
            'method': 'item/permissions/requestApproval',
            'params': {
                'threadId': 't', 'turnId': 'turn-1', 'itemId': 'item-1',
                'permissions': {'network': {'enabled': True}},
                'reason': 'download dependency',
            },
        }, 't')
        self.assertEqual(permissions.kind, 'permissions')
        self.assertEqual(codec.response('accept', permissions), {
            'permissions': {'network': {'enabled': True}}, 'scope': 'turn',
        })
        self.assertEqual(codec.response('decline', permissions), {'permissions': {}, 'scope': 'turn'})

    def test_unknown_request_is_rejected_by_codec_boundary(self):
        self.assertIsNone(CodexApprovalCodec().decode({'id': 4, 'method': 'request_user_input'}, 't'))
        self.assertIsNone(CodexApprovalCodec().decode({'id': 4, 'method': ['invalid']}, 't'))

    def test_malformed_known_request_fails_closed_and_string_path_stays_one_path(self):
        codec = CodexApprovalCodec()
        self.assertIsNone(codec.decode({'method': 'item/fileChange/requestApproval', 'params': {}}, 't'))
        self.assertIsNone(codec.decode({'id': 1, 'method': 'item/fileChange/requestApproval', 'params': []}, 't'))
        request = codec.decode({
            'id': 2,
            'method': 'item/fileChange/requestApproval',
            'params': {'item': {'changedPaths': 'C:/safe/report.csv'}},
        }, 't')
        self.assertEqual(request.changed_paths, ('C:/safe/report.csv',))

    def test_current_file_change_protocol_preserves_reason_and_grant_root_without_fake_paths(self):
        request = CodexApprovalCodec().decode({
            'id': 7,
            'method': 'item/fileChange/requestApproval',
            'params': {
                'threadId': 'thread-1',
                'turnId': 'turn-1',
                'itemId': 'item-1',
                'reason': 'write final workbook',
                'grantRoot': 'C:/workspace/test',
            },
        }, 'thread-1')
        self.assertEqual(request.kind, 'file_change')
        self.assertEqual(request.reason, 'write final workbook')
        self.assertEqual(request.grant_root, 'C:/workspace/test')
        self.assertEqual(request.changed_paths, ())

    def test_current_file_change_protocol_accepts_cfr_item_projection_without_changing_wire_fields(self):
        request = CodexApprovalCodec().decode({
            'id': 8,
            'method': 'item/fileChange/requestApproval',
            'params': {
                'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item-1',
                '_cfrChangedPaths': ['C:/workspace/test/report.xlsx'],
            },
        }, 'thread-1')
        self.assertEqual(request.changed_paths, ('C:/workspace/test/report.xlsx',))

    def test_current_file_change_card_uses_execution_workspace_and_explains_missing_paths(self):
        request = ApprovalRequest(
            'request-1', 'thread-1', 'turn-1', 'item-1', 'file_change', None, None,
            'C:/workspace/test', (), ('accept', 'decline'), grant_root=None,
        )
        card = build_approval_feedback_card(request, 'approval-1').payload
        content = card['body']['elements'][0]['content']
        self.assertIn('**Workspace:** `C:/workspace/test`', content)
        self.assertIn('Codex requests permission to write files', content)
        self.assertIn('does not include per-path details', content)
        self.assertNotIn('<unknown workspace>', content)

    def test_response_does_not_auto_approve_unknown_decision(self):
        with self.assertRaises(ValueError):
            CodexApprovalCodec.response('approve_all')


if __name__ == '__main__':
    unittest.main()
