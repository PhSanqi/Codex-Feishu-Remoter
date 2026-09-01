from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Mapping


@dataclass(frozen=True)
class ResolvedCodexExecutable:
    path: Path
    source: str
    kind: str


class CodexLauncher:
    """Resolve and invoke Codex without shell-dependent or hard-coded paths."""

    def __init__(self, config_overrides=None, executable=None, environment: Mapping[str, str] | None = None):
        if isinstance(config_overrides, (str, Path)) and executable is None:
            executable = config_overrides
            config_overrides = None
        if config_overrides is None:
            self.config_overrides = []
        elif isinstance(config_overrides, dict):
            self.config_overrides = [f'{key}={value}' for key, value in config_overrides.items()]
        else:
            self.config_overrides = list(config_overrides)
        self.executable = Path(executable).expanduser() if executable else None
        self.environment = dict(os.environ if environment is None else environment)

    @staticmethod
    def _kind(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == '.cmd':
            return 'cmd'
        if suffix == '.bat':
            return 'bat'
        return 'native'

    def resolve(self) -> ResolvedCodexExecutable:
        if self.executable:
            return ResolvedCodexExecutable(self.executable, 'explicit', self._kind(self.executable))
        configured = self.environment.get('CFR_CODEX_BIN')
        if configured:
            path = Path(configured).expanduser()
            return ResolvedCodexExecutable(path, 'CFR_CODEX_BIN', self._kind(path))
        names = ('codex.cmd', 'codex.exe', 'codex') if os.name == 'nt' else ('codex',)
        for name in names:
            found = shutil.which(name, path=self.environment.get('PATH'))
            if found:
                path = Path(found)
                return ResolvedCodexExecutable(path, 'path', self._kind(path))
        raise FileNotFoundError('codex executable not found via explicit path, CFR_CODEX_BIN, or PATH')

    def discover(self) -> Path:
        return self.resolve().path

    def build_command(self, *args):
        resolved = self.resolve()
        command_args = []
        for override in self.config_overrides:
            command_args.extend(['--config', str(override)])
        command_args.extend(str(arg) for arg in args)
        if resolved.kind in ('cmd', 'bat'):
            comspec = self.environment.get('COMSPEC', os.environ.get('COMSPEC', r'C:\Windows\System32\cmd.exe'))
            return [comspec, '/d', '/s', '/c', str(resolved.path), *command_args]
        return [str(resolved.path), *command_args]

    def build_app_server_command(self):
        return self.build_command('app-server', '--stdio')
