from __future__ import annotations

from collections import deque
from dataclasses import replace
import asyncio
import json
import logging
import mimetypes
import os
from pathlib import Path
import queue
import shutil
import threading
import time
import uuid
import weakref

from cfr.codex.permissions import APPROVAL_PRESETS, approval_preset
from cfr.core.models import ConversationResult, StructuredError, TurnResult

from .approvals import ApprovalBridge
from .commands import CHAT_COMMANDS, CODE_COMMANDS, CommandParser, ParsedCommand, help_text
from .code_runtime import CodeArtifactCollector, CodeSurfaceRuntime
from .config import FeishuSettings
from .credentials import resolve_cfr_config_dir
from .models import FeishuInboundMessage
from .progress import FeishuChatProgress
from .replies import FeishuReplyClient
from .security import validate_file, validate_workspace
from .store import FeishuStore


LOGGER = logging.getLogger(__name__)
MAX_PENDING_ATTACHMENTS_PER_CHAT = 32
MODEL_CATALOG_REFRESH_SECONDS = 60.0


class FeishuDaemon:
    """Long-running local owner of Feishu plus the selected execution adapters."""

    IMMEDIATE_COMMANDS = frozenset({
        'stop', 'steer', 'redirect', 'approve', 'deny',
        'help', 'status', 'whoami', 'surfaces', 'unknown',
    })

    def __init__(self, settings: FeishuSettings, transport, adapter=None, binding_store=None, instance_id=None, chat_adapter=None):
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
        self._owns_chat_adapter = chat_adapter is None
        if chat_adapter is None:
            from cfr.chat import ChromeChatAdapter
            if os.name != 'nt':
                from cfr.chrome_use import ChromeUseBridge
                chat_adapter = ChromeChatAdapter(ChromeUseBridge())
            else:
                chat_adapter = ChromeChatAdapter()
        self.chat_adapter = chat_adapter
        self.instance_id = instance_id or uuid.uuid4().hex
        self.lease_key = f'feishu:{settings.app_namespace}'
        # Global queue contains chat ids, not message ids. Each chat is
        # scheduled at most once, so messages keep strict FIFO order without
        # workers busy-spinning on a per-chat lock.
        self.queue: queue.Queue[str] = queue.Queue()
        self._chat_pending_counts: dict[str, int] = {}
        self._scheduled_chats: set[str] = set()
        self._active_chats: set[str] = set()
        self._queue_lock = threading.RLock()
        self.stop_event = threading.Event()
        self._workers: list[threading.Thread] = []
        self._heartbeat_thread = None
        self._started = False
        self._fatal_error = None
        self._locks = weakref.WeakValueDictionary()
        self._locks_guard = threading.RLock()
        # Embedded ChatGPT is one persistent WebView2 target. Serialize full
        # browser transactions across Feishu chats so navigation from one chat
        # cannot interleave with another chat's send/read sequence.
        self._chat_browser_lock = threading.RLock()
        self._catalog_lock = threading.Lock()
        self._catalog_warm_started = False
        self._model_catalog = None
        self._catalog_error = None
        self._catalog_checked_at = 0.0
        self._chat_runs = deque(maxlen=20)
        self._chat_runs_lock = threading.RLock()
        self._worker_failures = deque(maxlen=20)
        self.parser = CommandParser()
        self.replies = FeishuReplyClient(transport, self.store)
        self.approvals = ApprovalBridge(self.store, self.replies, settings)
        self.code_runtime = CodeSurfaceRuntime(
            settings=self.settings,
            store=self.store,
            binding_store=self.binding_store,
            adapter=self.adapter,
            replies=self.replies,
            approvals=self.approvals,
        )

    def start(self, background_workers=True):
        self.settings.validate_execution()
        self.store.acquire_daemon_lease(self.lease_key, self.instance_id, os.getpid(), ttl=30)
        queued = self.store.recover_on_startup()
        try:
            prune = getattr(self.store, 'prune_history', None)
            maintenance = prune() if prune is not None else {}
            self._cleanup_stale_attachment_paths(maintenance.get('attachment_paths', ()))
            list_paths = getattr(self.store, 'list_attachment_paths', None)
            if list_paths is not None:
                self._cleanup_orphan_attachment_cache(list_paths())
            LOGGER.info(
                'CFR_HISTORY_PRUNED inbox=%s replies=%s approvals=%s attachments=%s',
                maintenance.get('inbox', 0), maintenance.get('replies', 0),
                maintenance.get('approvals', 0), maintenance.get('attachments', 0),
            )
        except Exception as exc:
            # Retention is maintenance, not an execution prerequisite.
            LOGGER.warning('CFR_HISTORY_PRUNE_FAILED type=%s', type(exc).__name__)
        try:
            removed_downloads = self._cleanup_orphan_chat_download_cache()
            if removed_downloads:
                LOGGER.info('CHAT_DOWNLOAD_CACHE_CLEANED files=%s', removed_downloads)
        except Exception as exc:
            # Chat download files are transient delivery copies. Failure to
            # clean them must never block the runtime from starting.
            LOGGER.warning('CHAT_DOWNLOAD_CACHE_CLEANUP_FAILED type=%s', type(exc).__name__)
        for row in queued:
            self.enqueue(row['message_id'], row['chat_id'])
        self._started = True
        self._fatal_error = None
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
        last_success = time.monotonic()
        while not self.stop_event.wait(10):
            try:
                owned = self.store.heartbeat_daemon_lease(self.lease_key, self.instance_id, ttl=30)
            except Exception as exc:
                # A short SQLite writer collision must not silently kill the
                # heartbeat thread.  Preserve the last confirmed 30s lease and
                # retry; fail closed only when the grace window is exhausted.
                LOGGER.warning('FEISHU_DAEMON_HEARTBEAT_DEFERRED type=%s', type(exc).__name__)
                if time.monotonic() - last_success < 25:
                    continue
                self._fatal_error = StructuredError('FEISHU_DAEMON_LEASE_HEARTBEAT_FAILED', 'Feishu daemon lease could not be renewed before expiry')
                self.stop_event.set()
                break
            if not owned:
                self._fatal_error = StructuredError('FEISHU_DAEMON_LEASE_LOST', 'Feishu daemon lease ownership was lost')
                self.stop_event.set()
                break
            last_success = time.monotonic()

    def enqueue(self, message_id, chat_id=None):
        if self.stop_event.is_set():
            return False
        if not chat_id:
            record = self.store.get_inbox(message_id)
            if record is None:
                return False
            chat_id = record.chat_id
        chat_id = str(chat_id)
        with self._queue_lock:
            self._chat_pending_counts[chat_id] = self._chat_pending_counts.get(chat_id, 0) + 1
            if chat_id not in self._scheduled_chats:
                self._scheduled_chats.add(chat_id)
                self.queue.put(chat_id)
        return True

    def enqueue_for_chat(self, message_id, chat_id):
        return self.enqueue(message_id, chat_id)

    def runtime_snapshot(self):
        with self._queue_lock:
            queue_depth = sum(self._chat_pending_counts.values())
            queued_chats = len(self._scheduled_chats - self._active_chats)
            active_chats = len(self._active_chats)
        return {
            'queue_depth': queue_depth,
            'queued_chats': queued_chats,
            'active_chats': active_chats,
            'workers_total': len(self._workers),
            'workers_alive': sum(worker.is_alive() for worker in self._workers),
            'heartbeat_alive': bool(self._heartbeat_thread and self._heartbeat_thread.is_alive()),
            'recent_worker_failures': list(self._worker_failures),
        }

    @property
    def is_running(self):
        if not self._started or self.stop_event.is_set():
            return False
        if self._heartbeat_thread is not None and not self._heartbeat_thread.is_alive():
            return False
        if self._workers and not any(worker.is_alive() for worker in self._workers):
            return False
        return True

    @property
    def fatal_error(self):
        if self._fatal_error is not None:
            return self._fatal_error
        if self._started and not self.stop_event.is_set():
            if self._heartbeat_thread is not None and not self._heartbeat_thread.is_alive():
                return StructuredError('FEISHU_DAEMON_HEARTBEAT_STOPPED', 'Feishu daemon heartbeat thread stopped unexpectedly')
            if self._workers and any(not worker.is_alive() for worker in self._workers):
                alive = sum(worker.is_alive() for worker in self._workers)
                return StructuredError(
                    'FEISHU_WORKER_STOPPED',
                    f'Feishu worker capacity degraded: {alive}/{len(self._workers)} workers alive',
                )
        return self._fatal_error

    def _worker(self):
        last_error_log = 0.0
        while not self.stop_event.is_set():
            try:
                chat_id = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._queue_lock:
                if chat_id in self._active_chats:
                    # A duplicate scheduler token belongs to the worker that
                    # already owns this chat. Consuming this token must not
                    # clear the other worker's ownership or schedule state.
                    self.queue.task_done()
                    continue
                self._active_chats.add(chat_id)
            try:
                try:
                    message_id = self.store.next_queued_message_id(chat_id)
                    if message_id is not None:
                        with self._queue_lock:
                            remaining = max(0, self._chat_pending_counts.get(chat_id, 1) - 1)
                            if remaining:
                                self._chat_pending_counts[chat_id] = remaining
                            else:
                                self._chat_pending_counts.pop(chat_id, None)
                        self.process_one(message_id)
                except Exception as exc:
                    now = time.monotonic()
                    if not hasattr(self, '_worker_failures'):
                        self._worker_failures = deque(maxlen=20)
                    self._worker_failures.append({
                        'at': time.time(),
                        'chat_id_suffix': str(chat_id)[-8:],
                        'error_type': type(exc).__name__,
                    })
                    if now - last_error_log >= 5.0:
                        LOGGER.warning('FEISHU_WORKER_RETRY chat_suffix=%s type=%s', str(chat_id)[-8:], type(exc).__name__)
                        last_error_log = now
                    self.stop_event.wait(0.25)
            finally:
                database_retry = False
                try:
                    queued_in_database = self.store.next_queued_message_id(chat_id) is not None
                except Exception:
                    # Preserve the schedule on transient SQLite failure; the
                    # next worker pass will retry the durable row.
                    queued_in_database = True
                    database_retry = True
                with self._queue_lock:
                    self._active_chats.discard(chat_id)
                    if queued_in_database:
                        self.queue.put(chat_id)
                    else:
                        # SQLite is the queue authority.  Any non-zero in-memory
                        # count here is telemetry drift, not work to spin on.
                        self._chat_pending_counts.pop(chat_id, None)
                        self._scheduled_chats.discard(chat_id)
                self.queue.task_done()
                if database_retry:
                    self.stop_event.wait(0.05)

    def process_pending(self, limit=None):
        processed = 0
        while limit is None or processed < limit:
            record = self.process_one()
            if record is None:
                break
            processed += 1
        return processed

    def process_one(self, message_id=None):
        record = self.store.claim_next(message_id)
        if record is None:
            return None
        completed = False
        try:
            resources = tuple(json.loads(record.resources_json or '[]'))
        except (TypeError, json.JSONDecodeError):
            resources = ()
        message = FeishuInboundMessage(
            record.event_id, record.message_id, record.chat_id, record.chat_type,
            record.sender_open_id, 'user', record.message_type, record.text_content,
            resources=resources,
        )
        try:
            try:
                response_ids = self._dispatch(message, record)
                self.store.mark_completed(message.message_id, response_ids[-1] if response_ids else None)
                completed = True
            except StructuredError as exc:
                self.store.mark_failed(message.message_id, exc.code, exc.message)
                self._safe_error_reply(message, exc)
            except Exception as exc:
                self.store.mark_failed(message.message_id, type(exc).__name__, str(exc))
                LOGGER.exception(
                    'FEISHU_JOB_UNEXPECTED_FAILED message=%s type=%s',
                    message.message_id[-8:], type(exc).__name__,
                )
                self._safe_error_reply(
                    message,
                    StructuredError('FEISHU_JOB_FAILED', 'CFR 内部执行失败；详细错误已记录到本机运行日志。'),
                )
        finally:
            claimed = getattr(self.store, 'list_claimed_attachments', lambda _message_id: [])(message.message_id)
            if claimed:
                if completed:
                    self._clear_pending_attachments(message.chat_id, claimed)
                else:
                    release = getattr(self.store, 'release_claimed_attachments', None)
                    if release is not None:
                        try:
                            release(message.message_id)
                        except Exception as exc:
                            LOGGER.warning(
                                'FEISHU_ATTACHMENT_CLAIM_RELEASE_FAILED message=%s type=%s',
                                message.message_id[-8:], type(exc).__name__,
                            )
        return record

    def _lock_for(self, chat_id):
        if not hasattr(self, '_locks_guard'):
            self._locks_guard = threading.RLock()
            self._locks = weakref.WeakValueDictionary()
        with self._locks_guard:
            return self._locks.setdefault(chat_id, threading.RLock())

    def _dispatch(self, message: FeishuInboundMessage, record=None):
        if message.message_type in {'image', 'file', 'media', 'video'}:
            lock = self._lock_for(message.chat_id)
            with lock:
                return self._handle_attachment_message(message)
        command = self.parser.parse(message.text)
        if command and command.name in {'stop', 'steer', 'redirect'}:
            return self._handle_command(message, command)
        lock = self._lock_for(message.chat_id)
        with lock:
            if self._selected_surface(message.chat_id) == 'chat':
                if command:
                    with self._chat_browser_lock:
                        return self._handle_command(message, command)
                return self._handle_prompt(message, record)
            if command:
                return self._handle_command(message, command)
            return self._handle_prompt(message, record)

    def _handle_attachment_message(self, message):
        resource_type = {'image': 'image', 'file': 'file', 'media': 'video', 'video': 'video'}.get(message.message_type)
        resources = [
            item for item in message.resources
            if isinstance(item, dict)
            and item.get('type') == resource_type
            and item.get('file_key')
        ]
        if not resources:
            raise StructuredError('FEISHU_ATTACHMENT_RESOURCE_MISSING', '飞书附件没有可下载的资源标识。')
        pending_count = len(self._pending_attachments(message.chat_id))
        if pending_count + len(resources) > MAX_PENDING_ATTACHMENTS_PER_CHAT:
            raise StructuredError(
                'FEISHU_PENDING_ATTACHMENT_LIMIT',
                f'当前聊天最多暂存 {MAX_PENDING_ATTACHMENTS_PER_CHAT} 个附件；请先发送普通消息消费现有附件。',
            )
        inbox = resolve_cfr_config_dir() / 'feishu' / 'inbox'
        message_root = inbox / uuid.uuid5(uuid.NAMESPACE_URL, f'feishu-message:{message.message_id}').hex
        paths = []
        try:
            for index, resource in enumerate(resources):
                destination = message_root / f'{index:02d}'
                if resource_type == 'image':
                    path = self.transport.download_image_to_file(resource['file_key'], destination, message_id=message.message_id)
                    kind = 'image'
                else:
                    raw_name = str(resource.get('file_name') or '').replace('\x00', '').strip()
                    safe_name = Path(raw_name).name if raw_name else None
                    if safe_name in {'', '.', '..'}:
                        safe_name = None
                    path = self.transport.download_file_to_file(
                        resource['file_key'], destination, message_id=message.message_id,
                        file_name=safe_name,
                        # Feishu's message-resource API accepts image/file;
                        # file covers ordinary files, audio, and video.
                        resource_type='file',
                    )
                    kind = 'file'
                path = Path(path).resolve(strict=True)
                if not CodeArtifactCollector._within(path, destination):
                    raise StructuredError(
                        'FEISHU_ATTACHMENT_CACHE_ESCAPE',
                        '飞书附件下载结果超出 CFR 消息缓存目录，已拒绝使用。',
                    )
                paths.append((Path(path), kind))
            add_many = getattr(self.store, 'add_pending_attachments', None)
            if add_many is not None:
                add_many(
                    message.chat_id,
                    [(path, kind, path.name) for path, kind in paths],
                    source_message_id=message.message_id,
                )
            else:
                for path, kind in paths:
                    self.store.add_pending_attachment(message.chat_id, path, kind, path.name)
        except Exception:
            for path, _kind in paths:
                try:
                    path.unlink(missing_ok=True)
                    self._remove_empty_cache_parents(path.parent, inbox)
                except OSError:
                    pass
            raise
        paths = [path for path, _kind in paths]
        surface = 'Chat' if self._selected_surface(message.chat_id) == 'chat' else 'Code'
        saved = str(paths[0]) if len(paths) == 1 else f'{len(paths)} 个附件：\n' + '\n'.join(str(path) for path in paths)
        return self.replies.reply_text(
            message.message_id,
            f'飞书附件已缓存到 CFR 本地：{saved}\n已加入待发送附件；下一条普通消息会一并发送到 {surface} Surface，成功消费后自动清理该缓存。',
            'final',
            chat_id=message.chat_id,
        )

    def _handle_image_message(self, message):
        return self._handle_attachment_message(message)

    def _pending_attachments(self, chat_id):
        getter = getattr(self.store, 'list_pending_attachments', None)
        return getter(chat_id) if getter else []

    def _claim_pending_attachments(self, chat_id, message_id):
        claimer = getattr(self.store, 'claim_pending_attachments', None)
        return claimer(chat_id, message_id) if claimer else self._pending_attachments(chat_id)

    def _clear_pending_attachments(self, chat_id, attachments, extra_paths=()):
        clearer = getattr(self.store, 'clear_pending_attachments', None)
        if clearer and attachments:
            clearer(chat_id, [item['attachment_id'] for item in attachments])

        inbox = (resolve_cfr_config_dir() / 'feishu' / 'inbox').resolve()
        owned_paths = []
        for item in attachments:
            try:
                path = Path(item['path']).resolve()
                path.relative_to(inbox)
            except (KeyError, ValueError):
                continue
            owned_paths.append(path)
        owned_paths.extend(Path(path).resolve() for path in extra_paths)
        for path in dict.fromkeys(owned_paths):
            try:
                path.unlink(missing_ok=True)
                self._remove_empty_cache_parents(path.parent, inbox)
            except OSError as exc:
                LOGGER.warning('FEISHU_ATTACHMENT_CACHE_CLEANUP_FAILED file=%s type=%s', path.name, type(exc).__name__)

    @staticmethod
    def _remove_empty_cache_parents(parent, root):
        root = Path(root).resolve()
        current = Path(parent).resolve()
        while current != root:
            try:
                current.relative_to(root)
                current.rmdir()
            except (OSError, ValueError):
                break
            current = current.parent

    @staticmethod
    def _cleanup_stale_attachment_paths(paths):
        inbox = (resolve_cfr_config_dir() / 'feishu' / 'inbox').resolve()
        for raw_path in paths:
            try:
                path = Path(raw_path).resolve()
                path.relative_to(inbox)
                path.unlink(missing_ok=True)
                FeishuDaemon._remove_empty_cache_parents(path.parent, inbox)
            except (OSError, ValueError):
                continue

    @staticmethod
    def _cleanup_orphan_attachment_cache(referenced_paths):
        """Delete CFR-owned cache files that have no durable attachment row."""
        inbox = (resolve_cfr_config_dir() / 'feishu' / 'inbox').resolve()
        if not inbox.is_dir():
            return 0
        referenced = set()
        for raw_path in referenced_paths or ():
            try:
                path = Path(raw_path).resolve()
                path.relative_to(inbox)
            except (OSError, ValueError):
                continue
            referenced.add(path)
        removed = 0
        for path in list(inbox.rglob('*')):
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(inbox)
            except (OSError, ValueError):
                continue
            if resolved in referenced:
                continue
            try:
                resolved.unlink(missing_ok=True)
                removed += 1
                FeishuDaemon._remove_empty_cache_parents(resolved.parent, inbox)
            except OSError:
                continue
        if removed:
            LOGGER.info('FEISHU_ATTACHMENT_ORPHAN_CACHE_CLEANED files=%s', removed)
        return removed

    @staticmethod
    def _cleanup_chat_download(path):
        root = (resolve_cfr_config_dir() / 'chatgpt' / 'downloads').resolve()
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(root)
            resolved.unlink(missing_ok=True)
            FeishuDaemon._remove_empty_cache_parents(resolved.parent, root)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _cleanup_orphan_chat_download_cache():
        """Clear CFR-owned transient Chat delivery files before accepting work."""
        root = (resolve_cfr_config_dir() / 'chatgpt' / 'downloads').resolve()
        if not root.exists():
            return 0
        files = sum(1 for path in root.rglob('*') if path.is_file() or path.is_symlink())
        shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        return files

    @staticmethod
    def _prepare_codex_attachments(attachments, workspace):
        return CodeSurfaceRuntime.prepare_attachments(attachments, workspace)

    @staticmethod
    def _codex_prompt_with_files(text, file_paths):
        return CodeSurfaceRuntime.prompt_with_files(text, file_paths)

    def handle_control_command(self, message: FeishuInboundMessage):
        """Run an immediate command; serialize any compatibility caller."""
        command = self.parser.parse(message.text)
        if command is None:
            return None
        try:
            if command.name in self.IMMEDIATE_COMMANDS:
                return self._handle_command(message, command)
            with self._lock_for(message.chat_id):
                return self._handle_command(message, command)
        except StructuredError as error:
            return self._safe_error_reply(message, error)

    def _bound_session(self, message):
        session = self.store.get_session(message.chat_id)
        if not session or not session.thread_id:
            raise StructuredError('FEISHU_NO_ACTIVE_SESSION', '当前聊天没有已绑定的 CFR 线程。')
        return session

    def _selected_surface(self, chat_id):
        default = getattr(getattr(self, 'settings', None), 'default_surface', 'code')
        optional_getter = getattr(self.store, 'get_selected_surface_or_none', None)
        if optional_getter is not None:
            return optional_getter(chat_id) or default
        getter = getattr(self.store, 'get_selected_surface', None)
        return getter(chat_id) if getter else default

    def update_workspace_roots(self, roots):
        """Atomically hot-reload the workspace allowlist for future operations."""
        settings = replace(
            self.settings,
            allowed_workspace_roots=tuple(Path(root).expanduser().resolve() for root in roots),
        )
        self.settings = settings
        self.approvals.settings = settings
        runtime = getattr(self, 'code_runtime', None)
        if runtime is not None:
            runtime.settings = settings
        return settings

    def _chat_health(self):
        adapter = getattr(self, 'chat_adapter', None)
        if adapter is None:
            return {'available': False, 'status': 'not_connected', 'description': 'Chat adapter is not configured.'}
        try:
            return adapter.health()
        except Exception:
            return {'available': False, 'status': 'not_connected', 'description': 'Chat adapter health check failed.'}

    def _begin_chat_run(self, message, binding, attachment_count, record=None):
        if not hasattr(self, '_chat_runs'):
            self._chat_runs = deque(maxlen=20)
        if not hasattr(self, '_chat_runs_lock'):
            self._chat_runs_lock = threading.RLock()
        started_at = time.time()
        received_at = getattr(record, 'received_at', None) or started_at
        run = {
            'id': message.message_id,
            'surface': 'chat',
            'chat_id': message.chat_id,
            'status': 'running',
            'active': True,
            'stage': 'uploading' if attachment_count else 'sending',
            'received_at': received_at,
            'started_at': started_at,
            'completed_at': None,
            'url': (binding or {}).get('url'),
            'conversation_id': None,
            'attachment_count': attachment_count,
            'output_count': 0,
            'error_code': None,
            'queue_ms': round(max(0.0, started_at - received_at) * 1000, 1),
            'current_owner': 'cfr',
            'timings_ms': {},
            'owner_timings_ms': {},
            'timeline': [],
            'artifact_failures': [],
        }
        with self._chat_runs_lock:
            self._chat_runs.appendleft(run)
        return run['id']

    def _update_chat_run(self, run_id, **changes):
        if not hasattr(self, '_chat_runs') or not hasattr(self, '_chat_runs_lock'):
            return
        with self._chat_runs_lock:
            run = next((item for item in self._chat_runs if item['id'] == run_id), None)
            if run is not None:
                trace = changes.pop('trace', None)
                if isinstance(trace, dict):
                    event = str(trace.get('event') or 'activity')
                    owner = str(trace.get('owner') or 'cfr')
                    duration = max(0.0, float(trace.get('duration_ms') or 0.0))
                    timings = run.setdefault('timings_ms', {})
                    owners = run.setdefault('owner_timings_ms', {})
                    timings[event] = round(float(timings.get(event, 0.0)) + duration, 1)
                    owners[owner] = round(float(owners.get(owner, 0.0)) + duration, 1)
                    timeline = run.setdefault('timeline', [])
                    timeline.append({
                        'event': event,
                        'owner': owner,
                        'at': trace.get('at') or time.time(),
                        'duration_ms': duration,
                        'elapsed_ms': trace.get('elapsed_ms'),
                        'detail': trace.get('detail'),
                    })
                    del timeline[:-40]
                    run['current_owner'] = owner
                    run['last_activity_at'] = trace.get('at') or time.time()
                    LOGGER.info(
                        'CHAT_TRACE event=%s owner=%s duration_ms=%.1f',
                        event, owner, duration,
                    )
                run.update(changes)

    def _trace_chat_run(self, run_id, event, owner, started_at, detail=None):
        now = time.monotonic()
        trace = {
            'event': event,
            'owner': owner,
            'duration_ms': round(max(0.0, now - started_at) * 1000, 1),
            'at': time.time(),
        }
        if detail:
            trace['detail'] = str(detail)
        self._update_chat_run(run_id, trace=trace, stage=event)
        return now

    def chat_runtime_snapshot(self):
        if not hasattr(self, '_chat_runs') or not hasattr(self, '_chat_runs_lock'):
            return []
        with self._chat_runs_lock:
            return [dict(item) for item in self._chat_runs]

    def _surface_catalog(self, chat_id):
        from cfr.surfaces import execution_surfaces
        return execution_surfaces(selected=self._selected_surface(chat_id), chat_status=self._chat_health())

    @staticmethod
    def _resolve(items, value, keys):
        value = (value or '').strip()
        if value.isdigit():
            index = int(value) - 1
            if 0 <= index < len(items):
                return items[index]
        return next((item for item in items if value in [str(item.get(key) or '') if isinstance(item, dict) else str(getattr(item, key, '') or '') for key in keys]), None)

    @staticmethod
    def _history_limit(argument):
        raw = str(argument or '10').strip()
        try:
            requested = int(raw)
        except ValueError as error:
            raise StructuredError('CFR_HISTORY_LIMIT_INVALID', '用法：/history [数量]；数量会自动限制在 1-50。') from error
        return min(max(requested, 1), 50), requested

    def _resolve_workspace(self, value):
        value = (value or '').strip()
        roots = [Path(root).resolve() for root in self.settings.allowed_workspace_roots if Path(root).is_dir()]
        if value.isdigit():
            index = int(value) - 1
            if 0 <= index < len(roots):
                return roots[index]
            raise StructuredError('FEISHU_WORKSPACE_NOT_ALLOWED', '工作区编号不存在；发送 /workspace 查看可选工作区。')
        named = [root for root in roots if root.name.casefold() == value.casefold()]
        if len(named) == 1:
            return named[0]
        return validate_workspace(value, self.settings)

    @staticmethod
    def _resolve_binding(bindings, value):
        value = (value or '').strip()
        if value.isdigit():
            index = int(value) - 1
            if 0 <= index < len(bindings):
                return bindings[index]
        exact = [item for item in bindings if value in {str(item.thread_id), str(item.thread_name or '')}]
        if len(exact) == 1:
            return exact[0]
        suffix = [item for item in bindings if str(item.thread_id).endswith(value)] if value else []
        return suffix[0] if len(suffix) == 1 else None

    @staticmethod
    def _current_thread_model(catalog, identity):
        """Resolve the native thread model identity against the native catalog."""
        from cfr.control.model_registry import resolve_model
        return resolve_model(catalog, identity)

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
        try:
            catalog = models()
            if catalog.get('available'):
                self._model_catalog = catalog['data']
                self._catalog_error = None
            else:
                self._catalog_error = catalog.get('error_code') or 'CODEX_MODEL_LIST_UNAVAILABLE'
            self._catalog_checked_at = time.monotonic()
        finally:
            with self._catalog_lock:
                self._catalog_warm_started = False

    def _catalog(self):
        catalog = getattr(self, '_model_catalog', None)
        if catalog is not None:
            checked_at = float(getattr(self, '_catalog_checked_at', 0.0) or 0.0)
            if checked_at and time.monotonic() - checked_at >= MODEL_CATALOG_REFRESH_SECONDS:
                self._start_catalog_warm()
            return catalog
        self._start_catalog_warm()
        if getattr(self, '_catalog_error', None):
            raise StructuredError('CODEX_MODEL_LIST_UNAVAILABLE', '当前安装的 Codex 模型目录不可用。')
        raise StructuredError('CODEX_MODEL_LIST_SYNCING', '正在后台同步 Codex 模型目录，请稍后重试。')

    def _thread_settings(self, message):
        session = self._bound_session(message)
        return session, self._run_async(self.adapter.read_thread_settings(session.thread_id))

    def _pending_thread_settings(self, message):
        session = self.store.get_session(message.chat_id)
        if not session or session.thread_id or getattr(session, 'state', None) != 'pending_initial':
            return None, None
        getter = getattr(self.store, 'get_pending_thread_settings', None)
        return session, getter(message.chat_id) if getter else {}

    def _save_pending_thread_settings(self, message, **changes):
        updater = getattr(self.store, 'update_pending_thread_settings', None)
        if updater is None:
            raise StructuredError('FEISHU_PENDING_SETTINGS_UNAVAILABLE', '当前运行时不能保存新线程预设。')
        return updater(message.chat_id, **changes)

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
        selected_surface = self._selected_surface(message.chat_id)
        if command.name != 'unknown':
            allowed = CHAT_COMMANDS if selected_surface == 'chat' else CODE_COMMANDS
            if command.name not in allowed:
                owner = 'Code' if command.name in CODE_COMMANDS else 'Chat' if command.name in CHAT_COMMANDS else 'other'
                return self.replies.reply_text(
                    message.message_id,
                    f'/{command.name} 属于 {owner} Surface，当前是 {selected_surface.title()} Surface，已拒绝执行。\n'
                    f'发送 /help 查看当前 Surface 的有效指令；需要切换请使用 /surface {owner.lower()}。',
                    'status',
                    chat_id=message.chat_id,
                )
        if command.name == 'help':
            return self.replies.reply_text(message.message_id, help_text(selected_surface), 'status', chat_id=message.chat_id)
        if selected_surface == 'code' and command.name == 'approval':
            current = getattr(self.store, 'get_approval_mode', lambda _chat_id: None)(message.chat_id) or 'ask'
            if not command.argument:
                lines = [f'当前 Code 审批模式：{APPROVAL_PRESETS[current]["label"]}', 'Codex 原生映射：']
                for index, key in enumerate(('ask', 'auto', 'full'), 1):
                    preset = APPROVAL_PRESETS[key]
                    reviewer = preset.get('approvals_reviewer') or '—'
                    lines.append(
                        f'{index}. {preset["label"]} · approvalPolicy={preset["approval_policy"]} · '
                        f'approvalsReviewer={reviewer} · sandbox={preset["sandbox"]}'
                    )
                return self.replies.reply_text(message.message_id, '\n'.join(lines), 'status', chat_id=message.chat_id)
            mode = approval_preset(command.argument)
            if mode is None:
                raise StructuredError('FEISHU_APPROVAL_MODE_INVALID', '用法：/approval <1|2|3|ask|auto|full>。')
            self.store.set_approval_mode(message.chat_id, mode)
            preset = APPROVAL_PRESETS[mode]
            suffix = '；该模式关闭审批并使用 danger-full-access，请只在你明确需要时使用。' if mode == 'full' else ''
            return self.replies.reply_text(
                message.message_id,
                f'Code 审批模式已设置为：{preset["label"]}。从下一次 Code turn 起按 Codex 原生权限字段生效{suffix}',
                'status',
                chat_id=message.chat_id,
            )
        if command.name == 'history':
            limit, requested_limit = self._history_limit(command.argument)
            if selected_surface == 'chat':
                binding = self.store.ensure_chat_binding(message.chat_id)
                items = self.chat_adapter.conversation_history(binding, limit=limit)
                identity_reader = getattr(self.chat_adapter, 'current_identity', None)
                identity = {}
                if identity_reader is not None:
                    identity = identity_reader(binding)
                    if identity.get('url'):
                        self.store.update_chat_binding(message.chat_id, tab_id=identity.get('tab_id'), url=identity.get('url'))
                title = 'ChatGPT 当前 Conversation 最近对话：'
            else:
                from cfr.codex.rollout import recent_rollout_messages
                session = self._bound_session(message)
                binding = self.binding_store.get_binding(session.thread_id)
                if not binding or not binding.rollout_path:
                    raise StructuredError('CODEX_HISTORY_UNAVAILABLE', '当前 Codex thread 还没有可读取的 native rollout。')
                items = recent_rollout_messages(binding.rollout_path, limit=limit)
                title = f'Codex thread {session.thread_id[-8:]} 最近对话：'
            lines = []
            for index, item in enumerate(items, 1):
                role = '你' if item.get('role') == 'user' else '助手'
                text = str(item.get('text') or '').strip()
                if len(text) > 1200:
                    text = text[:1197] + '...'
                lines.append(f'{index}. {role}: {text}')
            return self.replies.reply_text(
                message.message_id,
                title
                + (f'（请求 {requested_limit} 条，按上限返回 {limit} 条）' if requested_limit != limit else '')
                + '\n'
                + ('\n\n'.join(lines) if lines else (
                    '当前还没有打开具体 Conversation。先发送 /chats 查看当前范围内的对话，再用 /chat <编号> 打开；也可以 /new 创建新对话。'
                    if selected_surface == 'chat' and not identity.get('conversation_id') else
                    '当前 Conversation 暂无可读取的对话记录。'
                )),
                'status',
                chat_id=message.chat_id,
            )
        if selected_surface == 'chat' and command.name == 'new':
            binding = self.store.ensure_chat_binding(message.chat_id)
            state = self.chat_adapter.new_conversation(binding)
            self.store.update_chat_binding(message.chat_id, tab_id=state.get('tab_id'), url=state.get('url'))
            if command.argument:
                return self._handle_chat_prompt(message, command.argument)
            return self.replies.reply_text(message.message_id, '已打开新的原生 ChatGPT 对话。下一条普通消息会发送到该对话。', 'status', chat_id=message.chat_id)
        if selected_surface == 'chat' and command.name == 'projects':
            projects = self.chat_adapter.list_projects()
            lines = [f'{index}. {item["name"]}' for index, item in enumerate(projects, 1)]
            lines.append(f'{len(projects) + 1}. 普通 Chat（不在 Project 中）')
            text = 'ChatGPT 项目：\n' + '\n'.join(lines)
            return self.replies.reply_text(message.message_id, text, 'status', chat_id=message.chat_id)
        if selected_surface == 'chat' and command.name == 'project':
            if not command.argument:
                raise StructuredError('CHAT_PROJECT_REQUIRED', '用法：/project <名称|Project ID|URL>')
            binding = self.store.ensure_chat_binding(message.chat_id)
            state = self.chat_adapter.open_project(binding, command.argument)
            self.store.update_chat_binding(message.chat_id, tab_id=state.get('tab_id'), url=state.get('url'))
            if not state.get('project_id'):
                return self.replies.reply_text(message.message_id, '已切换到普通 Chat（不在 Project 中）。', 'status', chat_id=message.chat_id)
            return self.replies.reply_text(message.message_id, f'已打开 ChatGPT Project：{state.get("project_id") or command.argument}', 'status', chat_id=message.chat_id)
        if selected_surface == 'chat' and command.name in {'chats', 'chat'}:
            binding = self.store.ensure_chat_binding(message.chat_id)
            conversations = self.chat_adapter.list_project_conversations(binding)
            if command.name == 'chats' or not command.argument:
                in_project = '/g/g-p-' in str(binding.get('url') or '')
                title = '当前 Project 对话：' if in_project else '普通 Chat 对话：'
                empty = (
                    '当前页面没有可枚举的 Project 对话。'
                    if in_project else
                    '当前普通 Chat 页面暂未读到可枚举对话；可先用 /new <首条消息> 创建，或打开任意普通对话后再 /chats。'
                )
                text = title + '\n' + ('\n'.join(
                    f'{index}. {item["title"]} · {item["conversation_id"][-8:]}'
                    for index, item in enumerate(conversations, 1)
                ) or empty)
                return self.replies.reply_text(message.message_id, text, 'status', chat_id=message.chat_id)
            requested = command.argument.strip()
            target = None
            if requested.isdigit() and 0 < int(requested) <= len(conversations):
                target = conversations[int(requested) - 1]
            else:
                target = next((item for item in conversations if requested in {item['conversation_id'], item['title'], item['url']}), None)
            state = self.chat_adapter.open_conversation(
                binding,
                target['url'] if target else requested,
                project_id=(target or {}).get('project_id'),
            )
            self.store.update_chat_binding(message.chat_id, tab_id=state.get('tab_id'), url=state.get('url'))
            return self.replies.reply_text(message.message_id, f'已打开 ChatGPT Conversation：{state.get("conversation_id") or requested}', 'status', chat_id=message.chat_id)
        if selected_surface == 'chat' and command.name in {'models', 'model'}:
            binding = self.store.ensure_chat_binding(message.chat_id)
            if command.name == 'model' and command.argument:
                state = self.chat_adapter.set_model(binding, command.argument)
                text = f'ChatGPT 模型已设置为：{state.get("current") or command.argument}。'
            else:
                state = self.chat_adapter.models(binding)
                lines = []
                for index, item in enumerate(state.get('models') or (), 1):
                    suffix = ' · 当前' if item.get('selected') else ' · 当前账号不可用' if item.get('disabled') else ''
                    lines.append(f'{index}. {item.get("name")}{suffix}')
                text = (
                    f'当前 ChatGPT 模型：{state.get("current") or "未识别"}\n网页实时可选模型：\n'
                    + ('\n'.join(lines) or '当前没有可读取的模型。')
                )
            self.store.update_chat_binding(message.chat_id, tab_id=state.get('tab_id'), url=state.get('url'))
            return self.replies.reply_text(message.message_id, text, 'status', chat_id=message.chat_id)
        if selected_surface == 'chat' and command.name in {'reasoning', 'effort'}:
            binding = self.store.ensure_chat_binding(message.chat_id)
            if command.argument:
                state = self.chat_adapter.set_reasoning_effort(binding, command.argument)
                prefix = 'ChatGPT Thinking effort 已设置为'
            else:
                state = self.chat_adapter.reasoning_effort(binding)
                prefix = '当前 ChatGPT Thinking effort'
            self.store.update_chat_binding(message.chat_id, tab_id=state.get('tab_id'), url=state.get('url'))
            return self.replies.reply_text(
                message.message_id,
                f'{prefix}：{state.get("label") or "未识别"}（{state.get("index") or "?"}/{state.get("total") or "?"}）。',
                'status',
                chat_id=message.chat_id,
            )
        if selected_surface == 'chat' and command.name == 'scheduled':
            argument = (command.argument or '').strip()
            if not argument:
                self.chat_adapter.open_scheduled(self.store.ensure_chat_binding(message.chat_id))
                return self.replies.reply_text(message.message_id, '已打开 ChatGPT 原生 Scheduled 管理页。', 'status', chat_id=message.chat_id)
            action, _, remainder = argument.partition(' ')
            action = action.lower()
            if action == 'list':
                tasks = self.chat_adapter.list_scheduled_tasks()
                lines = [
                    f'{index}. {item["title"]} · {item.get("detail") or "无调度摘要"} · {"已暂停" if item.get("paused") else "运行中"} · {item.get("task_id") or "无 task ID"}'
                    for index, item in enumerate(tasks, 1)
                ]
                return self.replies.reply_text(message.message_id, 'ChatGPT 定时任务：\n' + ('\n'.join(lines) or '暂无任务。'), 'status', chat_id=message.chat_id)
            if action == 'create':
                if not remainder.strip():
                    raise StructuredError('CHAT_SCHEDULED_PROMPT_REQUIRED', '用法：/scheduled create <自然语言任务>')
                task = self.chat_adapter.create_scheduled_task(remainder.strip())
                return self.replies.reply_text(message.message_id, f'已创建 Scheduled Task：{task.get("title")} · {task.get("detail")} · {task.get("task_id")}', 'status', chat_id=message.chat_id)
            token, _, edit_value = remainder.strip().partition(' ')
            if action not in {'pause', 'resume', 'edit', 'delete'} or not token:
                raise StructuredError('CHAT_SCHEDULED_COMMAND_INVALID', '用法：/scheduled list|create|pause|resume|edit|delete；发送 /help 查看详细说明。')
            tasks = self.chat_adapter.list_scheduled_tasks()
            task = self._resolve(tasks, token, ('task_id', 'title'))
            if not task:
                raise StructuredError('CHAT_SCHEDULED_TASK_NOT_FOUND', f'没有唯一匹配的 Scheduled task：{token}')
            task_id = task['task_id']
            if action == 'pause':
                result = self.chat_adapter.pause_scheduled_task(task_id)
                return self.replies.reply_text(message.message_id, f'已暂停：{result.get("title") or task["title"]} · {task_id}', 'status', chat_id=message.chat_id)
            if action == 'resume':
                result = self.chat_adapter.resume_scheduled_task(task_id)
                return self.replies.reply_text(message.message_id, f'已恢复：{result.get("title") or task["title"]} · {task_id}', 'status', chat_id=message.chat_id)
            if action == 'delete':
                self.chat_adapter.delete_scheduled_task(task_id)
                return self.replies.reply_text(message.message_id, f'已删除定时任务：{task["title"]} · {task_id}', 'status', chat_id=message.chat_id)
            field, separator, value = edit_value.partition('=')
            field = field.strip().lower()
            value = value.strip()
            if not separator or field != 'title' or not value:
                raise StructuredError('CHAT_SCHEDULED_EDIT_REQUIRED', '当前已验证用法：/scheduled edit <编号|task ID> title=<新标题>')
            result = self.chat_adapter.edit_scheduled_task(task_id, **{field: value})
            return self.replies.reply_text(message.message_id, f'已编辑定时任务：{result.get("title") or task["title"]} · {task_id}', 'status', chat_id=message.chat_id)
        if selected_surface == 'chat' and command.name == 'search':
            if not command.argument:
                raise StructuredError('CHAT_SEARCH_PROMPT_REQUIRED', '用法：/search <问题>')
            binding = self.store.ensure_chat_binding(message.chat_id)
            result = self.chat_adapter.send_search(binding, command.argument)
            self.store.update_chat_binding(message.chat_id, tab_id=result.get('tab_id'), url=result.get('url'))
            return self.replies.reply_text(message.message_id, result.get('text') or 'ChatGPT 网页搜索已完成。', 'final', chat_id=message.chat_id)
        if selected_surface == 'chat' and command.name == 'deepresearch':
            binding = self.store.ensure_chat_binding(message.chat_id)
            if not command.argument or command.argument.strip().lower() == 'status':
                result = self.chat_adapter.deep_research_status(binding)
                if result.get('state') == 'completed' and result.get('report'):
                    return self.replies.reply_text(message.message_id, result['report'], 'final', chat_id=message.chat_id)
                state = {'running': '运行中', 'completed': '已完成', 'failed': '失败'}.get(result.get('state'), result.get('state') or '未识别')
                return self.replies.reply_text(message.message_id, f'Deep Research 状态：{state}', 'status', chat_id=message.chat_id)
            result = self.chat_adapter.start_deep_research(binding, command.argument)
            self.store.update_chat_binding(message.chat_id, tab_id=result.get('tab_id'), url=result.get('url'))
            state = {'running': '运行中', 'completed': '已完成', 'failed': '失败'}.get(result.get('state'), result.get('state') or '运行中')
            return self.replies.reply_text(message.message_id, f'Deep Research 已启动：{state}', 'accepted', chat_id=message.chat_id)
        if selected_surface == 'chat' and command.name == 'image':
            binding = self.store.ensure_chat_binding(message.chat_id)
            if not command.argument or command.argument.strip().lower() == 'status':
                result = self.chat_adapter.image_status(binding)
                image = result.get('image') or {}
                if result.get('state') == 'completed' and image.get('src'):
                    downloaded = self.chat_adapter.download_generated_image(binding)
                    return self.replies.reply_image(message.message_id, downloaded['data'], 'image', chat_id=message.chat_id)
                state = {'running': '运行中', 'completed': '已完成', 'failed': '失败'}.get(result.get('state'), result.get('state') or '未识别')
                return self.replies.reply_text(message.message_id, f'图片生成状态：{state}', 'status', chat_id=message.chat_id)
            result = self.chat_adapter.start_image_generation(binding, command.argument)
            self.store.update_chat_binding(message.chat_id, tab_id=result.get('tab_id'), url=result.get('url'))
            state = {'running': '运行中', 'completed': '已完成', 'failed': '失败'}.get(result.get('state'), result.get('state') or '运行中')
            return self.replies.reply_text(message.message_id, f'图片生成已启动：{state}', 'accepted', chat_id=message.chat_id)
        if selected_surface == 'chat' and command.name == 'upload':
            if not command.argument:
                raise StructuredError('CHAT_UPLOAD_PATH_REQUIRED', '用法：/upload <绝对文件路径>')
            path = validate_file(command.argument, self.settings)
            binding = self.store.ensure_chat_binding(message.chat_id)
            result = self.chat_adapter.upload_file(binding, path)
            self.store.update_chat_binding(message.chat_id, tab_id=result.get('tab_id'), url=result.get('url'))
            return self.replies.reply_text(message.message_id, f'已附加文件：{result.get("name") or path.name}', 'status', chat_id=message.chat_id)
        if selected_surface == 'code' and command.name == 'upload':
            if not command.argument:
                raise StructuredError('CODEX_UPLOAD_PATH_REQUIRED', '用法：/upload <绝对文件路径>')
            path = validate_file(command.argument, self.settings)
            content_type = mimetypes.guess_type(path.name)[0] or ''
            kind = 'image' if content_type.startswith('image/') else 'file'
            self.store.add_pending_attachment(message.chat_id, path, kind, path.name)
            native = 'localImage' if kind == 'image' else 'mention'
            return self.replies.reply_text(
                message.message_id,
                f'已加入下一条 Codex 消息：{path.name}（native {native}）。',
                'status',
                chat_id=message.chat_id,
            )
        if command.name == 'whoami':
            return self.replies.reply_text(message.message_id, f'open_id: {message.sender_open_id}', 'status', chat_id=message.chat_id)
        if command.name == 'workspace':
            if command.argument:
                workspace = self._resolve_workspace(command.argument)
                if self.store.get_session(message.chat_id):
                    self.store.switch_to_pending_session(message.chat_id, message.chat_type, message.sender_open_id, workspace)
                    return self.replies.reply_text(
                        message.message_id,
                        f'已选择工作区：{workspace.name}。当前进入新线程准备状态。\n建议先用 /models、/model <编号>、/reasoning <档位> 完成预设，再发送第一条普通任务。',
                        'accepted',
                        chat_id=message.chat_id,
                    )
                return self._handle_command(message, ParsedCommand('new', str(workspace)))
            return self._workspace_list(message)
        if command.name == 'workspaces':
            return self._workspace_list(message)
        if command.name == 'new':
            session = self.store.get_session(message.chat_id)
            if command.argument:
                workspace = validate_workspace(command.argument, self.settings)
            else:
                workspace = None
                if session and session.thread_id:
                    binding = self.binding_store.get_binding(session.thread_id)
                    workspace = Path(binding.cwd) if binding else None
                if workspace is None and session and session.pending_cwd:
                    workspace = Path(session.pending_cwd)
                if workspace is None:
                    raise StructuredError('FEISHU_WORKSPACE_REQUIRED', '当前没有可复用的 Code workspace；请使用 /new <绝对路径>。')
                workspace = validate_workspace(workspace, self.settings)
            if session:
                self.store.switch_to_pending_session(message.chat_id, message.chat_type, message.sender_open_id, workspace)
            else:
                self.store.create_pending_session(message.chat_id, message.chat_type, message.sender_open_id, workspace)
            return self.replies.reply_text(message.message_id, f'已准备新的 CFR/Codex session：{workspace.name}。下一条普通任务会创建新的 native Codex thread。', 'accepted', chat_id=message.chat_id)
        if command.name == 'use':
            if not command.argument:
                raise StructuredError('FEISHU_THREAD_REQUIRED', '用法：/session <会话编号或线程 ID>')
            bindings = self.binding_store.list_bindings(limit=10)
            binding = self._resolve_binding(bindings, command.argument)
            if not binding:
                raise StructuredError('BINDING_NOT_FOUND', '未找到该 CFR 会话。')
            validate_workspace(binding.cwd, self.settings)
            current = self.store.get_session(message.chat_id)
            if current and current.thread_id == binding.thread_id:
                return self.replies.reply_text(message.message_id, f'当前聊天已经绑定该 CFR 线程：{binding.thread_name or binding.thread_id[-8:]}', 'status', chat_id=message.chat_id)
            if current:
                raise StructuredError('FEISHU_SESSION_ALREADY_EXISTS', '当前聊天已绑定其他 CFR 会话；如需切换，请先 /unbind。')
            self.store.create_pending_session(message.chat_id, message.chat_type, message.sender_open_id, binding.cwd)
            self.store.bind_session(message.chat_id, binding.thread_id)
            return self.replies.reply_text(message.message_id, f'已绑定 CFR 线程：{binding.thread_name or binding.thread_id[-8:]}', 'status', chat_id=message.chat_id)
        if command.name == 'unbind':
            self.store.unbind_session(message.chat_id)
            return self.replies.reply_text(
                message.message_id,
                '已解除当前飞书聊天与 Code 线程的绑定；Codex 线程未删除。重新绑定请先发送 /sessions，再使用 /session <编号|Thread ID 后缀>。',
                'status',
                chat_id=message.chat_id,
            )
        if command.name == 'status':
            return self.replies.reply_text(message.message_id, self._status_text(message.chat_id), 'status', chat_id=message.chat_id)
        if command.name == 'list':
            records = self.binding_store.list_bindings(limit=10)
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
            pending_session, pending_settings = self._pending_thread_settings(message)
            if not command.argument:
                if pending_session is not None:
                    selected = pending_settings.get('model') or '未单独指定（将使用 Codex 新线程默认值）'
                    return self.replies.reply_text(
                        message.message_id,
                        f'当前 session 尚未创建 native Codex thread。\n新线程预设模型：{selected}\n发送 /models 查看模型；此时用 /model <编号> 会在 thread/start 时一次性应用，不会先创建线程再热切换。',
                        'status',
                        chat_id=message.chat_id,
                    )
                started = time.monotonic()
                try:
                    _, state = self._thread_settings(message)
                    return self.replies.reply_text(message.message_id, self._settings_text(state['settings'], state.get('confirmed', True)) + '\n发送 /models 查看全部模型。', 'status', chat_id=message.chat_id)
                finally:
                    LOGGER.info('FEISHU_COMMAND_MODEL_READ_LATENCY elapsed_ms=%s', int((time.monotonic() - started) * 1000))
            model = self._resolve(self._catalog(), command.argument, ('model', 'id', 'display_name'))
            if not model:
                raise StructuredError('CONTROL_INVALID_CODEX_SETTING', '指定模型不在当前 Codex 模型列表中。')
            if pending_session is not None:
                self._save_pending_thread_settings(
                    message,
                    model=model['model'],
                    reasoning_effort=None,
                    service_tier=None,
                )
                return self.replies.reply_text(
                    message.message_id,
                    f'新线程预设模型：{model["model"]}。\n该设置会直接进入 thread/start；请在发送第一条普通任务前继续设置 /reasoning（如需要）。',
                    'status',
                    chat_id=message.chat_id,
                )
            session, _ = self._thread_settings(message)
            self._run_async(self.adapter.update_thread_settings(session.thread_id, model=model['model']))
            return self.replies.reply_text(
                message.message_id,
                f'当前线程模型已切换为：{model["model"]}\n提示：在线程已经开始使用后切换模型可能降低后续上下文缓存复用。新任务建议先 /workspace 或 /new，再在首条任务前 /model。',
                'status',
                chat_id=message.chat_id,
            )
        if command.name in {'reasoning', 'effort'}:
            defaults = self._defaults() if command.argument and command.argument.lower().startswith('default ') else None
            pending_session, pending_settings = self._pending_thread_settings(message) if defaults is None else (None, None)
            if not command.argument:
                if pending_session is not None:
                    model_identity = pending_settings.get('model')
                    if not model_identity:
                        return self.replies.reply_text(
                            message.message_id,
                            '当前 session 尚未创建 native thread，且还没有预设模型。\n请先发送 /models，再用 /model <编号> 选择模型；随后可用 /reasoning <档位> 在 thread/start 前预设推理强度。',
                            'status',
                            chat_id=message.chat_id,
                        )
                    model = self._current_thread_model(self._catalog(), model_identity)
                    if not model:
                        raise StructuredError('CONTROL_THREAD_MODEL_UNAVAILABLE', '新线程预设模型不在当前 Codex 模型列表中。')
                    efforts = model.get('supported_reasoning_efforts', [])
                    current_effort = pending_settings.get('reasoning_effort') or model.get('default_reasoning_effort') or '模型默认'
                    text = '\n'.join([f'新线程预设推理强度：{current_effort}', '当前预设模型支持：'] + [f'{index}. {item["reasoning_effort"]}' for index, item in enumerate(efforts, 1)])
                    return self.replies.reply_text(message.message_id, text, 'status', chat_id=message.chat_id)
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
            if pending_session is not None:
                model_identity = pending_settings.get('model')
                if not model_identity:
                    raise StructuredError('CONTROL_THREAD_MODEL_REQUIRED', '请先用 /model <编号> 预设新线程模型，再设置 /reasoning。')
                state = {'settings': {'model': model_identity, 'effort': pending_settings.get('reasoning_effort')}}
                session = None
            else:
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
            if pending_session is not None:
                self._save_pending_thread_settings(message, reasoning_effort=effort['reasoning_effort'])
                return self.replies.reply_text(message.message_id, f'新线程预设推理强度：{effort["reasoning_effort"]}。该设置会随 thread/start 一次性应用。', 'status', chat_id=message.chat_id)
            self._run_async(self.adapter.update_thread_settings(session.thread_id, effort=effort['reasoning_effort']))
            return self.replies.reply_text(message.message_id, f'当前线程推理强度已设置为：{effort["reasoning_effort"]}', 'status', chat_id=message.chat_id)
        if command.name in {'tiers', 'tier'}:
            pending_session, pending_settings = self._pending_thread_settings(message)
            if pending_session is not None:
                model_identity = pending_settings.get('model')
                if not model_identity:
                    raise StructuredError('CONTROL_THREAD_MODEL_REQUIRED', '请先用 /model <编号> 预设新线程模型，再设置服务层级。')
                session = None
                state = {'settings': {'model': model_identity, 'service_tier': pending_settings.get('service_tier')}}
            else:
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
                if pending_session is not None:
                    self._save_pending_thread_settings(message, service_tier=None)
                    return self.replies.reply_text(message.message_id, '新线程服务层级已恢复模型/运行时默认。', 'status', chat_id=message.chat_id)
                self._run_async(self.adapter.update_thread_settings(session.thread_id, service_tier=None))
                return self.replies.reply_text(message.message_id, '当前线程服务层级已恢复默认。', 'status', chat_id=message.chat_id)
            tier = self._resolve(tiers, command.argument, ('id', 'name'))
            if not tier:
                raise StructuredError('CONTROL_INVALID_CODEX_SETTING', '该模型不支持指定服务层级。')
            if pending_session is not None:
                self._save_pending_thread_settings(message, service_tier=tier['id'])
                return self.replies.reply_text(message.message_id, f'新线程预设服务层级：{tier["id"]}。', 'status', chat_id=message.chat_id)
            self._run_async(self.adapter.update_thread_settings(session.thread_id, service_tier=tier['id']))
            return self.replies.reply_text(message.message_id, f'当前线程服务层级已设置为：{tier["id"]}', 'status', chat_id=message.chat_id)
        if command.name in {'surfaces', 'surface'}:
            from cfr.surfaces import find_execution_surface
            catalog = self._surface_catalog(message.chat_id)
            surfaces = catalog['data']
            if command.name == 'surfaces' or not command.argument:
                lines = []
                for index, item in enumerate(surfaces, 1):
                    state = '当前' if item['id'] == catalog['selected'] else '可用' if item['available'] else '未接入'
                    lines.append(f'{index}. {item["name"]} · {state}')
                return self.replies.reply_text(
                    message.message_id,
                    '执行 Surface：\n' + '\n'.join(lines) + '\n/surface 与 Codex /mode 是两层不同的控制。',
                    'status',
                    chat_id=message.chat_id,
                )
            surface = find_execution_surface(command.argument)
            if not surface:
                return self.replies.reply_text(message.message_id, '未知执行 Surface。发送 /surfaces 查看 Chat / Work / Code。', 'status', chat_id=message.chat_id)
            surface = next(item for item in surfaces if item['id'] == surface['id'])
            if not surface['available']:
                return self.replies.reply_text(
                    message.message_id,
                    f'{surface["name"]} Surface 当前不可用：{surface.get("description") or surface.get("status") or "not connected"}；不会用 Codex 模拟。',
                    'status',
                    chat_id=message.chat_id,
                )
            setter = getattr(self.store, 'set_selected_surface', None)
            if setter:
                setter(message.chat_id, surface['id'])
            label = 'Chat（原生 ChatGPT 网页）' if surface['id'] == 'chat' else 'Code（原生 Codex）'
            guidance = (
                '\n进入 Chat 后先发送 /projects，再用 /project <编号|名称|Project ID|URL> 选择 Project；'
                '如明确需要普通 Chat，可在 /projects 列表中选择“普通 Chat”。'
                if surface['id'] == 'chat' else ''
            )
            return self.replies.reply_text(message.message_id, f'当前执行 Surface：{label}。{guidance}', 'status', chat_id=message.chat_id)
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
            existing = self.store.get_inbox(message.message_id)
            if existing is None:
                if not self.store.enqueue_message(queued):
                    raise StructuredError('FEISHU_REDIRECT_QUEUE_FAILED', '新的方向未能安全排队。')
            elif existing.status not in {'queued', 'running'} or not self.store.rewrite_queued_text(message.message_id, command.argument):
                raise StructuredError('FEISHU_REDIRECT_QUEUE_FAILED', '新的方向未能安全排队。')
            self.enqueue_for_chat(message.message_id, message.chat_id)
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
            if self._selected_surface(message.chat_id) == 'chat':
                binding = self.store.get_chat_binding(message.chat_id)
                if not binding:
                    return self.replies.reply_text(message.message_id, '当前 Chat Surface 还没有网页会话。', 'status', chat_id=message.chat_id)
                result = self.chat_adapter.stop(binding)
                return self.replies.reply_text(message.message_id, str(result.get('status') or 'STOP_REQUESTED'), 'status', chat_id=message.chat_id)
            session = self.store.get_session(message.chat_id)
            if not session or not session.thread_id:
                raise StructuredError('FEISHU_NO_ACTIVE_SESSION', 'No active CFR session')
            result = self._run_async(self.adapter.stop(session.thread_id))
            return self.replies.reply_text(message.message_id, str(result.get('status', 'STOP_REQUESTED')), 'status', chat_id=message.chat_id)
        unknown = command.argument if command.name == 'unknown' else f'/cfr {command.name}'
        return self.replies.reply_text(message.message_id, f'未知命令：{unknown}\n发送 /help 查看 CFR 使用说明。', 'status', chat_id=message.chat_id)

    def _workspace_list(self, message):
        roots = '\n'.join(f'{index}. {Path(root).name} | {root}' for index, root in enumerate(self.settings.allowed_workspace_roots, 1)) or '未配置。'
        session = self.store.get_session(message.chat_id)
        current = session.pending_cwd if session and session.pending_cwd else None
        if session and session.thread_id:
            binding = self.binding_store.get_binding(session.thread_id)
            current = str(binding.cwd) if binding else current
        return self.replies.reply_text(
            message.message_id,
            f'当前工作区：{current or "未绑定"}\n可选工作区：\n{roots}\n使用 /workspace <编号> 直接准备该工作区的新 Code thread；也仍支持 /workspace <绝对路径>。',
            'status',
            chat_id=message.chat_id,
        )

    def _handle_prompt(self, message, record=None):
        if self._selected_surface(message.chat_id) == 'chat':
            return self._handle_chat_prompt(message, record=record)
        return self._handle_code_prompt(message, record)

    def _handle_chat_prompt(self, message, prompt=None, record=None):
        binding = self.store.ensure_chat_binding(message.chat_id)
        attachments = self._claim_pending_attachments(message.chat_id, message.message_id)
        progress = FeishuChatProgress(self.replies, message.message_id, message.sender_open_id)
        run_started = time.monotonic()
        run_id = self._begin_chat_run(message, binding, len(attachments), record=record)

        def on_progress(value):
            value = value or {}
            trace = value.get('trace')
            if isinstance(trace, dict):
                self._update_chat_run(run_id, trace=trace, status='running', active=True, stage=trace.get('event') or 'browser')
            elif value.get('state'):
                state = str(value['state'])
                self._update_chat_run(run_id, stage=state, status='running', active=True)
            progress.on_progress(value)

        progress.start()
        try:
            with self._chat_browser_lock:
                result = self.chat_adapter.send_message(
                    binding,
                    message.text if prompt is None else prompt,
                    on_progress=on_progress,
                    file_paths=[Path(item['path']) for item in attachments] or None,
                )
        except Exception as error:
            identity_reader = getattr(self.chat_adapter, 'current_identity', None)
            if identity_reader is not None:
                try:
                    with self._chat_browser_lock:
                        identity = identity_reader(binding)
                    if identity.get('url'):
                        self.store.update_chat_binding(message.chat_id, tab_id=identity.get('tab_id'), url=identity.get('url'))
                except Exception:
                    pass
            self._update_chat_run(
                run_id,
                status='failed', active=False, stage='failed', completed_at=time.time(),
                current_owner='idle', total_ms=round(max(0.0, time.monotonic() - run_started) * 1000, 1),
                error_code=getattr(error, 'code', type(error).__name__),
            )
            progress.finish('failed')
            progress.wait()
            raise
        delivery_binding = {
            **binding,
            'tab_id': result.get('tab_id') or binding.get('tab_id'),
            'url': result.get('url') or binding.get('url'),
        }
        response_ids = []
        delivery_errors = []
        progress.on_progress({'state': 'delivering'})
        self._update_chat_run(run_id, stage='delivering', url=delivery_binding.get('url'))
        try:
            try:
                self.store.update_chat_binding(message.chat_id, tab_id=delivery_binding.get('tab_id'), url=delivery_binding.get('url'))
            except Exception as error:
                LOGGER.warning('CHAT_BINDING_UPDATE_FAILED type=%s', type(error).__name__)
            text = result.get('text')
            if text:
                started = time.monotonic()
                try:
                    response_ids.extend(self.replies.reply_text(message.message_id, text, 'final', chat_id=message.chat_id))
                    self._trace_chat_run(run_id, 'feishu_text_sent', 'feishu', started)
                except Exception as error:
                    delivery_errors.append(error)
                    self._trace_chat_run(run_id, 'feishu_text_failed', 'feishu', started, getattr(error, 'code', type(error).__name__))
            if result.get('generated_image_src'):
                started = time.monotonic()
                try:
                    self._update_chat_run(run_id, stage='image_downloading', current_owner='browser/openai')
                    LOGGER.info('CHAT_ARTIFACT_DOWNLOAD_STARTED type=image')
                    with self._chat_browser_lock:
                        downloaded = self.chat_adapter.download_generated_image(delivery_binding, result.get('generated_image'))
                    started = self._trace_chat_run(run_id, 'image_download_saved', 'browser/openai', started, f'{len(downloaded.get("data") or b"")} bytes')
                    self._update_chat_run(run_id, stage='image_sending', current_owner='feishu')
                    response_ids.extend(self.replies.reply_image(message.message_id, downloaded['data'], 'chat-image', chat_id=message.chat_id))
                    self._trace_chat_run(run_id, 'image_feishu_sent', 'feishu', started)
                except Exception as error:
                    delivery_errors.append(error)
                    self._trace_chat_run(run_id, 'image_delivery_failed', 'delivery', started, getattr(error, 'code', type(error).__name__))
            generated_files = result.get('generated_files') or ([result['generated_file']] if result.get('generated_file') else [])
            for index, generated_file in enumerate(generated_files):
                downloaded = None
                file_name = str((generated_file or {}).get('name') or f'file-{index + 1}')
                started = time.monotonic()
                try:
                    self._update_chat_run(run_id, stage='file_downloading', current_owner='browser/openai')
                    LOGGER.info('CHAT_ARTIFACT_DOWNLOAD_STARTED type=file name=%s', Path(file_name).name)
                    with self._chat_browser_lock:
                        downloaded = self.chat_adapter.download_generated_file_to_file(delivery_binding, generated_file)
                    saved_path = Path(downloaded['path'])
                    size = saved_path.stat().st_size if saved_path.is_file() else len(downloaded.get('data') or b'')
                    started = self._trace_chat_run(run_id, 'file_download_saved', 'browser/openai', started, f'{saved_path.name} · {size} bytes')
                    LOGGER.info('CHAT_ARTIFACT_DOWNLOAD_SAVED name=%s bytes=%s', saved_path.name, size)
                    self._update_chat_run(run_id, stage='file_sending', current_owner='feishu')
                    delivered = self.replies.reply_file(
                        message.message_id,
                        downloaded['path'],
                        f'chat-file-{index}',
                        chat_id=message.chat_id,
                    )
                    response_ids.extend(delivered)
                    self._trace_chat_run(run_id, 'file_feishu_sent', 'feishu', started, saved_path.name)
                    LOGGER.info('CHAT_ARTIFACT_FEISHU_SENT name=%s', saved_path.name)
                except Exception as error:
                    delivery_errors.append(error)
                    code = getattr(error, 'code', type(error).__name__)
                    self._trace_chat_run(run_id, 'file_delivery_failed', 'delivery', started, f'{Path(file_name).name} · {code}')
                    with self._chat_runs_lock:
                        run = next((item for item in self._chat_runs if item['id'] == run_id), None)
                        if run is not None:
                            run.setdefault('artifact_failures', []).append({'name': Path(file_name).name, 'error_code': code})
                    LOGGER.warning('CHAT_ARTIFACT_DELIVERY_FAILED name=%s code=%s', Path(file_name).name, code)
                finally:
                    if downloaded and downloaded.get('path'):
                        self._cleanup_chat_download(downloaded['path'])
            if delivery_errors and not response_ids:
                raise delivery_errors[0]
            if delivery_errors:
                try:
                    response_ids.extend(self.replies.reply_text(
                        message.message_id,
                        f'ChatGPT 已完成，但有 {len(delivery_errors)} 个结果未能通过飞书回传。可在 CFR 控制中心查看运行状态。',
                        'chat-delivery-warning',
                        chat_id=message.chat_id,
                    ))
                except Exception:
                    pass
            if not response_ids and not delivery_errors:
                response_ids.extend(self.replies.reply_text(
                    message.message_id,
                    'ChatGPT 已完成，但当前网页控制层没有读取到文本、图片或文件结果。',
                    'final',
                    chat_id=message.chat_id,
                ))
            try:
                self._clear_pending_attachments(message.chat_id, attachments)
            except Exception as error:
                # The ChatGPT turn and Feishu delivery are already complete.
                # Retaining input cache for a later maintenance retry is safer
                # than converting a successful user result into a failed job.
                LOGGER.warning('CHAT_ATTACHMENT_CLEANUP_FAILED type=%s', type(error).__name__)
        except Exception as error:
            self._update_chat_run(
                run_id,
                status='failed', active=False, stage='delivery_failed', completed_at=time.time(),
                output_count=len(response_ids), current_owner='idle',
                total_ms=round(max(0.0, time.monotonic() - run_started) * 1000, 1),
                error_code=getattr(error, 'code', type(error).__name__),
            )
            progress.finish('failed')
            progress.wait()
            raise
        identity = self.chat_adapter.parse_identity(delivery_binding.get('url')) if hasattr(self.chat_adapter, 'parse_identity') else {}
        self._update_chat_run(
            run_id,
            status='completed', active=False,
            stage='completed_with_warnings' if delivery_errors else 'completed',
            completed_at=time.time(), output_count=len(response_ids),
            conversation_id=identity.get('conversation_id'),
            error_code='CHAT_PARTIAL_DELIVERY' if delivery_errors else None,
            total_ms=round(max(0.0, time.monotonic() - run_started) * 1000, 1),
            current_owner='idle',
        )
        progress.finish('completed')
        progress.wait()
        return response_ids

    def _handle_code_prompt(self, message, record=None):
        runtime = getattr(self, 'code_runtime', None)
        if (
            runtime is None
            or runtime.store is not self.store
            or runtime.binding_store is not self.binding_store
            or runtime.adapter is not self.adapter
            or runtime.replies is not self.replies
            or runtime.approvals is not self.approvals
        ):
            runtime = self.code_runtime = CodeSurfaceRuntime(
                settings=getattr(self, 'settings', None),
                store=self.store,
                binding_store=self.binding_store,
                adapter=self.adapter,
                replies=self.replies,
                approvals=self.approvals,
            )
        return runtime.handle_prompt(message, record)

    @staticmethod
    def _path_within(path, root):
        return CodeArtifactCollector._within(path, root)

    def _codex_artifact_paths(self, text, workspace_path, started_at, excluded=()):
        collector = CodeArtifactCollector(getattr(self, 'adapter', None))
        return collector.collect(
            text=text,
            thread_id=None,
            workspace=workspace_path,
            started_at=started_at,
            excluded=excluded,
        )

    def _reply_codex_artifacts(self, message, text, workspace_path, started_at, excluded=()):
        response_ids = []
        for path in self._codex_artifact_paths(text, workspace_path, started_at, excluded):
            phase = f'codex-artifact-{uuid.uuid5(uuid.NAMESPACE_URL, str(path)).hex[:12]}'
            content_type = mimetypes.guess_type(path.name)[0] or ''
            if content_type.startswith('image/'):
                response_ids.extend(self.replies.reply_image(
                    message.message_id, path.read_bytes(), phase, chat_id=message.chat_id,
                ))
            elif content_type.startswith('video/') or path.suffix.lower() == '.mp4':
                response_ids.extend(self.replies.reply_video(
                    message.message_id, path, phase, chat_id=message.chat_id,
                ))
            else:
                response_ids.extend(self.replies.reply_file(
                    message.message_id, path, phase, chat_id=message.chat_id,
                ))
        return response_ids

    @staticmethod
    def _terminal_turn_text(turn):
        return CodeSurfaceRuntime.terminal_turn_text(turn)

    def _finalize_turn_result(self, result):
        """Send the real adapter terminal result through the single bridge finalizer."""
        runtime = getattr(self, 'code_runtime', None)
        if runtime is not None and runtime.approvals is self.approvals:
            return runtime.finalize_turn_result(result)
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
        if self._selected_surface(chat_id) == 'chat':
            health = self._chat_health()
            binding_getter = getattr(self.store, 'get_chat_binding', None)
            binding = binding_getter(chat_id) if binding_getter else None
            browser_status = {
                'ready': '就绪',
                'not_connected': '未连接',
                'waiting_user': '等待人工处理',
                'page_not_ready': '页面未就绪',
                'rate_limited': '请求受限',
            }.get(str(health.get('status') or ''), str(health.get('status') or '未识别'))
            return '\n'.join([
                'CFR 状态',
                'Surface：Chat',
                f'浏览器控制：{browser_status}',
                f'标签页：{binding.get("tab_id") if binding and binding.get("tab_id") else "待确认"}',
                f'URL：{binding.get("url") if binding and binding.get("url") else health.get("url") or "无"}',
            ])
        session = self.store.get_session(chat_id)
        if not session:
            return 'CFR 状态\n会话：未绑定'
        binding = self.binding_store.get_binding(session.thread_id) if session.thread_id else None
        registry = getattr(self.adapter, 'registry', None)
        active = registry.get(session.thread_id) if registry is not None and session.thread_id else None
        rollout_size = None
        if binding and binding.rollout_path:
            try:
                rollout_size = Path(binding.rollout_path).stat().st_size
            except OSError:
                pass
        context_percent = None
        if registry is not None and session.thread_id:
            snapshot = getattr(registry, 'runtime_snapshot', lambda: {})()
            for item in snapshot.get('telemetry', ()):
                if item.get('thread_id') == session.thread_id and item.get('context_usage_percent') is not None:
                    context_percent = item['context_usage_percent']
                    break
        session_state = {
            'pending_initial': '待创建线程',
            'bound': '已绑定',
            'unbound': '未绑定',
        }.get(str(session.state), str(session.state))
        desktop_state = {
            'up_to_date': '已同步',
            'desktop_ahead': 'Desktop 有更新',
            'cfr_ahead': 'CFR 有更新',
            'diverged': '已分叉',
        }.get(str(binding.desktop_sync_state if binding else 'up_to_date'), str(binding.desktop_sync_state if binding else 'up_to_date'))
        lines = [
            'CFR 状态',
            f'工作区：{binding.cwd.name if binding else Path(session.pending_cwd).name if session.pending_cwd else "无"}',
            f'会话：{session_state}',
            f'线程：{session.thread_id[-8:] if session.thread_id else "待创建"}',
            f'活动 Turn：{"有" if active else "无"}',
            f'Desktop 同步：{desktop_state}',
        ]
        if not session.thread_id and getattr(session, 'state', None) == 'pending_initial':
            getter = getattr(self.store, 'get_pending_thread_settings', None)
            pending = getter(chat_id) if getter else {}
            lines.extend([
                f'新线程模型：{pending.get("model") or "Codex 默认"}',
                f'新线程推理强度：{pending.get("reasoning_effort") or "模型默认"}',
                f'新线程服务层级：{pending.get("service_tier") or "默认"}',
            ])
        if rollout_size is not None:
            lines.append(f'原生 rollout：{rollout_size / (1024 * 1024):.1f} MB')
        if context_percent is not None:
            lines.append(f'上下文占用：{context_percent:.1f}%')
            if context_percent >= 80:
                lines.append('上下文压力：高；继续在同一 thread 增加轮次通常会增加模型上下文开销，建议完成当前工作后 /new。')
        return '\n'.join(lines)

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

    def consume_user_input_message(self, message):
        """Resolve a blocking Codex request_user_input outside the per-chat worker queue."""
        return self.approvals.consume_user_input_message(message)

    def handle_card_action(self, payload):
        if not isinstance(payload, dict):
            raise StructuredError('FEISHU_CARD_ACTION_INVALID', 'Card action payload is invalid')
        event = payload.get('event') or payload
        if not isinstance(event, dict):
            raise StructuredError('FEISHU_CARD_ACTION_INVALID', 'Card action event is invalid')
        action = payload.get('action') or event.get('action') or {}
        value = action.get('value') if isinstance(action, dict) else {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        operator_value = event.get('operator') or {}
        operator = (
            operator_value.get('open_id') if isinstance(operator_value, dict) else None
        ) or payload.get('operator_open_id')
        if not isinstance(value, dict) or not value.get('approval_id') or not value.get('action') or not operator:
            raise StructuredError('FEISHU_CARD_ACTION_INVALID', 'Card action is missing required fields')
        return self.approvals.resolve(value.get('approval_id'), operator, value.get('action'))

    def stop(self):
        if not self._started:
            return
        self.stop_event.set()
        self.approvals.cancel_all_pending()
        registry = getattr(self.adapter, 'registry', None)
        active = list(getattr(registry, '_active', {})) if registry is not None else []
        for thread_id in active:
            try:
                self._run_async(self.adapter.stop(thread_id))
            except Exception:
                pass
        # Keep the outbound transport and SQLite stores alive while active
        # workers unwind.  Closing them first is what turned a completed Codex
        # turn into late FEISHU_API_NOT_CONNECTED/coroutine warnings.
        deadline = time.monotonic() + 20
        for worker in self._workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        alive = [worker.name for worker in self._workers if worker.is_alive()]
        if alive:
            raise StructuredError(
                'FEISHU_DAEMON_STOP_TIMEOUT',
                f'Feishu workers are still draining: {", ".join(alive)}; runtime resources were kept open',
            )
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)
        # Workers can finalize approval feedback while unwinding interrupted
        # turns. Keep the feedback executor alive until all workers exit, but
        # never let an optional executor/chat/store close skip transport or
        # durable lease cleanup.
        cleanup_errors = []
        critical_errors = []

        def cleanup(label, operation, *, critical=False, require_success=False):
            try:
                result = operation()
                if require_success and result is False:
                    raise RuntimeError(f'{label} cleanup returned false')
                return result
            except Exception as exc:
                cleanup_errors.append((label, exc))
                if critical:
                    critical_errors.append((label, exc))
                LOGGER.warning('FEISHU_DAEMON_CLEANUP_FAILED stage=%s type=%s', label, type(exc).__name__)
                return None

        cleanup('approvals', self.approvals.close)
        cleanup('transport', self.transport.stop, critical=True)
        close_chat = getattr(self.chat_adapter, 'close', None)
        if self._owns_chat_adapter and close_chat is not None:
            cleanup('chat', close_chat)
        cleanup(
            'lease',
            lambda: self.store.release_daemon_lease(self.lease_key, self.instance_id),
            critical=True,
        )
        if critical_errors:
            stages = ', '.join(label for label, _ in critical_errors)
            raise StructuredError(
                'FEISHU_DAEMON_STOP_CLEANUP_FAILED',
                f'Feishu daemon critical cleanup failed at: {stages}',
            ) from critical_errors[0][1]
        cleanup('store', self.store.close)
        if hasattr(self.binding_store, 'close'):
            cleanup('bindings', self.binding_store.close)
        self._started = False
