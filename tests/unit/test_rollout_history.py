import json
from pathlib import Path
import tempfile
import unittest

from cfr.codex.rollout import RolloutWatcher, recent_rollout_messages


class RolloutHistoryTests(unittest.TestCase):
    def test_recent_history_deduplicates_native_visible_message_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollout.jsonl'
            records = [
                {'timestamp': 't1', 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'hello', 'turn_id': 'turn-1'}},
                {'timestamp': 't1', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}], 'internal_chat_message_metadata_passthrough': {'turn_id': 'turn-1'}}},
                {'timestamp': 't2', 'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'world', 'turn_id': 'turn-1'}},
                {'timestamp': 't2', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'world'}], 'internal_chat_message_metadata_passthrough': {'turn_id': 'turn-1'}}},
            ]
            path.write_text('\n'.join(json.dumps(item) for item in records) + '\n', encoding='utf-8')
            history = recent_rollout_messages(path, limit=10)
            self.assertEqual([(item['role'], item['text']) for item in history], [('user', 'hello'), ('assistant', 'world')])

    def test_history_reads_only_tail_after_large_tool_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollout.jsonl'
            huge = {'timestamp': 't0', 'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'x' * 20_000}}
            user = {'timestamp': 't1', 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'recent question', 'turn_id': 'turn-2'}}
            answer = {'timestamp': 't2', 'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'recent answer', 'turn_id': 'turn-2'}}
            path.write_text('\n'.join(json.dumps(item) for item in (huge, user, answer)) + '\n', encoding='utf-8')
            history = recent_rollout_messages(path, limit=10, max_bytes=4096)
            self.assertEqual([item['text'] for item in history], ['recent question', 'recent answer'])

    def test_history_expands_bounded_tail_when_large_final_record_hides_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollout.jsonl'
            user = {'timestamp': 't1', 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'question', 'turn_id': 'turn-2'}}
            answer = {'timestamp': 't2', 'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'answer', 'turn_id': 'turn-2'}}
            huge = {'timestamp': 't3', 'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'x' * 20_000}}
            path.write_text('\n'.join(json.dumps(item) for item in (user, answer, huge)) + '\n', encoding='utf-8')
            history = recent_rollout_messages(path, limit=2, max_bytes=4096, max_total_bytes=65536)
            self.assertEqual([item['text'] for item in history], ['question', 'answer'])

    def test_rollout_watcher_bounds_each_poll_and_skips_one_oversized_non_message_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollout.jsonl'
            huge = {'timestamp': 't0', 'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'x' * 20_000}}
            user = {'timestamp': 't1', 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'after huge', 'turn_id': 'turn-2'}}
            path.write_text('\n'.join(json.dumps(item) for item in (huge, user)) + '\n', encoding='utf-8')
            watcher = RolloutWatcher('thread-1', path, max_poll_bytes=4096)
            events = []
            for _ in range(10):
                events.extend(watcher.poll())
                if events:
                    break
            self.assertEqual([event.text for event in events], ['after huge'])
            self.assertLessEqual(watcher.byte_offset, path.stat().st_size)


if __name__ == '__main__':
    unittest.main()
