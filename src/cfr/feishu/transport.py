from __future__ import annotations

from dataclasses import dataclass
import asyncio
import importlib.metadata
import inspect
import json
import logging
import threading
import time
from typing import Any, Callable, Protocol

from cfr.core.models import StructuredError

from .config import FeishuSettings
from .approval_card import parse_v2_card_callback
from .log_sanitize import configure_feishu_sdk_logging, sanitize_feishu_log_text
from .sdk_compat import ChannelSdkLoopDiagnostic, ChannelSdkShutdownCapture, SdkShutdownDiagnostic, capture_channel_sdk_shutdown_targets, classify_channel_error, drain_sdk_tasks, preclose_device_flow_before_public_shutdown, prepare_channel_sdk_runtime, shutdown_channel_without_device_flow_close

LOGGER = logging.getLogger(__name__)


def _require_lark():
    try:
        import lark_oapi as lark
    except ImportError as exc:
        raise StructuredError('FEISHU_SDK_NOT_INSTALLED', 'Install the optional Feishu dependency with: python -m pip install -e ".[feishu]"') from exc
    return lark


def channel_sdk_metadata():
    try:
        version = importlib.metadata.version('lark-channel-sdk')
    except importlib.metadata.PackageNotFoundError:
        return {'installed': False, 'version': None}
    return {'installed': True, 'version': version}


def _require_channel_sdk():
    try:
        import lark_channel
        from lark_channel import FeishuChannel, Events
    except ImportError as exc:
        raise StructuredError('FEISHU_SDK_NOT_INSTALLED', 'Install the optional Feishu dependency with: python -m pip install -e ".[feishu]"') from exc
    return lark_channel, FeishuChannel, Events


_SEND_ERROR_CODES = {
    'format_error': 'FEISHU_API_FORMAT_ERROR',
    'target_revoked': 'FEISHU_API_TARGET_REVOKED',
    'rate_limited': 'FEISHU_API_RATE_LIMITED',
    'permission_denied': 'FEISHU_API_PERMISSION_DENIED',
    'send_timeout': 'FEISHU_API_TIMEOUT',
    'not_connected': 'FEISHU_API_NOT_CONNECTED',
}


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _require_send_success(result) -> str:
    """Convert a Channel SendResult into a CFR response id or safe error."""
    if isinstance(result, str):
        if result:
            return result
        raise StructuredError('FEISHU_API_UNKNOWN', 'Feishu send returned no message id')
    success = _field(result, 'success', None)
    error = _field(result, 'error', None)
    if success is False:
        raw_code = _field(error, 'code', 'unknown') or 'unknown'
        raw_code = getattr(raw_code, 'value', raw_code)
        code = str(raw_code).lower().split('.')[-1]
        ext_code = _field(error, 'ext_code', None) or _field(error, 'extCode', None)
        error_data = _field(error, 'data', None)
        if ext_code is None and isinstance(error_data, dict):
            ext_code = error_data.get('ext_code') or error_data.get('extCode')
        detail = _field(error, 'message', None)
        data = {'provider_code': str(raw_code), 'provider_ext_code': str(ext_code) if ext_code is not None else None}
        if detail:
            data['detail'] = sanitize_feishu_log_text(str(detail))[:120]
        raise StructuredError(_SEND_ERROR_CODES.get(code, 'FEISHU_API_UNKNOWN'), 'Feishu outbound delivery failed', data)
    message_id = _field(result, 'message_id', None)
    if success is True and not message_id:
        raise StructuredError('FEISHU_API_UNKNOWN', 'Feishu send succeeded without a message id')
    if message_id:
        return str(message_id)
    raise StructuredError('FEISHU_API_UNKNOWN', 'Feishu send returned an invalid result')


class FeishuTransport(Protocol):
    def reply_text(self, message_id: str, chat_id: str, text: str, uuid: str) -> str: ...
    def send_text(self, open_id: str, text: str, uuid: str) -> str: ...
    def send_card(self, open_id: str, card: dict[str, Any], uuid: str) -> str: ...
    def update_card(self, message_id: str, card: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class FakeOutboundMessage:
    kind: str
    target: str
    content: Any
    uuid: str
    message_id: str


class FakeFeishuTransport:
    """Deterministic transport used by local acceptance and unit tests."""

    def __init__(self):
        self.messages: list[FakeOutboundMessage] = []
        self.card_updates: list[dict[str, Any]] = []
        self._counter = 0
        self.started = False
        self.stopped = False

    def _send(self, kind, target, content, uuid):
        self._counter += 1
        existing = next((item for item in self.messages if item.uuid == uuid), None)
        if existing:
            return existing.message_id
        message_id = f'fake-out-{self._counter}'
        self.messages.append(FakeOutboundMessage(kind, target, content, uuid, message_id))
        return message_id

    def reply_text(self, message_id, chat_id, text=None, uuid=None):
        if uuid is None:  # legacy three-argument fixture shape
            uuid, text, chat_id = text, chat_id, message_id
        return self._send('reply_text', chat_id, text, uuid)

    def send_text(self, open_id, text, uuid):
        return self._send('send_text', open_id, text, uuid)

    def send_card(self, open_id, card, uuid):
        return self._send('send_card', open_id, card, uuid)

    def update_card(self, message_id, card):
        if not any(item.message_id == message_id and item.kind == 'send_card' for item in self.messages):
            raise StructuredError('FEISHU_API_MESSAGE_NOT_FOUND', 'Original Feishu card message was not found')
        self.card_updates.append({'message_id': str(message_id), 'card': card})
        return str(message_id)

    def latest_card(self, message_id):
        updates = [item for item in self.card_updates if item['message_id'] == str(message_id)]
        if updates:
            return updates[-1]['card']
        for item in self.messages:
            if item.message_id == str(message_id) and item.kind == 'send_card':
                return item.content
        return None

    def start(self, *_args, **_kwargs):
        self.started = True
        return self

    def wait_until_stopped(self, timeout=None):
        return self.stopped

    @property
    def is_running(self):
        return self.started and not self.stopped

    def stop(self):
        self.stopped = True

    disconnect = stop


class LarkFeishuTransport:
    """Legacy OpenAPI adapter retained for non-live compatibility only."""

    def __init__(self, settings: FeishuSettings):
        self.settings = settings
        self._client = None

    @property
    def client(self):
        if self._client is None:
            lark = _require_lark()
            self._client = lark.Client.builder().app_id(self.settings.app_id).app_secret(self.settings.app_secret).log_level(lark.LogLevel.WARNING).build()
        return self._client

    def reply_text(self, message_id, chat_id, text=None, uuid=None):
        if uuid is None:
            uuid, text = text, chat_id
        lark = _require_lark()
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(ReplyMessageRequestBody.builder().content(json.dumps({'text': text}, ensure_ascii=False)).msg_type('text').uuid(uuid).build()).build()
        response = self.client.im.v1.message.reply(request)
        return getattr(getattr(response, 'data', None), 'message_id', None) or message_id

    def send_text(self, open_id, text, uuid):
        _require_lark()
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        body = CreateMessageRequestBody.builder().receive_id(open_id).msg_type('text').content(json.dumps({'text': text}, ensure_ascii=False)).uuid(uuid).build()
        request = CreateMessageRequest.builder().receive_id_type('open_id').request_body(body).build()
        response = self.client.im.v1.message.create(request)
        return getattr(getattr(response, 'data', None), 'message_id', None)

    def send_card(self, open_id, card, uuid):
        _require_lark()
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        body = CreateMessageRequestBody.builder().receive_id(open_id).msg_type('interactive').content(json.dumps(card, ensure_ascii=False)).uuid(uuid).build()
        request = CreateMessageRequest.builder().receive_id_type('open_id').request_body(body).build()
        response = self.client.im.v1.message.create(request)
        return getattr(getattr(response, 'data', None), 'message_id', None)

    def update_card(self, message_id, card):
        lark = _require_lark()
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
        body = PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build()
        request = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        response = self.client.im.v1.message.patch(request)
        return getattr(getattr(response, 'data', None), 'message_id', None) or message_id

    def start(self, *_args, **_kwargs):
        raise StructuredError('FEISHU_LEGACY_TRANSPORT_DISABLED', 'Use ChannelFeishuTransport for the live conversational path')

    def stop(self):
        return None


class ChannelFeishuTransport:
    """Official Channel SDK transport with a persistent asyncio loop thread."""

    backend_name = 'lark-channel-sdk'

    def __init__(self, settings: FeishuSettings, outbound_timeout=15.0, connect_timeout=15.0, disconnect_timeout=5.0):
        self.settings = settings
        self.outbound_timeout = outbound_timeout
        self.connect_timeout = connect_timeout
        self.disconnect_timeout = disconnect_timeout
        self._channel = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._shutdown_requested = None
        self._stop_requested = threading.Event()
        self._stopping = threading.Event()
        self._disconnect_started = False
        self._disconnect_completed = threading.Event()
        self._owned_tasks = set()
        self._error = None
        self._connection_state = 'stopped'
        self._message_handler = None
        self._card_handler = None
        self._sdk_loop_diagnostic = None
        self._sdk_shutdown_compatibility_mode = 'NOT_REQUIRED'
        self._device_flow_close_scheduled = False
        self._device_flow_close_completed = 'NOT_OBSERVABLE'
        self._sdk_ws_tasks_drained = 0
        self._sdk_cache_tasks_drained = 0
        self._async_generators_shutdown = False
        self._shutdown_blocking_issues = []
        self._shutdown_diagnostic = SdkShutdownDiagnostic()
        self._shutdown_capture = None

    @staticmethod
    def _value(obj, *names, default=None):
        for name in names:
            value = getattr(obj, name, None)
            if value is not None:
                return value
        return default

    @classmethod
    def _nested(cls, obj, path, default=None):
        value = obj
        for name in path:
            value = cls._value(value, name, default=None)
            if value is None:
                return default
        return value

    def _normalize_message(self, message):
        from .models import FeishuInboundMessage
        conversation = self._value(message, 'conversation', default=None)
        sender = self._value(message, 'sender', default=None)
        message_id = self._value(message, 'message_id', 'id', default='')
        sender_type = self._value(sender, 'type', 'sender_type', default='unknown') or 'unknown'
        sender_is_bot = bool(self._value(message, 'sender_is_bot', default=False) or sender_type in {'bot', 'app'})
        raw_content_type = self._value(message, 'raw_content_type', default='unknown') or 'unknown'
        content_text = self._value(message, 'content_text', default=None)
        if content_text is None:
            content_text = self._value(message, 'body_text', default=None)
        normalized_text = str(content_text) if content_text is not None else None
        chat_type = self._value(message, 'chat_type', default=None)
        if not chat_type:
            chat_type = self._value(conversation, 'chat_type', 'type', default=None)
        chat_type = str(chat_type or 'unknown').strip().lower()
        chat_id = self._value(message, 'chat_id', default=None)
        if not chat_id:
            chat_id = self._value(conversation, 'chat_id', 'id', default='')
        sender_open_id = self._value(message, 'sender_id', default=None)
        if not sender_open_id:
            sender_open_id = self._value(sender, 'open_id', 'id', default='')
        return FeishuInboundMessage(
            event_id=self._value(message, 'event_id', default=None),
            message_id=str(message_id),
            chat_id=str(chat_id or ''),
            chat_type=chat_type,
            sender_open_id=str(sender_open_id or ''),
            sender_type=str(sender_type),
            message_type=str(raw_content_type),
            text=normalized_text if raw_content_type == 'text' else None,
            root_id=self._value(message, 'root_id', default=None),
            parent_id=self._value(message, 'parent_id', default=None),
            create_time=self._value(message, 'create_time', default=None),
            mentions=tuple(self._value(message, 'mentions', default=()) or ()),
            mentioned_bot=bool(self._value(message, 'mentioned_bot', default=False)),
            body_text=normalized_text,
            sender_is_bot=sender_is_bot,
            reply_to_message_id=self._value(message, 'reply_to_message_id', default=None),
        )

    @staticmethod
    async def _invoke_handler(handler, *args):
        if inspect.iscoroutinefunction(handler):
            return await handler(*args)
        result = await asyncio.to_thread(handler, *args)
        return await result if inspect.isawaitable(result) else result

    async def _on_message(self, message):
        if self._stopping.is_set():
            return None
        normalized = self._normalize_message(message)
        if not self._message_handler:
            return None
        return await self._invoke_handler(self._message_handler, normalized, normalized.event_id)

    async def _on_card(self, event):
        if self._stopping.is_set():
            return None
        if not self._card_handler:
            return None
        from .models import FeishuCardAction
        value = self._nested(event, ('action', 'value'), default={})
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise StructuredError('FEISHU_CARD_ACTION_INVALID', 'Card action value must be valid JSON') from exc
        operator_open_id = self._nested(event, ('operator', 'open_id'), default=None)
        action, approval_id, operator_open_id = parse_v2_card_callback(
            value,
            operator_open_id,
            self._nested(event, ('action', 'tag'), default=None),
        )
        card = FeishuCardAction(
            action=action,
            approval_id=approval_id,
            operator_open_id=str(operator_open_id),
            raw={
                'message_id': self._value(event, 'message_id', 'id', default=None),
                'chat_id': self._value(event, 'chat_id', default=None),
                'tag': self._nested(event, ('action', 'tag'), default=None),
            },
        )
        return await self._invoke_handler(self._card_handler, card)

    def _set_state(self, state):
        self._connection_state = state

    def _load_channel_sync(self):
        lark_channel, FeishuChannel, Events = _require_channel_sdk()
        self._sdk_loop_diagnostic = prepare_channel_sdk_runtime()
        configure_feishu_sdk_logging(self.settings.sdk_log_level)
        channel_kwargs = {'app_id': self.settings.app_id, 'app_secret': self.settings.app_secret}
        log_level_type = getattr(lark_channel, 'LogLevel', None)
        log_level = getattr(log_level_type, self.settings.sdk_log_level, None) if log_level_type else None
        if log_level is not None:
            channel_kwargs['log_level'] = log_level
        channel = FeishuChannel(**channel_kwargs)
        channel.on(Events.MESSAGE, self._on_message)
        channel.on(Events.CARD_ACTION, self._on_card)
        channel.on(Events.RECONNECTING, lambda *_: self._set_state('reconnecting'))
        channel.on(Events.RECONNECTED, lambda *_: self._set_state('ready'))
        channel.on(Events.ERROR, lambda error=None, *_: self._set_error(error or RuntimeError('channel error')))
        self._channel = channel

    async def _run_channel(self):
        if self._channel is None:
            raise StructuredError('FEISHU_CHANNEL_NOT_INITIALIZED', 'Feishu Channel SDK was not bootstrapped')
        self._set_state('starting')
        if self._stop_requested.is_set():
            return
        await self._channel.connect_until_ready(timeout=self.connect_timeout)
        if not bool(getattr(self._channel, 'is_ready', False)):
            raise StructuredError('FEISHU_CHANNEL_NOT_READY', 'Feishu Channel SDK did not report ready')
        self._set_state('ready')
        self._ready.set()
        await self._shutdown_requested.wait()

    async def _await_public_disconnect(self, device_flow_preclose=None):
        """Run SDK shutdown while preserving a terminal DeviceFlow pre-close."""
        disconnect = getattr(self._channel, 'disconnect', None)
        if disconnect is None:
            disconnect = getattr(self._channel, 'stop', None)
        if disconnect is None:
            return
        if device_flow_preclose is not None and device_flow_preclose.completed:
            # FeishuChannel.disconnect() calls the SDK's synchronous stop(),
            # which unconditionally creates a second DeviceFlow.close()
            # coroutine.  Dispose the safety pipeline first, then use the CFR
            # compatibility boundary for the rest of the SDK stop lifecycle.
            safety = getattr(self._channel, '_safety', None)
            dispose = getattr(safety, 'dispose', None) if safety is not None else None
            if callable(dispose):
                await asyncio.wait_for(dispose(), timeout=self.disconnect_timeout)
            await shutdown_channel_without_device_flow_close(
                self._channel,
                join_timeout=min(5.0, max(2.0, self.disconnect_timeout)),
            )
            return
        result = disconnect()
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=self.disconnect_timeout)

    async def _shutdown_channel_sync(self):
        """Serialize SDK shutdown while the CFR owner loop is still alive."""
        if not self._disconnect_started:
            self._disconnect_started = True
            try:
                device_flow_preclose = None
                try:
                    self._shutdown_capture = capture_channel_sdk_shutdown_targets(self._channel)
                except Exception as exc:
                    self._shutdown_capture = ChannelSdkShutdownCapture(captured_at=time.monotonic())
                    self._shutdown_blocking_issues.append('PRE_SHUTDOWN_CAPTURE_FAILED')
                    LOGGER.warning('Feishu SDK pre-shutdown capture failed: %s', sanitize_feishu_log_text(exc))
                if self._shutdown_capture is not None:
                    device_flow_preclose = preclose_device_flow_before_public_shutdown(
                        self._shutdown_capture,
                        timeout=min(10.0, max(8.0, self.disconnect_timeout)),
                    )
                if self._channel is not None:
                    await self._await_public_disconnect(device_flow_preclose)
            except asyncio.TimeoutError as exc:
                self._error = StructuredError('FEISHU_CHANNEL_SHUTDOWN_TIMEOUT', 'Feishu Channel disconnect exceeded its timeout')
                LOGGER.warning(sanitize_feishu_log_text('%s: %s'), self._error.code, exc)
            except Exception as exc:
                LOGGER.debug('Feishu Channel disconnect failed during cleanup: %s', sanitize_feishu_log_text(exc))
            finally:
                diagnostic = drain_sdk_tasks(self._channel, timeout=self.disconnect_timeout, capture=self._shutdown_capture, device_flow_preclose=device_flow_preclose)
                self._shutdown_diagnostic = diagnostic
                self._sdk_shutdown_compatibility_mode = diagnostic.compatibility_mode
                self._device_flow_close_scheduled = diagnostic.device_flow_close_scheduled
                self._device_flow_close_completed = diagnostic.device_flow_close_completed
                self._sdk_ws_tasks_drained = diagnostic.sdk_ws_tasks_drained
                self._sdk_cache_tasks_drained = diagnostic.sdk_cache_tasks_drained
                self._async_generators_shutdown = self._async_generators_shutdown or diagnostic.async_generators_shutdown
                self._shutdown_blocking_issues = list(diagnostic.blocking_issues)
                self._disconnect_completed.set()

    def _set_error(self, error):
        conflict_code = classify_channel_error(error)
        if conflict_code:
            self._error = StructuredError(conflict_code, 'Feishu Channel SDK event-loop ownership conflict')
        elif isinstance(error, StructuredError):
            self._error = error
        else:
            self._error = error
        self._set_state('failed')
        self._ready.set()

    def _run_loop(self):
        try:
            # Import and construct before creating/running CFR's asyncio loop.
            self._load_channel_sync()
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._shutdown_requested = asyncio.Event()
            if self._stop_requested.is_set():
                self._shutdown_requested.set()
            self._loop.run_until_complete(self._run_channel())
        except BaseException as exc:
            if not self._stopping.is_set():
                self._set_error(exc)
        finally:
            if self._loop is not None and not self._loop.is_closed():
                try:
                    self._loop.run_until_complete(self._shutdown_channel_sync())
                except Exception as exc:
                    self._shutdown_blocking_issues.append('FEISHU_CHANNEL_SHUTDOWN_SERIALIZATION_FAILED')
                    LOGGER.debug('Feishu Channel serialized shutdown failed: %s', sanitize_feishu_log_text(exc))
            if self._loop is not None and not self._loop.is_closed():
                try:
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                    self._async_generators_shutdown = True
                except Exception as exc:
                    LOGGER.debug('Feishu loop async-generator cleanup failed: %s', sanitize_feishu_log_text(exc))
                finally:
                    asyncio.set_event_loop(None)
                    self._loop.close()
            if self._connection_state != 'failed':
                self._set_state('stopped')
            self._stopped.set()

    def start(self, message_handler: Callable | None = None, card_handler: Callable | None = None):
        if self._thread and self._thread.is_alive():
            return self
        self._message_handler = message_handler
        self._card_handler = card_handler
        self._ready.clear()
        self._stopped.clear()
        self._stop_requested.clear()
        self._stopping.clear()
        self._disconnect_started = False
        self._disconnect_completed.clear()
        self._owned_tasks.clear()
        self._sdk_shutdown_compatibility_mode = 'NOT_REQUIRED'
        self._device_flow_close_scheduled = False
        self._device_flow_close_completed = 'NOT_OBSERVABLE'
        self._sdk_ws_tasks_drained = 0
        self._sdk_cache_tasks_drained = 0
        self._async_generators_shutdown = False
        self._shutdown_blocking_issues = []
        self._shutdown_diagnostic = SdkShutdownDiagnostic()
        self._shutdown_capture = None
        self._error = None
        self._set_state('starting')
        self._thread = threading.Thread(target=self._run_loop, name='cfr-feishu-channel', daemon=True)
        self._thread.start()
        return self

    def connect_until_ready(self, message_handler: Callable | None = None, card_handler: Callable | None = None, timeout=None):
        if self._thread is None or not self._thread.is_alive():
            self.start(message_handler or (lambda *_: None), card_handler)
        elif message_handler is not None:
            self._message_handler = message_handler
            self._card_handler = card_handler
        if not self._ready.wait(timeout if timeout is not None else self.connect_timeout):
            raise StructuredError('FEISHU_CHANNEL_NOT_READY', 'Feishu Channel SDK did not become ready before timeout')
        if self._error:
            if isinstance(self._error, StructuredError):
                raise self._error
            raise StructuredError('FEISHU_CHANNEL_UNKNOWN', 'Feishu Channel SDK connection failed') from self._error
        return self

    @property
    def is_running(self):
        return bool(self._thread and self._thread.is_alive()) and self._connection_state not in {'stopped', 'failed'}

    @property
    def connection_state(self):
        return self._connection_state

    @property
    def error(self):
        return self._error

    def wait_until_stopped(self, timeout=None):
        self._stopped.wait(timeout)
        return self._stopped.is_set()

    @property
    def shutdown_diagnostic(self) -> SdkShutdownDiagnostic:
        """Read-only SDK shutdown ownership evidence for probes and gates."""
        return self._shutdown_diagnostic

    def _submit(self, coroutine):
        if not self._loop or self._loop.is_closed() or not self.is_running or self._stopping.is_set():
            raise StructuredError('FEISHU_API_NOT_CONNECTED', 'Feishu Channel SDK is not connected')
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=self.outbound_timeout)
        except StructuredError:
            future.cancel()
            raise
        except Exception as exc:
            future.cancel()
            raise StructuredError('FEISHU_API_UNKNOWN', 'Feishu outbound delivery failed') from exc

    async def _send(self, target, content, options):
        return await self._channel.send(target, content, options)

    def reply_text(self, message_id, chat_id, text, uuid):
        result = self._submit(self._send(chat_id, {'text': text}, {'reply_to': message_id, 'uuid': uuid}))
        return _require_send_success(result)

    def send_text(self, open_id, text, uuid):
        return _require_send_success(self._submit(self._send(open_id, {'text': text}, {'uuid': uuid})))

    def send_card(self, open_id, card, uuid):
        return _require_send_success(self._submit(self._send(open_id, {'card': card}, {'uuid': uuid})))

    async def _update_card(self, message_id, card):
        return await self._channel.update_card(message_id, card)

    def update_card(self, message_id, card):
        return _require_send_success(self._submit(self._update_card(message_id, card)))

    def stop(self):
        self._stopping.set()
        self._stop_requested.set()
        if self._loop and not self._loop.is_closed() and self._shutdown_requested is not None:
            self._loop.call_soon_threadsafe(self._shutdown_requested.set)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.disconnect_timeout + 5.0)
        if self._thread and self._thread.is_alive():
            self._error = StructuredError('FEISHU_CHANNEL_SHUTDOWN_TIMEOUT', 'Feishu Channel transport thread did not exit before timeout')
            self._set_state('failed')
            return
        self._stopped.set()
        if self._connection_state != 'failed':
            self._set_state('stopped')

    disconnect = stop
