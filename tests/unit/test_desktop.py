import unittest
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cfr.desktop import DesktopInstanceLock, EmbeddedChatHost, ResilientFileHandler, WindowsTray, _autostart_runtime, _configure_desktop_logging, run


class _Kernel:
    def __init__(self, error=0):
        self.error = error
        self.closed = []

    def SetLastError(self, _value):
        pass

    def CreateMutexW(self, *_args):
        return 123

    def GetLastError(self):
        return self.error

    def CloseHandle(self, handle):
        self.closed.append(handle)


class DesktopTests(unittest.TestCase):
    def test_embedded_chat_switches_pages_inside_main_window(self):
        window = Mock()
        window.native = SimpleNamespace(InvokeRequired=False)
        host = EmbeddedChatHost(
            window,
            browser_url='http://127.0.0.1:9223',
            state_dir=Path('C:/CFR/browser/embedded'),
            storage_path=Path('C:/CFR/webview2'),
        )
        control = Mock()
        chat = Mock()
        control.Visible = True
        chat.Visible = False
        host._control_webview = control
        host._chat_webview = chat
        host.available = True
        host.show()
        self.assertFalse(control.Visible)
        self.assertTrue(chat.Visible)
        chat.BringToFront.assert_called_once_with()
        host.show_control_center()
        self.assertTrue(control.Visible)
        self.assertFalse(chat.Visible)
        control.BringToFront.assert_called_once_with()
        self.assertEqual(window.show.call_count, 2)

    def test_windows_build_embeds_and_bundles_cfr_icon(self):
        script = (Path(__file__).resolve().parents[2] / 'scripts' / 'build_windows_exe.ps1').read_text(encoding='utf-8')
        self.assertIn("assets\\cfr_icon_light.ico", script)
        self.assertIn('--icon $icon', script)

    def test_tray_close_hides_window_and_cancels_close(self):
        class Window:
            def __init__(self):
                self.hidden = 0
                self.hidden_event = threading.Event()

            def hide(self):
                self.hidden += 1
                self.hidden_event.set()

        window = Window()
        tray = WindowsTray(window)
        tray._available = True
        self.assertFalse(tray.on_window_closing())
        self.assertTrue(window.hidden_event.wait(0.5))
        self.assertEqual(window.hidden, 1)

    def test_tray_exit_allows_window_close(self):
        window = SimpleNamespace(hide=lambda: (_ for _ in ()).throw(AssertionError('must not hide')))
        tray = WindowsTray(window)
        tray._available = True
        tray._exit_requested.set()
        self.assertTrue(tray.on_window_closing())

    def test_tray_request_exit_marks_exit_before_destroy(self):
        observed = []
        tray = None

        class Window:
            def destroy(self):
                observed.append(tray.exit_requested)

        tray = WindowsTray(Window())
        tray.request_exit()
        self.assertEqual(observed, [True])

    def test_desktop_log_handler_is_removed_without_poisoning_later_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = __import__('logging').getLogger()
            previous_handlers = tuple(root.handlers)
            path, handler, previous_level = _configure_desktop_logging(Path(directory))
            self.assertIsInstance(handler, ResilientFileHandler)
            __import__('logging').getLogger('cfr.test').info('before cleanup')
            root.removeHandler(handler)
            handler.close()
            root.setLevel(previous_level)
            self.assertTrue(path.is_file())
            self.assertEqual(tuple(root.handlers), previous_handlers)
            self.assertEqual(__import__('logging').getLogger('httpx').level, __import__('logging').WARNING)
            self.assertEqual(__import__('logging').getLogger('httpcore').level, __import__('logging').WARNING)

    def test_autostart_does_nothing_after_desktop_closing_begins(self):
        supervisor = SimpleNamespace(
            setup_state=Mock(side_effect=AssertionError('must not inspect setup after close')),
            start_browser_bridge=Mock(),
            start_feishu=Mock(),
        )
        closing = threading.Event()
        closing.set()
        _autostart_runtime(supervisor, closing)
        supervisor.start_browser_bridge.assert_not_called()
        supervisor.start_feishu.assert_not_called()

    def test_chat_autostart_never_starts_feishu_when_browser_is_not_ready(self):
        supervisor = SimpleNamespace(
            setup_state=Mock(return_value={'ready': True, 'selected_surface': 'chat'}),
            start_browser_bridge=Mock(return_value={'available': False, 'status': 'waiting_user'}),
            start_feishu=Mock(),
        )
        _autostart_runtime(supervisor, threading.Event())
        supervisor.start_browser_bridge.assert_called_once_with()
        supervisor.start_feishu.assert_not_called()

    def test_windows_instance_mutex_is_released_by_owner(self):
        kernel = _Kernel()
        lock = DesktopInstanceLock(kernel32=kernel)
        with patch('cfr.desktop.sys.platform', 'win32'):
            self.assertTrue(lock.acquire())
            lock.release()
        self.assertEqual(kernel.closed, [123])

    def test_windows_duplicate_instance_does_not_own_mutex(self):
        kernel = _Kernel(error=183)
        lock = DesktopInstanceLock(kernel32=kernel)
        with patch('cfr.desktop.sys.platform', 'win32'):
            self.assertFalse(lock.acquire())
        self.assertIsNone(lock.handle)
        self.assertEqual(kernel.closed, [123])

    def test_duplicate_desktop_exits_before_starting_control_plane(self):
        with patch('cfr.desktop.DesktopInstanceLock.acquire', return_value=False), patch('cfr.desktop._activate_existing_window') as activate, patch('cfr.desktop.resolve_cfr_config_dir') as config:
            self.assertEqual(run(), 0)
        activate.assert_called_once_with()
        config.assert_not_called()

    def test_control_server_is_stopped_when_window_creation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            web_root = root / 'm3_control' / 'dist'
            web_root.mkdir(parents=True)
            (web_root / 'index.html').write_text('<html></html>', encoding='utf-8')
            data = root / 'data'
            stopped = []

            class Server:
                bootstrap_url = 'http://127.0.0.1:1/?bootstrap=x'
                url = 'http://127.0.0.1:1'

                def start(self):
                    return self

                def stop(self):
                    stopped.append(True)

            webview = SimpleNamespace(
                settings={'REMOTE_DEBUGGING_PORT': None, 'ALLOW_DOWNLOADS': False},
                create_window=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('window failed')),
                start=lambda *_args, **_kwargs: None,
            )
            with patch('cfr.desktop.DesktopInstanceLock.acquire', return_value=True), patch(
                'cfr.desktop.DesktopInstanceLock.release'
            ), patch('cfr.desktop.resolve_cfr_config_dir', return_value=data), patch(
                'cfr.desktop.resource_root', return_value=root
            ), patch('cfr.desktop.CfrSupervisor', return_value=object()), patch(
                'cfr.desktop.LocalControlServer', return_value=Server()
            ), patch('cfr.desktop._wait_control_server'), patch('cfr.desktop._show_fatal_error'), patch.dict(
                'sys.modules', {'webview': webview}
            ):
                self.assertEqual(run(), 1)
        self.assertEqual(stopped, [True])


if __name__ == '__main__':
    unittest.main()
