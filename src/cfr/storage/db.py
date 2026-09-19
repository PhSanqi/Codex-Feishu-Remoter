import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

from cfr.core.models import BindingRecord, ThreadRef


INITIALIZED_FILE_CACHE_LIMIT = 128
MAX_EVENT_DEDUPE_ROWS = 100_000
EVENT_DEDUPE_MAX_AGE_SECONDS = 90 * 24 * 60 * 60


class BindingStore:
    _initialization_lock: ClassVar[threading.Lock] = threading.Lock()
    _initialized_files: ClassVar[dict[str, tuple[int, int]]] = {}

    def __init__(self, path):
        self.db_path = Path(path)
        self._memory = str(path) == ':memory:'
        self._memory_conn = sqlite3.connect(':memory:', check_same_thread=False) if self._memory else None
        self._memory_lock = threading.RLock()
        if str(self.db_path) != ':memory:' and self.db_path.parent != Path('.'):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_initialized()

    def _file_identity(self):
        try:
            stat = self.db_path.resolve().stat()
        except OSError:
            return None
        return stat.st_dev, stat.st_ino

    def _ensure_initialized(self):
        if self._memory:
            self._initialize()
            return
        key = str(self.db_path.resolve())
        with self._initialization_lock:
            identity = self._file_identity()
            if identity is not None and self._initialized_files.get(key) == identity:
                return
            self._initialize()
            identity = self._file_identity()
            if identity is not None:
                if key not in self._initialized_files and len(self._initialized_files) >= INITIALIZED_FILE_CACHE_LIMIT:
                    self._initialized_files.pop(next(iter(self._initialized_files)))
                self._initialized_files[key] = identity

    def _connect(self):
        if self._memory:
            if self._memory_conn is None:
                raise RuntimeError('In-memory BindingStore connection is unavailable')
            connection = self._memory_conn
        else:
            connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.execute('pragma busy_timeout=5000')
        if not self._memory:
            connection.execute('pragma synchronous=normal')
        return connection

    @contextmanager
    def _connection(self):
        conn = self._connect()
        if self._memory:
            self._memory_lock.acquire()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if self._memory:
                self._memory_lock.release()
            if not self._memory:
                conn.close()

    def _initialize(self):
        with self._connection() as conn:
            if not self._memory:
                conn.execute('pragma journal_mode=wal').fetchone()
            conn.executescript('''
                create table if not exists projects(id integer primary key, name text);
                create table if not exists codex_bindings(
                    id integer primary key,
                    thread_id text unique not null,
                    thread_name text,
                    cwd text not null,
                    rollout_path text,
                    last_seen_turn_id text,
                    desktop_sync_state text default 'up_to_date',
                    created_at real,
                    updated_at real,
                    active_turn_id text,
                    writer_state text default 'idle'
                );
                create table if not exists event_dedupe(event_key text primary key, created_at real);
                create index if not exists idx_codex_bindings_updated on codex_bindings(updated_at);
                create index if not exists idx_event_dedupe_created on event_dedupe(created_at);
            ''')
            columns = {row[1] for row in conn.execute('pragma table_info(codex_bindings)')}
            if 'last_rollout_byte_offset' not in columns:
                conn.execute('alter table codex_bindings add column last_rollout_byte_offset integer default 0')
                if 'last_rollout_offset' in columns:
                    conn.execute('update codex_bindings set last_rollout_byte_offset=coalesce(last_rollout_offset, 0)')
            if 'active_turn_id' not in columns:
                conn.execute('alter table codex_bindings add column active_turn_id text')
            if 'writer_state' not in columns:
                conn.execute("alter table codex_bindings add column writer_state text default 'idle'")
            for column, kind in (
                ('observed_model', 'text'),
                ('observed_reasoning_effort', 'text'),
                ('observed_service_tier', 'text'),
                ('observed_settings_at', 'real'),
            ):
                if column not in columns:
                    conn.execute(f'alter table codex_bindings add column {column} {kind}')
            conn.execute('''
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
            ''')
            conn.execute('''
                create table if not exists cfr_thread_runtime_lease_generations(
                    thread_id text primary key,
                    generation integer not null
                )
            ''')

    @staticmethod
    def _record(row):
        if row is None:
            return None
        return BindingRecord(
            thread_id=row[0],
            thread_name=row[1],
            cwd=Path(row[2]),
            rollout_path=Path(row[3]) if row[3] else None,
            last_rollout_byte_offset=row[4] or 0,
            last_seen_turn_id=row[5],
            desktop_sync_state=row[6] or 'up_to_date',
            active_turn_id=row[7],
            writer_state=row[8] or 'idle',
            observed_model=row[9],
            observed_reasoning_effort=row[10],
            observed_service_tier=row[11],
            observed_settings_at=row[12],
            created_at=row[13],
            updated_at=row[14],
        )

    def upsert_binding(self, binding: BindingRecord | ThreadRef):
        now = time.time()
        thread_id = binding.thread_id
        name = getattr(binding, 'thread_name', None) or getattr(binding, 'name', None)
        cwd = str(binding.cwd)
        rollout_path = str(binding.rollout_path) if binding.rollout_path else ''
        with self._connection() as conn:
            conn.execute('''
                insert into codex_bindings(thread_id,thread_name,cwd,rollout_path,desktop_sync_state,created_at,updated_at)
                values(?,?,?,?,?,?,?)
                on conflict(thread_id) do update set
                    thread_name=excluded.thread_name,
                    cwd=excluded.cwd,
                    rollout_path=case when excluded.rollout_path='' then codex_bindings.rollout_path else excluded.rollout_path end,
                    updated_at=excluded.updated_at
            ''', (thread_id, name, cwd, rollout_path, 'up_to_date', now, now))

    bind = upsert_binding

    def get_binding(self, thread_id):
        with self._connection() as conn:
            row = conn.execute('''
                select thread_id,thread_name,cwd,rollout_path,last_rollout_byte_offset,
                       last_seen_turn_id,desktop_sync_state,active_turn_id,writer_state,
                       observed_model,observed_reasoning_effort,observed_service_tier,observed_settings_at,
                       created_at,updated_at
                from codex_bindings where thread_id=?
            ''', (thread_id,)).fetchone()
        return self._record(row)

    get = get_binding

    def list_bindings(self, limit=None):
        query = '''
            select thread_id,thread_name,cwd,rollout_path,last_rollout_byte_offset,
                   last_seen_turn_id,desktop_sync_state,active_turn_id,writer_state,
                   observed_model,observed_reasoning_effort,observed_service_tier,observed_settings_at,
                   created_at,updated_at
            from codex_bindings order by updated_at desc
        '''
        with self._connection() as conn:
            if limit is None:
                rows = conn.execute(query).fetchall()
            else:
                rows = conn.execute(query + ' limit ?', (max(0, int(limit)),)).fetchall()
        return [self._record(row) for row in rows]

    list = list_bindings

    def list_rollout_paths(self):
        """Return only persisted native rollout paths for bounded storage telemetry."""
        with self._connection() as conn:
            rows = conn.execute(
                "select rollout_path from codex_bindings where rollout_path is not null and rollout_path != ''"
            ).fetchall()
        return [Path(row[0]) for row in rows]

    def has_busy_bindings(self):
        """Return whether any CFR-known thread can be disrupted by a Desktop restart."""
        with self._connection() as conn:
            row = conn.execute('''
                select 1 from codex_bindings
                where active_turn_id is not null or coalesce(writer_state, 'idle') != 'idle'
                limit 1
            ''').fetchone()
        return row is not None

    def update_rollout_offset(self, thread_id, value):
        with self._connection() as conn:
            conn.execute('update codex_bindings set last_rollout_byte_offset=?,updated_at=? where thread_id=?', (value, time.time(), thread_id))

    offset = update_rollout_offset

    def update_last_seen_turn(self, thread_id, turn_id):
        with self._connection() as conn:
            conn.execute('update codex_bindings set last_seen_turn_id=?,updated_at=? where thread_id=?', (turn_id, time.time(), thread_id))

    seen_turn = update_last_seen_turn

    def update_desktop_sync_state(self, thread_id, state):
        with self._connection() as conn:
            conn.execute('update codex_bindings set desktop_sync_state=?,updated_at=? where thread_id=?', (state, time.time(), thread_id))

    def update_observed_settings(self, thread_id, settings, observed_at=None):
        columns = {
            'model': 'observed_model',
            'effort': 'observed_reasoning_effort',
            'service_tier': 'observed_service_tier',
        }
        values = [(columns[key], settings[key]) for key in columns if key in settings]
        if not values:
            return
        values.append(('observed_settings_at', time.time() if observed_at is None else observed_at))
        assignments = ','.join(f'{column}=?' for column, _ in values)
        with self._connection() as conn:
            conn.execute(
                f'update codex_bindings set {assignments} where thread_id=?',
                (*[value for _, value in values], thread_id),
            )

    def update_rollout_path(self, thread_id, path):
        with self._connection() as conn:
            conn.execute('update codex_bindings set rollout_path=?,updated_at=? where thread_id=?', (str(path), time.time(), thread_id))

    def set_active_turn(self, thread_id, turn_id, state='cfr_active'):
        with self._connection() as conn:
            conn.execute('update codex_bindings set active_turn_id=?,writer_state=?,last_seen_turn_id=?,updated_at=? where thread_id=?', (turn_id, state, turn_id, time.time(), thread_id))

    set_turn = set_active_turn

    def clear_active_turn(self, thread_id, state='idle'):
        with self._connection() as conn:
            conn.execute('update codex_bindings set active_turn_id=null,writer_state=?,updated_at=? where thread_id=?', (state, time.time(), thread_id))

    clear_turn = clear_active_turn

    def set_writer_state(self, thread_id, state):
        with self._connection() as conn:
            conn.execute('update codex_bindings set writer_state=?,updated_at=? where thread_id=?', (state, time.time(), thread_id))

    def seen_event(self, key):
        try:
            with self._connection() as conn:
                conn.execute('insert into event_dedupe values(?,?)', (key, time.time()))
            return False
        except sqlite3.IntegrityError:
            return True

    seen = seen_event

    def prune_event_dedupe(
        self,
        *,
        max_rows=MAX_EVENT_DEDUPE_ROWS,
        max_age_seconds=EVENT_DEDUPE_MAX_AGE_SECONDS,
        now=None,
    ):
        """Bound CLI rollout-event history without touching native rollouts.

        The persisted rollout byte offset remains the primary replay boundary.
        This table is only a secondary duplicate guard for visible CLI events,
        so retaining the newest 100k rows or 90 days is sufficient while
        preventing unbounded CFR-owned SQLite growth.
        """
        max_rows = max(1, int(max_rows))
        max_age_seconds = max(0.0, float(max_age_seconds))
        cutoff = (time.time() if now is None else float(now)) - max_age_seconds
        with self._connection() as conn:
            before = conn.execute('select count(*) from event_dedupe').fetchone()[0]
            conn.execute('delete from event_dedupe where created_at < ?', (cutoff,))
            remaining = conn.execute('select count(*) from event_dedupe').fetchone()[0]
            excess = max(0, remaining - max_rows)
            if excess:
                conn.execute(
                    '''delete from event_dedupe where event_key in (
                           select event_key from event_dedupe
                           order by created_at,event_key limit ?
                       )''',
                    (excess,),
                )
            after = conn.execute('select count(*) from event_dedupe').fetchone()[0]
        return {'before': before, 'after': after, 'deleted': before - after}

    def close(self):
        if self._memory_conn:
            self._memory_conn.close()
            self._memory_conn = None
