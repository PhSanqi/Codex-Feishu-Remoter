import argparse
import asyncio
import getpass
import json
import sqlite3
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

from cfr.codex.binding import CodexAdapter
from cfr.codex.approvals import CodexApprovalCodec
from cfr.codex.launcher import CodexLauncher
from cfr.codex.rollout import RolloutWatcher
from cfr.core.models import StructuredError
from cfr.core.projector import EventProjector
from cfr.storage.db import BindingStore


def _json_value(value):
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _print(value):
    print(json.dumps(_json_value(value), ensure_ascii=False, default=str))


def _console_server_request(message):
    """Handle the CFR CLI's supported Codex server requests without auto-approval."""
    method = message.get('method') if isinstance(message, dict) else None
    params = (message.get('params') or {}) if isinstance(message, dict) else {}
    if method == 'currentTime/read':
        return {'currentTimeAt': int(time.time())}
    if method == 'item/tool/requestUserInput':
        questions = [item for item in (params.get('questions') or ()) if isinstance(item, dict)]
        if not questions or not sys.stdin.isatty():
            return {'error': {'code': -32014, 'message': 'CFR_CLI_USER_INPUT_UNAVAILABLE'}}
        answers = {}
        for index, question in enumerate(questions, 1):
            header = str(question.get('header') or f'Question {index}')
            prompt = str(question.get('question') or '').strip()
            print(f'\n[{header}] {prompt}')
            options = [item for item in (question.get('options') or ()) if isinstance(item, dict)]
            for option_index, option in enumerate(options, 1):
                label = str(option.get('label') or '')
                description = str(option.get('description') or '')
                print(f'  {option_index}. {label}' + (f' - {description}' if description else ''))
            value = getpass.getpass('Answer: ') if question.get('isSecret') else input('Answer: ')
            value = value.strip()
            if options and value.isdigit() and 1 <= int(value) <= len(options):
                value = str(options[int(value) - 1].get('label') or value)
            answers[str(question.get('id') or index)] = {'answers': [value]}
        return {'answers': answers}

    codec = CodexApprovalCodec()
    thread_id = str(params.get('threadId') or params.get('conversationId') or '')
    request = codec.decode(message, thread_id)
    if request is None:
        return {'error': {'code': -32601, 'message': 'UNSUPPORTED_CODEX_SERVER_REQUEST'}}
    print(f'\nCodex approval required: {request.kind}')
    if request.cwd:
        print(f'Workspace: {request.cwd}')
    if request.command:
        print(f'Command: {request.command}')
    if request.changed_paths:
        print('Changed paths: ' + ', '.join(request.changed_paths))
    if request.requested_permissions:
        print('Requested permissions: ' + json.dumps(request.requested_permissions, ensure_ascii=False))
    if request.reason:
        print(f'Reason: {request.reason}')
    if not sys.stdin.isatty():
        return codec.response('decline', request)
    approved = input('Approve once? [y/N] ').strip().lower() in {'y', 'yes'}
    return codec.response('accept' if approved else 'decline', request)


def _parser():
    parser = argparse.ArgumentParser(prog='cfr')
    parser.add_argument('--db', default='cfr.sqlite3')
    parser.add_argument('--codex-home', default=None)
    parser.add_argument('--codex-bin', default=None)
    root = parser.add_subparsers(dest='cmd', required=True)
    doctor = root.add_parser('doctor')
    doctor.add_argument('--json', action='store_true')
    doctor.add_argument('--live', action='store_true')
    doctor.add_argument('--timeout', type=float, default=60)
    doctor.add_argument('--gate-origin', choices=('desktop_agent', 'host_manual', 'unknown'), default='unknown')
    feishu = root.add_parser('feishu')
    feishu_ops = feishu.add_subparsers(dest='feishu_op', required=True)
    feishu_doctor = feishu_ops.add_parser('doctor')
    feishu_doctor.add_argument('--json', action='store_true')
    feishu_doctor.add_argument('--live', action='store_true')
    feishu_doctor.add_argument('--timeout', type=float, default=15)
    feishu_doctor.add_argument('--gate-origin', choices=('desktop_agent', 'host_manual', 'unknown'), default='unknown')
    feishu_run = feishu_ops.add_parser('run')
    feishu_run.add_argument('--setup-only', action='store_true')
    feishu_run.add_argument('--show-identifiers', action='store_true')
    feishu_run.add_argument('--verbose', action='store_true')
    credentials = feishu_ops.add_parser('credentials')
    credential_ops = credentials.add_subparsers(dest='credentials_op', required=True)
    credential_ops.add_parser('status')
    credential_ops.add_parser('import-env')
    set_app_id = credential_ops.add_parser('set-app-id')
    set_app_id.add_argument('app_id')
    credential_ops.add_parser('set-secret')
    credential_ops.add_parser('clear-secret')
    clear_all = credential_ops.add_parser('clear-all')
    clear_all.add_argument('--yes', action='store_true')
    codex = root.add_parser('codex')
    ops = codex.add_subparsers(dest='op', required=True)
    new = ops.add_parser('new')
    new.add_argument('--cwd', required=True)
    new.add_argument('--name', required=True)
    new.add_argument('--message', required=True)
    send = ops.add_parser('send')
    send.add_argument('thread_id')
    send.add_argument('message')
    ops.add_parser('list')
    for name in ('status', 'watch', 'stop'):
        command = ops.add_parser(name)
        command.add_argument('thread_id')
    watch = ops.choices['watch']
    watch.add_argument('--once', action='store_true')
    watch.add_argument('--interval', type=float, default=1.0)
    return parser


def _run_feishu_setup_only(transport, show_identifiers=False, timeout=15, connection_lease=None):
    """Connect and report identifiers without constructing execution state."""
    def show_identifier(message, _event_id=None):
        if show_identifiers:
            print(json.dumps({'sender_open_id': message.sender_open_id, 'chat_id': message.chat_id, 'message_id': message.message_id}, ensure_ascii=False))
        return None
    try:
        transport.connect_until_ready(show_identifier, timeout=timeout)
        print('FeishuConnectionReady: READY')
        if hasattr(transport, 'wait_until_stopped'):
            transport.wait_until_stopped()
        else:
            while getattr(transport, 'is_running', True):
                time.sleep(1)
        if getattr(transport, 'error', None):
            raise StructuredError('FEISHU_CHANNEL_UNKNOWN', 'Feishu Channel transport stopped with an error')
    except KeyboardInterrupt:
        return 0
    finally:
        transport.stop()
        if connection_lease is not None:
            connection_lease.release()


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.cmd == 'doctor':
        from cfr.doctor import run_doctor
        result = run_doctor(
            project_root=Path.cwd(),
            database=args.db,
            timeout=args.timeout,
            live=args.live,
            codex_home=args.codex_home,
            codex_bin=args.codex_bin,
            gate_origin=args.gate_origin,
        )
        if args.json:
            _print(result)
        else:
            print(f"Doctor: {result['Verdict']}")
            _print(result)
        return {'PASS': 0, 'WARN': 2, 'FAIL': 1}.get(result['Verdict'], 1)
    if args.cmd == 'feishu':
        if args.feishu_op == 'credentials':
            from cfr.feishu.credentials import (
                FeishuCredentialResolver,
                clear_persistent_all,
                clear_persistent_secret,
                import_environment_credentials,
                set_persistent_app_id,
                set_persistent_secret,
            )
            try:
                if args.credentials_op == 'status':
                    _print(FeishuCredentialResolver().safe_status())
                elif args.credentials_op == 'import-env':
                    _print(import_environment_credentials())
                elif args.credentials_op == 'set-app-id':
                    _print(set_persistent_app_id(args.app_id))
                elif args.credentials_op == 'set-secret':
                    _print(set_persistent_secret(getpass.getpass('Feishu App Secret: ')))
                elif args.credentials_op == 'clear-secret':
                    _print(clear_persistent_secret())
                elif args.credentials_op == 'clear-all':
                    if not args.yes and input('Clear the persistent Feishu App ID and App Secret? [y/N] ').strip().lower() not in {'y', 'yes'}:
                        _print({'Cancelled': True})
                        return 0
                    _print(clear_persistent_all())
                return 0
            except StructuredError as exc:
                _print({'error': {'code': exc.code, 'message': exc.message}})
                return 2
        from cfr.feishu.config import load_settings
        from cfr.feishu.doctor import run_feishu_doctor
        if args.feishu_op == 'doctor':
            result = run_feishu_doctor(database=args.db, codex_home=args.codex_home, codex_bin=args.codex_bin, live=args.live, gate_origin=args.gate_origin, timeout=args.timeout)
            if args.json:
                _print(result)
            else:
                print(f"Feishu Doctor: {result['Verdict']}")
                _print(result)
            return {'PASS': 0, 'WARN': 2, 'FAIL': 1}.get(result['Verdict'], 1)
        try:
            from cfr.feishu.daemon import FeishuDaemon
            from cfr.feishu.gateway import FeishuGateway
            from cfr.feishu.connection import FeishuConnectionLease
            from cfr.feishu.transport import ChannelFeishuTransport
            settings = load_settings(database=args.db, verbose=args.verbose)
            transport = ChannelFeishuTransport(settings)
            if args.setup_only:
                settings.validate_connection()
                connection_lease = FeishuConnectionLease(settings.database, f'feishu:{settings.app_namespace}').acquire()
                return _run_feishu_setup_only(transport, args.show_identifiers, connection_lease=connection_lease)
            daemon = FeishuDaemon(settings, transport)
            gateway = FeishuGateway(settings, daemon.store, daemon)
            daemon.start(background_workers=True)
            try:
                transport.connect_until_ready(gateway.handle_message_event, gateway.handle_card_action, timeout=15)
                print('FeishuConnectionReady: READY')
                while transport.is_running and not daemon.stop_event.wait(1):
                    pass
                if transport.error:
                    raise StructuredError('FEISHU_CHANNEL_UNKNOWN', 'Feishu Channel transport stopped with an error')
            finally:
                daemon.stop()
            return 0
        except StructuredError as exc:
            _print({'error': {'code': exc.code, 'message': exc.message}})
            return 2
    store = BindingStore(args.db)
    try:
        adapter = CodexAdapter(store=store, launcher=CodexLauncher(executable=args.codex_bin), codex_home=args.codex_home)
        if args.op == 'new':
            _print(asyncio.run(adapter.create_conversation(
                Path(args.cwd), args.name, args.message,
                on_server_request=_console_server_request,
            )))
        elif args.op == 'send':
            _print(asyncio.run(adapter.send_message(
                args.thread_id, args.message,
                on_server_request=_console_server_request,
            )))
        elif args.op == 'list':
            _print(store.list_bindings())
        elif args.op == 'status':
            _print(store.get_binding(args.thread_id))
        elif args.op == 'watch':
            _watch(store, args.thread_id, args.interval, args.once)
        elif args.op == 'stop':
            _print(asyncio.run(adapter.stop(args.thread_id)))
    except StructuredError as exc:
        _print({'error': {'code': exc.code, 'message': exc.message, 'data': exc.data}})
        return 2
    finally:
        store.close()
    return 0


def _watch(store, thread_id, interval, once):
    try:
        store.prune_event_dedupe()
    except sqlite3.Error:
        # This table is a secondary duplicate guard. Retention maintenance
        # must not prevent the native rollout watcher from starting.
        pass
    binding = store.get_binding(thread_id)
    if not binding:
        raise StructuredError('BINDING_NOT_FOUND', f'No binding for {thread_id}')
    if not binding.rollout_path:
        raise StructuredError('ROLLOUT_NOT_BOUND', f'No rollout path for {thread_id}')
    watcher = RolloutWatcher(thread_id=thread_id, path=binding.rollout_path, byte_offset=binding.last_rollout_byte_offset)
    projector = EventProjector(store)
    try:
        while True:
            for event in watcher.poll():
                projected = projector.project_event(event)
                if projected:
                    _print(projected)
                    if projected.turn_id:
                        store.update_last_seen_turn(thread_id, projected.turn_id)
            store.update_rollout_offset(thread_id, watcher.byte_offset)
            if once:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        store.update_rollout_offset(thread_id, watcher.byte_offset)


def cli():
    """Console-script entry point with one shared exit-code contract."""
    raise SystemExit(main())


if __name__ == '__main__':
    cli()
