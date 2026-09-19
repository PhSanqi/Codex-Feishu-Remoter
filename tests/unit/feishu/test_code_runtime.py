from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from cfr.core.models import StructuredError, TurnResult
from cfr.feishu.code_runtime import CodeArtifactCollector, CodeSurfaceRuntime


class _Replies:
    def __init__(self):
        self.text = []
        self.images = []
        self.files = []
        self.videos = []

    def reply_text(self, _message_id, text, *_args, **_kwargs):
        self.text.append(text)
        return ['text-reply']

    def reply_image(self, _message_id, image, *_args, **_kwargs):
        self.images.append(bytes(image))
        return ['image-reply']

    def reply_file(self, _message_id, path, *_args, **_kwargs):
        self.files.append(Path(path))
        return ['file-reply']

    def reply_video(self, _message_id, path, *_args, **_kwargs):
        self.videos.append(Path(path))
        return ['video-reply']


class CodeRuntimeTests(unittest.TestCase):
    def _adapter(self, codex_home):
        return SimpleNamespace(
            resolved_cfr_codex_home=SimpleNamespace(path=Path(codex_home)),
            registry=None,
        )

    def test_empty_final_can_resend_latest_native_generated_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / 'generated_images' / 'thread-1'
            generated.mkdir(parents=True)
            old = generated / 'older.png'
            latest = generated / 'latest.png'
            old.write_bytes(b'old')
            latest.write_bytes(b'latest')
            now = time.time()
            import os
            os.utime(old, (now - 30, now - 30))
            os.utime(latest, (now - 20, now - 20))
            collector = CodeArtifactCollector(self._adapter(root))
            paths = collector.collect(
                text='', thread_id='thread-1', workspace=root / 'workspace',
                started_at=now, excluded=(),
            )
            self.assertEqual(paths, [latest.resolve()])

    def test_fresh_generated_image_is_collected_without_markdown_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / 'generated_images' / 'thread-1'
            generated.mkdir(parents=True)
            image = generated / 'result.png'
            image.write_bytes(b'png')
            collector = CodeArtifactCollector(self._adapter(root))
            paths = collector.collect(
                text='处理完成。', thread_id='thread-1', workspace=root / 'workspace',
                started_at=time.time() - 2, excluded=(),
            )
            self.assertEqual(paths, [image.resolve()])

    def test_empty_final_delivers_image_without_invalid_empty_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / 'generated_images' / 'thread-1'
            generated.mkdir(parents=True)
            image = generated / 'result.png'
            image.write_bytes(b'png')
            replies = _Replies()
            runtime = CodeSurfaceRuntime(
                settings=None,
                store=SimpleNamespace(),
                binding_store=SimpleNamespace(),
                adapter=self._adapter(root),
                replies=replies,
                approvals=SimpleNamespace(),
            )
            message = SimpleNamespace(message_id='m', chat_id='oc')
            ids = runtime.deliver_final(
                message,
                TurnResult('thread-1', 'turn-1', 'completed', '', int(time.time()) - 1),
                root / 'workspace',
            )
            self.assertEqual(ids, ['image-reply'])
            self.assertEqual(replies.text, [])
            self.assertEqual(replies.images, [b'png'])

    def test_large_image_is_sent_as_file_without_loading_entire_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'large.png'
            with image.open('wb') as stream:
                stream.seek(21 * 1024 * 1024)
                stream.write(b'x')
            replies = _Replies()
            runtime = CodeSurfaceRuntime(
                settings=None,
                store=SimpleNamespace(),
                binding_store=SimpleNamespace(),
                adapter=self._adapter(root),
                replies=replies,
                approvals=SimpleNamespace(),
            )
            message = SimpleNamespace(message_id='m', chat_id='oc')
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('large image must not be read into memory')):
                response_ids = runtime._send_artifact(message, image)
            self.assertEqual(response_ids, ['file-reply'])
            self.assertEqual(replies.images, [])
            self.assertEqual(replies.files, [image])

    def test_explicit_workspace_deliverable_is_found_but_staged_input_is_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            staged = workspace / '.cfr' / 'attachments' / 'input.pdf'
            report = workspace / 'report.xlsx'
            staged.parent.mkdir(parents=True)
            staged.write_bytes(b'input')
            report.write_bytes(b'xlsx')
            collector = CodeArtifactCollector(self._adapter(root / 'codex'))
            paths = collector.collect(
                text=f'[report]({report})', thread_id='thread-1', workspace=workspace,
                started_at=time.time() - 2, excluded=(staged,),
            )
            self.assertEqual(paths, [report.resolve()])

    def test_bare_windows_style_deliverable_path_is_recognized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            report = workspace / 'report.xlsx'
            report.write_bytes(b'xlsx')
            collector = CodeArtifactCollector(self._adapter(root / 'codex'))
            windows_path = str(report.resolve()).replace('/', '\\')
            paths = collector.collect(
                text=f'已生成：{windows_path}', thread_id='thread-1', workspace=workspace,
                started_at=time.time() - 30, excluded=(),
            )
            self.assertEqual(paths, [report.resolve()])

    def test_stale_generic_temp_file_is_not_a_resendable_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            stale = root / 'stale.png'
            stale.write_bytes(b'stale')
            old = time.time() - 120
            import os
            os.utime(stale, (old, old))
            collector = CodeArtifactCollector(self._adapter(root / 'codex'))
            with patch('cfr.feishu.code_runtime.tempfile.gettempdir', return_value=str(root)):
                paths = collector.collect(
                    text=f'[stale]({stale})', thread_id='thread-1', workspace=workspace,
                    started_at=time.time() - 5, excluded=(),
                )
            self.assertEqual(paths, [])

    def test_stale_workspace_file_is_not_auto_delivered_from_model_prose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            stale = workspace / 'old-report.xlsx'
            stale.write_bytes(b'old')
            old = time.time() - 120
            import os
            os.utime(stale, (old, old))
            collector = CodeArtifactCollector(self._adapter(root / 'codex'))
            paths = collector.collect(
                text=f'[old report]({stale})', thread_id='thread-1', workspace=workspace,
                started_at=time.time() - 5, excluded=(),
            )
            self.assertEqual(paths, [])

    def test_attachment_staging_rolls_back_partial_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            source = root / 'source.pdf'
            workspace.mkdir()
            source.write_bytes(b'pdf')
            attachments = [
                {'attachment_id': 'first', 'kind': 'file', 'path': str(source), 'name': 'source.pdf'},
                {'attachment_id': 'second', 'kind': 'file', 'path': str(root / 'missing.pdf'), 'name': 'missing.pdf'},
            ]
            with self.assertRaises(FileNotFoundError):
                CodeSurfaceRuntime.prepare_attachments(attachments, workspace)
            staged = workspace / '.cfr' / 'attachments'
            self.assertEqual(list(staged.glob('*')) if staged.exists() else [], [])

    def test_attachment_state_cleanup_failure_does_not_delete_retryable_cache_or_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'feishu' / 'inbox'
            inbox.mkdir(parents=True)
            source = inbox / 'input.pdf'
            source.write_bytes(b'pdf')
            runtime = CodeSurfaceRuntime(
                settings=None,
                store=SimpleNamespace(clear_pending_attachments=Mock(side_effect=RuntimeError('database busy'))),
                binding_store=SimpleNamespace(),
                adapter=self._adapter(root / 'codex'),
                replies=_Replies(),
                approvals=SimpleNamespace(),
            )
            attachment = {'attachment_id': 'a', 'path': str(source), 'kind': 'file'}
            with patch('cfr.feishu.code_runtime.resolve_cfr_config_dir', return_value=root):
                self.assertFalse(runtime.clear_attachments('oc', [attachment]))
            self.assertTrue(source.exists())

    def test_bound_session_missing_binding_fails_before_codex_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(allowed_workspace_roots=(root,))
            session = SimpleNamespace(state='bound', thread_id='missing-thread', pending_cwd=None)
            store = SimpleNamespace(
                get_session=lambda _chat_id: session,
                list_pending_attachments=lambda _chat_id: [{'attachment_id': 'a', 'path': str(root / 'input.pdf')}],
            )
            adapter = self._adapter(root / 'codex')
            adapter.send_message = Mock(side_effect=AssertionError('Codex must not start'))
            runtime = CodeSurfaceRuntime(
                settings=settings,
                store=store,
                binding_store=SimpleNamespace(get_binding=lambda _thread_id: None),
                adapter=adapter,
                replies=_Replies(),
                approvals=SimpleNamespace(),
            )
            message = SimpleNamespace(message_id='m', chat_id='oc', sender_open_id='ou', text='run')
            with self.assertRaises(StructuredError) as caught:
                runtime.handle_prompt(message)
            self.assertEqual(caught.exception.code, 'CFR_BINDING_MISSING')
            adapter.send_message.assert_not_called()

    def test_revoked_bound_workspace_fails_before_codex_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / 'allowed'
            revoked = root / 'revoked'
            allowed.mkdir()
            revoked.mkdir()
            session = SimpleNamespace(state='bound', thread_id='thread-1', pending_cwd=None)
            binding = SimpleNamespace(cwd=revoked)
            adapter = self._adapter(root / 'codex')
            adapter.send_message = Mock(side_effect=AssertionError('Codex must not start'))
            runtime = CodeSurfaceRuntime(
                settings=SimpleNamespace(allowed_workspace_roots=(allowed,)),
                store=SimpleNamespace(get_session=lambda _chat_id: session, list_pending_attachments=lambda _chat_id: []),
                binding_store=SimpleNamespace(get_binding=lambda _thread_id: binding),
                adapter=adapter,
                replies=_Replies(),
                approvals=SimpleNamespace(),
            )
            message = SimpleNamespace(message_id='m', chat_id='oc', sender_open_id='ou', text='run')
            with self.assertRaises(StructuredError) as caught:
                runtime.handle_prompt(message)
            self.assertEqual(caught.exception.code, 'FEISHU_WORKSPACE_NOT_ALLOWED')
            adapter.send_message.assert_not_called()

    def test_codex_file_citation_marker_is_collected_and_hidden_from_user_text(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            report = workspace / '附件信息汇总.csv'
            report.write_text('name,type\nreport,csv\n', encoding='utf-8')
            collector = CodeArtifactCollector(self._adapter(workspace / 'codex'))
            text = f'文件已经生成。\n:codex-file-citation{{path="{report}" purpose="output"}}'
            paths = collector.collect(
                text=text,
                thread_id='thread-1',
                workspace=workspace,
                started_at=time.time() - 2,
                excluded=(),
            )
            self.assertEqual(paths, [report.resolve()])
            visible = collector.user_visible_text(text)
            self.assertEqual(visible, '文件已经生成。')
            self.assertNotIn('codex-file-citation', visible)

    def test_codex_file_citation_delivers_real_file_instead_of_raw_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            report = workspace / 'report.xlsx'
            report.write_bytes(b'xlsx')
            replies = _Replies()
            runtime = CodeSurfaceRuntime(
                settings=None,
                store=SimpleNamespace(),
                binding_store=SimpleNamespace(),
                adapter=self._adapter(workspace / 'codex'),
                replies=replies,
                approvals=SimpleNamespace(),
            )
            marker = f':codex-file-citation{{path="{report}" purpose="output"}}'
            ids = runtime.deliver_final(
                SimpleNamespace(message_id='m', chat_id='oc'),
                TurnResult('thread-1', 'turn-1', 'completed', f'已整理完成。\n{marker}', int(time.time()) - 1),
                workspace,
            )
            self.assertEqual(ids, ['text-reply', 'file-reply'])
            self.assertEqual(replies.files, [report.resolve()])
            self.assertEqual(replies.text, ['已整理完成。'])


if __name__ == '__main__':
    unittest.main()
