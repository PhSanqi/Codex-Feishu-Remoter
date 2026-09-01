from enum import StrEnum
import threading

from cfr.core.models import StructuredError


class LeaseState(StrEnum):
    IDLE = 'idle'
    CFR_ACTIVE = 'cfr_active'
    EXTERNAL_ACTIVE = 'external_active'
    UNKNOWN = 'unknown'


class WriterLeaseManager:
    """Fast in-process CFR writer state; not a cross-system workspace lease."""
    def __init__(self):
        self._states: dict[str, LeaseState] = {}
        self._lock = threading.RLock()

    def state_for(self, thread_id: str) -> LeaseState:
        with self._lock:
            return self._states.get(thread_id, LeaseState.IDLE)

    def acquire(self, thread_id: str) -> None:
        with self._lock:
            state = self._states.get(thread_id, LeaseState.IDLE)
            if state is not LeaseState.IDLE:
                raise StructuredError('writer_active', f'{thread_id} is {state.value}')
            self._states[thread_id] = LeaseState.CFR_ACTIVE

    def mark_external(self, thread_id: str) -> None:
        with self._lock:
            self._states[thread_id] = LeaseState.EXTERNAL_ACTIVE

    def recover(self, thread_id: str) -> None:
        with self._lock:
            if self._states.get(thread_id) is LeaseState.EXTERNAL_ACTIVE:
                self._states[thread_id] = LeaseState.CFR_ACTIVE

    def release(self, thread_id: str) -> None:
        with self._lock:
            self._states[thread_id] = LeaseState.IDLE


class WriterLease:
    """Backward-compatible single-thread facade used by older unit callers."""

    def __init__(self):
        self._manager = WriterLeaseManager()
        self._thread_id = '__default__'

    @property
    def state(self) -> LeaseState:
        return self._manager.state_for(self._thread_id)

    def acquire(self):
        self._manager.acquire(self._thread_id)

    def external(self):
        self._manager.mark_external(self._thread_id)

    def release(self):
        self._manager.release(self._thread_id)
