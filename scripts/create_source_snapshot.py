"""Create a portable, source-only CFR handoff ZIP."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ('README.md', 'pyproject.toml', 'START_CFR.cmd')
SOURCE_DIRS = ('src', 'scripts', 'tests', 'docs')
M3_FILES = ('index.html', 'package.json', 'package-lock.json', 'tsconfig.json', 'vite.config.ts')
FORBIDDEN_DIRS = {
    '.git', '.tmp', 'tmp', 'temp', 'node_modules', 'dist', '__pycache__',
    '.pytest_cache', '.mypy_cache', '.ruff_cache', '.venv', 'venv', 'env',
    'coverage', 'htmlcov', 'generated_schema', 'generated-schemas',
    'runtime_evidence', 'runtime-evidence', 'probe_artifacts', 'probe-artifacts',
}
FORBIDDEN_SUFFIXES = ('.pyc', '.pyo', '.log', '.sqlite', '.sqlite3', '.db', '.db-wal', '.db-shm', '.tsbuildinfo', '.zip', '.rar')


def _forbidden(relative: Path) -> bool:
    parts = relative.parts
    return (
        any(part.lower() in FORBIDDEN_DIRS or part.lower().endswith('.egg-info') for part in parts)
        or relative.name.startswith('.env')
        or relative.name == '.coverage'
        or relative.name.lower().endswith(FORBIDDEN_SUFFIXES)
    )


def _files_under(root: Path, relative: Path):
    directory = root / relative
    if directory.is_dir():
        for path in sorted(directory.rglob('*')):
            if path.is_file() and not path.is_symlink():
                yield path.relative_to(root)


def source_files(root: Path):
    candidates = []
    for name in ROOT_FILES:
        path = root / name
        if path.is_file() and not path.is_symlink():
            candidates.append(Path(name))
    for directory in SOURCE_DIRS:
        candidates.extend(_files_under(root, Path(directory)))
    candidates.extend(_files_under(root, Path('m3_control') / 'src'))
    for name in M3_FILES:
        path = root / 'm3_control' / name
        if path.is_file() and not path.is_symlink():
            candidates.append(Path('m3_control') / name)
    return [path for path in sorted(set(candidates), key=lambda item: item.as_posix()) if not _forbidden(path)]


def _target(root: Path, round_id: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    base = root / f'CFR_SOURCE_SNAPSHOT_{round_id}_{stamp}'
    candidate = base.with_suffix('.zip')
    index = 2
    while candidate.exists():
        candidate = root / f'{base.name}_{index}.zip'
        index += 1
    return candidate


def _verify(path: Path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.namelist()
    forbidden = [name for name in entries if '\\' in name or _forbidden(Path(name))]
    if forbidden:
        raise ValueError(', '.join(forbidden))
    return len(entries)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest().upper()


def write_snapshot(root: Path, round_id: str):
    output = _target(root, round_id)
    try:
        with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for relative in source_files(root):
                archive.write(root / relative, relative.as_posix())
        entries = _verify(output)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {
        'SourceSnapshotFile': output.name,
        'SourceSnapshotSha256': _sha256(output),
        'SourceSnapshotEntryCount': entries,
        'SourceSnapshotForbiddenEntries': 'NONE',
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('round_id')
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_]+', args.round_id):
        parser.error('round_id may contain only letters, digits, and underscores')
    try:
        print(json.dumps(write_snapshot(args.root.resolve(), args.round_id)))
    except Exception as error:
        print(json.dumps({'status': 'error', 'error_code': 'SOURCE_SNAPSHOT_FAILED', 'message': str(error)}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
