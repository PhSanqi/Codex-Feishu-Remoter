from __future__ import annotations

import logging
import threading
import time


LOGGER = logging.getLogger(__name__)
MAX_REASONING_PREVIEW_CHARS = 500
MAX_REASONING_PREVIEW_LINES = 2
MAX_OUTPUT_PREVIEW_CHARS = 1000
MAX_OUTPUT_PREVIEW_LINES = 4
MAX_REASONING_PREVIEW_LINE_CHARS = 22
MAX_OUTPUT_PREVIEW_LINE_CHARS = 22
EMPTY_SLOT = '—'


class FeishuTurnProgress:
    """Ephemeral, throttled presentation of one native Codex turn."""

    def __init__(self, replies, message_id, open_id, workspace=None, *, clock=time.monotonic, interval=1.0):
        self.replies = replies
        self.message_id = message_id
        self.open_id = open_id
        self.workspace = workspace
        self.clock = clock
        self.interval = interval
        self.started_at = clock()
        self.card_id = None
        self.disabled = False
        self._terminal = False
        self._worker = None
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._state = {'state': 'accepted', 'steer_available': False, 'stage': 'starting'}

    def start(self):
        try:
            self.card_id = self.replies.send_card(self.message_id, self.open_id, self._card(), phase='progress')
        except Exception as error:
            self.disabled = True
            LOGGER.warning('FEISHU_PROGRESS_CARD_SEND_FAILED type=%s', type(error).__name__)
            return None
        self._worker = threading.Thread(target=self._run, name='cfr-feishu-progress', daemon=True)
        self._worker.start()
        return self.card_id

    def on_progress(self, event):
        if self.disabled or not isinstance(event, dict):
            return
        with self._lock:
            for key in ('state', 'thread_id', 'turn_id', 'steer_available', 'stage', 'reasoning_summary', 'answer_preview', 'steer_received', 'last_native_activity_at'):
                if key in event:
                    self._state[key] = event[key]

    def finish(self, turn):
        status = getattr(turn, 'status', turn)
        state = {'completed': 'completed', 'timeout': 'timed_out', 'timed_out': 'timed_out', 'interrupted': 'stopped'}.get(status, 'failed')
        self.on_progress({'state': state, 'stage': state, 'steer_available': False})
        with self._lock:
            self._terminal = True
        self._wake.set()

    def wait(self, timeout=1):
        if self._worker:
            self._worker.join(timeout)

    def _run(self):
        while True:
            self._wake.wait(self.interval)
            self._wake.clear()
            with self._lock:
                card = self._card()
                terminal = self._terminal
            if not self._update_card(card):
                return
            if terminal:
                return

    def _update_card(self, card):
        started = time.monotonic()
        try:
            self.replies.update_card(self.card_id, card)
        except Exception as error:
            self.disabled = True
            LOGGER.warning('FEISHU_PROGRESS_CARD_UPDATE_FAILED type=%s', type(error).__name__)
            return False
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if elapsed_ms > 2000:
                LOGGER.warning('FEISHU_PROGRESS_CARD_UPDATE_SLOW elapsed_ms=%s', elapsed_ms)
        return True

    def _card(self):
        state = self._state
        names = {'accepted': '已接收', 'running': '运行中', 'generating': '正在生成回复', 'completed': '已完成', 'failed': '失败', 'timed_out': '超时', 'stopped': '已停止'}
        stages = {'starting': '正在准备任务', 'running': 'Codex 正在处理', 'planning': '正在规划', 'reasoning': '模型正在推理', 'generating': '正在生成回复', 'tool': '正在执行工具', 'file_change': '正在处理文件变更', 'multi_agent': '正在协调 Codex 子任务', 'transport_retry': 'Codex 正在重连模型服务', 'transport_fallback': 'Codex 已切换 HTTPS'}
        active = bool(state.get('steer_available'))
        stage = state.get('stage', 'running')
        explanations = {
            'reasoning': ('模型正在推理，当前尚无可安全展示的思考摘要。', None),
            'planning': ('Codex 正在规划下一步执行。', None),
            'generating': (None, '模型已进入回复生成阶段，正在等待首个可展示输出。'),
        }
        reasoning, output = explanations.get(stage, (None, None)) if not state.get('reasoning_summary') and not state.get('answer_preview') else (None, None)
        if state.get('last_native_activity_at') is None:
            activity = EMPTY_SLOT
        else:
            age = max(0, int(self.clock() - state['last_native_activity_at']))
            activity = '刚刚' if age == 0 else f'{age}s 前'
        reasoning_lines = self._fixed_preview_lines(state.get('reasoning_summary') or reasoning, MAX_REASONING_PREVIEW_CHARS, MAX_REASONING_PREVIEW_LINES, MAX_REASONING_PREVIEW_LINE_CHARS).splitlines()
        output_lines = self._fixed_preview_lines(state.get('answer_preview') or output, MAX_OUTPUT_PREVIEW_CHARS, MAX_OUTPUT_PREVIEW_LINES, MAX_OUTPUT_PREVIEW_LINE_CHARS).splitlines()
        workspace = str(self.workspace or EMPTY_SLOT).rstrip('/\\').replace('\\', '/').rsplit('/', 1)[-1]
        lines = [
            f'{names.get(state.get("state"), "运行中")} · {int(self.clock() - self.started_at)}s · {workspace}',
            f'阶段：{stages.get(stage, "Codex 正在处理")} · 活动：{activity}',
            f'线程：{str(state["thread_id"])[-8:] if state.get("thread_id") else EMPTY_SLOT} · Turn：{str(state["turn_id"])[-8:] if state.get("turn_id") else EMPTY_SLOT}',
            f'/steer：{"可" if active else "否"} · /redirect：{"可" if active else "否"}',
            f'思考：{reasoning_lines[0]}',
            f'　　　{reasoning_lines[1]}',
            f'输出：{output_lines[0]}',
            *(f'　　　{line}' for line in output_lines[1:]),
            f'引导：{"Codex 已读取" if state.get("steer_received") else EMPTY_SLOT}',
        ]
        return {'schema': '2.0', 'header': {'title': {'tag': 'plain_text', 'content': 'CFR · Codex · 执行过程'}}, 'body': {'elements': [{'tag': 'markdown', 'content': '\n'.join(lines)}]}}

    @staticmethod
    def _fixed_preview_lines(value, max_chars, line_count, line_chars):
        text = str(value or '')
        truncated = len(text) > max_chars
        text = text[-max_chars:]
        if truncated and '\n' in text:
            text = text.split('\n', 1)[1] or text
        lines = []
        for part in text.splitlines():
            chunks = []
            while part:
                chunks.append(part[-line_chars:])
                part = part[:-line_chars]
            lines.extend(reversed(chunks))
        if len(lines) > line_count:
            lines = lines[-line_count:]
            truncated = True
        if truncated and lines:
            lines[0] = '…' + lines[0][-(line_chars - 1):]
        return '\n'.join(lines + [EMPTY_SLOT] * (line_count - len(lines)))
