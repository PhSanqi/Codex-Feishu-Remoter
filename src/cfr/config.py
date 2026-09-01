from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ResolvedCfrCodexHome:
    path: Path
    source: str


def resolve_cfr_codex_home(explicit=None, environment: Mapping[str, str] | None = None, user_home=None):
    environment = os.environ if environment is None else environment
    if explicit:
        return ResolvedCfrCodexHome(Path(explicit).expanduser(), 'explicit')
    configured = environment.get('CFR_CODEX_HOME')
    if configured:
        return ResolvedCfrCodexHome(Path(configured).expanduser(), 'CFR_CODEX_HOME')
    base = Path(user_home).expanduser() if user_home else Path.home()
    return ResolvedCfrCodexHome(base / '.codex', 'default_user_home')


def child_process_env(process_env=None, resolved_home: ResolvedCfrCodexHome | None = None):
    child = dict(process_env or {})
    if resolved_home is not None:
        child['CODEX_HOME'] = str(resolved_home.path)
    return child


@dataclass(frozen=True)
class Settings:
    database: Path = Path("cfr.sqlite3")
    codex_home: Path | None = None

    def resolved_codex_home(self):
        return resolve_cfr_codex_home(self.codex_home)
