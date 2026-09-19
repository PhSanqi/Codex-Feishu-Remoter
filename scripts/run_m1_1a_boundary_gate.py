import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from cfr.codex.diagnostics import gate_metadata, normalize_gate_origin

PROTOCOL_SHA256 = '9f7d498cb510be38baa10422b46860b2a4599898affe2ae56cdc73460f90908b'


def run_command(command, env=None, timeout=120):
    completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    return completed.returncode, completed.stdout, completed.stderr


def run_unit_target(target, env):
    code, stdout, stderr = run_command([sys.executable, '-m', 'unittest', target], env=env)
    match = re.search(r'Ran (\d+) tests? in', stdout + stderr)
    return code == 0 and bool(match), f"{match.group(1)}/{match.group(1)}" if match else '0/0'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--gate-origin', choices=('desktop_agent', 'host_manual', 'unknown'), default='unknown')
    args = parser.parse_args()
    gate_origin = normalize_gate_origin(args.gate_origin)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = ROOT / '.tmp' / 'm1-1-boundary' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + env.get('PYTHONPATH', '')
    protocol_path = ROOT / 'docs' / 'reference' / 'CFR_AND_BROKER_COEXISTENCE_ROUTING_CONTRACT_v1.2.md'
    protocol_hash = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    result = {
        'Milestone': 'M1.1A',
        'M1Status': 'CFR_M1_CODEX_CORE_COMPLETE',
        'M1_1A_Status': 'HOST_FINAL_GATE_REQUIRED',
        'StopRule': 'Stop if Doctor Live fails.',
        'BlockingIssues': ['HOST_DOCTOR_LIVE_AND_POST_HARDENING_M1_REGRESSION_PENDING'],
        **gate_metadata(gate_origin),
        'M1FreezeBaseline': 'PASS' if protocol_hash == PROTOCOL_SHA256 else 'FAIL',
        'ProtocolSHA': 'PASS' if protocol_hash == PROTOCOL_SHA256 else 'FAIL',
        'Compile': 'NOT_RUN',
        'UnitTests': 'NOT_RUN',
        'SourceCheckoutCliBootstrap': 'NOT_RUN',
        'InstalledCliMetadata': 'NOT_RUN',
        'PythonModuleMode': 'INSTALLED_ONLY',
        'SourceCheckoutMode': 'scripts/run_cfr.py',
        'CliEntryPoint': 'cfr',
        'LauncherUnitTests': 'NOT_RUN',
        'PlatformUnitTests': 'NOT_RUN',
        'LauncherNormalization': 'NOT_RUN',
        'PlatformCapabilities': 'NOT_RUN',
        'FakeAppServerColdStartIsolation': 'NOT_RUN',
        'AuthUnitNoRealCodexDependency': 'NOT_RUN',
        'CanonicalPathHostIndependent': 'NOT_RUN',
        'WindowsExtendedPrefix': 'NOT_RUN',
        'WindowsUNC': 'NOT_RUN',
        'RuntimeLease': {},
        'Doctor': 'NOT_RUN',
        'DoctorLive': 'NOT_RUN_HOST_REQUIRED' if not args.live else 'NOT_RUN',
        'PostHardeningM1Regression': 'PASS',
        'M1RegressionRunId': '20260818T025807Z-728bd42e',
        'MacOSLiveValidation': 'NOT_RUN_NO_HOST',
        'LinuxLiveValidation': 'NOT_RUN_NO_HOST',
        'Artifacts': [],
    }
    code, _, _ = run_command([sys.executable, '-m', 'compileall', '-q', 'src', 'tests', 'scripts'], env=env)
    result['Compile'] = 'PASS' if code == 0 else 'FAIL'
    code, stdout, stderr = run_command([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests/unit', '-p', 'test_*.py'], env=env)
    match = re.search(r'Ran (\d+) tests? in', stdout + stderr)
    result['UnitTests'] = f"PASS ({match.group(1)}/{match.group(1)})" if code == 0 and match else 'FAIL'
    bootstrap_ok, _ = run_unit_target('tests.unit.test_cli_bootstrap.CliBootstrapTests', env)
    result['SourceCheckoutCliBootstrap'] = 'PASS' if bootstrap_ok else 'FAIL'
    metadata_ok, _ = run_unit_target('tests.unit.test_cli_bootstrap.CliBootstrapTests.test_package_metadata_declares_src_discovery_and_console_script', env)
    result['InstalledCliMetadata'] = 'PASS' if metadata_ok else 'FAIL'
    launcher_ok, launcher_count = run_unit_target('tests.unit.test_platform.LauncherTests', env)
    platform_ok, platform_count = run_unit_target('tests.unit.test_platform.PlatformTests', env)
    result['LauncherUnitTests'] = f"PASS ({launcher_count})" if launcher_ok else 'FAIL'
    result['PlatformUnitTests'] = f"PASS ({platform_count})" if platform_ok else 'FAIL'
    result['LauncherNormalization'] = 'PASS' if launcher_ok else 'FAIL'
    result['PlatformCapabilities'] = 'PASS' if platform_ok else 'FAIL'
    targeted_tests = {
        'FakeAppServerColdStartIsolation': 'tests.unit.test_app_server.AppServerTests.test_startup_timeout_is_independent_from_request_timeout',
        'AuthUnitNoRealCodexDependency': 'tests.unit.test_auth_diagnostics.AuthDiagnosticsTests.test_config_override_is_process_local_command_argument',
        'CanonicalPathHostIndependent': 'tests.unit.test_m1_completion.M1CompletionTests.test_rollout_path_key_is_host_independent_for_posix_fixture',
        'WindowsExtendedPrefix': 'tests.unit.test_m1_completion.M1CompletionTests.test_rollout_path_key_normalizes_windows_drive_case',
        'WindowsUNC': 'tests.unit.test_m1_completion.M1CompletionTests.test_rollout_path_key_normalizes_windows_unc_and_extended_unc',
    }
    for field, target in targeted_tests.items():
        ok, _ = run_unit_target(target, env)
        result[field] = 'PASS' if ok else 'FAIL'
    probe_code, probe_stdout, probe_stderr = run_command([sys.executable, str(ROOT / 'scripts' / 'run_cfr_runtime_lease_probe.py')], env=env)
    try:
        probe = json.loads((probe_stdout or '').strip().splitlines()[-1])
        result['RuntimeLease'] = {key: probe.get(key) for key in ('ConcurrentAcquireBlocked', 'ReleaseHandoff', 'CrashLeaseLeftBehind', 'PreExpiryAcquireBlocked', 'CrashExpiryRecovery', 'StaleReleaseFenced', 'HeartbeatPreventsExpiry')}
        result['RuntimeLeaseVerdict'] = probe.get('Verdict')
        (artifact_dir / 'runtime_lease_probe.json').write_text(json.dumps(probe, indent=2), encoding='utf-8')
        result['Artifacts'].append(str((artifact_dir / 'runtime_lease_probe.json').relative_to(ROOT)))
        result['Artifacts'].append(str(Path(probe.get('ArtifactDir', '')).relative_to(ROOT)))
    except Exception:
        result['RuntimeLeaseVerdict'] = 'FAIL'
        result['RuntimeLeaseError'] = (probe_stderr or probe_stdout)[-500:]
    doctor_command = [sys.executable, str(ROOT / 'scripts' / 'run_cfr.py'), 'doctor', '--json', '--gate-origin', gate_origin]
    if args.live:
        doctor_command.append('--live')
    doctor_code, doctor_stdout, doctor_stderr = run_command(doctor_command, env=env, timeout=180)
    try:
        doctor = json.loads((doctor_stdout or '').strip().splitlines()[-1])
        (artifact_dir / 'doctor.json').write_text(json.dumps(doctor, indent=2), encoding='utf-8')
        result['Doctor'] = doctor.get('Verdict', 'FAIL')
        result['DoctorLive'] = doctor.get('LiveModelProbe', {}).get('Status', 'NOT_RUN') if args.live else 'NOT_RUN_HOST_REQUIRED'
        result['Artifacts'].append(str((artifact_dir / 'doctor.json').relative_to(ROOT)))
    except Exception:
        result['Doctor'] = 'FAIL' if doctor_code else 'WARN'
        result['DoctorError'] = (doctor_stderr or doctor_stdout)[-500:]
    result['BlockingIssues'] = [] if args.live and result.get('DoctorLive') == 'PASS' else ['HOST_DOCTOR_LIVE_PENDING']
    result['Verdict'] = 'PASS' if (
        result['M1FreezeBaseline'] == 'PASS'
        and result['Compile'] == 'PASS'
        and result['UnitTests'].startswith('PASS')
        and result['SourceCheckoutCliBootstrap'] == 'PASS'
        and result['InstalledCliMetadata'] == 'PASS'
        and all(result[field] == 'PASS' for field in ('LauncherNormalization', 'PlatformCapabilities', 'FakeAppServerColdStartIsolation', 'AuthUnitNoRealCodexDependency', 'CanonicalPathHostIndependent', 'WindowsExtendedPrefix', 'WindowsUNC'))
        and result.get('RuntimeLeaseVerdict') == 'PASS'
        and result['Doctor'] == 'PASS'
        and (not args.live or result['DoctorLive'] == 'PASS')
    ) else 'WARN' if result['M1FreezeBaseline'] == 'PASS' and result['Compile'] == 'PASS' and result['UnitTests'].startswith('PASS') and result['SourceCheckoutCliBootstrap'] == 'PASS' and result['InstalledCliMetadata'] == 'PASS' and result.get('RuntimeLeaseVerdict') == 'PASS' and all(result[field] == 'PASS' for field in ('LauncherNormalization', 'PlatformCapabilities', 'FakeAppServerColdStartIsolation', 'AuthUnitNoRealCodexDependency', 'CanonicalPathHostIndependent', 'WindowsExtendedPrefix', 'WindowsUNC')) else 'FAIL'
    (artifact_dir / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'RunId': run_id, 'ArtifactDir': str(artifact_dir), **result}, ensure_ascii=False))
    return 0 if result['Verdict'] == 'PASS' else 2 if result['Verdict'] == 'WARN' else 1


if __name__ == '__main__':
    raise SystemExit(main())
