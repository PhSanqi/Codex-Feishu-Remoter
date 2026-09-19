from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.feishu.replies import FeishuReplyClient, chunk_text, deterministic_reply_uuid
from cfr.core.models import StructuredError
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
    def test_text_chunking_is_bounded_by_utf8_bytes_and_round_trips(self):
        value = ('中文🙂abc' * 4000) + '结束'
        parts = chunk_text(value, limit=12000)
        self.assertEqual(''.join(parts), value)
        self.assertTrue(parts)
        self.assertLessEqual(max(len(part.encode('utf-8')) for part in parts), 12000)

    def test_text_chunking_rejects_impossible_or_nonpositive_limits(self):
        with self.assertRaises(ValueError):
            chunk_text('🙂', limit=3)
        with self.assertRaises(ValueError):
            chunk_text('x', limit=0)

    def test_blank_text_is_not_sent_or_reserved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            transport = FakeFeishuTransport()
            replies = FeishuReplyClient(transport, store)
            self.assertEqual(replies.reply_text('m-empty', '   ', chat_id='oc'), [])
            self.assertEqual(transport.messages, [])
            self.assertIsNone(store.get_reply_record('m-empty', 'final', 0))

    def test_transient_structured_delivery_error_retries_with_same_uuid(self):
        class TransientTransport(FailingTransport):
            def __init__(self):
                super().__init__()
                self.fail = False

            def reply_text(self, message_id, chat_id, text=None, uuid=None):
                if uuid is None:
                    uuid, text = text, chat_id
                self.calls.append(uuid)
                if len(self.calls) == 1:
                    raise StructuredError('FEISHU_API_TIMEOUT', 'temporary timeout')
                return 'reply-1'

        with tempfile.TemporaryDirectory() as directory:
            store = FeishuStore(Path(directory) / 'db.sqlite3')
            transport = TransientTransport()
            replies = FeishuReplyClient(transport, store)
            self.assertEqual(replies.reply_text('m-retry', 'hello', chat_id='oc'), ['reply-1'])
            expected = deterministic_reply_uuid('m-retry', 'final', 0)
            self.assertEqual(transport.calls, [expected, expected])

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

    def test_file_and_video_replies_are_independently_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FeishuStore(root / 'db.sqlite3')
            transport = FakeFeishuTransport()
            replies = FeishuReplyClient(transport, store)
            report = root / 'report.txt'
            video = root / 'clip.mp4'
            report.write_text('report', encoding='utf-8')
            video.write_bytes(b'video')
            self.assertEqual(replies.reply_file('m', report, phase='batch-file', chat_id='oc'), ['fake-out-1'])
            self.assertEqual(replies.reply_video('m', video, phase='batch-video', chat_id='oc'), ['fake-out-2'])
            self.assertEqual(replies.reply_file('m', report, phase='batch-file', chat_id='oc'), ['fake-out-1'])
            self.assertEqual([item.kind for item in transport.messages], ['reply_file', 'reply_video'])


if __name__ == '__main__':
    unittest.main()
