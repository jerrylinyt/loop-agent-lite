#!/usr/bin/env python3
"""Read-only completion-gate client used inside managed worker worktrees.

The client never invokes Git.  It publishes one durable request, races only the
``pending -> cancelled`` transition when its deadline expires, and translates a
supervisor-owned durable response into the strict one-line protocol consumed by
``engine.loop``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

from engine import parallel_contract
from engine import parallel_spool


RESPONSE_SCHEMA = 1
DEFAULT_WAIT_TIMEOUT_SECONDS = 120.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.05


class GateClientError(RuntimeError):
    """The durable gate exchange cannot be trusted."""


def _positive_int(raw: object, label: str) -> int:
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise GateClientError(f"{label} 必須是正整數") from exc
    if value < 1 or isinstance(raw, bool):
        raise GateClientError(f"{label} 必須是正整數")
    return value


def request_from_environment(env: Mapping[str, str], *, deadline_at: str) -> dict:
    """Build and validate the complete immutable request payload."""
    try:
        run_id = parallel_contract.require_run_id(env.get("RUN_ID"))
        task = _positive_int(env.get("TASK"), "TASK")
        request_id = parallel_spool.require_request_id(env.get("REQUEST_ID"))
        validated_sha = parallel_contract.require_git_sha(env.get("VALIDATED_SHA"))
        validated_round = _positive_int(env.get("VALIDATED_ROUND"), "VALIDATED_ROUND")
        run_config_hash = parallel_contract.require_config_hash(
            env.get("RUN_CONFIG_HASH"), "RUN_CONFIG_HASH")
        launch_spec_hash = parallel_contract.require_config_hash(
            env.get("LAUNCH_SPEC_HASH"), "LAUNCH_SPEC_HASH")
        manifest_hash = parallel_contract.require_config_hash(
            env.get("MANIFEST_HASH"), "MANIFEST_HASH")
    except (parallel_contract.ParallelContractError,
            parallel_spool.SpoolError) as exc:
        raise GateClientError(str(exc)) from exc
    return {
        "schema": 1,
        "run_id": run_id,
        "task": task,
        "request_id": request_id,
        "validated_sha": validated_sha,
        "validated_round": validated_round,
        "run_config_hash": run_config_hash,
        "launch_spec_hash": launch_spec_hash,
        "manifest_hash": manifest_hash,
        "deadline_at": deadline_at,
    }


def _protocol_payload(request: Mapping[str, object], status: str, reason: str | None = None) -> dict:
    payload = {
        "status": status,
        "run_id": request["run_id"],
        "task": request["task"],
        "request_id": request["request_id"],
        "validated_sha": request["validated_sha"],
    }
    if reason:
        payload["reason"] = str(reason).strip()
    return payload


def durable_response_envelope(
    request: Mapping[str, object],
    *,
    returncode: int,
    status: str,
    reason: str | None = None,
) -> dict:
    """Build and self-validate the supervisor's durable response artifact."""
    if returncode == 31 or status == "recovery-required-after-claim":
        raise GateClientError(
            "recovery-required-after-claim 是 local/nonterminal result，"
            "不可佔用 immutable durable response")
    payload = _protocol_payload(request, status, reason)
    try:
        parallel_contract.parse_gate_response(
            returncode,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            run_id=str(request["run_id"]), task=int(request["task"]),
            request_id=str(request["request_id"]),
            validated_sha=str(request["validated_sha"]),
        )
    except (KeyError, TypeError, ValueError,
            parallel_contract.ParallelContractError) as exc:
        raise GateClientError(f"supervisor gate response 不合法：{exc}") from exc
    return {
        "schema": RESPONSE_SCHEMA,
        "request_id": request["request_id"],
        "returncode": returncode,
        "response": payload,
    }


def _validate_durable_response(record, request: Mapping[str, object]) -> tuple[int, dict]:
    envelope = record.payload
    if (not isinstance(envelope, dict)
            or set(envelope) != {"schema", "request_id", "returncode", "response"}
            or envelope.get("schema") != RESPONSE_SCHEMA
            or envelope.get("request_id") != request.get("request_id")
            or not isinstance(envelope.get("returncode"), int)
            or isinstance(envelope.get("returncode"), bool)
            or not isinstance(envelope.get("response"), dict)):
        raise GateClientError("durable gate response schema 不合法")
    returncode = envelope["returncode"]
    response = envelope["response"]
    if (returncode == 31
            or response.get("status") == "recovery-required-after-claim"):
        raise GateClientError(
            "durable gate response 不可使用 nonterminal recovery-required")
    try:
        parallel_contract.parse_gate_response(
            returncode,
            json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            run_id=str(request["run_id"]), task=int(request["task"]),
            request_id=str(request["request_id"]),
            validated_sha=str(request["validated_sha"]),
        )
    except parallel_contract.ParallelContractError as exc:
        raise GateClientError(f"durable gate response 不符合 request：{exc}") from exc
    return returncode, response


def _validate_response_linearization(
    spool: parallel_spool.DurableSpool,
    request: Mapping[str, object],
    response_record,
) -> tuple[int, dict]:
    """Bind a terminal response to the request's durable CAS state."""
    returncode, response = _validate_durable_response(response_record, request)
    request_id = str(request["request_id"])
    durable = spool.get_request(request_id)
    if durable is None:
        raise GateClientError("gate response 沒有 durable request state")
    status = response["status"]
    claimed_statuses = {
        "merged", "already-merged", "stale-integration", "fatal-invariant",
    }
    cancelled_statuses = {
        "busy", "supervisor-lost-before-claim", "paused", "cancelled",
    }
    if (status in claimed_statuses and durable.state != "claimed"):
        raise GateClientError(
            f"gate response {status} 要求 request 已 claimed；目前是 {durable.state}")
    if (status in cancelled_statuses and durable.state != "cancelled"):
        raise GateClientError(
            f"gate response {status} 要求 request 已 cancelled；目前是 {durable.state}")
    return returncode, response


def execute_gate(
    run_dir: Path,
    *,
    env: Mapping[str, str],
    wait_timeout: float,
    poll_interval: float,
    monotonic=time.monotonic,
    sleeper=time.sleep,
) -> tuple[int, dict]:
    """Perform one publish/wait/cancel exchange without touching a repository."""
    if (not math.isfinite(wait_timeout) or wait_timeout <= 0
            or not math.isfinite(poll_interval) or poll_interval <= 0):
        raise GateClientError("gate timeout/poll interval 必須是有限正數")
    run_dir = Path(run_dir)
    try:
        info = run_dir.lstat()
    except OSError as exc:
        raise GateClientError(f"parallel run dir 不可讀：{exc}") from exc
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise GateClientError("parallel run dir 必須是實體目錄")
    deadline_at = (datetime.now(timezone.utc) + timedelta(seconds=wait_timeout)).isoformat()
    request = request_from_environment(env, deadline_at=deadline_at)
    spool = parallel_spool.DurableSpool(
        run_dir / "requests", responses_root=run_dir / "responses")
    try:
        spool.publish_request(str(request["request_id"]), request)
    except parallel_spool.SpoolError as exc:
        raise GateClientError(f"gate request publish 失敗：{exc}") from exc

    deadline = monotonic() + wait_timeout
    while True:
        try:
            response = spool.get_response(str(request["request_id"]))
        except parallel_spool.SpoolNotFoundError:
            response = None
        except parallel_spool.SpoolError as exc:
            raise GateClientError(f"gate response spool 損壞：{exc}") from exc
        if response is not None:
            return _validate_response_linearization(spool, request, response)
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleeper(min(poll_interval, remaining))

    try:
        cancellation = spool.cancel_request(str(request["request_id"]))
    except parallel_spool.SpoolNotFoundError as exc:
        raise GateClientError("gate request 在 deadline 時無 durable state") from exc
    except parallel_spool.SpoolError as exc:
        raise GateClientError(f"gate deadline transition 失敗：{exc}") from exc

    # A response may have linearized just before our transition attempt.
    try:
        response = spool.get_response(str(request["request_id"]))
    except parallel_spool.SpoolNotFoundError:
        response = None
    if response is not None:
        return _validate_response_linearization(spool, request, response)
    # Only the client that actually won pending -> cancelled can prove that no
    # supervisor claimed this request.  Merely observing ``cancelled`` is not
    # sufficient: Pause/Abort may have won the CAS and owns the corresponding
    # terminal response.  Returning safe-retry in that race could let a worker
    # resume while the parent is quiescing.
    if cancellation.transitioned:
        return 11, _protocol_payload(
            request, "supervisor-lost-before-claim",
            "gate deadline 前 supervisor 尚未 claim；client 已原子取消 pending request",
        )
    if cancellation.record.state == "cancelled":
        return 31, _protocol_payload(
            request, "recovery-required-after-claim",
            "gate request 已由另一個 owner 取消但尚無 terminal response；不可推定為安全重試",
        )
    if cancellation.record.state == "claimed":
        return 31, _protocol_payload(
            request, "recovery-required-after-claim",
            "gate request 已被 claim 但 deadline 內沒有 terminal response；必須由 supervisor reconcile",
        )
    raise GateClientError(
        f"gate request deadline 後出現未知 state {cancellation.record.state!r}")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="managed parallel worker completion gate")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--wait-timeout", type=float, default=DEFAULT_WAIT_TIMEOUT_SECONDS)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS,
                        help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        returncode, payload = execute_gate(
            Path(args.run_dir), env=os.environ,
            wait_timeout=args.wait_timeout, poll_interval=args.poll_interval)
    except (GateClientError, OSError, ValueError) as exc:
        # We cannot safely know whether a malformed/corrupt spool request was
        # claimed.  Preserve the worker's durable gate_request for reconciliation.
        raw_request = {
            "run_id": os.environ.get("RUN_ID"),
            "task": os.environ.get("TASK"),
            "request_id": os.environ.get("REQUEST_ID"),
            "validated_sha": os.environ.get("VALIDATED_SHA"),
        }
        try:
            request = {
                "run_id": parallel_contract.require_run_id(raw_request["run_id"]),
                "task": _positive_int(raw_request["task"], "TASK"),
                "request_id": parallel_spool.require_request_id(raw_request["request_id"]),
                "validated_sha": parallel_contract.require_git_sha(raw_request["validated_sha"]),
            }
            payload = _protocol_payload(
                request, "recovery-required-after-claim", str(exc))
            returncode = 31
        except (GateClientError, parallel_contract.ParallelContractError,
                parallel_spool.SpoolError):
            print(f"parallel gate fatal：{exc}", file=sys.stderr)
            return 31
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
