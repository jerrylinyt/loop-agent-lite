"""Pure managed-worker launch and state contracts.

This module deliberately performs no Git, filesystem, process, or workspace
mutation.  ``engine.loop`` may add these arguments to its parser and call the
helpers before it enters any side-effecting startup path.
"""

from __future__ import annotations

import argparse
import copy
import re
from dataclasses import dataclass

from engine import parallel_contract as contract


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
        "run_config_hash", "launch_spec_hash", "manifest_hash",
    )
    return (getattr(args, "start_task", None) is not None
            or getattr(args, "stop_after_task", False) is True
            or getattr(args, "managed_worker_resume", False) is True
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
    }
    missing = []
    for option, value in required.items():
        if option == "--stop-after-task":
            absent = value is not True
        elif option == "--start-task":
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
    )


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
    if (assignment["status"] != "running" or assignment["exit_reason"] is not None
            or assignment.get("gate_request") is not None
            or assignment.get("validated_sha") is not None
            or assignment.get("validated_round") is not None):
        raise contract.ParallelContractError(
            "managed worker resume 只接受 idle running（validated/gate/exit 欄位全為 null）")
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
