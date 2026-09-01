from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform as platform_module
import shutil


@dataclass(frozen=True)
class PlatformCapabilities:
    os_family: str
    platform_name: str
    python_version: str
    home_directory: Path
    default_cfr_data_directory: Path
    codex_launcher_supported: bool
    app_server_stdio_supported: bool
    rollout_watch_supported: bool
    desktop_continuity_live_validated: bool | None


def os_family(system=None):
    value = (system or platform_module.system()).lower()
    if value.startswith('win'):
        return 'windows'
    if value == 'darwin':
        return 'darwin'
    if value == 'linux':
        return 'linux'
    return 'unknown'


def open_codex_desktop_thread(thread_id: str, *, system=None, opener=None):
    """Ask Windows to open one existing Codex Desktop thread."""
    if os_family(system) != 'windows':
        raise NotImplementedError('Codex Desktop thread handoff is unsupported on this platform')
    launch = opener or getattr(os, 'startfile', None)
    if launch is None:
        raise NotImplementedError('The Codex Desktop protocol launcher is unavailable')
    uri = f'codex://threads/{thread_id}'
    try:
        launch(uri)
    except OSError as error:
        raise RuntimeError('Codex Desktop could not open the thread') from error
    return uri


def detect_platform_capabilities(desktop_continuity_live_validated=None, home_directory=None):
    family = os_family()
    home = Path(home_directory).expanduser() if home_directory else Path.home()
    supported = family in {'windows', 'darwin', 'linux'}
    return PlatformCapabilities(
        os_family=family,
        platform_name=platform_module.platform(),
        python_version=platform_module.python_version(),
        home_directory=home,
        default_cfr_data_directory=Path.cwd(),
        codex_launcher_supported=supported,
        app_server_stdio_supported=supported,
        rollout_watch_supported=supported,
        desktop_continuity_live_validated=desktop_continuity_live_validated,
    )


def codex_home_permissions(path: Path):
    path = Path(path)
    exists = path.exists()
    readable = os.access(path, os.R_OK) if exists else False
    writable = os.access(path, os.W_OK) if exists else False
    return {'Path': str(path), 'Exists': exists, 'Readable': readable, 'Writable': writable}
