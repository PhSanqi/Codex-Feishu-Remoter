import os
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[2]


class CliBootstrapTests(unittest.TestCase):
    def _run_source_cli(self, *arguments):
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        return subprocess.run(
            [sys.executable, str(ROOT / 'scripts' / 'run_cfr.py'), *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=15,
        )

    def test_source_checkout_help_bootstraps_without_pythonpath(self):
        completed = self._run_source_cli('--help')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('doctor', completed.stdout)

    def test_source_checkout_doctor_help_does_not_call_codex(self):
        completed = self._run_source_cli('doctor', '--help')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('--live', completed.stdout)

    def test_package_metadata_declares_src_discovery_and_console_script(self):
        metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
        self.assertEqual(metadata['project']['scripts']['cfr'], 'cfr.cli.main:cli')
        self.assertEqual(metadata['tool']['setuptools']['packages']['find']['where'], ['src'])

    def test_cli_entry_point_is_callable(self):
        sys.path.insert(0, str(ROOT / 'src'))
        from cfr.cli.main import cli
        self.assertTrue(callable(cli))


if __name__ == '__main__':
    unittest.main()
