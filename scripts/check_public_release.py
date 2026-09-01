"""Fail closed when tracked files cross CFR's public-release boundary."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_TRACKED = {
    '.gitattributes',
    '.gitignore',
    'CONTRIBUTING.md',
    'LICENSE',
    'README.md',
    'SECURITY.md',
    'pyproject.toml',
    'START_CFR.cmd',
}

FORBIDDEN_PREFIXES = (
    '.tmp/',
    'experiments/',
    'm3_control/dist/',
    'm3_control/node_modules/',
)

FORBIDDEN_NAMES = {
    '.env',
    'auth.json',
}

FORBIDDEN_SUFFIXES = (
    '.sqlite3',
    '.pyc',
)

SNAPSHOT_RE = re.compile(r'^CFR_SOURCE_SNAPSHOT_.*\.zip$', re.IGNORECASE)
MACHINE_PATH_RE = re.compile(
    r'(?:[A-Za-z]:\\Users\\[^\\\r\n]+\\|/Users/[^/\r\n]+/|/home/[^/\r\n]+/)',
    re.IGNORECASE,
)


def tracked_files() -> list[str]:
    completed = subprocess.run(
        ['git', 'ls-files', '-z'],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [item.decode('utf-8', errors='surrogateescape') for item in completed.stdout.split(b'\0') if item]


def excluded_path(path: str) -> bool:
    normalized = path.replace('\\', '/')
    name = Path(normalized).name
    return (
        normalized.startswith(FORBIDDEN_PREFIXES)
        or name in FORBIDDEN_NAMES
        or normalized.lower().endswith(FORBIDDEN_SUFFIXES)
        or bool(SNAPSHOT_RE.match(name))
        or '/__pycache__/' in f'/{normalized}/'
    )


def machine_paths(path: str) -> list[str]:
    normalized = path.replace('\\', '/')
    if normalized.startswith('tests/') or normalized == 'scripts/check_public_release.py':
        return []
    full = ROOT / path
    try:
        text = full.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return []
    return sorted(set(match.group(0) for match in MACHINE_PATH_RE.finditer(text)))


def main() -> int:
    problems: list[str] = []
    tracked = tracked_files()
    missing_required = sorted(REQUIRED_TRACKED.difference(tracked))
    for path in missing_required:
        problems.append(f'MISSING_REQUIRED_TRACKED_PATH {path}')
    forbidden_prefix_counts = {prefix: 0 for prefix in FORBIDDEN_PREFIXES}
    for path in tracked:
        if excluded_path(path):
            normalized = path.replace('\\', '/')
            prefix = next((item for item in FORBIDDEN_PREFIXES if normalized.startswith(item)), None)
            if prefix is not None:
                forbidden_prefix_counts[prefix] += 1
            else:
                problems.append(f'FORBIDDEN_TRACKED_PATH {path}')
            continue
        for value in machine_paths(path):
            problems.append(f'MACHINE_SPECIFIC_PATH {path}: {value}')

    for prefix, count in forbidden_prefix_counts.items():
        if count:
            problems.append(f'FORBIDDEN_TRACKED_PREFIX {prefix} ({count} files)')

    if problems:
        print('PublicReleaseBoundary: FAIL')
        for problem in problems:
            print(problem)
        print(f'ProblemCount: {len(problems)}')
        return 1

    print('PublicReleaseBoundary: PASS')
    print('ProblemCount: 0')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
