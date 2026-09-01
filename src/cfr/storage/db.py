import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cfr.core.models import BindingRecord, ThreadRef


class BindingStore:
    def __init__(self, path):
        self.db_path = Path(path)
        self._memory = str(path) == ':memory:'
        self._memory_conn = sqlite3.connect(':memory:') if self._memory else None
        if str(self.db_path) != ':memory:' and self.db_path.parent != Path('.'):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        return self._memory_conn if self._memory else sqlite3.connect(self.db_path)

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            if not self._memory:
                conn.close()

    def _initialize(self):
        with self._connection() as conn:
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

    def list_bindings(self):
        with self._connection() as conn:
            rows = conn.execute('''
                select thread_id,thread_name,cwd,rollout_path,last_rollout_byte_offset,
                       last_seen_turn_id,desktop_sync_state,active_turn_id,writer_state,
                       observed_model,observed_reasoning_effort,observed_service_tier,observed_settings_at,
                       created_at,updated_at
                from codex_bindings order by updated_at desc
            ''').fetchall()
        return [self._record(row) for row in rows]

    list = list_bindings

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

    def close(self):
        if self._memory_conn:
            self._memory_conn.close()
            self._memory_conn = None
