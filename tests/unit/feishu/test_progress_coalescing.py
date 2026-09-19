import threading
import unittest

from cfr.feishu.progress import FeishuChatProgress, FeishuTurnProgress


class ProgressCoalescingTests(unittest.TestCase):
    @staticmethod
    def _turn_progress():
        progress = FeishuTurnProgress.__new__(FeishuTurnProgress)
        progress.disabled = False
        progress._lock = threading.RLock()
        progress._state = {'state': 'running', 'answer_preview': ''}
        progress._revision = 0
        return progress

    @staticmethod
    def _chat_progress():
        progress = FeishuChatProgress.__new__(FeishuChatProgress)
        progress.disabled = False
        progress._lock = threading.RLock()
        progress._state = {'state': 'submitted', 'reasoning_text': '', 'answer_preview': ''}
        progress._revision = 0
        return progress

    def test_code_progress_revision_changes_only_when_visible_state_changes(self):
        progress = self._turn_progress()
        progress.on_progress({'state': 'running', 'answer_preview': ''})
        self.assertEqual(progress._revision, 0)
        progress.on_progress({'answer_preview': 'new output'})
        self.assertEqual(progress._revision, 1)
        progress.on_progress({'answer_preview': 'new output'})
        self.assertEqual(progress._revision, 1)

    def test_chat_progress_revision_changes_only_when_visible_state_changes(self):
        progress = self._chat_progress()
        progress.on_progress({'state': 'submitted', 'answer_preview': ''})
        self.assertEqual(progress._revision, 0)
        progress.on_progress({'state': 'generating'})
        self.assertEqual(progress._revision, 1)
        progress.on_progress({'state': 'generating'})
        self.assertEqual(progress._revision, 1)


if __name__ == '__main__':
    unittest.main()
