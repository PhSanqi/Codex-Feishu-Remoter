from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import time
import uuid
from pathlib import Path

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


class FeishuStore:
    """Connection-per-operation durable state in the CFR-owned SQLite file."""

    def __init__(self, path):
        self.db_path = Path(path)
        self._memory = str(path) == ':memory:'
        self._memory_conn = sqlite3.connect(':memory:', check_same_thread=False) if self._memory else None
        if not self._memory and self.db_path.parent != Path('.'):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = self._memory_conn if self._memory else sqlite3.connect(self.db_path, timeout=1.0)
        connection.execute('pragma busy_timeout=1000')
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            if not self._memory:
                connection.close()

    def _initialize(self):
        with self._connection() as connection:
            connection.executescript('''
                create table if not exists feishu_inbox(
                    message_id text primary key,
                    event_id text,
                    chat_id text not null,
                    chat_type text not null,
                    sender_open_id text not null,
                    message_type text not null,
                    text_content text,
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
                    created_at real not null,
                    updated_at real not null
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
                    message_id,event_id,chat_id,chat_type,sender_open_id,message_type,text_content,status,received_at
                ) values(?,?,?,?,?,?,?,?,?)
            ''', (message.message_id, message.event_id, message.chat_id, message.chat_type, message.sender_open_id, message.message_type, message.text, 'queued', time.time()))
            return cursor.rowcount == 1

    def get_inbox(self, message_id):
        with self._connection() as connection:
            return self._inbox(connection.execute('select * from feishu_inbox where message_id=?', (message_id,)).fetchone())

    def claim_next(self):
        connection = self._connect()
        try:
            connection.execute('begin immediate')
            row = connection.execute('select * from feishu_inbox where status=? order by received_at limit 1', ('queued',)).fetchone()
            if not row:
                connection.commit()
                return None
            now = time.time()
            cursor = connection.execute('update feishu_inbox set status=?,started_at=? where message_id=? and status=?', ('running', now, row['message_id'], 'queued'))
            connection.commit()
            if cursor.rowcount != 1:
                return None
            return self.get_inbox(row['message_id'])
        finally:
            if not self._memory:
                connection.close()

    def recover_on_startup(self):
        with self._connection() as connection:
            connection.execute('update feishu_inbox set status=? where status=?', ('interrupted_on_restart', 'running'))
            connection.execute('update feishu_approvals set state=? where state=?', ('orphaned', 'pending'))
            connection.execute("update feishu_replies set state='failed' where state='pending'")
            return connection.execute('select message_id from feishu_inbox where status=? order by received_at', ('queued',)).fetchall()

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
            connection.execute('delete from feishu_sessions where chat_id=?', (chat_id,))
            connection.execute('insert into feishu_sessions(chat_id,chat_type,owner_open_id,state,pending_cwd,created_at,updated_at) values(?,?,?,?,?,?,?)', (chat_id, chat_type, owner_open_id, 'pending_initial', str(pending_cwd), now, now))

    def bind_session(self, chat_id, thread_id):
        now = time.time()
        with self._connection() as connection:
            other = connection.execute('select chat_id from feishu_sessions where thread_id=? and chat_id<>?', (thread_id, chat_id)).fetchone()
            if other:
                raise StructuredError('FEISHU_THREAD_ALREADY_BOUND', 'The native Codex thread is already bound to another chat')
            connection.execute('update feishu_sessions set state=?,thread_id=?,pending_cwd=null,updated_at=? where chat_id=?', ('bound', thread_id, now, chat_id))

    def unbind_session(self, chat_id):
        with self._connection() as connection:
            connection.execute('delete from feishu_sessions where chat_id=?', (chat_id,))

    def list_sessions(self):
        with self._connection() as connection:
            return [self._session(row) for row in connection.execute('select * from feishu_sessions order by updated_at desc').fetchall()]

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
