from __future__ import annotations

import os
import threading
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

    def acquire(self):
        try:
            self.store.acquire_daemon_lease(self.lease_key, self.owner_instance_id, os.getpid(), ttl=self.ttl)
        except Exception:
            self.store.close()
            raise
        self._acquired = True
        self._heartbeat_thread = threading.Thread(target=self._heartbeat, name='cfr-feishu-connection-heartbeat', daemon=True)
        self._heartbeat_thread.start()
        return self

    def _heartbeat(self):
        while not self._stop.wait(max(1, self.ttl // 3)):
            if not self.store.heartbeat_daemon_lease(self.lease_key, self.owner_instance_id, ttl=self.ttl):
                self._stop.set()
                break

    @property
    def lease_lost(self):
        return self._acquired and self._stop.is_set() and not self.store.inspect_daemon_lease(self.lease_key)

    def release(self):
        self._stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)
        if self._acquired:
            self.store.release_daemon_lease(self.lease_key, self.owner_instance_id)
            self._acquired = False
        self.store.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
