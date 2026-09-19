from __future__ import annotations

import os
import threading
import time
import uuid

from .store import FeishuStore


class FeishuConnectionLease:
    """Small connection-only owner shared by daemon, setup-only, and doctor."""

    def __init__(self, database, lease_key, ttl=30, owner_instance_id=None):
        self.store = FeishuStore(database)
        self.lease_key = lease_key
        self.ttl = ttl
        self.owner_instance_id = owner_instance_id or f'connection-{uuid.uuid4().hex}'
        self._stop = threading.Event()
        self._heartbeat_thread = None
        self._acquired = False
        self._lost = False
        self._last_confirmed = 0.0
        self._last_error: str | None = None

    def acquire(self):
        if self._acquired and not self._lost:
            return self
        try:
            self.store.acquire_daemon_lease(self.lease_key, self.owner_instance_id, os.getpid(), ttl=self.ttl)
        except Exception:
            self.store.close()
            raise
        self._acquired = True
        self._lost = False
        self._last_confirmed = time.monotonic()
        self._last_error = None
        self._heartbeat_thread = threading.Thread(target=self._heartbeat, name='cfr-feishu-connection-heartbeat', daemon=True)
        self._heartbeat_thread.start()
        return self

    def _heartbeat(self):
        while not self._stop.wait(max(1, self.ttl // 3)):
            try:
                owned = self.store.heartbeat_daemon_lease(self.lease_key, self.owner_instance_id, ttl=self.ttl)
            except Exception as exc:
                self._last_error = type(exc).__name__
                if time.monotonic() - self._last_confirmed < max(1, self.ttl - 1):
                    continue
                self._lost = True
                self._stop.set()
                break
            if not owned:
                self._lost = True
                self._stop.set()
                break
            self._last_confirmed = time.monotonic()
            self._last_error = None

    @property
    def lease_lost(self):
        if not self._acquired:
            return False
        if self._lost:
            return True
        try:
            row = self.store.inspect_daemon_lease(self.lease_key)
        except Exception:
            expired_without_confirmation = time.monotonic() - self._last_confirmed >= max(1, self.ttl - 1)
            if expired_without_confirmation:
                self._lost = True
            return expired_without_confirmation
        return bool(
            not row
            or row.get('owner_instance_id') != self.owner_instance_id
            or float(row.get('expires_at') or 0) <= time.time()
        )

    def release(self):
        self._stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)
        try:
            if self._acquired:
                self.store.release_daemon_lease(self.lease_key, self.owner_instance_id)
        finally:
            self._acquired = False
            self.store.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
