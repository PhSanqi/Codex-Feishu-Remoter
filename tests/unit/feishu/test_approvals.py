from pathlib import Path
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.codex.approvals import CodexApprovalCodec
from cfr.core.models import StructuredError


class ApprovalTests(unittest.TestCase):
    def test_known_command_approval_decodes(self):
        request = CodexApprovalCodec().decode({'id': 4, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 't', 'item': {'id': 'i', 'command': 'echo safe', 'cwd': 'C:\\tmp'}}}, 't')
        self.assertEqual(request.kind, 'command')
        self.assertEqual(request.command, 'echo safe')

    def test_unknown_request_is_rejected_by_codec_boundary(self):
        self.assertIsNone(CodexApprovalCodec().decode({'id': 4, 'method': 'request_user_input'}, 't'))

    def test_response_does_not_auto_approve_unknown_decision(self):
        with self.assertRaises(ValueError):
            CodexApprovalCodec.response('approve_all')


if __name__ == '__main__':
    unittest.main()
