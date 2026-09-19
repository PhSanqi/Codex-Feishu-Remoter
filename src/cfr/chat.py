from __future__ import annotations

from collections import deque
import base64
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from cfr.core.models import StructuredError
from cfr.feishu.credentials import resolve_cfr_config_dir
from cfr.platform import hidden_subprocess_kwargs


CHATGPT_HOSTS = {'chatgpt.com', 'www.chatgpt.com', 'chat.openai.com'}
CHROME_DEVTOOLS_MCP_VERSION = '1.8.0'
MAX_CHAT_DOWNLOAD_BYTES = 64 * 1024 * 1024
LINUX_SHARED_TAB_NAME = 'CFR_CHAT_SURFACE'
PROMPT_SELECTOR = (
    '#prompt-textarea, rich-textarea .ql-editor[contenteditable="true"], '
    'div.ql-editor[contenteditable="true"], div.ProseMirror[contenteditable="true"], '
    'div[contenteditable="true"][aria-label*="prompt" i], '
    'div[contenteditable="true"][aria-label*="message" i], '
    'textarea[aria-label*="prompt" i], textarea[placeholder*="ask" i], '
    'textarea[placeholder*="message" i], [role="textbox"][contenteditable="true"], '
    'main textarea, main [role="textbox"], main [contenteditable="true"]'
)
SEND_SELECTOR = (
    'button[data-testid="send-button"], button[data-testid*="send" i], '
    'button[aria-label="Send prompt"], button[aria-label="Send"], '
    'button[aria-label*="send message" i], button[type="submit"]'
)
STOP_SELECTOR = (
    'button[data-testid="stop-button"], button[data-testid*="stop" i], '
    'button[aria-label="Stop generating"], button[aria-label="Stop"], '
    'button[aria-label*="stop response" i]'
)
ASSISTANT_SELECTOR = (
    '[data-message-author-role="assistant"], model-response, .model-response-text, '
    '[data-test-id="model-response"], [data-response-author="model"], '
    '[data-testid*="assistant" i], [data-is-assistant="true"]'
)


def _chrome_executable():
    configured = os.environ.get('CFR_CHAT_CHROME_BIN')
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which('chrome') or shutil.which('google-chrome') or shutil.which('chromium')
    if found:
        return found
    if os.name == 'nt':
        for candidate in (
            Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe',
            Path(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe',
        ):
            if candidate.is_file():
                return str(candidate)
    return None


def linux_shared_tab_mode():
    """Linux CFR has one Chat browser policy: shared Chrome + one CFR-owned tab."""
    return os.name != 'nt'


def _system_chrome_profile_dir(environment=None):
    env = os.environ if environment is None else environment
    configured = str(env.get('CFR_CHAT_SHARED_PROFILE_DIR') or '').strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == 'nt':
        local = env.get('LOCALAPPDATA')
        return Path(local) / 'Google' / 'Chrome' / 'User Data' / 'Default' if local else None
    candidates = [
        Path.home() / '.config' / 'google-chrome' / 'Default',
        Path.home() / '.config' / 'chromium' / 'Default',
    ]
    return next((path for path in candidates if path.exists()), candidates[0])


def _profile_has_chatgpt_cookie(profile_dir):
    if profile_dir is None:
        return False
    for database in (profile_dir / 'Cookies', profile_dir / 'Network' / 'Cookies'):
        if not database.is_file():
            continue
        try:
            connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=0.2)
            try:
                row = connection.execute(
                    "select 1 from cookies where host_key like '%chatgpt.com' or host_key like '%openai.com' limit 1"
                ).fetchone()
            finally:
                connection.close()
            if row:
                return True
        except (sqlite3.Error, OSError):
            continue
    return False


def chat_setup_snapshot(environment=None, *, browser_backend=None, embedded_available=False):
    """Return non-invasive Chat Surface prerequisites for setup/readiness UI."""
    env = os.environ if environment is None else environment
    preference = str(browser_backend or env.get('CFR_CHAT_BROWSER_BACKEND') or env.get('CFR_CHAT_BROWSER_MODE') or 'auto').strip().lower()
    if preference not in {'auto', 'embedded', 'dedicated', 'shared'}:
        preference = 'auto'
    configured_profile = env.get('CFR_CHAT_BROWSER_PROFILE_DIR')
    config_dir = resolve_cfr_config_dir()
    user_data_dir = Path(configured_profile).expanduser() if configured_profile else config_dir / 'browser' / 'chatgpt-profile'
    state_dir = user_data_dir.parent
    embedded_state_dir = config_dir / 'browser' / 'embedded'
    embedded_storage = config_dir / 'webview2'
    dedicated_auth_marker = state_dir / 'chatgpt-authenticated'
    dedicated_pending_marker = state_dir / 'chatgpt-login-bootstrap.pending'
    embedded_auth_marker = embedded_state_dir / 'chatgpt-authenticated'
    embedded_pending_marker = embedded_state_dir / 'chatgpt-login-bootstrap.pending'
    chrome = _chrome_executable()
    npx = shutil.which('npx') or shutil.which('npx.cmd')
    shared_profile = _system_chrome_profile_dir(env)
    dedicated_authenticated = dedicated_auth_marker.exists()
    embedded_authenticated = embedded_auth_marker.exists()
    shared_authenticated = _profile_has_chatgpt_cookie(shared_profile)
    dedicated_ready = bool(chrome and npx and dedicated_authenticated)
    embedded_ready = bool(embedded_available and npx and embedded_authenticated)
    shared_ready = bool(chrome and shared_authenticated)
    linux_shared_tab = linux_shared_tab_mode()
    if linux_shared_tab:
        # Linux has one browser model only: attach to the user's already-running
        # Chrome and own one background ChatGPT tab inside it.  Legacy
        # embedded/dedicated preferences are intentionally ignored at runtime.
        effective = 'shared'
    elif preference == 'embedded':
        # WebView2 is not a Linux backend. Treat a migrated explicit embedded
        # preference as stale platform state so setup UI and runtime agree.
        effective = 'embedded' if embedded_available or os.name == 'nt' else 'shared'
    elif preference == 'dedicated':
        effective = 'dedicated'
    elif preference == 'shared':
        effective = 'shared'
    elif embedded_available:
        effective = 'embedded'
    elif os.name != 'nt':
        effective = 'shared'
    else:
        effective = 'dedicated'
    if effective == 'embedded':
        effective_ready = embedded_ready
        effective_authenticated = embedded_authenticated
        effective_pending = embedded_pending_marker.exists()
        effective_profile = embedded_storage
    elif effective == 'shared':
        effective_ready = shared_ready
        effective_authenticated = shared_authenticated
        effective_pending = False
        effective_profile = shared_profile
    else:
        effective_ready = dedicated_ready
        effective_authenticated = dedicated_authenticated
        effective_pending = dedicated_pending_marker.exists()
        effective_profile = user_data_dir
    return {
        'mode': effective,
        'preference': preference,
        'effective_backend': effective,
        'chrome_available': bool(chrome),
        'chrome_executable': chrome,
        'npx_available': bool(npx),
        'npx_executable': npx,
        'authenticated': effective_authenticated,
        'login_pending': effective_pending,
        'profile_dir': str(effective_profile) if effective_profile is not None else None,
        'ready': effective_ready,
        'legacy_profile_preserved': True,
        'linux_shared_tab': linux_shared_tab,
        'shared_tab_name': LINUX_SHARED_TAB_NAME if linux_shared_tab else None,
        'dedicated': {
            'available': bool(chrome and npx),
            'authenticated': dedicated_authenticated,
            'login_pending': dedicated_pending_marker.exists(),
            'profile_dir': str(user_data_dir),
            'ready': dedicated_ready,
        },
        'embedded': {
            'available': bool(embedded_available and npx),
            'authenticated': embedded_authenticated,
            'login_pending': embedded_pending_marker.exists(),
            'state_dir': str(embedded_state_dir),
            'storage_path': str(embedded_storage),
            'ready': embedded_ready,
        },
        'shared': {
            'available': bool(chrome) if linux_shared_tab else bool(chrome and npx),
            'authenticated': shared_authenticated,
            'login_pending': False,
            'profile_dir': str(shared_profile) if shared_profile is not None else None,
            'ready': shared_ready,
        },
    }


class ChromeDevToolsMcp:
    """Small synchronous MCP client for the CFR-owned Chrome DevTools MCP child."""

    def __init__(self, *, timeout=8, command=None, mode=None, user_data_dir=None, browser_url=None):
        self.timeout = timeout
        self.mode = str(mode or os.environ.get('CFR_CHAT_BROWSER_MODE') or 'dedicated').strip().lower()
        if self.mode not in {'dedicated', 'shared', 'embedded'}:
            raise StructuredError('CHAT_BROWSER_MODE_INVALID', 'CFR_CHAT_BROWSER_MODE must be dedicated, embedded, or shared.')
        configured_profile = user_data_dir or os.environ.get('CFR_CHAT_BROWSER_PROFILE_DIR')
        if self.mode == 'shared':
            default_profile = _system_chrome_profile_dir() or (resolve_cfr_config_dir() / 'browser' / 'shared')
        else:
            default_profile = resolve_cfr_config_dir() / 'browser' / ('embedded' if self.mode == 'embedded' else 'chatgpt-profile')
        self.user_data_dir = Path(configured_profile).expanduser() if configured_profile else default_profile
        self.browser_url = str(browser_url or os.environ.get('CFR_CHAT_BROWSER_URL') or '').strip() or None
        self.command = command or self._default_command()
        self.proc = None
        self._lock = threading.RLock()
        self._messages = queue.Queue()
        self._stderr = deque(maxlen=30)
        self._roots = ()
        self._id = 0
        self._stdout_thread = None
        self._stderr_thread = None

    def _default_command(self):
        npx = shutil.which('npx') or shutil.which('npx.cmd')
        if not npx:
            return None
        command = [
            npx, '-y', '--prefer-offline', f'chrome-devtools-mcp@{CHROME_DEVTOOLS_MCP_VERSION}',
            '--no-category-emulation', '--no-category-performance',
            '--no-category-network', '--no-usage-statistics',
        ]
        if self.mode == 'shared':
            command.append('--autoConnect')
        elif self.mode == 'embedded':
            if not self.browser_url:
                raise StructuredError('CHAT_EMBEDDED_BROWSER_URL_REQUIRED', '内置 ChatGPT 浏览器缺少本机 DevTools 地址。')
            command.extend(['--browserUrl', self.browser_url])
        else:
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            command.extend(['--userDataDir', str(self.user_data_dir)])
        return command

    @property
    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def start(self):
        with self._lock:
            if self.running:
                return self
            if not self.command:
                raise StructuredError('CHAT_BROWSER_MCP_MISSING', 'Node/npx 不可用，无法启动 Chrome DevTools MCP。')
            if self.mode == 'embedded':
                self._wait_embedded_browser()
            self._messages = queue.Queue()
            self._stderr.clear()
            self._id = 0
            self.proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                bufsize=1,
                **hidden_subprocess_kwargs(new_process_group=True),
            )
            self._stdout_thread = threading.Thread(target=self._read_stdout, name='cfr-chat-browser-stdout', daemon=True)
            self._stderr_thread = threading.Thread(target=self._read_stderr, name='cfr-chat-browser-stderr', daemon=True)
            self._stdout_thread.start()
            self._stderr_thread.start()
            try:
                self._request_locked('initialize', {
                    'protocolVersion': '2025-06-18',
                    'capabilities': {'roots': {'listChanged': True}},
                    'clientInfo': {'name': 'cfr', 'version': '0.1'},
                }, timeout=max(self.timeout, 20))
                self._write({'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {}})
            except Exception:
                self.close()
                raise
            return self

    def _wait_embedded_browser(self, timeout=20):
        if not self.browser_url:
            raise StructuredError('CHAT_EMBEDDED_BROWSER_URL_REQUIRED', '内置 ChatGPT 浏览器缺少本机 DevTools 地址。')
        deadline = time.monotonic() + timeout
        endpoint = self.browser_url.rstrip('/') + '/json/version'
        while time.monotonic() < deadline:
            try:
                with urlopen(endpoint, timeout=0.75) as response:
                    if response.status == 200:
                        return
            except (OSError, HTTPError, URLError):
                pass
            time.sleep(0.1)
        raise StructuredError('CHAT_EMBEDDED_BROWSER_NOT_READY', 'CFR 内置 ChatGPT 浏览器尚未准备好。')

    def _read_stdout(self):
        stream = self.proc.stdout if self.proc else None
        if not stream:
            return
        for line in stream:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if 'id' in message or message.get('method') == 'roots/list':
                self._messages.put(message)

    def _read_stderr(self):
        stream = self.proc.stderr if self.proc else None
        if not stream:
            return
        for line in stream:
            self._stderr.append(line.rstrip())

    def _write(self, message):
        if not self.running or not self.proc.stdin:
            raise StructuredError('CHAT_BROWSER_MCP_STOPPED', 'Chrome DevTools MCP 未运行。')
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()

    def _request_locked(self, method, params, *, timeout):
        self._id += 1
        request_id = self._id
        self._write({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = self._stderr[-1] if self._stderr else None
                raise StructuredError('CHAT_BROWSER_MCP_TIMEOUT', f'Chrome DevTools MCP 响应超时。{f" {detail}" if detail else ""}')
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty as error:
                raise StructuredError('CHAT_BROWSER_MCP_TIMEOUT', 'Chrome DevTools MCP 响应超时。') from error
            if self._handle_server_request_locked(message):
                continue
            if message.get('id') != request_id:
                continue
            if message.get('error'):
                rpc_error = message['error'] or {}
                raise StructuredError('CHAT_BROWSER_MCP_FAILED', str(rpc_error.get('message') or 'Chrome DevTools MCP request failed'))
            return message.get('result') or {}

    def _root_descriptors(self):
        return [{'uri': path.as_uri(), 'name': path.name or str(path)} for path in self._roots]

    def _handle_server_request_locked(self, message):
        if message.get('method') != 'roots/list' or 'id' not in message:
            return False
        self._write({'jsonrpc': '2.0', 'id': message['id'], 'result': {'roots': self._root_descriptors()}})
        return True

    def set_roots(self, paths, *, timeout=5):
        roots = []
        for value in paths or ():
            path = Path(value).expanduser().resolve(strict=True)
            path = path.parent if path.is_file() else path
            if not path.is_dir():
                raise StructuredError('CHAT_BROWSER_ROOT_INVALID', f'Chat 浏览器文件根目录无效：{path}')
            if path not in roots:
                roots.append(path)
        with self._lock:
            roots = tuple(roots)
            if roots == self._roots:
                return
            self._roots = roots
            if not self.running:
                return
            self._write({'jsonrpc': '2.0', 'method': 'notifications/roots/list_changed'})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    message = self._messages.get(timeout=deadline - time.monotonic())
                except queue.Empty as error:
                    raise StructuredError('CHAT_BROWSER_ROOTS_TIMEOUT', 'Chrome DevTools MCP 未确认新的文件访问根目录。') from error
                if self._handle_server_request_locked(message):
                    return
            raise StructuredError('CHAT_BROWSER_ROOTS_TIMEOUT', 'Chrome DevTools MCP 未确认新的文件访问根目录。')

    def request(self, method, params=None, *, timeout=None):
        with self._lock:
            self.start()
            return self._request_locked(method, params or {}, timeout=timeout or self.timeout)

    def tool(self, name, arguments=None, *, timeout=None):
        result = self.request('tools/call', {'name': name, 'arguments': arguments or {}}, timeout=timeout)
        if result.get('isError'):
            raise StructuredError('CHAT_BROWSER_TOOL_FAILED', self.text(result) or f'{name} failed')
        return result

    @staticmethod
    def text(result):
        return '\n'.join(
            str(item.get('text') or '')
            for item in (result.get('content') or [])
            if isinstance(item, dict) and item.get('type') == 'text'
        ).strip()

    def close(self):
        with self._lock:
            proc, self.proc = self.proc, None
            stdout_thread, stderr_thread = self._stdout_thread, self._stderr_thread
            self._stdout_thread = None
            self._stderr_thread = None
            if not proc:
                return
            try:
                if proc.poll() is None:
                    if os.name == 'nt':
                        subprocess.run(
                            ['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
                            **hidden_subprocess_kwargs(),
                        )
                    else:
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        if os.name == 'nt':
                            proc.kill()
                        else:
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                if proc.poll() is None:
                    if os.name == 'nt':
                        subprocess.run(
                            ['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False,
                            **hidden_subprocess_kwargs(),
                        )
                    else:
                        proc.kill()
            finally:
                for thread in (stdout_thread, stderr_thread):
                    if thread and thread.is_alive():
                        thread.join(timeout=0.5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        if stream and not stream.closed:
                            stream.close()
                    except OSError:
                        pass


class ChromeChatAdapter:
    """Control native ChatGPT conversations in the CFR-owned browser runtime."""

    def __init__(self, mcp=None, *, query_timeout=8 * 60, show_browser=None):
        self.mcp = mcp or ChromeDevToolsMcp()
        self.query_timeout = query_timeout
        self._show_browser_callback = show_browser
        self._login_proc = None
        self._health_lock = threading.RLock()
        self._last_health = None

    @property
    def _browser_state_dir(self):
        return Path(self.mcp.user_data_dir).parent

    @property
    def _auth_marker(self):
        return self._browser_state_dir / 'chatgpt-authenticated'

    @property
    def _login_pending_marker(self):
        return self._browser_state_dir / 'chatgpt-login-bootstrap.pending'

    def start(self):
        """Start one CFR browser runtime; dedicated login is bootstrapped without automation."""
        if getattr(self.mcp, 'mode', 'dedicated') in {'shared', 'embedded'}:
            self.mcp.start()
            self._ensure_chatgpt_tab()
        return self.health()

    def show_browser(self):
        callback = self._show_browser_callback
        if callback is not None:
            callback()

    def start_manual_login(self):
        """Launch the dedicated ChatGPT profile with no automation attached.

        This path is intentionally separate from normal health/startup.  It
        shuts down MCP and any Chrome process using the CFR-owned profile, then
        starts a plain Chrome window.  Call ``finish_manual_login`` only after
        the user has completed login and closed that window.
        """
        mode = getattr(self.mcp, 'mode', 'dedicated')
        if mode == 'shared':
            self.mcp.close()
            chrome = _chrome_executable()
            if not chrome:
                raise StructuredError('CHAT_CHROME_MISSING', '未找到普通 Google Chrome。')
            try:
                subprocess.Popen(
                    [chrome, 'https://chatgpt.com/'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **hidden_subprocess_kwargs(),
                )
            except OSError as exc:
                raise StructuredError('CHAT_MANUAL_LOGIN_START_FAILED', '无法打开普通 Chrome。') from exc
            return {
                'available': False,
                'status': 'waiting_user',
                'error_code': 'CHAT_SHARED_CONNECT_REQUIRED',
                'mode': mode,
                'manual_login': True,
                'description': (
                    '已在你当前的普通 Chrome 中打开 ChatGPT。Linux CFR 使用 chrome-use 扩展 + '
                    'Native Messaging，不再要求 Remote Debugging。'
                ),
                'url': 'https://chatgpt.com/',
            }
        if mode != 'dedicated':
            raise StructuredError(
                'CHAT_MANUAL_LOGIN_DEDICATED_ONLY',
                '人工登录仅适用于 CFR 专用 Chrome 后端。',
            )
        self.mcp.close()
        self._stop_profile_browser()
        self._auth_marker.unlink(missing_ok=True)
        self._login_pending_marker.unlink(missing_ok=True)
        self._launch_login_bootstrap()
        state = self._waiting_for_login()
        state['manual_login'] = True
        state['description'] = (
            '已打开纯人工 ChatGPT 登录窗口。登录期间 CFR 不连接 DevTools/MCP；'
            '完成登录后请关闭该窗口，再点击“检测登录状态”。'
        )
        return state

    def finish_manual_login(self):
        """Verify a manual login only after the plain Chrome window is closed."""
        mode = getattr(self.mcp, 'mode', 'dedicated')
        if mode == 'shared':
            try:
                self.mcp.start()
                self._ensure_chatgpt_tab()
            except StructuredError as error:
                return {
                    'available': False,
                    'status': 'not_connected',
                    'error_code': error.code,
                    'mode': mode,
                    'manual_login': True,
                    'description': error.message,
                    'url': 'https://chatgpt.com/',
                }
            return self.health()
        if mode != 'dedicated':
            raise StructuredError(
                'CHAT_MANUAL_LOGIN_DEDICATED_ONLY',
                '人工登录仅适用于 CFR 专用 Chrome 后端。',
            )
        if self._profile_browser_running():
            return {
                'available': False,
                'status': 'waiting_user',
                'error_code': 'CHAT_MANUAL_LOGIN_BROWSER_STILL_OPEN',
                'mode': mode,
                'manual_login': True,
                'description': '请先关闭人工登录用的 Chrome 窗口，再检测登录状态。',
                'url': 'https://chatgpt.com/',
            }
        return self.health()

    def _remember_health(self, state):
        if not hasattr(self, '_health_lock'):
            self._health_lock = threading.RLock()
            self._last_health = None
        with self._health_lock:
            self._last_health = dict(state)
        return state

    def health_snapshot(self):
        """Return cached browser health without starting Chrome or reading the DOM."""
        mode = getattr(self.mcp, 'mode', 'unknown')
        if not getattr(self.mcp, 'running', False):
            waiting = bool(
                mode in {'dedicated', 'embedded'}
                and (
                    self._login_pending_marker.exists()
                    or (self._login_proc is not None and self._login_proc.poll() is None)
                )
            )
            return {
                'available': False,
                'status': 'waiting_user' if waiting else 'not_connected',
                'error_code': 'CHATGPT_LOGIN_REQUIRED' if waiting else 'CHAT_BROWSER_NOT_RUNNING',
                'mode': mode,
                'description': '等待完成 ChatGPT 登录。' if waiting else 'CFR Chat 浏览器运行时未启动。',
                'url': None,
            }
        with self._health_lock:
            state = dict(self._last_health or {})
        if state:
            return state
        return {
            'available': False,
            'status': 'starting',
            'error_code': None,
            'mode': mode,
            'description': 'CFR Chat 浏览器运行时正在初始化。',
            'url': None,
        }

    def health(self):
        mode = getattr(self.mcp, 'mode', 'dedicated')
        if mode == 'dedicated':
            waiting = self._prepare_dedicated_runtime()
            if waiting is not None:
                return self._remember_health(waiting)
        elif mode == 'embedded' and not getattr(self.mcp, 'running', False):
            try:
                self.mcp.start()
                self._ensure_chatgpt_tab()
            except StructuredError as error:
                return self._remember_health({
                    'available': False,
                    'status': 'not_connected',
                    'error_code': error.code,
                    'mode': mode,
                    'description': error.message,
                    'url': None,
                })
        if getattr(self.mcp, 'driver', None) != 'chrome-use' and not self._remote_debugging_enabled():
            return self._remember_health({
                'available': False,
                'status': 'not_connected',
                'error_code': 'CHAT_CHROME_REMOTE_DEBUGGING_DISABLED',
                'description': '请在 Chrome 的 chrome://inspect/#remote-debugging 开启 Remote Debugging。',
            })
        try:
            pages = self._pages()
        except StructuredError as error:
            return self._remember_health({'available': False, 'status': 'not_connected', 'error_code': error.code, 'description': error.message})
        if mode == 'shared':
            owner = self._shared_owner_page(pages)
            chat_pages = [owner] if owner is not None else []
        else:
            chat_pages = [page for page in pages if self._is_chatgpt_url(page['url'])]
        state = self._state(chat_pages[0]['id']) if chat_pages else {}
        if state.get('blocked'):
            if mode == 'dedicated' and state.get('kind') == 'login':
                self.mcp.close()
                self._auth_marker.unlink(missing_ok=True)
                self._launch_login_bootstrap()
                return self._remember_health(self._waiting_for_login())
            if mode == 'embedded' and state.get('kind') == 'login':
                self._browser_state_dir.mkdir(parents=True, exist_ok=True)
                self._login_pending_marker.touch()
                self._auth_marker.unlink(missing_ok=True)
                self.show_browser()
                return self._remember_health(self._waiting_for_login())
            if state.get('kind') == 'rate_limited':
                return self._remember_health({
                    'available': False,
                    'status': 'rate_limited',
                    'error_code': 'CHATGPT_RATE_LIMITED',
                    'mode': getattr(self.mcp, 'mode', 'unknown'),
                    'description': 'ChatGPT 暂时限制了网页请求频率；请等待几分钟后再重试。',
                    'url': chat_pages[0]['url'],
                })
            return self._remember_health({
                'available': False,
                'status': 'waiting_user',
                'error_code': 'CHATGPT_WEB_NOT_READY',
                'mode': getattr(self.mcp, 'mode', 'unknown'),
                'description': f'ChatGPT 专用浏览器需要人工处理：{state.get("kind") or "verification"}。',
                'url': chat_pages[0]['url'],
            })
        if mode in {'dedicated', 'embedded'} and state.get('authenticated'):
            self._browser_state_dir.mkdir(parents=True, exist_ok=True)
            self._auth_marker.touch()
            self._login_pending_marker.unlink(missing_ok=True)
        descriptions = {
            'dedicated': 'CFR 专用 Chrome 已连接；原有登录 Profile 保持不变。',
            'embedded': 'CFR 内置 ChatGPT 浏览器已连接；登录态保存在 CFR 持久化 WebView2 User Data Folder，并与旧 Chrome Profile 隔离。',
            'shared': (
                '已连接 chrome-use Linux session；CFR 只操作自己的后台 ChatGPT tab group。'
                if getattr(self.mcp, 'driver', None) == 'chrome-use'
                else '已连接当前 Chrome；CFR 只操作带独占标记的 Linux 后台 ChatGPT 标签页。'
            ),
        }
        return self._remember_health({
            'available': bool(chat_pages and state.get('promptVisible')),
            'status': 'ready' if chat_pages and state.get('promptVisible') else 'page_not_ready' if chat_pages else 'ready_no_chat_tab',
            'mode': getattr(getattr(self, 'mcp', None), 'mode', 'unknown'),
            'description': descriptions.get(mode, 'CFR Chat 浏览器已连接。'),
            'url': chat_pages[0]['url'] if chat_pages else None,
        })

    def status(self, binding=None):
        page = self._page(binding, create=False)
        if page is None:
            return {'ready': False, 'blocked': False, 'url': None, 'status': 'no_chat_tab'}
        state = self._state(page['id'])
        state['status'] = 'blocked' if state.get('blocked') else 'ready' if state.get('promptVisible') else 'page_not_ready'
        return state

    def send_message(self, binding, prompt, *, on_progress=None, file_paths=None):
        return self._send_message(binding, prompt, on_progress=on_progress, file_paths=file_paths)

    def send_search(self, binding, prompt):
        return self._send_message(binding, prompt, tool_labels=('网页搜索', 'Web search', 'Search the web'))

    def start_deep_research(self, binding, prompt):
        prompt = str(prompt or '').strip()
        if not prompt:
            raise StructuredError('CHAT_DEEP_RESEARCH_PROMPT_REQUIRED', 'Deep Research 需要非空问题。')
        page = self._page(binding, create=True)
        self._restore_bound_url(page['id'], binding.get('url'))
        before = self._wait_ready(page['id'])
        if self.parse_identity(before.get('url')).get('conversation_id'):
            before = self._wait_history(page['id'], before)
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
        before_count = snapshot.count('Iframe "internal://deep-research"')
        prompt_uid = self._prompt_uid(page['id'])
        self.mcp.tool('fill', {'pageId': page['id'], 'uid': prompt_uid, 'value': prompt})
        self._select_tool(page['id'], ('深度研究', 'Deep research'))
        self.mcp.tool('press_key', {'pageId': page['id'], 'key': 'Enter'})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
            if snapshot.count('Iframe "internal://deep-research"') > before_count:
                state = self._parse_deep_research_snapshot(snapshot)
                current = self._state(page['id'])
                return {
                    'state': state['state'],
                    'report': state.get('report'),
                    'tab_id': str(page['id']),
                    'url': current.get('url'),
                    **self.parse_identity(current.get('url')),
                }
            time.sleep(0.4)
        raise StructuredError('CHAT_DEEP_RESEARCH_START_TIMEOUT', '未能确认 ChatGPT Deep Research 已启动。')

    def deep_research_status(self, binding):
        page = self._page(binding, create=bool((binding or {}).get('url')))
        if page is None:
            return {'state': 'none', 'report': None, 'tab_id': None, 'url': None}
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
        return {'tab_id': str(page['id']), 'url': page['url'], **self._parse_deep_research_snapshot(snapshot)}

    def start_image_generation(self, binding, prompt):
        prompt = str(prompt or '').strip()
        if not prompt:
            raise StructuredError('CHAT_IMAGE_PROMPT_REQUIRED', '图片生成需要非空提示词。')
        page = self._page(binding, create=True)
        self._restore_bound_url(page['id'], binding.get('url'))
        before = self._wait_ready(page['id'])
        if self.parse_identity(before.get('url')).get('conversation_id'):
            before = self._wait_history(page['id'], before)
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
        before_count = self._image_request_count(snapshot)
        prompt_uid = self._prompt_uid(page['id'])
        self.mcp.tool('fill', {'pageId': page['id'], 'uid': prompt_uid, 'value': prompt})
        self._select_tool(page['id'], ('创建图片', 'Create image', 'Create images'))
        self.mcp.tool('press_key', {'pageId': page['id'], 'key': 'Enter'})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
            if self._image_request_count(snapshot) > before_count:
                state = self._parse_image_snapshot(snapshot)
                current = self._state(page['id'])
                return {
                    'state': state,
                    'tab_id': str(page['id']),
                    'url': current.get('url'),
                    **self.parse_identity(current.get('url')),
                }
            time.sleep(0.4)
        raise StructuredError('CHAT_IMAGE_START_TIMEOUT', '未能确认 ChatGPT 图片生成已启动。')

    def image_status(self, binding):
        page = self._page(binding, create=bool((binding or {}).get('url')))
        if page is None:
            return {'state': 'none', 'tab_id': None, 'url': None, 'image': None}
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
        state = self._parse_image_snapshot(snapshot)
        image = self._latest_generated_image(page['id']) if state == 'completed' else None
        return {'state': state, 'tab_id': str(page['id']), 'url': page['url'], 'image': image}

    def download_generated_image(self, binding, image=None):
        page = self._page(binding, create=bool((binding or {}).get('url')))
        if page is None:
            raise StructuredError('CHAT_IMAGE_NOT_FOUND', '当前 ChatGPT Conversation 没有可下载的生成图片。')
        state = self._wait_ready(page['id'])
        if self.parse_identity(state.get('url')).get('conversation_id'):
            self._wait_history(page['id'], state)
        if not image:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                image = self._latest_generated_image(page['id'])
                if image and image.get('src'):
                    break
                time.sleep(0.2)
        if not image or not image.get('src'):
            raise StructuredError('CHAT_IMAGE_NOT_FOUND', '当前 ChatGPT Conversation 没有可下载的生成图片。')
        source = json.dumps(image['src'])
        max_bytes = MAX_CHAT_DOWNLOAD_BYTES
        payload = self._evaluate(page['id'], f'''async () => {{
          const response = await fetch({source}, {{credentials: 'include'}});
          if (!response.ok) return {{ok: false, status: response.status}};
          const declared = Number(response.headers.get('content-length') || 0);
          if (declared > {max_bytes}) return {{ok: false, stage: 'size', size: declared}};
          const buffer = await response.arrayBuffer();
          if (buffer.byteLength > {max_bytes}) return {{ok: false, stage: 'size', size: buffer.byteLength}};
          const bytes = new Uint8Array(buffer);
          let binary = '';
          for (let offset = 0; offset < bytes.length; offset += 32768) {{
            binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
          }}
          return {{ok: true, contentType: response.headers.get('content-type') || '', size: bytes.length, base64: btoa(binary)}};
        }}''', timeout=180)
        if not isinstance(payload, dict) or not payload.get('ok'):
            if isinstance(payload, dict) and payload.get('stage') == 'size':
                raise StructuredError('CHAT_IMAGE_TOO_LARGE', f'ChatGPT 图片超过 CFR 下载上限：{payload.get("size") or "unknown"} bytes')
            raise StructuredError('CHAT_IMAGE_DOWNLOAD_FAILED', f'ChatGPT 图片下载失败：HTTP {payload.get("status") if isinstance(payload, dict) else "unknown"}')
        content_type = str(payload.get('contentType') or '')
        if not content_type.lower().startswith('image/'):
            raise StructuredError('CHAT_IMAGE_DOWNLOAD_FAILED', f'ChatGPT 返回的不是图片：{content_type or "unknown content type"}')
        try:
            data = base64.b64decode(str(payload.get('base64') or ''), validate=True)
        except ValueError as error:
            raise StructuredError('CHAT_IMAGE_DOWNLOAD_FAILED', 'ChatGPT 图片数据解码失败。') from error
        if len(data) != int(payload.get('size') or -1):
            raise StructuredError('CHAT_IMAGE_DOWNLOAD_FAILED', 'ChatGPT 图片下载大小校验失败。')
        return {'data': data, 'content_type': content_type, 'source': image['src'], 'alt': image.get('alt') or ''}

    def download_generated_image_to_file(self, binding):
        image = self.download_generated_image(binding)
        destination = resolve_cfr_config_dir() / 'chatgpt' / 'downloads' / uuid.uuid4().hex
        destination.mkdir(parents=True, exist_ok=True)
        source_id = re.search(r'[?&]id=([^&]+)', str(image.get('source') or ''))
        stem = re.sub(r'[^A-Za-z0-9._-]+', '_', source_id.group(1) if source_id else 'chatgpt-image').strip('._') or 'chatgpt-image'
        extension = {
            'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'image/gif': '.gif',
        }.get(str(image.get('content_type') or '').lower(), '.img')
        path = destination / f'{stem}{extension}'
        temp_path = destination / f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp'
        try:
            temp_path.write_bytes(image['data'])
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
        return {**image, 'path': str(path)}

    def _capture_generated_file_download_url(self, page_id, file_name):
        """Ask the real ChatGPT file card for its current signed download URL."""
        wanted = json.dumps(str(file_name or ''), ensure_ascii=False)
        payload = self._evaluate(page_id, f'''async () => {{
          const wanted = {wanted};
          const normalize = value => String(value || '').trim().replace(/\\s+/g, ' ');
          const downloadSelector = 'button[aria-label="Download file"], button[aria-label="下载文件"]';
          const downloadButtons = Array.from(document.querySelectorAll(downloadSelector));
          let button = null;
          if (wanted) {{
            const labels = Array.from(document.querySelectorAll('button[aria-label], button'))
              .filter(node => normalize(node.getAttribute('aria-label') || node.textContent) === normalize(wanted));
            for (const label of labels) {{
              let root = label;
              for (let depth = 0; root && depth < 8; depth++, root = root.parentElement) {{
                const candidate = root.querySelector?.(downloadSelector);
                if (candidate) {{ button = candidate; break; }}
              }}
              if (button) break;
            }}
          }}
          if (!button) button = downloadButtons[downloadButtons.length - 1] || null;
          if (!button) return {{ok: false, stage: 'button'}};

          const captured = [];
          const oldFetch = window.fetch;
          const oldOpen = window.open;
          const oldAnchorClick = HTMLAnchorElement.prototype.click;
          const record = (kind, url, status) => {{
            url = String(url || '');
            if (/[/]backend-api[/](?:estuary[/]content|files[/])/i.test(url)) captured.push({{kind, url, status: status ?? null}});
          }};
          try {{
            window.fetch = async (...args) => {{
              const input = args[0];
              const requestUrl = typeof input === 'string' ? input : (input?.url || String(input || ''));
              record('fetch-request', requestUrl);
              const response = await oldFetch.apply(window, args);
              record('fetch-response', response.url, response.status);
              return response;
            }};
            window.open = (...args) => {{ record('window-open', args[0]); return null; }};
            HTMLAnchorElement.prototype.click = function() {{ record('anchor-click', this.href); }};
            button.click();
            const deadline = Date.now() + 8000;
            while (Date.now() < deadline) {{
              const completed = [...captured].reverse().find(item =>
                ['anchor-click', 'window-open'].includes(item.kind) && (
                  /[/]backend-api[/]estuary[/]content/i.test(item.url) ||
                  /[/]backend-api[/]files[/][^/]+[/](?:download|content)/i.test(item.url) ||
                  /[/]backend-api[/]files[/]download[/]/i.test(item.url)
                )
              );
              if (completed) return {{ok: true, url: completed.url}};
              await new Promise(resolve => setTimeout(resolve, 100));
            }}
            const signed = [...captured].reverse().find(item =>
              item.kind === 'fetch-response' && item.status >= 200 && item.status < 300 && (
                /[/]backend-api[/]estuary[/]content/i.test(item.url) ||
                /[/]backend-api[/]files[/][^/]+[/](?:download|content)/i.test(item.url) ||
                /[/]backend-api[/]files[/]download[/]/i.test(item.url)
              )
            );
            return signed ? {{ok: true, url: signed.url}} : {{ok: false, stage: 'capture', captured}};
          }} finally {{
            window.fetch = oldFetch;
            window.open = oldOpen;
            HTMLAnchorElement.prototype.click = oldAnchorClick;
          }}
        }}''', timeout=20)
        if not isinstance(payload, dict) or not payload.get('ok') or not payload.get('url'):
            raise StructuredError('CHAT_FILE_DOWNLOAD_URL_UNAVAILABLE', 'ChatGPT 文件卡没有返回可用的下载地址。')
        url = str(payload['url'])
        try:
            parsed = urlparse(url)
        except ValueError as error:
            raise StructuredError('CHAT_FILE_DOWNLOAD_URL_UNAVAILABLE', 'ChatGPT 文件下载地址无效。') from error
        path = parsed.path.lower()
        if parsed.scheme != 'https' or parsed.hostname not in CHATGPT_HOSTS or not (
            path.startswith('/backend-api/estuary/content')
            or re.match(r'^/backend-api/files/[^/]+/(?:download|content)/?$', path)
            or path.startswith('/backend-api/files/download/')
        ):
            raise StructuredError('CHAT_FILE_DOWNLOAD_URL_UNAVAILABLE', 'ChatGPT 文件下载地址不在允许范围。')
        return url

    def _stream_page_download_to_file(self, page_id, initialize_script, *, fallback_name, max_bytes=MAX_CHAT_DOWNLOAD_BYTES):
        """Stream a browser-authenticated response through bounded MCP chunks.

        The previous implementation returned the complete file as one Base64
        JSON value. A 50-60 MiB artifact could therefore require several
        hundred MiB across WebView2, Node/MCP, JSON and Python copies. This
        keeps browser authentication in-page but transfers only one network
        chunk at a time and writes it directly to CFR's delivery cache.
        """
        initial = self._evaluate(page_id, initialize_script, timeout=45)
        if not isinstance(initial, dict) or not initial.get('ok'):
            stage = initial.get('stage') if isinstance(initial, dict) else 'unknown'
            status = initial.get('status') if isinstance(initial, dict) else 'unknown'
            if stage == 'size':
                raise StructuredError('CHAT_FILE_TOO_LARGE', f'ChatGPT 文件超过 CFR 下载上限：{initial.get("size") or "unknown"} bytes')
            raise StructuredError('CHAT_FILE_DOWNLOAD_FAILED', f'ChatGPT 文件下载失败：{stage} HTTP {status}')
        token = str(initial.get('token') or '')
        if not token:
            raise StructuredError('CHAT_FILE_DOWNLOAD_FAILED', 'ChatGPT 文件下载流未建立。')
        destination = resolve_cfr_config_dir() / 'chatgpt' / 'downloads' / uuid.uuid4().hex
        destination.mkdir(parents=True, exist_ok=True)
        name = Path(str(initial.get('fileName') or fallback_name or 'chatgpt-file')).name
        name = re.sub(r'[^\w .()\-]+', '_', name, flags=re.UNICODE).strip(' .') or 'chatgpt-file'
        output_path = destination / name
        temp_path = destination / f'.{name}.{os.getpid()}.{threading.get_ident()}.tmp'
        token_json = json.dumps(token)
        total = 0
        try:
            with temp_path.open('wb') as handle:
                while True:
                    payload = self._evaluate(page_id, f'''async () => {{
                      const token = {token_json};
                      const registry = globalThis.__cfrDownloadStreams;
                      const state = registry?.get(token);
                      if (!state) return {{ok: false, stage: 'state'}};
                      const next = await state.reader.read();
                      if (next.done) {{
                        registry.delete(token);
                        return {{ok: true, done: true, total: state.total}};
                      }}
                      const bytes = next.value instanceof Uint8Array ? next.value : new Uint8Array(next.value || []);
                      state.total += bytes.byteLength;
                      if (state.total > {int(max_bytes)}) {{
                        try {{ await state.reader.cancel(); }} catch {{}}
                        registry.delete(token);
                        return {{ok: false, stage: 'size', size: state.total}};
                      }}
                      let binary = '';
                      for (let offset = 0; offset < bytes.length; offset += 32768) {{
                        binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
                      }}
                      return {{ok: true, done: false, size: bytes.length, total: state.total, base64: btoa(binary)}};
                    }}''', timeout=45)
                    if not isinstance(payload, dict) or not payload.get('ok'):
                        if isinstance(payload, dict) and payload.get('stage') == 'size':
                            raise StructuredError('CHAT_FILE_TOO_LARGE', f'ChatGPT 文件超过 CFR 下载上限：{payload.get("size") or "unknown"} bytes')
                        raise StructuredError('CHAT_FILE_DOWNLOAD_FAILED', f'ChatGPT 文件流读取失败：{payload.get("stage") if isinstance(payload, dict) else "unknown"}')
                    if payload.get('done'):
                        break
                    try:
                        chunk = base64.b64decode(str(payload.get('base64') or ''), validate=True)
                    except ValueError as error:
                        raise StructuredError('CHAT_FILE_DOWNLOAD_FAILED', 'ChatGPT 文件分块数据解码失败。') from error
                    if len(chunk) != int(payload.get('size') or -1):
                        raise StructuredError('CHAT_FILE_DOWNLOAD_FAILED', 'ChatGPT 文件分块大小校验失败。')
                    total += len(chunk)
                    if total > max_bytes:
                        raise StructuredError('CHAT_FILE_TOO_LARGE', f'ChatGPT 文件超过 CFR 下载上限：{total} bytes')
                    handle.write(chunk)
            expected = int(initial.get('declaredSize') or 0)
            if expected and not initial.get('contentEncoding') and total != expected:
                raise StructuredError('CHAT_FILE_DOWNLOAD_FAILED', f'ChatGPT 文件大小校验失败：expected {expected}, got {total}')
            os.replace(temp_path, output_path)
        except Exception:
            try:
                self._evaluate(page_id, f'''async () => {{
                  const token = {token_json};
                  const registry = globalThis.__cfrDownloadStreams;
                  const state = registry?.get(token);
                  if (state) {{ try {{ await state.reader.cancel(); }} catch {{}} registry.delete(token); }}
                  return true;
                }}''', timeout=5)
            except Exception:
                pass
            raise
        finally:
            temp_path.unlink(missing_ok=True)
        return {
            'path': str(output_path),
            'content_type': str(initial.get('contentType') or ''),
            'source': str(initial.get('source') or ''),
            'name': name,
            'size': total,
        }

    def download_generated_file_to_file(self, binding, file=None):
        page = self._page(binding, create=bool((binding or {}).get('url')))
        if page is None:
            raise StructuredError('CHAT_FILE_NOT_FOUND', '当前 ChatGPT Conversation 没有可下载的生成文件。')
        state = self._wait_ready(page['id'])
        identity = self.parse_identity(state.get('url'))
        if identity.get('conversation_id'):
            self._wait_history(page['id'], state)
        file = file or self._latest_generated_file(page['id'])
        if not file:
            raise StructuredError('CHAT_FILE_NOT_FOUND', '当前 ChatGPT Conversation 没有可下载的生成文件。')
        direct_url = str(file.get('href') or '').strip()
        if direct_url:
            try:
                parsed = urlparse(direct_url)
            except ValueError:
                direct_url = ''
            else:
                url_path = parsed.path.lower()
                known = (
                    url_path == '/backend-api/sandbox/download'
                    or re.match(r'^/backend-api/files/[^/]+/(?:download|content)/?$', url_path)
                    or url_path.startswith('/backend-api/estuary/content')
                    or url_path.startswith('/backend-api/files/download/')
                )
                if parsed.scheme != 'https' or parsed.hostname not in CHATGPT_HOSTS or not known:
                    direct_url = ''
        sandbox_url = str(file.get('sandbox_url') or '').strip()
        sandbox_path = ''
        if sandbox_url:
            try:
                parsed_sandbox = urlparse(sandbox_url)
            except ValueError:
                pass
            else:
                candidate = parsed_sandbox.path
                if (
                    parsed_sandbox.scheme == 'sandbox'
                    and candidate.startswith('/mnt/data/')
                    and '\\' not in candidate
                    and '\x00' not in candidate
                    and '..' not in Path(candidate).parts
                ):
                    sandbox_path = candidate
        file_id_value = str(file.get('file_id') or '')
        conversation_id_value = str(identity.get('conversation_id') or '')
        if not direct_url and not sandbox_path:
            try:
                direct_url = self._capture_generated_file_download_url(page['id'], file.get('name'))
            except StructuredError:
                if not file_id_value or not conversation_id_value:
                    raise
        if not direct_url and not sandbox_path and (not file_id_value or not conversation_id_value):
            raise StructuredError('CHAT_FILE_NOT_FOUND', '当前 ChatGPT Conversation 没有可下载的生成文件。')
        direct_url = json.dumps(direct_url or None)
        sandbox_path_json = json.dumps(sandbox_path or None)
        file_id = json.dumps(file_id_value or None)
        conversation_id = json.dumps(conversation_id_value or None)
        fallback_name = json.dumps(str(file.get('name') or ''))
        max_bytes = MAX_CHAT_DOWNLOAD_BYTES
        initialize_script = f'''async () => {{
          const directUrl = {direct_url};
          const sandboxPath = {sandbox_path_json};
          const fileId = {file_id};
          const conversationId = {conversation_id};
          const fallbackName = {fallback_name};
          let metadata = null;
          let downloadUrl = directUrl;
          if (!downloadUrl && sandboxPath) {{
            const sandboxUrl = new URL('/backend-api/sandbox/download', location.origin);
            sandboxUrl.searchParams.set('path', sandboxPath);
            downloadUrl = sandboxUrl.href;
          }}
          if (!downloadUrl) {{
            const metadataUrl = `/backend-api/files/download/${{encodeURIComponent(fileId)}}?conversation_id=${{encodeURIComponent(conversationId)}}&inline=false`;
            const metadataResponse = await fetch(metadataUrl, {{credentials: 'include'}});
            if (!metadataResponse.ok) return {{ok: false, stage: 'metadata', status: metadataResponse.status}};
            metadata = await metadataResponse.json().catch(() => null);
            downloadUrl = metadata?.download_url || metadata?.downloadUrl;
            if (!downloadUrl) return {{ok: false, stage: 'metadata', status: metadataResponse.status}};
          }}
          let response = await fetch(downloadUrl, {{credentials: 'include'}});
          if (response.ok && String(response.headers.get('content-type') || '').toLowerCase().includes('application/json')) {{
            const candidate = await response.clone().json().catch(() => null);
            const nestedUrl = candidate?.download_url || candidate?.downloadUrl;
            if (nestedUrl) {{
              metadata = candidate;
              downloadUrl = nestedUrl;
              response = await fetch(downloadUrl, {{credentials: 'include'}});
            }}
          }}
          if (!response.ok) return {{ok: false, stage: 'download', status: response.status}};
          const declared = Number(response.headers.get('content-length') || 0);
          if (declared > {max_bytes}) return {{ok: false, stage: 'size', size: declared}};
          if (!response.body) return {{ok: false, stage: 'stream', status: response.status}};
          const token = globalThis.crypto?.randomUUID?.() || `cfr-${{Date.now()}}-${{Math.random().toString(16).slice(2)}}`;
          globalThis.__cfrDownloadStreams ||= new Map();
          globalThis.__cfrDownloadStreams.set(token, {{reader: response.body.getReader(), total: 0}});
          return {{
            ok: true,
            token,
            contentType: response.headers.get('content-type') || '',
            contentEncoding: response.headers.get('content-encoding') || '',
            declaredSize: declared,
            fileName: metadata?.file_name || metadata?.fileName || fallbackName,
            source: downloadUrl,
          }};
        }}'''
        fallback_file_name = Path(sandbox_path).name if sandbox_path else (file_id_value or 'chatgpt-file')
        downloaded = self._stream_page_download_to_file(
            page['id'],
            initialize_script,
            fallback_name=file.get('name') or fallback_file_name,
            max_bytes=max_bytes,
        )
        return {**downloaded, 'file_id': file_id_value or None}

    def upload_file(self, binding, path):
        result = self.upload_files(binding, [path])
        return {**result, 'name': result['names'][0]}

    def upload_files(self, binding, paths):
        paths = self._upload_paths(paths)
        page = self._page(binding, create=True)
        self._restore_bound_url(page['id'], binding.get('url'))
        before = self._wait_ready(page['id'])
        if self.parse_identity(before.get('url')).get('conversation_id'):
            self._wait_history(page['id'], before)
        self._upload_files_on_page(page['id'], paths)
        current = self._state(page['id'])
        return {'tab_id': str(page['id']), 'url': current.get('url'), 'names': [path.name for path in paths]}

    @staticmethod
    def _upload_paths(paths):
        paths = [Path(path).expanduser().resolve(strict=True) for path in (paths or [])]
        if not paths or any(not path.is_file() for path in paths):
            raise StructuredError('CHAT_UPLOAD_FILE_REQUIRED', '需要上传一个或多个现有本地文件。')
        return paths

    def _upload_files_on_page(self, page_id, paths):
        self.mcp.set_roots([path.parent for path in paths])
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page_id}))
        plus = re.search(
            r'uid=(\S+)\s+button\s+"(?:添加文件等|Add files and more)"',
            snapshot,
            re.IGNORECASE,
        )
        if not plus:
            raise StructuredError('CHAT_UPLOAD_CONTROL_NOT_FOUND', '没有在 ChatGPT composer 找到原生文件上传入口。')
        opened = self._focus_and_enter(page_id, '''() => {
          const button = document.querySelector('[data-testid="composer-plus-btn"]') || Array.from(document.querySelectorAll('button')).find(node =>
            /^(?:添加文件等|Add files and more)$/i.test(String(node.getAttribute('aria-label') || node.textContent || '').trim())
          );
          if (!button) return false;
          button.focus();
          return document.activeElement === button;
        }''')
        if not opened:
            raise StructuredError('CHAT_UPLOAD_CONTROL_NOT_FOUND', 'ChatGPT 文件上传入口当前不可交互。')
        menu = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page_id}))
        chooser_uid = self._upload_menu_uid(menu)
        if not chooser_uid:
            raise StructuredError('CHAT_UPLOAD_CONTROL_NOT_FOUND', 'ChatGPT 附件菜单没有可用的“从电脑上传”入口。')
        self.mcp.tool(
            'upload_file',
            {'pageId': page_id, 'uid': chooser_uid, 'filePaths': [str(path) for path in paths]},
            timeout=max(self.mcp.timeout, 60),
        )
        self._wait_uploaded_files(page_id, [path.name for path in paths])

    @staticmethod
    def _upload_menu_uid(snapshot):
        labels = ('上传照片和文件', '从电脑上传', 'Upload photos and files', 'Upload from computer', 'Upload files')
        lines = str(snapshot or '').splitlines()
        for index, line in enumerate(lines):
            if 'StaticText' not in line or not any(label in line for label in labels):
                continue
            child_indent = len(line) - len(line.lstrip())
            for parent_line in reversed(lines[max(0, index - 3):index]):
                parent_indent = len(parent_line) - len(parent_line.lstrip())
                if parent_indent >= child_indent:
                    continue
                match = re.search(r'uid=(\S+)\s+(?:generic|button|menuitem)\b', parent_line, re.IGNORECASE)
                if match:
                    return match.group(1)
        return None

    def _wait_uploaded_files(self, page_id, names, timeout=60):
        deadline = time.monotonic() + timeout
        encoded_names = json.dumps(list(names), ensure_ascii=False)
        prompt = json.dumps(PROMPT_SELECTOR)
        ready_since = None
        while time.monotonic() < deadline:
            status = self._evaluate(page_id, f'''() => {{
              const names = {encoded_names};
              const visible = (n) => {{
                if (!n) return false;
                const r = n.getBoundingClientRect();
                const s = getComputedStyle(n);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
              }};
              const prompts = Array.from(document.querySelectorAll({prompt})).filter(visible);
              const composer = prompts[prompts.length - 1] || null;
              const form = composer?.closest('form') || null;
              if (!form) return {{missing: names, busy: false, errorText: ''}};
              const parts = [String(form.innerText || '')];
              for (const node of form.querySelectorAll('[aria-label], [title], [alt]')) {{
                parts.push(node.getAttribute('aria-label') || '', node.getAttribute('title') || '', node.getAttribute('alt') || '');
              }}
              const haystack = parts.join('\\n');
              const formText = String(form.innerText || '');
              const busy = /uploading|正在上传/i.test(formText) || Array.from(form.querySelectorAll(
                '[aria-busy="true"], progress, [data-state="loading"], [data-testid*="upload" i][data-state="loading"]'
              )).some(visible);
              const errorNodes = Array.from(document.querySelectorAll('[role="alert"], [data-testid*="toast" i], [data-testid*="error" i]')).filter(visible);
              const errorText = [formText, ...errorNodes.map(node => String(node.innerText || node.textContent || '').trim())].filter(text =>
                /upload failed|failed to upload|unsupported file|not supported|could(?: not|n't) upload|too large|maximum file size|上传失败|无法上传|不支持|文件过大/i.test(text)
              ).join('\\n');
              return {{missing: names.filter(name => !haystack.includes(name)), busy, errorText}};
            }}''')
            if not isinstance(status, dict):
                status = {'missing': list(names), 'busy': False, 'errorText': ''}
            error_text = str(status.get('errorText') or '').strip()
            if error_text:
                raise StructuredError('CHAT_UPLOAD_REJECTED', f'ChatGPT 拒绝附件上传：{error_text[:300]}')
            missing = status.get('missing') if isinstance(status.get('missing'), list) else list(names)
            if not missing and not status.get('busy'):
                if ready_since is None:
                    ready_since = time.monotonic()
                elif time.monotonic() - ready_since >= 1.0:
                    return
            else:
                ready_since = None
            time.sleep(0.25)
        raise StructuredError('CHAT_UPLOAD_VERIFY_FAILED', f'ChatGPT 未确认附件已就绪：{", ".join(missing)}')

    def _clear_stale_composer_attachments(self, page_id, timeout=5):
        """Remove leftover attachment chips from a previous failed CFR send."""
        deadline = time.monotonic() + timeout
        prompt = json.dumps(PROMPT_SELECTOR)
        removed = 0
        while time.monotonic() < deadline:
            result = self._evaluate(page_id, f'''() => {{
              const visible = (n) => {{
                if (!n) return false;
                const r = n.getBoundingClientRect();
                const s = getComputedStyle(n);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
              }};
              const prompts = Array.from(document.querySelectorAll({prompt})).filter(visible);
              const composer = prompts[prompts.length - 1] || null;
              const form = composer?.closest('form') || null;
              if (!form) return {{remaining: 0, clicked: 0}};
              const candidates = Array.from(form.querySelectorAll('button, [role="button"]'));
              const removers = candidates.filter(node => {{
                const label = [
                  node.getAttribute('aria-label') || '', node.getAttribute('title') || '',
                  node.getAttribute('data-testid') || '', node.textContent || ''
                ].join(' ').trim();
                return visible(node) && (
                  /(?:remove|delete).*(?:file|attachment|upload|image)|(?:file|attachment|upload|image).*(?:remove|delete)/i.test(label)
                  || /(?:移除|删除|取消).*(?:文件|附件|图片|上传)|(?:文件|附件|图片|上传).*(?:移除|删除|取消)/.test(label)
                );
              }});
              for (const node of removers) node.click();
              return {{remaining: removers.length, clicked: removers.length}};
            }}''')
            if not isinstance(result, dict):
                return removed
            clicked = int(result.get('clicked') or 0)
            removed += clicked
            if clicked == 0:
                return removed
            time.sleep(0.15)
        raise StructuredError('CHAT_STALE_ATTACHMENT_CLEAR_TIMEOUT', 'ChatGPT composer 中残留附件未能在发送前清理。')

    def _wait_send_enabled(self, page_id, timeout=10):
        deadline = time.monotonic() + timeout
        send = json.dumps(SEND_SELECTOR)
        while time.monotonic() < deadline:
            enabled = self._evaluate(page_id, f'''() => {{
              const visible = (n) => {{
                if (!n) return false;
                const r = n.getBoundingClientRect();
                const s = getComputedStyle(n);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
              }};
              return Array.from(document.querySelectorAll({send})).some(n =>
                visible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true'
              );
            }}''')
            if enabled:
                return
            state = self._state(page_id)
            if state.get('blocked'):
                self._require_ready(state)
            time.sleep(0.15)
        raise StructuredError('CHAT_SEND_NOT_READY', 'ChatGPT 发送按钮在输入完成后仍不可用；未发送消息。')

    @staticmethod
    def _progress(callback, **values):
        if callback is not None:
            try:
                callback(values)
            except Exception:
                pass

    def _focus_and_enter(self, page_id, focus_script, *, timeout=None):
        """Activate a WebView2 control through the real keyboard path.

        chrome-devtools-mcp's click tool can stall against hidden WebView2 targets,
        while focus + Enter uses the same keyboard activation path as a user.
        """
        configured_timeout = getattr(self.mcp, 'timeout', 20)
        if not isinstance(configured_timeout, (int, float)):
            configured_timeout = 20
        timeout = timeout or max(configured_timeout, 20)
        focused = self._evaluate(page_id, focus_script, timeout=timeout)
        if not focused:
            return False
        self.mcp.tool('press_key', {'pageId': page_id, 'key': 'Enter'}, timeout=timeout)
        return True

    def _send_message(self, binding, prompt, *, tool_labels=None, on_progress=None, file_paths=None):
        prompt = str(prompt or '').strip()
        if not prompt:
            raise StructuredError('CHAT_PROMPT_REQUIRED', 'Chat Surface 需要非空消息。')
        if len(prompt) > 200_000:
            raise StructuredError('CHAT_PROMPT_TOO_LARGE', 'Chat Surface 单条消息过长。')
        trace_started = time.monotonic()

        def trace(event, owner, started, detail=None):
            now = time.monotonic()
            payload = {
                'event': event,
                'owner': owner,
                'duration_ms': round(max(0.0, now - started) * 1000, 1),
                'elapsed_ms': round(max(0.0, now - trace_started) * 1000, 1),
                'at': time.time(),
            }
            if detail:
                payload['detail'] = str(detail)
            self._progress(on_progress, trace=payload)
            return now

        def milestone(event, owner):
            now = time.monotonic()
            self._progress(on_progress, trace={
                'event': event,
                'owner': owner,
                'duration_ms': 0.0,
                'elapsed_ms': round(max(0.0, now - trace_started) * 1000, 1),
                'at': time.time(),
            })

        phase = time.monotonic()
        page = self._page(binding, create=True)
        phase = trace('browser_page_resolved', 'browser', phase, page.get('url'))
        self._restore_bound_url(page['id'], binding.get('url'))
        before = self._wait_ready(page['id'])
        phase = trace('page_ready', 'browser/openai', phase, before.get('url'))
        if self.parse_identity(before.get('url')).get('conversation_id'):
            before = self._wait_history(page['id'], before)
            phase = trace('history_hydrated', 'browser/openai', phase)
        if 'generatedFileKey' not in before:
            try:
                before['generatedFileKey'] = self._generated_files_key(self._generated_files(page['id']))
            except Exception:
                before['generatedFileKey'] = ''
        if file_paths:
            stale_started = time.monotonic()
            stale_count = self._clear_stale_composer_attachments(page['id'])
            if stale_count:
                phase = trace('stale_composer_attachments_cleared', 'browser', stale_started, f'{stale_count} attachment(s)')
            self._upload_files_on_page(page['id'], self._upload_paths(file_paths))
            phase = trace('input_files_uploaded', 'browser/openai', phase, f'{len(file_paths)} files')
        prompt_uid = self._prompt_uid(page['id'])
        self.mcp.tool('fill', {'pageId': page['id'], 'uid': prompt_uid, 'value': prompt})
        if tool_labels:
            self._select_tool(page['id'], tool_labels)
        self._wait_send_enabled(page['id'])
        phase = trace('composer_ready', 'browser', phase)
        self.mcp.tool('press_key', {'pageId': page['id'], 'key': 'Enter'})
        started = self._wait_send_started(page['id'], before, timeout=8)
        if not started:
            clicked = self._focus_and_enter(page['id'], self._click_send_script())
            if clicked:
                started = self._wait_send_started(page['id'], before, timeout=5)
        if not started:
            raise StructuredError('CHAT_SEND_NOT_TRIGGERED', '未能确认 ChatGPT 网页已发送消息。')
        phase = trace('send_confirmed', 'browser/openai', phase)
        self._progress(on_progress, state='generating', reasoning_text='', answer_preview='')
        artifact_expected = bool(re.search(
            r'图片|图像|照片|表格|文件|附件|下载|xlsx|xls|csv|docx|pdf|pptx|zip|'
            r'image|photo|picture|spreadsheet|workbook|file|download|attachment',
            prompt,
            re.IGNORECASE,
        ))
        first_reasoning = False
        first_output = False

        def stream_progress(value):
            nonlocal first_reasoning, first_output
            if not first_reasoning and str((value or {}).get('reasoning_text') or '').strip():
                first_reasoning = True
                milestone('first_reasoning_visible', 'openai')
            if not first_output and str((value or {}).get('answer_preview') or '').strip():
                first_output = True
                milestone('first_output_visible', 'openai')
            if on_progress is not None:
                on_progress(value)

        response_started = time.monotonic()
        answer = self._wait_answer(
            page['id'], before, on_progress=stream_progress,
            settle_seconds=3.0 if artifact_expected else 1.5,
        )
        phase = trace('response_complete', 'openai', response_started)
        self._progress(on_progress, state='completed', reasoning_text=answer.get('reasoning_text') or '', answer_preview=answer['text'])
        generated_image = self._latest_generated_image(page['id']) if answer.get('generated_image_src') else None
        generated_files = []
        artifact_started = time.monotonic()
        visible_file_hint = bool(re.search(r'\.(?:xlsx|xls|csv|docx|pptx|pdf|zip|7z|tar|gz|txt|md|json|svg)\b', answer.get('text') or '', re.IGNORECASE))
        artifact_deadline = artifact_started + (12 if answer.get('generated_file_key') or visible_file_hint else 5 if artifact_expected else 0)
        while artifact_deadline and time.monotonic() < artifact_deadline:
            try:
                generated_files = self._generated_files(page['id'])
            except Exception:
                generated_files = []
            if generated_files:
                break
            time.sleep(0.35)
        trace(
            'artifact_discovery_complete', 'browser/openai', artifact_started,
            f'{len(generated_files)} file(s), image={bool(generated_image)}',
        )
        return {
            'text': answer['text'],
            'tab_id': str(page['id']),
            'url': answer.get('url'),
            'generated_image_src': answer.get('generated_image_src'),
            'generated_image': generated_image,
            'generated_file': generated_files[-1] if generated_files else None,
            'generated_files': generated_files,
            'blocked': False,
        }

    @staticmethod
    def _generated_file_key(file):
        if not isinstance(file, dict):
            return ''
        return next(
            (str(file.get(key) or '') for key in ('file_id', 'sandbox_url', 'href', 'name') if file.get(key)),
            '',
        )

    @classmethod
    def _generated_files_key(cls, files):
        return '||'.join(sorted(filter(None, (cls._generated_file_key(file) for file in files or ()))))

    def _select_tool(self, page_id, labels):
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page_id}))
        plus = re.search(r'uid=(\S+)\s+button\s+"(?:添加文件等|Add files and more)"', snapshot, re.IGNORECASE)
        if not plus:
            raise StructuredError('CHAT_TOOL_MENU_NOT_FOUND', '没有在 ChatGPT composer 找到工具菜单。')
        opened = self._focus_and_enter(page_id, '''() => {
          const button = document.querySelector('[data-testid="composer-plus-btn"]') || Array.from(document.querySelectorAll('button')).find(node =>
            /^(?:添加文件等|Add files and more)$/i.test(String(node.getAttribute('aria-label') || node.textContent || '').trim())
          );
          if (!button) return false;
          button.focus();
          return document.activeElement === button;
        }''')
        if not opened:
            raise StructuredError('CHAT_TOOL_MENU_NOT_FOUND', 'ChatGPT 工具菜单当前不可交互。')
        encoded = json.dumps(list(labels), ensure_ascii=False)
        selected = self._focus_and_enter(page_id, f'''() => {{
          const labels = {encoded};
          const node = Array.from(document.querySelectorAll('span')).find(n => labels.includes(String(n.textContent || '').trim()));
          let item = node;
          while (item && item !== document.body && item.getAttribute('tabindex') !== '0') item = item.parentElement;
          if (!item || item === document.body) return false;
          item.focus();
          return document.activeElement === item;
        }}''')
        if not selected:
            raise StructuredError('CHAT_TOOL_NOT_FOUND', f'ChatGPT 当前页面没有可用工具：{labels[0]}')
        verified = self._evaluate(page_id, f'''() => Array.from(document.querySelectorAll('[role="textbox"] span')).some(n =>
          {encoded}.includes(String(n.textContent || '').trim()) && String(n.className || '').includes('inline-selection-pill')
        )''')
        if not verified:
            raise StructuredError('CHAT_TOOL_VERIFY_FAILED', f'ChatGPT 工具选择状态未通过校验：{labels[0]}')

    @staticmethod
    def _parse_reasoning_power_snapshot(snapshot):
        match = re.search(
            r'uid=(\S+)\s+menuitem\s+"Power"[^\n]*description="([^\"]+)"',
            str(snapshot or ''),
            re.IGNORECASE,
        )
        if not match:
            return None
        description = match.group(2)
        value = re.search(r'^(.+?),\s*(\d+)\s+of\s+(\d+)\.', description, re.IGNORECASE)
        if not value:
            return None
        return {
            'uid': match.group(1),
            'label': value.group(1).strip(),
            'index': int(value.group(2)),
            'total': int(value.group(3)),
        }

    def _reasoning_power_dom_state(self, page_id, *, timeout=None):
        """Read the native Power slider when the accessibility snapshot is terse.

        chrome-use intentionally emits a compact accessibility tree and omits
        the ``aria-describedby`` text that Chrome DevTools MCP used to include.
        ChatGPT still exposes the same screen-reader description in the DOM, so
        use that as the authoritative fallback instead of hard-coding slider
        positions or product-tier labels.
        """
        value = self._evaluate(page_id, '''() => {
          const item = document.querySelector('[role="menuitem"][aria-label="Power"]');
          if (!item) return null;
          const ids = String(item.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean);
          const description = ids.map(id => String(document.getElementById(id)?.textContent || '').trim()).filter(Boolean).join(' ');
          const slider = item.querySelector('[role="slider"]');
          return {
            description,
            now: slider?.getAttribute('aria-valuenow') || null,
            min: slider?.getAttribute('aria-valuemin') || null,
            max: slider?.getAttribute('aria-valuemax') || null,
          };
        }''', timeout=timeout)
        if not isinstance(value, dict):
            return None
        description = str(value.get('description') or '')
        match = re.search(r'(^|\s)(.+?),\s*(\d+)\s+of\s+(\d+)\.', description, re.IGNORECASE)
        if match:
            return {
                'uid': None,
                'label': match.group(2).strip(),
                'index': int(match.group(3)),
                'total': int(match.group(4)),
            }
        try:
            current = int(value.get('now'))
            minimum = int(value.get('min'))
            maximum = int(value.get('max'))
        except (TypeError, ValueError):
            return None
        total = maximum - minimum + 1
        index = current - minimum + 1
        labels = ('Instant', 'Medium', 'High', 'Extra High')
        label = labels[index - 1] if total == len(labels) and 1 <= index <= len(labels) else str(index)
        return {'uid': None, 'label': label, 'index': index, 'total': total}

    def _open_thinking_effort_menu(self, page_id):
        """Open the composer effort menu without relying on transient snapshot UIDs."""
        timeout = max(self.mcp.timeout, 20)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page_id}, timeout=timeout))
            if self._parse_reasoning_power_snapshot(snapshot) or re.search(
                r'menuitem\s+"(?:Select model|选择模型)"', snapshot, re.IGNORECASE,
            ):
                return snapshot
            opened = self._focus_and_enter(page_id, '''() => {
              const exact = new Set(['instant', 'medium', 'high', 'extra high', 'thinking effort']);
              const visible = node => {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const buttons = Array.from(document.querySelectorAll('button')).filter(visible);
              const button = buttons.find(node => {
                const values = [node.textContent, node.getAttribute('aria-label'), node.getAttribute('title')]
                  .map(value => String(value || '').trim().toLowerCase())
                  .filter(Boolean);
                return values.some(value => exact.has(value) || value.includes('thinking effort'));
              }) || buttons.find(node =>
                node.getAttribute('aria-haspopup') === 'menu'
                && String(node.className || '').includes('__composer-pill')
                && String(node.textContent || '').trim()
              );
              if (!button) return false;
              button.focus();
              return document.activeElement === button;
            }''', timeout=timeout)
            if not opened:
                time.sleep(0.2)
                continue
            menu_deadline = min(deadline, time.monotonic() + 4)
            while time.monotonic() < menu_deadline:
                snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page_id}, timeout=timeout))
                if self._parse_reasoning_power_snapshot(snapshot) or re.search(
                    r'menuitem\s+"(?:Select model|选择模型)"', snapshot, re.IGNORECASE,
                ):
                    return snapshot
                time.sleep(0.15)
            return snapshot
        raise StructuredError(
            'CHAT_REASONING_CONTROL_UNAVAILABLE',
            '当前 ChatGPT composer 没有可用的 Thinking effort 入口。',
        )

    def _open_reasoning_power(self, page_id):
        timeout = max(self.mcp.timeout, 20)
        snapshot = self._open_thinking_effort_menu(page_id)
        state = self._parse_reasoning_power_snapshot(snapshot) or self._reasoning_power_dom_state(page_id, timeout=timeout)
        if state is None:
            raise StructuredError(
                'CHAT_REASONING_CONTROL_UNAVAILABLE',
                'ChatGPT Thinking effort 菜单已打开，但没有找到 Power 控件。',
            )
        focused = self._evaluate(page_id, '''() => {
          const item = document.querySelector('[role="menuitem"][aria-label="Power"]');
          if (!item) return false;
          item.focus();
          return document.activeElement === item;
        }''', timeout=timeout)
        if not focused:
            raise StructuredError('CHAT_REASONING_FOCUS_FAILED', '无法聚焦 ChatGPT Thinking effort 控件。')
        return state

    def _control_with_mcp_retry(self, operation):
        try:
            return operation()
        except StructuredError as error:
            if error.code != 'CHAT_BROWSER_MCP_TIMEOUT':
                raise
            self.mcp.close()
            self.mcp.start()
            return operation()

    def _reasoning_effort_once(self, binding=None):
        page = self._page(binding, create=True)
        self._restore_bound_url(page['id'], (binding or {}).get('url'))
        self._wait_ready(page['id'])
        try:
            state = self._open_reasoning_power(page['id'])
            return {
                **state,
                'tab_id': str(page['id']),
                'url': self._state(page['id']).get('url') or page['url'],
            }
        finally:
            try:
                self.mcp.tool('press_key', {'pageId': page['id'], 'key': 'Escape'}, timeout=max(self.mcp.timeout, 20))
            except StructuredError:
                pass

    def reasoning_effort(self, binding=None):
        return self._control_with_mcp_retry(lambda: self._reasoning_effort_once(binding))

    def _set_reasoning_effort_once(self, binding, value):
        page = self._page(binding, create=True)
        self._restore_bound_url(page['id'], (binding or {}).get('url'))
        self._wait_ready(page['id'])
        aliases = {
            'instant': 1, '即时': 1,
            'medium': 2, '中': 2, '中等': 2,
            'high': 3, '高': 3,
            'extra high': 4, 'extra-high': 4, 'extrahigh': 4, 'max': 4, '最高': 4,
        }
        try:
            state = self._open_reasoning_power(page['id'])
            requested = str(value or '').strip().lower()
            target = int(requested) if requested.isdigit() else aliases.get(requested)
            if target is None or not 1 <= target <= state['total']:
                raise StructuredError(
                    'CHAT_REASONING_VALUE_INVALID',
                    f'ChatGPT 当前 Thinking effort 有 {state["total"]} 档；请使用 1-{state["total"]} 或 Instant / Medium / High / Extra High。',
                )
            key = 'ArrowRight' if target > state['index'] else 'ArrowLeft'
            for _ in range(abs(target - state['index'])):
                self.mcp.tool('press_key', {'pageId': page['id'], 'key': key}, timeout=max(self.mcp.timeout, 20))
            snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}, timeout=max(self.mcp.timeout, 20)))
            final = self._parse_reasoning_power_snapshot(snapshot) or self._reasoning_power_dom_state(
                page['id'], timeout=max(self.mcp.timeout, 20)
            )
            if final is None or final['index'] != target:
                raise StructuredError('CHAT_REASONING_VERIFY_FAILED', 'ChatGPT Thinking effort 修改后未通过状态校验。')
            return {
                **final,
                'tab_id': str(page['id']),
                'url': self._state(page['id']).get('url') or page['url'],
            }
        finally:
            try:
                self.mcp.tool('press_key', {'pageId': page['id'], 'key': 'Escape'}, timeout=max(self.mcp.timeout, 20))
            except StructuredError:
                pass

    def set_reasoning_effort(self, binding, value):
        return self._control_with_mcp_retry(lambda: self._set_reasoning_effort_once(binding, value))

    @staticmethod
    def _parse_model_menu_snapshot(snapshot):
        models = []
        for line in str(snapshot or '').splitlines():
            match = re.search(r'uid=(\S+)\s+menuitemradio\s+"([^"]+)"(.*)', line, re.IGNORECASE)
            if not match:
                continue
            detail = match.group(3).lower()
            models.append({
                'uid': match.group(1),
                'name': match.group(2).strip(),
                'selected': ' checked' in f' {detail}',
                'disabled': ' disabled' in f' {detail}',
            })
        return models

    def _open_model_menu(self, page_id):
        timeout = max(self.mcp.timeout, 20)
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page_id}, timeout=timeout))
        models = self._parse_model_menu_snapshot(snapshot)
        if models:
            return models
        menu = self._open_thinking_effort_menu(page_id)
        select_model = re.search(r'uid=(\S+)\s+menuitem\s+"(?:Select model|选择模型)"', menu, re.IGNORECASE)
        if not select_model:
            raise StructuredError('CHAT_MODEL_CONTROL_UNAVAILABLE', 'ChatGPT Thinking effort 菜单没有 Select model 入口。')
        opened = self._focus_and_enter(page_id, '''() => {
          const item = Array.from(document.querySelectorAll('[role="menuitem"]')).find(node =>
            ['Select model', '选择模型'].includes(String(node.getAttribute('aria-label') || node.textContent || '').trim())
          );
          if (!item) return false;
          item.focus();
          return document.activeElement === item;
        }''', timeout=timeout)
        if not opened:
            raise StructuredError('CHAT_MODEL_CONTROL_UNAVAILABLE', 'ChatGPT Select model 入口当前不可交互。')
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page_id}, timeout=timeout))
        models = self._parse_model_menu_snapshot(snapshot)
        if not models:
            raise StructuredError('CHAT_MODEL_CONTROL_UNAVAILABLE', 'ChatGPT 当前没有可读取的模型列表。')
        return models

    def _models_once(self, binding=None):
        page = self._page(binding, create=True)
        self._restore_bound_url(page['id'], (binding or {}).get('url'))
        self._wait_ready(page['id'])
        try:
            models = self._open_model_menu(page['id'])
            return {
                'models': [{k: item[k] for k in ('name', 'selected', 'disabled')} for item in models],
                'current': next((item['name'] for item in models if item['selected']), None),
                'tab_id': str(page['id']),
                'url': self._state(page['id']).get('url') or page['url'],
            }
        finally:
            try:
                self.mcp.tool('press_key', {'pageId': page['id'], 'key': 'Escape'}, timeout=max(self.mcp.timeout, 20))
                self.mcp.tool('press_key', {'pageId': page['id'], 'key': 'Escape'}, timeout=max(self.mcp.timeout, 20))
            except StructuredError:
                pass

    def models(self, binding=None):
        return self._control_with_mcp_retry(lambda: self._models_once(binding))

    def _set_model_once(self, binding, value):
        page = self._page(binding, create=True)
        self._restore_bound_url(page['id'], (binding or {}).get('url'))
        self._wait_ready(page['id'])
        try:
            models = self._open_model_menu(page['id'])
            requested = str(value or '').strip()
            target = None
            if requested.isdigit() and 1 <= int(requested) <= len(models):
                target = models[int(requested) - 1]
            else:
                matches = [item for item in models if item['name'].casefold() == requested.casefold()]
                if len(matches) == 1:
                    target = matches[0]
            if target is None:
                raise StructuredError('CHAT_MODEL_NOT_FOUND', f'没有唯一匹配的 ChatGPT 模型：{requested}')
            if target['disabled']:
                raise StructuredError('CHAT_MODEL_UNAVAILABLE', f'当前账号不可选择 ChatGPT 模型：{target["name"]}')
            if not target['selected']:
                selected = self._focus_and_enter(page['id'], f'''() => {{
                  const target = {json.dumps(target['name'], ensure_ascii=False)};
                  const item = Array.from(document.querySelectorAll('[role="menuitemradio"]')).find(node =>
                    String(node.getAttribute('aria-label') || node.textContent || '').trim() === target
                  );
                  if (!item || item.getAttribute('aria-disabled') === 'true') return false;
                  item.focus();
                  return document.activeElement === item;
                }}''', timeout=max(self.mcp.timeout, 20))
                if not selected:
                    raise StructuredError('CHAT_MODEL_UNAVAILABLE', f'当前无法选择 ChatGPT 模型：{target["name"]}')
            verified = self._open_model_menu(page['id'])
            current = next((item for item in verified if item['selected']), None)
            if current is None or current['name'].casefold() != target['name'].casefold():
                raise StructuredError('CHAT_MODEL_VERIFY_FAILED', 'ChatGPT 模型修改后未通过状态校验。')
            return {
                'current': current['name'],
                'models': [{k: item[k] for k in ('name', 'selected', 'disabled')} for item in verified],
                'tab_id': str(page['id']),
                'url': self._state(page['id']).get('url') or page['url'],
            }
        finally:
            try:
                self.mcp.tool('press_key', {'pageId': page['id'], 'key': 'Escape'}, timeout=max(self.mcp.timeout, 20))
                self.mcp.tool('press_key', {'pageId': page['id'], 'key': 'Escape'}, timeout=max(self.mcp.timeout, 20))
            except StructuredError:
                pass

    def set_model(self, binding, value):
        return self._control_with_mcp_retry(lambda: self._set_model_once(binding, value))

    @staticmethod
    def _parse_deep_research_snapshot(snapshot):
        marker = 'Iframe "internal://deep-research"'
        index = str(snapshot or '').rfind(marker)
        if index < 0:
            return {'state': 'none', 'report': None}
        chunk = str(snapshot)[index:]
        if not re.search(r'研究完成情况|Research (?:complete|completed)', chunk, re.IGNORECASE):
            return {'state': 'running', 'report': None}
        report = None
        for line in chunk.splitlines():
            match = re.search(r'\bbutton\s+"(.*)"\s*$', line)
            if not match:
                continue
            value = match.group(1).strip()
            if value.lower() in {'导出', '展开', 'export', 'expand'} or len(value) < 80:
                continue
            report = value
            break
        return {'state': 'completed', 'report': report}

    @staticmethod
    def _image_request_count(snapshot):
        text = str(snapshot or '')
        return text.count('StaticText "创建图片"') + text.count('StaticText "Create image"')

    @staticmethod
    def _parse_image_snapshot(snapshot):
        text = str(snapshot or '')
        index = max(text.rfind('StaticText "创建图片"'), text.rfind('StaticText "Create image"'))
        if index < 0:
            return 'none'
        chunk = text[index:]
        if re.search(r'已生成图片|Generated image', chunk, re.IGNORECASE):
            return 'completed'
        if re.search(r'正在生成|Generating|停止回答|Stop responding', chunk, re.IGNORECASE):
            return 'running'
        return 'submitted'

    def _latest_generated_image(self, page_id):
        value = self._evaluate(page_id, '''() => {
          const turns = Array.from(document.querySelectorAll('section')).filter(x => x.getAttribute('data-turn') === 'assistant').reverse();
          for (const turn of turns) {
            const images = Array.from(turn.querySelectorAll('img')).filter(img =>
              /已生成图片|generated image/i.test(String(img.alt || '')) || String(img.src || '').includes('/backend-api/estuary/content')
            );
            if (!images.length) continue;
            const image = images[0];
            return {src: image.src, alt: image.alt || '', width: image.naturalWidth || null, height: image.naturalHeight || null};
          }
          return null;
        }''')
        return value if isinstance(value, dict) else None

    def _generated_files(self, page_id):
        value = self._evaluate(page_id, '''() => {
          const rawTurns = Array.from(document.querySelectorAll(
            'section[data-turn="assistant"], [data-message-author-role="assistant"], [data-testid*="assistant" i]'
          ));
          const turns = [];
          for (const node of rawTurns) {
            const root = node.matches('section[data-turn="assistant"]') ? node : (node.closest('article, section') || node);
            if (!turns.includes(root)) turns.push(root);
          }
          const safeSandbox = (value) => {
            const raw = String(value || '').trim();
            if (!raw.startsWith('sandbox:/mnt/data/')) return '';
            try {
              const path = decodeURI(new URL(raw).pathname);
              if (!path.startsWith('/mnt/data/') || path.includes('\\\\') || path.includes('\\0') || path.split('/').includes('..')) return '';
              return `sandbox:${path}`;
            } catch { return ''; }
          };
          const safeBackend = (value) => {
            const raw = String(value || '').trim();
            if (!raw || raw.startsWith('sandbox:') || raw.startsWith('blob:')) return '';
            try {
              const url = new URL(raw, location.origin);
              if (!['chatgpt.com', 'chat.openai.com'].includes(url.hostname.toLowerCase()) || url.protocol !== 'https:' || url.port) return '';
              const path = url.pathname.toLowerCase();
              const known = path === '/backend-api/sandbox/download'
                || new RegExp('^/backend-api/files/[^/]+/(?:download|content)/?$').test(path)
                || path === '/backend-api/estuary/content'
                || path.startsWith('/backend-api/files/download/');
              return known ? url.href : '';
            } catch { return ''; }
          };
          const valuesFor = (node) => {
            const values = [];
            if (node.tagName?.toLowerCase() === 'a') values.push(node.getAttribute('href') || '', node.href || '', node.getAttribute('download') || '');
            for (const attr of Array.from(node.attributes || [])) values.push(String(attr.value || ''));
            for (const child of Array.from(node.querySelectorAll?.('*') || []).slice(0, 500)) {
              if (child.tagName?.toLowerCase() === 'a') values.push(child.getAttribute('href') || '', child.href || '', child.getAttribute('download') || '');
              for (const attr of Array.from(child.attributes || [])) values.push(String(attr.value || ''));
            }
            if (String(node.outerHTML || '').length <= 200000) values.push(String(node.outerHTML || ''));
            return values.filter(Boolean).map(String);
          };
          const sandboxFrom = (value) => {
            const match = String(value || '').match(/sandbox:[/]mnt[/]data[/][^\\s"'<>]+/i);
            return match ? safeSandbox(match[0].replaceAll('&amp;', '&')) : '';
          };
          const backendFrom = (value) => {
            const raw = String(value || '').replaceAll('&amp;', '&');
            const match = raw.match(/(?:https:[/][/]chatgpt[.]com)?[/]backend-api[/](?:sandbox[/]download|estuary[/]content|files[/]download[/]|files[/][^/]+[/](?:download|content))[^ "'<>]*/i);
            return match ? safeBackend(match[0]) : '';
          };
          const fileNameFrom = (label) => {
            const normalized = String(label || '')
              .replaceAll(String.fromCharCode(9), ' ')
              .replaceAll(String.fromCharCode(10), ' ')
              .replaceAll(String.fromCharCode(13), ' ')
              .trim();
            const lower = normalized.toLowerCase();
            for (const extension of ['.xlsx', '.xls', '.csv', '.docx', '.pptx', '.pdf', '.zip', '.7z', '.tar', '.gz', '.txt', '.md', '.json', '.svg']) {
              const index = lower.indexOf(extension);
              if (index < 0) continue;
              const prefix = normalized.slice(0, index + extension.length).trim();
              return prefix.split(' ').pop() || prefix;
            }
            return normalized;
          };
          for (const turn of turns.reverse()) {
            const results = [];
            const seen = new Set();
            const nodes = Array.from(turn.querySelectorAll(
              'a[href], a[download], button, [role="button"], [data-testid], [aria-label], [title], [data-file-id], [data-asset-pointer], [data-filename]'
            ));
            for (const node of nodes) {
              const generatedImage = Array.from(node.querySelectorAll?.('img') || []).some(img =>
                /已生成图片|generated image/i.test(String(img.alt || '')) || String(img.src || '').includes('/backend-api/estuary/content')
              );
              const values = valuesFor(node);
              const href = values.map(safeBackend).find(Boolean) || values.map(backendFrom).find(Boolean) || '';
              const sandboxUrl = values.map(safeSandbox).find(Boolean) || values.map(sandboxFrom).find(Boolean) || '';
              const match = values.join(' ').match(/file[_-][A-Za-z0-9-]{16,}/);
              const label = String(node.getAttribute('data-filename') || node.getAttribute('download') || node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent || '').trim();
              const explicitFileName = /[.](?:xlsx|xls|csv|docx|pptx|pdf|zip|7z|tar|gz|txt|md|json|svg)(?:$|[^A-Za-z0-9_])/i.test(label);
              const nonImageBackend = href && !String(href).includes('/backend-api/estuary/content');
              let downloadableCard = false;
              if (explicitFileName) {
                let root = node;
                for (let depth = 0; root && depth < 8; depth++, root = root.parentElement) {
                  if (root.querySelector?.('button[aria-label="Download file"], button[aria-label="下载文件"]')) {
                    downloadableCard = true;
                    break;
                  }
                }
              }
              if (generatedImage && !explicitFileName && !sandboxUrl && !match && !nonImageBackend) continue;
              if (!href && !sandboxUrl && !match && !downloadableCard) continue;
              const item = {file_id: match ? match[0] : null, name: fileNameFrom(label), href, sandbox_url: sandboxUrl};
              const key = item.file_id || item.sandbox_url || item.href || item.name || '';
              if (seen.has(key)) continue;
              seen.add(key);
              results.push(item);
            }
            if (results.length) return results;
          }
          return [];
        }''')
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def _latest_generated_file(self, page_id):
        files = self._generated_files(page_id)
        return files[-1] if files else None

    def new_conversation(self, binding):
        page = self._page(binding, create=True)
        identity = self.parse_identity(binding.get('url'))
        url = f'https://chatgpt.com/g/{identity["project_id"]}/project' if identity.get('project_id') else 'https://chatgpt.com/'
        return self.navigate({'tab_id': str(page['id'])}, url)

    def navigate(self, binding, url):
        if not self._is_chatgpt_url(url):
            raise StructuredError('CHAT_NAVIGATION_REJECTED', 'Chat Surface 只允许导航到 ChatGPT 官方网页。')
        page = self._page(binding, create=True)
        self.mcp.tool('navigate_page', {'pageId': page['id'], 'type': 'url', 'url': url, 'timeout': 15_000}, timeout=20)
        pages = self._pages()
        current_page = next((item for item in pages if item['id'] == page['id'] and self._is_chatgpt_url(item['url'])), None)
        if current_page is None:
            current_page = next((item for item in pages if item['selected'] and self._is_chatgpt_url(item['url'])), None)
        if current_page is None:
            current_page = next((item for item in pages if self._same_navigation_target(item['url'], url)), None)
        if current_page is None or not self._is_chatgpt_url(current_page['url']):
            raise StructuredError('CHAT_NAVIGATION_VERIFY_FAILED', 'ChatGPT 网页导航后状态校验失败。')
        return {'tab_id': str(current_page['id']), 'url': current_page['url'], **self.parse_identity(current_page['url'])}

    def current_identity(self, binding=None):
        page = self._page(binding, create=False)
        if page is None:
            return {'tab_id': None, 'url': None, 'project_id': None, 'conversation_id': None}
        return {'tab_id': str(page['id']), 'url': page['url'], **self.parse_identity(page['url'])}

    def list_projects(self):
        embedded = getattr(self.mcp, 'mode', 'dedicated') == 'embedded'
        original = next((item for item in self._pages() if self._is_chatgpt_url(item['url'])), None) if embedded else None
        page = self._new_page('https://chatgpt.com/projects', background=True)
        try:
            return self._wait_project_catalog(page['id'])
        finally:
            if embedded:
                if original and self._is_chatgpt_url(original.get('url')):
                    try:
                        self.mcp.tool(
                            'navigate_page',
                            {'pageId': page['id'], 'type': 'url', 'url': original['url'], 'timeout': 15_000},
                            timeout=20,
                        )
                    except StructuredError:
                        pass
            else:
                try:
                    self.mcp.tool('close_page', {'pageId': page['id']})
                except StructuredError:
                    pass

    def open_project(self, binding, project):
        requested = str(project or '').strip()
        if not requested:
            raise StructuredError('CHAT_PROJECT_REQUIRED', '需要指定 ChatGPT Project 名称、ID 或 URL。')
        if self._is_chatgpt_url(requested):
            identity = self.parse_identity(requested)
            if not identity.get('project_id'):
                raise StructuredError('CHAT_PROJECT_INVALID', '该 URL 不是 ChatGPT Project URL。')
            return self.navigate(binding, f'https://chatgpt.com/g/{identity["project_id"]}/project')
        if requested.startswith('g-p-'):
            return self.navigate(binding, f'https://chatgpt.com/g/{requested}/project')

        state = self.navigate(binding, 'https://chatgpt.com/projects')
        page_id = int(state['tab_id'])
        projects = self._wait_project_catalog(page_id)
        match = None
        if requested.isdigit():
            index = int(requested)
            if 1 <= index <= len(projects):
                match = projects[index - 1]
            elif index == len(projects) + 1:
                return self.navigate(binding, 'https://chatgpt.com/')
            else:
                raise StructuredError('CHAT_PROJECT_NOT_FOUND', f'没有找到 ChatGPT Project 编号：{requested}')
        else:
            match = next((item for item in projects if item.get('name') == requested), None)
        if match and match.get('url'):
            return self.navigate(binding, match['url'])
        self.mcp.tool('select_page', {'pageId': page_id, 'bringToFront': False})
        clicked = self._focus_and_enter(page_id, self._click_project_script(match.get('name') if match else requested))
        if not clicked:
            raise StructuredError('CHAT_PROJECT_NOT_FOUND', f'没有找到 ChatGPT Project：{requested}')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pages = self._pages()
            candidate = next((
                item for item in pages
                if item['id'] == page_id
                and '/g/g-p-' in item['url']
                and item['url'].rstrip('/').endswith('/project')
            ), None)
            if candidate:
                return {'tab_id': str(candidate['id']), 'url': candidate['url'], **self.parse_identity(candidate['url'])}
            time.sleep(0.2)
        raise StructuredError('CHAT_PROJECT_OPEN_TIMEOUT', f'打开 ChatGPT Project 超时：{requested}')

    def _wait_project_catalog(self, page_id, timeout=10):
        deadline = time.monotonic() + timeout
        projects = {}
        stable_signature = None
        stable_hits = 0
        while time.monotonic() < deadline:
            result = self._evaluate(page_id, r'''() => {
              const main = document.querySelector('main');
              if (!main) return {ready: false, loading: true, projects: []};
              const loading = !!main.querySelector('[aria-busy="true"], [role="progressbar"]');
              const projects = [];
              const seen = new Set();
              const push = (name, url) => {
                name = String(name || '').trim();
                url = String(url || '').trim();
                const match = url.match(/\/g\/(g-p-[0-9a-f]+)(?:-[^/]+)?(?:\/project)?\/?(?:[?#].*)?$/i);
                const projectId = match ? match[1] : '';
                const key = projectId || `${name}|${url}`;
                if (!name || seen.has(key)) return;
                const unresolved = projects.find(item => item.name === name && !item.project_id);
                if (projectId && unresolved) {
                  unresolved.url = url;
                  unresolved.project_id = projectId;
                  seen.add(key);
                  return;
                }
                seen.add(key);
                projects.push({name, url, project_id: projectId || null});
              };
              for (const row of main.querySelectorAll('[role="row"]')) {
                if (row.querySelector('[role="columnheader"]')) continue;
                const cell = row.querySelector('[role="gridcell"]');
                if (!cell) continue;
                const rowText = String(row.innerText || '').trim();
                const headerName = /(?:^|\n)(?:名称|Name)(?:\n|$)/i.test(rowText);
                const headerModified = /(?:^|\n)(?:修改时间|Modified|Last modified)(?:\n|$)/i.test(rowText);
                if (headerName && headerModified) continue;
                const anchor = row.querySelector('a[href*="/g/g-p-"]');
                push(String(cell.innerText || '').trim().split('\n')[0], anchor?.href || '');
              }
              for (const anchor of document.querySelectorAll('a[href*="/g/g-p-"]')) {
                const url = String(anchor.href || '');
                if (url.includes('/c/')) continue;
                push(anchor.getAttribute('aria-label') || anchor.getAttribute('title') || anchor.innerText, url);
              }
              return {ready: true, loading, projects};
            }''')
            if not isinstance(result, dict) or not result.get('ready'):
                time.sleep(0.2)
                continue
            for item in result.get('projects') or []:
                if not isinstance(item, dict) or not str(item.get('name') or '').strip():
                    continue
                name = str(item.get('name') or '').strip()
                project_id = item.get('project_id') or None
                if project_id:
                    existing_key = next((
                        key for key, existing in projects.items()
                        if existing.get('project_id') == project_id
                        or (not existing.get('project_id') and existing.get('name') == name)
                    ), None)
                    if existing_key is not None:
                        projects[existing_key] = {
                            'name': name,
                            'url': str(item.get('url') or '').strip() or None,
                            'project_id': project_id,
                        }
                        continue
                elif any(existing.get('project_id') and existing.get('name') == name for existing in projects.values()):
                    continue
                key = project_id or item.get('url') or item.get('name')
                projects[str(key)] = {
                    'name': name,
                    'url': str(item.get('url') or '').strip() or None,
                    'project_id': project_id,
                }
            signature = tuple(sorted((item['project_id'] or '', item['url'] or '', item['name']) for item in projects.values()))
            if signature and not result.get('loading') and signature == stable_signature:
                stable_hits += 1
                if stable_hits >= 3:
                    return list(projects.values())
            else:
                stable_signature = signature
                stable_hits = 0
            time.sleep(0.25)
        return list(projects.values())

    def _ensure_sidebar_open(self, page_id, timeout=5):
        status = self._evaluate(page_id, '''() => {
          if (document.querySelector('[role="dialog"] nav[aria-label="Chat history"], nav[aria-label="Chat history"] a[data-sidebar-item]')) {
            return 'open';
          }
          const button = document.querySelector('[data-testid="open-sidebar-button"]');
          if (!button) return 'unavailable';
          button.focus();
          return document.activeElement === button ? 'focused' : 'unavailable';
        }''')
        if status == 'open':
            return True
        if status != 'focused':
            return False
        self.mcp.tool('press_key', {'pageId': page_id, 'key': 'Enter'}, timeout=max(self.mcp.timeout, 20))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready = self._evaluate(page_id, '''() => !!document.querySelector(
              '[role="dialog"] nav[aria-label="Chat history"], nav[aria-label="Chat history"] a[data-sidebar-item]'
            )''')
            if ready:
                return True
            time.sleep(0.1)
        return False

    def list_project_conversations(self, binding=None, project_id=None):
        page = self._page(binding, create=bool((binding or {}).get('url')))
        if page is None:
            return []
        self._ensure_sidebar_open(page['id'])
        current = self.parse_identity(page['url'])
        wanted_project = project_id or current.get('project_id')
        deadline = time.monotonic() + 10
        links = {}
        stable_signature = None
        stable_hits = 0
        empty_ready_hits = 0
        while time.monotonic() < deadline:
            result = self._evaluate(page['id'], '''() => ({
              ready: !!document.querySelector('main form'),
              links: Array.from(document.querySelectorAll('a[href*="/c/"]')).map(a => ({
                title: String(a.innerText || '').trim(),
                url: String(a.href || '')
              })).filter(item => item.title && item.url)
            })''')
            if not isinstance(result, dict) or not result.get('ready'):
                time.sleep(0.25)
                continue
            current_links = result.get('links') or []
            for item in current_links:
                if isinstance(item, dict) and item.get('url'):
                    links[str(item['url'])] = item
            signature = tuple(sorted(links))
            if signature:
                empty_ready_hits = 0
                if signature == stable_signature:
                    stable_hits += 1
                    if stable_hits >= 3:
                        break
                else:
                    stable_signature = signature
                    stable_hits = 0
            else:
                empty_ready_hits += 1
                if empty_ready_hits >= 8:
                    break
            time.sleep(0.25)
        seen = set()
        conversations = []
        for item in links.values():
            identity = self.parse_identity(item.get('url'))
            conversation_id = identity.get('conversation_id')
            if not conversation_id or (wanted_project and identity.get('project_id') != wanted_project) or conversation_id in seen:
                continue
            seen.add(conversation_id)
            title_lines = str(item.get('title') or '').strip().splitlines()
            conversations.append({
                'title': title_lines[0] if title_lines else '未命名对话',
                'url': item.get('url'),
                'project_id': identity.get('project_id'),
                'conversation_id': conversation_id,
            })
        return conversations

    def _history_items(self, page_id, limit=10):
        limit = min(max(int(limit), 1), 50)
        history_script = '''() => {
          const limit = __CFR_HISTORY_LIMIT__;
          const bound = (value) => {
            const text = String(value || '').trim();
            return text.length > 1600 ? text.slice(0, 1597) + '...' : text;
          };
          const visible = (node) => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
          };
          let roots = Array.from(document.querySelectorAll('section[data-turn="user"], section[data-turn="assistant"]'));
          if (!roots.length) {
            roots = [];
            for (const node of document.querySelectorAll(
              '[data-message-author-role="user"], [data-message-author-role="assistant"], user-message, model-response'
            )) {
              const root = node.closest('article, section') || node;
              if (!roots.includes(root)) roots.push(root);
            }
          }
          return roots.slice(-limit).map(root => {
            const explicit = String(root.getAttribute('data-turn') || root.getAttribute('data-message-author-role') || '').toLowerCase();
            const role = explicit === 'user' || root.matches('user-message, [data-message-author-role="user"]')
              ? 'user'
              : 'assistant';
            if (role === 'assistant') {
              const parts = [];
              for (const node of root.querySelectorAll('.markdown')) {
                if (!visible(node) || node.closest(
                  '[data-streaming-response-status], [data-testid*="cot" i], [data-testid*="reasoning" i], [class*="reasoning" i], [class*="thinking" i]'
                )) continue;
                const value = bound(node.innerText || node.textContent || '');
                if (value && !parts.includes(value)) parts.push(value);
              }
              if (parts.length) return {role, text: bound(parts.join('\\n'))};
            }
            return {role, text: bound(root.innerText || root.textContent || '')};
          }).filter(item => item.text);
        }'''.replace('__CFR_HISTORY_LIMIT__', str(limit))
        result = self._evaluate(page_id, history_script)
        if not isinstance(result, list):
            return []
        history = []
        for item in result[-limit:]:
            if not isinstance(item, dict) or item.get('role') not in {'user', 'assistant'}:
                continue
            text = str(item.get('text') or '').strip()
            if text:
                history.append({'role': item['role'], 'text': text})
        return history

    def conversation_history(self, binding=None, limit=10):
        """Return recent visible turns from the currently bound ChatGPT page."""
        limit = min(max(int(limit), 1), 50)
        page = self._page(binding, create=bool((binding or {}).get('url')))
        if page is None:
            return []
        self._restore_bound_url(page['id'], (binding or {}).get('url'))
        state = self._state(page['id'])
        self._observe_page_health(state)
        if state.get('blocked'):
            self._require_ready(state)
        if not self.parse_identity(state.get('url')).get('conversation_id'):
            return []
        state = self._wait_ready(page['id'], timeout=20)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            history = self._history_items(page['id'], limit)
            if history:
                return history
            state = self._state(page['id'])
            if state.get('blocked'):
                self._require_ready(state)
            time.sleep(0.2)
        raise StructuredError('CHAT_HISTORY_NOT_READY', 'ChatGPT Conversation 已打开，但可见消息在 20 秒内仍未完成加载。')

    def open_conversation(self, binding, conversation, *, project_id=None):
        requested = str(conversation or '').strip()
        if not requested:
            raise StructuredError('CHAT_CONVERSATION_REQUIRED', '需要指定 ChatGPT Conversation ID 或 URL。')
        if self._is_chatgpt_url(requested):
            identity = self.parse_identity(requested)
            if not identity.get('conversation_id'):
                raise StructuredError('CHAT_CONVERSATION_INVALID', '该 URL 不是 ChatGPT Conversation URL。')
            return self.navigate(binding, requested)
        project = project_id or self.parse_identity((binding or {}).get('url')).get('project_id')
        url = f'https://chatgpt.com/g/{project}/c/{requested}' if project else f'https://chatgpt.com/c/{requested}'
        return self.navigate(binding, url)

    def open_scheduled(self, binding=None):
        page = self._scheduled_page()
        return {'tab_id': str(page['id']), 'url': page['url'], 'project_id': None, 'conversation_id': None}

    def list_scheduled_tasks(self):
        page = self._scheduled_page()
        deadline = time.monotonic() + 10
        empty_since = None
        while time.monotonic() < deadline:
            result = self._evaluate(page['id'], '''() => {
              const main = Array.from(document.querySelectorAll('main')).find(x => /已计划|scheduled/i.test(String(x.getAttribute('aria-label') || x.innerText || '')));
              if (!main) return null;
              return Array.from(main.querySelectorAll('article')).filter(a => a.querySelector('h3')).map(a => {
                const style = String(a.getAttribute('style') || '');
                const match = style.match(/scheduled-task-([a-z0-9]+)/i);
                const buttons = Array.from(a.querySelectorAll('button')).map(b => String(b.getAttribute('aria-label') || '').trim());
                return {
                  task_id: match ? match[1] : null,
                  title: String(a.querySelector('h3')?.innerText || '').trim(),
                  detail: String(a.querySelector('p')?.innerText || '').trim(),
                  paused: buttons.some(label => /恢复|resume/i.test(label)),
                  can_pause: buttons.some(label => /暂停|pause/i.test(label)),
                  can_edit: buttons.some(label => /编辑|edit/i.test(label)),
                  has_more: buttons.some(label => /更多任务操作|more task/i.test(label))
                };
              });
            }''')
            if isinstance(result, list):
                if result:
                    return result
                empty_since = empty_since or time.monotonic()
                if time.monotonic() - empty_since >= 5:
                    return []
            time.sleep(0.2)
        raise StructuredError('CHAT_SCHEDULED_PAGE_TIMEOUT', 'ChatGPT Scheduled 管理页未加载完成。')

    def create_scheduled_task(self, prompt):
        prompt = str(prompt or '').strip()
        if not prompt:
            raise StructuredError('CHAT_SCHEDULED_PROMPT_REQUIRED', 'Scheduled create 需要非空自然语言任务。')
        before = {item['task_id'] for item in self.list_scheduled_tasks() if item.get('task_id')}
        page = self._scheduled_page()
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
        textbox = re.search(r'uid=(\S+)\s+textbox\s+"(?:安排任务|Schedule task)"', snapshot, re.IGNORECASE)
        if not textbox:
            raise StructuredError('CHAT_SCHEDULED_COMPOSER_NOT_FOUND', '没有找到 ChatGPT Scheduled 任务输入框。')
        self.mcp.tool('fill', {'pageId': page['id'], 'uid': textbox.group(1), 'value': prompt})
        self.mcp.tool('press_key', {'pageId': page['id'], 'key': 'Enter'})
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                tasks = self.list_scheduled_tasks()
            except StructuredError:
                continue
            created = next((item for item in tasks if item.get('task_id') and item['task_id'] not in before), None)
            if created:
                return created
        raise StructuredError('CHAT_SCHEDULED_CREATE_TIMEOUT', 'Scheduled 任务已提交，但 45 秒内没有在任务列表确认新 task ID。')

    def pause_scheduled_task(self, task_id):
        return self._set_scheduled_paused(task_id, True)

    def resume_scheduled_task(self, task_id):
        return self._set_scheduled_paused(task_id, False)

    def _set_scheduled_paused(self, task_id, paused):
        page = self._scheduled_task_page(task_id)
        action = '恢复|resume' if not paused else '暂停|pause'
        clicked = self._focus_and_enter(page['id'], f'''() => {{
          const id = {str(task_id)!r};
          const article = Array.from(document.querySelectorAll('article')).find(x => String(x.getAttribute('style') || '').includes('scheduled-task-' + id));
          const button = Array.from(article?.querySelectorAll('button') || []).find(x => /{action}/i.test(String(x.getAttribute('aria-label') || '')));
          if (!button) return false;
          button.focus();
          return document.activeElement === button;
        }}''')
        if not clicked:
            raise StructuredError('CHAT_SCHEDULED_ACTION_NOT_FOUND', f'Scheduled task 没有可用的 {"pause" if paused else "resume"} 控件：{task_id}')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            tasks = self.list_scheduled_tasks()
            task = next((item for item in tasks if item.get('task_id') == task_id), None)
            if task and bool(task.get('paused')) == paused:
                return task
            time.sleep(0.3)
        raise StructuredError('CHAT_SCHEDULED_ACTION_TIMEOUT', f'Scheduled task 状态没有确认变更：{task_id}')

    def edit_scheduled_task(self, task_id, *, title=None, instructions=None):
        if title is None and instructions is None:
            raise StructuredError('CHAT_SCHEDULED_EDIT_REQUIRED', 'Scheduled edit 至少需要 title 或 instructions。')
        page = self._scheduled_task_page(task_id)
        clicked = self._focus_and_enter(page['id'], f'''() => {{
          const id = {str(task_id)!r};
          const article = Array.from(document.querySelectorAll('article')).find(x => String(x.getAttribute('style') || '').includes('scheduled-task-' + id));
          const button = Array.from(article?.querySelectorAll('button') || []).find(x => /编辑|edit/i.test(String(x.getAttribute('aria-label') || '')));
          if (!button) return false;
          button.focus();
          return document.activeElement === button;
        }}''')
        if not clicked:
            raise StructuredError('CHAT_SCHEDULED_EDIT_NOT_FOUND', f'没有找到 Scheduled task 编辑控件：{task_id}')
        deadline = time.monotonic() + 5
        snapshot = ''
        while time.monotonic() < deadline:
            snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
            has_title = re.search(r'uid=(\S+)\s+textbox\s+"(?:标题|Title)"', snapshot, re.IGNORECASE)
            has_instructions = re.search(r'uid=(\S+)\s+textbox\s+"(?:说明|Instructions)"', snapshot, re.IGNORECASE)
            if has_title and (instructions is None or has_instructions):
                break
            time.sleep(0.2)
        if title is not None:
            uid = re.search(r'uid=(\S+)\s+textbox\s+"(?:标题|Title)"', snapshot, re.IGNORECASE)
            if not uid:
                raise StructuredError('CHAT_SCHEDULED_TITLE_NOT_FOUND', 'Scheduled 编辑弹层没有标题字段。')
            self.mcp.tool('fill', {'pageId': page['id'], 'uid': uid.group(1), 'value': str(title)})
        if instructions is not None:
            snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
            uid = re.search(r'uid=(\S+)\s+textbox\s+"(?:说明|Instructions)"', snapshot, re.IGNORECASE)
            if not uid:
                raise StructuredError('CHAT_SCHEDULED_INSTRUCTIONS_NOT_FOUND', 'Scheduled 编辑弹层没有说明字段。')
            self.mcp.tool('fill', {'pageId': page['id'], 'uid': uid.group(1), 'value': str(instructions)})
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
        save = re.search(r'uid=(\S+)\s+button\s+"(?:保存|Save)"(?!.*disabled)', snapshot, re.IGNORECASE)
        if not save:
            save = re.search(r'uid=(\S+)\s+button\s+"(?:保存|Save)"', snapshot, re.IGNORECASE)
        if not save:
            raise StructuredError('CHAT_SCHEDULED_SAVE_NOT_FOUND', 'Scheduled 编辑弹层没有可用保存按钮。')
        saved = self._focus_and_enter(page['id'], '''() => {
          const buttons = Array.from(document.querySelectorAll('button')).filter(node => /^(?:保存|Save)$/i.test(String(node.getAttribute('aria-label') || node.textContent || '').trim()));
          const button = buttons.find(node => node.getAttribute('aria-disabled') !== 'true' && !node.disabled) || buttons[0];
          if (!button) return false;
          button.focus();
          return document.activeElement === button;
        }''')
        if not saved:
            raise StructuredError('CHAT_SCHEDULED_SAVE_NOT_FOUND', 'Scheduled 编辑弹层保存按钮当前不可交互。')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            tasks = self.list_scheduled_tasks()
            task = next((item for item in tasks if item.get('task_id') == task_id), None)
            if task and (title is None or task.get('title') == str(title)):
                return task
            time.sleep(0.3)
        raise StructuredError('CHAT_SCHEDULED_EDIT_TIMEOUT', f'Scheduled task 编辑没有在列表中确认：{task_id}')

    def delete_scheduled_task(self, task_id):
        page = self._scheduled_task_page(task_id)
        clicked = self._focus_and_enter(page['id'], f'''() => {{
          const id = {str(task_id)!r};
          const article = Array.from(document.querySelectorAll('article')).find(x => String(x.getAttribute('style') || '').includes('scheduled-task-' + id));
          const button = Array.from(article?.querySelectorAll('button') || []).find(x => /更多任务操作|more task/i.test(String(x.getAttribute('aria-label') || '')));
          if (!button) return false;
          button.focus();
          return document.activeElement === button;
        }}''')
        if not clicked:
            raise StructuredError('CHAT_SCHEDULED_MORE_NOT_FOUND', f'没有找到 Scheduled task 更多操作：{task_id}')
        time.sleep(0.2)
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
        delete = re.search(r'uid=(\S+)\s+menuitem\s+"(?:删除|Delete)"', snapshot, re.IGNORECASE)
        if not delete:
            raise StructuredError('CHAT_SCHEDULED_DELETE_NOT_FOUND', f'没有找到 Scheduled task 删除操作：{task_id}')
        opened = self._focus_and_enter(page['id'], '''() => {
          const item = Array.from(document.querySelectorAll('[role="menuitem"]')).find(node => /^(?:删除|Delete)$/i.test(String(node.getAttribute('aria-label') || node.textContent || '').trim()));
          if (!item) return false;
          item.focus();
          return document.activeElement === item;
        }''')
        if not opened:
            raise StructuredError('CHAT_SCHEDULED_DELETE_NOT_FOUND', f'Scheduled task 删除操作当前不可交互：{task_id}')
        time.sleep(0.2)
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
        confirm = re.search(r'uid=(\S+)\s+button\s+"(?:删除|Delete)"', snapshot, re.IGNORECASE)
        if confirm:
            self._focus_and_enter(page['id'], '''() => {
              const buttons = Array.from(document.querySelectorAll('button')).filter(node => /^(?:删除|Delete)$/i.test(String(node.getAttribute('aria-label') || node.textContent || '').trim()));
              const button = buttons[buttons.length - 1];
              if (!button) return false;
              button.focus();
              return document.activeElement === button;
            }''')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not any(item.get('task_id') == task_id for item in self.list_scheduled_tasks()):
                return {'task_id': task_id, 'deleted': True}
            time.sleep(0.3)
        raise StructuredError('CHAT_SCHEDULED_DELETE_TIMEOUT', f'Scheduled task 删除没有从列表中确认：{task_id}')

    def _scheduled_page(self):
        page = next((item for item in self._pages() if self._same_navigation_target(item['url'], 'https://chatgpt.com/scheduled')), None)
        return page or self._new_page('https://chatgpt.com/scheduled', background=True)

    def _scheduled_task_page(self, task_id):
        tasks = self.list_scheduled_tasks()
        if not any(item.get('task_id') == task_id for item in tasks):
            raise StructuredError('CHAT_SCHEDULED_TASK_NOT_FOUND', f'没有找到 Scheduled task：{task_id}')
        return self._scheduled_page()

    def stop(self, binding):
        page = self._page(binding, create=bool((binding or {}).get('url')))
        if page is None:
            return {'status': 'NO_ACTIVE_TURN'}
        research = self._parse_deep_research_snapshot(
            self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page['id']}))
        )
        if research['state'] == 'running':
            return {'status': 'DEEP_RESEARCH_STOP_NOT_VERIFIED'}
        clicked = self._focus_and_enter(page['id'], self._click_stop_script())
        return {'status': 'STOP_REQUESTED' if clicked else 'NO_ACTIVE_TURN'}

    def close(self):
        self.mcp.close()
        if getattr(self.mcp, 'mode', 'dedicated') == 'dedicated':
            self._stop_profile_browser()

    def _prepare_dedicated_runtime(self):
        if self.mcp.running:
            return None
        if self._profile_browser_running():
            if self._login_pending_marker.exists() and not self._auth_marker.exists():
                return self._waiting_for_login()
            self._stop_profile_browser()
        if not self._auth_marker.exists() and not self._login_pending_marker.exists():
            self._launch_login_bootstrap()
            return self._waiting_for_login()
        try:
            self.mcp.start()
            self._ensure_chatgpt_tab()
        except StructuredError:
            self.mcp.close()
            if not self._auth_marker.exists():
                self._launch_login_bootstrap()
                return self._waiting_for_login()
            raise
        return None

    def _ensure_chatgpt_tab(self):
        if getattr(self.mcp, 'mode', 'dedicated') == 'shared':
            pages = self._pages()
            owner = self._shared_owner_page(pages)
            if owner is not None:
                return owner
            return self._new_page('https://chatgpt.com/', background=True)
        if getattr(self.mcp, 'mode', 'dedicated') == 'embedded':
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pages = self._pages()
                if any(self._is_chatgpt_url(page['url']) for page in pages):
                    return
                time.sleep(0.1)
            raise StructuredError(
                'CHAT_EMBEDDED_TARGET_MISSING',
                'CFR 内置 ChatGPT 页面尚未出现在 WebView2 调试目标中。',
            )
        pages = self._pages()
        if not any(self._is_chatgpt_url(page['url']) for page in pages):
            self.mcp.tool('new_page', {
                'url': 'https://chatgpt.com/',
                # Shared Chrome is the user's normal browser. Never steal
                # focus merely because CFR is starting its Chat runtime.
                'background': getattr(self.mcp, 'mode', 'dedicated') == 'shared',
                'timeout': 15_000,
            }, timeout=20)

    def _shared_owner_page(self, pages=None):
        """Return CFR's one marked shared-Chrome tab, never a user's tab."""
        if getattr(self.mcp, 'mode', 'dedicated') != 'shared':
            return None
        if getattr(self.mcp, 'driver', None) == 'chrome-use':
            return next(
                (page for page in (pages if pages is not None else self._pages()) if self._is_chatgpt_url(page.get('url'))),
                None,
            )
        for page in pages if pages is not None else self._pages():
            if not self._is_chatgpt_url(page.get('url')):
                continue
            try:
                marker = self._evaluate(
                    page['id'],
                    "() => ({name: window.name, session: sessionStorage.getItem('__cfr_chat_surface__')})",
                )
            except StructuredError:
                continue
            if isinstance(marker, dict):
                owned = str(marker.get('name') or '') == LINUX_SHARED_TAB_NAME or marker.get('session') == '1'
            else:
                owned = str(marker or '') == LINUX_SHARED_TAB_NAME
            if owned:
                return page
        return None

    def _mark_shared_owner_page(self, page_id):
        if getattr(self.mcp, 'driver', None) == 'chrome-use':
            return
        marker = json.dumps(LINUX_SHARED_TAB_NAME)
        value = self._evaluate(
            page_id,
            f"() => {{ window.name = {marker}; sessionStorage.setItem('__cfr_chat_surface__', '1'); return window.name; }}",
        )
        if str(value or '') != LINUX_SHARED_TAB_NAME:
            raise StructuredError(
                'CHAT_SHARED_TAB_MARK_FAILED',
                '无法为 CFR Linux Chat 标签页建立独占标记。',
            )

    def _waiting_for_login(self):
        mode = getattr(self.mcp, 'mode', 'dedicated')
        if mode == 'embedded':
            error_code = 'CHATGPT_EMBEDDED_LOGIN_REQUIRED'
            description = '请在 CFR 内置 ChatGPT 窗口完成一次登录；原有专用 Chrome 登录和 Profile 不会被删除。'
        else:
            error_code = 'CHATGPT_LOGIN_BOOTSTRAP_REQUIRED'
            description = '请在 CFR 专用普通 Chrome 中完成 ChatGPT 登录，然后关闭该登录窗口；CFR 会用同一 Profile 继续接管。'
        return {
            'available': False,
            'status': 'waiting_user',
            'error_code': error_code,
            'mode': mode,
            'description': description,
            'url': 'https://chatgpt.com/',
        }

    def _launch_login_bootstrap(self):
        chrome = _chrome_executable()
        if not chrome:
            raise StructuredError('CHAT_CHROME_MISSING', '未找到可用于 CFR Chat 登录初始化的 Google Chrome。')
        self.mcp.close()
        self.mcp.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._browser_state_dir.mkdir(parents=True, exist_ok=True)
        self._login_pending_marker.touch()
        if self._profile_browser_running():
            return
        # Login is intentionally outside DevTools/MCP control. Do not add
        # automation, webdriver, remote-debugging, or anti-detection flags here.
        self._login_proc = subprocess.Popen(
            [chrome, f'--user-data-dir={self.mcp.user_data_dir}', '--new-window', 'https://chatgpt.com/auth/login'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _profile_browser_running(self):
        if os.name != 'nt':
            if self._login_proc and self._login_proc.poll() is None:
                return True
            return bool(self._profile_browser_pids())
        profile = str(Path(self.mcp.user_data_dir).resolve()).replace("'", "''")
        script = (
            f"$p='{profile}'; "
            "$x=Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine -like ('*'+$p+'*') -and $_.CommandLine -notlike '*--type=*' }; "
            "if ($x) { exit 0 } else { exit 1 }"
        )
        try:
            return subprocess.run(
                ['powershell', '-NoProfile', '-Command', script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False,
                **hidden_subprocess_kwargs(),
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _profile_browser_pids(self):
        """Return Linux browser parents using CFR's dedicated profile.

        Chrome launched by MCP can outlive the MCP wrapper.  Before a manual
        login we must ensure that such a process cannot cause the new window to
        join an automation-controlled browser instance.
        """
        if os.name == 'nt' or not Path('/proc').is_dir():
            return ()
        profile = str(Path(self.mcp.user_data_dir).resolve())
        wanted = f'--user-data-dir={profile}'
        found = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                argv = (entry / 'cmdline').read_bytes().split(b'\0')
            except (OSError, PermissionError):
                continue
            args = [item.decode('utf-8', errors='ignore') for item in argv if item]
            if wanted in args:
                found.append(pid)
        return tuple(found)

    def _stop_profile_browser(self):
        if os.name == 'nt':
            profile = str(Path(self.mcp.user_data_dir).resolve()).replace("'", "''")
            script = (
                f"$p='{profile}'; "
                "$x=Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "Where-Object { $_.CommandLine -and $_.CommandLine -like ('*'+$p+'*') }; "
                "foreach($i in $x){ Stop-Process -Id $i.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            try:
                subprocess.run(
                    ['powershell', '-NoProfile', '-Command', script],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
                    **hidden_subprocess_kwargs(),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            return
        proc = self._login_proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        self._login_proc = None
        pids = self._profile_browser_pids()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        if pids:
            deadline = time.monotonic() + 2
            remaining = set(pids)
            while remaining and time.monotonic() < deadline:
                remaining = {pid for pid in remaining if Path(f'/proc/{pid}').exists()}
                if remaining:
                    time.sleep(0.05)
            for pid in remaining:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def _remote_debugging_enabled(self):
        if getattr(self.mcp, 'mode', 'dedicated') != 'shared':
            return True
        if os.name != 'nt':
            return True
        local = os.environ.get('LOCALAPPDATA')
        if not local:
            return True
        return (Path(local) / 'Google' / 'Chrome' / 'User Data' / 'DevToolsActivePort').exists()

    def _pages(self):
        text = self.mcp.text(self.mcp.tool('list_pages'))
        pages = []
        for line in text.splitlines():
            match = re.match(r'\s*(\d+):\s+.*?(https?://\S+?)(?:\)|\s|$)', line)
            if match:
                pages.append({'id': int(match.group(1)), 'url': match.group(2), 'selected': '[selected]' in line})
        return pages

    def _new_page(self, url, *, background=True):
        if getattr(self.mcp, 'mode', 'dedicated') == 'shared':
            before_ids = {page['id'] for page in self._pages()}
            result = self.mcp.tool(
                'new_page',
                {'url': url, 'background': True, 'timeout': 15_000},
                timeout=20,
            )
            created = self._parse_first_page(self.mcp.text(result))
            if created is None:
                pages = self._pages()
                new_pages = [
                    page for page in pages
                    if page['id'] not in before_ids and self._is_chatgpt_url(page['url'])
                ]
                created = new_pages[0] if len(new_pages) == 1 else None
            if created is None:
                raise StructuredError(
                    'CHAT_SHARED_TAB_CREATE_UNCONFIRMED',
                    '无法确认 CFR Linux 专用 ChatGPT 标签页已经创建；不会复用用户当前标签页。',
                )
            self._mark_shared_owner_page(created['id'])
            return created
        if getattr(self.mcp, 'mode', 'dedicated') == 'embedded':
            pages = self._pages()
            page = next((item for item in pages if item['selected'] and self._is_chatgpt_url(item['url'])), None)
            if page is None:
                page = next((item for item in pages if self._is_chatgpt_url(item['url'])), None)
            if page is None:
                raise StructuredError(
                    'CHAT_EMBEDDED_TARGET_MISSING',
                    'CFR 内置 ChatGPT 页面不可用；不会创建额外浏览器窗口。',
                )
            if not self._same_navigation_target(page['url'], url):
                self.mcp.tool(
                    'navigate_page',
                    {'pageId': page['id'], 'type': 'url', 'url': url, 'timeout': 15_000},
                    timeout=20,
                )
                pages = self._pages()
                page = next((item for item in pages if item['id'] == page['id']), None) or next(
                    (item for item in pages if self._same_navigation_target(item['url'], url)),
                    page,
                )
            return page
        result = self.mcp.tool('new_page', {'url': url, 'background': background, 'timeout': 15_000}, timeout=20)
        created = self._parse_first_page(self.mcp.text(result))
        if created and self._same_navigation_target(created['url'], url):
            return created
        pages = self._pages()
        page = next((item for item in reversed(pages) if self._same_navigation_target(item['url'], url)), None)
        if not page:
            login_page = next((item for item in reversed(pages) if '/auth/login' in item['url']), None)
            if login_page:
                raise StructuredError('CHATGPT_LOGIN_REQUIRED', 'CFR 专用 Chrome 尚未登录 ChatGPT；请在该窗口完成一次登录。')
            raise StructuredError('CHAT_PAGE_CREATE_FAILED', '无法在 CFR Chrome 中创建 ChatGPT 页面。')
        return page

    def _page(self, binding=None, *, create):
        binding = binding or {}
        pages = self._pages()
        if getattr(self.mcp, 'mode', 'dedicated') == 'shared':
            owner = self._shared_owner_page(pages)
            if owner is not None:
                return owner
            if not create:
                return None
            wanted_url = binding.get('url')
            url = wanted_url if self._is_chatgpt_url(wanted_url) else 'https://chatgpt.com/'
            return self._new_page(url, background=True)
        wanted_id = None
        try:
            wanted_id = int(binding.get('tab_id')) if binding.get('tab_id') is not None else None
        except (TypeError, ValueError):
            pass
        if wanted_id is not None:
            page = next((item for item in pages if item['id'] == wanted_id and self._is_chatgpt_url(item['url'])), None)
            if page:
                return page
        wanted_url = binding.get('url')
        if wanted_url:
            page = next((item for item in pages if item['url'] == wanted_url and self._is_chatgpt_url(item['url'])), None)
            if page:
                return page
        if not create:
            # In shared mode an unbound read must never fall back to whatever
            # ChatGPT tab the human currently has selected. CFR only reads or
            # mutates shared-browser tabs once a Feishu chat has an explicit
            # tab/url binding.
            if getattr(self.mcp, 'mode', 'dedicated') == 'shared':
                return None
            selected = next((item for item in pages if item['selected'] and self._is_chatgpt_url(item['url'])), None)
            if selected:
                return selected
            if getattr(self.mcp, 'mode', 'dedicated') == 'embedded':
                return next((item for item in pages if self._is_chatgpt_url(item['url'])), None)
            return None
        url = wanted_url if self._is_chatgpt_url(wanted_url) else 'https://chatgpt.com/'
        return self._new_page(url, background=True)

    @staticmethod
    def _parse_first_page(text):
        pages = []
        for line in str(text or '').splitlines():
            match = re.match(r'\s*(\d+):\s+.*?(https?://\S+?)(?:\)|\s|$)', line)
            if match:
                pages.append({'id': int(match.group(1)), 'url': match.group(2), 'selected': '[selected]' in line})
        return next((page for page in pages if page['selected']), pages[-1] if pages else None)

    def _restore_bound_url(self, page_id, url):
        if not self._is_chatgpt_url(url):
            return
        current = self._state(page_id).get('url')
        bound_identity = self.parse_identity(url)
        current_identity = self.parse_identity(current)
        # A generic / or project landing URL represents a pending new chat. If
        # that tab has already become a concrete conversation, do not navigate
        # it back to the landing page and lose the newly-created identity.
        if not bound_identity.get('conversation_id') and current_identity.get('conversation_id'):
            if not bound_identity.get('project_id') or bound_identity.get('project_id') == current_identity.get('project_id'):
                return
        if current != url:
            self.mcp.tool('navigate_page', {'pageId': page_id, 'type': 'url', 'url': url, 'timeout': 15_000}, timeout=20)

    @staticmethod
    def parse_identity(url):
        try:
            path = urlparse(str(url or '')).path
        except ValueError:
            return {'project_id': None, 'conversation_id': None}
        project_match = re.search(r'/g/(g-p-[0-9a-f]+)(?:-[^/]+)?(?:/|$)', path, re.IGNORECASE)
        conversation_match = re.search(r'/c/([^/?#]+)', path, re.IGNORECASE)
        return {
            'project_id': project_match.group(1) if project_match else None,
            'conversation_id': conversation_match.group(1) if conversation_match else None,
        }

    @staticmethod
    def _same_navigation_target(current, requested):
        if current == requested:
            return True
        try:
            left, right = urlparse(str(current)), urlparse(str(requested))
        except ValueError:
            return False
        return left.hostname in CHATGPT_HOSTS and right.hostname in CHATGPT_HOSTS and left.path.rstrip('/') == right.path.rstrip('/')

    @staticmethod
    def _click_project_script(name):
        target = json.dumps(name)
        return f'''() => {{
          const target = {target};
          const rows = Array.from(document.querySelectorAll('main [role="row"]'));
          const row = rows.find(item => String(item.querySelector('[role="gridcell"]')?.innerText || '').trim().split('\\n')[0] === target);
          if (!row) return false;
          const control = row.matches('[tabindex="0"]')
            ? row
            : row.querySelector('a, button, [role="link"], [role="button"], [tabindex="0"]') || row.querySelector('[role="gridcell"]');
          if (!control) return false;
          control.focus();
          return document.activeElement === control;
        }}'''

    def _prompt_uid(self, page_id):
        snapshot = self.mcp.text(self.mcp.tool('take_snapshot', {'pageId': page_id}))
        candidates = []
        for line in snapshot.splitlines():
            match = re.search(r'uid=(\S+)\s+textbox\b(.*)', line, re.IGNORECASE)
            if not match:
                continue
            detail = match.group(2)
            label_match = re.match(r'\s*"([^"]*)"', detail)
            label = (label_match.group(1) if label_match else detail).lower()
            if any(term in label for term in ('search', 'email', 'password', '搜索')):
                continue
            score = 2 if any(term in label for term in ('ask', 'message', 'prompt', 'chat', 'anything', '聊天')) else 0
            candidates.append((score, match.group(1)))
        if not candidates:
            raise StructuredError('CHAT_COMPOSER_NOT_FOUND', '没有在 ChatGPT 页面找到可输入的对话框。')
        best_score = max(score for score, _ in candidates)
        return [uid for score, uid in candidates if score == best_score][-1]

    def _state(self, page_id, *, text_limit=None, include_artifacts=True):
        return self._evaluate(
            page_id,
            self._state_script(text_limit=text_limit, include_artifacts=include_artifacts),
        ) or {}

    def _evaluate(self, page_id, function, *, timeout=None):
        result = self.mcp.tool('evaluate_script', {
            'pageId': page_id,
            'function': function,
            'waitForStableDom': False,
        }, timeout=timeout)
        text = self.mcp.text(result)
        match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
        raw = match.group(1) if match else text
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip()

    def _require_ready(self, state):
        if state.get('blocked'):
            kind = state.get('kind') or 'blocked'
            if kind == 'rate_limited':
                raise StructuredError('CHATGPT_RATE_LIMITED', 'ChatGPT 暂时限制了网页请求频率；请等待几分钟后再重试。')
            raise StructuredError('CHATGPT_WEB_NOT_READY', f'ChatGPT 网页需要人工处理：{kind}。')
        if not state.get('promptVisible'):
            raise StructuredError('CHATGPT_WEB_NOT_READY', 'ChatGPT 网页尚未显示可用对话框。')

    def _observe_page_health(self, state):
        kind = state.get('kind')
        blocked = bool(state.get('blocked'))
        ready = bool(state.get('promptVisible')) and not blocked
        status = 'ready' if ready else 'rate_limited' if kind == 'rate_limited' else 'waiting_user' if blocked else 'page_not_ready'
        error_code = None if ready else 'CHATGPT_RATE_LIMITED' if kind == 'rate_limited' else 'CHATGPT_WEB_NOT_READY'
        return self._remember_health({
            'available': ready,
            'status': status,
            'error_code': error_code,
            'mode': getattr(getattr(self, 'mcp', None), 'mode', 'unknown'),
            'description': 'ChatGPT 网页已就绪。' if ready else f'ChatGPT 网页状态：{kind or status}。',
            'url': state.get('url'),
        })

    def _wait_ready(self, page_id, timeout=10):
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        state = {}
        dismissed_overlay = False
        while time.monotonic() < deadline:
            state = self._state(page_id)
            self._observe_page_health(state)
            if state.get('blocked'):
                self._require_ready(state)
            if state.get('promptVisible'):
                return state
            if (
                not dismissed_overlay
                and time.monotonic() - started >= 0.8
                and self.parse_identity(state.get('url')).get('conversation_id')
            ):
                # ChatGPT's generated-file viewer can cover the composer while
                # keeping the conversation URL. Escape closes that transient
                # overlay without navigating away from the bound conversation.
                try:
                    self.mcp.tool('press_key', {'pageId': page_id, 'key': 'Escape'})
                except StructuredError:
                    pass
                dismissed_overlay = True
            time.sleep(0.2)
        self._require_ready(state)
        return state

    def _wait_history(self, page_id, state, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._history_items(page_id, 1):
                return self._state(page_id)
            time.sleep(0.2)
            state = self._state(page_id)
            self._observe_page_health(state)
            if state.get('blocked'):
                self._require_ready(state)
        raise StructuredError('CHAT_HISTORY_NOT_READY', 'ChatGPT 历史消息尚未加载完成；未发送新消息。')

    def _wait_send_started(self, page_id, before, *, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._state(page_id)
            if (
                state.get('generating')
                or state.get('assistantCount', 0) > before.get('assistantCount', 0)
                or state.get('userCount', 0) > before.get('userCount', 0)
                or state.get('promptLength') == 0
                or (
                    self.parse_identity(state.get('url')).get('conversation_id')
                    and not self.parse_identity(before.get('url')).get('conversation_id')
                )
            ):
                return True
            time.sleep(0.15)
        return False

    def _wait_answer(self, page_id, before, *, on_progress=None, settle_seconds=1.5):
        idle_deadline = time.monotonic() + self.query_timeout
        last_text_length = None
        last_image = ''
        last_file = ''
        last_reasoning_length = None
        stable_since = time.monotonic()
        while time.monotonic() < idle_deadline:
            # Streaming polls deliberately carry only a bounded visible tail.
            # Sending the entire cumulative DOM text every 400 ms makes a long
            # answer approach quadratic transfer cost across the MCP bridge.
            state = self._state(page_id, text_limit=4000, include_artifacts=False)
            if state.get('blocked'):
                self._require_ready(state)
            text = str(state.get('assistantText') or '').strip()
            text_length = int(state.get('assistantTextLength') or len(text))
            image = str(state.get('generatedImageSrc') or '').strip()
            file_key = str(state.get('generatedFileKey') or '').strip()
            reasoning = str(state.get('reasoningText') or '').strip()
            reasoning_length = int(state.get('reasoningTextLength') or len(reasoning))
            changed = False
            if text_length != last_text_length:
                last_text_length = text_length
                changed = True
            if image != last_image:
                last_image = image
                changed = True
            if file_key != last_file:
                last_file = file_key
                changed = True
            if reasoning_length != last_reasoning_length:
                last_reasoning_length = reasoning_length
                changed = True
            if changed:
                stable_since = time.monotonic()
                idle_deadline = stable_since + self.query_timeout
            self._progress(
                on_progress,
                state='generating' if state.get('generating') else 'submitted',
                reasoning_text=reasoning,
                answer_preview=text,
            )
            has_answer = bool(text_length) and (
                state.get('assistantCount', 0) > before.get('assistantCount', 0)
                or text_length != int(before.get('assistantTextLength') or len(str(before.get('assistantText') or '').strip()))
            )
            has_answer = has_answer or bool(image and image != str(before.get('generatedImageSrc') or '').strip())
            # A new assistant root is sufficient for file-only answers. The
            # expensive artifact DOM walk runs once at terminal state below.
            has_answer = has_answer or state.get('assistantCount', 0) > before.get('assistantCount', 0)
            if has_answer and not state.get('generating') and time.monotonic() - stable_since >= settle_seconds:
                final = self._state(page_id, include_artifacts=True)
                return {
                    'text': str(final.get('assistantText') or text).strip(),
                    'url': final.get('url') or state.get('url'),
                    'reasoning_text': str(final.get('reasoningText') or reasoning).strip(),
                    'generated_image_src': str(final.get('generatedImageSrc') or image).strip() or None,
                    'generated_file_key': str(final.get('generatedFileKey') or '').strip() or None,
                }
            time.sleep(0.4)
        raise StructuredError('CHAT_RESPONSE_TIMEOUT', '等待 ChatGPT 网页回复继续产生内容超时。')

    @staticmethod
    def _is_chatgpt_url(url):
        try:
            parsed = urlparse(str(url or ''))
        except ValueError:
            return False
        return parsed.scheme == 'https' and parsed.hostname in CHATGPT_HOSTS

    @staticmethod
    def _state_script(text_limit=None, include_artifacts=True):
        prompt = json.dumps(PROMPT_SELECTOR)
        stop = json.dumps(STOP_SELECTOR)
        assistant = json.dumps(ASSISTANT_SELECTOR)
        text_limit_value = 'null' if text_limit is None else str(max(1, int(text_limit)))
        include_artifacts_value = 'true' if include_artifacts else 'false'
        return f'''() => {{
          const textLimit = {text_limit_value};
          const includeArtifacts = {include_artifacts_value};
          const visible = (n) => {{
            if (!n) return false;
            const r = n.getBoundingClientRect();
            const s = getComputedStyle(n);
            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
          }};
          const prompts = Array.from(document.querySelectorAll({prompt})).filter(visible);
          const promptEl = prompts[prompts.length - 1] || null;
          const promptLength = promptEl ? String(promptEl.value ?? promptEl.innerText ?? promptEl.textContent ?? '').trim().length : -1;
          let assistants = Array.from(document.querySelectorAll('section[data-turn="assistant"]'));
          if (!assistants.length) {{
            const roots = [];
            for (const node of document.querySelectorAll({assistant})) {{
              const root = node.closest('article, section') || node;
              if (!roots.includes(root)) roots.push(root);
            }}
            assistants = roots;
          }}
          const lastAssistant = assistants[assistants.length - 1] || null;
          const finalMarkdown = lastAssistant ? Array.from(lastAssistant.querySelectorAll('.markdown')).filter(node =>
            visible(node) && node.closest(
              '[data-streaming-response-status], [data-testid*="cot" i], [data-testid*="reasoning" i], [class*="reasoning" i], [class*="thinking" i]'
            ) === null
          ) : [];
          const finalParts = [];
          for (const node of finalMarkdown) {{
            const value = String(node.innerText || node.textContent || '').trim();
            if (value && !finalParts.includes(value)) finalParts.push(value);
          }}
          const fullAssistantText = finalParts.length
            ? finalParts.join('\\n').trim()
            : String(lastAssistant?.innerText || '').trim();
          const assistantText = textLimit && fullAssistantText.length > textLimit
            ? fullAssistantText.slice(-textLimit)
            : fullAssistantText;
          const generating = Array.from(document.querySelectorAll({stop})).some(visible);
          const generatedImages = Array.from(lastAssistant?.querySelectorAll('img') || []).filter(img =>
            /已生成图片|generated image/i.test(String(img.alt || '')) || String(img.src || '').includes('/backend-api/estuary/content')
          );
          const generatedImageSrc = String(generatedImages[generatedImages.length - 1]?.src || '');
          const assistantTurn = lastAssistant;
          const fileValues = [];
          if (includeArtifacts && assistantTurn && !generating) {{
            for (const node of assistantTurn.querySelectorAll(
              'a[href], a[download], button, [role="button"], [data-file-id], [data-asset-pointer], [data-filename]'
            )) {{
              const generatedImage = Array.from(node.querySelectorAll?.('img') || []).some(img =>
                /已生成图片|generated image/i.test(String(img.alt || '')) || String(img.src || '').includes('/backend-api/estuary/content')
              );
              const nodeValues = [];
              for (const attr of Array.from(node.attributes || [])) nodeValues.push(String(attr.value || ''));
              if (node.tagName?.toLowerCase() === 'a') nodeValues.push(node.getAttribute('href') || '', node.href || '', node.getAttribute('download') || '');
              for (const child of Array.from(node.querySelectorAll?.('*') || []).slice(0, 500)) {{
                for (const attr of Array.from(child.attributes || [])) nodeValues.push(String(attr.value || ''));
                if (child.tagName?.toLowerCase() === 'a') nodeValues.push(child.getAttribute('href') || '', child.href || '', child.getAttribute('download') || '');
              }}
              if (String(node.outerHTML || '').length <= 200000) nodeValues.push(String(node.outerHTML || ''));
              const joined = nodeValues.filter(Boolean).join(' ');
              const fileEvidence = /file[_-][A-Za-z0-9-]{16,}|sandbox:[/]mnt[/]data[/]|[.](?:xlsx|xls|csv|docx|pptx|pdf|zip|7z|tar|gz|txt|md|json|svg)(?:$|[^A-Za-z0-9_])|[/]backend-api[/](?:sandbox[/]download|files[/])/i.test(joined);
              if (generatedImage && !fileEvidence) continue;
              fileValues.push(...nodeValues);
            }}
          }}
          const fileJoined = fileValues.filter(Boolean).join(' ');
          const fileIds = Array.from(fileJoined.matchAll(/file[_-][A-Za-z0-9-]{16,}/g), match => match[0]);
          const sandboxPaths = Array.from(fileJoined.matchAll(/sandbox:[/]mnt[/]data[/][^ "'<>]+/g), match => match[0]);
          const backendPaths = fileValues.filter(value => /[/]backend-api[/](?:sandbox[/]download|files[/]|estuary[/]content)/i.test(String(value || '')));
          const generatedFileKey = Array.from(new Set([...fileIds, ...sandboxPaths, ...backendPaths])).sort().join('|');
          const visibleText = (node) => visible(node) ? String(node.innerText || '').trim() : '';
          const commentaryRoots = assistantTurn ? Array.from(assistantTurn.querySelectorAll('.markdown')).filter(node =>
            node.closest('[data-streaming-response-status]') !== null ||
            node.closest('[data-testid*="cot" i], [data-testid*="reasoning" i], [class*="reasoning" i], [class*="thinking" i]') !== null
          ) : [];
          const statusRoots = assistantTurn ? Array.from(assistantTurn.querySelectorAll('[data-streaming-response-status]')) : [];
          const reasoningParts = [...commentaryRoots, ...statusRoots].map(visibleText).filter(Boolean);
          const fullReasoningText = Array.from(new Set(reasoningParts)).join('\\n').trim();
          const reasoningText = textLimit && fullReasoningText.length > textLimit
            ? fullReasoningText.slice(-textLimit)
            : fullReasoningText;
          let userCount = document.querySelectorAll('section[data-turn="user"]').length;
          if (!userCount) {{
            const users = [];
            for (const node of document.querySelectorAll('[data-message-author-role="user"], user-message')) {{
              const root = node.closest('article, section') || node;
              if (!users.includes(root)) users.push(root);
            }}
            userCount = users.length;
          }}
          const body = String(document.body?.innerText || '').slice(0, 5000);
          const frames = Array.from(document.querySelectorAll('iframe')).map(f => String(f.src || ''));
          const captcha = frames.some(src => /turnstile|arkose/i.test(src)) || /verify you are human|human verification/i.test(body);
          const rateLimited = /请求过于频繁|too many requests|temporarily limited your access|rate limit/i.test(body);
          const loginControl = Array.from(document.querySelectorAll('button, a')).some(node => /^(log in|sign in|登录|登陆)$/i.test(String(node.innerText || node.textContent || '').trim()));
          const accountNavigation = !!document.querySelector('a[href="/projects"], a[href*="chatgpt.com/projects"], a[href="/scheduled"], a[href*="chatgpt.com/scheduled"]');
          const profileControl = Array.from(document.querySelectorAll('button')).some(node => /profile|个人资料|账户|account/i.test(String(node.getAttribute('aria-label') || node.innerText || '')));
          const authenticated = accountNavigation || profileControl;
          const login = location.pathname.startsWith('/auth/') || !!document.querySelector('input[type="password"], input[autocomplete="current-password"]') || (loginControl && !authenticated);
          return {{
            url: location.href,
            title: document.title,
            authenticated,
            promptVisible: !!promptEl,
            promptLength,
            assistantCount: assistants.length,
            assistantText,
            assistantTextLength: fullAssistantText.length,
            generatedImageSrc,
            generatedFileKey,
            reasoningText,
            reasoningTextLength: fullReasoningText.length,
            userCount,
            generating,
            blocked: captcha || login || rateLimited,
            kind: captcha ? 'captcha' : (login ? 'login' : (rateLimited ? 'rate_limited' : null))
          }};
        }}'''

    @staticmethod
    def _click_send_script():
        send = json.dumps(SEND_SELECTOR)
        stop = json.dumps(STOP_SELECTOR)
        return f'''() => {{
          const visible = (n) => {{ const r=n.getBoundingClientRect(); const s=getComputedStyle(n); return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'; }};
          if (Array.from(document.querySelectorAll({stop})).some(visible)) return false;
          const button = Array.from(document.querySelectorAll({send})).find(n => visible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true');
          if (!button) return false;
          button.focus();
          return document.activeElement === button;
        }}'''

    @staticmethod
    def _click_stop_script():
        stop = json.dumps(STOP_SELECTOR)
        return f'''() => {{
          const visible = (n) => {{ const r=n.getBoundingClientRect(); const s=getComputedStyle(n); return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'; }};
          const button = Array.from(document.querySelectorAll({stop})).find(visible);
          if (!button) return false;
          button.focus();
          return document.activeElement === button;
        }}'''
