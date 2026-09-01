import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]


def _snapshot_module():
    spec = importlib.util.spec_from_file_location('cfr_source_snapshot_test', ROOT / 'scripts' / 'create_source_snapshot.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceSnapshotTests(unittest.TestCase):
    def test_snapshot_is_allowlisted_and_portable(self):
        snapshot = _snapshot_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('README.md', 'pyproject.toml', 'START_CFR.cmd'):
                (root / name).write_text('source', encoding='utf-8')
            for name in ('src/ok.py', 'scripts/tool.py', 'tests/test_ok.py', 'docs/guide.md', 'm3_control/src/app.tsx', 'm3_control/index.html', 'm3_control/package.json', 'm3_control/package-lock.json', 'm3_control/tsconfig.json', 'm3_control/vite.config.ts'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('source', encoding='utf-8')
            for name in ('src/node_modules/skip.js', 'src/dist/skip.js', 'src/.tmp/skip.py', 'src/cfr.egg-info/PKG-INFO', 'src/.env', 'tests/nested.zip'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('excluded', encoding='utf-8')
            result = snapshot.write_snapshot(root, 'M3C_1')
            archive_path = root / result['SourceSnapshotFile']
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
        self.assertIn('src/ok.py', names)
        self.assertIn('m3_control/src/app.tsx', names)
        self.assertTrue(all('\\' not in name for name in names))
        self.assertFalse(any(any(part in name for part in ('node_modules', 'dist', '.tmp', '.egg-info', '.env', '.zip')) for name in names))
