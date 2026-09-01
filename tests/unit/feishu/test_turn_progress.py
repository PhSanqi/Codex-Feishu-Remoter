import queue
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cfr.codex.turns import TurnManager
from cfr.core.models import TurnResult
from cfr.feishu.daemon import FeishuDaemon
from cfr.feishu.models import FeishuInboundMessage
from cfr.feishu.progress import FeishuTurnProgress, MAX_OUTPUT_PREVIEW_CHARS, MAX_OUTPUT_PREVIEW_LINE_CHARS, MAX_OUTPUT_PREVIEW_LINES, MAX_REASONING_PREVIEW_CHARS, MAX_REASONING_PREVIEW_LINE_CHARS, MAX_REASONING_PREVIEW_LINES


class _Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value


class _Subscription:
    def __init__(self, clock, events):
        self.clock = clock
        self.events = list(events)

    def get(self, timeout=None):
        event = self.events.pop(0)
        self.clock.value += event.pop('_advance', 0)
        if event.get('_empty'):
            raise queue.Empty()
        return event

    def close(self):
        pass


class _TurnClient:
    def __init__(self, subscription):
        self.timeout = 30
        self.subscription = subscription
        self.requests = []

    def request(self, method, params):
        self.requests.append((method, params))
        if method == 'turn/start':
            return {'turn': {'id': 'turn-1'}}
        return {'ok': True}

    def subscribe(self, _predicate):
        return self.subscription


class _ProgressReplies:
    def __init__(self, fail_update=False):
        self.cards = []
        self.updates = []
        self.fail_update = fail_update

    def send_card(self, *_args, **_kwargs):
        self.cards.append(_args[2])
        return 'card-1'

    def update_card(self, card_id, card):
        if self.fail_update:
            raise RuntimeError('card unavailable')
        self.updates.append((card_id, card))
        return card_id


class _BlockingProgressReplies(_ProgressReplies):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.current_updates = 0
        self.max_concurrent_updates = 0

    def update_card(self, card_id, card):
        with self._lock:
            self.current_updates += 1
            self.max_concurrent_updates = max(self.max_concurrent_updates, self.current_updates)
        try:
            if not self.entered.is_set():
                self.entered.set()
                self.release.wait(1)
            self.updates.append((card_id, card))
            return card_id
        finally:
            with self._lock:
                self.current_updates -= 1


class TurnTimeoutAndProgressTests(unittest.TestCase):
    @staticmethod
    def _slots(card):
        content = card['body']['elements'][0]['content']
        lines = content.splitlines()
        reasoning = '\n'.join([lines[4].removeprefix('思考：'), lines[5].removeprefix('　　　')])
        output = '\n'.join([lines[6].removeprefix('输出：'), *(line.removeprefix('　　　') for line in lines[7:10])])
        return content, reasoning, output

    def _events(self, long_first=False):
        return [
            {'_advance': 31 if long_first else 0, 'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1'}}},
            {'method': 'item/reasoning/summaryTextDelta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'delta': '安全摘要'}},
            {'method': 'item/reasoning/textDelta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'delta': 'RAW_REASONING_MUST_NOT_LEAK'}},
            {'method': 'item/started', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'item': {'type': 'commandExecution'}}},
            {'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item-1', 'delta': '完整回复第一段。'}},
            {'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item-1', 'delta': '完整回复第二段。'}},
            {'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1', 'status': 'completed'}}},
        ]

    def test_long_turn_keeps_active_registry_steer_and_safe_progress(self):
        clock = _Clock()
        client = _TurnClient(_Subscription(clock, self._events(long_first=True)))
        manager = TurnManager(client)
        progress = []
        steered = []

        def on_progress(event):
            progress.append(event)
            if event['state'] == 'running' and not steered:
                active = manager.registry.get('thread-1')
                self.assertEqual(active.turn_id, 'turn-1')
                manager.steer_turn('thread-1', active.turn_id, '继续')
                steered.append(True)

        with patch('cfr.codex.turns.time.monotonic', clock.monotonic):
            result = manager.run_turn('thread-1', '任务', turn_timeout=1800, on_progress=on_progress)

        self.assertEqual(client.timeout, 30)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.final_agent_message, '完整回复第一段。完整回复第二段。')
        self.assertEqual(manager.registry.get('thread-1'), None)
        method, params = client.requests[1]
        self.assertEqual(method, 'turn/steer')
        self.assertEqual(params['threadId'], 'thread-1')
        self.assertEqual(params['expectedTurnId'], 'turn-1')
        self.assertEqual(params['input'], [{'type': 'text', 'text': '继续'}])
        self.assertTrue(params['clientUserMessageId'].startswith('cfr-steer-'))
        self.assertNotIn('turn/interrupt', [method for method, _ in client.requests])
        self.assertNotIn('turn/start', [method for method, _ in client.requests[1:]])
        self.assertTrue(any(event.get('reasoning_summary') == '安全摘要' for event in progress))
        self.assertTrue(any(event.get('answer_preview') for event in progress))
        self.assertFalse(any('RAW_REASONING_MUST_NOT_LEAK' in repr(event) for event in progress))
        self.assertEqual(progress[-1]['state'], 'completed')
        self.assertFalse(progress[-1]['steer_available'])

    def test_configured_turn_timeout_still_finishes_as_timeout(self):
        clock = _Clock()
        client = _TurnClient(_Subscription(clock, [{'_advance': 1801, '_empty': True}]))
        manager = TurnManager(client)
        with patch('cfr.codex.turns.time.monotonic', clock.monotonic):
            result = manager.run_turn('thread-1', '任务', turn_timeout=1800)
        self.assertEqual(result.status, 'timeout')
        self.assertIsNone(manager.registry.get('thread-1'))

    def test_progress_card_coalesces_updates_and_does_not_leak_raw_reasoning(self):
        clock = _Clock()
        replies = _ProgressReplies()
        progress = FeishuTurnProgress(replies, 'message-1', 'user-1', 'workspace', clock=clock.monotonic, interval=0.01)
        progress.start()
        for index in range(20):
            progress.on_progress({'state': 'generating', 'thread_id': 'thread-1', 'turn_id': 'turn-1', 'steer_available': True, 'stage': 'generating', 'answer_preview': f'preview-{index}'})
        progress.on_progress({'reasoning_summary': '安全摘要'})
        progress.wait()
        progress.finish('completed')
        progress.wait()
        self.assertEqual(len(replies.cards), 1)
        self.assertGreaterEqual(len(replies.updates), 1)
        rendered = repr(replies.updates[-1][1])
        self.assertIn('安全摘要', rendered)
        self.assertIn('已完成', rendered)
        self.assertNotIn('RAW_REASONING_MUST_NOT_LEAK', rendered)

    def test_progress_worker_heartbeats_without_codex_events_and_terminates(self):
        clock = _Clock()
        replies = _ProgressReplies()
        progress = FeishuTurnProgress(replies, 'message-1', 'user-1', clock=clock.monotonic, interval=0.01)
        progress.start()
        time.sleep(0.02)
        clock.value = 1
        time.sleep(0.02)
        clock.value = 2
        time.sleep(0.02)
        progress.finish('completed')
        progress.wait()
        elapsed = [update[1]['body']['elements'][0]['content'].split(' · ', 2)[1].removesuffix('s') for update in replies.updates]
        self.assertGreaterEqual(len(elapsed), 3)
        self.assertGreater(int(elapsed[-1]), int(elapsed[0]))
        self.assertFalse(progress._worker.is_alive())

    def test_slow_update_keeps_latest_state_and_never_updates_concurrently(self):
        replies = _BlockingProgressReplies()
        progress = FeishuTurnProgress(replies, 'message-1', 'user-1', interval=0.01)
        progress.start()
        self.assertTrue(replies.entered.wait(0.5))
        progress.on_progress({'answer_preview': 'old'})
        progress.on_progress({'answer_preview': 'newest', 'reasoning_summary': 'latest summary'})
        replies.release.set()
        deadline = time.monotonic() + 0.5
        while len(replies.updates) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        progress.finish('completed')
        progress.wait()
        rendered = repr(replies.updates[-1][1])
        self.assertIn('newest', rendered)
        self.assertIn('latest summary', rendered)
        self.assertEqual(replies.max_concurrent_updates, 1)

    def test_slow_card_update_logs_safe_latency_diagnostic(self):
        progress = FeishuTurnProgress(_ProgressReplies(), 'message-1', 'user-1')
        progress.card_id = 'card-1'
        with patch('cfr.feishu.progress.time.monotonic', side_effect=[0, 2.1]), self.assertLogs('cfr.feishu.progress', 'WARNING') as logs:
            self.assertTrue(progress._update_card(progress._card()))
        self.assertIn('FEISHU_PROGRESS_CARD_UPDATE_SLOW elapsed_ms=2100', logs.output[0])

    def test_progress_card_uses_card_v2_body_markdown_shape(self):
        card = FeishuTurnProgress(_ProgressReplies(), 'message-1', 'user-1')._card()
        self.assertEqual(card['schema'], '2.0')
        self.assertNotIn('elements', card)
        self.assertIn('body', card)
        self.assertIsInstance(card['body']['elements'], list)
        self.assertEqual(card['body']['elements'][0]['tag'], 'markdown')
        self.assertIsInstance(card['body']['elements'][0]['content'], str)
        self.assertNotIn('div', repr(card))
        self.assertNotIn('lark_md', repr(card))

    def test_card_update_failure_does_not_fail_native_turn(self):
        clock = _Clock()
        replies = _ProgressReplies(fail_update=True)
        progress = FeishuTurnProgress(replies, 'message-1', 'user-1', clock=clock.monotonic, interval=0)
        progress.start()
        client = _TurnClient(_Subscription(clock, self._events()))
        with patch('cfr.codex.turns.time.monotonic', clock.monotonic):
            result = TurnManager(client).run_turn('thread-1', '任务', turn_timeout=1800, on_progress=progress.on_progress)
        progress.finish(result)
        progress.wait()
        self.assertEqual(result.status, 'completed')
        self.assertTrue(progress.disabled)

    def test_native_steer_user_message_projects_received_without_text(self):
        clock = _Clock()
        events = [
            {'method': 'item/started', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'item': {'type': 'userMessage', 'clientId': 'ordinary-user-message'}}},
            {'method': 'item/started', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'item': {'type': 'userMessage', 'clientId': 'cfr-steer-accepted'}}},
            {'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1', 'status': 'completed'}}},
        ]
        progress = []
        with patch('cfr.codex.turns.time.monotonic', clock.monotonic):
            TurnManager(_TurnClient(_Subscription(clock, events))).run_turn('thread-1', '任务', turn_timeout=1800, on_progress=progress.append)
        self.assertEqual(sum(bool(event.get('steer_received')) for event in progress), 1)
        self.assertFalse(any('ordinary-user-message' in repr(event) for event in progress))

    def test_stages_and_native_activity_are_projected_without_visible_output(self):
        clock = _Clock()
        events = [
            {'method': 'item/started', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'item': {'type': 'reasoning'}}},
            {'method': 'item/started', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'item': {'type': 'plan'}}},
            {'method': 'item/started', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'item': {'type': 'agentMessage'}}},
            {'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1', 'status': 'completed'}}},
        ]
        progress_events = []
        with patch('cfr.codex.turns.time.monotonic', clock.monotonic):
            TurnManager(_TurnClient(_Subscription(clock, events))).run_turn('thread-1', '任务', turn_timeout=1800, on_progress=progress_events.append)
        self.assertTrue(any(event.get('stage') == 'reasoning' for event in progress_events))
        self.assertTrue(any(event.get('stage') == 'planning' for event in progress_events))
        self.assertTrue(any(event.get('stage') == 'generating' for event in progress_events))
        self.assertTrue(any(event.get('last_native_activity_at') == 0 for event in progress_events))
        card = FeishuTurnProgress(_ProgressReplies(), 'message-1', 'user-1', clock=clock.monotonic)
        card.on_progress({'state': 'running', 'stage': 'reasoning', 'last_native_activity_at': 0})
        clock.value = 2
        rendered = card._card()['body']['elements'][0]['content']
        self.assertIn('模型正在推理', rendered)
        self.assertIn('活动：2s 前', rendered)

    def test_progress_card_keeps_only_latest_compact_reasoning_and_output_tails(self):
        clock = _Clock()
        events = [{'method': 'item/reasoning/summaryTextDelta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'delta': 'EARLY_REASONING\n'}}]
        events += [{'method': 'item/reasoning/summaryTextDelta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'delta': f'reason-{index}\n'}} for index in range(100)]
        events += [{'method': 'item/reasoning/summaryTextDelta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'delta': 'LATEST_REASONING'}}, {'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item', 'delta': 'OLD_START_TEXT\n'}}]
        events += [{'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item', 'delta': f'output-{index}\n'}} for index in range(500)]
        events += [
            {'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item', 'delta': 'NEWEST_OUTPUT_MARKER'}},
            {'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1', 'status': 'completed'}}},
        ]
        card = FeishuTurnProgress(_ProgressReplies(), 'message-1', 'user-1', clock=clock.monotonic)
        with patch('cfr.codex.turns.time.monotonic', clock.monotonic):
            TurnManager(_TurnClient(_Subscription(clock, events))).run_turn('thread-1', '任务', turn_timeout=1800, on_progress=card.on_progress)
        rendered, reasoning, output = self._slots(card._card())
        self.assertLessEqual(len(reasoning), MAX_REASONING_PREVIEW_CHARS + 1)
        self.assertEqual(len(reasoning.splitlines()), MAX_REASONING_PREVIEW_LINES)
        self.assertLessEqual(len(output), MAX_OUTPUT_PREVIEW_CHARS + 1)
        self.assertEqual(len(output.splitlines()), MAX_OUTPUT_PREVIEW_LINES)
        self.assertIn('LATEST_REASONING', reasoning)
        self.assertNotIn('EARLY_REASONING', reasoning)
        self.assertIn('NEWEST_OUTPUT_MARKER', output)
        self.assertNotIn('OLD_START_TEXT', output)
        self.assertEqual(len(rendered.splitlines()), 11)
        card.finish('completed')
        terminal = card._card()['body']['elements'][0]['content']
        self.assertEqual(len(terminal.splitlines()), 11)
        self.assertTrue(terminal.startswith('已完成 ·'))

    def test_fixed_slots_are_present_across_lifecycle_states(self):
        card = FeishuTurnProgress(_ProgressReplies(), 'message-1', 'user-1')
        sections = ('workspace', 's · ', '/steer：', '/redirect：', '线程：', 'Turn：', '阶段：', '活动：', '思考：', '输出：', '引导：')
        for state, stage in (('accepted', 'starting'), ('running', 'running'), ('running', 'reasoning'), ('generating', 'generating'), ('completed', 'generating'), ('stopped', 'running')):
            card.workspace = 'workspace'
            card.on_progress({'state': state, 'stage': stage, 'thread_id': 'thread-12345678', 'turn_id': 'turn-87654321'})
            rendered, reasoning, output = self._slots(card._card())
            self.assertTrue(all(section in rendered for section in sections))
            self.assertIn('线程：12345678 · Turn：87654321', rendered)
            self.assertEqual(len(rendered.splitlines()), 11)
            self.assertEqual(len(reasoning.splitlines()), MAX_REASONING_PREVIEW_LINES)
            self.assertEqual(len(output.splitlines()), MAX_OUTPUT_PREVIEW_LINES)

    def test_fixed_preview_slots_keep_line_budgets_and_latest_tails(self):
        card = FeishuTurnProgress(_ProgressReplies(), 'message-1', 'user-1')
        for reasoning, output in ((None, None), ('short', 'one line'), ('EARLY_REASONING_MARKER\n' + 'x' * 600 + 'LATEST_REASONING_MARKER', 'OLD_START_MARKER\n' + 'y' * 1200 + 'NEWEST_OUTPUT_MARKER')):
            card.on_progress({'reasoning_summary': reasoning, 'answer_preview': output})
            _, reasoning_slot, output_slot = self._slots(card._card())
            self.assertEqual(len(reasoning_slot.splitlines()), MAX_REASONING_PREVIEW_LINES)
            self.assertEqual(len(output_slot.splitlines()), MAX_OUTPUT_PREVIEW_LINES)
        self.assertIn('LATEST_REASONING_MARKER', reasoning_slot.replace('\n', ''))
        self.assertNotIn('EARLY_REASONING_MARKER', reasoning_slot)
        self.assertIn('NEWEST_OUTPUT_MARKER', output_slot.replace('\n', ''))
        self.assertNotIn('OLD_START_MARKER', output_slot)

    def test_compact_preview_lines_bound_cjk_content_and_keep_latest_tail(self):
        card = FeishuTurnProgress(_ProgressReplies(), 'message-1', 'user-1')
        card.on_progress({
            'reasoning_summary': '早期思考标记\n' + '正在检查当前线程状态与新方向之间的执行边界。' * 40 + '最新思考标记',
            'answer_preview': '早期输出标记\n' + '正在验证真实业务路径并保持接口兼容。' * 100 + '最新输出标记',
        })
        _, reasoning, output = self._slots(card._card())
        self.assertEqual(len(reasoning.splitlines()), MAX_REASONING_PREVIEW_LINES)
        self.assertEqual(len(output.splitlines()), MAX_OUTPUT_PREVIEW_LINES)
        self.assertTrue(all(line == '—' or len(line) <= MAX_REASONING_PREVIEW_LINE_CHARS for line in reasoning.splitlines()))
        self.assertTrue(all(line == '—' or len(line) <= MAX_OUTPUT_PREVIEW_LINE_CHARS for line in output.splitlines()))
        self.assertIn('最新思考标记', reasoning)
        self.assertNotIn('早期思考标记', reasoning)
        self.assertIn('最新输出标记', output)
        self.assertNotIn('早期输出标记', output)

    def test_fixed_layout_does_not_grow_for_streaming_terminal_or_steer(self):
        card = FeishuTurnProgress(_ProgressReplies(), 'message-1', 'user-1')
        initial, _, _ = self._slots(card._card())
        card.on_progress({'state': 'running', 'stage': 'generating', 'reasoning_summary': 'summary\n' * 100, 'answer_preview': 'output\n' * 500})
        streaming, _, _ = self._slots(card._card())
        card.on_progress({'steer_received': True})
        steered, _, _ = self._slots(card._card())
        card.finish('completed')
        terminal, reasoning, output = self._slots(card._card())
        self.assertEqual(len(initial.splitlines()), len(streaming.splitlines()))
        self.assertEqual(len(initial.splitlines()), len(steered.splitlines()))
        self.assertEqual(len(initial.splitlines()), len(terminal.splitlines()))
        self.assertIn('引导：—', initial)
        self.assertIn('引导：Codex 已读取', steered)
        self.assertEqual(len(reasoning.splitlines()), MAX_REASONING_PREVIEW_LINES)
        self.assertEqual(len(output.splitlines()), MAX_OUTPUT_PREVIEW_LINES)


class _DaemonReplies(_ProgressReplies):
    def __init__(self):
        super().__init__()
        self.text = []

    def reply_text(self, message_id, text, phase, **_kwargs):
        self.text.append((message_id, text, phase))
        return ['reply-1']


class _DaemonAdapter:
    def __init__(self, result):
        self.result = result

    async def send_message(self, _thread_id, _message, **kwargs):
        kwargs['on_progress']({'state': 'running', 'thread_id': self.result.thread_id, 'turn_id': self.result.turn_id, 'steer_available': True, 'stage': 'running'})
        return self.result


class FeishuFinalReplyTests(unittest.TestCase):
    def _daemon(self, result):
        daemon = FeishuDaemon.__new__(FeishuDaemon)
        daemon.replies = _DaemonReplies()
        daemon.adapter = _DaemonAdapter(result)
        daemon.store = SimpleNamespace(get_session=lambda _chat: SimpleNamespace(state='bound', thread_id='thread-1', pending_cwd=None))
        daemon.binding_store = SimpleNamespace(get_binding=lambda _thread: SimpleNamespace(cwd=Path('workspace')))
        daemon.approvals = SimpleNamespace(finalize_feedback_for_turn=lambda **_kwargs: None)
        return daemon

    def test_complete_final_reply_keeps_full_turn_message(self):
        daemon = self._daemon(TurnResult('thread-1', 'turn-1', 'completed', '完整最终回复'))
        daemon._handle_prompt(FeishuInboundMessage('event', 'message', 'chat', 'p2p', 'user', 'user', 'text', '任务'))
        self.assertEqual(daemon.replies.text[-1][1], '完整最终回复')

    def test_timeout_never_presents_partial_output_as_completed(self):
        daemon = self._daemon(TurnResult('thread-1', 'turn-1', 'timeout', '不应作为成功回复的部分内容'))
        daemon._handle_prompt(FeishuInboundMessage('event', 'message', 'chat', 'p2p', 'user', 'user', 'text', '任务'))
        self.assertEqual(daemon.replies.text[-1][1], '任务执行超时，未正常完成。')
        self.assertNotIn('部分内容', daemon.replies.text[-1][1])


if __name__ == '__main__':
    unittest.main()
