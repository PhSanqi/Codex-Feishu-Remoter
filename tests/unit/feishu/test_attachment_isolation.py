from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cfr.feishu.daemon import FeishuDaemon
from cfr.feishu.models import FeishuInboundMessage


class _AttachmentStore:
    def __init__(self):
        self.items = []

    def add_pending_attachment(self, chat_id, path, kind, name):
        item = {'attachment_id': str(len(self.items)), 'chat_id': chat_id, 'path': str(path), 'kind': kind, 'name': name}
        self.items.append(item)
        return item

    def clear_pending_attachments(self, _chat_id, ids):
        self.items = [item for item in self.items if item['attachment_id'] not in set(ids)]


class _AttachmentTransport:
    def download_file_to_file(self, _key, destination, *, message_id=None, file_name=None, resource_type='file'):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / file_name
        path.write_text(str(message_id), encoding='utf-8')
        return path


class _Replies:
    def reply_text(self, *_args, **_kwargs):
        return ['reply']


class AttachmentIsolationTests(unittest.TestCase):
    @staticmethod
    def _message(message_id):
        return FeishuInboundMessage(
            None, message_id, 'chat', 'p2p', 'operator', 'user', 'file', None,
            resources=({'type': 'file', 'file_key': f'key-{message_id}', 'file_name': 'report.csv'},),
        )

    def test_same_named_messages_are_downloaded_to_distinct_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=Path(directory)):
            daemon = FeishuDaemon.__new__(FeishuDaemon)
            daemon.transport = _AttachmentTransport()
            daemon.store = _AttachmentStore()
            daemon.replies = _Replies()
            daemon._selected_surface = lambda _chat_id: 'code'
            daemon._handle_attachment_message(self._message('message-one'))
            daemon._handle_attachment_message(self._message('message-two'))
            paths = [Path(item['path']) for item in daemon.store.items]
            self.assertEqual(len(set(paths)), 2)
            self.assertEqual([path.name for path in paths], ['report.csv', 'report.csv'])
            self.assertEqual([path.read_text(encoding='utf-8') for path in paths], ['message-one', 'message-two'])

            daemon._clear_pending_attachments('chat', list(daemon.store.items))
            self.assertTrue(all(not path.exists() for path in paths))
            self.assertFalse(any((Path(directory) / 'feishu' / 'inbox').rglob('*')))

    def test_orphan_cache_cleanup_keeps_only_durable_attachment_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=Path(directory)):
            inbox = Path(directory) / 'feishu' / 'inbox'
            kept = inbox / 'batch' / 'kept.txt'
            orphan = inbox / 'legacy-orphan.jpg'
            kept.parent.mkdir(parents=True)
            kept.write_text('keep', encoding='utf-8')
            orphan.write_bytes(b'orphan')
            removed = FeishuDaemon._cleanup_orphan_attachment_cache([kept])
            self.assertEqual(removed, 1)
            self.assertTrue(kept.exists())
            self.assertFalse(orphan.exists())

    def test_chat_delivery_cache_is_fully_transient_across_runtime_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=Path(directory)):
            root = Path(directory) / 'chatgpt' / 'downloads'
            first = root / 'delivery-a' / 'report.xlsx'
            second = root / 'legacy.png'
            first.parent.mkdir(parents=True)
            first.write_bytes(b'xlsx')
            second.write_bytes(b'png')
            removed = FeishuDaemon._cleanup_orphan_chat_download_cache()
            self.assertEqual(removed, 2)
            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.rglob('*')), [])


if __name__ == '__main__':
    unittest.main()
