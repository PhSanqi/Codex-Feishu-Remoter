from pathlib import Path
import asyncio
import io
import logging
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from cfr.feishu.config import FeishuSettings
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.transport import ChannelFeishuTransport, _channel_proxy_url, _normalize_channel_proxy_url, channel_sdk_metadata
from cfr.feishu.transport import _require_send_success
from cfr.feishu.sdk_compat import ChannelSdkLoopDiagnostic, DeviceFlowPreCloseResult
from cfr.feishu.log_sanitize import configure_feishu_sdk_logging, sanitize_feishu_log_text
from cfr.feishu.config import load_settings
from unittest.mock import patch


def close_lark_channel_global_loop():
    try:
        import lark_channel.ws.client as module
        loop = getattr(module, 'loop', None)
        if loop is not None and not loop.is_running() and not loop.is_closed():
            loop.close()
    except Exception:
        pass


class NormalizedMessage:
    id = 'om-1'
    event_id = 'evt-1'
    conversation = type('Conversation', (), {'id': 'oc-1', 'type': 'group'})()
    sender = type('Identity', (), {'id': 'ou-1', 'type': 'user'})()
    raw_content_type = 'text'
    body_text = 'hello'
    mentioned_bot = True
    mentions = ()
    sender_is_bot = False


class ChannelTransportTests(unittest.TestCase):
    def test_generic_desktop_socks_proxy_is_normalized_to_socks5h(self):
        self.assertEqual(
            _normalize_channel_proxy_url('socks://127.0.0.1:57777/'),
            'socks5h://127.0.0.1:57777/',
        )

    def test_channel_proxy_prefers_environment_and_normalizes_socks(self):
        self.assertEqual(
            _channel_proxy_url(
                environment={'ALL_PROXY': 'socks://127.0.0.1:57777/'},
                system_proxies={'all': 'socks://127.0.0.1:9999/'},
            ),
            'socks5h://127.0.0.1:57777/',
        )

    def test_channel_proxy_can_normalize_system_socks_proxy(self):
        self.assertEqual(
            _channel_proxy_url(environment={}, system_proxies={'all': 'socks://127.0.0.1:57777/'}),
            'socks5h://127.0.0.1:57777/',
        )

    def test_async_callback_offloads_sync_handler_that_uses_asyncio_run(self):
        transport = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))
        observed = []

        def handler(message, event_id):
            async def bridge():
                return {'status': 'queued', 'message_id': message.message_id, 'event_id': event_id}
            observed.append(message)
            return asyncio.run(bridge())

        transport._message_handler = handler
        result = asyncio.run(transport._on_message(NormalizedMessage()))
        self.assertEqual(result, {'status': 'queued', 'message_id': 'om-1', 'event_id': 'evt-1'})
        self.assertEqual(len(observed), 1)
        self.assertEqual((observed[0].chat_id, observed[0].text), ('oc-1', 'hello'))

    def test_async_callback_keeps_async_handler_and_propagates_sync_failure(self):
        transport = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))

        async def async_handler(message, event_id):
            return {'status': 'async', 'message_id': message.message_id, 'event_id': event_id}

        transport._message_handler = async_handler
        self.assertEqual(asyncio.run(transport._on_message(NormalizedMessage()))['status'], 'async')

        def failing_handler(*_args):
            raise RuntimeError('handler failure')

        transport._message_handler = failing_handler
        with self.assertRaisesRegex(RuntimeError, 'handler failure'):
            asyncio.run(transport._on_message(NormalizedMessage()))

    def test_transport_loop_stays_alive_until_start_worker_terminal(self):
        source = (Path(__file__).resolve().parents[3] / 'src' / 'cfr' / 'feishu' / 'transport.py').read_text(encoding='utf-8')
        shutdown = source.index('self._loop.run_until_complete(self._shutdown_channel_sync())')
        asyncgens = source.index('self._loop.run_until_complete(self._loop.shutdown_asyncgens())')
        self.assertLess(shutdown, asyncgens)
        self.assertIn('await shutdown_channel_without_device_flow_close(', source)

    def test_shutdown_and_approval_share_start_worker_serialization(self):
        transport_source = (Path(__file__).resolve().parents[3] / 'src' / 'cfr' / 'feishu' / 'transport.py').read_text(encoding='utf-8')
        compat_source = (Path(__file__).resolve().parents[3] / 'src' / 'cfr' / 'feishu' / 'sdk_compat.py').read_text(encoding='utf-8')
        self.assertIn('shutdown_channel_without_device_flow_close', transport_source)
        self.assertIn('handoff_channel_bg_ownership', compat_source)

    def test_backend_is_channel_sdk_and_normalizes_neutral_fields(self):
        settings = FeishuSettings('app', 'secret', (), ())
        transport = ChannelFeishuTransport(settings)
        message = transport._normalize_message(NormalizedMessage())
        self.assertIsInstance(message, FeishuInboundMessage)
        self.assertEqual(message.body_text, 'hello')
        self.assertTrue(message.mentioned_bot)
        self.assertEqual(message.message_type, 'text')
        self.assertEqual(transport.backend_name, 'lark-channel-sdk')

    def test_official_content_text_is_primary_contract(self):
        message = type('ChannelInboundFixture', (), {
            'message_id': 'om-official', 'chat_id': 'oc-official', 'chat_type': 'p2p',
            'sender_id': 'ou-official', 'raw_content_type': 'text',
            'content_text': '/cfr help', 'mentioned_bot': False, 'sender_is_bot': False,
        })()
        normalized = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))._normalize_message(message)
        self.assertEqual(normalized.text, '/cfr help')
        self.assertEqual(normalized.body_text, '/cfr help')

    def test_content_text_precedes_legacy_body_text(self):
        message = type('ChannelInboundFixture', (), {
            'message_id': 'om-precedence', 'chat_id': 'oc-precedence', 'chat_type': 'p2p',
            'sender_id': 'ou-precedence', 'raw_content_type': 'text',
            'content_text': 'official', 'body_text': 'legacy',
        })()
        normalized = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))._normalize_message(message)
        self.assertEqual(normalized.text, 'official')

    def test_missing_chat_type_normalizes_unknown(self):
        message = type('ChannelInboundFixture', (), {
            'message_id': 'om-missing-type', 'chat_id': 'oc-missing-type',
            'sender_id': 'ou-missing-type', 'raw_content_type': 'text',
            'content_text': 'hello',
        })()
        normalized = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))._normalize_message(message)
        self.assertEqual(normalized.chat_type, 'unknown')

    def test_non_text_content_is_blocked_even_with_content_text(self):
        message = type('ChannelInboundFixture', (), {
            'message_id': 'om-file-content', 'chat_id': 'oc-file-content', 'chat_type': 'p2p',
            'sender_id': 'ou-file-content', 'raw_content_type': 'file',
            'content_text': '<file>',
        })()
        normalized = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))._normalize_message(message)
        self.assertEqual(normalized.message_type, 'file')
        self.assertIsNone(normalized.text)

    def test_image_resource_identity_survives_channel_normalization(self):
        resource = type('Resource', (), {'type': 'image', 'file_key': 'img_test_1', 'file_name': None})()
        message = type('ChannelInboundFixture', (), {
            'message_id': 'om-image', 'chat_id': 'oc-image', 'chat_type': 'p2p',
            'sender_id': 'ou-image', 'raw_content_type': 'image', 'resources': (resource,),
        })()
        normalized = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))._normalize_message(message)
        self.assertEqual(normalized.message_type, 'image')
        self.assertEqual(normalized.resources, ({'type': 'image', 'file_key': 'img_test_1', 'file_name': None},))

    def test_file_resource_identity_survives_channel_normalization(self):
        resource = type('Resource', (), {'type': 'file', 'file_key': 'file_test_1', 'file_name': 'report.pdf'})()
        message = type('ChannelInboundFixture', (), {
            'message_id': 'om-file', 'chat_id': 'oc-file', 'chat_type': 'p2p',
            'sender_id': 'ou-file', 'raw_content_type': 'file', 'resources': (resource,),
        })()
        normalized = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))._normalize_message(message)
        self.assertEqual(normalized.message_type, 'file')
        self.assertEqual(normalized.resources, ({'type': 'file', 'file_key': 'file_test_1', 'file_name': 'report.pdf'},))

    def test_video_resource_identity_survives_channel_normalization(self):
        resource = type('Resource', (), {'type': 'video', 'file_key': 'video_test_1', 'file_name': 'clip.mp4'})()
        message = type('ChannelInboundFixture', (), {
            'message_id': 'om-video', 'chat_id': 'oc-video', 'chat_type': 'p2p',
            'sender_id': 'ou-video', 'raw_content_type': 'media', 'resources': (resource,),
        })()
        normalized = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))._normalize_message(message)
        self.assertEqual(normalized.message_type, 'media')
        self.assertEqual(normalized.resources, ({'type': 'video', 'file_key': 'video_test_1', 'file_name': 'clip.mp4'},))

    def test_sdk_batched_messages_are_split_back_into_original_messages(self):
        first_resource = type('Resource', (), {'type': 'image', 'file_key': 'img_batch_1', 'file_name': None})()
        video_resource = type('Resource', (), {'type': 'video', 'file_key': 'video_batch_2', 'file_name': 'clip.mp4'})()
        first = type('BatchedSource', (), {
            'message_id': 'om-batch-1', 'chat_id': 'oc-batch', 'chat_type': 'p2p',
            'sender_id': 'ou-batch', 'raw_content_type': 'image', 'resources': (first_resource,),
        })()
        second = type('BatchedSource', (), {
            'message_id': 'om-batch-2', 'chat_id': 'oc-batch', 'chat_type': 'p2p',
            'sender_id': 'ou-batch', 'raw_content_type': 'media', 'resources': (video_resource,),
        })()
        carrier = type('ChannelInboundFixture', (), {
            'message_id': 'om-batch-2', 'chat_id': 'oc-batch', 'chat_type': 'p2p',
            'sender_id': 'ou-batch', 'raw_content_type': 'media',
            'resources': (first_resource, video_resource), 'batched_sources': (first, second),
        })()
        transport = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))
        observed = []
        transport._message_handler = lambda message, _event_id: observed.append(message) or {'status': 'queued'}
        asyncio.run(transport._on_message(carrier))
        self.assertEqual([item.message_id for item in observed], ['om-batch-1', 'om-batch-2'])
        self.assertEqual([item.message_type for item in observed], ['image', 'media'])
        self.assertEqual(observed[0].resources, ({'type': 'image', 'file_key': 'img_batch_1', 'file_name': None},))
        self.assertEqual(observed[1].resources, ({'type': 'video', 'file_key': 'video_batch_2', 'file_name': 'clip.mp4'},))

    def test_channel_media_roundtrip_methods_use_native_sdk(self):
        self.addCleanup(close_lark_channel_global_loop)
        calls = []

        class Channel:
            async def upload_media(self, source, *, kind):
                calls.append(('upload', source.kind, bytes(source.buffer), kind))
                return 'img_roundtrip_1'

            async def send(self, target, content, options):
                calls.append(('send', target, content, options))
                return type('SendResult', (), {'success': True, 'message_id': 'om-image-reply'})()

            async def download_resource(self, file_key, resource_type='image', message_id=None):
                calls.append(('download', file_key, resource_type, message_id))
                return b'png-bytes'

            async def download_resource_to_file(self, file_key, *, resource_type='image', message_id=None, dest_dir, file_name=None):
                calls.append(('download-file', file_key, resource_type, message_id))
                path = Path(dest_dir) / 'downloaded.png'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'png-bytes')
                return path

        transport = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))
        transport._channel = Channel()
        transport._submit = lambda coroutine, **_kwargs: asyncio.run(coroutine)
        key = transport.upload_image(b'png-bytes')
        self.assertEqual(key, 'img_roundtrip_1')
        self.assertEqual(transport.reply_image_key('om-source', 'oc-chat', key, 'uuid-image'), 'om-image-reply')
        with tempfile.TemporaryDirectory() as directory:
            local_file = Path(directory) / 'report.txt'
            local_video = Path(directory) / 'clip.mp4'
            local_file.write_text('report', encoding='utf-8')
            local_video.write_bytes(b'video')
            self.assertEqual(transport.reply_file('om-source', 'oc-chat', local_file, 'uuid-file'), 'om-image-reply')
            self.assertEqual(transport.reply_video('om-source', 'oc-chat', local_video, 'uuid-video'), 'om-image-reply')
        self.assertEqual(transport.download_image(key), b'png-bytes')
        with tempfile.TemporaryDirectory() as directory:
            path = transport.download_image_to_file(key, Path(directory), message_id='om-source')
            self.assertEqual(path.read_bytes(), b'png-bytes')
            file_path = transport.download_file_to_file('file_roundtrip_1', Path(directory), message_id='om-source', file_name='report.pdf')
            self.assertEqual(file_path.read_bytes(), b'png-bytes')
            video_path = transport.download_file_to_file('video_roundtrip_1', Path(directory), message_id='om-source', file_name='clip.mp4', resource_type='video')
            self.assertEqual(video_path.read_bytes(), b'png-bytes')
        self.assertEqual(calls[0], ('upload', 'buffer', b'png-bytes', 'image'))
        send_calls = [call for call in calls if call[0] == 'send']
        self.assertEqual(send_calls[0][3], {'reply_to': 'om-source', 'uuid': 'uuid-image'})
        self.assertEqual(send_calls[1][2]['file']['file_name'], 'report.txt')
        self.assertTrue(str(send_calls[1][2]['file']['source']).endswith('report.txt'))
        self.assertTrue(str(send_calls[2][2]['video']['source']).endswith('clip.mp4'))
        self.assertEqual(calls[-1], ('download-file', 'video_roundtrip_1', 'video', 'om-source'))

    def test_send_result_false_is_safe_error_without_fallback(self):
        result = type('SendResult', (), {'success': False, 'message_id': None, 'error': type('Error', (), {'code': 'permission_denied', 'message': 'denied'})()})()
        with self.assertRaises(Exception) as caught:
            _require_send_success(result)
        self.assertEqual(caught.exception.code, 'FEISHU_API_PERMISSION_DENIED')

    def test_disconnected_submit_closes_rejected_coroutine(self):
        transport = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))

        async def pending():
            return 'never-run'

        coroutine = pending()
        with self.assertRaises(Exception) as caught:
            transport._submit(coroutine)
        self.assertEqual(caught.exception.code, 'FEISHU_API_NOT_CONNECTED')
        self.assertIsNone(coroutine.cr_frame)

    def test_send_card_wraps_payload_and_preserves_target_and_uuid(self):
        settings = FeishuSettings('app', 'secret', (), ())
        calls = []

        class Channel:
            async def send(self, *args):
                calls.append(args)
                return type('SendResult', (), {'success': True, 'message_id': 'card-1'})()

        transport = ChannelFeishuTransport(settings)
        transport._channel = Channel()
        transport._submit = lambda coroutine: asyncio.run(coroutine)
        payload = {'schema': '2.0', 'body': {'elements': []}}
        self.assertEqual(transport.send_card('ou-requester', payload, 'approval:1'), 'card-1')
        self.assertEqual(calls, [('ou-requester', {'card': payload}, {'uuid': 'approval:1'})])

    def test_update_card_uses_official_channel_update_card_with_original_message_id(self):
        settings = FeishuSettings('app', 'secret', (), ())
        calls = []

        class Channel:
            async def update_card(self, message_id, card):
                calls.append((message_id, card))
                return type('SendResult', (), {'success': True, 'message_id': message_id})()

        transport = ChannelFeishuTransport(settings)
        transport._channel = Channel()
        transport._submit = lambda coroutine: asyncio.run(coroutine)
        payload = {'schema': '2.0', 'body': {'elements': []}}
        self.assertEqual(transport.update_card('om-original', payload), 'om-original')
        self.assertEqual(calls, [('om-original', payload)])

    def test_progress_card_update_has_shorter_timeout_than_final_delivery(self):
        class Channel:
            async def update_card(self, _message_id, _card):
                await asyncio.sleep(0.05)
                return type('SendResult', (), {'success': True, 'message_id': 'late'})()

        transport = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()), outbound_timeout=0.01)
        transport._channel = Channel()
        transport._submit = lambda coroutine: asyncio.run(coroutine)
        with self.assertRaises(asyncio.TimeoutError):
            transport.update_card('om-original', {'schema': '2.0'})

    def test_unknown_message_type_and_bot_fail_closed(self):
        message = type('Message', (), {
            'id': 'om-2', 'conversation': type('Conversation', (), {'id': 'oc-2', 'type': 'p2p'})(),
            'sender': type('Identity', (), {'id': 'ou-2', 'type': 'unknown'})(),
            'raw_content_type': 'file', 'body_text': None, 'mentioned_bot': False, 'sender_is_bot': False,
        })()
        normalized = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))._normalize_message(message)
        self.assertEqual(normalized.message_type, 'file')
        self.assertFalse(normalized.is_user)

    def test_nested_card_action_mapping(self):
        event = type('CardActionEvent', (), {
            'message_id': 'om-card', 'chat_id': 'oc-card',
            'operator': type('Operator', (), {'open_id': 'ou-card'})(),
            'action': type('Action', (), {'tag': 'button', 'value': {'action': 'allow_once', 'approval_id': 'approval-1'}})(),
        })()
        transport = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))
        captured = []
        transport._card_handler = lambda action: captured.append(action)
        import asyncio
        asyncio.run(transport._on_card(event))
        self.assertEqual(captured[0].approval_id, 'approval-1')
        self.assertEqual(captured[0].operator_open_id, 'ou-card')
        self.assertEqual(captured[0].raw['tag'], 'button')

    def test_sdk_probe_is_metadata_only(self):
        result = channel_sdk_metadata()
        self.assertIn('installed', result)
        self.assertIn('version', result)

    def test_sdk_bootstrap_precedes_cfr_event_loop(self):
        settings = FeishuSettings('app', 'secret', (), ())
        observed = []
        transport = ChannelFeishuTransport(settings, connect_timeout=1)
        class Events:
            MESSAGE = 'message'
            CARD_ACTION = 'card_action'
            RECONNECTING = 'reconnecting'
            RECONNECTED = 'reconnected'
            ERROR = 'error'
        class Channel:
            is_ready = False
            def __init__(self, **_kwargs):
                observed.append(('construct', transport._loop))
                self.disconnected = False
            def on(self, *_args):
                return None
            async def connect_until_ready(self, **_kwargs):
                self.is_ready = True
            async def disconnect(self):
                self.disconnected = True
        def factory(**kwargs):
            return Channel(**kwargs)
        diagnostic = ChannelSdkLoopDiagnostic('fake.ws.client', False, False, False, 'NOT_REQUIRED')
        with patch('cfr.feishu.transport._require_channel_sdk', return_value=(object(), factory, Events)), patch('cfr.feishu.transport.prepare_channel_sdk_runtime', return_value=diagnostic):
            transport.connect_until_ready(timeout=1)
            self.assertEqual(transport.connection_state, 'ready')
            transport.stop()
        self.assertEqual(observed, [('construct', None)])
        self.assertTrue(transport.wait_until_stopped(1))

    def test_sdk_error_bus_does_not_turn_request_scoped_send_error_into_disconnect(self):
        settings = FeishuSettings('app', 'secret', (), ())
        transport = ChannelFeishuTransport(settings, connect_timeout=1)

        class Events:
            MESSAGE = 'message'
            CARD_ACTION = 'card_action'
            RECONNECTING = 'reconnecting'
            RECONNECTED = 'reconnected'
            ERROR = 'error'

        class Snapshot:
            state = 'connected'

        class Channel:
            is_ready = False

            def __init__(self, **_kwargs):
                self.handlers = {}

            def on(self, event, handler):
                self.handlers[event] = handler

            async def connect_until_ready(self, **_kwargs):
                self.is_ready = True

            def connection_snapshot(self):
                return Snapshot()

            async def disconnect(self):
                return None

        holder = {}

        def factory(**kwargs):
            holder['channel'] = Channel(**kwargs)
            return holder['channel']

        diagnostic = ChannelSdkLoopDiagnostic('fake.ws.client', False, False, False, 'NOT_REQUIRED')
        with patch('cfr.feishu.transport._require_channel_sdk', return_value=(object(), factory, Events)), patch('cfr.feishu.transport.prepare_channel_sdk_runtime', return_value=diagnostic):
            transport.connect_until_ready(timeout=1)
            holder['channel'].handlers[Events.ERROR](RuntimeError('outbound request failed'))
            self.assertTrue(transport.is_running)
            self.assertEqual(transport.connection_state, 'ready')
            self.assertIsNone(transport.error)
            self.assertEqual(transport.last_event_error['type'], 'RuntimeError')
            transport.stop()

    def test_sdk_terminal_connection_snapshot_is_projected_as_not_running(self):
        transport = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))

        class Thread:
            @staticmethod
            def is_alive():
                return True

        class Channel:
            @staticmethod
            def connection_snapshot():
                return type('Snapshot', (), {'state': 'error'})()

        transport._thread = Thread()
        transport._channel = Channel()
        transport._connection_state = 'ready'
        self.assertFalse(transport.is_running)
        self.assertEqual(transport.connection_state, 'failed')

    def test_startup_failure_disconnects_channel_and_classifies_conflict(self):
        settings = FeishuSettings('app', 'secret', (), ())
        transport = ChannelFeishuTransport(settings, connect_timeout=1)
        class Events:
            MESSAGE = 'message'
            CARD_ACTION = 'card_action'
            RECONNECTING = 'reconnecting'
            RECONNECTED = 'reconnected'
            ERROR = 'error'
        class Channel:
            is_ready = False
            def __init__(self, **_kwargs):
                self.disconnected = False
            def on(self, *_args):
                return None
            async def connect_until_ready(self, **_kwargs):
                raise RuntimeError('This event loop is already running')
            async def disconnect(self):
                self.disconnected = True
        holder = {}
        def factory(**kwargs):
            holder['channel'] = Channel(**kwargs)
            return holder['channel']
        diagnostic = ChannelSdkLoopDiagnostic('fake.ws.client', False, False, False, 'NOT_REQUIRED')
        with patch('cfr.feishu.transport._require_channel_sdk', return_value=(object(), factory, Events)), patch('cfr.feishu.transport.prepare_channel_sdk_runtime', return_value=diagnostic):
            transport.start()
            self.assertTrue(transport.wait_until_stopped(1))
        self.assertEqual(transport.error.code, 'FEISHU_SDK_EVENT_LOOP_CONFLICT')
        self.assertTrue(holder['channel'].disconnected)

    def test_stop_is_idempotent_and_disconnect_is_awaited(self):
        settings = FeishuSettings('app', 'secret', (), ())
        transport = ChannelFeishuTransport(settings, connect_timeout=1)
        class Events:
            MESSAGE = 'message'
            CARD_ACTION = 'card_action'
            RECONNECTING = 'reconnecting'
            RECONNECTED = 'reconnected'
            ERROR = 'error'
        class Channel:
            is_ready = False
            def __init__(self, **_kwargs):
                self.disconnect_calls = 0
            def on(self, *_args):
                return None
            async def connect_until_ready(self, **_kwargs):
                self.is_ready = True
            async def disconnect(self):
                self.disconnect_calls += 1
        holder = {}
        def factory(**kwargs):
            holder['channel'] = Channel(**kwargs)
            return holder['channel']
        diagnostic = ChannelSdkLoopDiagnostic('fake.ws.client', False, False, False, 'NOT_REQUIRED')
        with patch('cfr.feishu.transport._require_channel_sdk', return_value=(object(), factory, Events)), patch('cfr.feishu.transport.prepare_channel_sdk_runtime', return_value=diagnostic):
            transport.connect_until_ready(timeout=1)
            transport.stop()
            transport.stop()
        self.assertTrue(transport.wait_until_stopped(1))
        self.assertTrue(transport._disconnect_completed.is_set())
        self.assertEqual(holder['channel'].disconnect_calls, 1)

    def test_terminal_device_flow_preclose_uses_stop_bridge_without_second_close(self):
        settings = FeishuSettings('app', 'secret', (), ())
        transport = ChannelFeishuTransport(settings, disconnect_timeout=1)

        class Safety:
            def __init__(self): self.dispose_calls = 0
            async def dispose(self): self.dispose_calls += 1

        class Ws:
            def __init__(self): self.stop_calls = 0
            def stop(self): self.stop_calls += 1

        class DeviceFlow:
            def __init__(self): self.close_calls = 0
            async def close(self): self.close_calls += 1

        class Channel:
            def __init__(self):
                self._shutdown = __import__('threading').Event()
                self._start_future = None
                self._lifecycle_lock = __import__('threading').RLock()
                self._background_generation = 0
                self._lifecycle_generation = 0
                self._stop_requested = __import__('threading').Event()
                self._ws_client = Ws()
                self._bg_tasks_lock = __import__('threading').RLock()
                self._bg_tasks = set()
                self._bg_lock = __import__('threading').RLock()
                self._bg_loop = None
                self._bg_thread = None
                self._bot_identity_retry_future = None
                self._started = True
                self._ready_flag = True
                self._connection_state = 'connected'
                self._connection_last_disconnected_at = None
                self._ready_event = None
                self._safety = Safety()
                self._device_flow = DeviceFlow()
                self.disconnect_calls = 0
                self.stop_bg_loop_calls = 0

            async def disconnect(self): self.disconnect_calls += 1
            def _stop_keepalive_watchdog(self): pass
            def _cancel_bg_tasks(self): pass
            def _stop_bg_loop(self, *, join_timeout): self.stop_bg_loop_calls += 1

        channel = Channel()
        ws = channel._ws_client
        transport._channel = channel
        preclose = DeviceFlowPreCloseResult(
            True, True, True, True, False, False, True,
            False, False, None, None, 0, None, 'PRECLOSE_OWNED_HTTP', None,
        )
        asyncio.run(transport._await_public_disconnect(preclose))
        self.assertEqual(channel.disconnect_calls, 0)
        self.assertEqual(channel._safety.dispose_calls, 1)
        self.assertEqual(channel._device_flow.close_calls, 0)
        self.assertEqual(ws.stop_calls, 1)
        self.assertEqual(channel.stop_bg_loop_calls, 0)

    def test_stop_before_ready_does_not_leave_transport_thread(self):
        settings = FeishuSettings('app', 'secret', (), ())
        transport = ChannelFeishuTransport(settings, connect_timeout=1)
        class Events:
            MESSAGE = 'message'
            CARD_ACTION = 'card_action'
            RECONNECTING = 'reconnecting'
            RECONNECTED = 'reconnected'
            ERROR = 'error'
        class Channel:
            is_ready = False
            def __init__(self, **_kwargs):
                self.disconnected = False
            def on(self, *_args):
                return None
            async def connect_until_ready(self, **_kwargs):
                self.is_ready = True
            async def disconnect(self):
                self.disconnected = True
        def factory(**kwargs):
            return Channel(**kwargs)
        diagnostic = ChannelSdkLoopDiagnostic('fake.ws.client', False, False, False, 'NOT_REQUIRED')
        with patch('cfr.feishu.transport._require_channel_sdk', return_value=(object(), factory, Events)), patch('cfr.feishu.transport.prepare_channel_sdk_runtime', return_value=diagnostic):
            transport.start()
            transport.stop()
        self.assertTrue(transport.wait_until_stopped(1))
        self.assertFalse(transport.is_running)

    def test_sensitive_feishu_log_fields_are_redacted(self):
        raw = 'wss://example.test/ws?access_key=ak123&ticket=t123 app_secret=secret Authorization: Bearer tok'
        safe = sanitize_feishu_log_text(raw)
        self.assertNotIn('ak123', safe)
        self.assertNotIn('t123', safe)
        self.assertNotIn('app_secret=secret', safe)
        self.assertNotIn('tok', safe)
        self.assertIn('access_key=<redacted>', safe)
        self.assertIn('ticket=<redacted>', safe)

    def test_future_sdk_child_logger_is_redacted_by_root_handler_filter(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            configure_feishu_sdk_logging()
            logging.getLogger('lark_channel.created_after_configuration').warning('ticket=secret-value')
        finally:
            root.removeHandler(handler)
        self.assertIn('ticket=<redacted>', stream.getvalue())
        self.assertNotIn('secret-value', stream.getvalue())

    def test_sdk_log_level_defaults_to_warning(self):
        settings = load_settings({'CFR_FEISHU_APP_ID': 'app', 'CFR_FEISHU_APP_SECRET': 'secret'})
        self.assertEqual(settings.sdk_log_level, 'WARNING')


if __name__ == '__main__':
    unittest.main()
