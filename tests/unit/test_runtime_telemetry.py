import json
import unittest

from cfr.codex.turns import ActiveTurnRegistry, RECENT_TURN_LIMIT, TurnManager
from cfr.core.models import TURN_TIMELINE_MAX_EVENTS, TurnTelemetry


class _Subscription:
    def __init__(self, events):
        self.events = iter(events)

    def get(self, timeout=None):
        return next(self.events)

    def close(self):
        pass


class _Client:
    timeout = 1

    def __init__(self, status='completed'):
        self.status = status

    def subscribe(self, _predicate):
        return _Subscription([
            {'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1'}}},
            {'method': 'item/reasoning/textDelta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'delta': 'RAW_REASONING_SECRET'}},
            {'method': 'item/reasoning/summaryTextDelta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'delta': 'SUMMARY_SECRET'}},
            {'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'answer', 'delta': 'ok'}},
            {'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1', 'status': self.status}}},
        ])

    def request(self, method, _params):
        if method == 'turn/start':
            return {'turn': {'id': 'turn-1'}}
        raise AssertionError(method)


class _RetryClient(_Client):
    def subscribe(self, _predicate):
        return _Subscription([
            {'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1'}}},
            {'method': 'error', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'willRetry': True, 'error': {'message': 'Reconnecting... 2/5', 'additionalDetails': 'NETWORK_SECRET_10054'}}},
            {'method': 'warning', 'params': {'threadId': 'thread-1', 'message': 'Falling back from WebSockets to HTTPS transport. NETWORK_SECRET_FALLBACK'}},
            {'method': 'item/reasoning/summaryTextDelta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'delta': 'SUMMARY_SECRET'}},
            {'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'answer', 'delta': 'ok'}},
            {'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1', 'status': 'completed'}}},
        ])


class _StartFailureClient(_Client):
    def subscribe(self, _predicate):
        return _Subscription([])

    def request(self, method, _params):
        if method == 'turn/start':
            raise RuntimeError('start failed')
        raise AssertionError(method)


class RuntimeTelemetryTests(unittest.TestCase):
    def test_metrics_tokens_and_terminal_stage_are_projected(self):
        telemetry = TurnTelemetry('message-secret', thread_id='thread-1', turn_id='turn-1')
        telemetry.mark_wall('task_received_at', 100.0)
        telemetry.mark_wall('task_queued_at', 100.1)
        telemetry.mark('task_execution_started_at', at=1.0, wall_at=100.2)
        telemetry.mark('app_server_start_started_at', at=1.1, wall_at=100.3)
        telemetry.mark('app_server_started_at', at=1.3, wall_at=100.5)
        telemetry.mark('initialize_started_at', at=1.4, wall_at=100.6)
        telemetry.mark('initialize_completed_at', at=1.8, wall_at=101.0)
        telemetry.mark('thread_resume_started_at', at=1.9, wall_at=101.1)
        telemetry.mark('thread_resume_completed_at', at=2.4, wall_at=101.6)
        telemetry.mark('turn_start_requested_at', at=2.5, wall_at=101.7)
        telemetry.mark('turn_started_at', at=2.6, wall_at=101.8)
        telemetry.observe_native_activity(at=3.0, wall_at=102.2)
        telemetry.mark('reasoning_started_at', at=3.0, wall_at=102.2)
        telemetry.mark('agent_message_started_at', at=4.0, wall_at=103.2)
        telemetry.mark('first_answer_delta_at', at=4.1, wall_at=103.3)
        telemetry.mark('turn_completed_at', at=4.6, wall_at=103.8)
        telemetry.mark('runtime_cleanup_started_at', at=4.7, wall_at=103.9)
        telemetry.mark('runtime_cleanup_completed_at', at=4.9, wall_at=104.1)
        telemetry.mark('final_reply_started_at', at=5.0, wall_at=104.2)
        telemetry.mark('final_reply_completed_at', at=5.1, wall_at=104.3)
        telemetry.set_runtime_settings({'model': 'gpt-test', 'reasoningEffort': 'medium', 'serviceTier': 'default'})
        telemetry.update_token_usage({'tokenUsage': {'total': {'inputTokens': 10, 'cachedInputTokens': 2, 'cacheWriteInputTokens': 123, 'outputTokens': 3, 'reasoningOutputTokens': 1, 'totalTokens': 13}, 'last': {'totalTokens': 7}, 'modelContextWindow': 100}})
        telemetry.set_stage('completed', status='completed', event='Turn completed', at=4.6, wall_at=103.8)

        snapshot = telemetry.snapshot(now=5.1, wall_now=104.3)
        self.assertEqual(snapshot['stage'], 'completed')
        self.assertEqual(snapshot['status'], 'completed')
        self.assertEqual(snapshot['metrics']['queue_ms'], 100)
        self.assertEqual(snapshot['metrics']['app_server_start_ms'], 200)
        self.assertEqual(snapshot['metrics']['initialize_ms'], 400)
        self.assertEqual(snapshot['metrics']['thread_resume_ms'], 500)
        self.assertEqual(snapshot['metrics']['turn_start_ms'], 100)
        self.assertEqual(snapshot['metrics']['ttfn_ms'], 400)
        self.assertEqual(snapshot['metrics']['ttft_ms'], 1500)
        self.assertEqual(snapshot['metrics']['generation_ms'], 500)
        self.assertEqual(snapshot['metrics']['cleanup_ms'], 200)
        self.assertEqual(snapshot['metrics']['final_delivery_ms'], 100)
        self.assertEqual(snapshot['context_used_tokens'], 7)
        self.assertEqual(snapshot['context_usage_percent'], 7.0)
        self.assertEqual(snapshot['token_usage']['cache_write_input_tokens'], 123)
        self.assertEqual(snapshot['last_native_activity_age_ms'], 2100)
        ordered = [snapshot['timestamps'][name] for name in ('turn_started_at', 'first_native_activity_at', 'first_answer_delta_at', 'turn_completed_at')]
        self.assertEqual(ordered, sorted(ordered))

    def test_tool_projection_is_bounded_and_does_not_leak_payloads(self):
        telemetry = TurnTelemetry('message-secret')
        item = {
            'id': 'tool-1', 'type': 'mcpToolCall', 'server': 'github', 'tool': 'get_repo',
            'arguments': {'token': 'ARGUMENT_SECRET'}, 'result': 'RESULT_SECRET',
            'output': 'OUTPUT_SECRET', 'patch': 'PATCH_SECRET',
        }
        telemetry.observe_tool(item, at=1.0, wall_at=100.0)
        telemetry.observe_tool({**item, 'status': 'completed', 'durationMs': 25}, completed=True, at=1.1, wall_at=100.1)
        for index in range(TURN_TIMELINE_MAX_EVENTS + 5):
            telemetry.add_event(f'event-{index}', at=2 + index, wall_at=102 + index)
        payload = json.dumps(telemetry.snapshot(now=30, wall_now=130))
        self.assertNotIn('message-secret', json.dumps({key: value for key, value in telemetry.snapshot(now=30, wall_now=130).items() if key != 'correlation_id'}))
        for secret in ('ARGUMENT_SECRET', 'RESULT_SECRET', 'OUTPUT_SECRET', 'PATCH_SECRET'):
            self.assertNotIn(secret, payload)
        self.assertEqual(len(telemetry.snapshot(now=30, wall_now=130)['timeline']), TURN_TIMELINE_MAX_EVENTS)
        self.assertEqual(telemetry.snapshot(now=30, wall_now=130)['timeline'][0]['label'], 'event-5')
        self.assertEqual(telemetry.recent_tool['name'], 'github · get_repo')

    def test_missing_measurements_remain_null(self):
        snapshot = TurnTelemetry('correlation').snapshot(now=1.0, wall_now=1.0)
        self.assertIsNone(snapshot['metrics']['ttft_ms'])
        self.assertIsNone(snapshot['metrics']['cleanup_ms'])
        self.assertIsNone(snapshot['context_usage_percent'])
        self.assertIsNone(snapshot['last_native_activity_age_ms'])
        self.assertNotIn('cache_write_input_tokens', snapshot['token_usage'])

        telemetry = TurnTelemetry('zero-age')
        telemetry.observe_native_activity(at=3.0, wall_at=12.0)
        self.assertEqual(telemetry.snapshot(now=3.0, wall_now=12.0)['last_native_activity_age_ms'], 0)

    def test_turn_started_updates_last_activity_without_starting_ttfn(self):
        telemetry = TurnTelemetry('correlation')
        telemetry.mark('turn_started_at', at=10.0, wall_at=100.0)
        telemetry.observe_native_activity(at=10.1, wall_at=100.1, count_for_ttfn=False)

        snapshot = telemetry.snapshot(now=20.1, wall_now=110.1)
        self.assertEqual(snapshot['last_native_activity_age_ms'], 10000)
        self.assertIsNone(snapshot['metrics']['ttfn_ms'])

        telemetry.observe_native_activity(at=25.1, wall_at=115.1)
        snapshot = telemetry.snapshot(now=25.1, wall_now=115.1)
        self.assertEqual(snapshot['last_native_activity_age_ms'], 0)
        self.assertEqual(snapshot['metrics']['ttfn_ms'], 15100)

    def test_cfr_controlled_overhead_excludes_model_wait(self):
        telemetry = TurnTelemetry('correlation')
        telemetry.mark('task_execution_started_at', at=1.0, wall_at=1.0)
        telemetry.mark('turn_started_at', at=3.2, wall_at=3.2)
        self.assertEqual(telemetry.snapshot(now=100.0, wall_now=100.0)['metrics']['cfr_controlled_overhead_ms'], 2200)
        telemetry.mark('first_answer_delta_at', at=100.0, wall_at=100.0)
        telemetry.mark('turn_completed_at', at=110.0, wall_at=110.0)
        telemetry.mark('final_reply_completed_at', at=110.07, wall_at=110.07)
        metrics = telemetry.snapshot(now=110.07, wall_now=110.07)['metrics']
        self.assertEqual(metrics['cfr_post_turn_ms'], 70)
        self.assertEqual(metrics['cfr_controlled_overhead_ms'], 2270)

    def test_native_reasoning_is_not_retained_and_terminal_stage_matches(self):
        for native_status, expected_stage in (('completed', 'completed'), ('interrupted', 'stopped'), ('failed', 'failed')):
            registry = ActiveTurnRegistry()
            result = TurnManager(_Client(native_status), registry).run_turn('thread-1', 'PROMPT_SECRET')
            snapshot = result.telemetry.snapshot()
            encoded = json.dumps(snapshot)
            self.assertEqual(snapshot['stage'], expected_stage)
            self.assertEqual(snapshot['status'], expected_stage)
            for secret in ('PROMPT_SECRET', 'RAW_REASONING_SECRET', 'SUMMARY_SECRET'):
                self.assertNotIn(secret, encoded)

    def test_transport_retry_and_https_fallback_are_classified_without_raw_error_details(self):
        result = TurnManager(_RetryClient(), ActiveTurnRegistry()).run_turn('thread-1', 'PROMPT_SECRET')
        snapshot = result.telemetry.snapshot()
        encoded = json.dumps(snapshot)
        self.assertEqual(snapshot['transport_state'], 'https_fallback')
        self.assertEqual(snapshot['transport_retry_count'], 1)
        self.assertEqual(snapshot['transport_fallback_count'], 1)
        self.assertIsNotNone(snapshot['metrics']['transport_retry_ms'])
        self.assertIsNotNone(snapshot['metrics']['model_response_wait_ms'])
        self.assertNotIn('NETWORK_SECRET_10054', encoded)
        self.assertNotIn('NETWORK_SECRET_FALLBACK', encoded)
        self.assertNotIn('PROMPT_SECRET', encoded)

    def test_registry_recent_telemetry_is_hard_bounded(self):
        registry = ActiveTurnRegistry()
        for index in range(RECENT_TURN_LIMIT + 5):
            registry.begin(f'correlation-{index}', received_at=1, queued_at=1, execution_started_at=1)
        snapshots = registry.telemetry_snapshots()
        self.assertEqual(len(snapshots), RECENT_TURN_LIMIT)
        self.assertEqual(len(registry._pending), RECENT_TURN_LIMIT)

    def test_turn_start_failure_is_terminal_and_not_left_pending(self):
        registry = ActiveTurnRegistry()
        with self.assertRaises(RuntimeError):
            TurnManager(_StartFailureClient(), registry).run_turn('thread-1', 'prompt')
        snapshot = registry.telemetry_snapshots()[0]
        self.assertEqual(snapshot['status'], 'failed')
        self.assertEqual(snapshot['stage'], 'failed')
        self.assertEqual(len(registry._pending), 0)

    def test_invalid_turn_timeout_is_rejected_before_subscription(self):
        client = _Client()
        for value in (0, -1, float('inf'), 'invalid'):
            with self.subTest(value=value), self.assertRaises(Exception) as caught:
                TurnManager(client, ActiveTurnRegistry()).run_turn('thread-1', 'prompt', timeout=value)
            self.assertIn('CODEX_TURN_TIMEOUT_INVALID', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
