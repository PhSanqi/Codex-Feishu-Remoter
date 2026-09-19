from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import ClassVar

from cfr.core.models import StructuredError

from .models import FeishuInboundMessage, FeishuSession, InboxRecord


INBOX_STATES = {'queued', 'running', 'completed', 'failed', 'ignored', 'interrupted_on_restart'}
APPROVAL_STATES = {'pending', 'approved', 'declined', 'cancelled', 'expired', 'orphaned'}
FEEDBACK_RANKS = {
    'PENDING': 0,
    'ACKNOWLEDGED_PROCESSING': 2,
    'APPROVED': 3,
    'DECLINED': 3,
    'EXECUTION_FAILED': 3,
}
INBOX_RETENTION_SECONDS = 30 * 24 * 60 * 60
APPROVAL_RETENTION_SECONDS = 90 * 24 * 60 * 60
ATTACHMENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_TERMINAL_INBOX_ROWS = 10_000
MAX_TERMINAL_REPLY_ROWS = 20_000
MAX_TERMINAL_APPROVAL_ROWS = 5_000
INITIALIZED_FILE_CACHE_LIMIT = 128


class FeishuStore:
    """Connection-per-operation durable state in the CFR-owned SQLite file."""

    _initialization_lock: ClassVar[threading.Lock] = threading.Lock()
    _initialized_files: ClassVar[dict[str, tuple[int, int]]] = {}

    def __init__(self, path):
        self.db_path = Path(path)
        self._memory = str(path) == ':memory:'
        self._memory_conn = sqlite3.connect(':memory:', check_same_thread=False) if self._memory else None
        self._memory_lock = threading.RLock()
        if not self._memory and self.db_path.parent != Path('.'):
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
                raise RuntimeError('In-memory FeishuStore connection is unavailable')
            connection = self._memory_conn
        else:
            connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.execute('pragma busy_timeout=5000')
        if not self._memory:
            connection.execute('pragma synchronous=normal')
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        if self._memory:
            self._memory_lock.acquire()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if self._memory:
                self._memory_lock.release()
            if not self._memory:
                connection.close()

    def _initialize(self):
        with self._connection() as connection:
            if not self._memory:
                connection.execute('pragma journal_mode=wal').fetchone()
            connection.executescript('''
                create table if not exists feishu_inbox(
                    message_id text primary key,
                    event_id text,
                    chat_id text not null,
                    chat_type text not null,
                    sender_open_id text not null,
                    message_type text not null,
                    text_content text,
                    resources_json text,
                    status text not null,
                    received_at real not null,
                    started_at real,
                    completed_at real,
                    response_message_id text,
                    error_code text,
                    error_message text
                );
                create table if not exists feishu_sessions(
                    chat_id text primary key,
                    chat_type text not null,
                    owner_open_id text not null,
                    state text not null,
                    thread_id text unique,
                    pending_cwd text,
                    pending_settings_json text,
                    approval_mode text not null default 'ask',
                    created_at real not null,
                    updated_at real not null
                );
                create table if not exists feishu_surface_state(
                    chat_id text primary key,
                    selected_surface text not null,
                    updated_at real not null
                );
                create table if not exists feishu_chat_bindings(
                    chat_id text primary key,
                    tab_key text not null unique,
                    tab_id text,
                    url text,
                    created_at real not null,
                    updated_at real not null
                );
                create table if not exists feishu_pending_attachments(
                    attachment_id text primary key,
                    chat_id text not null,
                    kind text not null,
                    path text not null,
                    name text not null,
                    source_message_id text,
                    claimed_by_message_id text,
                    claimed_at real,
                    created_at real not null
                );
                create table if not exists feishu_daemon_lease(
                    lease_key text primary key,
                    owner_instance_id text not null,
                    owner_pid integer,
                    acquired_at real not null,
                    heartbeat_at real not null,
                    expires_at real not null
                );
                create table if not exists feishu_replies(
                    reply_key text primary key,
                    message_id text not null,
                    phase text not null,
                    chunk_index integer not null,
                    response_message_id text,
                    state text not null default 'pending',
                    created_at real not null
                );
                create table if not exists feishu_approvals(
                    approval_id text primary key,
                    codex_request_id text,
                    thread_id text not null,
                    turn_id text,
                    kind text not null,
                    requester_open_id text not null,
                    state text not null,
                    decision text,
                    created_at real not null,
                    expires_at real not null,
                    resolved_at real,
                    summary_json text,
                    card_message_id text,
                    feedback_state text not null default 'PENDING',
                    feedback_revision integer not null default 0,
                    feedback_update_failed integer not null default 0,
                    feedback_update_error_code text,
                    feedback_updated_at real
                );
                create index if not exists idx_feishu_inbox_status_received
                    on feishu_inbox(status, received_at);
                create index if not exists idx_feishu_inbox_chat_status_received
                    on feishu_inbox(chat_id, status, received_at);
                create index if not exists idx_feishu_inbox_received
                    on feishu_inbox(received_at);
                create index if not exists idx_feishu_sessions_updated
                    on feishu_sessions(updated_at);
                create index if not exists idx_feishu_surface_updated
                    on feishu_surface_state(updated_at);
                create index if not exists idx_feishu_chat_bindings_updated
                    on feishu_chat_bindings(updated_at);
                create index if not exists idx_feishu_pending_attachments_chat_created
                    on feishu_pending_attachments(chat_id, created_at);
                create index if not exists idx_feishu_replies_state_created
                    on feishu_replies(state, created_at);
                create index if not exists idx_feishu_approvals_request_created
                    on feishu_approvals(codex_request_id, created_at);
                create index if not exists idx_feishu_approvals_turn_state
                    on feishu_approvals(thread_id, turn_id, state, created_at);
                create index if not exists idx_feishu_approvals_state_created
                    on feishu_approvals(state, created_at);
                create index if not exists idx_feishu_approvals_state_thread_turn
                    on feishu_approvals(state, thread_id, turn_id);
                create index if not exists idx_feishu_approvals_created
                    on feishu_approvals(created_at);
                create index if not exists idx_feishu_approvals_state_id
                    on feishu_approvals(state, approval_id);
            ''')
            columns = {row['name'] for row in connection.execute('pragma table_info(feishu_replies)').fetchall()}
            if 'state' not in columns:
                connection.execute("alter table feishu_replies add column state text not null default 'pending'")
            approval_columns = {row['name'] for row in connection.execute('pragma table_info(feishu_approvals)').fetchall()}
            migrations = {
                'card_message_id': 'alter table feishu_approvals add column card_message_id text',
                'feedback_state': "alter table feishu_approvals add column feedback_state text not null default 'PENDING'",
                'feedback_revision': 'alter table feishu_approvals add column feedback_revision integer not null default 0',
                'feedback_update_failed': 'alter table feishu_approvals add column feedback_update_failed integer not null default 0',
                'feedback_update_error_code': 'alter table feishu_approvals add column feedback_update_error_code text',
                'feedback_updated_at': 'alter table feishu_approvals add column feedback_updated_at real',
            }
            for name, statement in migrations.items():
                if name not in approval_columns:
                    connection.execute(statement)
            inbox_columns = {row['name'] for row in connection.execute('pragma table_info(feishu_inbox)').fetchall()}
            if 'resources_json' not in inbox_columns:
                connection.execute('alter table feishu_inbox add column resources_json text')
            session_columns = {row['name'] for row in connection.execute('pragma table_info(feishu_sessions)').fetchall()}
            if 'pending_settings_json' not in session_columns:
                connection.execute('alter table feishu_sessions add column pending_settings_json text')
            if 'approval_mode' not in session_columns:
                connection.execute("alter table feishu_sessions add column approval_mode text not null default 'ask'")
            attachment_columns = {row['name'] for row in connection.execute('pragma table_info(feishu_pending_attachments)').fetchall()}
            if 'source_message_id' not in attachment_columns:
                connection.execute('alter table feishu_pending_attachments add column source_message_id text')
            if 'claimed_by_message_id' not in attachment_columns:
                connection.execute('alter table feishu_pending_attachments add column claimed_by_message_id text')
            if 'claimed_at' not in attachment_columns:
                connection.execute('alter table feishu_pending_attachments add column claimed_at real')
            connection.execute('''
                create index if not exists idx_feishu_pending_attachments_claim
                on feishu_pending_attachments(chat_id,claimed_by_message_id,created_at)
            ''')
            connection.execute('''
                create index if not exists idx_feishu_pending_attachments_source
                on feishu_pending_attachments(chat_id,source_message_id,claimed_by_message_id)
            ''')

    @staticmethod
    def _inbox(row):
        return InboxRecord(**dict(row)) if row else None

    @staticmethod
    def _session(row):
        return FeishuSession(**dict(row)) if row else None

    def enqueue_message(self, message: FeishuInboundMessage) -> bool:
        with self._connection() as connection:
            cursor = connection.execute('''
                insert or ignore into feishu_inbox(
                    message_id,event_id,chat_id,chat_type,sender_open_id,message_type,text_content,resources_json,status,received_at
                ) values(?,?,?,?,?,?,?,?,?,?)
            ''', (
                message.message_id, message.event_id, message.chat_id, message.chat_type,
                message.sender_open_id, message.message_type, message.text,
                json.dumps(message.resources, ensure_ascii=False) if message.resources else None,
                'queued', time.time(),
            ))
            return cursor.rowcount == 1

    def get_inbox(self, message_id):
        with self._connection() as connection:
            return self._inbox(connection.execute('select * from feishu_inbox where message_id=?', (message_id,)).fetchone())

    def rewrite_queued_text(self, message_id, text):
        """Reuse a reserved/claimed inbound row as a queued follow-up."""
        with self._connection() as connection:
            cursor = connection.execute('''
                update feishu_inbox
                set text_content=?, status='queued', started_at=null, completed_at=null,
                    response_message_id=null, error_code=null, error_message=null
                where message_id=? and status in ('queued','running')
            ''', (str(text), message_id))
            return cursor.rowcount == 1

    def prune_history(
        self,
        *,
        now=None,
        inbox_retention=INBOX_RETENTION_SECONDS,
        approval_retention=APPROVAL_RETENTION_SECONDS,
        attachment_retention=ATTACHMENT_RETENTION_SECONDS,
        max_inbox=MAX_TERMINAL_INBOX_ROWS,
        max_replies=MAX_TERMINAL_REPLY_ROWS,
        max_approvals=MAX_TERMINAL_APPROVAL_ROWS,
    ):
        """Bound CFR-owned operational history without touching active state."""
        now = float(time.time() if now is None else now)
        inbox_cutoff = now - max(0, float(inbox_retention))
        approval_cutoff = now - max(0, float(approval_retention))
        attachment_cutoff = now - max(0, float(attachment_retention))
        terminal_inbox = ('completed', 'failed', 'ignored', 'interrupted_on_restart')
        terminal_approvals = ('approved', 'declined', 'cancelled', 'expired', 'orphaned')
        removed = {'inbox': 0, 'replies': 0, 'approvals': 0, 'attachments': 0, 'attachment_paths': []}

        with self._connection() as connection:
            stale_attachments = connection.execute(
                '''
                select a.path from feishu_pending_attachments a
                where (
                    a.claimed_by_message_id is not null
                    and not exists (
                        select 1 from feishu_inbox i
                        where i.message_id=a.claimed_by_message_id
                          and i.status in ('queued','running')
                    )
                ) or (
                    a.claimed_by_message_id is null
                    and a.created_at < ?
                    and not exists (
                        select 1 from feishu_inbox i
                        where i.chat_id=a.chat_id
                          and i.status in ('queued','running')
                          and i.received_at >= a.created_at
                    )
                )
                ''',
                (attachment_cutoff,),
            ).fetchall()
            cursor = connection.execute(
                '''
                delete from feishu_pending_attachments
                where (
                    claimed_by_message_id is not null
                    and not exists (
                        select 1 from feishu_inbox i
                        where i.message_id=feishu_pending_attachments.claimed_by_message_id
                          and i.status in ('queued','running')
                    )
                ) or (
                    claimed_by_message_id is null
                    and created_at < ?
                    and not exists (
                        select 1 from feishu_inbox i
                        where i.chat_id=feishu_pending_attachments.chat_id
                          and i.status in ('queued','running')
                          and i.received_at >= feishu_pending_attachments.created_at
                    )
                )
                ''',
                (attachment_cutoff,),
            )
            removed['attachments'] += max(0, cursor.rowcount)
            removed['attachment_paths'] = [row['path'] for row in stale_attachments]

            cursor = connection.execute(
                "delete from feishu_replies where state in ('sent','failed') and created_at < ?",
                (inbox_cutoff,),
            )
            removed['replies'] += max(0, cursor.rowcount)
            if max_replies >= 0:
                cursor = connection.execute('''
                    delete from feishu_replies where reply_key in (
                        select reply_key from feishu_replies
                        where state in ('sent','failed')
                        order by created_at desc limit -1 offset ?
                    )
                ''', (int(max_replies),))
                removed['replies'] += max(0, cursor.rowcount)

            approval_marks = ','.join('?' for _ in terminal_approvals)
            cursor = connection.execute(f'''
                delete from feishu_approvals
                where state in ({approval_marks})
                  and coalesce(resolved_at, expires_at, created_at) < ?
            ''', (*terminal_approvals, approval_cutoff))
            removed['approvals'] += max(0, cursor.rowcount)
            if max_approvals >= 0:
                cursor = connection.execute(f'''
                    delete from feishu_approvals where approval_id in (
                        select approval_id from feishu_approvals
                        where state in ({approval_marks})
                        order by created_at desc limit -1 offset ?
                    )
                ''', (*terminal_approvals, int(max_approvals)))
                removed['approvals'] += max(0, cursor.rowcount)

            inbox_marks = ','.join('?' for _ in terminal_inbox)
            cursor = connection.execute(f'''
                delete from feishu_inbox
                where status in ({inbox_marks})
                  and coalesce(completed_at, received_at) < ?
            ''', (*terminal_inbox, inbox_cutoff))
            removed['inbox'] += max(0, cursor.rowcount)
            if max_inbox >= 0:
                cursor = connection.execute(f'''
                    delete from feishu_inbox where message_id in (
                        select message_id from feishu_inbox
                        where status in ({inbox_marks})
                        order by received_at desc limit -1 offset ?
                    )
                ''', (*terminal_inbox, int(max_inbox)))
                removed['inbox'] += max(0, cursor.rowcount)
            cursor = connection.execute('''
                delete from feishu_replies
                where state in ('sent','failed')
                  and not exists (
                      select 1 from feishu_inbox
                      where feishu_inbox.message_id=feishu_replies.message_id
                  )
            ''')
            removed['replies'] += max(0, cursor.rowcount)
        return removed

    def claim_next(self, message_id=None):
        connection = self._connect()
        if self._memory:
            self._memory_lock.acquire()
        try:
            connection.execute('begin immediate')
            if message_id is None:
                row = connection.execute(
                    'select * from feishu_inbox where status=? order by received_at,rowid limit 1',
                    ('queued',),
                ).fetchone()
            else:
                row = connection.execute(
                    'select * from feishu_inbox where message_id=? and status=?',
                    (message_id, 'queued'),
                ).fetchone()
            if not row:
                connection.commit()
                return None
            now = time.time()
            cursor = connection.execute('update feishu_inbox set status=?,started_at=? where message_id=? and status=?', ('running', now, row['message_id'], 'queued'))
            connection.commit()
            if cursor.rowcount != 1:
                return None
            claimed = dict(row)
            claimed['status'] = 'running'
            claimed['started_at'] = now
            return self._inbox(claimed)
        except Exception:
            connection.rollback()
            raise
        finally:
            if self._memory:
                self._memory_lock.release()
            if not self._memory:
                connection.close()

    def next_queued_message_id(self, chat_id):
        with self._connection() as connection:
            row = connection.execute(
                'select message_id from feishu_inbox where chat_id=? and status=? order by received_at,rowid limit 1',
                (str(chat_id), 'queued'),
            ).fetchone()
        return row['message_id'] if row else None

    def recover_on_startup(self):
        with self._connection() as connection:
            connection.execute('update feishu_inbox set status=? where status=?', ('interrupted_on_restart', 'running'))
            connection.execute('''
                update feishu_pending_attachments
                set claimed_by_message_id=null,claimed_at=null
                where claimed_by_message_id in (
                    select message_id from feishu_inbox where status='interrupted_on_restart'
                )
            ''')
            connection.execute('update feishu_approvals set state=? where state=?', ('orphaned', 'pending'))
            connection.execute("update feishu_replies set state='failed' where state='pending'")
            return connection.execute(
                'select message_id,chat_id from feishu_inbox where status=? order by received_at,rowid',
                ('queued',),
            ).fetchall()

    def mark_completed(self, message_id, response_message_id=None):
        with self._connection() as connection:
            connection.execute('update feishu_inbox set status=?,completed_at=?,response_message_id=? where message_id=?', ('completed', time.time(), response_message_id, message_id))

    def mark_failed(self, message_id, code, message):
        with self._connection() as connection:
            connection.execute('update feishu_inbox set status=?,completed_at=?,error_code=?,error_message=? where message_id=?', ('failed', time.time(), code, message[:500], message_id))

    def mark_ignored(self, message_id, code='IGNORED', message='ignored'):
        with self._connection() as connection:
            connection.execute('update feishu_inbox set status=?,completed_at=?,error_code=?,error_message=? where message_id=?', ('ignored', time.time(), code, message[:500], message_id))

    def get_session(self, chat_id):
        with self._connection() as connection:
            return self._session(connection.execute('select * from feishu_sessions where chat_id=?', (chat_id,)).fetchone())

    def get_selected_surface_or_none(self, chat_id):
        with self._connection() as connection:
            row = connection.execute('select selected_surface from feishu_surface_state where chat_id=?', (chat_id,)).fetchone()
        return row['selected_surface'] if row and row['selected_surface'] in {'chat', 'code'} else None

    def get_selected_surface(self, chat_id):
        return self.get_selected_surface_or_none(chat_id) or 'code'

    def chat_exists(self, chat_id):
        with self._connection() as connection:
            row = connection.execute('''
                select 1 from feishu_sessions where chat_id=?
                union all select 1 from feishu_chat_bindings where chat_id=?
                union all select 1 from feishu_surface_state where chat_id=?
                union all select 1 from feishu_inbox where chat_id=?
                limit 1
            ''', (chat_id, chat_id, chat_id, chat_id)).fetchone()
        return row is not None

    def get_current_surface_state(self):
        with self._connection() as connection:
            row = connection.execute('''
                select i.chat_id, coalesce(s.selected_surface, 'code') as selected_surface, i.received_at as updated_at
                from feishu_inbox i
                left join feishu_surface_state s on s.chat_id=i.chat_id
                order by i.received_at desc
                limit 1
            ''').fetchone()
            if row is None:
                row = connection.execute('''
                    select chat_id,selected_surface,updated_at
                    from feishu_surface_state
                    order by updated_at desc
                    limit 1
                ''').fetchone()
        return dict(row) if row else None

    def list_surface_states(self, limit=None):
        clause = '' if limit is None else ' limit ?'
        params = () if limit is None else (max(0, int(limit)),)
        with self._connection() as connection:
            rows = connection.execute(
                'select chat_id,selected_surface,updated_at from feishu_surface_state order by updated_at desc' + clause,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_selected_surface(self, chat_id, surface):
        if surface not in {'chat', 'code'}:
            raise StructuredError('FEISHU_SURFACE_INVALID', 'Only Chat and Code surfaces can currently be selected')
        with self._connection() as connection:
            connection.execute('''
                insert into feishu_surface_state(chat_id,selected_surface,updated_at) values(?,?,?)
                on conflict(chat_id) do update set selected_surface=excluded.selected_surface,updated_at=excluded.updated_at
            ''', (chat_id, surface, time.time()))

    def get_chat_binding(self, chat_id):
        with self._connection() as connection:
            row = connection.execute('select * from feishu_chat_bindings where chat_id=?', (chat_id,)).fetchone()
            return dict(row) if row else None

    def list_chat_bindings(self, limit=None):
        clause = '' if limit is None else ' limit ?'
        params = () if limit is None else (max(0, int(limit)),)
        with self._connection() as connection:
            rows = connection.execute(
                'select chat_id,tab_key,tab_id,url,created_at,updated_at from feishu_chat_bindings order by updated_at desc' + clause,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def ensure_chat_binding(self, chat_id):
        existing = self.get_chat_binding(chat_id)
        if existing:
            return existing
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                'insert or ignore into feishu_chat_bindings(chat_id,tab_key,created_at,updated_at) values(?,?,?,?)',
                (chat_id, f'cfr-chat-{uuid.uuid4().hex}', now, now),
            )
        return self.get_chat_binding(chat_id)

    def add_pending_attachment(self, chat_id, path, kind, name=None, *, source_message_id=None):
        return self.add_pending_attachments(
            chat_id,
            [(path, kind, name)],
            source_message_id=source_message_id,
        )[0]

    def add_pending_attachments(self, chat_id, attachments, *, source_message_id=None):
        records = []
        for path, kind, name in attachments:
            if kind not in {'image', 'file'}:
                raise StructuredError('FEISHU_ATTACHMENT_KIND_INVALID', f'Unsupported attachment kind: {kind}')
            resolved = Path(path).expanduser().resolve(strict=True)
            if not resolved.is_file():
                raise StructuredError('FEISHU_ATTACHMENT_FILE_REQUIRED', 'Attachment must be an existing local file')
            records.append({
                'attachment_id': uuid.uuid4().hex,
                'chat_id': chat_id,
                'kind': kind,
                'path': str(resolved),
                'name': str(name or resolved.name),
                'source_message_id': str(source_message_id) if source_message_id else None,
                'created_at': time.time(),
            })
        with self._connection() as connection:
            connection.executemany(
                'insert into feishu_pending_attachments(attachment_id,chat_id,kind,path,name,source_message_id,created_at) values(?,?,?,?,?,?,?)',
                [
                    (
                        record['attachment_id'], record['chat_id'], record['kind'], record['path'], record['name'],
                        record['source_message_id'], record['created_at'],
                    )
                    for record in records
                ],
            )
        return [{key: value for key, value in record.items() if key != 'created_at'} for record in records]

    def list_pending_attachments(self, chat_id):
        with self._connection() as connection:
            rows = connection.execute(
                '''select attachment_id,chat_id,kind,path,name,source_message_id,created_at,claimed_by_message_id,claimed_at
                   from feishu_pending_attachments
                   where chat_id=? and claimed_by_message_id is null
                   order by created_at,attachment_id''',
                (chat_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_pending_attachments(self, chat_id, message_id):
        """Atomically bind the currently unclaimed attachment batch to one prompt."""
        connection = self._connect()
        if self._memory:
            self._memory_lock.acquire()
        try:
            connection.execute('begin immediate')
            now = time.time()
            prompt = connection.execute(
                'select received_at from feishu_inbox where message_id=? and chat_id=?',
                (str(message_id), str(chat_id)),
            ).fetchone()
            if prompt is None:
                connection.execute('''
                    update feishu_pending_attachments
                    set claimed_by_message_id=?,claimed_at=?
                    where chat_id=? and claimed_by_message_id is null
                ''', (str(message_id), now, str(chat_id)))
            else:
                connection.execute('''
                    update feishu_pending_attachments
                    set claimed_by_message_id=?,claimed_at=?
                    where chat_id=?
                      and claimed_by_message_id is null
                      and (
                          source_message_id is null
                          or exists (
                              select 1 from feishu_inbox source
                              where source.message_id=feishu_pending_attachments.source_message_id
                                and source.chat_id=feishu_pending_attachments.chat_id
                                and source.received_at <= ?
                          )
                      )
                ''', (str(message_id), now, str(chat_id), float(prompt['received_at'])))
            rows = connection.execute('''
                select attachment_id,chat_id,kind,path,name,source_message_id,created_at,claimed_by_message_id,claimed_at
                from feishu_pending_attachments
                where chat_id=? and claimed_by_message_id=?
                order by created_at,attachment_id
            ''', (str(chat_id), str(message_id))).fetchall()
            connection.commit()
            return [dict(row) for row in rows]
        except Exception:
            connection.rollback()
            raise
        finally:
            if self._memory:
                self._memory_lock.release()
            if not self._memory:
                connection.close()

    def list_claimed_attachments(self, message_id):
        with self._connection() as connection:
            rows = connection.execute('''
                select attachment_id,chat_id,kind,path,name,source_message_id,created_at,claimed_by_message_id,claimed_at
                from feishu_pending_attachments
                where claimed_by_message_id=?
                order by created_at,attachment_id
            ''', (str(message_id),)).fetchall()
        return [dict(row) for row in rows]

    def release_claimed_attachments(self, message_id):
        """Return a failed task's attachment batch to the chat pending pool."""
        with self._connection() as connection:
            cursor = connection.execute('''
                update feishu_pending_attachments
                set claimed_by_message_id=null,claimed_at=null
                where claimed_by_message_id=?
            ''', (str(message_id),))
            return max(0, cursor.rowcount)

    def list_attachment_paths(self):
        with self._connection() as connection:
            rows = connection.execute('select path from feishu_pending_attachments').fetchall()
        return [str(row['path']) for row in rows]

    def clear_pending_attachments(self, chat_id, attachment_ids=None):
        with self._connection() as connection:
            if attachment_ids:
                values = tuple(str(value) for value in attachment_ids)
                placeholders = ','.join('?' for _ in values)
                connection.execute(
                    f'delete from feishu_pending_attachments where chat_id=? and attachment_id in ({placeholders})',
                    (chat_id, *values),
                )
            else:
                connection.execute('delete from feishu_pending_attachments where chat_id=?', (chat_id,))

    def update_chat_binding(self, chat_id, *, tab_id=None, url=None):
        self.ensure_chat_binding(chat_id)
        updates = ['updated_at=?']
        values = [time.time()]
        if tab_id is not None:
            updates.append('tab_id=?')
            values.append(tab_id)
        if url is not None:
            updates.append('url=?')
            values.append(url)
        values.append(chat_id)
        with self._connection() as connection:
            connection.execute(f'update feishu_chat_bindings set {",".join(updates)} where chat_id=?', values)
        return self.get_chat_binding(chat_id)

    def create_pending_session(self, chat_id, chat_type, owner_open_id, pending_cwd):
        now = time.time()
        with self._connection() as connection:
            existing = connection.execute('select * from feishu_sessions where chat_id=?', (chat_id,)).fetchone()
            if existing:
                raise StructuredError('FEISHU_SESSION_ALREADY_EXISTS', 'This Feishu chat is already bound or pending')
            connection.execute('insert into feishu_sessions(chat_id,chat_type,owner_open_id,state,pending_cwd,created_at,updated_at) values(?,?,?,?,?,?,?)', (chat_id, chat_type, owner_open_id, 'pending_initial', str(pending_cwd), now, now))

    def switch_to_pending_session(self, chat_id, chat_type, owner_open_id, pending_cwd):
        """Replace this chat's Feishu session only; the native binding remains durable."""
        now = time.time()
        with self._connection() as connection:
            row = connection.execute('select approval_mode from feishu_sessions where chat_id=?', (chat_id,)).fetchone()
            approval_mode = row['approval_mode'] if row and row['approval_mode'] else 'ask'
            connection.execute('delete from feishu_sessions where chat_id=?', (chat_id,))
            connection.execute('insert into feishu_sessions(chat_id,chat_type,owner_open_id,state,pending_cwd,approval_mode,created_at,updated_at) values(?,?,?,?,?,?,?,?)', (chat_id, chat_type, owner_open_id, 'pending_initial', str(pending_cwd), approval_mode, now, now))

    def get_approval_mode(self, chat_id):
        with self._connection() as connection:
            row = connection.execute('select approval_mode from feishu_sessions where chat_id=?', (chat_id,)).fetchone()
        return str(row['approval_mode'] or 'ask') if row else None

    def set_approval_mode(self, chat_id, mode):
        if mode not in {'ask', 'auto', 'full'}:
            raise StructuredError('FEISHU_APPROVAL_MODE_INVALID', 'Unsupported Code approval mode')
        with self._connection() as connection:
            cursor = connection.execute(
                'update feishu_sessions set approval_mode=?,updated_at=? where chat_id=?',
                (mode, time.time(), chat_id),
            )
        if cursor.rowcount != 1:
            raise StructuredError('FEISHU_NO_ACTIVE_SESSION', '当前聊天没有 Code session；请先使用 /workspace 或 /new。')
        return mode

    def get_pending_thread_settings(self, chat_id):
        with self._connection() as connection:
            row = connection.execute(
                'select state,pending_settings_json from feishu_sessions where chat_id=?',
                (chat_id,),
            ).fetchone()
        if not row or row['state'] != 'pending_initial' or not row['pending_settings_json']:
            return {}
        try:
            value = json.loads(row['pending_settings_json'])
        except (TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            key: value.get(key)
            for key in ('model', 'reasoning_effort', 'service_tier')
            if key in value
        }

    def update_pending_thread_settings(self, chat_id, **changes):
        allowed = {'model', 'reasoning_effort', 'service_tier'}
        if not changes or not set(changes).issubset(allowed):
            raise StructuredError('FEISHU_PENDING_SETTINGS_INVALID', 'Unsupported pending thread setting')
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                'select state,pending_settings_json from feishu_sessions where chat_id=?',
                (chat_id,),
            ).fetchone()
            if not row or row['state'] != 'pending_initial':
                raise StructuredError('FEISHU_NO_PENDING_SESSION', '当前聊天没有等待创建的 Code session。')
            current = {}
            if row['pending_settings_json']:
                try:
                    decoded = json.loads(row['pending_settings_json'])
                    current = decoded if isinstance(decoded, dict) else {}
                except (TypeError, json.JSONDecodeError):
                    current = {}
            current.update(changes)
            connection.execute(
                'update feishu_sessions set pending_settings_json=?,updated_at=? where chat_id=?',
                (json.dumps(current, ensure_ascii=False, sort_keys=True), now, chat_id),
            )
        return self.get_pending_thread_settings(chat_id)

    def bind_session(self, chat_id, thread_id):
        now = time.time()
        with self._connection() as connection:
            other = connection.execute('select chat_id from feishu_sessions where thread_id=? and chat_id<>?', (thread_id, chat_id)).fetchone()
            if other:
                raise StructuredError('FEISHU_THREAD_ALREADY_BOUND', 'The native Codex thread is already bound to another chat')
            connection.execute('update feishu_sessions set state=?,thread_id=?,pending_cwd=null,pending_settings_json=null,updated_at=? where chat_id=?', ('bound', thread_id, now, chat_id))

    def unbind_session(self, chat_id):
        with self._connection() as connection:
            connection.execute('delete from feishu_sessions where chat_id=?', (chat_id,))

    def list_sessions(self, limit=None):
        clause = '' if limit is None else ' limit ?'
        params = () if limit is None else (max(0, int(limit)),)
        with self._connection() as connection:
            return [self._session(row) for row in connection.execute('select * from feishu_sessions order by updated_at desc' + clause, params).fetchall()]

    def recent_control_chat_state(self, limit=200):
        """Return complete state for the most recently changed chats only.

        The control plane is bounded by chat, not independently by table.  A
        recent Code session must therefore keep its older Chat/Surface rows in
        the same projection instead of losing them to three unrelated LIMITs.
        """
        limit = max(1, min(int(limit), 1000))
        with self._connection() as connection:
            chat_ids = [row['chat_id'] for row in connection.execute('''
                with candidates as (
                    select chat_id,updated_at from (
                        select chat_id,updated_at from feishu_sessions order by updated_at desc limit ?
                    )
                    union all
                    select chat_id,updated_at from (
                        select chat_id,updated_at from feishu_chat_bindings order by updated_at desc limit ?
                    )
                    union all
                    select chat_id,updated_at from (
                        select chat_id,updated_at from feishu_surface_state order by updated_at desc limit ?
                    )
                )
                select chat_id from candidates
                group by chat_id
                order by max(updated_at) desc, chat_id asc
                limit ?
            ''', (limit, limit, limit, limit)).fetchall()]
            if not chat_ids:
                return {'sessions': [], 'chat_bindings': [], 'surface_states': []}
            placeholders = ','.join('?' for _ in chat_ids)
            sessions = [
                self._session(row)
                for row in connection.execute(
                    f'select * from feishu_sessions where chat_id in ({placeholders})',
                    chat_ids,
                ).fetchall()
            ]
            chat_bindings = [dict(row) for row in connection.execute(
                f'''select chat_id,tab_key,tab_id,url,created_at,updated_at
                    from feishu_chat_bindings where chat_id in ({placeholders})''',
                chat_ids,
            ).fetchall()]
            surface_states = [dict(row) for row in connection.execute(
                f'''select chat_id,selected_surface,updated_at
                    from feishu_surface_state where chat_id in ({placeholders})''',
                chat_ids,
            ).fetchall()]
        return {
            'sessions': sessions,
            'chat_bindings': chat_bindings,
            'surface_states': surface_states,
        }

    def bound_chats_for_threads(self, thread_ids):
        values = tuple(dict.fromkeys(str(value) for value in thread_ids if value))
        if not values:
            return {}
        placeholders = ','.join('?' for _ in values)
        with self._connection() as connection:
            rows = connection.execute(
                f'select thread_id,chat_id from feishu_sessions where thread_id in ({placeholders})',
                values,
            ).fetchall()
        return {row['thread_id']: row['chat_id'] for row in rows if row['thread_id']}

    def reserve_reply(self, message_id, phase, chunk_index, response_message_id=None):
        key = f'{message_id}|{phase}|{chunk_index}'
        with self._connection() as connection:
            cursor = connection.execute('''
                insert into feishu_replies(reply_key,message_id,phase,chunk_index,response_message_id,state,created_at)
                values(?,?,?,?,?,?,?)
                on conflict(reply_key) do update set
                    response_message_id=null,
                    state='pending',
                    created_at=excluded.created_at
                where feishu_replies.state='failed'
            ''', (key, message_id, phase, chunk_index, response_message_id, 'pending', time.time()))
            return cursor.rowcount == 1

    def get_reply(self, message_id, phase, chunk_index):
        key = f'{message_id}|{phase}|{chunk_index}'
        with self._connection() as connection:
            row = connection.execute('select response_message_id from feishu_replies where reply_key=?', (key,)).fetchone()
            return row['response_message_id'] if row else None

    def get_reply_record(self, message_id, phase, chunk_index):
        key = f'{message_id}|{phase}|{chunk_index}'
        with self._connection() as connection:
            row = connection.execute('select response_message_id,state from feishu_replies where reply_key=?', (key,)).fetchone()
            return dict(row) if row else None

    def set_reply_response(self, message_id, phase, chunk_index, response_message_id):
        key = f'{message_id}|{phase}|{chunk_index}'
        with self._connection() as connection:
            connection.execute("update feishu_replies set response_message_id=?,state='sent' where reply_key=?", (response_message_id, key))

    def mark_reply_failed(self, message_id, phase, chunk_index):
        key = f'{message_id}|{phase}|{chunk_index}'
        with self._connection() as connection:
            connection.execute("update feishu_replies set state='failed' where reply_key=?", (key,))

    def acquire_daemon_lease(self, lease_key, owner_instance_id, owner_pid, ttl=30):
        now = time.time()
        connection = self._connect()
        try:
            connection.execute('begin immediate')
            row = connection.execute('select * from feishu_daemon_lease where lease_key=?', (lease_key,)).fetchone()
            if row and now < row['expires_at'] and row['owner_instance_id'] != owner_instance_id:
                connection.rollback()
                raise StructuredError('FEISHU_DAEMON_ALREADY_RUNNING', 'Another CFR Feishu daemon owns this app lease')
            connection.execute('''insert into feishu_daemon_lease(lease_key,owner_instance_id,owner_pid,acquired_at,heartbeat_at,expires_at) values(?,?,?,?,?,?)
                on conflict(lease_key) do update set owner_instance_id=excluded.owner_instance_id,owner_pid=excluded.owner_pid,acquired_at=excluded.acquired_at,heartbeat_at=excluded.heartbeat_at,expires_at=excluded.expires_at''', (lease_key, owner_instance_id, owner_pid, now, now, now + ttl))
            connection.commit()
        finally:
            if not self._memory:
                connection.close()

    def heartbeat_daemon_lease(self, lease_key, owner_instance_id, ttl=30):
        with self._connection() as connection:
            cursor = connection.execute('update feishu_daemon_lease set heartbeat_at=?,expires_at=? where lease_key=? and owner_instance_id=?', (time.time(), time.time() + ttl, lease_key, owner_instance_id))
            return cursor.rowcount == 1

    def release_daemon_lease(self, lease_key, owner_instance_id):
        with self._connection() as connection:
            cursor = connection.execute('delete from feishu_daemon_lease where lease_key=? and owner_instance_id=?', (lease_key, owner_instance_id))
            return cursor.rowcount == 1

    def inspect_daemon_lease(self, lease_key):
        with self._connection() as connection:
            row = connection.execute('select * from feishu_daemon_lease where lease_key=?', (lease_key,)).fetchone()
            return dict(row) if row else None

    def create_approval(self, approval_id, request, requester_open_id, expires_at, summary_json='{}'):
        with self._connection() as connection:
            connection.execute('insert into feishu_approvals(approval_id,codex_request_id,thread_id,turn_id,kind,requester_open_id,state,created_at,expires_at,summary_json,feedback_state,feedback_revision) values(?,?,?,?,?,?,?,?,?,?,?,?)', (approval_id, request.request_id, request.thread_id, request.turn_id, request.kind, requester_open_id, 'pending', time.time(), expires_at, summary_json, 'PENDING', 0))

    def get_approval(self, approval_id):
        with self._connection() as connection:
            row = connection.execute('select * from feishu_approvals where approval_id=?', (approval_id,)).fetchone()
            return dict(row) if row else None

    def find_approval_by_request_id(self, request_id):
        with self._connection() as connection:
            row = connection.execute('select * from feishu_approvals where codex_request_id=? order by created_at desc limit 1', (str(request_id),)).fetchone()
            return dict(row) if row else None

    def list_approvals_for_turn(self, thread_id, turn_id):
        """Return approvals bound to one authoritative thread/turn pair."""
        if thread_id is None or turn_id is None:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                'select * from feishu_approvals where thread_id=? and turn_id=? order by created_at asc, approval_id asc',
                (str(thread_id), str(turn_id)),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_pending_approval_turns(self):
        """Return the active approval identities in one bounded indexed query."""
        with self._connection() as connection:
            rows = connection.execute(
                "select distinct thread_id,turn_id from feishu_approvals "
                "where state='pending' and turn_id is not null"
            ).fetchall()
        return {(row['thread_id'], row['turn_id']) for row in rows}

    def list_recent_approvals(self, limit=100):
        """Bounded durable approval view for read-only management surfaces."""
        limit = min(max(int(limit), 1), 100)
        with self._connection() as connection:
            rows = connection.execute(
                '''select approval_id,thread_id,turn_id,kind,state,decision,created_at,resolved_at,feedback_state,feedback_updated_at
                   from feishu_approvals order by created_at desc,approval_id desc limit ?''',
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def bind_approval_card(self, approval_id, card_message_id):
        with self._connection() as connection:
            cursor = connection.execute(
                'update feishu_approvals set card_message_id=? where approval_id=? and card_message_id is null',
                (str(card_message_id), approval_id),
            )
            return cursor.rowcount == 1

    def transition_feedback(self, approval_id, state):
        """Advance only monotonically; the card projection never creates authority."""
        target_rank = FEEDBACK_RANKS.get(state)
        if target_rank is None:
            raise StructuredError('FEISHU_APPROVAL_FEEDBACK_STATE_INVALID', 'Approval feedback state is invalid')
        connection = self._connect()
        try:
            connection.execute('begin immediate')
            row = connection.execute('select feedback_state,feedback_revision from feishu_approvals where approval_id=?', (approval_id,)).fetchone()
            if not row:
                connection.rollback()
                return False
            current_state = row['feedback_state'] or 'PENDING'
            current_rank = FEEDBACK_RANKS.get(current_state, row['feedback_revision'] or 0)
            if target_rank < current_rank or (current_rank >= 3 and current_state != state):
                connection.rollback()
                return False
            connection.execute(
                'update feishu_approvals set feedback_state=?,feedback_revision=?,feedback_updated_at=? where approval_id=?',
                (state, max(target_rank, row['feedback_revision'] or 0), time.time(), approval_id),
            )
            connection.commit()
            return True
        finally:
            if not self._memory:
                connection.close()

    def mark_feedback_update_failed(self, approval_id, error_code):
        with self._connection() as connection:
            connection.execute(
                'update feishu_approvals set feedback_update_failed=1,feedback_update_error_code=?,feedback_updated_at=? where approval_id=?',
                (str(error_code)[:120], time.time(), approval_id),
            )

    def mark_feedback_update_succeeded(self, approval_id):
        with self._connection() as connection:
            connection.execute(
                'update feishu_approvals set feedback_update_failed=0,feedback_update_error_code=null,feedback_updated_at=? where approval_id=?',
                (time.time(), approval_id),
            )

    def find_approval_prefix(self, prefix):
        with self._connection() as connection:
            rows = connection.execute('select * from feishu_approvals where approval_id like ? and state=? limit 2', (f'{prefix}%', 'pending')).fetchall()
            return [dict(row) for row in rows]

    def resolve_approval(self, approval_id, decision, state='approved'):
        with self._connection() as connection:
            cursor = connection.execute('update feishu_approvals set state=?,decision=?,resolved_at=? where approval_id=? and state=?', (state, decision, time.time(), approval_id, 'pending'))
            return cursor.rowcount == 1

    def expire_approval(self, approval_id):
        return self.resolve_approval(approval_id, 'decline', 'expired')

    def close(self):
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None
