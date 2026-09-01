import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from cfr.config import child_process_env, resolve_cfr_codex_home
from cfr.cli.main import _parser, main


class ConfigTests(unittest.TestCase):
    def test_explicit_home_wins(self):
        resolved = resolve_cfr_codex_home('C:/explicit', {'CFR_CODEX_HOME': 'C:/env', 'CODEX_HOME': 'C:/ambient'}, 'C:/user')
        self.assertEqual(resolved.path, Path('C:/explicit'))
        self.assertEqual(resolved.source, 'explicit')

    def test_cfr_environment_fallback_ignores_ambient_codex_home(self):
        resolved = resolve_cfr_codex_home(None, {'CFR_CODEX_HOME': 'C:/cfr', 'CODEX_HOME': 'C:/ambient'}, 'C:/user')
        self.assertEqual(resolved.path, Path('C:/cfr'))
        self.assertEqual(resolved.source, 'CFR_CODEX_HOME')

    def test_default_user_home_is_cross_platform(self):
        resolved = resolve_cfr_codex_home(None, {'CODEX_HOME': 'C:/ambient'}, 'C:/user')
        self.assertEqual(resolved.path, Path('C:/user/.codex'))
        self.assertEqual(resolved.source, 'default_user_home')

    def test_child_home_overrides_ordinary_process_env(self):
        resolved = resolve_cfr_codex_home('C:/resolved')
        child = child_process_env({'CODEX_HOME': 'C:/caller', 'HTTP_PROXY': 'http://proxy'}, resolved)
        self.assertEqual(child['CODEX_HOME'], str(Path('C:/resolved')))
        self.assertEqual(child['HTTP_PROXY'], 'http://proxy')

    def test_cli_codex_home_reaches_adapter(self):
        parsed = _parser().parse_args(['--db', 'test.sqlite3', '--codex-home', 'C:/cfr-home', 'codex', 'list'])
        self.assertEqual(parsed.codex_home, 'C:/cfr-home')
        fake_store = type('Store', (), {'list_bindings': lambda self: [], 'close': lambda self: None})()
        with patch('cfr.cli.main.BindingStore', return_value=fake_store), patch('cfr.cli.main.CodexAdapter') as adapter:
            self.assertEqual(main(['--db', 'test.sqlite3', '--codex-home', 'C:/cfr-home', 'codex', 'list']), 0)
        self.assertEqual(adapter.call_args.kwargs['codex_home'], 'C:/cfr-home')

    def test_cli_exposes_doctor_and_codex_bin_boundary_options(self):
        parsed = _parser().parse_args(['--codex-bin', 'C:/codex.cmd', 'doctor', '--live', '--gate-origin', 'host_manual'])
        self.assertEqual(parsed.codex_bin, 'C:/codex.cmd')
        self.assertTrue(parsed.live)
        self.assertEqual(parsed.gate_origin, 'host_manual')
