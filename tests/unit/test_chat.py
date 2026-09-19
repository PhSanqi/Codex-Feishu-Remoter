from pathlib import Path
import base64
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch, call

from cfr.chat import ChromeChatAdapter, ChromeDevToolsMcp, chat_setup_snapshot
from cfr.core.models import StructuredError


def _stream_download_evaluator(*downloads):
    downloads = list(downloads)
    active = {}
    next_download = 0

    def evaluate(_page_id, script, **_kwargs):
        nonlocal next_download
        if 'Download file' in script or '下载文件' in script:
            return {'ok': False, 'stage': 'capture'}
        if '__cfrDownloadStreams.set' in script:
            item = downloads[next_download]
            next_download += 1
            token = f'test-stream-{next_download}'
            data = bytes(item['data'])
            active[token] = {'data': data, 'sent': False}
            return {
                'ok': True,
                'token': token,
                'contentType': item.get('contentType', 'application/octet-stream'),
                'declaredSize': len(data),
                'fileName': item.get('fileName'),
                'source': item.get('source', ''),
            }
        token_match = re.search(r'const token = ("[^"]+")', script)
        token = json.loads(token_match.group(1)) if token_match else None
        state = active.get(token)
        if 'state.reader.read' in script:
            if state is None:
                return {'ok': False, 'stage': 'state'}
            if state['sent']:
                active.pop(token, None)
                return {'ok': True, 'done': True, 'total': len(state['data'])}
            state['sent'] = True
            return {
                'ok': True,
                'done': False,
                'size': len(state['data']),
                'total': len(state['data']),
                'base64': base64.b64encode(state['data']).decode('ascii'),
            }
        if 'state.reader.cancel' in script:
            active.pop(token, None)
            return True
        raise AssertionError('unexpected evaluate script')

    return evaluate


class ChatBrowserRuntimeTests(unittest.TestCase):
    def test_mcp_stdout_drops_unconsumed_notifications(self):
        mcp = ChromeDevToolsMcp(command=['unused'])
        proc = Mock()
        proc.stdout = iter([
            '{"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n',
            '{"jsonrpc":"2.0","id":7,"result":{}}\n',
            '{"jsonrpc":"2.0","id":9,"method":"roots/list","params":{}}\n',
        ])
        mcp.proc = proc
        mcp._read_stdout()
        self.assertEqual(mcp._messages.qsize(), 2)
        self.assertEqual(mcp._messages.get_nowait()['id'], 7)
        self.assertEqual(mcp._messages.get_nowait()['method'], 'roots/list')

    def test_dedicated_is_default_and_uses_persistent_profile(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.chat.shutil.which', return_value='npx'):
            mcp = ChromeDevToolsMcp(user_data_dir=Path(directory) / 'profile')
        self.assertEqual(mcp.mode, 'dedicated')
        self.assertIn('--prefer-offline', mcp.command)
        self.assertIn('--userDataDir', mcp.command)
        self.assertNotIn('--autoConnect', mcp.command)

    def test_shared_mode_remains_explicit_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.chat.shutil.which', return_value='npx'):
            mcp = ChromeDevToolsMcp(mode='shared', user_data_dir=Path(directory) / 'unused')
        self.assertIn('--autoConnect', mcp.command)
        self.assertNotIn('--userDataDir', mcp.command)

    def test_shared_mode_tracks_system_chrome_profile_without_creating_cfr_profile(self):
        system_profile = Path('/home/test/.config/google-chrome/Default')
        with patch('cfr.chat.shutil.which', return_value='npx'), patch(
            'cfr.chat._system_chrome_profile_dir', return_value=system_profile
        ):
            mcp = ChromeDevToolsMcp(mode='shared')
        self.assertEqual(mcp.user_data_dir, system_profile)
        self.assertNotIn('--userDataDir', mcp.command)

    def test_embedded_mcp_connects_to_existing_webview_without_launching_chrome(self):
        with patch('cfr.chat.shutil.which', return_value='npx'):
            mcp = ChromeDevToolsMcp(
                mode='embedded',
                browser_url='http://127.0.0.1:9223',
                user_data_dir='C:/CFR/browser/embedded/profile',
            )
        self.assertIn('--browserUrl', mcp.command)
        self.assertIn('http://127.0.0.1:9223', mcp.command)
        self.assertNotIn('--userDataDir', mcp.command)

    def test_setup_auto_prefers_embedded_page_when_desktop_host_exists(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.chat.resolve_cfr_config_dir', return_value=Path(directory)), patch(
            'cfr.chat._chrome_executable', return_value='C:/Chrome/chrome.exe'
        ), patch('cfr.chat.shutil.which', return_value='npx'), patch('cfr.chat.linux_shared_tab_mode', return_value=False):
            legacy = Path(directory) / 'browser' / 'chatgpt-authenticated'
            legacy.parent.mkdir(parents=True)
            legacy.touch()
            state = chat_setup_snapshot(browser_backend='auto', embedded_available=True)
            self.assertEqual(state['effective_backend'], 'embedded')
            self.assertFalse(state['ready'])
            self.assertTrue(state['legacy_profile_preserved'])
            embedded = Path(directory) / 'browser' / 'embedded' / 'chatgpt-authenticated'
            embedded.parent.mkdir(parents=True)
            embedded.touch()
            migrated = chat_setup_snapshot(browser_backend='auto', embedded_available=True)
            self.assertEqual(migrated['effective_backend'], 'embedded')
            self.assertTrue(migrated['dedicated']['authenticated'])

    def test_setup_auto_uses_shared_system_chrome_on_linux(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.chat.resolve_cfr_config_dir', return_value=Path(directory)), patch(
            'cfr.chat._chrome_executable', return_value='/usr/bin/google-chrome'
        ), patch('cfr.chat.shutil.which', return_value='npx'), patch(
            'cfr.chat._system_chrome_profile_dir', return_value=Path('/home/test/.config/google-chrome/Default')
        ), patch('cfr.chat._profile_has_chatgpt_cookie', return_value=True), patch('cfr.chat.linux_shared_tab_mode', return_value=True):
            state = chat_setup_snapshot(browser_backend='auto', embedded_available=False)
        self.assertEqual(state['effective_backend'], 'shared')
        self.assertTrue(state['shared']['authenticated'])
        self.assertTrue(state['ready'])
        self.assertEqual(state['profile_dir'], '/home/test/.config/google-chrome/Default')

    def test_linux_setup_migrates_stale_embedded_preference_to_shared(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.chat.resolve_cfr_config_dir', return_value=Path(directory)), patch(
            'cfr.chat._chrome_executable', return_value='/usr/bin/google-chrome'
        ), patch('cfr.chat.shutil.which', return_value='npx'), patch(
            'cfr.chat._system_chrome_profile_dir', return_value=Path('/home/test/.config/google-chrome/Default')
        ), patch('cfr.chat._profile_has_chatgpt_cookie', return_value=True), patch('cfr.chat.os.name', 'posix'):
            state = chat_setup_snapshot(browser_backend='embedded', embedded_available=False)
        self.assertEqual(state['preference'], 'embedded')
        self.assertEqual(state['effective_backend'], 'shared')
        self.assertTrue(state['ready'])

    def test_shared_manual_login_reuses_normal_chrome_without_profile_or_automation_flags(self):
        mcp = Mock()
        mcp.mode = 'shared'
        mcp.running = False
        mcp.user_data_dir = Path('/unused')
        adapter = ChromeChatAdapter(mcp=mcp)
        with patch('cfr.chat._chrome_executable', return_value='/usr/bin/google-chrome'), patch('cfr.chat.subprocess.Popen') as popen:
            state = adapter.start_manual_login()
        self.assertEqual(state['mode'], 'shared')
        command = popen.call_args.args[0]
        self.assertEqual(command[0], '/usr/bin/google-chrome')
        self.assertIn('https://chatgpt.com/', command)
        self.assertFalse(any(value.startswith('--user-data-dir=') for value in command))
        self.assertFalse(any('automation' in value.lower() or 'webdriver' in value.lower() for value in command))
        mcp.close.assert_called_once_with()

    def test_shared_runtime_opens_missing_chatgpt_tab_in_background(self):
        mcp = Mock()
        mcp.mode = 'shared'
        mcp.user_data_dir = Path('/unused')
        mcp.text.return_value = '1: ChatGPT (https://chatgpt.com/)'
        adapter = ChromeChatAdapter(mcp=mcp)
        with patch.object(adapter, '_pages', return_value=[]), patch.object(adapter, '_mark_shared_owner_page') as mark:
            adapter._ensure_chatgpt_tab()
        self.assertIn(
            call(
            'new_page',
            {'url': 'https://chatgpt.com/', 'background': True, 'timeout': 15_000},
            timeout=20,
            ),
            mcp.tool.call_args_list,
        )
        mark.assert_called_once_with(1)

    def test_shared_unbound_read_never_uses_human_selected_chatgpt_tab(self):
        mcp = Mock()
        mcp.mode = 'shared'
        mcp.user_data_dir = Path('/unused')
        adapter = ChromeChatAdapter(mcp=mcp)
        with patch.object(adapter, '_pages', return_value=[
            {'id': 7, 'url': 'https://chatgpt.com/c/human', 'selected': True},
        ]), patch.object(adapter, '_shared_owner_page', return_value=None):
            self.assertIsNone(adapter._page({}, create=False))

    def test_shared_page_uses_only_cfr_marked_tab_even_when_human_chatgpt_is_selected(self):
        mcp = Mock()
        mcp.mode = 'shared'
        mcp.user_data_dir = Path('/unused')
        adapter = ChromeChatAdapter(mcp=mcp)
        pages = [
            {'id': 7, 'url': 'https://chatgpt.com/c/human', 'selected': True},
            {'id': 8, 'url': 'https://chatgpt.com/c/cfr', 'selected': False},
        ]
        with patch.object(adapter, '_pages', return_value=pages), patch.object(
            adapter,
            '_evaluate',
            side_effect=['', 'CFR_CHAT_SURFACE'],
        ):
            page = adapter._page({}, create=False)
        self.assertEqual(page['id'], 8)

    def test_embedded_login_wait_does_not_delete_or_spawn_legacy_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            shown = []
            mcp = SimpleNamespace(
                mode='embedded',
                user_data_dir=Path(directory) / 'browser' / 'embedded' / 'profile',
            )
            adapter = ChromeChatAdapter(mcp=mcp, show_browser=lambda: shown.append(True))
            state = adapter._waiting_for_login()
        self.assertEqual(state['error_code'], 'CHATGPT_EMBEDDED_LOGIN_REQUIRED')
        self.assertEqual(shown, [])

    def test_embedded_health_starts_existing_webview_bridge_on_demand(self):
        with tempfile.TemporaryDirectory() as directory:
            mcp = Mock()
            mcp.mode = 'embedded'
            mcp.running = False
            mcp.user_data_dir = Path(directory) / 'browser' / 'embedded' / 'profile'
            adapter = ChromeChatAdapter(mcp=mcp)
            adapter._ensure_chatgpt_tab = Mock()
            adapter._remote_debugging_enabled = Mock(return_value=True)
            adapter._pages = Mock(return_value=[{'id': 3, 'url': 'https://chatgpt.com/'}])
            adapter._state = Mock(return_value={'promptVisible': True, 'authenticated': True, 'blocked': False})
            state = adapter.health()
        self.assertTrue(state['available'])
        mcp.start.assert_called_once_with()
        adapter._ensure_chatgpt_tab.assert_called_once_with()

    def test_embedded_navigation_reuses_single_chatgpt_target_without_new_page(self):
        mcp = Mock()
        mcp.mode = 'embedded'
        mcp.user_data_dir = Path('C:/CFR/browser/embedded/profile')
        adapter = ChromeChatAdapter(mcp=mcp)
        before = [{'id': 1, 'url': 'https://chatgpt.com/', 'selected': True}]
        after = [{'id': 1, 'url': 'https://chatgpt.com/c/target', 'selected': True}]
        with patch.object(adapter, '_pages', side_effect=[before, after]):
            page = adapter._new_page('https://chatgpt.com/c/target')
        self.assertEqual(page['id'], 1)
        self.assertEqual(page['url'], 'https://chatgpt.com/c/target')
        self.assertEqual(mcp.tool.call_args.args[0], 'navigate_page')
        self.assertNotEqual(mcp.tool.call_args.args[0], 'new_page')

    def test_reasoning_power_snapshot_parses_native_chatgpt_effort(self):
        snapshot = 'uid=2_3 menuitem "Power" description="High, 3 of 4. Use Left and Right arrow keys to adjust power." keyshortcuts="ArrowLeft ArrowRight"'
        self.assertEqual(
            ChromeChatAdapter._parse_reasoning_power_snapshot(snapshot),
            {'uid': '2_3', 'label': 'High', 'index': 3, 'total': 4},
        )

    def test_reasoning_power_dom_fallback_reads_screen_reader_description(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._evaluate = Mock(return_value={
            'description': 'High, 3 of 4. Use Left and Right arrow keys to adjust power.',
            'now': '2',
            'min': '0',
            'max': '3',
        })
        self.assertEqual(
            adapter._reasoning_power_dom_state(1),
            {'uid': None, 'label': 'High', 'index': 3, 'total': 4},
        )

    def test_reasoning_power_dom_fallback_can_use_slider_numbers(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._evaluate = Mock(return_value={'description': '', 'now': '1', 'min': '0', 'max': '3'})
        self.assertEqual(
            adapter._reasoning_power_dom_state(1),
            {'uid': None, 'label': 'Medium', 'index': 2, 'total': 4},
        )

    def test_model_menu_snapshot_reads_real_web_model_states(self):
        snapshot = '\n'.join([
            'uid=4_0 menuitemradio "GPT-5.6 Sol" checked',
            'uid=4_1 menuitemradio "GPT-5.5"',
            'uid=4_2 menuitemradio "Pro" disableable disabled',
        ])
        self.assertEqual(ChromeChatAdapter._parse_model_menu_snapshot(snapshot), [
            {'uid': '4_0', 'name': 'GPT-5.6 Sol', 'selected': True, 'disabled': False},
            {'uid': '4_1', 'name': 'GPT-5.5', 'selected': False, 'disabled': False},
            {'uid': '4_2', 'name': 'Pro', 'selected': False, 'disabled': True},
        ])

    def test_hidden_webview_reasoning_menu_uses_focus_and_keyboard_activation(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        mcp = Mock()
        mcp.timeout = 5
        mcp.text.side_effect = lambda value: value
        closed = 'uid=1_0 button "Extra High"'
        opened = 'uid=2_3 menuitem "Power" description="Extra High, 4 of 4. Use Left and Right arrow keys to adjust power."'
        snapshots = iter([closed, opened])

        def tool(method, params=None, timeout=None):
            if method == 'take_snapshot':
                return next(snapshots)
            return ''

        mcp.tool.side_effect = tool
        adapter.mcp = mcp
        adapter._evaluate = Mock(return_value=True)
        state = adapter._open_reasoning_power(2)
        self.assertEqual(state['label'], 'Extra High')
        self.assertEqual(state['index'], 4)
        self.assertTrue(any(
            call.args[0] == 'press_key' and call.args[1]['key'] == 'Enter'
            for call in mcp.tool.call_args_list
        ))
        self.assertTrue(any('button.focus()' in call.args[1] for call in adapter._evaluate.call_args_list))

    def test_hidden_webview_model_menu_is_live_and_keyboard_opened(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        mcp = Mock()
        mcp.timeout = 5
        mcp.text.side_effect = lambda value: value
        model_menu = '\n'.join([
            'uid=4_0 menuitemradio "Latest" checked',
            'uid=4_1 menuitemradio "GPT-6 Astra"',
            'uid=4_2 menuitemradio "GPT-5.6 Sol"',
        ])
        snapshots = iter(['uid=1_0 button "Extra High"', model_menu])

        def tool(method, params=None, timeout=None):
            if method == 'take_snapshot':
                return next(snapshots)
            return ''

        mcp.tool.side_effect = tool
        adapter.mcp = mcp
        adapter._open_thinking_effort_menu = Mock(return_value='uid=2_1 menuitem "Select model"')
        adapter._evaluate = Mock(return_value=True)
        models = adapter._open_model_menu(2)
        self.assertEqual([item['name'] for item in models], ['Latest', 'GPT-6 Astra', 'GPT-5.6 Sol'])
        self.assertTrue(any(
            call.args[0] == 'press_key' and call.args[1]['key'] == 'Enter'
            for call in mcp.tool.call_args_list
        ))

    def test_hidden_webview_sidebar_is_opened_with_keyboard(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        mcp = Mock()
        mcp.timeout = 5
        mcp.tool.return_value = ''
        adapter.mcp = mcp
        adapter._evaluate = Mock(side_effect=['focused', False, True])
        with patch('cfr.chat.time.sleep'):
            self.assertTrue(adapter._ensure_sidebar_open(2))
        self.assertTrue(any(
            call.args[0] == 'press_key' and call.args[1]['key'] == 'Enter'
            for call in mcp.tool.call_args_list
        ))

    def test_chat_control_restarts_only_mcp_once_after_timeout(self):
        mcp = Mock()
        adapter = ChromeChatAdapter(mcp=mcp)
        operation = Mock(side_effect=[StructuredError('CHAT_BROWSER_MCP_TIMEOUT', 'timeout'), {'ok': True}])
        self.assertEqual(adapter._control_with_mcp_retry(operation), {'ok': True})
        self.assertEqual(operation.call_count, 2)
        mcp.close.assert_called_once_with()
        mcp.start.assert_called_once_with()

    def test_generated_file_without_dom_file_id_uses_real_card_download_url(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            'cfr.chat.resolve_cfr_config_dir', return_value=Path(directory)
        ):
            adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
            conversation = 'https://chatgpt.com/c/6a9aa85d-ec7c-83eb-8466-4957255d49a6'
            adapter._page = Mock(return_value={'id': 7, 'url': conversation})
            adapter._wait_ready = Mock(return_value={'url': conversation})
            adapter._wait_history = Mock()
            signed = 'https://chatgpt.com/backend-api/estuary/content?id=file_00000000e6348243b37513f289fabb18&sig=test'
            adapter._capture_generated_file_download_url = Mock(return_value=signed)
            adapter._evaluate = Mock(side_effect=_stream_download_evaluator({
                'data': b'PK\x03\x04',
                'contentType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'fileName': 'report.xlsx',
                'source': signed,
            }))
            result = adapter.download_generated_file_to_file(
                {'tab_id': '7', 'url': conversation},
                {'name': 'report.xlsx', 'file_id': None, 'href': '', 'sandbox_url': ''},
            )
            self.assertEqual(Path(result['path']).read_bytes(), b'PK\x03\x04')
            self.assertEqual(result['source'], signed)
            adapter._capture_generated_file_download_url.assert_called_once_with(7, 'report.xlsx')

    def test_mcp_roots_refresh_is_scoped_to_requested_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            mcp = ChromeDevToolsMcp(command=['unused'])
            proc = Mock()
            proc.poll.return_value = None
            proc.stdin = Mock()
            mcp.proc = proc
            mcp._messages.put({'jsonrpc': '2.0', 'id': 91, 'method': 'roots/list', 'params': {}})
            mcp.set_roots([root])
            writes = [json.loads(call.args[0]) for call in proc.stdin.write.call_args_list]
            self.assertEqual(writes[0]['method'], 'notifications/roots/list_changed')
            self.assertEqual(writes[1]['id'], 91)
            self.assertEqual(writes[1]['result']['roots'], [{'uri': root.as_uri(), 'name': root.name}])

    def test_login_bootstrap_uses_plain_chrome_without_automation_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            mcp = Mock()
            mcp.mode = 'dedicated'
            mcp.running = False
            mcp.user_data_dir = Path(directory) / 'profile'
            adapter = ChromeChatAdapter(mcp=mcp)
            with patch('cfr.chat._chrome_executable', return_value='chrome.exe'), patch.object(adapter, '_profile_browser_running', return_value=False), patch('cfr.chat.subprocess.Popen') as popen:
                adapter._launch_login_bootstrap()
            command = popen.call_args.args[0]
            self.assertEqual(command[0], 'chrome.exe')
            self.assertTrue(any(value.startswith('--user-data-dir=') for value in command))
            self.assertIn('https://chatgpt.com/auth/login', command)
            self.assertFalse(any('remote-debugging' in value or 'automation' in value or 'webdriver' in value for value in command))
            mcp.close.assert_called_once()

    def test_first_dedicated_start_waits_for_plain_browser_login_before_mcp(self):
        with tempfile.TemporaryDirectory() as directory:
            mcp = Mock()
            mcp.mode = 'dedicated'
            mcp.running = False
            mcp.user_data_dir = Path(directory) / 'profile'
            adapter = ChromeChatAdapter(mcp=mcp)
            with patch.object(adapter, '_profile_browser_running', return_value=False), patch.object(adapter, '_launch_login_bootstrap') as launch:
                state = adapter.start()
            self.assertEqual(state['status'], 'waiting_user')
            self.assertEqual(state['error_code'], 'CHATGPT_LOGIN_BOOTSTRAP_REQUIRED')
            launch.assert_called_once_with()
            mcp.start.assert_not_called()

    def test_manual_login_stops_automation_and_launches_plain_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            mcp = Mock()
            mcp.mode = 'dedicated'
            mcp.running = True
            mcp.user_data_dir = Path(directory) / 'profile'
            adapter = ChromeChatAdapter(mcp=mcp)
            adapter._auth_marker.parent.mkdir(parents=True, exist_ok=True)
            adapter._auth_marker.touch()
            with patch.object(adapter, '_stop_profile_browser') as stop, patch.object(adapter, '_launch_login_bootstrap') as launch:
                state = adapter.start_manual_login()
            mcp.close.assert_called_once_with()
            stop.assert_called_once_with()
            launch.assert_called_once_with()
            self.assertFalse(adapter._auth_marker.exists())
            self.assertEqual(state['status'], 'waiting_user')
            self.assertTrue(state['manual_login'])

    def test_manual_login_verify_does_not_start_automation_while_plain_browser_is_open(self):
        with tempfile.TemporaryDirectory() as directory:
            mcp = Mock()
            mcp.mode = 'dedicated'
            mcp.running = False
            mcp.user_data_dir = Path(directory) / 'profile'
            adapter = ChromeChatAdapter(mcp=mcp)
            with patch.object(adapter, '_profile_browser_running', return_value=True):
                state = adapter.finish_manual_login()
            self.assertEqual(state['error_code'], 'CHAT_MANUAL_LOGIN_BROWSER_STILL_OPEN')
            mcp.start.assert_not_called()

    def test_native_project_and_conversation_identity_is_parsed_from_url(self):
        identity = ChromeChatAdapter.parse_identity(
            'https://chatgpt.com/g/g-p-6a96bc9061ac8191a6715148c78c090d-cfr/c/6a96bca1-75e8-83eb-b4b1-41ad6003451a'
        )
        self.assertEqual(identity['project_id'], 'g-p-6a96bc9061ac8191a6715148c78c090d')
        self.assertEqual(identity['conversation_id'], '6a96bca1-75e8-83eb-b4b1-41ad6003451a')

    def test_plain_conversation_has_no_project_identity(self):
        identity = ChromeChatAdapter.parse_identity('https://chatgpt.com/c/6a951baf-6ae4-83eb-bb98-10e52c6b7cee')
        self.assertIsNone(identity['project_id'])
        self.assertEqual(identity['conversation_id'], '6a951baf-6ae4-83eb-bb98-10e52c6b7cee')

    def test_project_conversations_wait_for_project_page_hydration(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/g/g-p-aabbcc/project'})
        adapter._ensure_sidebar_open = Mock(return_value=True)
        hydrated = {'ready': True, 'links': [{
            'title': 'CFR chat\npreview text',
            'url': 'https://chatgpt.com/g/g-p-aabbcc-cfr/c/conv-1',
        }]}
        adapter._evaluate = Mock(side_effect=[{'ready': False, 'links': []}, hydrated, hydrated, hydrated, hydrated])
        with patch('cfr.chat.time.sleep'):
            conversations = adapter.list_project_conversations()
        self.assertEqual(conversations[0]['conversation_id'], 'conv-1')
        self.assertEqual(conversations[0]['title'], 'CFR chat')
        self.assertEqual(adapter._evaluate.call_count, 5)

    def test_plain_conversations_wait_past_initial_empty_sidebar_hydration(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/'})
        adapter._ensure_sidebar_open = Mock(return_value=True)
        empty = {'ready': True, 'links': []}
        hydrated = {'ready': True, 'links': [{
            'title': '普通对话',
            'url': 'https://chatgpt.com/c/plain-1',
        }]}
        adapter._evaluate = Mock(side_effect=[empty, empty, hydrated, hydrated, hydrated, hydrated])
        with patch('cfr.chat.time.sleep'):
            conversations = adapter.list_project_conversations()
        self.assertEqual(conversations[0]['conversation_id'], 'plain-1')
        self.assertIsNone(conversations[0]['project_id'])

    def test_conversation_history_returns_recent_visible_turns(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
        adapter._restore_bound_url = Mock()
        adapter._state = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1', 'blocked': False, 'promptVisible': True})
        adapter._evaluate = Mock(return_value=[
            {'role': 'user', 'text': 'one'},
            {'role': 'assistant', 'text': 'two'},
            {'role': 'user', 'text': 'three'},
        ])
        history = adapter.conversation_history({'url': 'https://chatgpt.com/c/conv-1'}, limit=2)
        self.assertEqual(history, [
            {'role': 'assistant', 'text': 'two'},
            {'role': 'user', 'text': 'three'},
        ])
        script = adapter._evaluate.call_args.args[1]
        self.assertIn('const limit = 2', script)
        self.assertIn('roots.slice(-limit)', script)
        self.assertIn('text.slice(0, 1597)', script)

    def test_conversation_history_script_is_valid_javascript(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is unavailable')
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
        adapter._restore_bound_url = Mock()
        adapter._state = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1', 'blocked': False, 'promptVisible': True})
        adapter._evaluate = Mock(return_value=[{'role': 'assistant', 'text': 'ready'}])
        adapter.conversation_history({'url': 'https://chatgpt.com/c/conv-1'})
        script = adapter._evaluate.call_args.args[1]
        checked = subprocess.run(
            [node, '-e', 'new Function("return (" + process.argv[1] + ")");', script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_project_catalog_waits_for_hydration_and_accumulates_multiple_projects(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        one = {'name': 'CFR', 'url': 'https://chatgpt.com/g/g-p-aabbcc-cfr/project', 'project_id': 'g-p-aabbcc'}
        two = {'name': '论文', 'url': 'https://chatgpt.com/g/g-p-ddeeff-paper/project', 'project_id': 'g-p-ddeeff'}
        adapter._evaluate = Mock(side_effect=[
            {'ready': False, 'loading': True, 'projects': []},
            {'ready': True, 'loading': True, 'projects': [one]},
            {'ready': True, 'loading': False, 'projects': [one, two]},
            {'ready': True, 'loading': False, 'projects': [one, two]},
            {'ready': True, 'loading': False, 'projects': [one, two]},
            {'ready': True, 'loading': False, 'projects': [one, two]},
        ])
        with patch('cfr.chat.time.sleep'):
            projects = adapter._wait_project_catalog(3)
        self.assertEqual([item['name'] for item in projects], ['CFR', '论文'])
        self.assertEqual(adapter._evaluate.call_count, 6)
        self.assertIn("split('\\n')[0]", adapter._evaluate.call_args.args[1])

    def test_project_catalog_keeps_row_only_projects_and_upgrades_native_url_without_duplicates(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        row_only = {'name': 'CFR', 'url': '', 'project_id': None}
        resolved = {'name': 'CFR', 'url': 'https://chatgpt.com/g/g-p-aabbcc-cfr/project', 'project_id': 'g-p-aabbcc'}
        paper = {'name': '论文', 'url': '', 'project_id': None}
        adapter._evaluate = Mock(side_effect=[
            {'ready': True, 'loading': True, 'projects': [row_only, paper]},
            {'ready': True, 'loading': False, 'projects': [resolved, paper]},
            {'ready': True, 'loading': False, 'projects': [resolved, paper]},
            {'ready': True, 'loading': False, 'projects': [resolved, paper]},
            {'ready': True, 'loading': False, 'projects': [resolved, paper]},
        ])
        with patch('cfr.chat.time.sleep'):
            projects = adapter._wait_project_catalog(3)
        self.assertEqual([item['name'] for item in projects], ['CFR', '论文'])
        self.assertEqual(sum(item['name'] == 'CFR' for item in projects), 1)
        self.assertEqual(next(item for item in projects if item['name'] == 'CFR')['project_id'], 'g-p-aabbcc')

    def test_open_project_prefers_discovered_native_project_url(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.navigate = Mock(side_effect=[
            {'tab_id': '3', 'url': 'https://chatgpt.com/projects'},
            {'tab_id': '4', 'url': 'https://chatgpt.com/g/g-p-aabbcc-cfr/project', 'project_id': 'g-p-aabbcc', 'conversation_id': None},
        ])
        adapter._wait_project_catalog = Mock(return_value=[{
            'name': 'CFR', 'url': 'https://chatgpt.com/g/g-p-aabbcc-cfr/project', 'project_id': 'g-p-aabbcc',
        }])
        result = adapter.open_project({'url': 'https://chatgpt.com/'}, 'CFR')
        self.assertEqual(result['project_id'], 'g-p-aabbcc')
        self.assertEqual(adapter.navigate.call_args_list[-1].args[1], 'https://chatgpt.com/g/g-p-aabbcc-cfr/project')

    def test_open_project_accepts_display_index(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.navigate = Mock(side_effect=[
            {'tab_id': '3', 'url': 'https://chatgpt.com/projects'},
            {'tab_id': '4', 'url': 'https://chatgpt.com/g/g-p-ddeeff-paper/project', 'project_id': 'g-p-ddeeff', 'conversation_id': None},
        ])
        adapter._wait_project_catalog = Mock(return_value=[
            {'name': 'CFR', 'url': 'https://chatgpt.com/g/g-p-aabbcc-cfr/project', 'project_id': 'g-p-aabbcc'},
            {'name': '论文', 'url': 'https://chatgpt.com/g/g-p-ddeeff-paper/project', 'project_id': 'g-p-ddeeff'},
        ])
        result = adapter.open_project({'url': 'https://chatgpt.com/'}, '2')
        self.assertEqual(result['project_id'], 'g-p-ddeeff')
        self.assertEqual(adapter.navigate.call_args_list[-1].args[1], 'https://chatgpt.com/g/g-p-ddeeff-paper/project')

    def test_open_project_last_display_index_exits_to_plain_chat(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.navigate = Mock(side_effect=[
            {'tab_id': '3', 'url': 'https://chatgpt.com/projects'},
            {'tab_id': '4', 'url': 'https://chatgpt.com/', 'project_id': None, 'conversation_id': None},
        ])
        adapter._wait_project_catalog = Mock(return_value=[
            {'name': 'CFR', 'url': 'https://chatgpt.com/g/g-p-aabbcc-cfr/project', 'project_id': 'g-p-aabbcc'},
            {'name': '论文', 'url': 'https://chatgpt.com/g/g-p-ddeeff-paper/project', 'project_id': 'g-p-ddeeff'},
        ])
        result = adapter.open_project({'url': 'https://chatgpt.com/g/g-p-aabbcc-cfr/project'}, '3')
        self.assertIsNone(result['project_id'])
        self.assertEqual(adapter.navigate.call_args_list[-1].args[1], 'https://chatgpt.com/')

    def test_open_project_accepts_display_index_when_project_rows_have_no_urls(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.navigate = Mock(return_value={'tab_id': '3', 'url': 'https://chatgpt.com/projects'})
        adapter._wait_project_catalog = Mock(return_value=[
            {'name': 'CFR', 'url': None, 'project_id': None},
            {'name': '模型路由', 'url': None, 'project_id': None},
        ])
        adapter.mcp = Mock()
        adapter._evaluate = Mock(return_value=True)
        adapter._pages = Mock(return_value=[{
            'id': 3, 'url': 'https://chatgpt.com/g/g-p-ddeeff-model/project', 'selected': False,
        }])
        result = adapter.open_project({'url': 'https://chatgpt.com/'}, '2')
        self.assertEqual(result['project_id'], 'g-p-ddeeff')
        self.assertIn(json.dumps('模型路由'), adapter._evaluate.call_args.args[1])
        self.assertIn("split('\\n')[0]", adapter._evaluate.call_args.args[1])

    def test_wait_ready_tolerates_spa_hydration(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._state = Mock(side_effect=[
            {'promptVisible': False, 'blocked': False},
            {'promptVisible': True, 'blocked': False},
        ])
        with patch('cfr.chat.time.sleep'):
            state = adapter._wait_ready(3)
        self.assertTrue(state['promptVisible'])
        self.assertEqual(adapter._state.call_count, 2)

    def test_wait_ready_dismisses_conversation_file_viewer_overlay_once(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.mcp = Mock()
        adapter._observe_page_health = Mock()
        adapter._state = Mock(side_effect=[
            {'promptVisible': False, 'blocked': False, 'url': 'https://chatgpt.com/c/conv-1'},
            {'promptVisible': True, 'blocked': False, 'url': 'https://chatgpt.com/c/conv-1'},
        ])
        ticks = iter([0.0, 0.0, 0.0, 1.0, 1.0])
        with patch('cfr.chat.time.monotonic', side_effect=lambda: next(ticks)), patch('cfr.chat.time.sleep'):
            state = adapter._wait_ready(3)
        self.assertTrue(state['promptVisible'])
        adapter.mcp.tool.assert_called_once_with('press_key', {'pageId': 3, 'key': 'Escape'})

    def test_rate_limited_page_fails_closed_even_when_composer_is_visible(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._state = Mock(return_value={'promptVisible': True, 'blocked': True, 'kind': 'rate_limited'})
        with self.assertRaises(StructuredError) as caught:
            adapter._wait_ready(3)
        self.assertEqual(caught.exception.code, 'CHATGPT_RATE_LIMITED')
        self.assertIn('限制', caught.exception.message)

    def test_wait_history_does_not_return_before_existing_messages_load(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._history_items = Mock(side_effect=[[], [{'role': 'assistant', 'text': 'loaded'}]])
        adapter._state = Mock(return_value={
            'assistantCount': 2,
            'userCount': 2,
            'blocked': False,
        })
        with patch('cfr.chat.time.sleep'):
            state = adapter._wait_history(3, {'assistantCount': 0, 'userCount': 0, 'blocked': False})
        self.assertEqual(state['assistantCount'], 2)
        self.assertEqual(adapter._history_items.call_count, 2)

    def test_wait_answer_streams_visible_web_reasoning_and_output(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.query_timeout = 60
        adapter._state = Mock(return_value={
            'assistantCount': 1,
            'assistantText': 'final answer',
            'reasoningText': 'visible web reasoning',
            'generating': False,
            'blocked': False,
            'url': 'https://chatgpt.com/c/conv-1',
        })
        events = []
        ticks = iter(range(100))
        with patch('cfr.chat.time.monotonic', side_effect=lambda: next(ticks)), patch('cfr.chat.time.sleep'):
            result = adapter._wait_answer(3, {'assistantCount': 0, 'assistantText': ''}, on_progress=events.append)
        self.assertEqual(result['text'], 'final answer')
        self.assertEqual(result['reasoning_text'], 'visible web reasoning')
        self.assertTrue(any(event.get('reasoning_text') == 'visible web reasoning' for event in events))
        self.assertTrue(any(event.get('answer_preview') == 'final answer' for event in events))

    def test_wait_answer_extends_timeout_while_reasoning_is_progressing(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.query_timeout = 5
        clock = [0.0]
        states = iter([
            {'assistantCount': 0, 'assistantText': '', 'reasoningText': 'a', 'reasoningTextLength': 1, 'generating': True, 'blocked': False},
            {'assistantCount': 0, 'assistantText': '', 'reasoningText': 'ab', 'reasoningTextLength': 2, 'generating': True, 'blocked': False},
            {'assistantCount': 1, 'assistantText': 'done', 'reasoningText': 'ab', 'reasoningTextLength': 2, 'generating': False, 'blocked': False, 'url': 'https://chatgpt.com/c/conv-1'},
            {'assistantCount': 1, 'assistantText': 'done', 'reasoningText': 'ab', 'reasoningTextLength': 2, 'generating': False, 'blocked': False, 'url': 'https://chatgpt.com/c/conv-1'},
        ])

        def state(*_args, **_kwargs):
            clock[0] += 4
            return next(states)

        adapter._state = Mock(side_effect=state)
        with patch('cfr.chat.time.monotonic', side_effect=lambda: clock[0]), patch('cfr.chat.time.sleep'):
            result = adapter._wait_answer(3, {'assistantCount': 0, 'assistantText': ''}, settle_seconds=0)
        self.assertEqual(result['text'], 'done')

    def test_wait_answer_accepts_new_generated_image_without_text(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.query_timeout = 60
        adapter._state = Mock(return_value={
            'assistantCount': 0,
            'assistantText': '',
            'generatedImageSrc': 'https://chatgpt.com/backend-api/estuary/content?id=file-new',
            'reasoningText': '',
            'generating': False,
            'blocked': False,
            'url': 'https://chatgpt.com/c/conv-image',
        })
        ticks = iter(range(100))
        with patch('cfr.chat.time.monotonic', side_effect=lambda: next(ticks)), patch('cfr.chat.time.sleep'):
            result = adapter._wait_answer(3, {
                'assistantCount': 0,
                'assistantText': '',
                'generatedImageSrc': '',
            })
        self.assertEqual(result['text'], '')
        self.assertIn('file-new', result['generated_image_src'])

    def test_wait_answer_accepts_new_generated_file_without_text(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.query_timeout = 60
        adapter._state = Mock(return_value={
            'assistantCount': 1,
            'assistantText': '',
            'generatedImageSrc': '',
            'generatedFileKey': 'file_new|sandbox:/mnt/data/report.xlsx',
            'reasoningText': '',
            'generating': False,
            'blocked': False,
            'url': 'https://chatgpt.com/c/conv-file',
        })
        ticks = iter(range(100))
        with patch('cfr.chat.time.monotonic', side_effect=lambda: next(ticks)), patch('cfr.chat.time.sleep'):
            result = adapter._wait_answer(3, {
                'assistantCount': 0,
                'assistantText': '',
                'generatedImageSrc': '',
                'generatedFileKey': '',
            })
        self.assertEqual(result['text'], '')
        self.assertIn('file_new', result['generated_file_key'])
        self.assertTrue(any(call.kwargs.get('include_artifacts') is False for call in adapter._state.call_args_list))

    def test_wait_send_started_accepts_user_turn_or_new_conversation_url(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._state = Mock(return_value={
            'userCount': 2, 'assistantCount': 1, 'promptLength': 20,
            'generating': False, 'url': 'https://chatgpt.com/c/existing',
        })
        self.assertTrue(adapter._wait_send_started(3, {
            'userCount': 1, 'assistantCount': 1, 'url': 'https://chatgpt.com/c/existing',
        }, timeout=1))
        adapter._state = Mock(return_value={
            'userCount': 1, 'assistantCount': 0, 'promptLength': 20,
            'generating': False, 'url': 'https://chatgpt.com/c/new-conversation',
        })
        self.assertTrue(adapter._wait_send_started(3, {
            'userCount': 1, 'assistantCount': 0, 'url': 'https://chatgpt.com/',
        }, timeout=1))

    def test_restore_generic_binding_does_not_overwrite_created_conversation(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._state = Mock(return_value={'url': 'https://chatgpt.com/c/new-conversation'})
        adapter.mcp = Mock()
        adapter._restore_bound_url(3, 'https://chatgpt.com/')
        adapter.mcp.tool.assert_not_called()

    def test_state_script_uses_visible_chatgpt_commentary_containers(self):
        script = ChromeChatAdapter._state_script()
        self.assertIn('data-streaming-response-status', script)
        self.assertIn('data-testid*="cot"', script)
        self.assertIn('reasoningText', script)
        self.assertIn('generatedImageSrc', script)
        self.assertIn("querySelectorAll('section[data-turn=\"assistant\"]')", script)
        self.assertIn('if (!assistants.length)', script)

    def test_state_script_supports_bounded_streaming_preview_without_artifact_walk(self):
        script = ChromeChatAdapter._state_script(text_limit=4000, include_artifacts=False)
        self.assertIn('const textLimit = 4000', script)
        self.assertIn('const includeArtifacts = false', script)
        self.assertIn('fullAssistantText.slice(-textLimit)', script)
        self.assertIn('assistantTextLength: fullAssistantText.length', script)
        self.assertIn('reasoningTextLength: fullReasoningText.length', script)
        self.assertIn('if (includeArtifacts && assistantTurn && !generating)', script)

    def test_state_script_is_valid_javascript(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is unavailable')
        script = ChromeChatAdapter._state_script()
        checked = subprocess.run(
            [node, '-e', 'new Function("return (" + process.argv[1] + ")");', script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn('generatedFileKey', script)
        self.assertIn("lastAssistant?.querySelectorAll('img')", script)

    def test_prompt_uid_ignores_search_word_in_current_draft(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.mcp = Mock()
        adapter.mcp.text.return_value = 'uid=1_55 textbox "CFR中的新聊天" multiline value="please search this"'
        adapter.mcp.tool.return_value = {}
        self.assertEqual(adapter._prompt_uid(3), '1_55')

    def test_deep_research_snapshot_distinguishes_running_and_completed(self):
        running = '''uid=1_10 Iframe "internal://deep-research"\n  uid=1_11 RootWebArea "sandbox" busy'''
        completed = '''uid=1_10 Iframe "internal://deep-research"\n  uid=1_11 RootWebArea "sandbox"\n    uid=1_12 StaticText "研究完成情况：2m · 3 次引用"\n    uid=1_13 button "导出"\n    uid=1_14 button "展开"\n    uid=1_15 button "OpenAI 官方首页域名核验报告 This is a sufficiently long completed research report payload used for parsing."'''
        self.assertEqual(ChromeChatAdapter._parse_deep_research_snapshot(running)['state'], 'running')
        result = ChromeChatAdapter._parse_deep_research_snapshot(completed)
        self.assertEqual(result['state'], 'completed')
        self.assertIn('OpenAI 官方首页域名核验报告', result['report'])

    def test_image_snapshot_distinguishes_running_and_completed(self):
        running = '''uid=1_1 StaticText "创建图片"\nuid=1_2 StaticText "正在生成更详细的图片，请稍等。"\nuid=1_3 button "停止回答"'''
        completed = '''uid=1_1 StaticText "创建图片"\nuid=1_2 button "已生成图片：白底中央黑色方块"\nuid=1_3 button "编辑图片"'''
        self.assertEqual(ChromeChatAdapter._parse_image_snapshot(running), 'running')
        self.assertEqual(ChromeChatAdapter._parse_image_snapshot(completed), 'completed')

    def test_download_generated_image_waits_for_hydrated_history(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
        adapter._wait_ready = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1', 'promptVisible': True})
        adapter._wait_history = Mock()
        adapter._latest_generated_image = Mock(side_effect=[None, {
            'src': 'https://chatgpt.com/backend-api/estuary/content?id=file-1', 'alt': 'image'
        }])
        adapter._evaluate = Mock(return_value={
            'ok': True, 'contentType': 'image/png', 'size': 3, 'base64': 'cG5n'
        })
        with patch('cfr.chat.time.sleep'):
            result = adapter.download_generated_image({'url': 'https://chatgpt.com/c/conv-1'})
        self.assertEqual(result['data'], b'png')
        adapter._wait_history.assert_called_once()
        self.assertEqual(adapter._latest_generated_image.call_count, 2)

    def test_download_generated_image_to_file_uses_cfr_local_download_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.chat.resolve_cfr_config_dir', return_value=Path(directory)):
            adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
            adapter.download_generated_image = Mock(return_value={
                'data': b'png',
                'content_type': 'image/png',
                'source': 'https://chatgpt.com/backend-api/estuary/content?id=file_test-image',
                'alt': 'test',
            })
            result = adapter.download_generated_image_to_file({'url': 'https://chatgpt.com/c/conv-1'})
            path = Path(result['path'])
            self.assertEqual(path.parent.parent, Path(directory) / 'chatgpt' / 'downloads')
            self.assertEqual(path.name, 'file_test-image.png')
            self.assertEqual(path.read_bytes(), b'png')

    def test_generated_image_download_rejects_oversized_payload_before_decode(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
        adapter._wait_ready = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1', 'promptVisible': True})
        adapter._wait_history = Mock()
        adapter._latest_generated_image = Mock(return_value={
            'src': 'https://chatgpt.com/backend-api/estuary/content?id=file-1', 'alt': 'image'
        })
        adapter._evaluate = Mock(return_value={'ok': False, 'stage': 'size', 'size': 999_999_999})
        with self.assertRaises(StructuredError) as caught:
            adapter.download_generated_image({'url': 'https://chatgpt.com/c/conv-1'})
        self.assertEqual(caught.exception.code, 'CHAT_IMAGE_TOO_LARGE')

    def test_download_generated_file_to_file_uses_authenticated_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.chat.resolve_cfr_config_dir', return_value=Path(directory)):
            adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
            adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
            adapter._wait_ready = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1', 'promptVisible': True})
            adapter._wait_history = Mock()
            adapter._latest_generated_file = Mock(return_value={'file_id': 'file_test-csv', 'name': 'fallback.csv'})
            adapter._evaluate = Mock(side_effect=_stream_download_evaluator({
                'data': b'col1,col2\n',
                'contentType': 'text/csv',
                'fileName': 'generated.csv',
                'source': 'https://chatgpt.com/backend-api/estuary/content?id=file_test-csv&sig=x',
            }))
            result = adapter.download_generated_file_to_file({'url': 'https://chatgpt.com/c/conv-1'})
            path = Path(result['path'])
            self.assertEqual(path.parent.parent, Path(directory) / 'chatgpt' / 'downloads')
            self.assertEqual(path.name, 'generated.csv')
            self.assertEqual(path.read_bytes(), b'col1,col2\n')
            adapter._wait_history.assert_called_once()

    def test_same_remote_generated_file_id_still_uses_unique_delivery_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.chat.resolve_cfr_config_dir', return_value=Path(directory)):
            adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
            adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
            adapter._wait_ready = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1', 'promptVisible': True})
            adapter._wait_history = Mock()
            adapter._evaluate = Mock(side_effect=_stream_download_evaluator(
                {'data': b'a,b\n', 'contentType': 'text/csv', 'fileName': 'report.csv', 'source': 'https://chatgpt.com/backend-api/estuary/content?id=file_same'},
                {'data': b'a,b\n', 'contentType': 'text/csv', 'fileName': 'report.csv', 'source': 'https://chatgpt.com/backend-api/estuary/content?id=file_same'},
            ))
            remote = {
                'file_id': 'file_same',
                'name': 'report.csv',
                'href': 'https://chatgpt.com/backend-api/estuary/content?id=file_same',
            }
            first = adapter.download_generated_file_to_file({'url': 'https://chatgpt.com/c/conv-1'}, remote)
            second = adapter.download_generated_file_to_file({'url': 'https://chatgpt.com/c/conv-1'}, remote)
            self.assertNotEqual(Path(first['path']).parent, Path(second['path']).parent)
            self.assertEqual(Path(first['path']).read_bytes(), b'a,b\n')
            self.assertEqual(Path(second['path']).read_bytes(), b'a,b\n')

    def test_generated_files_with_same_name_use_distinct_identity_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.chat.resolve_cfr_config_dir', return_value=Path(directory)):
            adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
            adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
            adapter._wait_ready = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1', 'promptVisible': True})
            adapter._wait_history = Mock()
            adapter._evaluate = Mock(side_effect=_stream_download_evaluator(
                {'data': b'a,b\n', 'contentType': 'text/csv', 'fileName': 'report.csv', 'source': 'https://chatgpt.com/backend-api/estuary/content?id=file_one'},
                {'data': b'c,d\n', 'contentType': 'text/csv', 'fileName': 'report.csv', 'source': 'https://chatgpt.com/backend-api/estuary/content?id=file_two'},
            ))
            one = adapter.download_generated_file_to_file(
                {'url': 'https://chatgpt.com/c/conv-1'},
                {'file_id': 'file_one', 'name': 'report.csv', 'href': 'https://chatgpt.com/backend-api/estuary/content?id=file_one'},
            )
            two = adapter.download_generated_file_to_file(
                {'url': 'https://chatgpt.com/c/conv-1'},
                {'file_id': 'file_two', 'name': 'report.csv', 'href': 'https://chatgpt.com/backend-api/estuary/content?id=file_two'},
            )
            self.assertNotEqual(one['path'], two['path'])
            self.assertEqual(Path(one['path']).read_bytes(), b'a,b\n')
            self.assertEqual(Path(two['path']).read_bytes(), b'c,d\n')

    def test_download_generated_file_prefers_current_dom_backend_href(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.chat.resolve_cfr_config_dir', return_value=Path(directory)):
            adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
            adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
            adapter._wait_ready = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1', 'promptVisible': True})
            adapter._wait_history = Mock()
            adapter._latest_generated_file = Mock(return_value={
                'file_id': 'file_test-csv',
                'name': 'generated.csv',
                'href': 'https://chatgpt.com/backend-api/estuary/content?id=file_test-csv&sig=current',
            })
            adapter._evaluate = Mock(side_effect=_stream_download_evaluator({
                'data': b'a,b\n', 'contentType': 'text/csv', 'fileName': 'generated.csv',
                'source': 'https://chatgpt.com/backend-api/estuary/content?id=file_test-csv&sig=current',
            }))
            result = adapter.download_generated_file_to_file({'url': 'https://chatgpt.com/c/conv-1'})
            script = adapter._evaluate.call_args_list[0].args[1]
            self.assertIn('sig=current', script)
            self.assertEqual(Path(result['path']).read_bytes(), b'a,b\n')

    def test_generated_file_download_rejects_oversized_payload_before_decode(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
        adapter._wait_ready = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1', 'promptVisible': True})
        adapter._wait_history = Mock()
        adapter._latest_generated_file = Mock(return_value={
            'file_id': 'file_test-csv', 'name': 'generated.csv',
            'href': 'https://chatgpt.com/backend-api/estuary/content?id=file_test-csv',
        })
        adapter._evaluate = Mock(return_value={'ok': False, 'stage': 'size', 'size': 999_999_999})
        with self.assertRaises(StructuredError) as caught:
            adapter.download_generated_file_to_file({'url': 'https://chatgpt.com/c/conv-1'})
        self.assertEqual(caught.exception.code, 'CHAT_FILE_TOO_LARGE')

    def test_download_generated_file_supports_sandbox_path_from_current_assistant_turn(self):
        with tempfile.TemporaryDirectory() as directory, patch('cfr.chat.resolve_cfr_config_dir', return_value=Path(directory)):
            adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
            adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
            adapter._wait_ready = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1', 'promptVisible': True})
            adapter._wait_history = Mock()
            adapter._latest_generated_file = Mock(return_value={
                'file_id': None,
                'name': 'cfr_chat_download_e2e.csv',
                'href': '',
                'sandbox_url': 'sandbox:/mnt/data/cfr_chat_download_e2e.csv',
            })
            adapter._evaluate = Mock(side_effect=_stream_download_evaluator({
                'data': b'a,b\n',
                'contentType': 'text/csv',
                'fileName': 'cfr_chat_download_e2e.csv',
                'source': 'https://chatgpt.com/backend-api/sandbox/download?path=%2Fmnt%2Fdata%2Fcfr_chat_download_e2e.csv',
            }))
            result = adapter.download_generated_file_to_file({'url': 'https://chatgpt.com/c/conv-1'})
            script = adapter._evaluate.call_args_list[0].args[1]
            self.assertIn('/backend-api/sandbox/download', script)
            self.assertIn('/mnt/data/cfr_chat_download_e2e.csv', script)
            self.assertEqual(Path(result['path']).read_bytes(), b'a,b\n')

    def test_latest_generated_file_scans_current_assistant_role_and_sandbox_controls(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._evaluate = Mock(return_value=[{
            'file_id': None,
            'name': 'Download cfr_chat_download_e2e.csv',
            'href': '',
            'sandbox_url': 'sandbox:/mnt/data/cfr_chat_download_e2e.csv',
        }])
        result = adapter._latest_generated_file(3)
        script = adapter._evaluate.call_args.args[1]
        self.assertIn('[data-message-author-role="assistant"]', script)
        self.assertIn('sandbox:/mnt/data/', script)
        self.assertIn("button, [role=\"button\"]", script)
        self.assertEqual(result['sandbox_url'], 'sandbox:/mnt/data/cfr_chat_download_e2e.csv')

    def test_generated_files_returns_all_unique_files_from_current_assistant_turn(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._evaluate = Mock(return_value=[
            {'file_id': 'file_csv', 'name': 'report.csv', 'href': '', 'sandbox_url': ''},
            {'file_id': 'file_xlsx', 'name': 'report.xlsx', 'href': '', 'sandbox_url': ''},
        ])
        files = adapter._generated_files(3)
        self.assertEqual([item['name'] for item in files], ['report.csv', 'report.xlsx'])
        script = adapter._evaluate.call_args.args[1]
        self.assertIn('const results = []', script)
        self.assertIn('if (results.length) return results', script)
        self.assertIn('generatedImage && !explicitFileName', script)
        self.assertIn('const key = item.file_id || item.sandbox_url || item.href || item.name', script)
        node = shutil.which('node')
        if node:
            checked = subprocess.run(
                [node, '-e', 'new Function("return (" + process.argv[1] + ")");', script],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_generated_file_identity_ignores_hydrating_display_label(self):
        first = {'file_id': 'file_abc', 'name': 'Download'}
        hydrated = {'file_id': 'file_abc', 'name': 'report.xlsx'}
        self.assertEqual(
            ChromeChatAdapter._generated_file_key(first),
            ChromeChatAdapter._generated_file_key(hydrated),
        )

    def test_send_message_returns_all_files_from_completed_assistant_turn(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
        adapter._restore_bound_url = Mock()
        adapter._wait_ready = Mock(return_value={
            'url': 'https://chatgpt.com/c/conv-1',
            'assistantCount': 1,
            'userCount': 1,
            'generatedFileKey': 'file_old',
        })
        adapter._wait_history = Mock(side_effect=lambda _page_id, state: state)
        adapter._prompt_uid = Mock(return_value='prompt-1')
        adapter._wait_send_enabled = Mock()
        adapter._wait_send_started = Mock(return_value=True)
        adapter._wait_answer = Mock(return_value={
            'text': 'done',
            'url': 'https://chatgpt.com/c/conv-1',
            'reasoning_text': '',
            'generated_file_key': 'file_csv|file_xlsx',
        })
        files = [
            {'file_id': 'file_csv', 'name': 'report.csv'},
            {'file_id': 'file_xlsx', 'name': 'report.xlsx'},
        ]
        adapter._generated_files = Mock(return_value=files)
        adapter._latest_generated_image = Mock(return_value=None)
        adapter.mcp = Mock()
        result = adapter.send_message({'tab_id': '3', 'url': 'https://chatgpt.com/c/conv-1'}, 'build both files')
        self.assertEqual(result['generated_files'], files)
        self.assertEqual(result['generated_file'], files[-1])

    def test_upload_file_uses_native_browser_file_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'upload.txt'
            source.write_text('payload', encoding='utf-8')
            adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
            adapter.mcp = Mock(timeout=8)
            adapter.mcp.text.side_effect = [
                'uid=plus-1 button "Add files and more"',
                '  uid=upload-1 generic\n    uid=label-1 StaticText "Upload photos and files"',
            ]
            adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/g/g-p-cfr/project'})
            adapter._restore_bound_url = Mock()
            adapter._wait_ready = Mock(return_value={'url': 'https://chatgpt.com/g/g-p-cfr/project'})
            adapter._state = Mock(return_value={'url': 'https://chatgpt.com/g/g-p-cfr/project'})
            adapter._wait_uploaded_files = Mock()
            adapter._focus_and_enter = Mock(return_value=True)
            result = adapter.upload_file({'url': 'https://chatgpt.com/g/g-p-cfr/project'}, source)
            upload = [call for call in adapter.mcp.tool.call_args_list if call.args[0] == 'upload_file']
            adapter.mcp.set_roots.assert_called_once_with([source.resolve().parent])
            adapter._focus_and_enter.assert_called_once()
            self.assertFalse(any(call.args[0] == 'click' for call in adapter.mcp.tool.call_args_list))
            self.assertEqual(len(upload), 1)
            self.assertEqual(upload[0].args[1]['uid'], 'upload-1')
            self.assertEqual(upload[0].args[1]['filePaths'], [str(source.resolve())])
            self.assertEqual(result['name'], 'upload.txt')

    def test_upload_files_uses_one_native_multi_file_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / 'report.pdf'
            second = Path(directory) / 'diagram.png'
            first.write_bytes(b'pdf')
            second.write_bytes(b'png')
            adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
            adapter.mcp = Mock(timeout=8)
            adapter.mcp.text.side_effect = [
                'uid=plus-2 button "添加文件等"',
                '  uid=upload-2 generic\n    uid=label-2 StaticText "从电脑上传"',
            ]
            adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
            adapter._restore_bound_url = Mock()
            adapter._wait_ready = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1'})
            adapter._wait_history = Mock()
            adapter._state = Mock(return_value={'url': 'https://chatgpt.com/c/conv-1'})
            adapter._wait_uploaded_files = Mock()
            adapter._focus_and_enter = Mock(return_value=True)
            result = adapter.upload_files({'url': 'https://chatgpt.com/c/conv-1'}, [first, second])
            upload = [call for call in adapter.mcp.tool.call_args_list if call.args[0] == 'upload_file']
            adapter.mcp.set_roots.assert_called_once_with([first.resolve().parent, second.resolve().parent])
            adapter._focus_and_enter.assert_called_once()
            self.assertFalse(any(call.args[0] == 'click' for call in adapter.mcp.tool.call_args_list))
            self.assertEqual(len(upload), 1)
            self.assertEqual(upload[0].args[1]['uid'], 'upload-2')
            self.assertEqual(upload[0].args[1]['filePaths'], [str(first.resolve()), str(second.resolve())])
            self.assertEqual(result['names'], ['report.pdf', 'diagram.png'])

    def test_upload_files_fails_closed_without_native_upload_control(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.mcp = Mock(timeout=8)
        adapter.mcp.text.return_value = 'uid=prompt-1 textbox "Message ChatGPT"'
        with self.assertRaises(StructuredError) as caught:
            adapter._upload_files_on_page(3, [Path('missing.txt')])
        self.assertEqual(caught.exception.code, 'CHAT_UPLOAD_CONTROL_NOT_FOUND')
        self.assertFalse(any(call.args[0] == 'upload_file' for call in adapter.mcp.tool.call_args_list))

    def test_uploaded_file_wait_requires_filename_inside_current_composer(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._evaluate = Mock(side_effect=[
            {'missing': ['diagram.png'], 'busy': False, 'errorText': ''},
            {'missing': [], 'busy': False, 'errorText': ''},
            {'missing': [], 'busy': False, 'errorText': ''},
        ])
        with patch('cfr.chat.time.sleep'), patch('cfr.chat.time.monotonic', side_effect=[0, 0, 0.2, 0.2, 1.3, 1.3]):
            adapter._wait_uploaded_files(3, ['diagram.png'])
        self.assertEqual(adapter._evaluate.call_count, 3)

    def test_uploaded_file_wait_fails_closed_on_native_rejection(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._evaluate = Mock(return_value={
            'missing': [], 'busy': False, 'errorText': 'Unsupported file type',
        })
        with self.assertRaises(StructuredError) as caught:
            adapter._wait_uploaded_files(3, ['clip.mp4'])
        self.assertEqual(caught.exception.code, 'CHAT_UPLOAD_REJECTED')

    def test_uploaded_file_wait_emits_valid_escaped_newline_in_browser_script(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._evaluate = Mock(return_value={'missing': [], 'busy': False, 'errorText': ''})
        with patch('cfr.chat.time.sleep'), patch('cfr.chat.time.monotonic', side_effect=[0, 0, 0, 1.1, 1.1]):
            adapter._wait_uploaded_files(3, ['diagram.png'])
        script = adapter._evaluate.call_args.args[1]
        self.assertIn("parts.join('\\n')", script)
        self.assertNotIn("parts.join('\n')", script)

    def test_send_message_uploads_files_in_same_prepared_composer_transaction(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
        adapter._restore_bound_url = Mock()
        ready = {'url': 'https://chatgpt.com/c/conv-1', 'assistantCount': 1, 'userCount': 1}
        adapter._wait_ready = Mock(return_value=ready)
        adapter._wait_history = Mock(return_value=ready)
        adapter._clear_stale_composer_attachments = Mock(return_value=0)
        adapter._upload_files_on_page = Mock()
        adapter._prompt_uid = Mock(return_value='prompt-1')
        adapter._wait_send_enabled = Mock()
        adapter._wait_send_started = Mock(return_value=True)
        adapter._wait_answer = Mock(return_value={'text': 'done', 'url': 'https://chatgpt.com/c/conv-1', 'reasoning_text': ''})
        adapter.mcp = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.txt'
            path.write_text('payload', encoding='utf-8')
            result = adapter.send_message(
                {'tab_id': '3', 'url': 'https://chatgpt.com/c/conv-1'}, 'inspect', file_paths=[path]
            )
        adapter._upload_files_on_page.assert_called_once()
        adapter._clear_stale_composer_attachments.assert_called_once_with(3)
        self.assertEqual(adapter._upload_files_on_page.call_args.args[0], 3)
        self.assertEqual(adapter._upload_files_on_page.call_args.args[1][0].name, 'report.txt')
        self.assertEqual(result['text'], 'done')
        self.assertEqual(adapter._wait_answer.call_args.kwargs['settle_seconds'], 1.5)

    def test_stale_composer_attachments_are_removed_before_new_batch(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._evaluate = Mock(side_effect=[
            {'remaining': 2, 'clicked': 2},
            {'remaining': 0, 'clicked': 0},
        ])
        with patch('cfr.chat.time.sleep'):
            removed = adapter._clear_stale_composer_attachments(3)
        self.assertEqual(removed, 2)
        self.assertEqual(adapter._evaluate.call_count, 2)
        script = adapter._evaluate.call_args_list[0].args[1]
        self.assertIn('remove|delete', script)
        self.assertIn('移除|删除|取消', script)

    def test_artifact_request_uses_longer_settle_without_extra_baseline_dom_scan(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
        adapter._restore_bound_url = Mock()
        ready = {
            'url': 'https://chatgpt.com/c/conv-1', 'assistantCount': 1, 'userCount': 1,
            'generatedFileKey': 'old-file',
        }
        adapter._wait_ready = Mock(return_value=ready)
        adapter._wait_history = Mock(return_value=ready)
        adapter._prompt_uid = Mock(return_value='prompt-1')
        adapter._wait_send_enabled = Mock()
        adapter._wait_send_started = Mock(return_value=True)
        adapter._wait_answer = Mock(return_value={
            'text': 'done', 'url': 'https://chatgpt.com/c/conv-1', 'reasoning_text': '',
            'generated_image_src': None, 'generated_file_key': None,
        })
        adapter._latest_generated_file = Mock(side_effect=AssertionError('unnecessary baseline scan'))
        adapter.mcp = Mock()
        result = adapter.send_message(
            {'tab_id': '3', 'url': 'https://chatgpt.com/c/conv-1'},
            '请生成一个 xlsx 文件',
        )
        self.assertEqual(result['text'], 'done')
        self.assertEqual(adapter._wait_answer.call_args.kwargs['settle_seconds'], 3.0)
        adapter._latest_generated_file.assert_not_called()

    def test_send_message_returns_generated_file_from_the_completed_answer(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._page = Mock(return_value={'id': 3, 'url': 'https://chatgpt.com/c/conv-1'})
        adapter._restore_bound_url = Mock()
        ready = {'url': 'https://chatgpt.com/c/conv-1', 'assistantCount': 1, 'userCount': 1}
        adapter._wait_ready = Mock(return_value=ready)
        adapter._wait_history = Mock(return_value=ready)
        adapter._generated_files = Mock(side_effect=[
            [{'file_id': 'file_old', 'name': 'old.csv'}],
            [{'file_id': 'file_new', 'name': 'new.xlsx', 'href': 'https://chatgpt.com/backend-api/estuary/content?id=file_new'}],
        ])
        adapter._prompt_uid = Mock(return_value='prompt-1')
        adapter._wait_send_enabled = Mock()
        adapter._wait_send_started = Mock(return_value=True)
        adapter._wait_answer = Mock(return_value={
            'text': 'done', 'url': 'https://chatgpt.com/c/conv-1', 'reasoning_text': '',
            'generated_file_key': 'file_new|new.xlsx',
        })
        adapter.mcp = Mock()
        result = adapter.send_message({'tab_id': '3', 'url': 'https://chatgpt.com/c/conv-1'}, 'make workbook')
        self.assertEqual(result['generated_file']['file_id'], 'file_new')
        self.assertEqual(result['generated_file']['name'], 'new.xlsx')

    def test_send_waits_until_native_send_button_is_enabled(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter._evaluate = Mock(side_effect=[False, True])
        adapter._state = Mock(return_value={'blocked': False})
        with patch('cfr.chat.time.sleep'):
            adapter._wait_send_enabled(3)
        self.assertEqual(adapter._evaluate.call_count, 2)

    def test_bound_url_wins_over_selected_home_page(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.mcp = SimpleNamespace(mode='dedicated')
        adapter._pages = Mock(return_value=[
            {'id': 2, 'url': 'https://chatgpt.com/', 'selected': True},
            {'id': 3, 'url': 'https://chatgpt.com/c/conv-1', 'selected': False},
        ])
        page = adapter._page({'tab_id': None, 'url': 'https://chatgpt.com/c/conv-1'}, create=False)
        self.assertEqual(page['id'], 3)

    def test_embedded_hidden_chat_page_is_used_without_prior_binding(self):
        adapter = ChromeChatAdapter.__new__(ChromeChatAdapter)
        adapter.mcp = SimpleNamespace(mode='embedded')
        adapter._pages = Mock(return_value=[
            {'id': 1, 'url': 'http://127.0.0.1:62655/', 'selected': True},
            {'id': 2, 'url': 'https://chatgpt.com/', 'selected': False},
        ])
        page = adapter._page({}, create=False)
        self.assertEqual(page['id'], 2)

    def test_authenticated_orphan_profile_browser_is_reclaimed_before_mcp_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            mcp = Mock()
            mcp.mode = 'dedicated'
            mcp.running = False
            mcp.user_data_dir = Path(directory) / 'profile'
            adapter = ChromeChatAdapter(mcp=mcp)
            adapter._browser_state_dir.mkdir(parents=True, exist_ok=True)
            adapter._auth_marker.touch()
            with patch.object(adapter, '_profile_browser_running', return_value=True), patch.object(adapter, '_stop_profile_browser') as stop, patch.object(adapter, '_ensure_chatgpt_tab'):
                self.assertIsNone(adapter._prepare_dedicated_runtime())
            stop.assert_called_once_with()
            mcp.start.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
