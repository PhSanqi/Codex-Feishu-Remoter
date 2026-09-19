import ast
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import importlib.util
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
PROBE_PATH = ROOT / 'scripts' / 'run_m2_feishu_shutdown_probe.py'
SPEC = importlib.util.spec_from_file_location('cfr_shutdown_probe', PROBE_PATH)
PROBE = importlib.util.module_from_spec(SPEC)
sys.modules['cfr_shutdown_probe'] = PROBE
SPEC.loader.exec_module(PROBE)


class ShutdownProbeEvidenceTests(unittest.TestCase):
    @staticmethod
    def _final_clean_child(run_id):
        child = PROBE.build_contract(run_id)
        child.update({
            'ChannelReady': 'PASS',
            'DisconnectCompleted': 'PASS',
            'TransportThreadExited': 'PASS',
            'PreShutdownCapture': 'PASS',
            'StartFutureCaptured': True,
            'StartFutureExited': True,
            'StartWorkerExited': True,
            'StartWorkerTerminalEvidence': 'START_FUTURE_NATURAL_COMPLETION',
            'StartWorkerTerminalTimedOut': False,
            'BgOwnershipHandoffAttempted': True,
            'BgOwnershipHandoffCompleted': True,
            'BgOwnershipDetachedFromSdk': True,
            'BgSchedulingBlockedBeforeDetach': True,
            'BgProducerQuiescenceContract': 'PASS',
            'StartWorkerTerminalBeforeBgDrain': True,
            'BgFinalDrainTerminalEvidence': 'PASS',
            'BgFinalDrainTasksRemaining': [],
            'BgCapturedLoopStopAllowed': True,
            'Host072914LateSleepRaceRegression': 'PASS',
            'WsClientCaptured': True,
            'WsLoopCaptured': True,
            'CacheTaskObservationAvailable': True,
            'WsTaskObservationAvailable': True,
            'BgLoopCaptured': True,
            'BgThreadCaptured': True,
            'BgThreadExited': True,
            'BgLoopClosedAfterPublicStop': True,
            'BgLoopClosureState': 'CLOSED_BY_UPSTREAM_CLEANLY',
            'BgTaskObservationStatus': 'CLOSED_CLEANLY_BY_UPSTREAM',
            'BgTaskObservationAvailable': False,
            'BgTasksRemaining': None,
            'BgAsyncGeneratorsShutdown': 'NOT_OBSERVABLE',
            'BgLoopClosedByCfr': False,
            'DeviceFlowCaptured': False,
            'DeviceFlowCloseCompleted': 'NOT_REQUIRED',
            'SdkWsTasksRemaining': 0,
            'SdkCacheTasksRemaining': 0,
            'SdkDeviceFlowTasksRemaining': 0,
            'RemainingTaskNames': [],
            'AsyncGeneratorsShutdown': 'PASS',
            'BlockingIssues': ['SDK_TASK_DRAIN_ERROR:RuntimeError'],
        })
        return child

    def test_shutdown_child_uses_real_diagnostic_not_constant_pass(self):
        source = PROBE_PATH.read_text(encoding='utf-8')
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = {item.id for item in node.targets if isinstance(item, ast.Name)}
                if targets & {'SdkWsTasksDrained', 'SdkCacheTasksDrained'}:
                    self.assertFalse(isinstance(node.value, ast.Constant) and node.value.value == 'PASS')
        self.assertIn('transport.shutdown_diagnostic', source)

    def test_shutdown_parent_fails_when_child_claims_clean_but_post_exit_warning_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            child = PROBE.build_contract('run-test')
            child.update({
                'ChannelReady': 'PASS',
                'DisconnectCompleted': 'PASS',
                'TransportThreadExited': 'PASS',
                'DeviceFlowCloseCompleted': 'NOT_OBSERVABLE',
                'SdkWsTasksBefore': 0,
                'SdkWsTasksDrained': 0,
                'SdkWsTasksRemaining': 0,
                'SdkCacheTasksBefore': 0,
                'SdkCacheTasksDrained': 0,
                'SdkCacheTasksRemaining': 0,
                'SdkDeviceFlowTasksBefore': 0,
                'SdkDeviceFlowTasksDrained': 0,
                'SdkDeviceFlowTasksRemaining': 0,
                'AsyncGeneratorsShutdown': 'PASS',
                'BlockingIssues': [],
            })
            (artifact_dir / 'child_result.json').write_text(json.dumps(child), encoding='utf-8')
            completed = subprocess.CompletedProcess([], 0, stdout='child exited\n', stderr='Task was destroyed but it is pending\n')
            with patch('cfr_shutdown_probe.subprocess.run', return_value=completed):
                result = PROBE._parent_run('run-test', artifact_dir, 1)
            self.assertEqual(result['Verdict'], 'FAIL')
            self.assertTrue(result['PendingTaskWarnings'])
            self.assertIn('POST_EXIT_PENDING_TASK_WARNING', result['BlockingIssues'])

    def test_shutdown_parent_accepts_clean_upstream_bg_loop_close(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            child = PROBE.build_contract('run-upstream-clean')
            child.update({
                'ChannelReady': 'PASS',
                'DisconnectCompleted': 'PASS',
                'TransportThreadExited': 'PASS',
                'PreShutdownCapture': 'PASS',
                'StartFutureCaptured': True,
                'StartFutureExited': True,
                'WsClientCaptured': True,
                'WsLoopCaptured': True,
                'CacheTaskObservationAvailable': True,
                'WsTaskObservationAvailable': True,
                'BgLoopCaptured': True,
                'BgThreadCaptured': True,
                'BgThreadExited': True,
                'BgLoopClosedAfterPublicStop': True,
                'BgLoopClosureState': 'CLOSED_BY_UPSTREAM_CLEANLY',
                'BgTaskObservationStatus': 'CLOSED_CLEANLY_BY_UPSTREAM',
                'BgTaskObservationAvailable': False,
                'BgTasksRemaining': None,
                'BgAsyncGeneratorsShutdown': 'NOT_OBSERVABLE',
                'BgLoopClosedByCfr': False,
                'DeviceFlowCloseCompleted': 'NOT_OBSERVABLE',
                'SdkWsTasksRemaining': 0,
                'SdkCacheTasksRemaining': 0,
                'SdkDeviceFlowTasksRemaining': 0,
                'AsyncGeneratorsShutdown': 'PASS',
                'BlockingIssues': [],
            })
            (artifact_dir / 'child_result.json').write_text(json.dumps(child), encoding='utf-8')
            completed = subprocess.CompletedProcess([], 0, stdout='child exited\n', stderr='')
            with patch('cfr_shutdown_probe.subprocess.run', return_value=completed):
                result = PROBE._parent_run('run-upstream-clean', artifact_dir, 1)
            self.assertEqual(result['Verdict'], 'PASS')
            self.assertEqual(result['BgTaskTerminalEvidence'], 'POST_EXIT_PROCESS_CLEAN')
            self.assertEqual(result['BgLoopClosureState'], 'CLOSED_BY_UPSTREAM_CLEANLY')

    def test_shutdown_parent_reclassifies_upstream_close_when_warning_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            child = PROBE.build_contract('run-upstream-warning')
            child.update({
                'ChannelReady': 'PASS',
                'DisconnectCompleted': 'PASS',
                'TransportThreadExited': 'PASS',
                'PreShutdownCapture': 'PASS',
                'StartFutureExited': True,
                'WsClientCaptured': True,
                'WsLoopCaptured': True,
                'CacheTaskObservationAvailable': True,
                'WsTaskObservationAvailable': True,
                'BgLoopCaptured': True,
                'BgThreadCaptured': True,
                'BgThreadExited': True,
                'BgLoopClosedAfterPublicStop': True,
                'BgLoopClosureState': 'CLOSED_BY_UPSTREAM_CLEANLY',
                'BgTaskObservationStatus': 'CLOSED_CLEANLY_BY_UPSTREAM',
                'BgTaskObservationAvailable': False,
                'BgTasksRemaining': None,
                'BgAsyncGeneratorsShutdown': 'NOT_OBSERVABLE',
                'DeviceFlowCloseCompleted': 'NOT_OBSERVABLE',
                'SdkWsTasksRemaining': 0,
                'SdkCacheTasksRemaining': 0,
                'SdkDeviceFlowTasksRemaining': 0,
                'AsyncGeneratorsShutdown': 'PASS',
                'BlockingIssues': [],
            })
            (artifact_dir / 'child_result.json').write_text(json.dumps(child), encoding='utf-8')
            completed = subprocess.CompletedProcess([], 0, stdout='child exited\n', stderr='FeishuChannel.stop: device_flow.close timed out\nTask was destroyed but it is pending! coro=<DeviceFlowClient.close()>\nRuntimeWarning: coroutine sleep was never awaited\n')
            with patch('cfr_shutdown_probe.subprocess.run', return_value=completed):
                result = PROBE._parent_run('run-upstream-warning', artifact_dir, 1)
            self.assertEqual(result['Verdict'], 'FAIL')
            self.assertEqual(result['BgLoopClosureState'], 'CLOSED_UNCLEANLY')
            self.assertIn('CHANNEL_BG_LOOP_CLOSED_UNCLEANLY', result['BlockingIssues'])
            self.assertTrue(result['DeviceFlowPublicStopTimeoutWarningDetected'])
            self.assertTrue(result['DeviceFlowPostExitPendingWarningDetected'])

    def test_final_clean_authority_reclassifies_transient_sdk_drain_error(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            child = self._final_clean_child('run-transient-sdk-drain')
            (artifact_dir / 'child_result.json').write_text(json.dumps(child), encoding='utf-8')
            completed = subprocess.CompletedProcess([], 0, stdout='child exited\n', stderr='')
            with patch('cfr_shutdown_probe.subprocess.run', return_value=completed):
                result = PROBE._parent_run('run-transient-sdk-drain', artifact_dir, 1)
            self.assertEqual(result['Verdict'], 'PASS')
            self.assertEqual(result['BlockingIssues'], [])
            self.assertEqual(result['NonBlockingDiagnostics'], ['SDK_TASK_DRAIN_TRANSIENT_RUNTIME_ERROR'])

    def test_final_clean_authority_never_downgrades_with_unclean_evidence(self):
        cases = (
            ('pending-warning', {}, 'Task was destroyed but it is pending\n'),
            ('remaining-task', {'RemainingTaskNames': ['lark_channel.pending']}, ''),
            ('unclean-loop', {'BgLoopClosureState': 'CLOSED_UNCLEANLY'}, ''),
        )
        for suffix, update, stderr in cases:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                artifact_dir = Path(directory)
                child = self._final_clean_child(f'run-unclean-{suffix}')
                child.update(update)
                (artifact_dir / 'child_result.json').write_text(json.dumps(child), encoding='utf-8')
                completed = subprocess.CompletedProcess([], 0, stdout='child exited\n', stderr=stderr)
                with patch('cfr_shutdown_probe.subprocess.run', return_value=completed):
                    result = PROBE._parent_run(f'run-unclean-{suffix}', artifact_dir, 1)
                self.assertEqual(result['Verdict'], 'FAIL')
                self.assertIn('SDK_TASK_DRAIN_ERROR:RuntimeError', result['BlockingIssues'])
                self.assertEqual(result['NonBlockingDiagnostics'], [])


if __name__ == '__main__':
    unittest.main()
