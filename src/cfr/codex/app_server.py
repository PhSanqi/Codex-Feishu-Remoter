from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from typing import Any, Callable
from dataclasses import dataclass

from .launcher import CodexLauncher
from cfr.network import proxy_child_env, resolve_proxy


class AppServerRpcError(Exception):
    def __init__(self, method: str, code: int | str, message: str, data: Any = None):
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)

    def __str__(self) -> str:
        return f'{self.method} failed ({self.code}): {self.message}'


@dataclass(frozen=True)
class ServerRequestResolution:
    """Neutral result returned by a server-request handler."""

    result: Any = None
    error: dict[str, Any] | None = None


class NotificationSubscription:
    def __init__(self, dispatcher, subscription_id, predicate):
        self._dispatcher = dispatcher
        self.subscription_id = subscription_id
        self.predicate = predicate
        self.queue = queue.Queue()

    def get(self, timeout=None):
        return self.queue.get(timeout=timeout)

    def close(self):
        self._dispatcher.unsubscribe(self.subscription_id)


class NotificationDispatcher:
    """Fan-out dispatcher: one subscriber cannot consume another's events."""

    def __init__(self):
        self._lock = threading.RLock()
        self._next_id = 0
        self._subscriptions = {}

    def subscribe(self, predicate: Callable[[dict], bool] | None = None):
        with self._lock:
            self._next_id += 1
            subscription = NotificationSubscription(self, self._next_id, predicate)
            self._subscriptions[subscription.subscription_id] = subscription
            return subscription

    def unsubscribe(self, subscription_id):
        with self._lock:
            self._subscriptions.pop(subscription_id, None)

    def publish(self, message: dict):
        with self._lock:
            subscriptions = list(self._subscriptions.values())
        for subscription in subscriptions:
            try:
                accepted = subscription.predicate is None or subscription.predicate(message)
            except Exception:
                accepted = False
            if accepted:
                subscription.queue.put(message)


class AppServerClient:
    def __init__(self, launcher=None, timeout=30, on_server_request=None, process_env=None, config_overrides=None, codex_home=None):
        self.launcher = launcher or CodexLauncher(config_overrides=config_overrides)
        self.timeout = timeout
        self._id = 0
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.RLock()
        self.notifications = queue.Queue()
        self.dispatcher = NotificationDispatcher()
        self.server_requests = queue.Queue()
        self.stderr = deque(maxlen=100)
        self._server_request_handler = on_server_request or self._reject_server_request
        self._server_request_response_lock = threading.RLock()
        self._resolved_server_request_ids: set[str | int] = set()
        self._inflight_server_request_ids: set[str | int] = set()
        # CFR-owned Codex processes inherit a detected fixed system proxy through
        # standard proxy environment variables. This keeps CFR independent from
        # Codex's experimental `respect_system_proxy` feature. Callers that need
        # an intentionally unmodified environment can pass an explicit dict.
        self.process_env = dict(process_env) if process_env is not None else dict(proxy_child_env(resolve_proxy()) or {})
        self.codex_home = Path(codex_home) if codex_home else None
        self.proc = None
        self._closing = False
        self._stdout_thread = None
        self._stderr_thread = None
        self.started_at = None
        self.close_started_at = None
        self.exited_at = None
        self.close_mode = None
        self.app_server_start_elapsed_ms = None
        self.initialize_elapsed_ms = None
        self.app_server_start_started_at = None
        self.app_server_started_at = None
        self.initialize_started_at = None
        self.initialize_completed_at = None

    @staticmethod
    def _timestamp():
        return datetime.now(timezone.utc).isoformat()

    @property
    def process_id(self) -> int | None:
        return self.proc.pid if self.proc else None

    @property
    def is_running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    @property
    def exit_code(self) -> int | None:
        return self.proc.poll() if self.proc else None

    def lifecycle_snapshot(self):
        return {
            'pid': self.process_id,
            'started': self.started_at is not None,
            'started_at': self.started_at,
            'close_started': self.close_started_at is not None,
            'close_started_at': self.close_started_at,
            'exited': self.exit_code is not None,
            'exited_at': self.exited_at,
            'exit_code': self.exit_code,
            'close_mode': self.close_mode,
        }

    def start(self):
        if self.is_running:
            return self
        started = self.app_server_start_started_at = time.monotonic()
        child_env = dict(self.process_env or {})
        if self.codex_home is not None:
            child_env['CODEX_HOME'] = str(self.codex_home)
        self.proc = subprocess.Popen(
            self.launcher.build_app_server_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            bufsize=1,
            env={**os.environ, **child_env} if child_env else None,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
        )
        self.app_server_started_at = time.monotonic()
        self.app_server_start_elapsed_ms = int((self.app_server_started_at - started) * 1000)
        self._closing = False
        with self._server_request_response_lock:
            self._resolved_server_request_ids.clear()
            self._inflight_server_request_ids.clear()
        self.started_at = self._timestamp()
        self.close_started_at = None
        self.exited_at = None
        self.close_mode = None
        self._stdout_thread = threading.Thread(target=self._read, name='cfr-app-server-stdout', daemon=True)
        self._stderr_thread = threading.Thread(target=self._err, name='cfr-app-server-stderr', daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            initialized = self.initialize_started_at = time.monotonic()
            self.request('initialize', {'clientInfo': {'name': 'cfr', 'version': '0.1'}, 'capabilities': {'experimentalApi': True}})
            self.initialize_completed_at = time.monotonic()
            self.initialize_elapsed_ms = int((self.initialize_completed_at - initialized) * 1000)
            self.notify('initialized', {})
        except Exception:
            self.close()
            raise
        return self

    def _write(self, message: dict):
        if self._closing or not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            raise RuntimeError('app-server is not running')
        payload = json.dumps(message, ensure_ascii=False) + '\n'
        with self._write_lock:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self.notifications.put({'method': 'cfr/malformed', 'params': {'line': line.rstrip()}})
                    continue
                request_id = message.get('id')
                if request_id is not None and ('result' in message or 'error' in message):
                    with self._pending_lock:
                        pending = self._pending.get(request_id)
                    if pending:
                        pending.put(message)
                    continue
                if request_id is not None and message.get('method'):
                    self.server_requests.put(message)
                    threading.Thread(target=self._handle_server_request, args=(message,), daemon=True).start()
                    continue
                if message.get('method'):
                    self.notifications.put(message)
                    self.dispatcher.publish(message)
        finally:
            if not self._closing:
                self._fail_pending(RuntimeError('app-server stdout EOF'))

    def _err(self):
        if not self.proc or not self.proc.stderr:
            return
        for line in self.proc.stderr:
            self.stderr.append(line.rstrip())

    def _handle_server_request(self, message):
        request_id = message.get('id')
        if request_id is None:
            return
        try:
            resolution = self._server_request_handler(message)
            if isinstance(resolution, ServerRequestResolution):
                result, error = resolution.result, resolution.error
            elif isinstance(resolution, dict) and 'error' in resolution and set(resolution).issubset({'error'}):
                result, error = None, resolution['error']
            else:
                result, error = resolution, None
        except Exception as exc:
            result = None
            error = {'code': -32000, 'message': 'CODEX_SERVER_REQUEST_HANDLER_FAILED'}
        self.respond_server_request(request_id, result=result, error=error)

    def _reject_server_request(self, message):
        return ServerRequestResolution(error={'code': -32601, 'message': 'UNSUPPORTED_CODEX_SERVER_REQUEST'})

    def respond_server_request(self, request_id, result=None, error=None):
        with self._server_request_response_lock:
            if request_id in self._resolved_server_request_ids or request_id in self._inflight_server_request_ids:
                return False
            if self._closing or not self.proc or self.proc.poll() is not None:
                return False
            self._inflight_server_request_ids.add(request_id)
        response = {'jsonrpc': '2.0', 'id': request_id}
        response['error' if error is not None else 'result'] = error if error is not None else result
        try:
            self._write(response)
        except Exception:
            with self._server_request_response_lock:
                self._inflight_server_request_ids.discard(request_id)
            return False
        with self._server_request_response_lock:
            self._inflight_server_request_ids.discard(request_id)
            self._resolved_server_request_ids.add(request_id)
        return True

    def server_request_resolution_snapshot(self):
        """Return read-only evidence for the JSON-RPC exactly-once guard."""
        with self._server_request_response_lock:
            return {
                'resolved': tuple(self._resolved_server_request_ids),
                'inflight': tuple(self._inflight_server_request_ids),
            }

    def _fail_pending(self, error):
        with self._pending_lock:
            pending = list(self._pending.values())
        for waiter in pending:
            waiter.put(error)

    def request(self, method, params, timeout=None):
        if self._closing or not self.proc or self.proc.poll() is not None:
            raise RuntimeError('app-server is not running')
        with self._id_lock:
            self._id += 1
            request_id = self._id
        waiter = queue.Queue(1)
        with self._pending_lock:
            self._pending[request_id] = waiter
        try:
            self._write({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
            try:
                response = waiter.get(timeout=timeout or self.timeout)
            except queue.Empty:
                raise TimeoutError(f'{method} timed out')
            if isinstance(response, Exception):
                raise response
            if 'error' in response:
                error = response['error'] or {}
                raise AppServerRpcError(method, error.get('code', 'unknown'), error.get('message', 'RPC error'), error.get('data'))
            return response.get('result')
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method, params):
        self._write({'jsonrpc': '2.0', 'method': method, 'params': params})

    def subscribe(self, predicate=None):
        return self.dispatcher.subscribe(predicate)

    def wait_for_notification(self, method, predicate=None, timeout=None):
        def matches(message):
            if message.get('method') != method:
                return False
            return predicate is None or predicate(message.get('params', {}))

        subscription = self.subscribe(matches)
        try:
            message = subscription.get(timeout=timeout or self.timeout)
            return message.get('params', {})
        except queue.Empty:
            raise TimeoutError(f'{method} timed out')
        finally:
            subscription.close()

    def close(self):
        if not self.proc:
            return
        if self.close_started_at is not None:
            return
        self.close_started_at = self._timestamp()
        self._closing = True
        self._fail_pending(RuntimeError('app-server closed'))
        if self.proc.poll() is not None:
            self.close_mode = 'already_exited'
            self.exited_at = self.exited_at or self._timestamp()
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
            if os.name == 'nt':
                try:
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:
                    pass
                self.close_mode = 'break'
            self.proc.wait(timeout=0.5 if os.name == 'nt' else 2)
            self.close_mode = self.close_mode or 'graceful'
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                self.close_mode = 'kill_tree'
                subprocess.run(
                    ['taskkill', '/PID', str(self.proc.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
                )
                try:
                    self.proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    self.close_mode = 'kill'
                    self.proc.kill()
                    self.proc.wait(timeout=2)
            else:
                self.close_mode = 'terminate'
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.close_mode = 'kill'
                    self.proc.kill()
                    self.proc.wait(timeout=5)
        finally:
            self.exited_at = self.exited_at or self._timestamp()
            for thread in (self._stdout_thread, self._stderr_thread):
                if thread and thread.is_alive():
                    thread.join(timeout=0.1)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    if stream and not stream.closed:
                        stream.close()
                except Exception:
                    pass

    def stderr_tail(self):
        return list(self.stderr)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
