from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from cfr.control import CfrSupervisor, LocalControlServer
from cfr.feishu.credentials import resolve_cfr_config_dir


LOGGER = logging.getLogger(__name__)
DESKTOP_MUTEX_NAME = 'Local\\CFR.Desktop.SingleInstance'


class ResilientFileHandler(RotatingFileHandler):
    """Best-effort desktop logging that can never break CFR execution."""

    def emit(self, record):
        try:
            if self.stream is None:
                Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
            super().emit(record)
        except OSError:
            # Telemetry must never become a control-path failure. A later
            # record gets another chance after the filesystem recovers.
            try:
                if self.stream is not None:
                    self.stream.close()
            except OSError:
                pass
            self.stream = None


class DesktopInstanceLock:
    """Process-lifetime Windows mutex; other platforms need no extra lock."""

    def __init__(self, name=DESKTOP_MUTEX_NAME, *, kernel32=None):
        self.name = name
        self.kernel32 = kernel32
        self.handle = None

    def acquire(self):
        if sys.platform != 'win32':
            return True
        if self.kernel32 is None:
            import ctypes
            self.kernel32 = ctypes.windll.kernel32
        self.kernel32.SetLastError(0)
        handle = self.kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            raise OSError('CFR desktop instance mutex could not be created')
        if self.kernel32.GetLastError() == 183:
            self.kernel32.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle is not None:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


class EmbeddedChatHost:
    """CFR-owned hidden ChatGPT automation page sharing the main WebView2 environment."""

    def __init__(self, window, *, browser_url: str, state_dir: Path, storage_path: Path):
        self.window = window
        self.browser_url = browser_url
        self.state_dir = Path(state_dir)
        self.storage_path = Path(storage_path)
        self.available = False
        self._shutdown = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self._control_webview = None
        self._chat_webview = None

    def start(self, timeout=10.0):
        """Attach a persistent ChatGPT WebView2 page inside the CFR main form."""
        if self._shutdown.is_set():
            return False
        deadline = time.monotonic() + timeout
        native = getattr(self.window, 'native', None)
        while native is None and not self._shutdown.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)
            native = getattr(self.window, 'native', None)
        if native is None:
            self._error = 'CFR main WebView2 window did not initialize before timeout'
            return False

        try:
            import clr

            clr.AddReference('System.Windows.Forms')
            clr.AddReference('System.Drawing')
            from System import Action, Uri
            from System.Windows.Forms import DockStyle
            from Microsoft.Web.WebView2.WinForms import WebView2
        except Exception as exc:
            self._error = f'{type(exc).__name__}: {exc}'
            LOGGER.exception('embedded ChatGPT tab dependencies are unavailable')
            return False

        def fail(exc):
            self._error = f'{type(exc).__name__}: {exc}'
            self.available = False
            self._ready.set()

        def initialize_chat_view(main_webview, chat_webview):
            try:
                core = main_webview.CoreWebView2
                if core is None:
                    return False

                def on_chat_ready(_sender, args):
                    try:
                        if not args.IsSuccess:
                            raise RuntimeError('ChatGPT WebView2 initialization failed')
                        chat_webview.Source = Uri('https://chatgpt.com/')
                        self.available = True
                    except Exception as exc:
                        fail(exc)
                        return
                    self._ready.set()

                chat_webview.CoreWebView2InitializationCompleted += on_chat_ready
                chat_webview.EnsureCoreWebView2Async(core.Environment)
                return True
            except Exception as exc:
                fail(exc)
                return True

        def attach_pages():
            if self._chat_webview is not None:
                self._ready.set()
                return
            try:
                main_webview = getattr(native, 'webview', None)
                if main_webview is None:
                    raise RuntimeError('CFR main WebView2 control is unavailable')
                chat_webview = WebView2()
                chat_webview.Dock = DockStyle.Fill
                chat_webview.Visible = False
                main_webview.Dock = DockStyle.Fill
                native.Controls.Add(chat_webview)
                main_webview.BringToFront()
                self._control_webview = main_webview
                self._chat_webview = chat_webview
                self._select_page(False)

                if initialize_chat_view(main_webview, chat_webview):
                    return

                def on_main_ready(_sender, args):
                    if not args.IsSuccess:
                        fail(RuntimeError('CFR main WebView2 initialization failed'))
                        return
                    initialize_chat_view(main_webview, chat_webview)

                main_webview.CoreWebView2InitializationCompleted += on_main_ready
            except Exception as exc:
                fail(exc)

        try:
            if getattr(native, 'InvokeRequired', False):
                native.Invoke(Action(attach_pages))
            else:
                attach_pages()
        except Exception as exc:
            fail(exc)

        remaining = max(0.0, deadline - time.monotonic())
        if not self._ready.wait(remaining):
            self._error = 'ChatGPT WebView2 initialization timed out'
            return False
        return self.available

    def _select_page(self, chat: bool):
        native = getattr(self.window, 'native', None)
        control = self._control_webview
        chat_webview = self._chat_webview
        if native is None or control is None or chat_webview is None:
            return
        try:
            def select():
                control.Visible = not chat
                chat_webview.Visible = chat
                (chat_webview if chat else control).BringToFront()

            if getattr(native, 'InvokeRequired', False):
                from System import Action
                native.BeginInvoke(Action(select))
            else:
                select()
        except Exception:
            LOGGER.exception('embedded ChatGPT page could not be selected')

    def show(self):
        """Temporarily reveal the automation page only for an explicit login flow."""
        if not self.available:
            raise RuntimeError(self._error or 'embedded ChatGPT page is unavailable')
        self._select_page(True)
        self.window.show()

    def show_control_center(self):
        self._select_page(False)
        self.window.show()

    def hide_automation_page(self):
        """Return WebView ownership to Control Center without changing window visibility."""
        self._select_page(False)

    def stop(self):
        self._shutdown.set()
        chat_webview = self._chat_webview
        native = getattr(self.window, 'native', None)
        try:
            if chat_webview is not None and native is not None:
                def dispose():
                    chat_webview.Dispose()

                if getattr(native, 'InvokeRequired', False):
                    from System import Action
                    native.Invoke(Action(dispose))
                else:
                    dispose()
        except Exception:
            pass
        self._control_webview = None
        self._chat_webview = None
        self.available = False

    def safe_dict(self):
        return {
            'available': bool(self.available),
            'browser_url': self.browser_url,
            'state_dir': str(self.state_dir),
            'storage_path': str(self.storage_path),
            'embedded_in_main_window': True,
            'automation_hidden': True,
        }


class WindowsTray:
    """Minimal Windows tray host using pywebview's existing WinForms runtime."""

    def __init__(self, window, *, title='CFR', chat_host=None):
        self.window = window
        self.title = title
        self.chat_host = chat_host
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._exit_requested = threading.Event()
        self._thread = None
        self._available = False
        self._error = None
        self._hide_pending = threading.Event()

    @property
    def available(self):
        return self._available

    @property
    def exit_requested(self):
        return self._exit_requested.is_set()

    def start(self):
        if sys.platform != 'win32':
            return False
        if self._thread is not None:
            return self._available
        self._thread = threading.Thread(target=self._run, name='cfr-desktop-tray', daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        if self._error is not None:
            LOGGER.warning('desktop tray unavailable: %s', self._error)
        return self._available

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def show_window(self, *_args):
        try:
            if self.chat_host is not None:
                self.chat_host.show_control_center()
            self.window.show()
        except Exception:
            LOGGER.exception('desktop tray could not show CFR window')

    def request_exit(self, *_args):
        self._exit_requested.set()
        try:
            if self.chat_host is not None:
                self.chat_host.stop()
            self.window.destroy()
        except Exception:
            LOGGER.exception('desktop tray could not close CFR window')
            self._stop.set()

    def on_window_closing(self):
        if self.exit_requested or not self.available:
            return True
        if not self._hide_pending.is_set():
            self._hide_pending.set()
            timer = threading.Timer(0.05, self._hide_after_cancelled_close)
            timer.daemon = True
            timer.start()
        # pywebview cancels closing when a closing handler returns False. The
        # deferred hide avoids WinForms making the just-cancelled form visible
        # again after this callback returns.
        return False

    def _hide_after_cancelled_close(self):
        try:
            if not self.exit_requested and self.available:
                self.window.hide()
        except Exception:
            LOGGER.exception('desktop tray could not hide CFR window')
        finally:
            self._hide_pending.clear()

    def _run(self):
        notify = None
        menu = None
        extracted_icon = None
        try:
            import clr

            clr.AddReference('System.Drawing')
            clr.AddReference('System.Windows.Forms')
            from System.Drawing import Icon, SystemIcons
            from System.Windows.Forms import Application, ContextMenuStrip, NotifyIcon, ToolStripMenuItem, ToolStripSeparator

            notify = NotifyIcon()
            try:
                # The packaged CFR.exe embeds the approved product icon. Using
                # the executable resource here keeps Explorer/taskbar/tray on
                # one Windows-native icon representation and avoids decoder
                # differences for palette-based ICO files.
                extracted_icon = Icon.ExtractAssociatedIcon(sys.executable)
            except Exception:
                extracted_icon = None
            notify.Icon = extracted_icon or SystemIcons.Application
            notify.Text = self.title[:63]

            menu = ContextMenuStrip()
            open_item = ToolStripMenuItem('打开 CFR')
            exit_item = ToolStripMenuItem('退出 CFR')
            open_item.Click += self.show_window
            exit_item.Click += self.request_exit
            notify.DoubleClick += self.show_window
            menu.Items.Add(open_item)
            menu.Items.Add(ToolStripSeparator())
            menu.Items.Add(exit_item)
            notify.ContextMenuStrip = menu
            notify.Visible = True
            self._available = True
            self._ready.set()

            while not self._stop.wait(0.05):
                Application.DoEvents()
        except Exception as exc:
            self._error = f'{type(exc).__name__}: {exc}'
            LOGGER.exception('desktop tray startup failed')
        finally:
            self._available = False
            self._ready.set()
            if notify is not None:
                try:
                    notify.Visible = False
                    notify.Dispose()
                except Exception:
                    pass
            if menu is not None:
                try:
                    menu.Dispose()
                except Exception:
                    pass
            if extracted_icon is not None:
                try:
                    extracted_icon.Dispose()
                except Exception:
                    pass


def _activate_existing_window() -> None:
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        window = ctypes.windll.user32.FindWindowW(None, 'CFR')
        if window:
            ctypes.windll.user32.ShowWindow(window, 9)
            ctypes.windll.user32.SetForegroundWindow(window)
    except Exception:
        pass


def resource_root() -> Path:
    bundled = getattr(sys, '_MEIPASS', None)
    return Path(bundled) if bundled else Path(__file__).resolve().parents[2]


def _configure_desktop_logging(data_dir: Path):
    log_dir = data_dir / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / 'desktop.log'
    handler = ResilientFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding='utf-8',
        delay=True,
    )
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    # HTTP request success logs can contain local identifiers and generate
    # significant disk churn during dashboard polling. CFR keeps its own
    # lifecycle/trace logs; third-party HTTP libraries only need warnings here.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    return path, handler, previous_level


def _show_fatal_error(message: str) -> None:
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, 'CFR startup failed', 0x10)
    except Exception:
        pass


def _wait_control_server(url: str, timeout=3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f'{url}/api/v1/status', timeout=0.5):
                return
        except HTTPError as error:
            if error.code == 403:
                return
        except (OSError, URLError):
            pass
        time.sleep(0.05)
    raise RuntimeError('CFR Control API did not become ready')


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _autostart_runtime(supervisor, closing: threading.Event) -> None:
    """Start optional runtime only while the desktop still owns the process."""
    try:
        if closing.is_set():
            return
        setup = supervisor.setup_state()
        if closing.is_set() or not setup.get('ready'):
            return
        if setup.get('selected_surface') == 'chat':
            state = supervisor.start_browser_bridge()
            if closing.is_set() or not state.get('available'):
                return
        if closing.is_set():
            return
        result = supervisor.start_feishu()
        if result.status != 'ok':
            LOGGER.warning('desktop autostart did not start Feishu: %s %s', result.error_code, result.message)
    except Exception:
        LOGGER.exception('desktop runtime autostart failed')


def run() -> int:
    instance = DesktopInstanceLock()
    if not instance.acquire():
        _activate_existing_window()
        return 0
    log_path = None
    log_handler = None
    previous_log_level = None
    server = None
    supervisor = None
    tray = None
    chat_host = None
    closing = threading.Event()
    autostart_done = threading.Event()
    try:
        data_dir = resolve_cfr_config_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path, log_handler, previous_log_level = _configure_desktop_logging(data_dir)
        try:
            import webview
        except ImportError as exc:
            raise RuntimeError('CFR Desktop requires the desktop optional dependency: pip install -e ".[desktop,feishu]"') from exc

        root = resource_root()
        web_root = root / 'm3_control' / 'dist'
        if not (web_root / 'index.html').is_file():
            raise RuntimeError('Bundled CFR Control Center UI is missing')

        supervisor = CfrSupervisor(project_root=root, database=data_dir / 'cfr.sqlite3')
        server = LocalControlServer(supervisor, port=0, web_root=web_root).start()
        _wait_control_server(server.url)
        remote_debugging_port = _reserve_loopback_port()
        webview.settings['REMOTE_DEBUGGING_PORT'] = remote_debugging_port
        webview.settings['ALLOW_DOWNLOADS'] = True
        webview_storage = data_dir / 'webview2'
        window = webview.create_window(
            'CFR',
            server.bootstrap_url,
            width=1280,
            height=860,
            min_size=(900, 640),
            text_select=True,
        )
        chat_host = EmbeddedChatHost(
            window,
            browser_url=f'http://127.0.0.1:{remote_debugging_port}',
            state_dir=data_dir / 'browser' / 'embedded',
            storage_path=webview_storage,
        )
        supervisor.attach_embedded_chat_host(chat_host)
        tray = WindowsTray(window, chat_host=chat_host)
        window.events.closing += tray.on_window_closing
        tray.start()

        def autostart():
            try:
                if not chat_host.start():
                    LOGGER.warning('embedded ChatGPT page unavailable: %s', chat_host._error or 'unknown error')
                supervisor.attach_embedded_chat_host(chat_host)
                _autostart_runtime(supervisor, closing)
            finally:
                autostart_done.set()

        webview.start(
            autostart,
            private_mode=False,
            storage_path=str(webview_storage),
        )
        return 0
    except Exception as exc:
        LOGGER.critical('desktop startup failed\n%s', traceback.format_exc())
        detail = f'\n\nLog: {log_path}' if log_path is not None else ''
        _show_fatal_error(f'{type(exc).__name__}: {exc}{detail}')
        return 1
    finally:
        closing.set()
        if supervisor is not None:
            try:
                shutdown = supervisor.shutdown()
                if shutdown.get('status') != 'ok':
                    LOGGER.warning('desktop supervisor shutdown degraded: %s', ','.join(shutdown.get('errors') or ()))
            except Exception:
                LOGGER.exception('desktop supervisor shutdown failed')
        autostart_done.wait(timeout=2.0)
        if tray is not None:
            tray.stop()
        if chat_host is not None:
            chat_host.stop()
        if server is not None:
            try:
                server.stop()
            except Exception:
                LOGGER.exception('desktop control server cleanup failed')
        if log_handler is not None:
            root_logger = logging.getLogger()
            root_logger.removeHandler(log_handler)
            log_handler.close()
            if previous_log_level is not None:
                root_logger.setLevel(previous_log_level)
        instance.release()


if __name__ == '__main__':
    raise SystemExit(run())
