from pathlib import Path
import asyncio
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))

import run_m2b_feishu_approval_live_probe as probe


class M2BProbeContractTests(unittest.TestCase):
    def test_schema_probe_uses_out_directory_and_stable_fallback(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if '--experimental' in command:
                return SimpleNamespace(returncode=1, stdout='', stderr='unknown option')
            out = Path(command[command.index('--out') + 1])
            (out / 'app_server.json').write_text(
                '{"item/commandExecution/requestApproval": true, "item/fileChange/requestApproval": true, '
                '"approvalPolicy": true, "approvalsReviewer": true, "sandbox": true, '
                '"accept": true, "decline": true, "cancel": true}',
                encoding='utf-8',
            )
            return SimpleNamespace(returncode=0, stdout='schema was written', stderr='')

        with tempfile.TemporaryDirectory() as directory, patch.object(probe.subprocess, 'run', side_effect=fake_run):
            result = probe.schema_probe(Path(directory), SimpleNamespace(build_command=lambda *args: list(args)))
        self.assertEqual(result['SchemaCommand'], 'PASS')
        self.assertTrue(result['ApprovalSchemaVerified'])
        self.assertEqual(result['SchemaFileCount'], 1)
        self.assertEqual(calls[0][0:3], ['app-server', 'generate-json-schema', '--out'])
        self.assertIn('--experimental', calls[0])
        self.assertNotIn('--experimental', calls[1])

    def test_effective_thread_config_is_separate_from_requested(self):
        params, requested = probe._requested_thread_params(
            'approvalPolicy approvalsReviewer sandbox item/commandExecution/requestApproval',
            Path('workspace'),
        )
        self.assertEqual(params['approvalPolicy'], 'on-request')
        self.assertEqual(requested['ApprovalPolicyRequested'], 'on-request')
        self.assertNotIn('Effective', requested)

    def test_mode_contract_exposes_real_evidence_fields(self):
        result = probe.contract_result('run', 'allow')
        for field in ('PendingObserved', 'ServerRequestResolvedEvidence', 'ExactlyOnce', 'NoAutoApprove', 'ApprovalSchemaVerified'):
            self.assertIn(field, result)
        self.assertEqual(result['Verdict'], 'NOT_RUN')

    def test_decline_prompt_is_independent(self):
        self.assertIn('decline_probe.txt', probe.DECLINE_PROMPT)
        self.assertIn('CFR_M2B_DECLINE_SHOULD_NOT_EXIST', probe.DECLINE_PROMPT)
        self.assertNotEqual(probe.ALLOW_PROMPT, probe.DECLINE_PROMPT)

    def test_decline_live_marker_absence_metadata_is_not_run(self):
        with tempfile.TemporaryDirectory() as directory:
            result = probe._mode_result('decline')
            probe.apply_marker_contract(result, Path(directory) / 'decline_probe.txt', probe.DECLINE_MARKER_BYTES, 'Decline')
        self.assertTrue(result['DeclineMarkerAbsent'])
        self.assertEqual(result['DeclineMarkerAbsenceContract'], 'PASS')
        self.assertEqual(result['DeclineMarkerExactByteContract'], 'NOT_RUN')
        self.assertEqual(result['DeclineMarkerMismatchDiagnostics'], 'NOT_RUN')

    def test_decline_live_marker_presence_fails_absence_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'decline_probe.txt'
            marker.write_bytes(b'unexpected')
            result = probe._mode_result('decline')
            probe.apply_marker_contract(result, marker, probe.DECLINE_MARKER_BYTES, 'Decline')
        self.assertFalse(result['DeclineMarkerAbsent'])
        self.assertEqual(result['DeclineMarkerAbsenceContract'], 'FAIL')
        self.assertEqual(result['DeclineMarkerExactByteContract'], 'FAIL')
        self.assertEqual(result['DeclineMarkerMismatchDiagnostics'], 'PASS')

    def test_parent_detects_post_exit_lifecycle_warnings(self):
        evidence = probe.post_exit_warning_evidence(
            'Task was destroyed but it is pending!\n'
            'RuntimeWarning: coroutine DeviceFlowClient.close was never awaited\n'
            'ExpiringCache.__del__: Event loop is closed\n'
        )
        self.assertTrue(evidence['PostExitPendingTaskWarnings'])
        self.assertTrue(evidence['PostExitRuntimeWarnings'])
        self.assertTrue(evidence['PostExitEventLoopClosedErrors'])

    def test_parent_zero_warning_evidence_is_clean(self):
        evidence = probe.post_exit_warning_evidence('clean child output\n')
        self.assertEqual(evidence, {
            'PostExitRuntimeWarnings': [],
            'PostExitPendingTaskWarnings': [],
            'PostExitEventLoopClosedErrors': [],
            'PostExitDeviceFlowWarnings': [],
        })

    def test_contract_exposes_parent_child_teardown_fields(self):
        result = probe.contract_result('run', 'allow')
        for field in ('ApprovalLiveProbeArchitecture', 'TransportDisconnectCompleted', 'TransportThreadExited', 'PostExitRuntimeWarnings', 'PostExitPendingTaskWarnings', 'PostExitEventLoopClosedErrors', 'ApprovalLiveTeardownVerdict'):
            self.assertIn(field, result)
        self.assertEqual(result['ApprovalLiveProbeArchitecture'], 'PARENT_CHILD')

    def test_waiting_is_gated_by_successful_card_delivery(self):
        source = Path(probe.__file__).read_text(encoding='utf-8')
        self.assertIn("reply['state'] == 'sent'", source)
        self.assertLess(source.index("reply['state'] == 'sent'"), source.index('WAITING_FOR_FEISHU_APPROVAL'))

    @staticmethod
    def _clean_lifecycle(exit_code):
        return {
            'TransportLifecycleStarted': True,
            'ChildExitCode': exit_code,
            'TransportDisconnectCompleted': True,
            'TransportThreadExited': True,
            'TransportShutdownBlockingIssues': [],
            'SdkWsTasksRemaining': 0,
            'SdkCacheTasksRemaining': 0,
            'SdkDeviceFlowTasksRemaining': 0,
            'BgThreadExited': True,
            'BgLoopClosureState': 'CLOSED_BY_UPSTREAM_CLEANLY',
            'PostExitRuntimeWarnings': [],
            'PostExitPendingTaskWarnings': [],
            'PostExitEventLoopClosedErrors': [],
            'PostExitDeviceFlowWarnings': [],
            'AccessKeyLeak': False,
            'TicketLeak': False,
        }

    def test_business_failure_clean_teardown_is_independent(self):
        evidence = self._clean_lifecycle(2)
        teardown = probe.classify_approval_live_teardown(evidence)
        result = probe.finalize_approval_live_verdict('FAIL_CARD_SEND', teardown, ['FEISHU_APPROVAL_CARD_SEND_FAILED'])
        self.assertEqual(teardown, 'PASS')
        self.assertEqual(result['BusinessVerdict'], 'FAIL_CARD_SEND')
        self.assertEqual(result['ApprovalLiveTeardownVerdict'], 'PASS')
        self.assertEqual(result['FinalVerdict'], 'FAIL')
        self.assertNotIn('FEISHU_APPROVAL_LIVE_TEARDOWN_FAILED', result['BlockingIssues'])

    def test_business_failure_with_teardown_warning_fails_both_domains(self):
        evidence = self._clean_lifecycle(2)
        evidence['PostExitPendingTaskWarnings'] = ['Task was destroyed but it is pending']
        teardown = probe.classify_approval_live_teardown(evidence)
        result = probe.finalize_approval_live_verdict('FAIL_CARD_SEND', teardown, ['FEISHU_APPROVAL_CARD_SEND_FAILED'])
        self.assertEqual(teardown, 'FAIL')
        self.assertEqual(result['FinalVerdict'], 'FAIL')
        self.assertIn('FEISHU_APPROVAL_CARD_SEND_FAILED', result['BlockingIssues'])
        self.assertIn('FEISHU_APPROVAL_LIVE_TEARDOWN_FAILED', result['BlockingIssues'])

    def test_business_pass_cannot_mask_teardown_failure(self):
        evidence = self._clean_lifecycle(0)
        evidence['TransportThreadExited'] = False
        teardown = probe.classify_approval_live_teardown(evidence)
        result = probe.finalize_approval_live_verdict('PASS', teardown)
        self.assertEqual(teardown, 'FAIL')
        self.assertEqual(result['FinalVerdict'], 'FAIL')
        self.assertIn('FEISHU_APPROVAL_LIVE_TEARDOWN_FAILED', result['BlockingIssues'])

    def test_teardown_not_run_when_transport_never_started(self):
        evidence = {'TransportLifecycleStarted': False, 'ChildExitCode': 2}
        self.assertEqual(probe.classify_approval_live_teardown(evidence), 'NOT_RUN')
        result = probe.finalize_approval_live_verdict('FEISHU_LIVE_SETUP_REQUIRED', 'NOT_RUN', ['FEISHU_LIVE_SETUP_REQUIRED'])
        self.assertEqual(result['ApprovalLiveTeardownVerdict'], 'NOT_RUN')
        self.assertNotIn('FEISHU_APPROVAL_LIVE_TEARDOWN_FAILED', result['BlockingIssues'])

    def test_unexpected_child_crash_fails_closed(self):
        evidence = {'TransportLifecycleStarted': False, 'ChildExitCode': 1}
        self.assertEqual(probe.classify_approval_live_teardown(evidence), 'FAIL')
        result = probe.finalize_approval_live_verdict('CHILD_CRASH', 'FAIL')
        self.assertEqual(result['FinalVerdict'], 'FAIL')
        self.assertIn('FEISHU_APPROVAL_LIVE_TEARDOWN_FAILED', result['BlockingIssues'])

    def test_zero_request_id_normalization_contract(self):
        cases = (
            ({'requestId': 0}, '0'),
            ({'requestId': '0'}, '0'),
            ({'request_id': 0}, '0'),
            ({'id': 0}, '0'),
            ({}, ''),
            ({'requestId': None, 'request_id': None, 'id': None}, ''),
        )
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(probe._safe_notification({'method': 'serverRequest/resolved', 'params': params})['requestId'], expected)

    def test_zero_request_id_resolved_match_and_wrong_id(self):
        snapshot = {'resolved': ['0'], 'inflight': []}
        self.assertTrue(probe._resolution_evidence(snapshot, 0)['DurableResolvedRequestIdMatched'])
        self.assertFalse(probe._resolution_evidence(snapshot, 1)['DurableResolvedRequestIdMatched'])

    def test_structured_stdout_isolated_from_warning_scanner(self):
        structured = '{"RunId":"run","PostExitRuntimeWarnings":["field name only"],"FinalVerdict":"PASS"}'
        evidence = probe.post_exit_warning_evidence(structured, '')
        self.assertEqual(evidence['PostExitRuntimeWarnings'], [])
        self.assertEqual(evidence['PostExitPendingTaskWarnings'], [])

    def test_raw_stderr_remains_warning_authority(self):
        stderr = 'RuntimeWarning: coroutine x was never awaited\nTask was destroyed but it is pending!\n'
        evidence = probe.post_exit_warning_evidence('{"FinalVerdict":"PASS"}', stderr)
        self.assertTrue(evidence['PostExitRuntimeWarnings'])
        self.assertTrue(evidence['PostExitPendingTaskWarnings'])

    def test_structured_stdout_plus_real_stderr_detects_only_stderr(self):
        stdout = '{"PostExitRuntimeWarnings":["RuntimeWarning: fake field"]}\n'
        stderr = 'RuntimeWarning: real stderr warning\n'
        evidence = probe.post_exit_warning_evidence(stdout, stderr)
        self.assertEqual(evidence['PostExitRuntimeWarnings'], ['RuntimeWarning: real stderr warning'])

    def test_child_pre_exit_final_verdict_is_suppressed(self):
        source = Path(probe.__file__).read_text(encoding='utf-8')
        self.assertIn("result['FinalVerdict'] = 'PENDING_PARENT_EXIT'", source)
        self.assertIn("child['ParentFinalTeardownAuthority']", source)

    def test_expiring_cache_pending_detection_is_a_teardown_failure(self):
        evidence = self._clean_lifecycle(0)
        evidence['PostExitEventLoopClosedErrors'] = ['ExpiringCache.__del__: Event loop is closed']
        self.assertEqual(probe.classify_approval_live_teardown(evidence), 'FAIL')

    def test_clean_cache_teardown_contract_is_pass(self):
        evidence = self._clean_lifecycle(0)
        evidence.update({'SdkCacheTasksRemaining': 0, 'PostExitEventLoopClosedErrors': []})
        self.assertEqual(probe.classify_approval_live_teardown(evidence), 'PASS')

    def test_parent_final_teardown_authority_is_independent(self):
        clean = self._clean_lifecycle(0)
        final = probe.finalize_approval_live_verdict('PASS', probe.classify_approval_live_teardown(clean))
        self.assertEqual(final['FinalVerdict'], 'PASS')
        failed = probe.finalize_approval_live_verdict('PASS', 'FAIL')
        self.assertEqual(failed['FinalVerdict'], 'FAIL')

    def test_scanner_coverage_detects_real_stderr_with_structured_stdout(self):
        stdout = '{"ArtifactDir":"x","FinalVerdict":"PENDING_PARENT_EXIT"}\n'
        stderr = 'Exception ignored while calling deallocator ExpiringCache.__del__\n'
        evidence = probe.post_exit_warning_evidence(stdout, stderr)
        self.assertTrue(evidence['PostExitEventLoopClosedErrors'])

    def test_resolved_notification_exposes_raw_and_normalized_request_id(self):
        notification = probe._safe_notification({
            'method': 'serverRequest/resolved',
            'params': {'requestId': 0, 'threadId': 'thread-safe'},
        })
        evidence = probe.resolved_notification_diagnostics([notification], '0')
        self.assertEqual(evidence['ObservedResolvedRequestIdRaw'], 0)
        self.assertEqual(evidence['ObservedResolvedRequestIdRawType'], 'int')
        self.assertEqual(evidence['ObservedResolvedRequestIdNormalized'], '0')
        self.assertEqual(evidence['ObservedResolvedRequestIdSource'], 'params.requestId')
        self.assertEqual(evidence['ObservedResolvedRequestIdCount'], 1)
        self.assertTrue(evidence['LiveResolvedRequestIdMatched'])

    def test_resolved_request_id_multiple_events_target_matches(self):
        notifications = [
            probe._safe_notification({'method': 'serverRequest/resolved', 'params': {'requestId': 1}}),
            probe._safe_notification({'method': 'serverRequest/resolved', 'params': {'requestId': 0}}),
        ]
        evidence = probe.resolved_notification_diagnostics(notifications, '0')
        self.assertEqual(evidence['ObservedResolvedRequestIds'], ['1', '0'])
        self.assertEqual(evidence['ObservedResolvedRequestIdCount'], 2)
        self.assertTrue(evidence['LiveResolvedRequestIdMatched'])

    def test_resolved_request_id_wrong_and_missing_fail_closed(self):
        wrong = probe.resolved_notification_diagnostics([
            probe._safe_notification({'method': 'serverRequest/resolved', 'params': {'requestId': 1}}),
        ], '0')
        missing = probe.resolved_notification_diagnostics([], '0')
        self.assertFalse(wrong['LiveResolvedRequestIdMatched'])
        self.assertFalse(missing['LiveResolvedRequestIdMatched'])
        self.assertEqual(missing['ResolvedNotificationEvidenceContract'], 'FAIL')

    def _integrated_resolution(self, live_ids, resolved_ids, inflight=()):
        notifications = [
            probe._safe_notification({'method': 'serverRequest/resolved', 'params': {'requestId': request_id}})
            for request_id in live_ids
        ]
        return probe.apply_resolution_authority_evidence(
            {},
            notifications,
            {'resolved': list(resolved_ids), 'inflight': list(inflight)},
            '0',
        )

    def test_live_resolution_match_not_overwritten_by_snapshot_match(self):
        result = self._integrated_resolution(['1'], ['0'])
        self.assertFalse(result['LiveResolvedRequestIdMatched'])
        self.assertTrue(result['DurableResolvedRequestIdMatched'])
        self.assertEqual(result['ResolvedNotificationEvidenceContract'], 'FAIL')
        self.assertEqual(result['ResolutionSnapshotContract'], 'PASS')
        self.assertFalse(result['ResolvedRequestIdMatched'])
        self.assertEqual(result['ResolutionExactlyOnceAuthorityContract'], 'FAIL')

    def test_correct_live_id_wrong_snapshot_still_fails_exactly_once(self):
        result = self._integrated_resolution(['0'], ['1'])
        self.assertTrue(result['LiveResolvedRequestIdMatched'])
        self.assertFalse(result['DurableResolvedRequestIdMatched'])
        self.assertEqual(result['ResolvedNotificationEvidenceContract'], 'PASS')
        self.assertEqual(result['ResolutionSnapshotContract'], 'FAIL')
        self.assertFalse(result['ResolvedRequestIdMatched'])
        self.assertEqual(result['ResolutionExactlyOnceAuthorityContract'], 'FAIL')

    def test_correct_live_id_correct_snapshot_passes_resolution_contract(self):
        result = self._integrated_resolution(['0'], ['0'])
        self.assertTrue(result['LiveResolvedRequestIdMatched'])
        self.assertTrue(result['DurableResolvedRequestIdMatched'])
        self.assertEqual(result['ResolvedNotificationEvidenceContract'], 'PASS')
        self.assertEqual(result['ResolutionSnapshotContract'], 'PASS')
        self.assertTrue(result['ResolvedRequestIdMatched'])
        self.assertEqual(result['ResolutionExactlyOnceAuthorityContract'], 'PASS')

    def test_resolution_authorities_use_distinct_fields(self):
        live = probe.resolved_notification_diagnostics([], '0')
        durable = probe._resolution_evidence({'resolved': ['0'], 'inflight': []}, '0')
        self.assertIn('LiveResolvedRequestIdMatched', live)
        self.assertIn('DurableResolvedRequestIdMatched', durable)
        self.assertNotIn('ResolvedRequestIdMatched', live)
        self.assertNotIn('ResolvedRequestIdMatched', durable)

    def test_resolution_snapshot_contract_is_independent(self):
        result = self._integrated_resolution(['0'], ['1'])
        self.assertEqual(result['ResolvedNotificationEvidenceContract'], 'PASS')
        self.assertEqual(result['ResolutionSnapshotContract'], 'FAIL')
        self.assertEqual(result['ResolvedRequestIdMatchedAuthority'], 'AGGREGATE_LIVE_AND_DURABLE')

    def test_multiple_live_resolved_ids_target_present_matches_integrated(self):
        result = self._integrated_resolution(['1', '0'], ['0'])
        self.assertTrue(result['LiveResolvedRequestIdMatched'])
        self.assertTrue(result['DurableResolvedRequestIdMatched'])
        self.assertEqual(result['ResolutionExactlyOnceAuthorityContract'], 'PASS')

    def test_resolution_field_collision_regression(self):
        result = self._integrated_resolution(['1'], ['0'])
        self.assertFalse(result['ResolvedRequestIdMatched'])
        self.assertEqual(result['ResolvedRequestIdMatchedAuthority'], 'AGGREGATE_LIVE_AND_DURABLE')

    def test_exactly_once_requires_both_resolution_authorities(self):
        cases = (
            (['1'], ['1'], 'FAIL'),
            (['1'], ['0'], 'FAIL'),
            (['0'], ['1'], 'FAIL'),
            (['0'], ['0'], 'PASS'),
        )
        for live_ids, resolved_ids, expected in cases:
            with self.subTest(live_ids=live_ids, resolved_ids=resolved_ids):
                result = self._integrated_resolution(live_ids, resolved_ids)
                self.assertEqual(result['ResolutionExactlyOnceAuthorityContract'], expected)

    def test_marker_exact_bytes_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'marker'
            path.write_bytes(b'abc')
            evidence = probe.marker_byte_evidence(path, b'abc')
            self.assertTrue(evidence['AllowMarkerContentExact'])
            self.assertEqual(evidence['AllowMarkerExpectedByteLength'], 3)
            self.assertEqual(evidence['AllowMarkerActualSha256'], evidence['AllowMarkerExpectedSha256'])
            self.assertEqual(evidence['AllowMarkerExactByteContract'], 'PASS')

    def test_marker_trailing_lf_crlf_bom_and_space_fail(self):
        cases = (b'abc\n', b'abc\r\n', b'\xef\xbb\xbfabc', b'abc ')
        for actual in cases:
            with self.subTest(actual=actual), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'marker'
                path.write_bytes(actual)
                evidence = probe.marker_byte_evidence(path, b'abc')
                self.assertFalse(evidence['AllowMarkerContentExact'])
                self.assertEqual(evidence['AllowMarkerExactByteContract'], 'FAIL')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'marker'
            path.write_bytes(b'abc\n')
            evidence = probe.marker_byte_evidence(path, b'abc')
            self.assertTrue(evidence['AllowMarkerTrailingLfPresent'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'marker'
            path.write_bytes(b'abc\r\n')
            evidence = probe.marker_byte_evidence(path, b'abc')
            self.assertTrue(evidence['AllowMarkerTrailingCrLfPresent'])

    def test_marker_diagnostics_are_safe_and_missing_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing'
            evidence = probe.marker_byte_evidence(path, b'abc')
            self.assertFalse(evidence['AllowMarkerContentExact'])
            self.assertIsNone(evidence['AllowMarkerActualByteLength'])
            self.assertEqual(evidence['AllowMarkerMismatchDiagnostics'], 'FAIL')

    def test_protected_operation_writes_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'allow_probe.txt'
            path.write_bytes(probe.ALLOW_MARKER_BYTES)
            evidence = probe.marker_byte_evidence(path, probe.ALLOW_MARKER_BYTES)
            self.assertEqual(path.read_bytes(), probe.ALLOW_MARKER_BYTES)
            self.assertEqual(evidence['AllowMarkerExactByteContract'], 'PASS')

    @staticmethod
    def _cache_transport(task, loop, drained=1):
        capture = SimpleNamespace(
            cache_cron_captured=True,
            cache_cron_task=task,
            cache_loop=loop,
            cache_loop_running_before_shutdown=False,
            cache_loop_closed_before_shutdown=False,
            cache_loop_same_as_ws_loop=False,
            cache_cron_task_done_before_shutdown=False,
            bg_loop=None,
        )
        diagnostic = SimpleNamespace(sdk_cache_tasks_drained=drained)
        return SimpleNamespace(_shutdown_capture=capture, shutdown_diagnostic=diagnostic, _loop=None)

    async def _pending_cache_task(self):
        await asyncio.Event().wait()

    def test_cache_cron_cancel_requested_not_terminal_fails(self):
        loop = asyncio.new_event_loop()
        task = loop.create_task(self._pending_cache_task())
        loop.run_until_complete(asyncio.sleep(0))
        task.cancel()
        evidence = probe.cache_cron_lifecycle_evidence(self._cache_transport(task, loop))
        self.assertEqual(evidence['CacheCronTerminalEvidence'], 'FAIL')
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        loop.close()

    def test_cache_cron_cancelled_terminal_passes(self):
        loop = asyncio.new_event_loop()
        task = loop.create_task(self._pending_cache_task())
        loop.run_until_complete(asyncio.sleep(0))
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        evidence = probe.cache_cron_lifecycle_evidence(self._cache_transport(task, loop))
        self.assertTrue(evidence['CacheCronAwaitedToTerminal'])
        self.assertEqual(evidence['CacheCronTerminalEvidence'], 'PASS')
        loop.close()

    def test_cache_cron_wrong_owner_loop_pending_fails(self):
        owner_loop = asyncio.new_event_loop()
        other_loop = asyncio.new_event_loop()
        task = owner_loop.create_task(self._pending_cache_task())
        owner_loop.run_until_complete(asyncio.sleep(0))
        evidence = probe.cache_cron_lifecycle_evidence(self._cache_transport(task, owner_loop))
        self.assertFalse(evidence['CacheCronDoneBeforeChildReturn'])
        self.assertEqual(evidence['CacheCronTerminalEvidence'], 'FAIL')
        task.cancel()
        owner_loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        owner_loop.close()
        other_loop.close()

    def test_cache_cron_owner_loop_closed_pending_fails(self):
        loop = asyncio.new_event_loop()
        task = SimpleNamespace(done=lambda: False, cancelled=lambda: False)
        loop.close()
        evidence = probe.cache_cron_lifecycle_evidence(self._cache_transport(task, loop))
        self.assertEqual(evidence['CacheCronTerminalEvidence'], 'FAIL')

    def test_cache_cron_terminal_before_child_return_passes(self):
        loop = asyncio.new_event_loop()
        async def complete():
            return None
        task = loop.create_task(complete())
        loop.run_until_complete(task)
        evidence = probe.cache_cron_lifecycle_evidence(self._cache_transport(task, loop))
        self.assertTrue(evidence['CacheCronDoneBeforeChildReturn'])
        self.assertEqual(evidence['CacheCronTerminalEvidence'], 'PASS')
        loop.close()

    def test_cache_terminal_then_loop_close_passes(self):
        loop = asyncio.new_event_loop()
        async def complete():
            return None
        task = loop.create_task(complete())
        loop.run_until_complete(task)
        loop.close()
        evidence = probe.cache_cron_lifecycle_evidence(self._cache_transport(task, loop))
        self.assertEqual(evidence['CacheCronTerminalEvidence'], 'PASS')
        self.assertEqual(evidence['CacheOwnerLoopClosureState'], 'CLOSED_CLEANLY')

    def test_cache_host_114236_shape_regression(self):
        baseline = Path('.tmp/m2b-approval-live/20260819T114236Z-90309a5c/result.json')
        if not baseline.exists():
            self.skipTest('authoritative Host artifact is not present')
        payload = json.loads(baseline.read_text(encoding='utf-8'))
        self.assertTrue(payload['BusinessVerdict'])
        self.assertTrue(payload['CacheCronCaptured'])
        self.assertFalse(payload['CacheCronOwnerLoopRunningAtCapture'])
        self.assertFalse(payload['CacheCronOwnerLoopClosedAtCapture'])
        self.assertFalse(payload['CacheCronDoneBeforeChildReturn'])
        self.assertEqual(payload['CacheCronTerminalEvidence'], 'FAIL')

    def test_parent_post_exit_cache_warning_authority_remains_fail_closed(self):
        clean = self._clean_lifecycle(0)
        clean['PostExitPendingTaskWarnings'] = ['Task was destroyed but it is pending!']
        self.assertEqual(probe.classify_approval_live_teardown(clean), 'FAIL')
        final = probe.finalize_approval_live_verdict('PASS', 'FAIL')
        self.assertEqual(final['FinalVerdict'], 'FAIL')

    def test_approval_business_path_unchanged_after_cache_fix(self):
        result = self._integrated_resolution(['0'], ['0'])
        self.assertTrue(result['LiveResolvedRequestIdMatched'])
        self.assertTrue(result['DurableResolvedRequestIdMatched'])
        self.assertEqual(result['ResolvedNotificationEvidenceContract'], 'PASS')
        self.assertEqual(result['ResolutionSnapshotContract'], 'PASS')

    def test_current_host_failure_shape_regression(self):
        baseline = Path('.tmp/m2b-approval-live/20260819T105322Z-92393ad8/result.json')
        if not baseline.exists():
            self.skipTest('authoritative Host artifact is not present')
        payload = json.loads(baseline.read_text(encoding='utf-8'))
        self.assertTrue(payload['ServerRequestResolvedObserved'])
        self.assertFalse(payload['ResolvedRequestIdMatched'])
        self.assertFalse(payload['AllowMarkerContentExact'])
        self.assertEqual(payload['ApprovalLiveTeardownVerdict'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
