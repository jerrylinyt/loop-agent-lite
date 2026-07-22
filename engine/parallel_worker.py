"""Managed-worker launch and state contracts.

The state helpers are pure.  Launch authorization is the narrow exception: it
validates immutable artifacts and atomically claims/verifies the durable launch
spool, but performs no Git, process, or worker-workspace mutation.
"""

from __future__ import annotations

import argparse
import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from engine import parallel_contract as contract
from engine import platform_compat as compat


WORKSPACE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
WORKER_RUNNER = "parallel-worker"
ASSIGNMENT_FIELDS = frozenset({
    "status", "validated_sha", "validated_round", "exit_reason", "pause_generation",
    "gate_request",
})
ASSIGNMENT_STATUSES = frozenset({
    "running", "integrated", "paused", "cancelled", "blocked",
    "recovery-required",
})


class LaunchReservationUnavailable(contract.ParallelContractError):
    """Pause/Abort cancelled a guardian reservation before its claim CAS."""


@dataclass(frozen=True)
class ManagedWorkerLaunch:
    """Canonical identity/configuration supplied by one supervisor assignment."""

    resume: bool
    run_id: str
    assigned_order: int
    stop_after_task: bool
    complete_gate_cmd: str
    integration_ref: str
    parent_workspace: str
    task_ref: str
    run_config_hash: str
    launch_spec_hash: str
    manifest_hash: str
    dispatch_token: str
    dispatch_request_id: str
    supervisor_session: str
    supervisor_generation: int
    dispatch_attempt: int


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare the complete supervisor-managed worker argv surface."""
    parser.add_argument("--start-task", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--stop-after-task", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--complete-gate-cmd", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--integration-ref", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--managed-worker-resume", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent-workspace", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--task-ref", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--run-config-hash", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--launch-spec-hash", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--manifest-hash", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--dispatch-token", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--dispatch-request-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-session", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-generation", type=int, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--dispatch-attempt", type=int, default=None,
                        help=argparse.SUPPRESS)


def task_ref_for(run_id: str, order: int) -> str:
    """Derive the only task ref accepted for a run/order pair."""
    run_id = contract.require_run_id(run_id)
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        raise contract.ParallelContractError("assigned order 必須是正整數")
    return f"refs/heads/loop/{run_id}/task-{order}"


def _valid_workspace_name(value: object) -> bool:
    return (isinstance(value, str) and not value.startswith(".")
            and WORKSPACE_NAME_RE.fullmatch(value) is not None)


def _worker_surface_present(args) -> bool:
    string_fields = (
        "complete_gate_cmd", "integration_ref", "parent_workspace", "task_ref",
        "run_config_hash", "launch_spec_hash", "manifest_hash", "dispatch_token",
        "dispatch_request_id", "supervisor_session",
    )
    return (getattr(args, "start_task", None) is not None
            or getattr(args, "stop_after_task", False) is True
            or getattr(args, "managed_worker_resume", False) is True
            or getattr(args, "supervisor_generation", None) is not None
            or getattr(args, "dispatch_attempt", None) is not None
            or any(getattr(args, field, None) is not None for field in string_fields))


def _parser_error(parser: argparse.ArgumentParser, message: str):
    parser.error(message)
    raise AssertionError("ArgumentParser.error must not return")


def validate_launch_args(parser: argparse.ArgumentParser, args) -> ManagedWorkerLaunch | None:
    """Validate and canonicalize the all-or-nothing managed-worker argv group.

    A normal loop invocation has none of these flags and returns ``None``.
    Worker mode always requires the full immutable identity/config group.  The
    integration ref is the sole run-id authority; the task ref is derived from
    that run id and ``--start-task`` rather than trusted independently.
    """
    if not _worker_surface_present(args):
        return None

    required = {
        "--start-task": getattr(args, "start_task", None),
        "--stop-after-task": getattr(args, "stop_after_task", False),
        "--complete-gate-cmd": getattr(args, "complete_gate_cmd", None),
        "--integration-ref": getattr(args, "integration_ref", None),
        "--parent-workspace": getattr(args, "parent_workspace", None),
        "--task-ref": getattr(args, "task_ref", None),
        "--run-config-hash": getattr(args, "run_config_hash", None),
        "--launch-spec-hash": getattr(args, "launch_spec_hash", None),
        "--manifest-hash": getattr(args, "manifest_hash", None),
        "--dispatch-token": getattr(args, "dispatch_token", None),
        "--dispatch-request-id": getattr(args, "dispatch_request_id", None),
        "--supervisor-session": getattr(args, "supervisor_session", None),
        "--supervisor-generation": getattr(args, "supervisor_generation", None),
        "--dispatch-attempt": getattr(args, "dispatch_attempt", None),
    }
    missing = []
    for option, value in required.items():
        if option == "--stop-after-task":
            absent = value is not True
        elif option in {
                "--start-task", "--supervisor-generation", "--dispatch-attempt"}:
            absent = value is None
        else:
            absent = not isinstance(value, str) or not value.strip()
        if absent:
            missing.append(option)
    if missing:
        _parser_error(
            parser,
            "managed worker flags 必須整組提供；缺少 " + ", ".join(missing),
        )

    order = args.start_task
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        _parser_error(parser, "--start-task 必須是正整數")
    if not _valid_workspace_name(args.parent_workspace):
        _parser_error(parser, "--parent-workspace 名稱不合法")
    gate_cmd = args.complete_gate_cmd.strip()
    if "\x00" in gate_cmd:
        _parser_error(parser, "--complete-gate-cmd 不得含 NUL")
    dispatch_token = args.dispatch_token
    if ("\x00" in dispatch_token or "\r" in dispatch_token or "\n" in dispatch_token
            or len(dispatch_token.encode("utf-8")) > 4096):
        _parser_error(parser, "--dispatch-token 格式不合法")
    if (not isinstance(args.dispatch_request_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", args.dispatch_request_id) is None):
        _parser_error(parser, "--dispatch-request-id 格式不合法")
    if (not isinstance(args.supervisor_session, str)
            or re.fullmatch(r"[0-9a-f]{32}", args.supervisor_session) is None):
        _parser_error(parser, "--supervisor-session 格式不合法")
    if (not isinstance(args.supervisor_generation, int)
            or isinstance(args.supervisor_generation, bool)
            or args.supervisor_generation < 1):
        _parser_error(parser, "--supervisor-generation 必須是正整數")
    if (not isinstance(args.dispatch_attempt, int)
            or isinstance(args.dispatch_attempt, bool)
            or args.dispatch_attempt < 0):
        _parser_error(parser, "--dispatch-attempt 必須是非負整數")

    try:
        run_id = contract.run_id_from_integration_ref(args.integration_ref)
        expected_task_ref = task_ref_for(run_id, order)
        run_config_hash = contract.require_config_hash(
            args.run_config_hash, "run_config_hash")
        launch_spec_hash = contract.require_config_hash(
            args.launch_spec_hash, "launch_spec_hash")
        manifest_hash = contract.require_config_hash(args.manifest_hash, "manifest_hash")
    except contract.ParallelContractError as exc:
        _parser_error(parser, str(exc))
    if args.task_ref != expected_task_ref:
        _parser_error(
            parser,
            f"--task-ref 必須由 run_id/order 唯一推導為 {expected_task_ref}",
        )

    resume = getattr(args, "managed_worker_resume", False) is True
    forbidden = {
        "--reset-state": getattr(args, "reset_state", False),
        "--resume-interrupted": getattr(args, "resume_interrupted", False),
        "--init-only": getattr(args, "init_only", False),
        "--preflight-only": getattr(args, "preflight_only", False),
        "--consume-import-plan": getattr(args, "consume_import_plan", False),
        "--max-rounds": getattr(args, "max_rounds", 0),
    }
    if resume:
        forbidden["--import-plan"] = getattr(args, "import_plan", "")
        active = [option for option, value in forbidden.items() if value]
        if active:
            _parser_error(
                parser,
                "--managed-worker-resume 不可搭配 " + ", ".join(active),
            )
    else:
        active = [option for option, value in forbidden.items() if value]
        if active:
            _parser_error(parser, "managed worker 首次啟動不可搭配 " + ", ".join(active))
        import_plan = getattr(args, "import_plan", "")
        if not str(import_plan).strip():
            _parser_error(parser, "managed worker 首次啟動必須提供 --import-plan")
        if getattr(args, "start_phase", "plan") != "exec":
            _parser_error(parser, "managed worker 首次啟動必須使用 --start-phase exec")

    return ManagedWorkerLaunch(
        resume=resume,
        run_id=run_id,
        assigned_order=order,
        stop_after_task=True,
        complete_gate_cmd=gate_cmd,
        integration_ref=args.integration_ref,
        parent_workspace=args.parent_workspace,
        task_ref=expected_task_ref,
        run_config_hash=run_config_hash,
        launch_spec_hash=launch_spec_hash,
        manifest_hash=manifest_hash,
        dispatch_token=dispatch_token,
        dispatch_request_id=args.dispatch_request_id,
        supervisor_session=args.supervisor_session,
        supervisor_generation=args.supervisor_generation,
        dispatch_attempt=args.dispatch_attempt,
    )


def _validated_launch_artifacts(run_dir: Path):
    from engine import parallel_state

    try:
        return parallel_state.validate_run_artifacts(run_dir)
    except (OSError, ValueError, parallel_state.ParallelStateError) as exc:
        raise contract.ParallelContractError(
            f"managed worker immutable authority 無法驗證：{exc}") from exc


def _expected_launch_reservation(
        artifacts, *, task: int, request_id: str,
        supervisor_session: str, supervisor_generation: int,
        attempt: int, resume: bool) -> dict:
    try:
        assignment_hash = artifacts.assignment_hashes[task]
    except KeyError as exc:
        raise contract.ParallelContractError(
            "managed worker launch task 不在 immutable assignments") from exc
    return {
        "schema": 1,
        "request_id": request_id,
        "run_id": artifacts.manifest["run_id"],
        "task": task,
        "manifest_hash": artifacts.manifest_hash,
        "run_config_hash": artifacts.run_config_hash,
        "launch_spec_hash": assignment_hash,
        "supervisor_session": supervisor_session,
        "supervisor_generation": supervisor_generation,
        "attempt": attempt,
        "resume": resume,
    }


def _require_live_parent(
        artifacts, *, task_order: int, supervisor_session: str,
        supervisor_generation: int, attempt: int) -> None:
    """Prove the exact supervisor still permits this assignment to start."""
    from engine import loop as loop_mod  # runtime import avoids module cycle
    from engine import parallel_state

    run_id = artifacts.manifest["run_id"]
    parent_workspace = artifacts.manifest["parent_workspace"]
    workspace_root = artifacts.run_dir.parent.parent.parent
    base_dir = loop_mod.workspace_path(workspace_root, parent_workspace)
    try:
        base_state, _raw, _recovered = loop_mod.load_checkpointed_state(
            base_dir / "state.json", repair=False)
    except (FileNotFoundError, OSError, ValueError, loop_mod.StateLoadError) as exc:
        raise contract.ParallelContractError(
            f"managed worker parent state 無法驗證：{exc}") from exc
    parallel = base_state.get("parallel")
    loop_state = base_state.get("loop")
    if (base_state.get("runner") != "parallel-supervisor"
            or not isinstance(parallel, dict)
            or parallel.get("run_id") != run_id
            or parallel.get("manifest_hash") != artifacts.manifest_hash
            or parallel.get("supervisor_generation") != supervisor_generation
            or not isinstance(loop_state, dict)
            or loop_state.get("session_id") != supervisor_session
            or not isinstance(loop_state.get("pid"), int)
            or isinstance(loop_state.get("pid"), bool)):
        raise contract.ParallelContractError(
            "managed worker parent supervisor state 不具有效 launch authority")
    try:
        aggregate = parallel_state.validate_aggregate(
            parallel_state.read_canonical_json(
                artifacts.run_dir, "aggregate.json"),
            run_id=run_id,
            plan=artifacts.plan,
        )
    except (OSError, ValueError, parallel_state.ParallelStateError) as exc:
        raise contract.ParallelContractError(
            f"managed worker parent aggregate 無法驗證：{exc}") from exc
    if (aggregate["status"] not in {"initializing", "running"}
            or aggregate["terminal_intent"] is not None
            or aggregate["batch"] != parallel.get("batch")):
        raise contract.ParallelContractError(
            "managed worker parent aggregate 已不允許派工")
    tasks = aggregate.get("tasks")
    matches = ([task for task in tasks
                if isinstance(task, dict)
                and task.get("order") == task_order]
               if isinstance(tasks, list) else [])
    if len(matches) != 1:
        raise contract.ParallelContractError(
            "managed worker task 不在 parent aggregate projection")
    task = matches[0]
    if (task.get("outcome") != "pending"
            or task.get("resource_state") not in {"provisioning", "running"}
            or task.get("restart_count") != attempt
            or task.get("batch") != aggregate.get("batch")):
        raise contract.ParallelContractError(
            "managed worker task 目前 batch/resource/restart attempt 不可派工")
    try:
        owner = loop_mod.active_run_lock_owner(base_dir / ".run.lock")
    except (OSError, ValueError) as exc:
        raise contract.ParallelContractError(
            f"managed worker parent run lock 無法安全讀取：{exc}") from exc
    if (not isinstance(owner, dict)
            or owner.get("pid") != loop_state.get("pid")
            or owner.get("session_id") != supervisor_session
            or owner.get("generation") != supervisor_generation):
        raise contract.ParallelContractError(
            "managed worker parent supervisor 未持有一致的 base run lock/session/generation")


def _launch_spool(artifacts):
    from engine import parallel_spool

    try:
        return parallel_spool.DurableSpool(artifacts.run_dir / "launches")
    except (OSError, ValueError, parallel_spool.SpoolError) as exc:
        raise contract.ParallelContractError(
            f"managed worker launch reservation 無法驗證：{exc}") from exc


def _publish_launch_rejected(spool, request_id: str, pid: int, reason: str) -> None:
    from engine import parallel_spool

    try:
        spool.publish_response(request_id, {
            "schema": 1,
            "request_id": request_id,
            "status": "rejected",
            "pid": pid,
            "reason": reason,
        })
    except parallel_spool.SpoolError as exc:
        raise contract.ParallelContractError(
            "managed worker claim 後 authority 失效，且 rejected marker 無法落盤："
            f"{exc}") from exc


def claim_guardian_launch(
        run_dir: Path | str, child_record: Mapping[str, object], *,
        payload_pid: int) -> object:
    """Claim and authorize one reservation before guardian payload release.

    The durable ACKed child record supplies the exact owner generation and
    payload identity.  A Pause/Abort cancellation that wins the spool CAS keeps
    the inert bootstrap behind its pipe forever; a successful claim binds the
    authorized response to that already-fenceable payload PID.
    """
    from engine import parallel_spool

    artifacts = _validated_launch_artifacts(Path(run_dir))
    required = (
        "run_id", "task", "child_id", "supervisor_session",
        "supervisor_generation", "attempt", "resume", "state", "payload_pid",
    )
    if not isinstance(child_record, Mapping) or any(
            field not in child_record for field in required):
        raise contract.ParallelContractError(
            "guardian launch 缺少 durable child authority")
    if (child_record["state"] != "acked"
            or child_record["run_id"] != artifacts.manifest["run_id"]
            or not isinstance(payload_pid, int) or isinstance(payload_pid, bool)
            or payload_pid < 2 or child_record["payload_pid"] != payload_pid):
        raise contract.ParallelContractError(
            "guardian launch durable payload identity 不符")
    task = child_record["task"]
    request_id = child_record["child_id"]
    session = child_record["supervisor_session"]
    generation = child_record["supervisor_generation"]
    attempt = child_record["attempt"]
    resume = child_record["resume"]
    expected = _expected_launch_reservation(
        artifacts,
        task=task,
        request_id=request_id,
        supervisor_session=session,
        supervisor_generation=generation,
        attempt=attempt,
        resume=resume,
    )
    _require_live_parent(
        artifacts,
        task_order=task,
        supervisor_session=session,
        supervisor_generation=generation,
        attempt=attempt,
    )
    spool = _launch_spool(artifacts)
    try:
        record = spool.get_request(request_id)
    except parallel_spool.SpoolError as exc:
        raise contract.ParallelContractError(
            f"managed worker launch reservation 無法驗證：{exc}") from exc
    if record is None or record.state != "pending" or record.payload != expected:
        state = None if record is None else record.state
        error_type = (
            LaunchReservationUnavailable if state == "cancelled"
            else contract.ParallelContractError)
        raise error_type(
            f"managed worker launch reservation 不可 claim（state={state!r}）")
    try:
        claimed = spool.claim_request(request_id)
    except parallel_spool.SpoolError as exc:
        raise contract.ParallelContractError(
            f"managed worker launch reservation claim 失敗：{exc}") from exc
    if not claimed.transitioned or claimed.record.state != "claimed":
        raise LaunchReservationUnavailable(
            "managed worker launch reservation 已取消、已使用或遭競態取走")
    if claimed.record.payload != expected:
        _publish_launch_rejected(
            spool, request_id, payload_pid,
            "launch reservation claim 後 payload 漂移")
        raise contract.ParallelContractError(
            "managed worker launch reservation claim 後 payload 漂移")
    try:
        _require_live_parent(
            artifacts,
            task_order=task,
            supervisor_session=session,
            supervisor_generation=generation,
            attempt=attempt,
        )
    except contract.ParallelContractError as exc:
        _publish_launch_rejected(spool, request_id, payload_pid, str(exc))
        raise
    try:
        spool.publish_response(request_id, {
            "schema": 1,
            "request_id": request_id,
            "status": "authorized",
            "pid": payload_pid,
            "supervisor_session": session,
            "supervisor_generation": generation,
            "attempt": attempt,
        })
    except parallel_spool.SpoolError as exc:
        raise contract.ParallelContractError(
            f"managed worker launch authorized marker 無法落盤：{exc}") from exc
    return artifacts


def authorize_launch(
    workspace_root: Path,
    workspace_name: str,
    repo: Path,
    launch: ManagedWorkerLaunch,
    *,
    import_plan: str = "",
    runtime_config: dict | None = None,
):
    """Bind hidden worker argv to immutable artifacts and one live parent.

    Hidden flags are not authority by themselves.  Before startup validation or
    any repository mutation, a worker must prove that its exact workspace,
    worktree, refs, hashes, gate command, and opaque dispatch token came from a
    currently locked parallel-supervisor base workspace.
    """
    launch = _require_launch(launch, resume=launch.resume)
    from engine import parallel_state

    try:
        run_dir = parallel_state.derive_run_directory(
            workspace_root, launch.parent_workspace, launch.run_id)
        artifacts = parallel_state.validate_run_artifacts(
            run_dir, workspace_root=workspace_root)
        assignment = artifacts.assignments[launch.assigned_order]
        token = parallel_state.read_dispatch_token(
            run_dir, launch.assigned_order,
            expected_hash=assignment["dispatch_token_hash"],
        )
    except (KeyError, OSError, ValueError, parallel_state.ParallelStateError) as exc:
        raise contract.ParallelContractError(
            f"managed worker immutable authority 無法驗證：{exc}") from exc

    exact = {
        "run_id": launch.run_id,
        "parent_workspace": launch.parent_workspace,
        "assigned_order": launch.assigned_order,
        "integration_ref": launch.integration_ref,
        "task_ref": launch.task_ref,
        "gate_command": launch.complete_gate_cmd,
        "run_config_hash": launch.run_config_hash,
    }
    for field, expected in exact.items():
        if assignment.get(field) != expected:
            raise contract.ParallelContractError(
                f"managed worker assignment {field} 與 argv 不一致")
    if artifacts.assignment_hashes[launch.assigned_order] != launch.launch_spec_hash:
        raise contract.ParallelContractError("managed worker launch_spec_hash 不符 immutable assignment")
    if artifacts.manifest_hash != launch.manifest_hash:
        raise contract.ParallelContractError("managed worker manifest_hash 不符 immutable manifest")
    if token != launch.dispatch_token:
        raise contract.ParallelContractError("managed worker dispatch token 不符 immutable assignment")
    if assignment.get("worker_workspace") != workspace_name:
        raise contract.ParallelContractError("managed worker workspace 名稱不符 immutable assignment")
    if Path(assignment.get("worker_repo", "")).resolve() != Path(repo).resolve():
        raise contract.ParallelContractError("managed worker repo 不符 immutable worktree")
    expected_workspace_path = Path(workspace_root) / workspace_name
    if Path(assignment.get("worker_workspace_path", "")) != expected_workspace_path:
        raise contract.ParallelContractError("managed worker workspace path 不符 immutable assignment")
    if launch.resume:
        if import_plan:
            raise contract.ParallelContractError("managed worker resume 不可重新匯入 plan")
    else:
        try:
            plan_path = Path(import_plan).expanduser().resolve(strict=True)
            expected_plan = (artifacts.run_dir / "plan.json").resolve(strict=True)
        except OSError as exc:
            raise contract.ParallelContractError(
                f"managed worker plan authority 無法驗證：{exc}") from exc
        if plan_path != expected_plan:
            raise contract.ParallelContractError(
                "managed worker import plan 不符 immutable run plan")
    if not isinstance(runtime_config, dict):
        raise contract.ParallelContractError("managed worker 缺少 canonical runtime config")
    expected_runtime = {
        key: artifacts.run_config[key]
        for key in (
            "flag_threshold", "done_threshold", "red_limit", "stall_limit",
            "stuck_stop", "stuck_stop_count", "round_timeout",
            "agent_backoff_max", "validate_timeout", "agent_cmd",
            "validate_cmd", "goal", "plan_doc",
        )
    }
    expected_runtime.update({
        "max_rounds": 0,
        "pause_after_plan": False,
        "allow_serial_stack": False,
        "notify_cmd": "",
        "repo": str(Path(assignment["worker_repo"]).resolve()),
    })
    if runtime_config != expected_runtime:
        raise contract.ParallelContractError(
            "managed worker runtime argv 不符 immutable run config")

    from engine import parallel_child
    from engine import parallel_spool

    expected_reservation = _expected_launch_reservation(
        artifacts,
        task=launch.assigned_order,
        request_id=launch.dispatch_request_id,
        supervisor_session=launch.supervisor_session,
        supervisor_generation=launch.supervisor_generation,
        attempt=launch.dispatch_attempt,
        resume=launch.resume,
    )
    _require_live_parent(
        artifacts,
        task_order=launch.assigned_order,
        supervisor_session=launch.supervisor_session,
        supervisor_generation=launch.supervisor_generation,
        attempt=launch.dispatch_attempt,
    )
    launch_spool = _launch_spool(artifacts)
    try:
        record = launch_spool.get_request(launch.dispatch_request_id)
        response = launch_spool.get_response(launch.dispatch_request_id)
        child = parallel_child.read_child_record(
            artifacts.run_dir, launch.assigned_order,
            launch.dispatch_request_id)
    except (OSError, ValueError, parallel_spool.SpoolError,
            parallel_child.ParallelChildError) as exc:
        raise contract.ParallelContractError(
            f"managed worker guardian authorization 無法驗證：{exc}") from exc
    if (record is None or record.state != "claimed"
            or record.payload != expected_reservation):
        state = None if record is None else record.state
        raise contract.ParallelContractError(
            f"managed worker launch 缺少 guardian claimed authority（state={state!r}）")
    payload_pid = child.get("payload_pid")
    expected_child = {
        "state": "acked",
        "run_id": launch.run_id,
        "task": launch.assigned_order,
        "child_id": launch.dispatch_request_id,
        "supervisor_session": launch.supervisor_session,
        "supervisor_generation": launch.supervisor_generation,
        "attempt": launch.dispatch_attempt,
        "resume": launch.resume,
    }
    if (any(child.get(field) != value for field, value in expected_child.items())
            or not isinstance(payload_pid, int) or isinstance(payload_pid, bool)
            or payload_pid < 2):
        raise contract.ParallelContractError(
            "managed worker durable ACKed child authority 不符")
    if compat.IS_WINDOWS:
        identity_matches = (
            os.getppid() == payload_pid
            and compat.process_matches_identity(
                payload_pid, child["payload_start_token"]))
    else:
        identity_matches = (
            os.getpid() == payload_pid
            and compat.process_matches_identity(
                payload_pid, child["payload_start_token"]))
    if not identity_matches:
        raise contract.ParallelContractError(
            "managed worker process 不符合 guardian durable payload identity")
    expected_response = {
        "schema": 1,
        "request_id": launch.dispatch_request_id,
        "status": "authorized",
        "pid": payload_pid,
        "supervisor_session": launch.supervisor_session,
        "supervisor_generation": launch.supervisor_generation,
        "attempt": launch.dispatch_attempt,
    }
    if response is None or response.payload != expected_response:
        raise contract.ParallelContractError(
            "managed worker 缺少 exact guardian authorized response")
    _require_live_parent(
        artifacts,
        task_order=launch.assigned_order,
        supervisor_session=launch.supervisor_session,
        supervisor_generation=launch.supervisor_generation,
        attempt=launch.dispatch_attempt,
    )
    return artifacts


def _assigned_task(state: dict, assigned_order: int) -> dict:
    plan = state.get("plan")
    if not isinstance(plan, list):
        raise contract.ParallelContractError("worker state plan 必須是 array")
    matches = [
        task for task in plan
        if (isinstance(task, dict)
            and isinstance(task.get("order"), int)
            and not isinstance(task.get("order"), bool)
            and task.get("order") == assigned_order)
    ]
    if len(matches) != 1:
        raise contract.ParallelContractError(
            f"assigned_order {assigned_order} 必須在 plan 中唯一存在")
    task = matches[0]
    if not isinstance(task.get("task"), str) or not task["task"].strip():
        raise contract.ParallelContractError(
            f"assigned_order {assigned_order} 缺少非空 task 描述")
    return task


def _require_launch(launch: object, *, resume: bool) -> ManagedWorkerLaunch:
    if not isinstance(launch, ManagedWorkerLaunch) or launch.resume is not resume:
        mode = "resume" if resume else "initial"
        raise contract.ParallelContractError(f"需要已驗證的 {mode} managed-worker launch")
    return launch


def initialize_state(state: dict, launch: ManagedWorkerLaunch) -> dict:
    """Return a new initial worker state without mutating the caller's state."""
    launch = _require_launch(launch, resume=False)
    if not isinstance(state, dict):
        raise contract.ParallelContractError("worker state 必須是 object")
    _assigned_task(state, launch.assigned_order)
    initialized = copy.deepcopy(state)
    initialized.update({
        "runner": WORKER_RUNNER,
        "managed_readonly": True,
        "parent_workspace": launch.parent_workspace,
        "run_id": launch.run_id,
        "assigned_order": launch.assigned_order,
        "current_order": launch.assigned_order,
        "stop_after_task": launch.stop_after_task,
        "complete_gate_cmd": launch.complete_gate_cmd,
        "integration_ref": launch.integration_ref,
        "task_ref": launch.task_ref,
        "run_config_hash": launch.run_config_hash,
        "launch_spec_hash": launch.launch_spec_hash,
        "manifest_hash": launch.manifest_hash,
        "assignment": {
            "status": "running",
            "validated_sha": None,
            "validated_round": None,
            "exit_reason": None,
            "pause_generation": 0,
            "gate_request": None,
        },
        "phase": "exec",
    })
    return initialized


def _require_exact(state: dict, field: str, expected) -> None:
    if field not in state or state[field] != expected:
        raise contract.ParallelContractError(
            f"worker state {field} drift；預期 {expected!r}，收到 {state.get(field)!r}")


def validate_resume_state(state: dict, launch: ManagedWorkerLaunch) -> None:
    """Fail closed unless persisted worker identity exactly matches resume argv."""
    launch = _require_launch(launch, resume=True)
    if not isinstance(state, dict):
        raise contract.ParallelContractError("worker state 必須是 object")
    _assigned_task(state, launch.assigned_order)
    if (not isinstance(state.get("assigned_order"), int)
            or isinstance(state.get("assigned_order"), bool)):
        raise contract.ParallelContractError("worker state assigned_order 必須是 int")
    if (not isinstance(state.get("current_order"), int)
            or isinstance(state.get("current_order"), bool)):
        raise contract.ParallelContractError("worker state current_order 必須是 int")
    expected = {
        "runner": WORKER_RUNNER,
        "managed_readonly": True,
        "parent_workspace": launch.parent_workspace,
        "run_id": launch.run_id,
        "assigned_order": launch.assigned_order,
        "current_order": launch.assigned_order,
        "stop_after_task": True,
        "complete_gate_cmd": launch.complete_gate_cmd,
        "integration_ref": launch.integration_ref,
        "task_ref": launch.task_ref,
        "run_config_hash": launch.run_config_hash,
        "launch_spec_hash": launch.launch_spec_hash,
        "manifest_hash": launch.manifest_hash,
        "phase": "exec",
    }
    for field, value in expected.items():
        if field in {"managed_readonly", "stop_after_task"}:
            if state.get(field) is not value:
                raise contract.ParallelContractError(f"worker state {field} 必須是 true")
        else:
            _require_exact(state, field, value)

    assignment = state.get("assignment")
    if not isinstance(assignment, dict) or not ASSIGNMENT_FIELDS.issubset(assignment):
        raise contract.ParallelContractError("worker state assignment schema 不完整")
    status = assignment["status"]
    idle_running = (
        status == "running"
        and assignment["exit_reason"] is None
        and assignment.get("gate_request") is None
        and assignment.get("validated_sha") is None
        and assignment.get("validated_round") is None
    )
    resumable_paused = (
        status == "paused"
        and isinstance(assignment.get("exit_reason"), str)
        and bool(assignment["exit_reason"].strip())
        and assignment.get("gate_request") is None
        and ((assignment.get("validated_sha") is None
              and assignment.get("validated_round") is None)
             or (assignment.get("validated_sha") is not None
                 and assignment.get("validated_round") is not None))
    )
    if not (idle_running or resumable_paused):
        raise contract.ParallelContractError(
            "managed worker resume 只接受 idle running 或 supervisor-paused state")
    pause_generation = assignment["pause_generation"]
    if (not isinstance(pause_generation, int) or isinstance(pause_generation, bool)
            or pause_generation < 0):
        raise contract.ParallelContractError("assignment.pause_generation 必須是非負整數")
    validated_round = assignment["validated_round"]
    if (validated_round is not None
            and (not isinstance(validated_round, int)
                 or isinstance(validated_round, bool) or validated_round < 1)):
        raise contract.ParallelContractError("assignment.validated_round 必須是正整數或 null")
    validated_sha = assignment["validated_sha"]
    if validated_sha is not None:
        contract.require_git_sha(validated_sha)


def prepare_resume_state(state: dict, launch: ManagedWorkerLaunch) -> dict:
    """Validate an explicitly authorized resume and consume paused metadata."""
    validate_resume_state(state, launch)
    resumed = copy.deepcopy(state)
    assignment = resumed["assignment"]
    if assignment["status"] == "paused":
        assignment.update({
            "status": "running",
            "validated_sha": None,
            "validated_round": None,
            "exit_reason": None,
            "gate_request": None,
        })
        resumed["done_count"] = 0
        resumed.setdefault("notes", []).append(
            "▶ parent supervisor 已明確 Resume；清除 paused gate snapshot 後重新完整收斂。")
    return resumed


def mark_supervisor_paused(
    state: dict,
    *,
    pause_generation: int,
    reason: str | None = None,
) -> dict:
    """Persist a managed worker's graceful supervisor Pause boundary.

    The worker calls this only after atomically claiming its exact-session
    stop-after-round marker.  Keeping the generation in the worker checkpoint
    lets owner-loss recovery distinguish a completed graceful stop from an
    abrupt crash without PID inference.
    """
    validate_persisted_state(state)
    if (not isinstance(pause_generation, int)
            or isinstance(pause_generation, bool)
            or pause_generation < 0):
        raise contract.ParallelContractError(
            "pause_generation 必須是非負整數")
    paused = copy.deepcopy(state)
    assignment = paused["assignment"]
    current_generation = assignment["pause_generation"]
    if pause_generation < current_generation:
        raise contract.ParallelContractError(
            "pause_generation 不可倒退")
    if assignment["status"] not in {"running", "paused"}:
        raise contract.ParallelContractError(
            "只有 idle running/paused managed worker 可完成平順 Pause")
    if assignment.get("gate_request") is not None:
        raise contract.ParallelContractError(
            "保留 gate_request 的 worker 不可投影為 paused")
    assignment.update({
        "status": "paused",
        "pause_generation": pause_generation,
        "exit_reason": (
            reason or f"supervisor pause generation {pause_generation}"),
    })
    validate_persisted_state(paused)
    return paused


def mark_supervisor_cancelled(
    state: dict,
    *,
    reason: str = "parent supervisor requested Abort",
) -> dict:
    """Persist a graceful managed-worker Abort boundary."""
    validate_persisted_state(state)
    cancelled = copy.deepcopy(state)
    assignment = cancelled["assignment"]
    if assignment["status"] not in {"running", "paused", "cancelled"}:
        raise contract.ParallelContractError(
            "只有 idle running/paused managed worker 可完成平順 Abort")
    if assignment.get("gate_request") is not None:
        raise contract.ParallelContractError(
            "保留 gate_request 的 worker 不可投影為 cancelled")
    assignment.update({
        "status": "cancelled",
        "exit_reason": reason,
    })
    validate_persisted_state(cancelled)
    return cancelled


def resolve_recovered_stale_gate(
    state: dict, *, request_id: str, validated_sha: str,
    validated_round: int,
) -> dict:
    """Parent-only projection after journal recovery proves a claimed gate stale.

    The caller must already have fenced/reaped the worker.  Exact retained gate
    identity is required so recovery cannot clear an unrelated completion vote.
    """
    validate_persisted_state(state)
    assignment = state["assignment"]
    expected = {
        "request_id": request_id,
        "validated_sha": validated_sha,
        "validated_round": validated_round,
    }
    if (assignment.get("status") not in {"running", "recovery-required"}
            or assignment.get("gate_request") != expected
            or assignment.get("validated_sha") != validated_sha
            or assignment.get("validated_round") != validated_round):
        raise contract.ParallelContractError(
            "stale gate recovery 不符合 retained worker gate identity")
    resolved = copy.deepcopy(state)
    resolved_assignment = resolved["assignment"]
    resolved_assignment.update({
        "status": "running",
        "validated_sha": None,
        "validated_round": None,
        "exit_reason": None,
        "gate_request": None,
    })
    resolved["done_count"] = 0
    resolved.setdefault("notes", []).append(
        "↪ parent 已完成 claimed gate journal recovery；結果為 stale，Resume 後重新同步與 Validate。")
    validate_persisted_state(resolved)
    return resolved


def resolve_recovered_integrated_gate(
    state: dict, *, request_id: str, validated_sha: str,
    validated_round: int,
) -> dict:
    """Project exact receipt authority into a fenced worker checkpoint.

    A supervisor may crash after the gate transaction commits but before the
    worker consumes its success response.  Once the replacement supervisor has
    verified the journal/receipt and reaped the old child, it may finish the
    same state projection without inventing a new request.
    """
    validate_persisted_state(state)
    assignment = state["assignment"]
    expected = {
        "request_id": request_id,
        "validated_sha": validated_sha,
        "validated_round": validated_round,
    }
    if (assignment.get("status") not in {"running", "recovery-required"}
            or assignment.get("gate_request") != expected
            or assignment.get("validated_sha") != validated_sha
            or assignment.get("validated_round") != validated_round):
        raise contract.ParallelContractError(
            "integrated gate recovery 不符合 retained worker gate identity")
    resolved = copy.deepcopy(state)
    resolved["assignment"].update({
        "status": "integrated",
        "validated_sha": validated_sha,
        "validated_round": validated_round,
        "exit_reason": None,
        "gate_request": None,
    })
    resolved["done_count"] = 0
    resolved.setdefault("notes", []).append(
        "✅ parent 已由 canonical receipt 完成 claimed gate 的 integrated 投影。")
    validate_persisted_state(resolved)
    return resolved


def validate_persisted_state(state: dict) -> None:
    """Validate the self-contained managed-worker shape at every state load."""
    if not isinstance(state, dict):
        raise contract.ParallelContractError("worker state 必須是 object")
    run_id = contract.require_run_id(state.get("run_id"))
    order = state.get("assigned_order")
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        raise contract.ParallelContractError("worker state assigned_order 必須是正整數")
    _assigned_task(state, order)
    expected = {
        "runner": WORKER_RUNNER,
        "managed_readonly": True,
        "current_order": order,
        "stop_after_task": True,
        "integration_ref": contract.integration_ref_for(run_id),
        "task_ref": task_ref_for(run_id, order),
        "phase": "exec",
    }
    for field, value in expected.items():
        if field in {"managed_readonly", "stop_after_task"}:
            if state.get(field) is not value:
                raise contract.ParallelContractError(f"worker state {field} 必須是 true")
        else:
            _require_exact(state, field, value)
    if not _valid_workspace_name(state.get("parent_workspace")):
        raise contract.ParallelContractError("worker state parent_workspace 名稱不合法")
    gate_cmd = state.get("complete_gate_cmd")
    if not isinstance(gate_cmd, str) or not gate_cmd.strip() or "\x00" in gate_cmd:
        raise contract.ParallelContractError("worker state complete_gate_cmd 不合法")
    for field in ("run_config_hash", "launch_spec_hash", "manifest_hash"):
        contract.require_config_hash(state.get(field), field)

    assignment = state.get("assignment")
    if not isinstance(assignment, dict) or set(assignment) != ASSIGNMENT_FIELDS:
        raise contract.ParallelContractError("worker state assignment schema 不完整")
    status = assignment.get("status")
    if status not in ASSIGNMENT_STATUSES:
        raise contract.ParallelContractError(f"assignment.status 不合法:{status!r}")
    pause_generation = assignment.get("pause_generation")
    if (not isinstance(pause_generation, int) or isinstance(pause_generation, bool)
            or pause_generation < 0):
        raise contract.ParallelContractError("assignment.pause_generation 必須是非負整數")
    validated_round = assignment.get("validated_round")
    if (validated_round is not None
            and (not isinstance(validated_round, int)
                 or isinstance(validated_round, bool) or validated_round < 1)):
        raise contract.ParallelContractError("assignment.validated_round 必須是正整數或 null")
    validated_sha = assignment.get("validated_sha")
    if validated_sha is not None:
        contract.require_git_sha(validated_sha)
    if (validated_sha is None) != (validated_round is None):
        raise contract.ParallelContractError(
            "assignment validated_sha/validated_round 必須同時存在或同時為 null")
    if status == "integrated" and validated_sha is None:
        raise contract.ParallelContractError("integrated assignment 必須保存 validated snapshot")
    exit_reason = assignment.get("exit_reason")
    if exit_reason is not None and (
            not isinstance(exit_reason, str) or not exit_reason.strip()):
        raise contract.ParallelContractError("assignment.exit_reason 必須是非空字串或 null")
    if status in {"paused", "cancelled", "blocked", "recovery-required"} \
            and exit_reason is None:
        raise contract.ParallelContractError(f"{status} assignment 必須保存 exit_reason")
    gate_request = assignment.get("gate_request")
    if (status == "running" and gate_request is None
            and (validated_sha is not None or validated_round is not None)):
        raise contract.ParallelContractError(
            "idle running assignment 的 validated/gate 欄位必須全為 null")
    if gate_request is not None:
        if (not isinstance(gate_request, dict)
                or set(gate_request) != {"request_id", "validated_sha", "validated_round"}
                or not isinstance(gate_request.get("request_id"), str)
                or re.fullmatch(r"[0-9a-f]{32}", gate_request["request_id"]) is None
                or gate_request.get("validated_sha") != validated_sha
                or gate_request.get("validated_round") != validated_round
                or status not in {"running", "recovery-required"}
                or (status == "running" and exit_reason is not None)
                or (status == "recovery-required" and exit_reason is None)):
            raise contract.ParallelContractError(
                "running/recovery-required assignment 的 gate_request 不一致")
