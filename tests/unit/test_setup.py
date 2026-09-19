from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from cfr.control.setup import SETUP_SCHEMA_VERSION, SetupManager
from cfr.feishu.credentials import LocalConfigStore
from cfr.network import ProxyResolution


class _Launcher:
    def resolve(self):
        return SimpleNamespace(path=Path('C:/tools/codex.cmd'))

    def build_command(self, *args):
        return ['codex.cmd', *args]


class _SecretStore:
    available = True

    def has_secret(self, _key):
        return False


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = LocalConfigStore(Path(self.directory.name))
        self.manager = SetupManager(config_store=self.config, project_root=self.directory.name)
        self.patches = [
            patch('cfr.control.setup.CodexLauncher', _Launcher),
            patch('cfr.control.setup._codex_version', return_value='codex-cli 1.2.3'),
            patch('cfr.control.setup._codex_runtime_revision', return_value='runtime-revision-1'),
            patch('cfr.control.setup.login_status', return_value={'LoginStatus': 'LOGGED_IN', 'AuthMode': 'CHATGPT'}),
            patch('cfr.control.setup.KeyringSecretStore', _SecretStore),
            patch('cfr.control.setup.channel_sdk_metadata', return_value={'installed': True, 'version': '1.4.0'}),
            patch('cfr.control.setup.chat_setup_snapshot', return_value={'mode': 'dedicated', 'chrome_available': True, 'npx_available': True, 'authenticated': False, 'login_pending': False, 'profile_dir': 'profile', 'ready': False}),
            patch('cfr.control.setup.codex_desktop_runtime_snapshot', return_value={'running': True, 'mode': 'codexhost', 'codexhost_available': True, 'codexhost_command': 'C:/tools/codexhost.cmd', 'codexhost_version': '0.4.4'}),
            patch('cfr.control.setup.resolve_cfr_proxy', return_value=ProxyResolution('direct', None, None, None, 'direct')),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def test_code_setup_does_not_require_chat_login(self):
        state = self.manager.snapshot()
        self.assertEqual(state['selected_surface'], 'code')
        self.assertNotIn('chat_surface', state['blocking'])
        self.assertIn('codex_validation', state['blocking'])
        self.assertIn('feishu_credentials', state['blocking'])

    def test_chat_setup_requires_chat_login(self):
        self.config.set_default_surface('chat')
        state = self.manager.snapshot()
        self.assertIn('chat_surface', state['blocking'])

    def test_codex_validation_is_bound_to_exact_version(self):
        self.config.set_codex_validation(
            'codex-cli 1.2.3',
            schema=SETUP_SCHEMA_VERSION,
            schema_fingerprint='schema-fingerprint-1',
            runtime_revision='runtime-revision-1',
        )
        self.assertNotIn('codex_validation', self.manager.snapshot()['blocking'])
        self.config.set_codex_validation(
            'codex-cli 1.2.2',
            schema=SETUP_SCHEMA_VERSION,
            schema_fingerprint='schema-fingerprint-1',
            runtime_revision='runtime-revision-1',
        )
        self.assertIn('codex_validation', self.manager.snapshot()['blocking'])

    def test_preferences_persist_surface_and_network(self):
        with patch.object(self.manager, '_snapshot_uncached', side_effect=AssertionError('save must not run cold readiness probes')):
            result = self.manager.save_preferences(
                default_surface='chat',
                network_mode='proxy',
                proxy_url='http://127.0.0.1:7890',
                chat_browser_backend='embedded',
                codex_desktop_launcher='codexhost',
            )
        self.assertEqual(self.config.get_default_surface(), 'chat')
        self.assertEqual(self.config.get_network_policy(), {'mode': 'proxy', 'proxy_url': 'http://127.0.0.1:7890'})
        self.assertEqual(result['selected_surface'], 'chat')
        self.assertEqual(result['chat_browser_backend'], 'embedded')
        self.assertEqual(result['codex_desktop_launcher'], 'codexhost')

    def test_embedded_host_availability_is_projected_without_deleting_legacy_profile(self):
        self.manager.set_embedded_chat_available(True)
        with patch('cfr.control.setup.chat_setup_snapshot', return_value={
            'mode': 'dedicated', 'preference': 'auto', 'effective_backend': 'dedicated',
            'chrome_available': True, 'npx_available': True, 'authenticated': True,
            'login_pending': False, 'profile_dir': 'legacy-profile', 'ready': True,
            'legacy_profile_preserved': True,
            'dedicated': {'available': True, 'authenticated': True, 'login_pending': False, 'profile_dir': 'legacy-profile', 'ready': True},
            'embedded': {'available': True, 'authenticated': False, 'login_pending': False, 'storage_path': 'embedded-storage', 'ready': False},
        }) as snapshot:
            state = self.manager.snapshot()
        snapshot.assert_called_once_with(browser_backend='auto', embedded_available=True)
        self.assertEqual(state['chat']['effective_backend'], 'dedicated')
        self.assertTrue(state['chat']['legacy_profile_preserved'])

    def test_codexhost_runtime_and_launcher_preference_are_visible(self):
        state = self.manager.snapshot()
        self.assertEqual(state['codex']['desktop_runtime_mode'], 'codexhost')
        self.assertTrue(state['codex']['codexhost_available'])
        self.assertEqual(state['codex']['codexhost_version'], '0.4.4')

    def test_snapshot_is_single_flight_cached_but_config_revision_invalidates_it(self):
        with patch('cfr.control.setup._codex_version', return_value='codex-cli 1.2.3') as version:
            self.assertIs(self.manager.snapshot(), self.manager.snapshot())
            self.assertEqual(version.call_count, 1)
            self.config.set_default_surface('chat')
            self.assertEqual(self.manager.snapshot()['selected_surface'], 'chat')
            self.assertEqual(version.call_count, 2)


if __name__ == '__main__':
    unittest.main()
