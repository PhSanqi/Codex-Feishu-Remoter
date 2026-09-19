from pathlib import Path
import unittest
from unittest.mock import call, patch

from cfr.chrome_use import ChromeUseBridge, chrome_use_setup_snapshot
from cfr.core.models import StructuredError


class ChromeUseBridgeTests(unittest.TestCase):
    def test_snapshot_translation_preserves_refs_for_existing_chat_adapter(self):
        raw = '\n'.join([
            '- textbox "Ask ChatGPT" [ref=e52]',
            '- button "Send" [disabled=true, ref=e53]',
            '- radio "Chat" [checked=true, ref=e47]',
        ])
        translated = ChromeUseBridge._snapshot_to_mcp(raw)
        self.assertIn('uid=e52 textbox "Ask ChatGPT"', translated)
        self.assertIn('uid=e53 button "Send" disabled', translated)
        self.assertIn('uid=e47 radio "Chat" checked', translated)

    def test_list_pages_exposes_only_session_owned_tabs(self):
        bridge = ChromeUseBridge(binary='/tmp/chrome-use')
        tabs = [
            {'tabId': 't1', 'title': 'ChatGPT', 'url': 'https://chatgpt.com/', 'active': True, 'ownership': 'created'},
            {'tabId': 't2', 'title': 'User', 'url': 'https://example.com/', 'active': False, 'ownership': 'foreign'},
        ]
        with patch.object(bridge, 'start', return_value=bridge), patch.object(bridge, '_tabs', return_value=tabs):
            bridge._running = True
            text = bridge.text(bridge.tool('list_pages'))
        self.assertIn('1: ChatGPT (https://chatgpt.com/) [selected]', text)
        self.assertNotIn('User', text)

    def test_close_does_not_stop_persistent_session(self):
        bridge = ChromeUseBridge(binary='/tmp/chrome-use')
        bridge._running = True
        with patch.object(bridge, '_run') as run:
            bridge.close()
        self.assertFalse(bridge.running)
        run.assert_not_called()

    def test_shared_profile_is_system_chrome_profile(self):
        with patch('cfr.chrome_use._system_chrome_profile_dir', return_value=Path('/home/test/.config/google-chrome/Default')):
            bridge = ChromeUseBridge(binary='/tmp/chrome-use')
        self.assertEqual(bridge.user_data_dir, Path('/home/test/.config/google-chrome/Default'))

    def test_tabs_never_expose_adopted_or_foreign_user_tabs(self):
        bridge = ChromeUseBridge(binary='/tmp/chrome-use')
        payload = {
            'success': True,
            'data': {
                'tabs': [
                    {'tabId': 't1', 'ownership': 'created'},
                    {'tabId': 't2', 'ownership': 'adopted'},
                    {'tabId': 't3', 'ownership': 'foreign'},
                ],
            },
        }
        with patch.object(bridge, '_run', return_value=payload):
            self.assertEqual(bridge._tabs(), [{'tabId': 't1', 'ownership': 'created'}])

    def test_isolated_created_window_is_minimized(self):
        bridge = ChromeUseBridge(binary='/tmp/chrome-use')
        metadata = [{'windowId': 55, 'chromeTabId': 101}]
        window = {'id': 55, 'state': 'maximized', 'focused': False, 'tabs': [{'id': 101}]}
        with patch.object(bridge, '_created_tab_metadata', return_value=metadata), patch.object(
            bridge, '_extension_call', side_effect=[window, {'id': 55, 'state': 'minimized'}]
        ) as extension_call:
            bridge._minimize_isolated_session_windows()
        self.assertEqual(
            extension_call.call_args_list,
            [
                call('windows.get', [55, {'populate': True}]),
                call('windows.update', [55, {'state': 'minimized', 'focused': False}]),
            ],
        )

    def test_window_with_user_tab_fails_closed_without_minimizing(self):
        bridge = ChromeUseBridge(binary='/tmp/chrome-use')
        metadata = [{'windowId': 55, 'chromeTabId': 101}]
        mixed_window = {
            'id': 55,
            'state': 'maximized',
            'focused': True,
            'tabs': [{'id': 101}, {'id': 202}],
        }
        with patch.object(bridge, '_created_tab_metadata', return_value=metadata), patch.object(
            bridge, '_extension_call', return_value=mixed_window
        ) as extension_call:
            with self.assertRaises(StructuredError) as raised:
                bridge._minimize_isolated_session_windows()
        self.assertEqual(raised.exception.code, 'CHAT_CHROME_USE_WINDOW_NOT_ISOLATED')
        extension_call.assert_called_once_with('windows.get', [55, {'populate': True}])

    def test_setup_snapshot_reports_silent_window_state_for_running_session(self):
        status = {
            'success': True,
            'data': {
                'cliVersion': '1.5.123',
                'extension': {
                    'hostInstalled': True,
                    'hostHealthy': True,
                    'relayUp': True,
                    'liveVersion': '0.5.26',
                },
                'sessions': [{'name': 'cfr-chat', 'pid': 123}],
            },
        }
        silent = {'session': 'cfr-chat', 'tab_count': 1, 'window_count': 1, 'isolated': True, 'minimized': True, 'windows': []}
        with patch('cfr.chrome_use.resolve_chrome_use_binary', return_value=Path('/tmp/chrome-use')), patch.object(
            ChromeUseBridge, '_run', return_value=status
        ), patch.object(ChromeUseBridge, 'silent_window_snapshot', return_value=silent):
            snapshot = chrome_use_setup_snapshot('/tmp/project')
        self.assertTrue(snapshot['session_running'])
        self.assertEqual(snapshot['silent_window'], silent)


if __name__ == '__main__':
    unittest.main()
