import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from cfr.codex.launcher import CodexLauncher
from cfr.platform import detect_platform_capabilities, open_codex_desktop_thread, os_family


class PlatformTests(unittest.TestCase):
    def test_platform_capabilities_are_report_only(self):
        with patch('cfr.platform.platform_module.system', return_value='Windows'):
            capabilities = detect_platform_capabilities()
        self.assertEqual(capabilities.os_family, 'windows')
        self.assertTrue(capabilities.codex_launcher_supported)
        self.assertIsNone(capabilities.desktop_continuity_live_validated)

    def test_platform_family_handles_known_and_unknown_systems(self):
        self.assertEqual(os_family('Darwin'), 'darwin')
        self.assertEqual(os_family('Linux'), 'linux')
        self.assertEqual(os_family('Plan9'), 'unknown')

    def test_windows_opens_exact_existing_thread_uri(self):
        opened = []
        uri = open_codex_desktop_thread('019abc-example-thread', system='Windows', opener=opened.append)
        self.assertEqual(uri, 'codex://threads/019abc-example-thread')
        self.assertEqual(opened, [uri])
        self.assertNotIn('/new?', uri)

    def test_desktop_open_rejects_unsupported_platform(self):
        with self.assertRaises(NotImplementedError):
            open_codex_desktop_thread('thread-1', system='Linux', opener=lambda _uri: None)

    def test_desktop_open_reports_launcher_failure(self):
        def fail(_uri):
            raise OSError('protocol unavailable')

        with self.assertRaisesRegex(RuntimeError, 'Codex Desktop'):
            open_codex_desktop_thread('thread-1', system='Windows', opener=fail)


class LauncherTests(unittest.TestCase):
    def test_explicit_executable_wins(self):
        resolved = CodexLauncher(executable='C:/custom/codex.exe', environment={'PATH': ''}).resolve()
        self.assertEqual(resolved.source, 'explicit')
        self.assertEqual(resolved.kind, 'native')

    def test_cfr_codex_bin_wins_over_path(self):
        resolved = CodexLauncher(environment={'CFR_CODEX_BIN': 'C:/configured/codex.cmd', 'PATH': ''}).resolve()
        self.assertEqual(resolved.source, 'CFR_CODEX_BIN')
        self.assertEqual(resolved.kind, 'cmd')

    @patch('cfr.codex.launcher.shutil.which', return_value='C:/path/codex.exe')
    def test_path_fallback_is_reported(self, which):
        resolved = CodexLauncher(environment={'PATH': 'C:/path'}).resolve()
        self.assertEqual(resolved.source, 'path')
        self.assertEqual(resolved.kind, 'native')
        which.assert_called()

    def test_cmd_build_uses_comspec_without_shell_true(self):
        launcher = CodexLauncher(executable='C:/custom/codex.cmd', environment={'COMSPEC': 'C:/Windows/cmd.exe'})
        command = launcher.build_app_server_command()
        self.assertEqual(command[:4], ['C:/Windows/cmd.exe', '/d', '/s', '/c'])
        self.assertEqual(command[-2:], ['app-server', '--stdio'])


if __name__ == '__main__':
    unittest.main()
