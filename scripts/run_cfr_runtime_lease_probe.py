import json
import multiprocessing
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from cfr.codex.runtime_lease import CfrThreadRuntimeLeaseManager
from cfr.core.models import StructuredError


def now():
    return datetime.now(timezone.utc).isoformat()


def worker_hold(database, thread_id, ttl, ready, release, output):
    manager = CfrThreadRuntimeLeaseManager(database, ttl=ttl, heartbeat_interval=max(ttl / 4, 0.05))
    try:
        lease = manager.acquire(thread_id)
        output.put({'status': 'PASS', 'generation': lease.generation})
        ready.set()
        release.wait(10)
        output.put({'released': manager.release(lease)})
    except Exception as exc:
        output.put({'status': 'FAIL', 'error': str(exc)})


def worker_try(database, thread_id, ttl, output):
    manager = CfrThreadRuntimeLeaseManager(database, ttl=ttl)
    try:
        lease = manager.acquire(thread_id)
        output.put({'status': 'PASS', 'generation': lease.generation, 'released': manager.release(lease)})
    except StructuredError as exc:
        output.put({'status': 'ERROR', 'code': exc.code, 'message': exc.message})
    except Exception as exc:
        output.put({'status': 'FAIL', 'error': str(exc)})


def worker_acquire_and_exit_without_release(database, thread_id, ttl, output):
    """Simulate a crashed worker by exiting after acquire without release/heartbeat."""
    manager = CfrThreadRuntimeLeaseManager(database, ttl=ttl, heartbeat_interval=max(ttl / 4, 0.05))
    try:
        lease = manager.acquire(thread_id)
        output.put({
            'status': 'PASS',
            'generation': lease.generation,
            'lease_id': lease.lease_id,
            'expires_at': lease.expires_at,
        })
        return
    except Exception as exc:
        output.put({'status': 'FAIL', 'error': str(exc)})


def run_probe(root):
    context = multiprocessing.get_context('spawn')
    results = {}

    case_a_db = root / 'case-a.sqlite3'
    ready = context.Event()
    release = context.Event()
    queue_a = context.Queue()
    queue_b = context.Queue()
    process_a = context.Process(target=worker_hold, args=(case_a_db, 'thread-x', 2, ready, release, queue_a))
    process_a.start()
    ready_ok = ready.wait(10)
    process_b = context.Process(target=worker_try, args=(case_a_db, 'thread-x', 2, queue_b))
    process_b.start()
    process_b.join(10)
    conflict = queue_b.get(timeout=5)
    release.set()
    process_a.join(10)
    handoff_queue = context.Queue()
    process_b2 = context.Process(target=worker_try, args=(case_a_db, 'thread-x', 2, handoff_queue))
    process_b2.start()
    process_b2.join(10)
    handoff = handoff_queue.get(timeout=5)
    results['ConcurrentAcquireBlocked'] = 'PASS' if ready_ok and conflict.get('code') == 'CFR_RUNTIME_WRITER_ACTIVE' else 'FAIL'
    results['ReleaseHandoff'] = 'PASS' if handoff.get('status') == 'PASS' else 'FAIL'

    case_b_db = root / 'case-b.sqlite3'
    queue_b = context.Queue()
    crash_ttl = 0.5
    process_crash = context.Process(target=worker_acquire_and_exit_without_release, args=(case_b_db, 'thread-x', crash_ttl, queue_b))
    process_crash.start()
    process_crash.join(10)
    first = queue_b.get(timeout=5)
    manager_b = CfrThreadRuntimeLeaseManager(case_b_db, ttl=2)
    try:
        manager_b.acquire('thread-x')
        pre_expiry_blocked = False
    except StructuredError as exc:
        pre_expiry_blocked = exc.code == 'CFR_RUNTIME_WRITER_ACTIVE'
    time.sleep(crash_ttl + 0.3)
    recovered = manager_b.acquire('thread-x')
    results['CrashLeaseLeftBehind'] = 'PASS' if first.get('status') == 'PASS' and first.get('lease_id') and first.get('expires_at') else 'FAIL'
    results['PreExpiryAcquireBlocked'] = 'PASS' if pre_expiry_blocked else 'FAIL'
    results['CrashExpiryRecovery'] = 'PASS' if first.get('status') == 'PASS' and recovered.generation == first.get('generation', 0) + 1 else 'FAIL'
    manager_b.release(recovered)

    case_c_db = root / 'case-c.sqlite3'
    manager_a = CfrThreadRuntimeLeaseManager(case_c_db, instance_id='A', ttl=0.1, id_factory=lambda: 'lease-a')
    lease_a = manager_a.acquire('thread-x')
    time.sleep(0.15)
    manager_b = CfrThreadRuntimeLeaseManager(case_c_db, instance_id='B', ttl=2, id_factory=lambda: 'lease-b')
    lease_b = manager_b.acquire('thread-x')
    stale_release = manager_a.release(lease_a)
    remaining = manager_b.inspect()
    results['StaleReleaseFenced'] = 'PASS' if not stale_release and remaining and remaining[0]['lease_id'] == lease_b.lease_id else 'FAIL'
    manager_b.release(lease_b)

    case_d_db = root / 'case-d.sqlite3'
    manager_h = CfrThreadRuntimeLeaseManager(case_d_db, ttl=0.25, heartbeat_interval=0.05)
    lease_h = manager_h.acquire('thread-x')
    manager_h.start_heartbeat(lease_h, interval=0.05)
    time.sleep(0.6)
    manager_other = CfrThreadRuntimeLeaseManager(case_d_db, ttl=0.25)
    try:
        manager_other.acquire('thread-x')
        blocked = False
    except StructuredError as exc:
        blocked = exc.code == 'CFR_RUNTIME_WRITER_ACTIVE'
    results['HeartbeatPreventsExpiry'] = 'PASS' if blocked and not lease_h.lease_lost else 'FAIL'
    manager_h.release(lease_h)
    return results


def main():
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    artifact_dir = ROOT / '.tmp' / 'runtime-lease' / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    results = run_probe(artifact_dir)
    verdict = 'PASS' if all(value == 'PASS' for value in results.values()) else 'FAIL'
    payload = {'RunId': run_id, 'Verdict': verdict, 'started_at': now(), **results}
    (artifact_dir / 'result.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    (artifact_dir / 'runtime_lease.log').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    print(json.dumps({'RunId': run_id, 'ArtifactDir': str(artifact_dir), **payload}))
    return 0 if verdict == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
