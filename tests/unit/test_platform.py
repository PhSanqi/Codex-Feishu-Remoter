import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from cfr.codex.launcher import CodexLauncher
from cfr.platform import (
    codex_desktop_runtime_snapshot,
    detect_platform_capabilities,
    open_codex_desktop_thread,
    os_family,
    resolve_codexhost_command,
    restart_codex_desktop_thread,
)


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

    def test_windows_default_handoff_uses_shell_protocol_activation(self):
        observed = {}

        def runner(command, **kwargs):
            observed['command'] = command
            observed['kwargs'] = kwargs
            return SimpleNamespace(returncode=0, stderr='')

        uri = open_codex_desktop_thread('019abc-example-thread', system='Windows', runner=runner)
        self.assertEqual(uri, 'codex://threads/019abc-example-thread')
        self.assertEqual(observed['command'][0], 'powershell.exe')
        self.assertEqual(observed['kwargs']['env']['CFR_CODEX_URI'], uri)
        self.assertEqual(observed['kwargs']['env']['CFR_CODEX_LAUNCHER'], 'auto')
        self.assertIn("$managed", observed['command'][-1])
        self.assertFalse(observed['kwargs']['check'])

    def test_windows_restart_handoff_is_scoped_to_codex_appx_gui(self):
        observed = {}

        def runner(command, **kwargs):
            observed['command'] = command
            observed['kwargs'] = kwargs
            return SimpleNamespace(returncode=0, stderr='')

        uri = restart_codex_desktop_thread('019abc-example-thread', system='Windows', runner=runner)
        self.assertEqual(uri, 'codex://threads/019abc-example-thread')
        script = observed['command'][-1]
        self.assertIn('Get-AppxPackage OpenAI.Codex', script)
        self.assertIn("$p.Name -eq 'ChatGPT.exe'", script)
        self.assertIn('StartsWith($root', script)
        self.assertIn('HashSet[int]', script)
        self.assertIn('ParentProcessId', script)
        self.assertIn("$parent.Name -eq 'ChatGPT.exe'", script)
        self.assertIn("Name -eq 'codexhost-shim.exe'", script)
        self.assertIn('CFR_CODEXHOST_CMD', script)
        self.assertIn('Previous Codex Desktop process tree did not exit', script)
        self.assertIn('$newIds.Contains([int]$_.ProcessId)', script)
        self.assertIn('$treeChanged', script)
        self.assertIn('Start-Process -FilePath $env:CFR_CODEX_URI', script)
        self.assertEqual(observed['kwargs']['env']['CFR_CODEX_URI'], uri)

    def test_restart_auto_preserves_codexhost_and_never_resolves_ps1(self):
        observed = {}

        def runner(command, **kwargs):
            observed['command'] = command
            observed['kwargs'] = kwargs
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        restart_codex_desktop_thread(
            'thread-1',
            system='Windows',
            runner=runner,
            codexhost_command='C:/tools/codexhost.cmd',
        )
        self.assertEqual(observed['kwargs']['env']['CFR_CODEXHOST_CMD'], 'C:/tools/codexhost.cmd')
        self.assertIn("elseif($running){$managed}", observed['command'][-1])
        self.assertIn('$oldShim -notcontains', observed['command'][-1])
        self.assertIn('$stopDeadline', observed['command'][-1])
        self.assertIn('System.Diagnostics.ProcessStartInfo', observed['command'][-1])
        self.assertIn('CreateNoWindow=$true', observed['command'][-1])

    @patch('cfr.platform.shutil.which')
    def test_codexhost_resolution_avoids_powershell_policy_path(self, which):
        which.side_effect = lambda name, path=None: 'C:/node/codexhost.cmd' if name == 'codexhost.cmd' else None
        self.assertEqual(resolve_codexhost_command({'PATH': 'C:/node'}), 'C:\\node\\codexhost.cmd')
        self.assertNotIn('codexhost.ps1', [call.args[0] for call in which.call_args_list])

    def test_runtime_snapshot_reports_managed_desktop(self):
        payload = '{"running":true,"mode":"codexhost","desktop_pids":[10],"shim_pids":[11]}'
        runner = lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr='')
        with patch('cfr.platform.resolve_codexhost_command', return_value='C:/tools/codexhost.cmd'), patch(
            'cfr.platform._codexhost_version', return_value='0.4.4'
        ):
            state = codex_desktop_runtime_snapshot(system='Windows', runner=runner, cache_ttl=0)
        self.assertTrue(state['running'])
        self.assertEqual(state['mode'], 'codexhost')
        self.assertEqual(state['shim_pids'], [11])
        self.assertEqual(state['codexhost_version'], '0.4.4')


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
        launcher = CodexLauncher(environment={'PATH': 'C:/path'})
        resolved = launcher.resolve()
        self.assertIs(launcher.resolve(), resolved)
        self.assertEqual(resolved.source, 'path')
        self.assertEqual(resolved.kind, 'native')
        self.assertEqual(which.call_count, 1)

    @patch('cfr.codex.launcher.shutil.which', return_value=None)
    def test_project_local_codex_is_final_fallback(self, _which):
        local = Path('/tmp/cfr-project-local-codex')
        launcher = CodexLauncher(environment={'PATH': ''})
        with patch.object(CodexLauncher, '_project_local_candidates', return_value=(local,)), patch.object(
            Path, 'is_file', return_value=True
        ):
            resolved = launcher.resolve()
        self.assertEqual(resolved.path, local)
        self.assertEqual(resolved.source, 'project-local')
        self.assertEqual(resolved.kind, 'native')

    def test_cmd_build_uses_comspec_without_shell_true(self):
        launcher = CodexLauncher(executable='C:/custom/codex.cmd', environment={'COMSPEC': 'C:/Windows/cmd.exe'})
        command = launcher.build_app_server_command()
        self.assertEqual(command[:4], ['C:/Windows/cmd.exe', '/d', '/s', '/c'])
        self.assertEqual(command[-2:], ['app-server', '--stdio'])


if __name__ == '__main__':
    unittest.main()
