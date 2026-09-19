"""Export the current CFR source into a clean public-repository tree.

This intentionally copies only maintained runtime, tests, build files, and
public-facing documentation. It never copies Git history or local runtime
state.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]

ROOT_FILES = (
    '.gitignore',
    'LICENSE',
    'SECURITY.md',
    'pyproject.toml',
    'START_CFR.cmd',
)

SCRIPT_FILES = (
    'check_public_release.py',
    'create_source_snapshot.py',
    'run_cfr.py',
    'run_cfr_control.py',
    'run_cfr_runtime_lease_probe.py',
    'run_codex_auth_routing_diagnostic.py',
    'run_codex_network_preflight.py',
    'run_codex_writer_handoff_probe.py',
    'run_feishu_credential_store_probe.py',
    'run_m2_feishu_sdk_probe.py',
)

M3_FILES = (
    'index.html',
    'package.json',
    'package-lock.json',
    'tsconfig.json',
    'vite.config.ts',
)

REFERENCE_DOCS = (
    'docs/PUBLIC_RELEASE_BOUNDARY.md',
    'docs/reference/CFR_AND_BROKER_COEXISTENCE_ROUTING_CONTRACT_v1.2.md',
    'docs/reference/README.md',
)


def _copy_file(relative: str, destination: Path) -> None:
    source = ROOT / relative
    if not source.is_file():
        raise FileNotFoundError(relative)
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_tree(relative: str, destination: Path) -> None:
    source = ROOT / relative
    if not source.is_dir():
        raise FileNotFoundError(relative)
    shutil.copytree(
        source,
        destination / relative,
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'),
    )


def export(destination: Path) -> None:
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f'destination already exists: {destination}')
    destination.mkdir(parents=True)

    for relative in ROOT_FILES:
        _copy_file(relative, destination)
    for relative in REFERENCE_DOCS:
        _copy_file(relative, destination)

    _copy_tree('src', destination)
    _copy_tree('tests', destination)
    _copy_tree('m3_control/src', destination)

    for name in SCRIPT_FILES:
        _copy_file(f'scripts/{name}', destination)
    for name in M3_FILES:
        _copy_file(f'm3_control/{name}', destination)


def init_git(destination: Path) -> None:
    subprocess.run(['git', 'init', '-b', 'main'], cwd=destination, check=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    parser.add_argument('--init-git', action='store_true')
    args = parser.parse_args(argv)
    try:
        export(args.destination)
        if args.init_git:
            init_git(args.destination.resolve())
    except Exception as error:
        print(f'PublicRepoExport: FAIL ({error})')
        return 1
    print(f'PublicRepoExport: PASS ({args.destination.resolve()})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
