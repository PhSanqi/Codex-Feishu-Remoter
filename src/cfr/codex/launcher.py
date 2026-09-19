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
        self._resolved: ResolvedCodexExecutable | None = None

    @staticmethod
    def _kind(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == '.cmd':
            return 'cmd'
        if suffix == '.bat':
            return 'bat'
        return 'native'

    @staticmethod
    def _project_local_candidates() -> tuple[Path, ...]:
        """Return Codex launchers installed by CFR's Linux bootstrap.

        Normal resolution still prefers an explicit path, CFR_CODEX_BIN and
        the ambient PATH.  This fallback exists so source-checkout CLI
        commands keep working when CFR was bootstrapped with the project-local
        npm prefix but the caller did not source scripts/cfr_env.sh first.
        """
        root = Path(__file__).resolve().parents[3]
        bin_dir = root / '.local-tools' / 'node_modules' / '.bin'
        names = ('codex.cmd', 'codex.exe', 'codex') if os.name == 'nt' else ('codex',)
        return tuple(bin_dir / name for name in names)

    def resolve(self) -> ResolvedCodexExecutable:
        if self._resolved is not None:
            return self._resolved
        if self.executable:
            self._resolved = ResolvedCodexExecutable(self.executable, 'explicit', self._kind(self.executable))
            return self._resolved
        configured = self.environment.get('CFR_CODEX_BIN')
        if configured:
            path = Path(configured).expanduser()
            self._resolved = ResolvedCodexExecutable(path, 'CFR_CODEX_BIN', self._kind(path))
            return self._resolved
        names = ('codex.cmd', 'codex.exe', 'codex') if os.name == 'nt' else ('codex',)
        for name in names:
            found = shutil.which(name, path=self.environment.get('PATH'))
            if found:
                path = Path(found)
                self._resolved = ResolvedCodexExecutable(path, 'path', self._kind(path))
                return self._resolved
        for path in self._project_local_candidates():
            if path.is_file():
                self._resolved = ResolvedCodexExecutable(path, 'project-local', self._kind(path))
                return self._resolved
        raise FileNotFoundError('codex executable not found via explicit path, CFR_CODEX_BIN, PATH, or CFR project-local tools')

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
