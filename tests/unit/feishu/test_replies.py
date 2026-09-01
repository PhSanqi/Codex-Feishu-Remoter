from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.feishu.replies import FeishuReplyClient, deterministic_reply_uuid
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.transport import FakeFeishuTransport
from cfr.feishu.store import FeishuStore


class FailingTransport:
    def __init__(self):
        self.calls = []
        self.fail = True

    def reply_text(self, message_id, chat_id, text=None, uuid=None):
        if uuid is None:
            uuid, text = text, chat_id
        self.calls.append(uuid)
        if self.fail:
            raise RuntimeError('temporary transport failure')
        return 'reply-1'

    def send_text(self, open_id, text, uuid):
        return self.reply_text(open_id, text, uuid)

    def send_card(self, open_id, card, uuid):
        return self.reply_text(open_id, str(card), uuid)


class ReplyTests(unittest.TestCase):
    def test_failed_send_is_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            transport = FailingTransport()
            replies = FeishuReplyClient(transport, store)
            with self.assertRaises(RuntimeError):
                replies.reply_text('m', 'hello')
            self.assertEqual(store.get_reply_record('m', 'final', 0)['state'], 'failed')
            transport.fail = False
            self.assertEqual(replies.reply_text('m', 'hello'), ['reply-1'])
            self.assertEqual(transport.calls, [deterministic_reply_uuid('m', 'final', 0)] * 2)
            self.assertEqual(store.get_reply_record('m', 'final', 0)['state'], 'sent')

    def test_restart_reply_uses_durable_chat_id_not_transport_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            first_store = FeishuStore(path)
            first_store.enqueue_message(FeishuInboundMessage('evt', 'm-restart', 'oc-durable', 'p2p', 'ou', 'user', 'text', 'hello'))
            first_store.close()
            second_store = FeishuStore(path)
            record = second_store.get_inbox('m-restart')
            transport = FakeFeishuTransport()
            result = FeishuReplyClient(transport, second_store).reply_text(record.message_id, 'hello', chat_id=record.chat_id)
            self.assertEqual(result, ['fake-out-1'])
            self.assertEqual(transport.messages[0].target, 'oc-durable')

    def test_stale_pending_reply_becomes_retryable_on_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            self.assertTrue(store.reserve_reply('m-stale', 'final', 0))
            store.recover_on_startup()
            self.assertEqual(store.get_reply_record('m-stale', 'final', 0)['state'], 'failed')
            transport = FakeFeishuTransport()
            self.assertEqual(FeishuReplyClient(transport, store).reply_text('m-stale', 'retry'), ['fake-out-1'])


if __name__ == '__main__':
    unittest.main()
