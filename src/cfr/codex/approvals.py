from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED_APPROVAL_METHODS = {
    'item/commandExecution/requestApproval': 'command',
    'item/fileChange/requestApproval': 'file_change',
    'item/permissions/requestApproval': 'permissions',
    'execCommandApproval': 'legacy_command',
    'applyPatchApproval': 'legacy_file_change',
}

# Server-request compatibility is an explicit contract. Methods in
# ``CFR_UNADVERTISED_SERVER_REQUEST_METHODS`` are intentionally rejected with
# JSON-RPC method-not-supported because CFR never advertises the corresponding
# client capability. Keeping them listed still lets Doctor detect a newly
# introduced request method instead of silently reporting full compatibility.
CFR_INTERACTIVE_SERVER_REQUEST_METHODS = frozenset({
    'item/commandExecution/requestApproval',
    'item/fileChange/requestApproval',
    'item/permissions/requestApproval',
    'item/tool/requestUserInput',
    'execCommandApproval',
    'applyPatchApproval',
})
CFR_AUTOMATIC_SERVER_REQUEST_METHODS = frozenset({'currentTime/read'})
CFR_UNADVERTISED_SERVER_REQUEST_METHODS = frozenset({
    'item/tool/call',
    'mcpServer/elicitation/request',
    'account/chatgptAuthTokens/refresh',
    'attestation/generate',
})
CFR_ACCOUNTED_SERVER_REQUEST_METHODS = (
    CFR_INTERACTIVE_SERVER_REQUEST_METHODS
    | CFR_AUTOMATIC_SERVER_REQUEST_METHODS
    | CFR_UNADVERTISED_SERVER_REQUEST_METHODS
)
CFR_REQUIRED_SERVER_REQUEST_METHODS = frozenset({
    'item/commandExecution/requestApproval',
    'item/fileChange/requestApproval',
    'item/permissions/requestApproval',
    'item/tool/requestUserInput',
})


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
    grant_root: str | None = None
    method: str | None = None
    native_kind: str | None = None
    command_actions: tuple[dict[str, Any], ...] = ()
    requested_permissions: dict[str, Any] | None = None
    network_approval_context: dict[str, Any] | None = None
    proposed_execpolicy_amendment: tuple[str, ...] = ()
    proposed_network_policy_amendments: tuple[dict[str, Any], ...] = ()


class CodexApprovalCodec:
    """Translate the small supported Codex approval surface to neutral data."""

    def decode(self, message: dict[str, Any], thread_id: str) -> ApprovalRequest | None:
        if not isinstance(message, dict):
            return None
        method = message.get('method')
        if not isinstance(method, str):
            return None
        kind = SUPPORTED_APPROVAL_METHODS.get(method)
        request_id = message.get('id')
        if kind is None or request_id is None:
            return None
        if not thread_id:
            return None
        raw_params = message.get('params')
        params = {} if raw_params is None else raw_params
        if not isinstance(params, dict):
            return None
        item = params.get('item') or params
        if not isinstance(item, dict):
            return None
        if method in {'execCommandApproval', 'applyPatchApproval'}:
            legacy_thread_id = params.get('conversationId') or thread_id
            if not legacy_thread_id:
                return None
            thread_id = str(legacy_thread_id)
        decisions = item.get('availableDecisions') or item.get('available_decisions') or ('accept', 'decline')
        if isinstance(decisions, str):
            decisions = (decisions,)
        elif not isinstance(decisions, (list, tuple, set)):
            decisions = ('accept', 'decline')
        changed = item.get('changedPaths') or item.get('changed_paths') or params.get('_cfrChangedPaths') or ()
        if method == 'applyPatchApproval' and not changed:
            file_changes = params.get('fileChanges') or {}
            if isinstance(file_changes, dict):
                changed = tuple(str(path) for path in file_changes)
        if isinstance(changed, str):
            changed = (changed,)
        elif not isinstance(changed, (list, tuple, set)):
            changed = ()
        command = item.get('command') or item.get('cmd')
        if method == 'execCommandApproval' and isinstance(command, (list, tuple)):
            command = ' '.join(str(part) for part in command)
        native_kind = item.get('kind')
        if method == 'execCommandApproval':
            native_kind = 'command'
        elif method == 'applyPatchApproval':
            native_kind = 'file_change'
        elif method == 'item/fileChange/requestApproval':
            native_kind = 'file_change'
        elif method == 'item/permissions/requestApproval':
            native_kind = 'permissions'
        command_actions = item.get('commandActions') or item.get('parsedCmd') or ()
        if not isinstance(command_actions, (list, tuple)):
            command_actions = ()
        requested_permissions = item.get('permissions') if method == 'item/permissions/requestApproval' else item.get('additionalPermissions')
        if not isinstance(requested_permissions, dict):
            requested_permissions = None
        network_context = item.get('networkApprovalContext')
        if not isinstance(network_context, dict):
            network_context = None
        execpolicy = item.get('proposedExecpolicyAmendment') or ()
        if isinstance(execpolicy, str):
            execpolicy = (execpolicy,)
        elif not isinstance(execpolicy, (list, tuple)):
            execpolicy = ()
        network_amendments = item.get('proposedNetworkPolicyAmendments') or ()
        if isinstance(network_amendments, dict):
            network_amendments = (network_amendments,)
        elif not isinstance(network_amendments, (list, tuple)):
            network_amendments = ()
        display_kind = kind
        if kind == 'command' and native_kind == 'writeStdin':
            display_kind = 'write_stdin'
        elif kind == 'legacy_command':
            display_kind = 'command'
        elif kind == 'legacy_file_change':
            display_kind = 'file_change'
        return ApprovalRequest(
            request_id=str(request_id),
            thread_id=thread_id,
            turn_id=params.get('turnId') or item.get('turnId'),
            item_id=params.get('itemId') or item.get('id'),
            kind=display_kind,
            reason=item.get('reason'),
            command=command,
            cwd=item.get('cwd'),
            changed_paths=tuple(str(path) for path in changed),
            available_decisions=tuple(str(decision) for decision in decisions),
            grant_root=item.get('grantRoot') or item.get('grant_root'),
            method=method,
            native_kind=str(native_kind) if native_kind is not None else None,
            command_actions=tuple(dict(value) for value in command_actions if isinstance(value, dict)),
            requested_permissions=dict(requested_permissions) if requested_permissions is not None else None,
            network_approval_context=dict(network_context) if network_context is not None else None,
            proposed_execpolicy_amendment=tuple(str(value) for value in execpolicy),
            proposed_network_policy_amendments=tuple(dict(value) for value in network_amendments if isinstance(value, dict)),
        )

    @staticmethod
    def response(decision: str, request: ApprovalRequest | None = None) -> dict[str, Any]:
        if decision not in {'accept', 'approve_once', 'decline', 'deny', 'cancel'}:
            raise ValueError(f'Unsupported approval decision: {decision}')
        normalized = 'accept' if decision == 'approve_once' else 'decline' if decision == 'deny' else decision
        method = request.method if request is not None else None
        if method == 'item/permissions/requestApproval':
            permissions = request.requested_permissions if normalized == 'accept' and request is not None else {}
            return {'permissions': dict(permissions or {}), 'scope': 'turn'}
        if method in {'execCommandApproval', 'applyPatchApproval'}:
            legacy = 'approved' if normalized == 'accept' else 'abort' if normalized == 'cancel' else {'denied': {'rejection': 'Declined by user'}}
            return {'decision': legacy}
        return {'decision': normalized}
