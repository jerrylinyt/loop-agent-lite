"""Pure contracts shared by the parallel supervisor and managed workers.

This module deliberately owns no Git mutation.  It validates the small pieces
of data that cross the worker/supervisor boundary so ``engine.loop`` can keep
its existing convergence state machine and treat the completion gate as a
strict external coordinator signal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


RUN_ID_HEX_LENGTH = 8
RUN_ID_RE = re.compile(rf"[0-9a-f]{{{RUN_ID_HEX_LENGTH}}}")
GIT_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
CONFIG_HASH_RE = re.compile(r"[0-9a-f]{64}")
INTEGRATION_REF_RE = re.compile(
    rf"refs/heads/loop/(?P<run_id>[0-9a-f]{{{RUN_ID_HEX_LENGTH}}})/integration"
)

GATE_STATUSES_BY_RC = {
    0: frozenset({"merged", "already-merged"}),
    10: frozenset({"stale-integration"}),
    11: frozenset({"busy", "supervisor-lost-before-claim"}),
    20: frozenset({"paused"}),
    21: frozenset({"cancelled"}),
    30: frozenset({"fatal-invariant"}),
    31: frozenset({"recovery-required-after-claim"}),
}

GATE_RESPONSE_FIELDS = frozenset({
    "status", "run_id", "task", "request_id", "validated_sha", "reason",
})
GATE_RESPONSE_REQUIRED_FIELDS = frozenset({
    "status", "run_id", "task", "request_id", "validated_sha",
})

WORKER_QUIESCENT_STATUSES = frozenset({
    "integrated", "paused", "cancelled", "blocked", "recovery-required",
})


class ParallelContractError(ValueError):
    """A managed-worker contract is malformed or inconsistent."""


@dataclass(frozen=True)
class GateResult:
    """Validated completion-gate response returned to a managed worker."""

    returncode: int
    status: str
    run_id: str
    task: int
    request_id: str
    validated_sha: str
    reason: str | None = None


def require_run_id(value: object) -> str:
    """Return a canonical fixed-length lowercase run id or fail closed."""
    if not isinstance(value, str) or RUN_ID_RE.fullmatch(value) is None:
        raise ParallelContractError(
            f"run_id 必須是 {RUN_ID_HEX_LENGTH} 字元小寫 hex"
        )
    return value


def integration_ref_for(run_id: str) -> str:
    """Derive the only sync ref shape a worker is allowed to receive."""
    return f"refs/heads/loop/{require_run_id(run_id)}/integration"


def run_id_from_integration_ref(value: object) -> str:
    """Validate a canonical full integration ref and return its run id."""
    if not isinstance(value, str):
        raise ParallelContractError("integration ref 必須是字串")
    match = INTEGRATION_REF_RE.fullmatch(value)
    if match is None:
        raise ParallelContractError(
            "integration ref 必須是 refs/heads/loop/<8字元小寫hex>/integration"
        )
    return match.group("run_id")


def require_config_hash(value: object, label: str) -> str:
    """Validate immutable run/launch hashes used by gate authority checks."""
    if not isinstance(value, str) or CONFIG_HASH_RE.fullmatch(value) is None:
        raise ParallelContractError(f"{label} 必須是 64 字元小寫 SHA-256")
    return value


def require_git_sha(value: object, label: str = "validated_sha") -> str:
    """Validate a full SHA-1/SHA-256 object id without resolving it."""
    if not isinstance(value, str) or GIT_SHA_RE.fullmatch(value) is None:
        raise ParallelContractError(f"{label} 必須是完整小寫 Git SHA")
    return value


def managed_sync_instructions(integration_ref: str, block_command: str) -> str:
    """Render the conditional prompt section for a validated safe sync ref."""
    run_id_from_integration_ref(integration_ref)
    if not isinstance(block_command, str) or not block_command.strip():
        raise ParallelContractError("block command 必須是非空字串")
    return (
        "1a. **同步整合基線（受管 worker）**：先用 "
        "`git rev-parse -q --verify MERGE_HEAD` 判斷是否已有 merge-in-progress。\n"
        f"   - 沒有 merge-in-progress：執行 `git merge --no-edit {integration_ref}`；"
        "顯示 already up-to-date 視為 no-op。\n"
        f"   - 已有 merge-in-progress：先確認 `MERGE_HEAD` 是 `{integration_ref}` 當前 tip 的祖先。"
        "只有成立時才可接續解衝突、跑完整 Validate 並 commit；該輪不得 done。\n"
        f"   - 若無法證明現有 merge 來自安全 ref，執行 `{block_command} "
        "\"偵測到未知 merge-in-progress\"` 後立即停止，不可自行 abort 掩蓋現場。\n"
        "   - 禁止 checkout/detach、rebase、force、`git update-ref`、刪改 integration/sync/peer "
        "task refs，也禁止直接 merge peer task branch。同步若產生任何 commit，本輪不得 done。"
    )


def parse_gate_response(
    returncode: int,
    stdout: str,
    *,
    run_id: str,
    task: int,
    request_id: str,
    validated_sha: str,
) -> GateResult:
    """Parse the gate's one-line JSON response and bind it to this request.

    Unknown exit codes, status/exit-code mismatches, extra fields, and echoed
    authority fields that differ from the request are all fatal.  Human text
    is retained only as an optional reason; it never controls state changes.
    """
    expected_run_id = require_run_id(run_id)
    expected_sha = require_git_sha(validated_sha)
    if (not isinstance(task, int) or isinstance(task, bool) or task < 1):
        raise ParallelContractError("task 必須是正整數")
    if not isinstance(request_id, str) or not request_id:
        raise ParallelContractError("request_id 必須是非空字串")

    allowed_statuses = GATE_STATUSES_BY_RC.get(returncode)
    if allowed_statuses is None:
        raise ParallelContractError(f"gate 回傳未知 exit code {returncode}")
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ParallelContractError("gate stdout 必須恰好是一行 JSON")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ParallelContractError(f"gate JSON 解析失敗:{exc}") from exc
    if not isinstance(payload, dict):
        raise ParallelContractError("gate JSON 頂層必須是 object")
    fields = set(payload)
    extra = fields - GATE_RESPONSE_FIELDS
    missing = GATE_RESPONSE_REQUIRED_FIELDS - fields
    if extra or missing:
        details = []
        if missing:
            details.append(f"缺少 {sorted(missing)}")
        if extra:
            details.append(f"未知欄位 {sorted(extra)}")
        raise ParallelContractError("gate JSON schema 不符:" + "；".join(details))

    status = payload["status"]
    if not isinstance(status, str) or status not in allowed_statuses:
        raise ParallelContractError(
            f"gate exit code {returncode} 不接受 status {status!r}"
        )
    echoed = {
        "run_id": expected_run_id,
        "task": task,
        "request_id": request_id,
        "validated_sha": expected_sha,
    }
    for field, expected in echoed.items():
        if payload[field] != expected:
            raise ParallelContractError(
                f"gate {field} 不符:預期 {expected!r},收到 {payload[field]!r}"
            )
    reason = payload.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise ParallelContractError("gate reason 必須是非空字串或省略")
    return GateResult(
        returncode=returncode,
        status=status,
        run_id=expected_run_id,
        task=task,
        request_id=request_id,
        validated_sha=expected_sha,
        reason=reason.strip() if reason is not None else None,
    )
