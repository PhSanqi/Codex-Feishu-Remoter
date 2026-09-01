import asyncio
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cfr.codex.binding import CodexAdapter
from cfr.feishu.commands import CommandParser, HELP_TEXT
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
    def __init__(self): self.text = []
    def reply_text(self, _id, text, *_args, **_kwargs): self.text.append(text); return ['reply']


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


class CommandSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.daemon = FeishuDaemon.__new__(FeishuDaemon)
        self.daemon.parser = CommandParser()
        self.daemon.replies = _Replies()
        self.daemon.adapter = _Adapter()
        self.daemon.settings = SimpleNamespace(allowed_workspace_roots=(Path.cwd(),), database=Path('db.sqlite3'))
        self.session = SimpleNamespace(thread_id='thread-1', pending_cwd=None)
        self.daemon.store = SimpleNamespace(get_session=lambda _chat: self.session)
        self.daemon.binding_store = SimpleNamespace(list_bindings=lambda: [])
        self.daemon._model_catalog = CATALOG['data']
        self.daemon._catalog_warm_started = True
        self.daemon._catalog_error = None
        self.message = FeishuInboundMessage('e', 'm', 'chat', 'p2p', 'user', 'user', 'text', '/model 2')

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

    def test_steer_uses_active_thread_client_path(self):
        self.daemon._handle_command(self.message, CommandParser().parse('/steer 继续检查'))
        self.assertEqual(self.daemon.adapter.steers, [('thread-1', '继续检查')])
        self.assertIn('软转向', self.daemon.replies.text[-1])
        self.assertIn('可能继续一段', self.daemon.replies.text[-1])
        self.assertIn('/stop', self.daemon.replies.text[-1])

    def test_help_makes_read_and_mutation_command_semantics_explicit(self):
        for text in ('/model', '/model <编号|模型ID|模型名>', '/model default', '/reasoning', '/reasoning <编号|档位>', '/sessions', '/session <编号|线程ID>', '/steer', '/redirect'):
            self.assertIn(text, HELP_TEXT)
        self.assertIn('不创建新 Turn', HELP_TEXT)
        self.assertIn('软转向', HELP_TEXT)
        self.assertIn('不保证立即打断当前生成内容', HELP_TEXT)
        self.assertIn('/stop：停止当前 Turn，不自动继续', HELP_TEXT)
        self.assertIn('Progress Card 只显示最新执行状态', HELP_TEXT)
        self.assertIn('同一个 Thread 中排队创建新 Turn', HELP_TEXT)
        self.assertIn('不自动继续', HELP_TEXT)

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
        self.assertIn('已切换到新工作区', self.daemon.replies.text[-1])


if __name__ == '__main__':
    unittest.main()
