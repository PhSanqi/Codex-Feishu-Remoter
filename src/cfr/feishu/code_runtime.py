from __future__ import annotations

from datetime import datetime
import asyncio
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid
from urllib.parse import unquote, urlparse

from cfr.config import resolve_cfr_codex_home
from cfr.codex.permissions import native_permission_settings
from cfr.core.models import ConversationResult, StructuredError, TurnResult

from .credentials import resolve_cfr_config_dir
from .models import FeishuExecutionContext
from .progress import FeishuTurnProgress
from .security import validate_workspace


LOGGER = logging.getLogger(__name__)

DELIVERABLE_SUFFIXES = {
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.svg',
    '.mp4', '.mov', '.avi', '.mkv', '.webm',
    '.pdf', '.csv', '.xlsx', '.xls', '.docx', '.pptx', '.zip', '.7z', '.tar', '.gz',
}
IGNORED_SCAN_DIRS = {
    '.git', '.hg', '.svn', 'node_modules', '__pycache__', '.pytest_cache',
    '.venv', 'venv', 'env', '.mypy_cache', '.ruff_cache',
}
MAX_DISCOVERED_ARTIFACTS = 12
MAX_WORKSPACE_SCAN_FILES = 20_000
MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024


class CodeArtifactCollector:
    """Find trusted deliverables independently from the model's final prose."""

    def __init__(self, adapter):
        resolved = getattr(adapter, 'resolved_cfr_codex_home', None)
        self.codex_home = Path(getattr(resolved, 'path', '') or resolve_cfr_codex_home().path).resolve()

    @staticmethod
    def _within(path, root):
        try:
            return os.path.commonpath([
                os.path.normcase(str(Path(path).resolve())),
                os.path.normcase(str(Path(root).resolve())),
            ]) == os.path.normcase(str(Path(root).resolve()))
        except (OSError, ValueError):
            return False

    def _generated_root(self, thread_id):
        if not thread_id:
            return None
        return (self.codex_home / 'generated_images' / str(thread_id)).resolve()

    @staticmethod
    def _explicit_targets(text):
        text = str(text or '')
        targets = list(re.findall(r'\[[^\]\n]*\]\(([^)\n]+)\)', text))
        for body in re.findall(r':codex-file-citation\{([^}\n]+)\}', text, re.IGNORECASE):
            match = re.search(r'\bpath\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s}]+))', body, re.IGNORECASE)
            if match:
                targets.append(next(value for value in match.groups() if value is not None))
        for value in re.findall(r'`([^`\n]+)`', text):
            if Path(value.strip()).suffix.lower() in DELIVERABLE_SUFFIXES:
                targets.append(value.strip())
        suffixes = '|'.join(re.escape(value[1:]) for value in sorted(DELIVERABLE_SUFFIXES, key=len, reverse=True))
        bare_pattern = re.compile(
            rf'(?P<path>(?:[A-Za-z]:[\\/]|(?:\.?\.?[\\/])|\\(?!\\))[^\r\n<>"|]+?\.(?:{suffixes}))(?=$|[\s`\])>,;，。])',
            re.IGNORECASE,
        )
        targets.extend(match.group('path').strip() for match in bare_pattern.finditer(text))
        return targets

    @staticmethod
    def user_visible_text(text):
        """Remove Codex host-only attachment markers from the Feishu prose."""
        return re.sub(r'\s*:codex-file-citation\{[^}\n]+\}\s*', '\n', str(text or ''), flags=re.IGNORECASE).strip()

    def _resolve_explicit(self, raw_target, workspace, allowed_roots, cutoff):
        target = unquote(str(raw_target or '').strip().strip('<>'))
        if not target:
            return None
        # A Linux path may be rendered with Windows separators by a remote
        # model/host (for example ``\\tmp\\...\\report.xlsx``).  On POSIX,
        # normalize that representation before Path resolution.  Drive and
        # UNC paths remain untouched so they are never reinterpreted as local
        # POSIX authority.
        if os.name != 'nt' and target.startswith('\\') and not target.startswith('\\\\'):
            target = target.replace('\\', '/')
        if not re.match(r'^[A-Za-z]:[\\/]', target):
            parsed = urlparse(target)
            if parsed.scheme.lower() == 'file':
                target = unquote(parsed.path)
                if re.match(r'^/[A-Za-z]:/', target):
                    target = target[1:]
            elif parsed.scheme:
                return None
        candidate = Path(target)
        if not candidate.is_absolute():
            if workspace is None:
                return None
            candidate = workspace / candidate
        try:
            path = candidate.resolve(strict=True)
            stat = path.stat()
        except (OSError, RuntimeError):
            return None
        if not path.is_file() or path.suffix.lower() not in DELIVERABLE_SUFFIXES:
            return None
        if not any(self._within(path, root) for root in allowed_roots if root is not None):
            return None
        in_generated = allowed_roots[-1] is not None and self._within(path, allowed_roots[-1])
        if not in_generated and stat.st_mtime < cutoff:
            # Explicit paths in model prose are not authority to send an old
            # workspace/temp file. Only fresh turn outputs are auto-delivered;
            # durable Codex-generated artifacts keep their native trust root.
            # Existing user-selected local files remain available through the
            # explicit /upload path instead of being inferred from model text.
            return None
        return path

    @staticmethod
    def _fresh_files(root, cutoff):
        if root is None or not root.is_dir():
            return []
        found = []
        examined = 0
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            dirs[:] = [
                name for name in dirs
                if name not in IGNORED_SCAN_DIRS
                and not (current_path.name == '.cfr' and name == 'attachments')
            ]
            for name in files:
                examined += 1
                if examined > MAX_WORKSPACE_SCAN_FILES:
                    return found
                path = current_path / name
                if path.suffix.lower() not in DELIVERABLE_SUFFIXES:
                    continue
                try:
                    if path.stat().st_mtime >= cutoff:
                        found.append(path.resolve())
                except OSError:
                    continue
        return found

    @staticmethod
    def _latest_image(root):
        if root is None or not root.is_dir():
            return None
        candidates = []
        for path in root.iterdir():
            if not path.is_file() or path.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:
                continue
            try:
                candidates.append((path.stat().st_mtime, path.resolve()))
            except OSError:
                continue
        return max(candidates, default=(None, None))[1]

    def collect(self, *, text, thread_id, workspace, started_at, excluded=(), request_text=''):
        workspace = Path(workspace).resolve() if workspace else None
        generated_root = self._generated_root(thread_id)
        temp_root = Path(tempfile.gettempdir()).resolve()
        allowed_roots = (workspace, temp_root, generated_root)
        cutoff = float(started_at or time.time()) - 3.0
        excluded_keys = {
            os.path.normcase(str(Path(path).resolve()))
            for path in excluded
            if path is not None
        }
        results = []
        seen = set()

        def add(path):
            if path is None:
                return
            key = os.path.normcase(str(Path(path).resolve()))
            if key in excluded_keys or key in seen:
                return
            if workspace is not None and self._within(path, workspace / '.cfr' / 'attachments'):
                return
            seen.add(key)
            results.append(Path(path).resolve())

        for target in self._explicit_targets(text):
            add(self._resolve_explicit(target, workspace, allowed_roots, cutoff))

        # Generated images are a native Codex artifact channel.  They do not
        # necessarily appear as a Markdown link or even in the final text.
        for path in self._fresh_files(generated_root, cutoff):
            add(path)

        # A "send that image again" turn can legitimately finish with an empty
        # assistant message and only view an image generated by an earlier turn.
        image_requested = bool(re.search(
            r'图片|图像|照片|jpg|jpeg|png|webp|image|photo|picture',
            str(request_text or ''),
            re.IGNORECASE,
        ))
        if not results and (not str(text or '').strip() or image_requested):
            add(self._latest_image(generated_root))

        return results[:MAX_DISCOVERED_ARTIFACTS]


class CodeSurfaceRuntime:
    """Own one Code Surface turn from admission through final delivery."""

    def __init__(self, *, settings, store, binding_store, adapter, replies, approvals):
        self.settings = settings
        self.store = store
        self.binding_store = binding_store
        self.adapter = adapter
        self.replies = replies
        self.approvals = approvals
        self.artifacts = CodeArtifactCollector(adapter)

    @staticmethod
    def run_async(awaitable):
        return asyncio.run(awaitable)

    @staticmethod
    def prepare_attachments(attachments, workspace):
        workspace = Path(workspace).resolve(strict=True)
        native_inputs = []
        file_paths = []
        staged_paths = []
        try:
            for item in attachments:
                source = Path(item['path']).resolve(strict=True)
                if item.get('kind') == 'image':
                    native_inputs.append({'type': 'localImage', 'path': str(source)})
                    continue
                try:
                    relative = source.relative_to(workspace)
                except ValueError:
                    destination = workspace / '.cfr' / 'attachments'
                    destination.mkdir(parents=True, exist_ok=True)
                    name = Path(item.get('name') or source.name).name
                    prefix = str(item.get('attachment_id') or uuid.uuid4().hex)[:8]
                    target = destination / f'{prefix}-{name}'
                    shutil.copy2(source, target)
                    relative = target.relative_to(workspace)
                    staged_paths.append(target)
                file_paths.append(relative.as_posix())
        except Exception:
            for path in staged_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            destination = workspace / '.cfr' / 'attachments'
            try:
                destination.rmdir()
            except OSError:
                pass
            raise
        return native_inputs, file_paths, staged_paths

    @staticmethod
    def prompt_with_files(text, file_paths):
        text = str(text or '').strip()
        if not file_paths:
            return text
        listing = '\n'.join(f'- {path}' for path in file_paths)
        context = f'CFR provided local files in the current workspace:\n{listing}\nRead these files as needed for the user request.'
        return f'{text}\n\n{context}' if text else context

    def pending_attachments(self, chat_id):
        getter = getattr(self.store, 'list_pending_attachments', None)
        return getter(chat_id) if getter else []

    def claim_attachments(self, chat_id, message_id):
        claimer = getattr(self.store, 'claim_pending_attachments', None)
        return claimer(chat_id, message_id) if claimer else self.pending_attachments(chat_id)

    def clear_attachments(self, chat_id, attachments, extra_paths=()):
        clearer = getattr(self.store, 'clear_pending_attachments', None)
        if clearer and attachments:
            try:
                clearer(chat_id, [item['attachment_id'] for item in attachments])
            except Exception as exc:
                LOGGER.warning('CODE_ATTACHMENT_STATE_CLEANUP_FAILED type=%s', type(exc).__name__)
                return False
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
                if self.artifacts._within(path.parent, inbox):
                    self._remove_empty_parents(path.parent, inbox)
                elif path.parent.name == 'attachments' and path.parent.parent.name == '.cfr':
                    try:
                        path.parent.rmdir()
                    except OSError:
                        pass
            except OSError as exc:
                LOGGER.warning('FEISHU_ATTACHMENT_CACHE_CLEANUP_FAILED file=%s type=%s', path.name, type(exc).__name__)
        return True

    @staticmethod
    def _remove_empty_parents(parent, root):
        root = Path(root).resolve()
        current = Path(parent).resolve()
        while current != root:
            try:
                current.relative_to(root)
                current.rmdir()
            except (OSError, ValueError):
                break
            current = current.parent

    def finalize_turn_result(self, result):
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

    @staticmethod
    def terminal_turn_text(turn):
        if turn.status == 'timeout':
            return '任务执行超时，未正常完成。'
        if turn.status == 'interrupted':
            return '任务已停止，未正常完成。'
        return '任务未正常完成。'

    def _send_artifact(self, message, path):
        phase = f'codex-artifact-{uuid.uuid5(uuid.NAMESPACE_URL, str(path)).hex[:12]}'
        content_type = mimetypes.guess_type(path.name)[0] or ''
        if content_type.startswith('image/'):
            try:
                if path.stat().st_size > MAX_INLINE_IMAGE_BYTES:
                    return self.replies.reply_file(message.message_id, path, phase, chat_id=message.chat_id)
            except OSError:
                pass
            return self.replies.reply_image(message.message_id, path.read_bytes(), phase, chat_id=message.chat_id)
        if content_type.startswith('video/') or path.suffix.lower() in {'.mp4', '.mov', '.avi', '.mkv', '.webm'}:
            return self.replies.reply_video(message.message_id, path, phase, chat_id=message.chat_id)
        return self.replies.reply_file(message.message_id, path, phase, chat_id=message.chat_id)

    def deliver_final(self, message, turn, workspace, excluded=(), request_text=''):
        registry = getattr(self.adapter, 'registry', None)
        telemetry = getattr(turn, 'telemetry', None)
        if telemetry is not None:
            telemetry.mark('final_reply_started_at')
        response_ids = []
        errors = []
        try:
            text = str(getattr(turn, 'final_agent_message', '') or '')
            paths = self.artifacts.collect(
                text=text,
                thread_id=getattr(turn, 'thread_id', None),
                workspace=workspace,
                started_at=getattr(turn, 'started_at', None),
                excluded=excluded,
                request_text=request_text,
            )
            display_text = self.artifacts.user_visible_text(text)
            if display_text:
                try:
                    response_ids.extend(self.replies.reply_text(message.message_id, display_text, 'final', chat_id=message.chat_id))
                except Exception as exc:
                    errors.append(exc)
            for path in paths:
                try:
                    response_ids.extend(self._send_artifact(message, path))
                except Exception as exc:
                    errors.append(exc)
                    LOGGER.warning('CODE_ARTIFACT_DELIVERY_FAILED file=%s type=%s', path.name, type(exc).__name__)
            if not response_ids and not errors:
                response_ids.extend(self.replies.reply_text(
                    message.message_id,
                    'Codex 已完成，但本轮没有可回传的文本或文件。',
                    'final',
                    chat_id=message.chat_id,
                ))
            if errors and not response_ids:
                raise errors[0]
            if errors:
                try:
                    response_ids.extend(self.replies.reply_text(
                        message.message_id,
                        f'Codex 已完成，但有 {len(errors)} 个结果未能通过飞书回传。可在 CFR 控制中心查看运行记录。',
                        'delivery-warning',
                        chat_id=message.chat_id,
                    ))
                except Exception:
                    pass
            return response_ids
        finally:
            if telemetry is not None:
                telemetry.mark('final_reply_completed_at')
                if registry is not None:
                    registry.observe(telemetry)

    def handle_prompt(self, message, record=None):
        session = self.store.get_session(message.chat_id)
        if not session:
            return self.replies.reply_text(
                message.message_id,
                'No CFR session. Use /cfr help, then /cfr new <workspace>.',
                'status',
                chat_id=message.chat_id,
            )
        if session.state == 'pending_initial':
            if not session.pending_cwd:
                raise StructuredError('FEISHU_SESSION_INVALID', '待创建的 Code session 缺少工作区，请重新使用 /workspace 或 /new。')
            workspace_path = validate_workspace(session.pending_cwd, self.settings)
        else:
            if not session.thread_id:
                raise StructuredError('FEISHU_NO_ACTIVE_SESSION', '当前聊天没有已绑定的 CFR 线程，请使用 /workspace 或 /new。')
            binding = self.binding_store.get_binding(session.thread_id)
            if binding is None:
                raise StructuredError('CFR_BINDING_MISSING', '当前 session 指向的 CFR/Codex binding 不存在，请重新选择工作区或会话。')
            workspace_path = validate_workspace(binding.cwd, self.settings)
        context = FeishuExecutionContext(
            message.message_id, message.chat_id, message.sender_open_id, session.thread_id,
            cwd=str(workspace_path),
        )
        approval_mode_getter = getattr(self.store, 'get_approval_mode', None)
        approval_mode = approval_mode_getter(message.chat_id) if approval_mode_getter else 'ask'
        permission_settings = native_permission_settings(approval_mode or 'ask')
        attachments = self.claim_attachments(message.chat_id, message.message_id)
        workspace = workspace_path.name if workspace_path else None
        native_attachments, file_paths, staged_paths = (
            self.prepare_attachments(attachments, workspace_path)
            if attachments and workspace_path else ([], [], [])
        )
        attachment_kwargs = {'attachments': native_attachments} if native_attachments else {}
        prompt = self.prompt_with_files(message.text or '', file_paths)
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
        excluded = [Path(item['path']) for item in attachments] + list(staged_paths)
        progress = FeishuTurnProgress(self.replies, message.message_id, message.sender_open_id, workspace)
        progress.start()

        if session.state == 'pending_initial':
            name = f'Feishu-{Path(session.pending_cwd).name}-{datetime.now().strftime("%Y%m%d-%H%M")}'
            pending_settings_getter = getattr(self.store, 'get_pending_thread_settings', None)
            pending_settings = pending_settings_getter(message.chat_id) if pending_settings_getter else {}
            pending_settings = {**pending_settings, **permission_settings}
            thread_settings_kwargs = {'thread_settings': pending_settings} if pending_settings else {}
            try:
                result = self.run_async(self.adapter.create_conversation(
                    Path(session.pending_cwd), name, prompt,
                    on_server_request=lambda request: self.approvals.handle_server_request(request, context),
                    on_server_notification=getattr(self.approvals, 'handle_server_notification', None),
                    on_progress=progress.on_progress,
                    telemetry=telemetry,
                    **attachment_kwargs,
                    **thread_settings_kwargs,
                ))
            except StructuredError as exc:
                self.finalize_turn_result(exc.data)
                progress.finish(exc.data.initial_turn if isinstance(exc.data, ConversationResult) else exc.data)
                raise
            except Exception:
                progress.finish('failed')
                raise
            turn = result.initial_turn
            self.finalize_turn_result(result)
            if turn.status != 'completed':
                terminal = TurnResult(turn.thread_id, turn.turn_id, turn.status, self.terminal_turn_text(turn), turn.started_at, turn.completed_at, turn.error_message, turn.telemetry)
                try:
                    response_ids = self.deliver_final(message, terminal, workspace_path, excluded, request_text=message.text or '')
                    self.clear_attachments(message.chat_id, attachments, staged_paths)
                    return response_ids
                finally:
                    progress.finish(turn)
            self.store.bind_session(message.chat_id, result.thread_id)
            try:
                response_ids = self.deliver_final(message, turn, workspace_path, excluded, request_text=message.text or '')
                self.clear_attachments(message.chat_id, attachments, staged_paths)
                return response_ids
            finally:
                progress.finish(turn)

        try:
            result = self.run_async(self.adapter.send_message(
                session.thread_id,
                prompt,
                on_server_request=lambda request: self.approvals.handle_server_request(request, context),
                on_server_notification=getattr(self.approvals, 'handle_server_notification', None),
                on_progress=progress.on_progress,
                telemetry=telemetry,
                permission_settings=permission_settings,
                **attachment_kwargs,
            ))
        except StructuredError as exc:
            self.finalize_turn_result(exc.data)
            progress.finish(exc.data)
            raise
        except Exception:
            progress.finish('failed')
            raise
        self.finalize_turn_result(result)
        if result.status != 'completed':
            terminal = TurnResult(result.thread_id, result.turn_id, result.status, self.terminal_turn_text(result), result.started_at, result.completed_at, result.error_message, result.telemetry)
            try:
                response_ids = self.deliver_final(message, terminal, workspace_path, excluded, request_text=message.text or '')
                self.clear_attachments(message.chat_id, attachments, staged_paths)
                return response_ids
            finally:
                progress.finish(result)
        try:
            response_ids = self.deliver_final(message, result, workspace_path, excluded, request_text=message.text or '')
            self.clear_attachments(message.chat_id, attachments, staged_paths)
            return response_ids
        finally:
            progress.finish(result)
