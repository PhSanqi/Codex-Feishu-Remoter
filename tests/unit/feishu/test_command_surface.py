import asyncio
import gc
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cfr.codex.binding import CodexAdapter
from cfr.feishu.commands import CHAT_HELP_TEXT, CODE_HELP_TEXT, CommandParser
from cfr.feishu.daemon import FeishuDaemon
from cfr.feishu.config import FeishuSettings
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.transport import ChannelFeishuTransport
from cfr.core.models import StructuredError
from cfr.storage.db import BindingStore


CATALOG = {'available': True, 'data': [
    {'id': 'one', 'model': 'model-one', 'display_name': 'One', 'supported_reasoning_efforts': [{'reasoning_effort': 'low'}, {'reasoning_effort': 'high'}], 'service_tiers': [{'id': 'priority', 'name': 'Fast'}]},
    {'id': 'two', 'model': 'model-two', 'display_name': 'Two', 'supported_reasoning_efforts': [{'reasoning_effort': 'medium'}], 'service_tiers': []},
]}


class _Replies:
    def __init__(self): self.text = []; self.images = []; self.files = []; self.videos = []; self.cards = []; self.card_updates = []
    def reply_text(self, _id, text, *_args, **_kwargs): self.text.append(text); return ['reply']
    def reply_image(self, _id, image, *_args, **_kwargs): self.images.append(bytes(image)); return ['image-reply']
    def reply_file(self, _id, path, *_args, **_kwargs): self.files.append(Path(path)); return ['file-reply']
    def reply_video(self, _id, path, *_args, **_kwargs): self.videos.append(Path(path)); return ['video-reply']
    def send_card(self, _id, _open_id, card, phase='progress'): self.cards.append((phase, card)); return 'card-1'
    def update_card(self, _id, card): self.card_updates.append(card); return _id


class _Adapter:
    def __init__(self):
        self.updates = []
        self.steers = []
        self.state = {'model': 'model-one', 'effort': 'low', 'service_tier': None, 'collaboration_mode': None}
        self.native_model_identity = {'model-two': 'Two'}

    async def read_thread_settings(self, _thread): return {'settings': dict(self.state)}

    async def update_thread_settings(self, thread, **values):
        self.updates.append((thread, values))
        self.state.update(values)
        if 'model' in values:
            self.state['model'] = self.native_model_identity.get(values['model'], values['model'])
        return {'settings': dict(self.state)}
    async def steer(self, thread, text): self.steers.append((thread, text)); return {'turnId': 'turn-1'}


class _ChatAdapter:
    def __init__(self):
        self.available = False
        self.sent = []
        self.sent_files = []
        self.new = []
        self.stopped = []
        self.searches = []
        self.deep_research = []
        self.images = []
        self.uploads = []
        self.upload_batches = []
        self.scheduled_actions = []
        self.history_limits = []
        self.downloaded_files = []
        self.reasoning = {'label': 'High', 'index': 3, 'total': 4}
        self.model_changes = []
        self.chat_models = [
            {'name': 'GPT-5.6 Sol', 'selected': True, 'disabled': False},
            {'name': 'GPT-5.5', 'selected': False, 'disabled': False},
            {'name': 'Pro', 'selected': False, 'disabled': True},
        ]
        self.tasks = [{
            'task_id': 'task-1', 'title': 'Daily brief', 'detail': '每天 · 下次运行 12小时后',
            'paused': False, 'can_pause': True, 'can_edit': True, 'has_more': True,
        }]

    def health(self):
        return {'available': self.available, 'status': 'ready' if self.available else 'not_connected', 'description': 'test chat transport'}

    def send_message(self, binding, text, *, on_progress=None, file_paths=None):
        self.sent.append((dict(binding), text))
        self.sent_files.append([Path(path) for path in (file_paths or [])])
        if on_progress:
            on_progress({'state': 'generating', 'reasoning_text': 'visible reasoning', 'answer_preview': 'partial'})
        return {'text': 'chat answer', 'tab_id': 'tab-1', 'url': 'https://chatgpt.com/c/chat-1'}

    def send_search(self, binding, text):
        self.searches.append((dict(binding), text))
        return {'text': 'search answer', 'tab_id': 'tab-search', 'url': 'https://chatgpt.com/c/search-1'}

    def start_deep_research(self, binding, text):
        self.deep_research.append((dict(binding), text))
        return {'state': 'running', 'tab_id': 'tab-research', 'url': 'https://chatgpt.com/c/research-1'}

    def deep_research_status(self, binding):
        return {'state': 'completed', 'report': 'research report', 'tab_id': 'tab-research', 'url': 'https://chatgpt.com/c/research-1'}

    def start_image_generation(self, binding, text):
        self.images.append((dict(binding), text))
        return {'state': 'running', 'tab_id': 'tab-image', 'url': 'https://chatgpt.com/c/image-1'}

    def image_status(self, binding):
        return {'state': 'completed', 'tab_id': 'tab-image', 'url': 'https://chatgpt.com/c/image-1', 'image': {
            'src': 'https://chatgpt.com/backend-api/estuary/content?id=file-1',
            'alt': '已生成图片：test', 'width': 1024, 'height': 1024,
        }}

    def download_generated_image(self, binding, image=None):
        return {'data': b'png-bytes', 'content_type': 'image/png', 'source': 'https://chatgpt.com/backend-api/estuary/content?id=file-1', 'alt': 'test'}

    def download_generated_file_to_file(self, binding, file=None):
        path = Path((file or {}).get('path') or 'generated.xlsx')
        self.downloaded_files.append((dict(binding), dict(file or {})))
        return {'path': str(path), 'name': path.name, 'data': b'file'}

    def upload_file(self, binding, path):
        self.uploads.append((dict(binding), Path(path)))
        return {'tab_id': 'tab-upload', 'url': 'https://chatgpt.com/c/upload-1', 'name': Path(path).name}

    def upload_files(self, binding, paths):
        paths = [Path(path) for path in paths]
        self.upload_batches.append((dict(binding), paths))
        return {'tab_id': 'tab-upload', 'url': 'https://chatgpt.com/c/upload-1', 'names': [path.name for path in paths]}

    def new_conversation(self, binding):
        self.new.append(dict(binding))
        return {'tab_id': 'tab-1', 'url': 'https://chatgpt.com/'}

    def stop(self, binding):
        self.stopped.append(dict(binding))
        return {'status': 'STOP_REQUESTED'}

    def list_projects(self):
        return [{'name': 'CFR'}, {'name': '论文'}]

    def open_project(self, binding, project):
        return {'tab_id': 'tab-project', 'url': 'https://chatgpt.com/g/g-p-cfr/project', 'project_id': 'g-p-cfr', 'conversation_id': None}

    def list_project_conversations(self, binding):
        return [
            {'title': '架构', 'url': 'https://chatgpt.com/g/g-p-cfr/c/conv-1', 'project_id': 'g-p-cfr', 'conversation_id': 'conv-1'},
            {'title': 'BrowserBridge', 'url': 'https://chatgpt.com/g/g-p-cfr/c/conv-2', 'project_id': 'g-p-cfr', 'conversation_id': 'conv-2'},
        ]

    def conversation_history(self, binding, limit=10):
        self.history_limits.append(limit)
        return [
            {'role': 'user', 'text': '历史问题'},
            {'role': 'assistant', 'text': '历史回答'},
        ][-limit:]

    def reasoning_effort(self, binding=None):
        return {**self.reasoning, 'tab_id': 'tab-reasoning', 'url': (binding or {}).get('url') or 'https://chatgpt.com/'}

    def set_reasoning_effort(self, binding, value):
        mapping = {'1': ('Instant', 1), '2': ('Medium', 2), '3': ('High', 3), '4': ('Extra High', 4), 'high': ('High', 3)}
        label, index = mapping[str(value).strip().lower()]
        self.reasoning = {'label': label, 'index': index, 'total': 4}
        return {**self.reasoning, 'tab_id': 'tab-reasoning', 'url': binding.get('url') or 'https://chatgpt.com/'}

    def models(self, binding=None):
        return {
            'models': [dict(item) for item in self.chat_models],
            'current': next(item['name'] for item in self.chat_models if item['selected']),
            'tab_id': 'tab-model',
            'url': (binding or {}).get('url') or 'https://chatgpt.com/',
        }

    def set_model(self, binding, value):
        token = str(value).strip()
        target = self.chat_models[int(token) - 1] if token.isdigit() else next(
            item for item in self.chat_models if item['name'].casefold() == token.casefold()
        )
        if target['disabled']:
            raise StructuredError('CHAT_MODEL_UNAVAILABLE', 'disabled')
        for item in self.chat_models:
            item['selected'] = item is target
        self.model_changes.append(target['name'])
        return self.models(binding)

    def current_identity(self, binding=None):
        binding = binding or {}
        url = binding.get('url') or 'https://chatgpt.com/'
        conversation_id = url.rstrip('/').split('/c/')[-1] if '/c/' in url else None
        return {'tab_id': binding.get('tab_id'), 'url': url, 'project_id': None, 'conversation_id': conversation_id}

    def open_conversation(self, binding, conversation, *, project_id=None):
        conversation_id = str(conversation).rstrip('/').split('/')[-1]
        return {'tab_id': 'tab-chat', 'url': f'https://chatgpt.com/g/{project_id or "g-p-cfr"}/c/{conversation_id}', 'project_id': project_id or 'g-p-cfr', 'conversation_id': conversation_id}

    def open_scheduled(self, binding=None):
        return {'tab_id': 'tab-scheduled', 'url': 'https://chatgpt.com/scheduled', 'project_id': None, 'conversation_id': None}

    def list_scheduled_tasks(self):
        return [dict(item) for item in self.tasks]

    def create_scheduled_task(self, prompt):
        self.scheduled_actions.append(('create', prompt))
        task = {'task_id': 'task-2', 'title': 'Created task', 'detail': '9月10日 · 已安排', 'paused': False, 'can_pause': True, 'can_edit': True, 'has_more': True}
        self.tasks.append(task)
        return dict(task)

    def pause_scheduled_task(self, task_id):
        self.scheduled_actions.append(('pause', task_id))
        task = next(item for item in self.tasks if item['task_id'] == task_id)
        task['paused'] = True
        return dict(task)

    def resume_scheduled_task(self, task_id):
        self.scheduled_actions.append(('resume', task_id))
        task = next(item for item in self.tasks if item['task_id'] == task_id)
        task['paused'] = False
        return dict(task)

    def edit_scheduled_task(self, task_id, **changes):
        self.scheduled_actions.append(('edit', task_id, changes))
        task = next(item for item in self.tasks if item['task_id'] == task_id)
        if changes.get('title'):
            task['title'] = changes['title']
        return dict(task)

    def delete_scheduled_task(self, task_id):
        self.scheduled_actions.append(('delete', task_id))
        self.tasks = [item for item in self.tasks if item['task_id'] != task_id]
        return {'task_id': task_id, 'deleted': True}


class CommandSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.daemon = FeishuDaemon.__new__(FeishuDaemon)
        self.daemon.parser = CommandParser()
        self.daemon.replies = _Replies()
        self.daemon.adapter = _Adapter()
        self.daemon.chat_adapter = _ChatAdapter()
        self.daemon._chat_browser_lock = threading.RLock()
        self.daemon.settings = SimpleNamespace(allowed_workspace_roots=(Path.cwd(),), database=Path('db.sqlite3'))
        self.session = SimpleNamespace(thread_id='thread-1', pending_cwd=None)
        self.selected_surface = 'code'
        self.approval_mode = 'ask'
        self.chat_binding = {'chat_id': 'chat', 'tab_key': 'chat-key', 'tab_id': None, 'url': None}
        self.pending_attachments = []

        def set_surface(_chat, surface): self.selected_surface = surface
        def update_chat(_chat, **values): self.chat_binding.update({key: value for key, value in values.items() if value is not None}); return dict(self.chat_binding)
        def add_attachment(chat_id, path, kind, name=None):
            item = {'attachment_id': f'att-{len(self.pending_attachments) + 1}', 'chat_id': chat_id, 'path': str(Path(path)), 'kind': kind, 'name': name or Path(path).name}
            self.pending_attachments.append(item)
            return item
        def clear_attachments(_chat, ids=None):
            wanted = set(ids or ())
            self.pending_attachments[:] = [] if not ids else [item for item in self.pending_attachments if item['attachment_id'] not in wanted]
        self.daemon.store = SimpleNamespace(
            get_session=lambda _chat: self.session,
            get_selected_surface=lambda _chat: self.selected_surface,
            set_selected_surface=set_surface,
            ensure_chat_binding=lambda _chat: dict(self.chat_binding),
            get_chat_binding=lambda _chat: dict(self.chat_binding),
            update_chat_binding=update_chat,
            add_pending_attachment=add_attachment,
            list_pending_attachments=lambda _chat: list(self.pending_attachments),
            clear_pending_attachments=clear_attachments,
            get_approval_mode=lambda _chat: self.approval_mode,
            set_approval_mode=lambda _chat, mode: setattr(self, 'approval_mode', mode) or mode,
        )
        self.daemon.binding_store = SimpleNamespace(list_bindings=lambda limit=None: [])
        self.daemon.approvals = SimpleNamespace(
            resolve=lambda approval_id, operator, action: (approval_id, operator, action),
        )
        self.daemon._model_catalog = CATALOG['data']
        self.daemon._catalog_warm_started = True
        self.daemon._catalog_error = None
        self.message = FeishuInboundMessage('e', 'm', 'chat', 'p2p', 'user', 'user', 'text', '/model 2')

    def test_card_action_boundary_rejects_malformed_nested_values(self):
        for payload in (
            None,
            {'event': 'invalid'},
            {'event': {'operator': 'invalid'}, 'action': {'value': {}}},
            {'event': {'operator': {'open_id': 'ou'}}, 'action': {'value': 'not-json'}},
        ):
            with self.subTest(payload=payload), self.assertRaises(StructuredError) as caught:
                self.daemon.handle_card_action(payload)
            self.assertEqual(caught.exception.code, 'FEISHU_CARD_ACTION_INVALID')

    def test_card_action_boundary_accepts_valid_payload(self):
        result = self.daemon.handle_card_action({
            'event': {'operator': {'open_id': 'ou'}},
            'action': {'value': json.dumps({'approval_id': 'approval-1', 'action': 'accept'})},
        })
        self.assertEqual(result, ('approval-1', 'ou', 'accept'))

    def test_per_chat_lock_registry_releases_idle_chat_keys(self):
        daemon = FeishuDaemon.__new__(FeishuDaemon)
        first = daemon._lock_for('chat-a')
        self.assertIs(first, daemon._lock_for('chat-a'))
        daemon._lock_for('chat-b')
        self.assertEqual(len(daemon._locks), 1)
        del first
        gc.collect()
        self.assertEqual(len(daemon._locks), 0)

    def test_model_index_updates_native_thread_without_turn(self):
        with patch('cfr.control.codex_catalog.models', return_value=CATALOG):
            self.daemon._handle_command(self.message, CommandParser().parse('/model 2'))
        self.assertEqual(self.daemon.adapter.updates, [('thread-1', {'model': 'model-two'})])
        self.assertIn('model-two', self.daemon.replies.text[-1])

    def test_unsupported_reasoning_is_rejected_before_native_update(self):
        with patch('cfr.control.codex_catalog.models', return_value=CATALOG):
            with self.assertRaises(Exception):
                self.daemon._handle_command(self.message, CommandParser().parse('/reasoning impossible'))
        self.assertEqual(self.daemon.adapter.updates, [])

    def test_model_mutation_then_reasoning_uses_current_thread_catalog_identity(self):
        with patch('cfr.control.codex_catalog.models', return_value=CATALOG):
            self.daemon._handle_command(self.message, CommandParser().parse('/model 2'))
            self.daemon._handle_command(self.message, CommandParser().parse('/reasoning'))
            self.assertIn('medium', self.daemon.replies.text[-1])
            self.daemon._handle_command(self.message, CommandParser().parse('/reasoning medium'))
            self.assertEqual(self.daemon.adapter.state['effort'], 'medium')
            self.daemon._handle_command(self.message, CommandParser().parse('/reasoning'))
        self.assertIn('当前推理强度：medium', self.daemon.replies.text[-1])

    def test_current_thread_model_resolves_each_native_catalog_identity(self):
        model = {'id': 'native-terra', 'model': 'gpt-5.6-terra', 'display_name': 'GPT-5.6-Terra'}
        for identity in ('native-terra', 'gpt-5.6-terra', 'GPT-5.6-Terra'):
            self.assertIs(self.daemon._current_thread_model([model], identity), model)
        self.assertIsNone(self.daemon._current_thread_model([model, {**model, 'id': 'other'}], 'gpt-5.6-terra'))

    def test_tiers_unknown_current_model_fails_closed(self):
        self.daemon.adapter.state['model'] = 'unknown-model'
        with patch('cfr.control.codex_catalog.models', return_value=CATALOG):
            with self.assertRaises(StructuredError) as raised:
                self.daemon._handle_command(self.message, CommandParser().parse('/tiers'))
        self.assertEqual(raised.exception.code, 'CONTROL_THREAD_MODEL_UNAVAILABLE')

    def test_resolved_model_with_empty_tiers_is_not_catalog_miss(self):
        self.daemon.adapter.state['model'] = 'Two'
        with patch('cfr.control.codex_catalog.models', return_value=CATALOG):
            self.daemon._handle_command(self.message, CommandParser().parse('/tiers'))
        self.assertIn('当前模型没有额外可切换服务层级。', self.daemon.replies.text[-1])

    def test_native_resume_projection_feeds_model_reasoning_and_tiers_commands(self):
        class NativeSettingsClient:
            state = {'model': 'model-one', 'reasoningEffort': 'low', 'serviceTier': None}

            def __init__(self, *_args, **_kwargs): pass
            def start(self): return self
            def close(self): pass
            def lifecycle_snapshot(self): return {'started': True, 'close_started': True, 'exited': True, 'exit_code': 0}
            def request(self, method, params, timeout=None):
                if method == 'thread/resume':
                    return {'thread': {'id': 'thread-1', 'cwd': str(self.rollout.parent), 'path': str(self.rollout), 'name': 'name'}, **type(self).state}
                if method == 'thread/settings/update':
                    type(self).state.update({
                        'model': params.get('model', type(self).state['model']),
                        'reasoningEffort': params.get('effort', type(self).state['reasoningEffort']),
                        'serviceTier': params.get('serviceTier', type(self).state['serviceTier']),
                    })
                    return {}
                return {}

        with tempfile.TemporaryDirectory() as directory:
            NativeSettingsClient.rollout = Path(directory) / 'rollout.jsonl'
            NativeSettingsClient.rollout.write_text('', encoding='utf-8')
            NativeSettingsClient.state = {'model': 'model-one', 'reasoningEffort': 'low', 'serviceTier': None}
            bindings = BindingStore(Path(directory) / 'cfr.sqlite3')
            bindings.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': NativeSettingsClient.rollout})())
            self.daemon.adapter = CodexAdapter(store=bindings)
            with patch('cfr.codex.binding.AppServerClient', NativeSettingsClient), patch('cfr.control.codex_catalog.models', return_value=CATALOG):
                self.daemon._handle_command(self.message, CommandParser().parse('/model 2'))
                self.daemon._handle_command(self.message, CommandParser().parse('/reasoning'))
                self.assertIn('medium', self.daemon.replies.text[-1])
                self.daemon._handle_command(self.message, CommandParser().parse('/reasoning medium'))
                self.daemon._handle_command(self.message, CommandParser().parse('/reasoning'))
                self.assertIn('当前推理强度：medium', self.daemon.replies.text[-1])
                self.daemon._handle_command(self.message, CommandParser().parse('/tiers'))
            self.assertIn('当前模型没有额外可切换服务层级。', self.daemon.replies.text[-1])
            bindings.close()

    def test_async_transport_callback_runs_reasoning_command_without_nested_loop_error(self):
        raw = type('ChannelInbound', (), {
            'message_id': 'message-1', 'event_id': 'event-1', 'chat_id': 'chat', 'chat_type': 'p2p',
            'sender_id': 'user', 'raw_content_type': 'text', 'content_text': '/reasoning',
            'mentioned_bot': False, 'sender_is_bot': False,
        })()
        transport = ChannelFeishuTransport(FeishuSettings('app', 'secret', (), ()))
        transport._message_handler = lambda message, _event_id: self.daemon.handle_control_command(message)
        with patch('cfr.control.codex_catalog.models', return_value=CATALOG):
            asyncio.run(transport._on_message(raw))
        self.assertIn('当前推理强度', self.daemon.replies.text[-1])

    def test_thread_settings_read_commands_continue_to_reply(self):
        modes = {'available': True, 'data': [{'name': 'Default', 'mode': 'default', 'model': None, 'reasoning_effort': None}]}
        with patch('cfr.control.codex_catalog.models', return_value=CATALOG), patch('cfr.control.codex_catalog.collaboration_modes', return_value=modes):
            for text in ('/reasoning', '/tiers', '/modes', '/model'):
                self.daemon._handle_command(self.message, CommandParser().parse(text))
        self.assertEqual(len(self.daemon.replies.text), 4)

    def test_model_and_reasoning_reads_use_projection_and_warm_catalog_without_native_rpc(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': None})())
            store.update_observed_settings('thread-1', {'model': 'model-one', 'effort': 'high', 'service_tier': 'priority'})
            self.daemon.binding_store = store
            self.daemon.adapter = CodexAdapter(store=store)
            with patch('cfr.codex.binding.AppServerClient', side_effect=AssertionError('read spawned app-server')), patch.object(self.daemon.adapter.runtime_leases, 'acquire', side_effect=AssertionError('read acquired writer lease')), self.assertLogs('cfr.feishu.daemon', 'INFO') as logs:
                self.daemon._handle_command(self.message, CommandParser().parse('/model'))
                self.daemon._handle_command(self.message, CommandParser().parse('/reasoning'))
            self.assertIn('当前线程模型：model-one', self.daemon.replies.text[-2])
            self.assertIn('当前推理强度：high', self.daemon.replies.text[-1])
            self.assertTrue(any('FEISHU_COMMAND_MODEL_READ_LATENCY' in line for line in logs.output))
            self.assertTrue(any('FEISHU_COMMAND_REASONING_READ_LATENCY' in line for line in logs.output))
            store.close()

    def test_reasoning_cold_catalog_returns_without_synchronous_native_read(self):
        self.daemon._model_catalog = None
        self.daemon._catalog_warm_started = True
        with patch('cfr.control.codex_catalog.models') as models:
            self.daemon._handle_command(self.message, CommandParser().parse('/reasoning'))
        models.assert_not_called()
        self.assertIn('正在同步 Codex 模型目录', self.daemon.replies.text[-1])

    def test_unhydrated_projection_is_unsynced_without_native_read(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BindingStore(Path(directory) / 'cfr.sqlite3')
            store.upsert_binding(type('Ref', (), {'thread_id': 'thread-1', 'name': 'name', 'cwd': Path(directory), 'rollout_path': None})())
            self.daemon.binding_store = store
            self.daemon.adapter = CodexAdapter(store=store)
            with patch('cfr.codex.binding.AppServerClient', side_effect=AssertionError('read spawned app-server')):
                self.daemon._handle_command(self.message, CommandParser().parse('/model'))
                self.daemon._handle_command(self.message, CommandParser().parse('/reasoning'))
            self.assertIn('尚未同步', self.daemon.replies.text[-2])
            self.assertIn('尚未同步', self.daemon.replies.text[-1])
            self.assertNotIn('不在已安装', self.daemon.replies.text[-1])
            store.close()

    def test_catalog_warms_once_in_background_and_repeated_reads_reuse_it(self):
        ready = threading.Event()
        release = threading.Event()
        self.daemon._model_catalog = None
        self.daemon._catalog_warm_started = False
        self.daemon._catalog_lock = threading.Lock()

        def load():
            ready.set()
            release.wait(1)
            return CATALOG

        with patch('cfr.control.codex_catalog.models', side_effect=load) as models:
            self.daemon._start_catalog_warm()
            self.assertTrue(ready.wait(1))
            self.assertIsNone(self.daemon._model_catalog)
            release.set()
            deadline = time.monotonic() + 1
            while self.daemon._model_catalog is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.daemon._handle_command(self.message, CommandParser().parse('/models'))
            self.daemon._handle_command(self.message, CommandParser().parse('/reasoning'))
        models.assert_called_once_with()

    def test_cached_codex_catalog_refreshes_in_background_after_ttl(self):
        self.daemon._catalog_checked_at = time.monotonic() - 61
        self.daemon._catalog_warm_started = False
        with patch.object(self.daemon, '_start_catalog_warm') as warm:
            self.assertIs(self.daemon._catalog(), CATALOG['data'])
        warm.assert_called_once_with()

    def test_steer_uses_active_thread_client_path(self):
        self.daemon._handle_command(self.message, CommandParser().parse('/steer 继续检查'))
        self.assertEqual(self.daemon.adapter.steers, [('thread-1', '继续检查')])
        self.assertIn('软转向', self.daemon.replies.text[-1])
        self.assertIn('可能继续一段', self.daemon.replies.text[-1])
        self.assertIn('/stop', self.daemon.replies.text[-1])

    def test_help_makes_read_and_mutation_command_semantics_explicit(self):
        for text in ('/surface', '/surface chat', '/model', '/model <编号|模型ID|模型名>', '/model default', '/reasoning', '/approval', '/sessions', '/session <编号|thread ID/后缀>', '/workspace <编号|名称|绝对路径>', '/upload <绝对文件路径>', '/steer', '/redirect'):
            self.assertIn(text, CODE_HELP_TEXT)
        self.assertIn('软转向', CODE_HELP_TEXT)
        self.assertIn('不保证立即中断当前输出', CODE_HELP_TEXT)
        self.assertIn('/stop：停止当前 Codex Turn，不自动继续', CODE_HELP_TEXT)
        self.assertIn('进度卡片只显示最新状态', CODE_HELP_TEXT)
        self.assertIn('同一 Thread 排队创建新 Turn', CODE_HELP_TEXT)

        for text in ('/surface code', '/projects', '/project <编号|名称|Project ID|URL>', '/chat <编号|Conversation ID|URL>', '/models', '/model <编号|模型名>', '/reasoning', '/reasoning <编号|档位>', '/search <问题>', '/deepresearch status', '/image status', '/upload <绝对文件路径>', '/scheduled list', '/scheduled create', '/scheduled pause', '/scheduled resume', '/scheduled edit', '/scheduled delete'):
            self.assertIn(text, CHAT_HELP_TEXT)
        self.assertNotIn('/model default', CHAT_HELP_TEXT)
        self.assertNotIn('/project <编号|名称|Project ID|URL>', CODE_HELP_TEXT)

    def test_help_is_surface_specific(self):
        self.daemon._handle_command(self.message, CommandParser().parse('/help'))
        self.assertIn('Code Surface 指令', self.daemon.replies.text[-1])
        self.assertIn('/model', self.daemon.replies.text[-1])
        self.assertNotIn('/deepresearch <问题>', self.daemon.replies.text[-1])
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/help'))
        self.assertIn('Chat Surface 指令', self.daemon.replies.text[-1])
        self.assertIn('/deepresearch <问题>', self.daemon.replies.text[-1])
        self.assertIn('/model <编号|模型名>', self.daemon.replies.text[-1])
        self.assertNotIn('/model default', self.daemon.replies.text[-1])

    def test_surface_is_separate_from_codex_collaboration_mode(self):
        self.daemon._handle_command(self.message, CommandParser().parse('/surface'))
        self.assertIn('Chat', self.daemon.replies.text[-1])
        self.assertIn('Work', self.daemon.replies.text[-1])
        self.assertIn('Code', self.daemon.replies.text[-1])
        self.assertIn('/mode', self.daemon.replies.text[-1])
        self.daemon._handle_command(self.message, CommandParser().parse('/surface chat'))
        self.assertIn('当前不可用', self.daemon.replies.text[-1])
        self.assertEqual(self.daemon.adapter.updates, [])
        self.daemon._handle_command(self.message, CommandParser().parse('/surface code'))
        self.assertIn('原生 Codex', self.daemon.replies.text[-1])

    def test_ready_chat_surface_can_be_selected_without_touching_codex(self):
        self.daemon.chat_adapter.available = True
        self.daemon._handle_command(self.message, CommandParser().parse('/surface chat'))
        self.assertEqual(self.selected_surface, 'chat')
        self.assertIn('原生 ChatGPT 网页', self.daemon.replies.text[-1])
        self.assertIn('先发送 /projects', self.daemon.replies.text[-1])
        self.assertIn('/project <编号|名称|Project ID|URL>', self.daemon.replies.text[-1])
        self.assertEqual(self.daemon.adapter.updates, [])

    def test_chat_prompt_routes_to_chat_adapter_and_persists_native_url(self):
        self.selected_surface = 'chat'
        prompt = FeishuInboundMessage('e', 'prompt-1', 'chat', 'p2p', 'user', 'user', 'text', 'hello chat')
        self.daemon._handle_prompt(prompt)
        self.assertEqual(self.daemon.chat_adapter.sent[0][1], 'hello chat')
        self.assertEqual(self.chat_binding['tab_id'], 'tab-1')
        self.assertEqual(self.chat_binding['url'], 'https://chatgpt.com/c/chat-1')
        self.assertEqual(self.daemon.replies.text[-1], 'chat answer')

    def test_chat_prompt_relays_new_generated_image(self):
        self.selected_surface = 'chat'
        prompt = FeishuInboundMessage('e', 'prompt-image', 'chat', 'p2p', 'user', 'user', 'text', 'brighten this image')
        with patch.object(self.daemon.chat_adapter, 'send_message', return_value={
            'text': '',
            'tab_id': 'tab-image-result',
            'url': 'https://chatgpt.com/c/image-result',
            'generated_image_src': 'https://chatgpt.com/backend-api/estuary/content?id=file-1',
        }):
            result = self.daemon._handle_prompt(prompt)
        self.assertEqual(result, ['image-reply'])
        self.assertEqual(self.daemon.replies.images[-1], b'png-bytes')
        self.assertEqual(self.chat_binding['url'], 'https://chatgpt.com/c/image-result')

    def test_chat_prompt_relays_generated_file_and_records_runtime(self):
        self.selected_surface = 'chat'
        prompt = FeishuInboundMessage('e', 'prompt-file-output', 'chat', 'p2p', 'user', 'user', 'text', 'make a workbook')
        with patch.object(self.daemon.chat_adapter, 'send_message', return_value={
            'text': '已生成工作簿。',
            'tab_id': 'tab-file-result',
            'url': 'https://chatgpt.com/c/file-result',
            'generated_file': {'path': 'generated.xlsx', 'name': 'generated.xlsx', 'file_id': 'file-new'},
        }):
            result = self.daemon._handle_prompt(prompt)
        self.assertEqual(result, ['reply', 'file-reply'])
        self.assertEqual(self.daemon.replies.files[-1], Path('generated.xlsx'))
        run = self.daemon.chat_runtime_snapshot()[0]
        self.assertEqual(run['status'], 'completed')
        self.assertFalse(run['active'])
        self.assertEqual(run['output_count'], 2)
        self.assertEqual(run['url'], 'https://chatgpt.com/c/file-result')

    def test_chat_prompt_relays_every_file_from_current_assistant_turn(self):
        self.selected_surface = 'chat'
        prompt = FeishuInboundMessage('e', 'prompt-multi-file-output', 'chat', 'p2p', 'user', 'user', 'text', 'make csv and xlsx')
        files = [
            {'path': 'generated.csv', 'name': 'generated.csv', 'file_id': 'file-csv'},
            {'path': 'generated.xlsx', 'name': 'generated.xlsx', 'file_id': 'file-xlsx'},
        ]
        with patch.object(self.daemon.chat_adapter, 'send_message', return_value={
            'text': '已生成两个文件。',
            'tab_id': 'tab-multi-file-result',
            'url': 'https://chatgpt.com/c/multi-file-result',
            'generated_files': files,
        }):
            result = self.daemon._handle_prompt(prompt)
        self.assertEqual(result, ['reply', 'file-reply', 'file-reply'])
        self.assertEqual(self.daemon.replies.files[-2:], [Path('generated.csv'), Path('generated.xlsx')])
        self.assertEqual([item[1]['file_id'] for item in self.daemon.chat_adapter.downloaded_files[-2:]], ['file-csv', 'file-xlsx'])
        self.assertEqual(self.daemon.chat_runtime_snapshot()[0]['output_count'], 3)

    def test_chat_download_cleanup_only_removes_cfr_owned_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            'cfr.feishu.daemon.resolve_cfr_config_dir', return_value=Path(directory)
        ):
            root = Path(directory)
            owned = root / 'chatgpt' / 'downloads' / 'generated.xlsx'
            outside = root / 'workspace' / 'generated.xlsx'
            owned.parent.mkdir(parents=True)
            outside.parent.mkdir(parents=True)
            owned.write_bytes(b'owned')
            outside.write_bytes(b'user')
            self.daemon._cleanup_chat_download(owned)
            self.daemon._cleanup_chat_download(outside)
            self.assertFalse(owned.exists())
            self.assertTrue(outside.exists())

    def test_chat_partial_artifact_failure_does_not_block_other_deliveries(self):
        self.selected_surface = 'chat'
        prompt = FeishuInboundMessage('e', 'prompt-partial', 'chat', 'p2p', 'user', 'user', 'text', 'build files')
        with patch.object(self.daemon.chat_adapter, 'send_message', return_value={
            'text': 'summary',
            'tab_id': 'tab-files',
            'url': 'https://chatgpt.com/c/files-result',
            'generated_files': [
                {'file_id': 'file-bad', 'name': 'bad.csv'},
                {'file_id': 'file-good', 'name': 'good.xlsx'},
            ],
        }), patch.object(
            self.daemon.chat_adapter,
            'download_generated_file_to_file',
            side_effect=[StructuredError('CHAT_FILE_DOWNLOAD_FAILED', 'bad'), {'path': 'good.xlsx'}],
        ):
            result = self.daemon._handle_prompt(prompt)
        self.assertIn(Path('good.xlsx'), self.daemon.replies.files)
        self.assertTrue(any('有 1 个结果未能通过飞书回传' in text for text in self.daemon.replies.text))
        run = self.daemon.chat_runtime_snapshot()[0]
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['stage'], 'completed_with_warnings')
        self.assertEqual(run['error_code'], 'CHAT_PARTIAL_DELIVERY')
        self.assertGreaterEqual(len(result), 2)

    def test_chat_prompt_batch_uploads_pending_attachments_before_sending_text(self):
        self.selected_surface = 'chat'
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.pdf'
            image = Path(directory) / 'diagram.png'
            report.write_bytes(b'pdf')
            image.write_bytes(b'png')
            self.daemon.store.add_pending_attachment('chat', report, 'file', report.name)
            self.daemon.store.add_pending_attachment('chat', image, 'image', image.name)
            prompt = FeishuInboundMessage('e', 'prompt-file', 'chat', 'p2p', 'user', 'user', 'text', 'summarize this')
            self.daemon._handle_prompt(prompt)
        self.assertEqual([path.name for path in self.daemon.chat_adapter.sent_files[0]], ['report.pdf', 'diagram.png'])
        self.assertEqual(self.daemon.chat_adapter.upload_batches, [])
        self.assertEqual(self.daemon.chat_adapter.sent[0][1], 'summarize this')
        self.assertEqual(self.pending_attachments, [])

    def test_chat_new_with_argument_creates_conversation_and_sends_first_message(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/new first message'))
        self.assertEqual(len(self.daemon.chat_adapter.new), 1)
        self.assertEqual(self.daemon.chat_adapter.sent[-1][1], 'first message')
        self.assertEqual(self.chat_binding['url'], 'https://chatgpt.com/c/chat-1')

    def test_chat_code_only_commands_fail_closed(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/mode plan'))
        self.assertIn('属于 Code Surface', self.daemon.replies.text[-1])
        self.assertIn('已拒绝执行', self.daemon.replies.text[-1])
        self.assertEqual(self.daemon.adapter.updates, [])

    def test_chat_models_read_and_select_native_web_model(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/models'))
        self.assertIn('GPT-5.6 Sol · 当前', self.daemon.replies.text[-1])
        self.assertIn('Pro · 当前账号不可用', self.daemon.replies.text[-1])
        self.daemon._handle_command(self.message, CommandParser().parse('/model 2'))
        self.assertEqual(self.daemon.chat_adapter.model_changes, ['GPT-5.5'])
        self.assertIn('GPT-5.5', self.daemon.replies.text[-1])

    def test_code_approval_mode_lists_and_persists_native_presets(self):
        self.daemon._handle_command(self.message, CommandParser().parse('/approval'))
        self.assertIn('全部请求', self.daemon.replies.text[-1])
        self.assertIn('approvalsReviewer=auto_review', self.daemon.replies.text[-1])
        self.assertIn('sandbox=danger-full-access', self.daemon.replies.text[-1])
        self.daemon._handle_command(self.message, CommandParser().parse('/approval 2'))
        self.assertEqual(self.approval_mode, 'auto')
        self.assertIn('替我审批', self.daemon.replies.text[-1])
        self.daemon._handle_command(self.message, CommandParser().parse('/approval 3'))
        self.assertEqual(self.approval_mode, 'full')
        self.assertIn('danger-full-access', self.daemon.replies.text[-1])

    def test_chat_reasoning_reads_and_updates_native_thinking_effort(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/reasoning'))
        self.assertIn('High（3/4）', self.daemon.replies.text[-1])
        self.daemon._handle_command(self.message, CommandParser().parse('/reasoning 4'))
        self.assertIn('Extra High（4/4）', self.daemon.replies.text[-1])
        self.assertEqual(self.chat_binding['tab_id'], 'tab-reasoning')

    def test_code_chat_only_commands_fail_closed(self):
        self.daemon._handle_command(self.message, CommandParser().parse('/search current OpenAI homepage'))
        self.assertIn('属于 Chat Surface', self.daemon.replies.text[-1])
        self.assertIn('已拒绝执行', self.daemon.replies.text[-1])
        self.assertEqual(self.daemon.chat_adapter.searches, [])

    def test_chat_project_and_conversation_commands_use_native_browser_adapter(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/projects'))
        self.assertIn('1. CFR', self.daemon.replies.text[-1])
        self.assertIn('3. 普通 Chat（不在 Project 中）', self.daemon.replies.text[-1])
        self.daemon._handle_command(self.message, CommandParser().parse('/project CFR'))
        self.assertEqual(self.chat_binding['url'], 'https://chatgpt.com/g/g-p-cfr/project')
        self.daemon._handle_command(self.message, CommandParser().parse('/chats'))
        self.assertIn('BrowserBridge', self.daemon.replies.text[-1])
        self.daemon._handle_command(self.message, CommandParser().parse('/chat 2'))
        self.assertEqual(self.chat_binding['url'], 'https://chatgpt.com/g/g-p-cfr/c/conv-2')

    def test_chat_navigation_commands_have_safe_ordering_from_landing_page(self):
        self.selected_surface = 'chat'
        self.chat_binding['url'] = 'https://chatgpt.com/'
        with patch.object(self.daemon.chat_adapter, 'conversation_history', return_value=[]):
            self.daemon._handle_command(self.message, CommandParser().parse('/history'))
        self.assertIn('还没有打开具体 Conversation', self.daemon.replies.text[-1])
        self.assertIn('/chats', self.daemon.replies.text[-1])

        self.daemon._handle_command(self.message, CommandParser().parse('/chats'))
        self.assertIn('普通 Chat 对话', self.daemon.replies.text[-1])
        self.daemon._handle_command(self.message, CommandParser().parse('/projects'))
        self.assertIn('ChatGPT 项目', self.daemon.replies.text[-1])
        self.daemon._handle_command(self.message, CommandParser().parse('/project 1'))
        self.assertIn('/g/g-p-cfr/project', self.chat_binding['url'])
        self.daemon._handle_command(self.message, CommandParser().parse('/chats'))
        self.daemon._handle_command(self.message, CommandParser().parse('/chat 2'))
        self.assertTrue(self.chat_binding['url'].endswith('/c/conv-2'))
        self.daemon._handle_command(self.message, CommandParser().parse('/history 2'))
        self.assertIn('历史问题', self.daemon.replies.text[-1])
        self.assertIn('历史回答', self.daemon.replies.text[-1])

    def test_chat_models_and_reasoning_work_from_home_project_and_conversation_bindings(self):
        self.selected_surface = 'chat'
        for url in (
            'https://chatgpt.com/',
            'https://chatgpt.com/g/g-p-cfr/project',
            'https://chatgpt.com/g/g-p-cfr/c/conv-1',
        ):
            with self.subTest(url=url):
                self.chat_binding['url'] = url
                self.daemon._handle_command(self.message, CommandParser().parse('/models'))
                self.assertIn('GPT-5.6 Sol', self.daemon.replies.text[-1])
                self.daemon._handle_command(self.message, CommandParser().parse('/reasoning'))
                self.assertIn('High（3/4）', self.daemon.replies.text[-1])

    def test_chat_help_documents_order_and_numeric_selection(self):
        self.assertIn('/projects（可选）→ /project <编号>（可选）→ /chats → /chat <编号> → /history', CHAT_HELP_TEXT)
        self.assertIn('编号均从 1 开始', CHAT_HELP_TEXT)
        self.assertIn('网页新增模型后会随原生菜单自动出现', CHAT_HELP_TEXT)

    def test_plain_chat_chats_uses_plain_label_and_guidance_when_empty(self):
        self.selected_surface = 'chat'
        self.chat_binding['url'] = 'https://chatgpt.com/'
        with patch.object(self.daemon.chat_adapter, 'list_project_conversations', return_value=[]):
            self.daemon._handle_command(self.message, CommandParser().parse('/chats'))
        self.assertIn('普通 Chat 对话', self.daemon.replies.text[-1])
        self.assertIn('/new <首条消息>', self.daemon.replies.text[-1])

    def test_chat_project_plain_option_reports_leaving_project(self):
        self.selected_surface = 'chat'
        with patch.object(self.daemon.chat_adapter, 'open_project', return_value={
            'tab_id': 'tab-home', 'url': 'https://chatgpt.com/', 'project_id': None, 'conversation_id': None,
        }):
            self.daemon._handle_command(self.message, CommandParser().parse('/project 3'))
        self.assertEqual(self.chat_binding['url'], 'https://chatgpt.com/')
        self.assertIn('不在 Project 中', self.daemon.replies.text[-1])

    def test_chat_scheduled_opens_native_management_page(self):
        self.selected_surface = 'chat'
        original = dict(self.chat_binding)
        self.daemon._handle_command(self.message, CommandParser().parse('/scheduled'))
        self.assertEqual(self.chat_binding, original)
        self.assertIn('Scheduled', self.daemon.replies.text[-1])

    def test_chat_scheduled_list_returns_native_task_identity(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/scheduled list'))
        self.assertIn('Daily brief', self.daemon.replies.text[-1])
        self.assertIn('task-1', self.daemon.replies.text[-1])

    def test_chat_scheduled_mutations_are_explicit_and_task_scoped(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/scheduled create remind me later'))
        self.assertEqual(self.daemon.chat_adapter.scheduled_actions[-1], ('create', 'remind me later'))
        self.daemon._handle_command(self.message, CommandParser().parse('/scheduled pause 1'))
        self.assertEqual(self.daemon.chat_adapter.scheduled_actions[-1], ('pause', 'task-1'))
        self.daemon._handle_command(self.message, CommandParser().parse('/scheduled resume task-1'))
        self.assertEqual(self.daemon.chat_adapter.scheduled_actions[-1], ('resume', 'task-1'))
        self.daemon._handle_command(self.message, CommandParser().parse('/scheduled edit task-1 title=Renamed brief'))
        self.assertEqual(self.daemon.chat_adapter.scheduled_actions[-1], ('edit', 'task-1', {'title': 'Renamed brief'}))
        self.daemon._handle_command(self.message, CommandParser().parse('/scheduled delete task-1'))
        self.assertEqual(self.daemon.chat_adapter.scheduled_actions[-1], ('delete', 'task-1'))

    def test_chat_scheduled_edit_rejects_ambiguous_free_text(self):
        self.selected_surface = 'chat'
        with self.assertRaises(StructuredError) as caught:
            self.daemon._handle_command(self.message, CommandParser().parse('/scheduled edit task-1 change it somehow'))
        self.assertEqual(caught.exception.code, 'CHAT_SCHEDULED_EDIT_REQUIRED')
        with self.assertRaises(StructuredError):
            self.daemon._handle_command(self.message, CommandParser().parse('/scheduled edit task-1 instructions=not-yet-verified'))

    def test_chat_search_uses_native_search_tool_path(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/search current OpenAI homepage'))
        self.assertEqual(self.daemon.chat_adapter.searches[0][1], 'current OpenAI homepage')
        self.assertEqual(self.chat_binding['url'], 'https://chatgpt.com/c/search-1')
        self.assertEqual(self.daemon.replies.text[-1], 'search answer')

    def test_chat_deep_research_starts_and_status_returns_report(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/deepresearch investigate OpenAI'))
        self.assertEqual(self.daemon.chat_adapter.deep_research[0][1], 'investigate OpenAI')
        self.assertEqual(self.chat_binding['url'], 'https://chatgpt.com/c/research-1')
        self.assertIn('运行中', self.daemon.replies.text[-1])
        self.daemon._handle_command(self.message, CommandParser().parse('/deepresearch status'))
        self.assertEqual(self.daemon.replies.text[-1], 'research report')

    def test_chat_image_starts_and_status_returns_native_result(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/image simple square'))
        self.assertEqual(self.daemon.chat_adapter.images[0][1], 'simple square')
        self.assertEqual(self.chat_binding['url'], 'https://chatgpt.com/c/image-1')
        self.daemon._handle_command(self.message, CommandParser().parse('/image status'))
        self.assertEqual(self.daemon.replies.images[-1], b'png-bytes')

    def test_chat_upload_uses_allowlisted_file(self):
        self.selected_surface = 'chat'
        path = Path.cwd() / 'upload.txt'
        with patch('cfr.feishu.daemon.validate_file', return_value=path):
            self.daemon._handle_command(self.message, CommandParser().parse(f'/upload {path}'))
        self.assertEqual(self.daemon.chat_adapter.uploads[0][1], path)
        self.assertEqual(self.chat_binding['url'], 'https://chatgpt.com/c/upload-1')
        self.assertIn('upload.txt', self.daemon.replies.text[-1])

    def test_chat_history_reads_current_native_conversation(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/history 2'))
        self.assertIn('历史问题', self.daemon.replies.text[-1])
        self.assertIn('历史回答', self.daemon.replies.text[-1])

    def test_history_limit_is_clamped_instead_of_rejected(self):
        self.selected_surface = 'chat'
        self.daemon._handle_command(self.message, CommandParser().parse('/history 999'))
        self.assertEqual(self.daemon.chat_adapter.history_limits[-1], 50)
        self.assertIn('请求 999 条', self.daemon.replies.text[-1])
        self.daemon._handle_command(self.message, CommandParser().parse('/history -4'))
        self.assertEqual(self.daemon.chat_adapter.history_limits[-1], 1)
        self.assertIn('按上限返回 1 条', self.daemon.replies.text[-1])

    def test_history_non_numeric_limit_keeps_clear_usage_error(self):
        with self.assertRaises(StructuredError) as caught:
            self.daemon._handle_command(self.message, CommandParser().parse('/history many'))
        self.assertEqual(caught.exception.code, 'CFR_HISTORY_LIMIT_INVALID')

    def test_code_history_reads_bounded_native_rollout(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / 'rollout.jsonl'
            records = [
                {'timestamp': '2026-09-02T00:00:00Z', 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '旧问题', 'turn_id': 'turn-1'}},
                {'timestamp': '2026-09-02T00:00:01Z', 'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': '旧回答', 'turn_id': 'turn-1'}},
            ]
            rollout.write_text('\n'.join(json.dumps(item, ensure_ascii=False) for item in records) + '\n', encoding='utf-8')
            self.daemon.binding_store = SimpleNamespace(get_binding=lambda _thread: SimpleNamespace(rollout_path=rollout))
            self.daemon._handle_command(self.message, CommandParser().parse('/history 2'))
        self.assertIn('旧问题', self.daemon.replies.text[-1])
        self.assertIn('旧回答', self.daemon.replies.text[-1])

    def test_code_upload_queues_native_attachment_for_next_turn(self):
        path = Path.cwd() / 'diagram.png'
        with patch('cfr.feishu.daemon.validate_file', return_value=path):
            self.daemon._handle_command(self.message, CommandParser().parse(f'/upload {path}'))
        self.assertEqual(self.pending_attachments[0]['kind'], 'image')
        self.assertEqual(self.pending_attachments[0]['path'], str(path))
        self.assertIn('localImage', self.daemon.replies.text[-1])

    def test_pending_attachments_prepare_native_image_and_workspace_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'inbox'
            workspace = root / 'workspace'
            inbox.mkdir()
            workspace.mkdir()
            image = inbox / 'diagram.png'
            report = inbox / 'report.pdf'
            image.write_bytes(b'png')
            report.write_bytes(b'pdf')
            inputs, files, staged = self.daemon._prepare_codex_attachments([
                {'attachment_id': 'image-1', 'kind': 'image', 'path': str(image), 'name': 'diagram.png'},
                {'attachment_id': 'file-123456', 'kind': 'file', 'path': str(report), 'name': 'report.pdf'},
            ], workspace)
            copied = workspace / files[0]
            prompt = self.daemon._codex_prompt_with_files('inspect', files)
            self.assertEqual(inputs, [{'type': 'localImage', 'path': str(image.resolve())}])
            self.assertEqual(copied.read_bytes(), b'pdf')
            self.assertTrue(files[0].startswith('.cfr/attachments/file-123-report.pdf'))
            self.assertIn(files[0], prompt)
            self.assertEqual([path.resolve() for path in staged], [copied.resolve()])

    def test_inbound_feishu_image_downloads_to_cfr_local_inbox(self):
        class MediaTransport:
            def download_image_to_file(self, image_key, destination, message_id=None):
                self.call = (image_key, Path(destination), message_id)
                Path(destination).mkdir(parents=True, exist_ok=True)
                path = Path(destination) / 'received.png'
                path.write_bytes(b'png')
                return path

        transport = MediaTransport()
        self.daemon.transport = transport
        message = FeishuInboundMessage(
            'evt-image', 'om-image', 'chat', 'p2p', 'user', 'user', 'image', None,
            resources=({'type': 'image', 'file_key': 'img-inbound-1'},),
        )
        with tempfile.TemporaryDirectory() as directory, patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=Path(directory)):
            result = self.daemon._handle_image_message(message)
            saved = transport.call[1] / 'received.png'
            self.assertTrue(saved.is_file())
            self.assertEqual(saved.read_bytes(), b'png')
            self.assertEqual(transport.call[0], 'img-inbound-1')
            self.assertEqual(transport.call[2], 'om-image')
            self.assertEqual(self.pending_attachments[0]['kind'], 'image')
            self.assertEqual(Path(self.pending_attachments[0]['path']).name, 'received.png')
            self.assertEqual(result, ['reply'])

    def test_inbound_message_with_multiple_image_resources_downloads_all(self):
        class MediaTransport:
            def __init__(self):
                self.calls = []

            def download_image_to_file(self, image_key, destination, message_id=None):
                self.calls.append((image_key, message_id))
                Path(destination).mkdir(parents=True, exist_ok=True)
                path = Path(destination) / f'{image_key}.png'
                path.write_bytes(b'png')
                return path

        transport = MediaTransport()
        self.daemon.transport = transport
        message = FeishuInboundMessage(
            'evt-images', 'om-image-2', 'chat', 'p2p', 'user', 'user', 'image', None,
            resources=(
                {'type': 'image', 'file_key': 'img-1'},
                {'type': 'image', 'file_key': 'img-2'},
            ),
        )
        with tempfile.TemporaryDirectory() as directory, patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=Path(directory)):
            result = self.daemon._handle_attachment_message(message)
        self.assertEqual(transport.calls, [('img-1', 'om-image-2'), ('img-2', 'om-image-2')])
        self.assertEqual([item['name'] for item in self.pending_attachments], ['img-1.png', 'img-2.png'])
        self.assertEqual(result, ['reply'])

    def test_inbound_feishu_file_downloads_and_queues_for_next_turn(self):
        class MediaTransport:
            def download_file_to_file(self, file_key, destination, message_id=None, file_name=None, resource_type='file'):
                self.call = (file_key, Path(destination), message_id, file_name, resource_type)
                Path(destination).mkdir(parents=True, exist_ok=True)
                path = Path(destination) / (file_name or 'received.bin')
                path.write_bytes(b'file')
                return path

        transport = MediaTransport()
        self.daemon.transport = transport
        message = FeishuInboundMessage(
            'evt-file', 'om-file', 'chat', 'p2p', 'user', 'user', 'file', None,
            resources=({'type': 'file', 'file_key': 'file-inbound-1', 'file_name': 'report.pdf'},),
        )
        with tempfile.TemporaryDirectory() as directory, patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=Path(directory)):
            result = self.daemon._handle_attachment_message(message)
        self.assertEqual(transport.call[0], 'file-inbound-1')
        self.assertEqual(transport.call[3], 'report.pdf')
        self.assertEqual(transport.call[4], 'file')
        self.assertEqual(self.pending_attachments[0]['kind'], 'file')
        self.assertEqual(self.pending_attachments[0]['name'], 'report.pdf')
        self.assertEqual(result, ['reply'])

    def test_same_named_inbound_files_use_distinct_message_scoped_cache_paths(self):
        class MediaTransport:
            def download_file_to_file(self, _file_key, destination, message_id=None, file_name=None, resource_type='file'):
                Path(destination).mkdir(parents=True, exist_ok=True)
                path = Path(destination) / (file_name or 'received.bin')
                path.write_text(str(message_id), encoding='utf-8')
                return path

        self.daemon.transport = MediaTransport()
        first = FeishuInboundMessage(
            'evt-file-1', 'om-file-1', 'chat', 'p2p', 'user', 'user', 'file', None,
            resources=({'type': 'file', 'file_key': 'file-1', 'file_name': 'report.pdf'},),
        )
        second = FeishuInboundMessage(
            'evt-file-2', 'om-file-2', 'chat', 'p2p', 'user', 'user', 'file', None,
            resources=({'type': 'file', 'file_key': 'file-2', 'file_name': 'report.pdf'},),
        )
        with tempfile.TemporaryDirectory() as directory, patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=Path(directory)):
            self.daemon._handle_attachment_message(first)
            self.daemon._handle_attachment_message(second)
            paths = [Path(item['path']) for item in self.pending_attachments[-2:]]
            self.assertNotEqual(paths[0], paths[1])
            self.assertEqual(paths[0].read_text(encoding='utf-8'), 'om-file-1')
            self.assertEqual(paths[1].read_text(encoding='utf-8'), 'om-file-2')

    def test_inbound_attachment_rejects_sdk_path_outside_message_cache(self):
        class EscapingTransport:
            def download_file_to_file(self, _file_key, destination, message_id=None, file_name=None, resource_type='file'):
                outside = Path(destination).parents[2] / 'escaped.pdf'
                outside.parent.mkdir(parents=True, exist_ok=True)
                outside.write_bytes(b'unsafe')
                return outside

        self.daemon.transport = EscapingTransport()
        message = FeishuInboundMessage(
            'evt-escape', 'om-escape', 'chat', 'p2p', 'user', 'user', 'file', None,
            resources=({'type': 'file', 'file_key': 'file-escape', 'file_name': '../escaped.pdf'},),
        )
        with tempfile.TemporaryDirectory() as directory, patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=Path(directory)):
            with self.assertRaises(StructuredError) as caught:
                self.daemon._handle_attachment_message(message)
        self.assertEqual(caught.exception.code, 'FEISHU_ATTACHMENT_CACHE_ESCAPE')
        self.assertEqual(self.pending_attachments, [])

    def test_inbound_feishu_video_downloads_through_file_resource_and_queues_as_file(self):
        class MediaTransport:
            def download_file_to_file(self, file_key, destination, message_id=None, file_name=None, resource_type='file'):
                self.call = (file_key, Path(destination), message_id, file_name, resource_type)
                Path(destination).mkdir(parents=True, exist_ok=True)
                path = Path(destination) / (file_name or 'received.mp4')
                path.write_bytes(b'video')
                return path

        transport = MediaTransport()
        self.daemon.transport = transport
        message = FeishuInboundMessage(
            'evt-video', 'om-video', 'chat', 'p2p', 'user', 'user', 'media', None,
            resources=({'type': 'video', 'file_key': 'video-inbound-1', 'file_name': 'clip.mp4'},),
        )
        with tempfile.TemporaryDirectory() as directory, patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=Path(directory)):
            result = self.daemon._handle_attachment_message(message)
        self.assertEqual(transport.call[0], 'video-inbound-1')
        self.assertEqual(transport.call[3], 'clip.mp4')
        self.assertEqual(transport.call[4], 'file')
        self.assertEqual(self.pending_attachments[0]['kind'], 'file')
        self.assertEqual(self.pending_attachments[0]['name'], 'clip.mp4')
        self.assertEqual(result, ['reply'])

    def test_chat_attachment_cache_is_deleted_only_after_reply_is_obtained(self):
        self.selected_surface = 'chat'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'feishu' / 'inbox'
            inbox.mkdir(parents=True)
            video = inbox / 'clip.mp4'
            video.write_bytes(b'video')
            self.daemon.store.add_pending_attachment('chat', video, 'file', video.name)
            prompt = FeishuInboundMessage('e', 'prompt-video', 'chat', 'p2p', 'user', 'user', 'text', 'inspect video')
            with patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=root):
                self.daemon._handle_prompt(prompt)
            self.assertEqual(self.daemon.chat_adapter.sent_files[-1], [video])
            self.assertFalse(video.exists())
            self.assertEqual(self.pending_attachments, [])

    def test_chat_result_is_delivered_before_input_cache_cleanup(self):
        self.selected_surface = 'chat'
        events = []
        original_reply = self.daemon.replies.reply_text

        def reply(*args, **kwargs):
            events.append('reply')
            return original_reply(*args, **kwargs)

        self.daemon.replies.reply_text = reply
        with patch.object(self.daemon, '_clear_pending_attachments', side_effect=lambda *_args: events.append('clear')):
            result = self.daemon._handle_prompt(
                FeishuInboundMessage('e', 'prompt-order', 'chat', 'p2p', 'user', 'user', 'text', 'inspect')
            )
        self.assertEqual(result, ['reply'])
        self.assertEqual(events, ['reply', 'clear'])

    def test_chat_cache_cleanup_failure_does_not_reclassify_delivered_result_as_failed(self):
        self.selected_surface = 'chat'
        with patch.object(self.daemon, '_clear_pending_attachments', side_effect=RuntimeError('database busy')):
            result = self.daemon._handle_prompt(
                FeishuInboundMessage('e', 'prompt-cleanup-fail', 'chat', 'p2p', 'user', 'user', 'text', 'inspect')
            )
        self.assertEqual(result, ['reply'])
        self.assertEqual(self.daemon.replies.text[-1], 'chat answer')

    def test_chat_attachment_cache_is_retained_when_chat_send_fails(self):
        self.selected_surface = 'chat'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'feishu' / 'inbox'
            inbox.mkdir(parents=True)
            video = inbox / 'clip.mp4'
            video.write_bytes(b'video')
            self.daemon.store.add_pending_attachment('chat', video, 'file', video.name)
            prompt = FeishuInboundMessage('e', 'prompt-video-fail', 'chat', 'p2p', 'user', 'user', 'text', 'inspect video')
            with patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=root), patch.object(self.daemon.chat_adapter, 'send_message', side_effect=RuntimeError('send failed')):
                with self.assertRaises(RuntimeError):
                    self.daemon._handle_prompt(prompt)
            self.assertTrue(video.exists())
            self.assertEqual(len(self.pending_attachments), 1)

    def test_attachment_cleanup_removes_cfr_cache_and_codex_stage_but_not_user_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'feishu' / 'inbox'
            inbox.mkdir(parents=True)
            cached = inbox / 'cached.pdf'
            cached.write_bytes(b'cached')
            staged = root / 'workspace' / '.cfr' / 'attachments' / 'staged.pdf'
            staged.parent.mkdir(parents=True)
            staged.write_bytes(b'staged')
            user_source = root / 'user-source.pdf'
            user_source.write_bytes(b'user')
            cached_item = self.daemon.store.add_pending_attachment('chat', cached, 'file', cached.name)
            user_item = self.daemon.store.add_pending_attachment('chat', user_source, 'file', user_source.name)
            with patch('cfr.feishu.daemon.resolve_cfr_config_dir', return_value=root):
                self.daemon._clear_pending_attachments('chat', [cached_item, user_item], [staged])
            self.assertFalse(cached.exists())
            self.assertFalse(staged.exists())
            self.assertTrue(user_source.exists())
            self.assertEqual(self.pending_attachments, [])

    def test_model_default_preserves_other_controlled_defaults(self):
        defaults = {'available': True, 'codex_model_defaults': {
            'model': {'effective_value': 'model-one'}, 'reasoning_effort': {'effective_value': 'high'}, 'service_tier': {'effective_value': 'priority'},
        }}
        with patch('cfr.control.codex_catalog.models', return_value=CATALOG), patch('cfr.control.codex_settings.read', return_value=defaults), patch('cfr.control.codex_settings.write') as write:
            self.daemon._handle_command(self.message, CommandParser().parse('/model default 2'))
        write.assert_called_once_with({'model': 'model-two', 'reasoning_effort': 'high', 'service_tier': 'priority'})
        self.assertEqual(self.daemon.adapter.updates, [])

    def test_mode_uses_schema_confirmed_native_shape(self):
        modes = {'available': True, 'data': [{'name': 'Default', 'mode': 'default', 'model': None, 'reasoning_effort': None}, {'name': 'Plan', 'mode': 'plan', 'model': None, 'reasoning_effort': 'medium'}]}
        with patch('cfr.control.codex_catalog.collaboration_modes', return_value=modes):
            self.daemon._handle_command(self.message, CommandParser().parse('/mode plan'))
        self.assertEqual(self.daemon.adapter.updates, [('thread-1', {'collaboration_mode': {'mode': 'plan', 'settings': {'model': 'model-one', 'reasoning_effort': 'medium'}}})])

    def test_workspace_switch_preserves_native_binding_history(self):
        calls = []
        self.daemon.store = SimpleNamespace(get_session=lambda _chat: self.session, switch_to_pending_session=lambda *args: calls.append(args))
        self.daemon.settings = SimpleNamespace(allowed_workspace_roots=(Path.cwd(),), database=Path('db.sqlite3'))
        with patch('cfr.feishu.daemon.validate_workspace', return_value=Path.cwd()):
            self.daemon._handle_command(self.message, CommandParser().parse(f'/workspace {Path.cwd()}'))
        self.assertEqual(calls[0][0], 'chat')
        self.assertIn('已选择工作区', self.daemon.replies.text[-1])

    def test_workspace_number_selects_allowlisted_root_without_full_path(self):
        calls = []
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            roots = (Path(first), Path(second))
            self.daemon.settings = SimpleNamespace(allowed_workspace_roots=roots, database=Path('db.sqlite3'))
            self.daemon.store = SimpleNamespace(
                get_session=lambda _chat: self.session,
                switch_to_pending_session=lambda *args: calls.append(args),
            )
            self.daemon._handle_command(self.message, CommandParser().parse('/workspace 2'))
        self.assertEqual(Path(calls[0][3]).resolve(), roots[1].resolve())
        self.assertIn(roots[1].name, self.daemon.replies.text[-1])

    def test_pending_session_model_and_reasoning_are_saved_before_thread_creation(self):
        pending = {}
        session = SimpleNamespace(thread_id=None, pending_cwd=str(Path.cwd()), state='pending_initial')
        def update_pending(_chat, **changes):
            pending.update(changes)
            return dict(pending)
        self.daemon.store = SimpleNamespace(
            get_session=lambda _chat: session,
            get_pending_thread_settings=lambda _chat: dict(pending),
            update_pending_thread_settings=update_pending,
        )
        self.daemon._handle_command(self.message, CommandParser().parse('/model 2'))
        self.assertEqual(pending['model'], 'model-two')
        self.assertIsNone(pending['reasoning_effort'])
        self.assertEqual(self.daemon.adapter.updates, [])
        self.daemon._handle_command(self.message, CommandParser().parse('/reasoning medium'))
        self.assertEqual(pending['reasoning_effort'], 'medium')
        self.assertEqual(self.daemon.adapter.updates, [])
        self.assertIn('thread/start', self.daemon.replies.text[-1])

    def test_session_accepts_displayed_thread_suffix_and_same_binding_is_idempotent(self):
        binding = SimpleNamespace(thread_id='01999999-aaaa-bbbb-cccc-00000665bef3', thread_name='Feishu-test', cwd=Path.cwd())
        current = SimpleNamespace(thread_id=binding.thread_id, pending_cwd=None, state='bound')
        self.daemon.store = SimpleNamespace(get_session=lambda _chat: current)
        self.daemon.binding_store = SimpleNamespace(list_bindings=lambda limit=None: [binding] if limit is None else [binding][:limit])
        self.daemon._handle_command(self.message, CommandParser().parse('/session 0665bef3'))
        self.assertIn('已经绑定该 CFR 线程', self.daemon.replies.text[-1])

    def test_code_new_without_path_reuses_current_workspace_and_replaces_feishu_session(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            self.daemon.store = SimpleNamespace(
                get_session=lambda _chat: self.session,
                switch_to_pending_session=lambda *args: calls.append(args),
                create_pending_session=lambda *args: self.fail('existing session should switch'),
            )
            self.daemon.binding_store = SimpleNamespace(get_binding=lambda _thread: SimpleNamespace(cwd=workspace))
            self.daemon.settings = SimpleNamespace(allowed_workspace_roots=(workspace,), database=Path('db.sqlite3'))
            self.daemon._handle_command(self.message, CommandParser().parse('/new'))
        self.assertEqual(Path(calls[0][3]).resolve(), workspace.resolve())
        self.assertIn('新的 CFR/Codex session', self.daemon.replies.text[-1])

    def test_code_new_with_path_switches_existing_session_to_requested_workspace(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            self.daemon.store = SimpleNamespace(
                get_session=lambda _chat: self.session,
                switch_to_pending_session=lambda *args: calls.append(args),
                create_pending_session=lambda *args: self.fail('existing session should switch'),
            )
            self.daemon.settings = SimpleNamespace(allowed_workspace_roots=(workspace,), database=Path('db.sqlite3'))
            self.daemon._handle_command(self.message, CommandParser().parse(f'/new {workspace}'))
        self.assertEqual(Path(calls[0][3]).resolve(), workspace.resolve())

    def test_code_new_without_path_requires_an_existing_workspace(self):
        self.daemon.store = SimpleNamespace(get_session=lambda _chat: None)
        with self.assertRaises(StructuredError) as caught:
            self.daemon._handle_command(self.message, CommandParser().parse('/new'))
        self.assertEqual(caught.exception.code, 'FEISHU_WORKSPACE_REQUIRED')

    def test_codex_final_links_send_only_fresh_generated_artifacts(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as temp_directory:
            workspace = Path(directory)
            xlsx = workspace / 'generated.xlsx'
            image = Path(temp_directory) / 'frame.jpg'
            video = workspace / 'clip.mp4'
            stale = workspace / 'old.txt'
            xlsx.write_bytes(b'xlsx')
            image.write_bytes(b'jpg')
            video.write_bytes(b'mp4')
            stale.write_bytes(b'old')
            old = time.time() - 60
            import os
            os.utime(stale, (old, old))
            text = (
                f'[表格]({xlsx})\n'
                f'[帧]({image})\n'
                f'[视频]({video})\n'
                f'[旧文件]({stale})\n'
                '[网页](https://example.com/report.pdf)'
            )
            with patch('cfr.feishu.code_runtime.tempfile.gettempdir', return_value=temp_directory):
                ids = self.daemon._reply_codex_artifacts(self.message, text, workspace, time.time() - 5)
            self.assertEqual(ids, ['file-reply', 'image-reply', 'video-reply'])
            self.assertEqual([path.resolve() for path in self.daemon.replies.files], [xlsx.resolve()])
            self.assertEqual(self.daemon.replies.images, [b'jpg'])
            self.assertEqual([path.resolve() for path in self.daemon.replies.videos], [video.resolve()])


if __name__ == '__main__':
    unittest.main()
