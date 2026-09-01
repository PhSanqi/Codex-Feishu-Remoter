import json
import os
import sys
import time


def send(message):
    sys.stdout.write(json.dumps(message) + '\n')
    sys.stdout.flush()


for line in sys.stdin:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = message.get('method')
    request_id = message.get('id')
    if request_id == 99 and ('result' in message or 'error' in message):
        send({'jsonrpc': '2.0', 'method': 'server_response_observed', 'params': message})
        continue
    if method == 'initialize':
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {'ok': True}})
    elif method == 'initialized':
        pass
    elif method == 'echo':
        send({'jsonrpc': '2.0', 'id': request_id, 'result': message.get('params')})
    elif method == 'env':
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {'CFR_TEST_PROXY': os.environ.get('CFR_TEST_PROXY'), 'CODEX_HOME': os.environ.get('CODEX_HOME')}})
    elif method == 'error':
        send({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32001, 'message': 'fake failure', 'data': {'kind': 'test'}}})
    elif method == 'notify':
        send({'jsonrpc': '2.0', 'method': 'unrelated', 'params': {'value': 1}})
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {'ok': True}})
    elif method == 'server_request':
        send({'jsonrpc': '2.0', 'id': 99, 'method': 'approval/request', 'params': {'danger': False}})
        # The client must resolve request 99 before this RPC completes.
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {'request_was_sent': True}})
    elif method == 'approval_roundtrip':
        send({'jsonrpc': '2.0', 'id': 99, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-x', 'turnId': 'turn-x', 'itemId': 'item-x', 'item': {'command': 'echo safe', 'cwd': os.getcwd(), 'reason': 'test', 'availableDecisions': ['accept', 'decline']}}})
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {'request_was_sent': True}})
    elif method == 'malformed':
        sys.stdout.write('this is not json\n')
        sys.stdout.flush()
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {'ok': True}})
    elif method == 'hang':
        time.sleep(5)
    elif method == 'exit':
        break
