from __future__ import annotations

from datetime import datetime
import asyncio
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time
import uuid

from cfr.core.models import ConversationResult, StructuredError, TurnResult

from .approvals import ApprovalBridge
from .commands import CommandParser, HELP_TEXT, ParsedCommand
from .config import FeishuSettings
from .models import FeishuExecutionContext, FeishuInboundMessage
from .progress import FeishuTurnProgress
from .replies import FeishuReplyClient
from .security import validate_workspace
from .store import FeishuStore


LOGGER = logging.getLogger(__name__)


class FeishuDaemon:
    """Long-running local owner of one Feishu WS client and one CodexAdapter."""

    def __init__(self, settings: FeishuSettings, transport, adapter=None, binding_store=None, instance_id=None):
        self.settings = settings
        self.transport = transport
        self.store = FeishuStore(settings.database)
        self.binding_store = binding_store
        if self.binding_store is None:
            from cfr.storage.db import BindingStore
            self.binding_store = BindingStore(settings.database)
        if adapter is None:
            from cfr.codex.binding import CodexAdapter
            adapter = CodexAdapter(store=self.binding_store, codex_home=None)
        self.adapter = adapter
        self.instance_id = instance_id or uuid.uuid4().hex
        self.lease_key = f'feishu:{settings.app_namespace}'
        self.queue: queue.Queue[str] = queue.Queue()
        self.stop_event = threading.Event()
        self._workers: list[threading.Thread] = []
        self._heartbeat_thread = None
        self._started = False
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.RLock()
        self._catalog_lock = threading.Lock()
        self._catalog_warm_started = False
        self._model_catalog = None
        self._catalog_error = None
        self.parser = CommandParser()
        self.replies = FeishuReplyClient(transport, self.store)
        self.approvals = ApprovalBridge(self.store, self.replies, settings)

    def start(self, background_workers=True):
        self.settings.validate_execution()
        self.store.acquire_daemon_lease(self.lease_key, self.instance_id, os.getpid(), ttl=30)
        queued = self.store.recover_on_startup()
        for row in queued:
            self.enqueue(row['message_id'])
        self._started = True
        self.stop_event.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat, name='cfr-feishu-daemon-heartbeat', daemon=True)
        self._heartbeat_thread.start()
        self._start_catalog_warm()
        if background_workers:
            for index in range(self.settings.worker_concurrency):
                worker = threading.Thread(target=self._worker, name=f'cfr-feishu-worker-{index}', daemon=True)
                self._workers.append(worker)
                worker.start()
        return self

    def _heartbeat(self):
        while not self.stop_event.wait(10):
            if not self.store.heartbeat_daemon_lease(self.lease_key, self.instance_id, ttl=30):
                self.stop_event.set()
                break

    def enqueue(self, message_id):
        self.queue.put(message_id)

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.process_one()
            finally:
                self.queue.task_done()

    def process_pending(self, limit=None):
        processed = 0
        while limit is None or processed < limit:
            record = self.process_one()
            if record is None:
                break
            processed += 1
        return processed

    def process_one(self):
        record = self.store.claim_next()
        if record is None:
            return None
        message = FeishuInboundMessage(record.event_id, record.message_id, record.chat_id, record.chat_type, record.sender_open_id, 'user', record.message_type, record.text_content)
        try:
            response_ids = self._dispatch(message, record)
            self.store.mark_completed(message.message_id, response_ids[-1] if response_ids else None)
        except StructuredError as exc:
            self.store.mark_failed(message.message_id, exc.code, exc.message)
            self._safe_error_reply(message, exc)
        except Exception as exc:
            self.store.mark_failed(message.message_id, type(exc).__name__, str(exc))
            self._safe_error_reply(message, StructuredError('FEISHU_JOB_FAILED', 'CFR could not complete this request'))
        return record

    def _lock_for(self, chat_id):
        with self._locks_guard:
            return self._locks.setdefault(chat_id, threading.Lock())

    def _dispatch(self, message: FeishuInboundMessage, record=None):
        command = self.parser.parse(message.text)
        if command and command.name in {'stop', 'steer', 'redirect'}:
            return self._handle_command(message, command)
        lock = self._lock_for(message.chat_id)
        with lock:
            if command:
                return self._handle_command(message, command)
            return self._handle_prompt(message, record)

    def handle_control_command(self, message: FeishuInboundMessage):
        """Run a slash command before inbox persistence or Codex admission."""
        command = self.parser.parse(message.text)
        if command is None:
            return None
        try:
            return self._handle_command(message, command)
        except StructuredError as error:
            return self._safe_error_reply(message, error)

    def _bound_session(self, message):
        session = self.store.get_session(message.chat_id)
        if not session or not session.thread_id:
            raise StructuredError('FEISHU_NO_ACTIVE_SESSION', '当前聊天没有已绑定的 CFR 线程。')
        return session

    @staticmethod
    def _resolve(items, value, keys):
        value = (value or '').strip()
        if value.isdigit():
            index = int(value) - 1
            if 0 <= index < len(items):
                return items[index]
        return next((item for item in items if value in [str(item.get(key) or '') if isinstance(item, dict) else str(getattr(item, key, '') or '') for key in keys]), None)

    @staticmethod
    def _current_thread_model(catalog, identity):
        """Resolve the native thread model identity against the native catalog."""
        identity = (identity or '').strip()
        if not identity:
            return None
        matches = [
            item for item in catalog
            if isinstance(item, dict) and identity in {
                str(item.get(field) or '').strip()
                for field in ('model', 'id', 'display_name')
            } - {''}
        ]
        return matches[0] if len(matches) == 1 else None

    def _start_catalog_warm(self):
        lock = getattr(self, '_catalog_lock', None)
        if lock is None:
            lock = self._catalog_lock = threading.Lock()
        with lock:
            if getattr(self, '_catalog_warm_started', False):
                return
            self._catalog_warm_started = True
        threading.Thread(target=self._warm_catalog, name='cfr-feishu-catalog-warm', daemon=True).start()

    def _warm_catalog(self):
        from cfr.control.codex_catalog import models
        catalog = models()
        if catalog.get('available'):
            self._model_catalog = catalog['data']
            self._catalog_error = None
        else:
            self._catalog_error = catalog.get('error_code') or 'CODEX_MODEL_LIST_UNAVAILABLE'

    def _catalog(self):
        catalog = getattr(self, '_model_catalog', None)
        if catalog is not None:
            return catalog
        self._start_catalog_warm()
        if getattr(self, '_catalog_error', None):
            raise StructuredError('CODEX_MODEL_LIST_UNAVAILABLE', '当前安装的 Codex 模型目录不可用。')
        raise StructuredError('CODEX_MODEL_LIST_SYNCING', '正在后台同步 Codex 模型目录，请稍后重试。')

    def _thread_settings(self, message):
        session = self._bound_session(message)
        return session, self._run_async(self.adapter.read_thread_settings(session.thread_id))

    def _defaults(self):
        from cfr.control.codex_settings import read
        state = read()
        values = state.get('codex_model_defaults') if state.get('available') else None
        if not values:
            raise StructuredError('CONTROL_CODEX_CONFIG_READ_UNAVAILABLE', '新线程默认设置不可用。')
        return values

    def _write_default(self, **changes):
        from cfr.control.codex_settings import CodexSettingsError, write
        defaults = self._defaults()
        values = {
            'model': defaults['model']['effective_value'],
            'reasoning_effort': defaults['reasoning_effort']['effective_value'],
            'service_tier': defaults['service_tier']['effective_value'],
        }
        values.update(changes)
        try:
            write(values)
        except CodexSettingsError as error:
            raise StructuredError(error.error_code, error.message) from error

    def _settings_text(self, settings, confirmed=True):
        if not confirmed:
            return '当前线程设置：尚未同步'
        return '\n'.join([
            f"当前线程模型：{settings.get('model') or '未提供'}",
            f"当前推理强度：{settings.get('effort') or '未提供'}",
            f"当前服务层级：{settings.get('service_tier') or '默认'}",
        ])

    def _handle_command(self, message, command):
        if command.name == 'help':
            return self.replies.reply_text(message.message_id, HELP_TEXT, 'status', chat_id=message.chat_id)
        if command.name == 'whoami':
            return self.replies.reply_text(message.message_id, f'open_id: {message.sender_open_id}', 'status', chat_id=message.chat_id)
        if command.name == 'workspace':
            if command.argument:
                workspace = validate_workspace(command.argument, self.settings)
                if self.store.get_session(message.chat_id):
                    self.store.switch_to_pending_session(message.chat_id, message.chat_type, message.sender_open_id, workspace)
                    return self.replies.reply_text(message.message_id, f'已切换到新工作区：{workspace.name}。请发送第一个任务。', 'accepted', chat_id=message.chat_id)
                return self._handle_command(message, ParsedCommand('new', command.argument))
            return self._workspace_list(message)
        if command.name == 'workspaces':
            return self._workspace_list(message)
        if command.name == 'new':
            if not command.argument:
                raise StructuredError('FEISHU_WORKSPACE_REQUIRED', 'Usage: /cfr new <absolute-workspace-path>')
            if self.store.get_session(message.chat_id):
                raise StructuredError('FEISHU_SESSION_ALREADY_EXISTS', 'Use /cfr unbind before /cfr new')
            workspace = validate_workspace(command.argument, self.settings)
            self.store.create_pending_session(message.chat_id, message.chat_type, message.sender_open_id, workspace)
            return self.replies.reply_text(message.message_id, f'CFR workspace ready: {workspace.name}. Send the first task.', 'accepted', chat_id=message.chat_id)
        if command.name == 'use':
            if not command.argument:
                raise StructuredError('FEISHU_THREAD_REQUIRED', '用法：/session <会话编号或线程 ID>')
            bindings = self.binding_store.list_bindings()[:10]
            binding = self._resolve(bindings, command.argument, ('thread_id',))
            if not binding:
                raise StructuredError('BINDING_NOT_FOUND', '未找到该 CFR 会话。')
            validate_workspace(binding.cwd, self.settings)
            if self.store.get_session(message.chat_id):
                raise StructuredError('FEISHU_SESSION_ALREADY_EXISTS', 'This chat is already bound')
            self.store.create_pending_session(message.chat_id, message.chat_type, message.sender_open_id, binding.cwd)
            self.store.bind_session(message.chat_id, binding.thread_id)
            return self.replies.reply_text(message.message_id, f'已绑定 CFR 线程：{binding.thread_name or binding.thread_id[-8:]}', 'status', chat_id=message.chat_id)
        if command.name == 'unbind':
            self.store.unbind_session(message.chat_id)
            return self.replies.reply_text(message.message_id, 'Feishu chat unbound. The CFR thread was not deleted.', 'status', chat_id=message.chat_id)
        if command.name == 'status':
            return self.replies.reply_text(message.message_id, self._status_text(message.chat_id), 'status', chat_id=message.chat_id)
        if command.name == 'list':
            records = self.binding_store.list_bindings()[:10]
            text = '可用会话：\n' + ('\n'.join(f'{index}. {item.thread_name or item.thread_id[-8:]} | {item.cwd.name} | {item.thread_id[-8:]}' for index, item in enumerate(records, 1)) or '无')
            return self.replies.reply_text(message.message_id, text, 'status', chat_id=message.chat_id)
        if command.name == 'doctor':
            from cfr.doctor import run_doctor
            result = run_doctor(project_root=Path.cwd(), database=self.settings.database, live=False)
            return self.replies.reply_text(message.message_id, f"诊断：{result.get('Verdict', 'UNKNOWN')}", 'status', chat_id=message.chat_id)
        if command.name == 'models':
            return self.replies.reply_text(message.message_id, '可用模型：\n' + ('\n'.join(f'{index}. {item.get("display_name") or item.get("model")}' for index, item in enumerate(self._catalog(), 1)) or '无'), 'status', chat_id=message.chat_id)
        if command.name == 'model':
            if command.argument and command.argument.lower().startswith('default '):
                model = self._resolve(self._catalog(), command.argument[8:].strip(), ('model', 'id', 'display_name'))
                if not model:
                    raise StructuredError('CONTROL_INVALID_CODEX_SETTING', '指定模型不在当前 Codex 模型列表中。')
                self._write_default(model=model['model'])
                return self.replies.reply_text(message.message_id, f'新建线程默认模型已设置为：{model["model"]}。现有线程不会改变。', 'status', chat_id=message.chat_id)
            if not command.argument:
                started = time.monotonic()
                try:
                    _, state = self._thread_settings(message)
                    return self.replies.reply_text(message.message_id, self._settings_text(state['settings'], state.get('confirmed', True)) + '\n发送 /models 查看全部模型。', 'status', chat_id=message.chat_id)
                finally:
                    LOGGER.info('FEISHU_COMMAND_MODEL_READ_LATENCY elapsed_ms=%s', int((time.monotonic() - started) * 1000))
            session, _ = self._thread_settings(message)
            model = self._resolve(self._catalog(), command.argument, ('model', 'id', 'display_name'))
            if not model:
                raise StructuredError('CONTROL_INVALID_CODEX_SETTING', '指定模型不在当前 Codex 模型列表中。')
            self._run_async(self.adapter.update_thread_settings(session.thread_id, model=model['model']))
            return self.replies.reply_text(message.message_id, f'当前线程模型已切换为：{model["model"]}', 'status', chat_id=message.chat_id)
        if command.name in {'reasoning', 'effort'}:
            defaults = self._defaults() if command.argument and command.argument.lower().startswith('default ') else None
            if not command.argument:
                started = time.monotonic()
                try:
                    _, state = self._thread_settings(message)
                    if not state.get('confirmed', True):
                        return self.replies.reply_text(message.message_id, '当前推理强度：尚未同步', 'status', chat_id=message.chat_id)
                    current_effort = state['settings'].get('effort') or '未提供'
                    catalog = getattr(self, '_model_catalog', None)
                    if catalog is None:
                        self._start_catalog_warm()
                        suffix = '模型目录暂不可用，请稍后重试 /reasoning。' if getattr(self, '_catalog_error', None) else '正在同步 Codex 模型目录，请稍后重试 /reasoning。'
                        return self.replies.reply_text(message.message_id, f'当前推理强度：{current_effort}\n{suffix}', 'status', chat_id=message.chat_id)
                    model = self._current_thread_model(catalog, state['settings'].get('model'))
                    if not model:
                        raise StructuredError('CONTROL_THREAD_MODEL_UNAVAILABLE', '当前线程模型不在已安装的 Codex 模型列表中。')
                    efforts = model.get('supported_reasoning_efforts', [])
                    text = '\n'.join([f'当前推理强度：{current_effort}', '当前模型支持：'] + [f'{index}. {item["reasoning_effort"]}' for index, item in enumerate(efforts, 1)])
                    return self.replies.reply_text(message.message_id, text, 'status', chat_id=message.chat_id)
                finally:
                    LOGGER.info('FEISHU_COMMAND_REASONING_READ_LATENCY elapsed_ms=%s', int((time.monotonic() - started) * 1000))
            session, state = self._thread_settings(message) if defaults is None else (None, {'settings': {'model': defaults['model']['effective_value'], 'effort': defaults['reasoning_effort']['effective_value']}})
            model = self._current_thread_model(self._catalog(), state['settings'].get('model'))
            if not model:
                raise StructuredError('CONTROL_THREAD_MODEL_UNAVAILABLE', '当前线程模型不在已安装的 Codex 模型列表中。')
            efforts = model.get('supported_reasoning_efforts', [])
            if command.argument and command.argument.lower().startswith('default '):
                effort = self._resolve(efforts, command.argument[8:].strip(), ('reasoning_effort',))
                if not effort:
                    raise StructuredError('CONTROL_INVALID_CODEX_SETTING', '该模型不支持指定推理强度。')
                self._write_default(reasoning_effort=effort['reasoning_effort'])
                return self.replies.reply_text(message.message_id, f'新建线程默认推理强度已设置为：{effort["reasoning_effort"]}。现有线程不会改变。', 'status', chat_id=message.chat_id)
            effort = self._resolve(efforts, command.argument, ('reasoning_effort',))
            if not effort:
                raise StructuredError('CONTROL_INVALID_CODEX_SETTING', '该模型不支持指定推理强度。')
            self._run_async(self.adapter.update_thread_settings(session.thread_id, effort=effort['reasoning_effort']))
            return self.replies.reply_text(message.message_id, f'当前线程推理强度已设置为：{effort["reasoning_effort"]}', 'status', chat_id=message.chat_id)
        if command.name in {'tiers', 'tier'}:
            session, state = self._thread_settings(message)
            model = self._current_thread_model(self._catalog(), state['settings'].get('model'))
            if not model:
                raise StructuredError('CONTROL_THREAD_MODEL_UNAVAILABLE', '当前线程模型不在已安装的 Codex 模型列表中。')
            tiers = model.get('service_tiers', [])
            if command.name == 'tiers' or not command.argument:
                available = '可用服务层级：' if tiers else '当前模型没有额外可切换服务层级。'
                text = '\n'.join([f'当前服务层级：{state["settings"].get("service_tier") or "默认"}', available] + [f'{index}. {item["id"]}' for index, item in enumerate(tiers, 1)])
                return self.replies.reply_text(message.message_id, text, 'status', chat_id=message.chat_id)
            if command.argument.lower() == 'default':
                self._run_async(self.adapter.update_thread_settings(session.thread_id, service_tier=None))
                return self.replies.reply_text(message.message_id, '当前线程服务层级已恢复默认。', 'status', chat_id=message.chat_id)
            tier = self._resolve(tiers, command.argument, ('id', 'name'))
            if not tier:
                raise StructuredError('CONTROL_INVALID_CODEX_SETTING', '该模型不支持指定服务层级。')
            self._run_async(self.adapter.update_thread_settings(session.thread_id, service_tier=tier['id']))
            return self.replies.reply_text(message.message_id, f'当前线程服务层级已设置为：{tier["id"]}', 'status', chat_id=message.chat_id)
        if command.name in {'modes', 'mode'}:
            from cfr.control.codex_catalog import collaboration_modes
            catalog = collaboration_modes()
            if not catalog.get('available'):
                raise StructuredError('CODEX_COLLABORATION_MODE_LIST_UNAVAILABLE', '当前安装的 Codex 不提供协作模式列表。')
            modes = catalog['data']
            if command.name == 'modes' or not command.argument:
                return self.replies.reply_text(message.message_id, '可用 Codex 协作模式：\n' + ('\n'.join(f'{index}. {item["name"] or item["mode"]}' for index, item in enumerate(modes, 1)) or '无'), 'status', chat_id=message.chat_id)
            requested = command.argument.lower()
            if requested == 'work':
                return self.replies.reply_text(message.message_id, 'Work 不是当前 CFR 已接入的 Codex 协作模式。ChatGPT Work 执行目标尚未接入 CFR。', 'status', chat_id=message.chat_id)
            mode = self._resolve(modes, 'default' if requested == 'code' else command.argument, ('mode', 'name'))
            if not mode:
                return self.replies.reply_text(message.message_id, '当前安装的 Codex 未提供该协作模式。发送 /modes 查看可用模式。', 'status', chat_id=message.chat_id)
            session, state = self._thread_settings(message)
            current_model = state['settings'].get('model')
            if not current_model:
                raise StructuredError('CONTROL_THREAD_MODEL_UNAVAILABLE', '无法读取当前线程模型，不能安全切换协作模式。')
            mode_settings = {'model': mode.get('model') or current_model}
            if mode.get('reasoning_effort'):
                mode_settings['reasoning_effort'] = mode['reasoning_effort']
            self._run_async(self.adapter.update_thread_settings(session.thread_id, collaboration_mode={'mode': mode['mode'], 'settings': mode_settings}))
            return self.replies.reply_text(message.message_id, f'已切换到 Codex 协作模式：{mode["name"] or mode["mode"]}', 'status', chat_id=message.chat_id)
        if command.name == 'steer':
            if not command.argument:
                raise StructuredError('FEISHU_STEER_REQUIRED', '用法：/steer <追加指令>')
            session = self._bound_session(message)
            result = self._run_async(self.adapter.steer(session.thread_id, command.argument))
            if result is None:
                return self.replies.reply_text(message.message_id, '当前没有可追加指令的活动任务。', 'status', chat_id=message.chat_id)
            return self.replies.reply_text(message.message_id, '追加指令已注入当前活动 Turn（软转向）。\nCodex 会在后续可处理新输入的执行阶段读取该指令。\n当前已经生成或正在生成的内容可能继续一段。\n如需立即停止当前任务，请使用 /stop。', 'status', chat_id=message.chat_id)
        if command.name == 'redirect':
            if not command.argument:
                raise StructuredError('FEISHU_REDIRECT_REQUIRED', '用法：/redirect <新方向>')
            session = self._bound_session(message)
            if self.adapter.registry.get(session.thread_id) is None:
                return self.replies.reply_text(message.message_id, '当前没有可重定向的活动任务。\n如需开始新任务，请直接发送普通文本。', 'status', chat_id=message.chat_id)
            result = self._run_async(self.adapter.stop(session.thread_id))
            if result.get('status') != 'STOP_REQUESTED':
                raise StructuredError('FEISHU_REDIRECT_INTERRUPT_FAILED', '当前活动 Turn 未能安全中断；未排队新方向。')
            queued = FeishuInboundMessage(
                message.event_id, message.message_id, message.chat_id, message.chat_type,
                message.sender_open_id, message.sender_type, message.message_type, command.argument,
                root_id=message.root_id, parent_id=message.parent_id, create_time=message.create_time,
                mentions=message.mentions, mentioned_bot=message.mentioned_bot, body_text=command.argument,
                sender_is_bot=message.sender_is_bot, reply_to_message_id=message.reply_to_message_id,
            )
            if not self.store.enqueue_message(queued):
                raise StructuredError('FEISHU_REDIRECT_QUEUE_FAILED', '新的方向未能安全排队。')
            self.enqueue(message.message_id)
            return self.replies.reply_text(message.message_id, '当前活动 Turn 已请求中断。\n新的方向已排队，将在同一个 CFR/Codex 会话中创建新的 Turn 继续执行。\n旧 Turn 已生成的内容不会作为新方向的最终交付。', 'status', chat_id=message.chat_id)
        if command.name == 'compact':
            return self.replies.reply_text(message.message_id, '当前 CFR 尚未实现可安全收尾的原生会话压缩。', 'status', chat_id=message.chat_id)
        if command.name in {'approve', 'deny'}:
            if not command.argument:
                raise StructuredError('FEISHU_APPROVAL_ID_REQUIRED', 'Approval id prefix required')
            matches = self.store.find_approval_prefix(command.argument)
            if len(matches) != 1:
                raise StructuredError('FEISHU_APPROVAL_ID_AMBIGUOUS', 'Approval id prefix is missing or ambiguous')
            result = self.approvals.resolve(matches[0]['approval_id'], message.sender_open_id, 'approve_once' if command.name == 'approve' else 'decline')
            return self.replies.reply_text(message.message_id, result.get('status', 'resolved'), 'status', chat_id=message.chat_id)
        if command.name == 'stop':
            session = self.store.get_session(message.chat_id)
            if not session or not session.thread_id:
                raise StructuredError('FEISHU_NO_ACTIVE_SESSION', 'No active CFR session')
            result = self._run_async(self.adapter.stop(session.thread_id))
            return self.replies.reply_text(message.message_id, str(result.get('status', 'STOP_REQUESTED')), 'status', chat_id=message.chat_id)
        unknown = command.argument if command.name == 'unknown' else f'/cfr {command.name}'
        return self.replies.reply_text(message.message_id, f'未知命令：{unknown}\n发送 /help 查看 CFR 使用说明。', 'status', chat_id=message.chat_id)

    def _workspace_list(self, message):
        roots = '\n'.join(f'- {root}' for root in self.settings.allowed_workspace_roots) or '未配置。'
        session = self.store.get_session(message.chat_id)
        current = session.pending_cwd if session and session.pending_cwd else None
        if session and session.thread_id:
            binding = self.binding_store.get_binding(session.thread_id)
            current = str(binding.cwd) if binding else current
        return self.replies.reply_text(message.message_id, f'当前工作区：{current or "未绑定"}\n允许的工作区：\n{roots}', 'status', chat_id=message.chat_id)

    def _handle_prompt(self, message, record=None):
        session = self.store.get_session(message.chat_id)
        if not session:
            return self.replies.reply_text(message.message_id, 'No CFR session. Use /cfr help, then /cfr new <workspace>.', 'status', chat_id=message.chat_id)
        context = FeishuExecutionContext(message.message_id, message.chat_id, message.sender_open_id, session.thread_id)
        workspace = Path(session.pending_cwd).name if session.pending_cwd else None
        if session.thread_id:
            binding = self.binding_store.get_binding(session.thread_id)
            workspace = binding.cwd.name if binding else workspace
        now = time.time()
        registry = getattr(self.adapter, 'registry', None)
        begin_telemetry = getattr(registry, 'begin', None)
        telemetry = begin_telemetry(
            message.message_id,
            thread_id=session.thread_id,
            received_at=record.received_at if record is not None else now,
            queued_at=record.received_at if record is not None else now,
            execution_started_at=record.started_at if record is not None else now,
        ) if begin_telemetry is not None else None

        def final_reply(text):
            if telemetry is not None:
                telemetry.mark('final_reply_started_at')
            try:
                return self.replies.reply_text(message.message_id, text, 'final', chat_id=message.chat_id)
            finally:
                if telemetry is not None:
                    telemetry.mark('final_reply_completed_at')
                    registry.observe(telemetry)

        progress = FeishuTurnProgress(self.replies, message.message_id, message.sender_open_id, workspace)
        progress.start()
        if session.state == 'pending_initial':
            name = f'Feishu-{Path(session.pending_cwd).name}-{datetime.now().strftime("%Y%m%d-%H%M")}'
            try:
                result = self._run_async(self.adapter.create_conversation(Path(session.pending_cwd), name, message.text or '', on_server_request=lambda request: self.approvals.handle_server_request(request, context), on_progress=progress.on_progress, telemetry=telemetry))
            except StructuredError as exc:
                self._finalize_turn_result(exc.data)
                progress.finish(exc.data.initial_turn if isinstance(exc.data, ConversationResult) else exc.data)
                raise
            except Exception:
                progress.finish('failed')
                raise
            self._finalize_turn_result(result)
            progress.finish(result.initial_turn)
            if result.initial_turn.status != 'completed':
                return final_reply(self._terminal_turn_text(result.initial_turn))
            self.store.bind_session(message.chat_id, result.thread_id)
            return final_reply(result.initial_turn.final_agent_message)
        try:
            result = self._run_async(self.adapter.send_message(session.thread_id, message.text or '', on_server_request=lambda request: self.approvals.handle_server_request(request, context), on_progress=progress.on_progress, telemetry=telemetry))
        except StructuredError as exc:
            self._finalize_turn_result(exc.data)
            progress.finish(exc.data)
            raise
        except Exception:
            progress.finish('failed')
            raise
        self._finalize_turn_result(result)
        progress.finish(result)
        if result.status != 'completed':
            return final_reply(self._terminal_turn_text(result))
        return final_reply(result.final_agent_message)

    @staticmethod
    def _terminal_turn_text(turn):
        if turn.status == 'timeout':
            return '任务执行超时，未正常完成。'
        if turn.status == 'interrupted':
            return '任务已停止，未正常完成。'
        return '任务未正常完成。'

    def _finalize_turn_result(self, result):
        """Send the real adapter terminal result through the single bridge finalizer."""
        turn = result.initial_turn if isinstance(result, ConversationResult) else result
        if not isinstance(turn, TurnResult):
            return None
        return self.approvals.finalize_feedback_for_turn(
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            turn_status=turn.status,
            error=turn.error_message,
            trigger_source='TURN_RESULT',
        )

    def _status_text(self, chat_id):
        session = self.store.get_session(chat_id)
        if not session:
            return 'CFR Status\nSession: unbound'
        binding = self.binding_store.get_binding(session.thread_id) if session.thread_id else None
        active = self.adapter.registry.get(session.thread_id) if session.thread_id else None
        return '\n'.join([
            'CFR Status',
            f'Workspace: {binding.cwd.name if binding else Path(session.pending_cwd).name if session.pending_cwd else "<none>"}',
            f'Session: {session.state}',
            f'Thread: {session.thread_id[-8:] if session.thread_id else "pending"}',
            f'Active turn: {"yes" if active else "no"}',
            f'Desktop: {binding.desktop_sync_state if binding else "up_to_date"}',
        ])

    def _safe_error_reply(self, message, error):
        registry = getattr(self.adapter, 'registry', None)
        telemetry_for = getattr(registry, 'telemetry_for', None)
        telemetry = telemetry_for(message.message_id) if telemetry_for is not None else None
        if telemetry is not None:
            telemetry.mark('final_reply_started_at')
        try:
            self.replies.reply_text(message.message_id, error.message, 'error', chat_id=message.chat_id)
        except Exception:
            pass
        finally:
            if telemetry is not None:
                telemetry.mark('final_reply_completed_at')
                if telemetry.status not in {'completed', 'stopped', 'timed_out', 'failed'}:
                    telemetry.set_stage('failed', status='failed', event='Task failed')
                registry.observe(telemetry)

    @staticmethod
    def _run_async(awaitable):
        return asyncio.run(awaitable)

    def handle_card_action(self, payload):
        event = payload.get('event') or payload
        action = payload.get('action') or event.get('action') or {}
        value = action.get('value') if isinstance(action, dict) else {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        operator = (event.get('operator') or {}).get('open_id') or payload.get('operator_open_id')
        if not isinstance(value, dict) or not value.get('approval_id') or not value.get('action') or not operator:
            raise StructuredError('FEISHU_CARD_ACTION_INVALID', 'Card action is missing required fields')
        return self.approvals.resolve(value.get('approval_id'), operator, value.get('action'))

    def stop(self):
        if not self._started:
            return
        self.stop_event.set()
        self.approvals.cancel_all_pending()
        self.approvals.close()
        for thread_id in list(self.adapter.registry._active):
            try:
                self._run_async(self.adapter.stop(thread_id))
            except Exception:
                pass
        for worker in self._workers:
            worker.join(timeout=2)
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)
        try:
            self.transport.stop()
        finally:
            self.store.release_daemon_lease(self.lease_key, self.instance_id)
            self.store.close()
            if hasattr(self.binding_store, 'close'):
                self.binding_store.close()
            self._started = False
