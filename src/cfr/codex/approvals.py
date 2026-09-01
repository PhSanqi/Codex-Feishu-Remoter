from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED_APPROVAL_METHODS = {
    'item/commandExecution/requestApproval': 'command',
    'item/fileChange/requestApproval': 'file_change',
}


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    thread_id: str
    turn_id: str | None
    item_id: str | None
    kind: str
    reason: str | None
    command: str | None
    cwd: str | None
    changed_paths: tuple[str, ...]
    available_decisions: tuple[str, ...]


class CodexApprovalCodec:
    """Translate the small supported Codex approval surface to neutral data."""

    def decode(self, message: dict[str, Any], thread_id: str) -> ApprovalRequest | None:
        method = message.get('method')
        kind = SUPPORTED_APPROVAL_METHODS.get(method)
        if kind is None:
            return None
        if not thread_id:
            return None
        params = message.get('params') or {}
        item = params.get('item') or params
        decisions = item.get('availableDecisions') or item.get('available_decisions') or ('accept', 'decline')
        if isinstance(decisions, str):
            decisions = (decisions,)
        changed = item.get('changedPaths') or item.get('changed_paths') or ()
        return ApprovalRequest(
            request_id=str(message.get('id')),
            thread_id=thread_id,
            turn_id=params.get('turnId') or item.get('turnId'),
            item_id=params.get('itemId') or item.get('id'),
            kind=kind,
            reason=item.get('reason'),
            command=item.get('command') or item.get('cmd'),
            cwd=item.get('cwd'),
            changed_paths=tuple(str(path) for path in changed),
            available_decisions=tuple(str(decision) for decision in decisions),
        )

    @staticmethod
    def response(decision: str) -> dict[str, str]:
        if decision not in {'accept', 'approve_once', 'decline', 'deny', 'cancel'}:
            raise ValueError(f'Unsupported approval decision: {decision}')
        normalized = 'accept' if decision == 'approve_once' else 'decline' if decision == 'deny' else decision
        return {'decision': normalized}
