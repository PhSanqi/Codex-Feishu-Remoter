from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from cfr.core.models import StructuredError
from cfr.feishu.credentials import resolve_cfr_config_dir
from cfr.platform import hidden_subprocess_kwargs


CHROME_USE_VERSION = '1.5.123'
CHROME_USE_EXTENSION_VERSION = '0.5.26'
CHROME_USE_EXTENSION_ID = 'knfcmbamhjmaonkfnjhldjedeobeafmk'
CHROME_USE_SESSION = 'cfr-chat'


def resolve_chrome_use_binary(project_root: Path | str | None = None) -> Path | None:
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    candidates = [
        root / '.local-tools' / 'chrome-use' / 'chrome-use',
    ]
    discovered = shutil.which('chrome-use')
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def _system_chrome_profile_dir() -> Path:
    candidates = [
        Path.home() / '.config' / 'google-chrome' / 'Default',
        Path.home() / '.config' / 'chromium' / 'Default',
    ]
    return next((path for path in candidates if path.exists()), candidates[0])


class ChromeUseBridge:
    """Linux browser bridge backed by chrome-use Extension + Native Messaging.

    The bridge intentionally exposes the subset of the old Chrome DevTools MCP
    contract consumed by ``ChromeChatAdapter``.  All commands are scoped to one
    chrome-use session, so CFR never adopts or drives the human's other tabs.
    """

    mode = 'shared'
    driver = 'chrome-use'

    def __init__(
        self,
        *,
        binary: Path | str | None = None,
        project_root: Path | str | None = None,
        session: str = CHROME_USE_SESSION,
        timeout: float = 20,
    ):
        self.project_root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
        self.binary = Path(binary).expanduser() if binary else resolve_chrome_use_binary(self.project_root)
        self.session = str(session or CHROME_USE_SESSION)
        self.timeout = timeout
        self.user_data_dir = _system_chrome_profile_dir()
        self._running = False
        self._lock = threading.RLock()
        self._roots: tuple[Path, ...] = ()

    @property
    def running(self) -> bool:
        return self._running

    def _require_binary(self) -> Path:
        if self.binary is None or not self.binary.is_file():
            raise StructuredError(
                'CHAT_CHROME_USE_MISSING',
                'chrome-use 未安装；请重新运行 Linux bootstrap。',
            )
        return self.binary

    def _run(self, args: list[str], *, timeout: float | None = None, session: bool = True) -> dict[str, Any]:
        binary = self._require_binary()
        command = [str(binary)]
        if session:
            command.extend(['--session', self.session])
        command.extend(args)
        if '--json' not in command:
            command.append('--json')
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                timeout=timeout or self.timeout,
                check=False,
                **hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            raise StructuredError('CHAT_CHROME_USE_TIMEOUT', 'chrome-use 操作超时。') from exc
        except OSError as exc:
            raise StructuredError('CHAT_CHROME_USE_START_FAILED', '无法启动 chrome-use。') from exc
        payload = None
        for line in reversed(str(completed.stdout or '').splitlines()):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if not isinstance(payload, dict):
            detail = str(completed.stderr or '').strip()[:500]
            raise StructuredError(
                'CHAT_CHROME_USE_PROTOCOL_ERROR',
                f'chrome-use 没有返回有效 JSON{": " + detail if detail else ""}',
            )
        success = payload.get('success')
        if success is None:
            success = payload.get('ok')
        if success is False:
            detail = payload.get('error') or payload.get('message') or completed.stderr or 'chrome-use command failed'
            raise StructuredError('CHAT_CHROME_USE_FAILED', str(detail)[:800])
        return payload

    def start(self):
        with self._lock:
            status = self._run(['status'], timeout=8, session=False)
            data = status.get('data') if isinstance(status.get('data'), dict) else {}
            extension = data.get('extension') if isinstance(data.get('extension'), dict) else {}
            if not extension.get('hostInstalled') or not extension.get('hostHealthy'):
                raise StructuredError(
                    'CHAT_CHROME_USE_HOST_REQUIRED',
                    'chrome-use Native Messaging host 尚未就绪。',
                )
            if not extension.get('relayUp'):
                raise StructuredError(
                    'CHAT_CHROME_USE_EXTENSION_REQUIRED',
                    f'请在当前 Chrome 安装并启用 chrome-use 扩展（{CHROME_USE_EXTENSION_ID}）。',
                )
            try:
                self._run(['session', 'name', 'CFR Chat'], timeout=8)
            except StructuredError:
                # Session naming is cosmetic; a healthy relay/session remains usable.
                pass
            self._minimize_isolated_session_windows()
            self._running = True
        return self

    def close(self):
        with self._lock:
            # Keep the chrome-use session and its CFR-owned tab group alive.
            # CFR process restarts should not orphan/replace the background
            # ChatGPT tab or surface it to the user.
            self._running = False

    def set_roots(self, paths, *, timeout=5):
        roots = []
        for value in paths or ():
            path = Path(value).expanduser().resolve(strict=True)
            path = path.parent if path.is_file() else path
            if path not in roots:
                roots.append(path)
        self._roots = tuple(roots)

    @staticmethod
    def text(result):
        return '\n'.join(
            str(item.get('text') or '')
            for item in (result.get('content') or [])
            if isinstance(item, dict) and item.get('type') == 'text'
        ).strip()

    @staticmethod
    def _content(text: str) -> dict[str, Any]:
        return {'content': [{'type': 'text', 'text': str(text)}]}

    @staticmethod
    def _tab_number(tab_id: Any) -> int | None:
        match = re.fullmatch(r't?(\d+)', str(tab_id or '').strip())
        return int(match.group(1)) if match else None

    @staticmethod
    def _tab_token(page_id: Any) -> str:
        number = ChromeUseBridge._tab_number(page_id)
        if number is None:
            raise StructuredError('CHAT_CHROME_USE_TAB_INVALID', f'无效 CFR tab id：{page_id}')
        return f't{number}'

    def _tabs(self) -> list[dict[str, Any]]:
        payload = self._run(['tab'], timeout=8)
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        tabs = data.get('tabs') if isinstance(data.get('tabs'), list) else []
        return [
            tab for tab in tabs
            if isinstance(tab, dict) and tab.get('ownership') == 'created'
        ]

    def _extension_call(self, method: str, arguments: Any, *, timeout: float = 8) -> Any:
        payload = self._run(
            ['extension', 'call', method, json.dumps(arguments, ensure_ascii=False, separators=(',', ':'))],
            timeout=timeout,
            session=False,
        )
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        return data.get('result') if isinstance(data, dict) and 'result' in data else data

    def _created_tab_metadata(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for tab in self._tabs():
            token = str(tab.get('tabId') or '').strip()
            if not token:
                continue
            try:
                payload = self._run(['tab', 'inspect', token], timeout=8)
            except StructuredError:
                continue
            data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
            if data.get('windowId') is None or data.get('chromeTabId') is None:
                continue
            rows.append(data)
        return rows

    def _minimize_isolated_session_windows(self) -> None:
        """Hide CFR-owned Chrome windows without ever touching a user window.

        chrome-use creates a separate real-Chrome window for a named session on
        this Linux setup.  Before minimizing it, verify that every tab in that
        window is one of this session's ``created`` tabs.  If a foreign/user tab
        is present, fail closed rather than changing the user's window state.
        """
        metadata = self._created_tab_metadata()
        by_window: dict[int, set[int]] = {}
        for item in metadata:
            try:
                window_id = int(item['windowId'])
                tab_id = int(item['chromeTabId'])
            except (KeyError, TypeError, ValueError):
                continue
            by_window.setdefault(window_id, set()).add(tab_id)
        for window_id, owned_tab_ids in by_window.items():
            window = self._extension_call('windows.get', [window_id, {'populate': True}])
            if not isinstance(window, dict):
                raise StructuredError(
                    'CHAT_CHROME_USE_WINDOW_INSPECTION_FAILED',
                    '无法确认 CFR 后台浏览器窗口的隔离状态。',
                )
            all_tab_ids = {
                int(tab['id'])
                for tab in (window.get('tabs') or [])
                if isinstance(tab, dict) and tab.get('id') is not None
            }
            if not all_tab_ids or not all_tab_ids.issubset(owned_tab_ids):
                raise StructuredError(
                    'CHAT_CHROME_USE_WINDOW_NOT_ISOLATED',
                    'CFR Chat tab 与用户 tab 共用了同一个 Chrome window；为避免干扰用户，已拒绝自动化。',
                )
            if window.get('state') != 'minimized' or window.get('focused'):
                self._extension_call(
                    'windows.update',
                    [window_id, {'state': 'minimized', 'focused': False}],
                )

    def silent_window_snapshot(self) -> dict[str, Any]:
        """Return read-only isolation/minimize state for CFR-owned windows."""
        metadata = self._created_tab_metadata()
        by_window: dict[int, set[int]] = {}
        for item in metadata:
            try:
                window_id = int(item['windowId'])
                tab_id = int(item['chromeTabId'])
            except (KeyError, TypeError, ValueError):
                continue
            by_window.setdefault(window_id, set()).add(tab_id)
        windows = []
        for window_id, owned_tab_ids in by_window.items():
            window = self._extension_call('windows.get', [window_id, {'populate': True}])
            if not isinstance(window, dict):
                continue
            all_tab_ids = {
                int(tab['id'])
                for tab in (window.get('tabs') or [])
                if isinstance(tab, dict) and tab.get('id') is not None
            }
            windows.append({
                'window_id': window_id,
                'isolated': bool(all_tab_ids and all_tab_ids.issubset(owned_tab_ids)),
                'state': str(window.get('state') or 'unknown'),
                'focused': bool(window.get('focused')),
                'tab_count': len(all_tab_ids),
            })
        return {
            'session': self.session,
            'tab_count': len(metadata),
            'window_count': len(windows),
            'isolated': bool(windows) and all(item['isolated'] for item in windows),
            'minimized': bool(windows) and all(item['state'] == 'minimized' and not item['focused'] for item in windows),
            'windows': windows,
        }

    def _select(self, page_id: Any) -> str:
        token = self._tab_token(page_id)
        self._run(['tab', 'select', token], timeout=8)
        return token

    @staticmethod
    def _snapshot_to_mcp(text: str) -> str:
        """Translate chrome-use ``[ref=eN]`` lines into legacy ``uid=eN`` lines."""
        rendered = []
        for raw in str(text or '').splitlines():
            match = re.match(r'^(\s*)-\s+([A-Za-z][\w-]*)(?:\s+"((?:[^"\\]|\\.)*)")?\s*(?:\[(.*?)\])?\s*$', raw)
            if not match:
                rendered.append(raw)
                continue
            indent, role, name, attrs = match.groups()
            attrs = attrs or ''
            ref_match = re.search(r'(?:^|,\s*)ref=(e\d+)(?:,|$)', attrs)
            if not ref_match:
                rendered.append(raw)
                continue
            ref = ref_match.group(1)
            label = (name or '').replace('\\"', '"')
            normalized_role = 'Iframe' if role.lower() == 'iframe' else role
            suffix = []
            if re.search(r'(?:^|,\s*)checked=true(?:,|$)', attrs):
                suffix.append('checked')
            if re.search(r'(?:^|,\s*)disabled=true(?:,|$)', attrs):
                suffix.append('disabled')
            desc = re.search(r'(?:^|,\s*)description="([^"]*)"', attrs)
            if desc:
                suffix.append(f'description="{desc.group(1)}"')
            quoted = f' "{label}"' if label else ''
            rendered.append(f'{indent}uid={ref} {normalized_role}{quoted}{(" " + " ".join(suffix)) if suffix else ""}')
        return '\n'.join(rendered)

    def tool(self, name, arguments=None, *, timeout=None):
        with self._lock:
            self.start()
            args = dict(arguments or {})
            page_id = args.get('pageId')
            timeout_seconds = (float(timeout) if timeout else self.timeout)
            if name == 'list_pages':
                lines = []
                for tab in self._tabs():
                    if tab.get('ownership') != 'created':
                        continue
                    number = self._tab_number(tab.get('tabId'))
                    if number is None:
                        continue
                    selected = ' [selected]' if tab.get('active') else ''
                    title = str(tab.get('title') or 'Page')
                    url = str(tab.get('url') or '')
                    lines.append(f'{number}: {title} ({url}){selected}')
                return self._content('\n'.join(lines))
            if name == 'new_page':
                label = f'cfr-{int(time.time() * 1000)}'
                payload = self._run(['tab', 'new', '--label', label, str(args.get('url') or 'about:blank')], timeout=timeout_seconds)
                data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
                number = self._tab_number(data.get('tabId'))
                url = str(data.get('url') or args.get('url') or '')
                self._minimize_isolated_session_windows()
                return self._content(f'{number}: Page ({url}) [selected]' if number is not None else '')
            if name == 'select_page':
                self._select(page_id)
                return self.tool('list_pages')
            if name == 'close_page':
                token = self._tab_token(page_id)
                self._run(['tab', 'close', token], timeout=timeout_seconds)
                return self._content(f'Closed {token}')
            if name == 'navigate_page':
                self._select(page_id)
                payload = self._run(['open', str(args.get('url') or 'about:blank')], timeout=timeout_seconds)
                data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
                return self._content(json.dumps(data, ensure_ascii=False))
            if page_id is not None:
                self._select(page_id)
            if name == 'take_snapshot':
                payload = self._run(['snapshot', '-i'], timeout=timeout_seconds)
                data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
                snapshot = str(data.get('snapshot') or '')
                return self._content(self._snapshot_to_mcp(snapshot))
            if name == 'fill':
                uid = str(args.get('uid') or '')
                selector = uid if uid.startswith('@') else f'@{uid}'
                payload = self._run(['fill', selector, str(args.get('value') or '')], timeout=timeout_seconds)
                return self._content(json.dumps(payload.get('data') or {}, ensure_ascii=False))
            if name == 'press_key':
                payload = self._run(['press', str(args.get('key') or '')], timeout=timeout_seconds)
                return self._content(json.dumps(payload.get('data') or {}, ensure_ascii=False))
            if name == 'evaluate_script':
                function = str(args.get('function') or '').strip()
                script = f'({function})()' if re.match(r'^(?:async\s+)?\(?[^=]*\)?\s*=>|^function\b', function) else function
                payload = self._run(['eval', script], timeout=timeout_seconds)
                data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
                return self._content(json.dumps(data.get('result'), ensure_ascii=False))
            if name == 'upload_file':
                uid = str(args.get('uid') or '')
                selector = uid if uid.startswith('@') else f'@{uid}'
                files = [str(item) for item in (args.get('filePaths') or [])]
                payload = self._run(['upload', selector, *files], timeout=timeout_seconds)
                return self._content(json.dumps(payload.get('data') or {}, ensure_ascii=False))
            raise StructuredError('CHAT_CHROME_USE_TOOL_UNSUPPORTED', f'chrome-use 尚未适配浏览器工具：{name}')


def chrome_use_setup_snapshot(project_root: Path | str | None = None) -> dict[str, Any]:
    binary = resolve_chrome_use_binary(project_root)
    if binary is None:
        return {
            'installed': False,
            'version': None,
            'host_installed': False,
            'host_healthy': False,
            'relay_up': False,
            'extension_version': None,
        }
    bridge = ChromeUseBridge(binary=binary, project_root=project_root)
    try:
        payload = bridge._run(['status'], timeout=5, session=False)
    except StructuredError:
        return {
            'installed': True,
            'version': CHROME_USE_VERSION,
            'host_installed': False,
            'host_healthy': False,
            'relay_up': False,
            'extension_version': None,
        }
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    extension = data.get('extension') if isinstance(data.get('extension'), dict) else {}
    snapshot = {
        'installed': True,
        'version': str(data.get('cliVersion') or CHROME_USE_VERSION),
        'host_installed': bool(extension.get('hostInstalled')),
        'host_healthy': bool(extension.get('hostHealthy')),
        'relay_up': bool(extension.get('relayUp')),
        'extension_version': extension.get('liveVersion'),
    }
    sessions = data.get('sessions') if isinstance(data.get('sessions'), list) else []
    snapshot['session_running'] = any(
        isinstance(item, dict) and item.get('name') == CHROME_USE_SESSION for item in sessions
    )
    if snapshot['relay_up'] and snapshot['session_running']:
        try:
            snapshot['silent_window'] = bridge.silent_window_snapshot()
        except StructuredError:
            snapshot['silent_window'] = None
    else:
        snapshot['silent_window'] = None
    return snapshot
