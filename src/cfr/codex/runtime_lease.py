from __future__ import annotations

from dataclasses import dataclass
import os
import errno
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import logging

from cfr.core.models import StructuredError


LOGGER = logging.getLogger(__name__)


LEASE_TABLE_SQL = '''
create table if not exists cfr_thread_runtime_leases(
    thread_id text primary key,
    lease_id text not null,
    owner_instance_id text not null,
    owner_pid integer,
    generation integer not null,
    acquired_at real not null,
    heartbeat_at real not null,
    expires_at real not null
)
;
create table if not exists cfr_thread_runtime_lease_generations(
    thread_id text primary key,
    generation integer not null
)
'''


@dataclass
class CfrThreadRuntimeLease:
    thread_id: str
    lease_id: str
    owner_instance_id: str
    owner_pid: int
    generation: int
    acquired_at: float
    heartbeat_at: float
    expires_at: float
    lease_lost: bool = False


class CfrThreadRuntimeLeaseManager:
    """Durable CFR-internal coordination scoped to one native Codex thread.

    ``:memory:`` is intentionally supported for isolated unit tests only. Each
    SQLite connection gets a separate in-memory database, so it must never be
    used when coordination needs to cross threads or processes.
    """

    def __init__(self, database, instance_id=None, owner_pid=None, ttl=60.0, heartbeat_interval=10.0, clock=None, id_factory=None):
        self.database = Path(database) if str(database) != ':memory:' else ':memory:'
        self._memory_uri = f'file:cfr-runtime-{uuid.uuid4().hex}?mode=memory&cache=shared' if self.database == ':memory:' else None
        self._memory_anchor = sqlite3.connect(self._memory_uri, uri=True, timeout=5.0, check_same_thread=False) if self._memory_uri else None
        self.instance_id = instance_id or str(uuid.uuid4())
        self.owner_pid = owner_pid if owner_pid is not None else os.getpid()
        self.ttl = float(ttl)
        self.heartbeat_interval = float(heartbeat_interval)
        self.clock = clock or time.time
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._heartbeat_threads: dict[str, tuple[threading.Event, threading.Thread]] = {}
        self._heartbeat_lock = threading.RLock()
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _connect(self):
        if self.database == ':memory:':
            connection = sqlite3.connect(self._memory_uri, uri=True, timeout=5.0, check_same_thread=False)
        else:
            if self.database.parent != Path('.'):
                self.database.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.database, timeout=5.0, check_same_thread=False)
        connection.execute('pragma busy_timeout=5000')
        if self.database != ':memory:':
            connection.execute('pragma synchronous=normal')
        return connection

    @staticmethod
    def _ensure_schema(connection):
        connection.executescript(LEASE_TABLE_SQL)

    def _ensure_schema_once(self, connection):
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            if self.database != ':memory:':
                connection.execute('pragma journal_mode=wal').fetchone()
            self._ensure_schema(connection)
            connection.commit()
            self._schema_ready = True

    @staticmethod
    def _pid_is_alive(pid) -> bool:
        """Conservatively determine whether a local lease owner still exists."""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return True
        if pid <= 0:
            return True
        if pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as exc:
            return False if exc.errno == errno.ESRCH else True
        return True

    def acquire(self, thread_id: str) -> CfrThreadRuntimeLease:
        now = float(self.clock())
        lease_id = self.id_factory()
        connection = self._connect()
        try:
            self._ensure_schema_once(connection)
            connection.execute('begin immediate')
            row = connection.execute(
                'select lease_id, owner_instance_id, owner_pid, generation, acquired_at, heartbeat_at, expires_at '
                'from cfr_thread_runtime_leases where thread_id=?',
                (thread_id,),
            ).fetchone()
            if row and now < row[6] and self._pid_is_alive(row[2]):
                connection.rollback()
                raise StructuredError('CFR_RUNTIME_WRITER_ACTIVE', f'{thread_id} has an unexpired CFR runtime lease')
            if row and now < row[6]:
                LOGGER.warning(
                    'CFR_RUNTIME_STALE_OWNER_RECLAIMED thread=%s owner_pid=%s',
                    thread_id,
                    row[2],
                )
            generation_row = connection.execute(
                'select generation from cfr_thread_runtime_lease_generations where thread_id=?',
                (thread_id,),
            ).fetchone()
            generation = (int(generation_row[0]) + 1) if generation_row else 1
            connection.execute(
                '''insert into cfr_thread_runtime_lease_generations(thread_id,generation) values(?,?)
                   on conflict(thread_id) do update set generation=excluded.generation''',
                (thread_id, generation),
            )
            connection.execute(
                '''insert into cfr_thread_runtime_leases
                   (thread_id,lease_id,owner_instance_id,owner_pid,generation,acquired_at,heartbeat_at,expires_at)
                   values(?,?,?,?,?,?,?,?)
                   on conflict(thread_id) do update set
                     lease_id=excluded.lease_id,
                     owner_instance_id=excluded.owner_instance_id,
                     owner_pid=excluded.owner_pid,
                     generation=excluded.generation,
                     acquired_at=excluded.acquired_at,
                     heartbeat_at=excluded.heartbeat_at,
                     expires_at=excluded.expires_at''',
                (thread_id, lease_id, self.instance_id, self.owner_pid, generation, now, now, now + self.ttl),
            )
            connection.commit()
            return CfrThreadRuntimeLease(thread_id, lease_id, self.instance_id, self.owner_pid, generation, now, now, now + self.ttl)
        except StructuredError:
            raise
        except sqlite3.OperationalError as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise StructuredError('CFR_RUNTIME_LEASE_BUSY', str(exc)) from exc
        finally:
            connection.close()

    def heartbeat(self, lease: CfrThreadRuntimeLease) -> bool:
        if lease.lease_lost:
            return False
        now = float(self.clock())
        connection = None
        try:
            connection = self._connect()
            connection.execute('begin immediate')
            cursor = connection.execute(
                '''update cfr_thread_runtime_leases
                   set heartbeat_at=?, expires_at=?
                   where thread_id=? and lease_id=? and generation=? and owner_instance_id=?''',
                (now, now + self.ttl, lease.thread_id, lease.lease_id, lease.generation, lease.owner_instance_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                lease.lease_lost = True
                return False
            connection.commit()
            lease.heartbeat_at = now
            lease.expires_at = now + self.ttl
            return True
        except sqlite3.Error as exc:
            try:
                if connection is not None:
                    connection.rollback()
            except sqlite3.Error:
                pass
            # A busy/temporarily unavailable SQLite writer does not mean that
            # another CFR instance owns the thread.  The durable row remains
            # authoritative until its last confirmed expiry.  Retry on the next
            # heartbeat while that lease is still valid; only fail closed once
            # CFR can no longer prove that its previously acquired lease has not
            # expired.
            if float(self.clock()) < lease.expires_at:
                LOGGER.warning(
                    'CFR_RUNTIME_LEASE_HEARTBEAT_DEFERRED thread=%s type=%s',
                    lease.thread_id,
                    type(exc).__name__,
                )
                return True
            lease.lease_lost = True
            return False
        finally:
            if connection is not None:
                connection.close()

    def release(self, lease: CfrThreadRuntimeLease) -> bool:
        self.stop_heartbeat(lease)
        for attempt in range(3):
            connection = None
            try:
                connection = self._connect()
                connection.execute('begin immediate')
                cursor = connection.execute(
                    '''delete from cfr_thread_runtime_leases
                       where thread_id=? and lease_id=? and generation=? and owner_instance_id=?''',
                    (lease.thread_id, lease.lease_id, lease.generation, lease.owner_instance_id),
                )
                connection.commit()
                return cursor.rowcount == 1
            except sqlite3.Error:
                try:
                    if connection is not None:
                        connection.rollback()
                except sqlite3.Error:
                    pass
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
            finally:
                if connection is not None:
                    connection.close()
        LOGGER.error('CFR_RUNTIME_LEASE_RELEASE_FAILED thread=%s', lease.thread_id)
        return False

    def start_heartbeat(self, lease: CfrThreadRuntimeLease, interval=None):
        interval = max(0.01, self.heartbeat_interval if interval is None else float(interval))
        with self._heartbeat_lock:
            existing = self._heartbeat_threads.get(lease.lease_id)
            if existing and existing[1].is_alive():
                return existing[1]
            stop_event = threading.Event()

        def run():
            while not stop_event.wait(interval):
                if not self.heartbeat(lease):
                    break

        thread = threading.Thread(target=run, name=f'cfr-lease-heartbeat-{lease.thread_id}', daemon=True)
        with self._heartbeat_lock:
            self._heartbeat_threads[lease.lease_id] = (stop_event, thread)
        thread.start()
        return thread

    def stop_heartbeat(self, lease: CfrThreadRuntimeLease, timeout=2.0):
        with self._heartbeat_lock:
            entry = self._heartbeat_threads.pop(lease.lease_id, None)
        if entry:
            event, thread = entry
            event.set()
            thread.join(timeout=timeout)

    def inspect(self):
        connection = self._connect()
        try:
            self._ensure_schema_once(connection)
            rows = connection.execute('select thread_id,lease_id,owner_instance_id,owner_pid,generation,acquired_at,heartbeat_at,expires_at from cfr_thread_runtime_leases').fetchall()
            return [dict(zip(('thread_id', 'lease_id', 'owner_instance_id', 'owner_pid', 'generation', 'acquired_at', 'heartbeat_at', 'expires_at'), row, strict=True)) for row in rows]
        finally:
            connection.close()

    def close(self):
        with self._heartbeat_lock:
            entries = tuple(self._heartbeat_threads.values())
            self._heartbeat_threads.clear()
        for event, _thread in entries:
            event.set()
        for _event, thread in entries:
            if thread.is_alive():
                thread.join(timeout=2.0)
        if self._memory_anchor is not None:
            self._memory_anchor.close()
            self._memory_anchor = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


RuntimeLeaseManager = CfrThreadRuntimeLeaseManager
