#!/usr/bin/env python3
"""Parallel supervisor for frozen ``engine.loop`` execution plans.

The supervisor intentionally does not implement another convergence engine.
It provisions one ordinary ``engine.loop`` process per assignment and owns the
small amount of durable coordination required around those workers: batching,
the completion-gate spool, serialized repository operations, and projection of
the aggregate run into the base workspace.

Most helpers in this module are pure on purpose.  CLI, Dashboard, and recovery
paths must all construct byte-for-byte equivalent worker argv and immutable run
configuration rather than growing subtly different launch implementations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from engine import loop as loop_mod
from engine import platform_compat as compat
from engine import parallel_contract
from engine import parallel_child
from engine import parallel_gate
from engine import parallel_spool
from engine import parallel_state
from engine import parallel_worker
from engine import repo_executor
from engine import repo_owner
from engine.paths import default_workspace_root
from engine.work import validate_plan


DEFAULT_MAX_PARALLEL = 2
DEFAULT_WORKER_RESTART_LIMIT = 3
LAUNCH_RESERVATION_SCHEMA = 1
SUPERVISOR_RUNNER = "parallel-supervisor"
RUN_NONTERMINAL = frozenset({
    "initializing", "running", "pause_requested", "paused",
    "cancel_requested", "finalizing", "finalizing_cancel", "blocked",
})
RUN_TERMINAL = frozenset({"completed", "cancelled"})


class ParallelError(RuntimeError):
    """A parallel run cannot safely start or continue."""


@dataclass(frozen=True)
class Batch:
    """One contiguous scheduling batch from a frozen plan."""

    number: int
    orders: tuple[int, ...]


class SupervisorRunLock:
    """Held base-workspace lock with exact session/generation authority."""

    def __init__(self, path: Path, *, session: str, generation: int):
        self.path = Path(path)
        self.session = session
        self.generation = _require_positive_integer(generation, "generation")
        if (not isinstance(session, str) or len(session) != 32
                or any(ch not in "0123456789abcdef" for ch in session)):
            raise ParallelError("supervisor lock session 必須是 32 字元小寫 hex")
        self.stream = None

    def __enter__(self):
        try:
            loop_mod.ensure_real_directory(self.path.parent, "parallel base workspace")
            fd = loop_mod._open_regular(self.path, os.O_RDWR | os.O_CREAT)
            stream = os.fdopen(fd, "a+b", closefd=True)
            compat.lock_file(stream, blocking=False)
        except (OSError, ValueError, BlockingIOError, PermissionError) as exc:
            try:
                stream.close()
            except (NameError, OSError):
                pass
            raise ParallelError(f"base workspace run lock 無法取得：{exc}") from exc
        payload = json.dumps({
            "pid": os.getpid(),
            "session_id": self.session,
            "generation": self.generation,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        except OSError:
            compat.unlock_file(stream)
            stream.close()
            raise
        self.stream = stream
        return self

    def __exit__(self, _kind, _value, _traceback):
        if self.stream is not None:
            try:
                compat.unlock_file(self.stream)
            finally:
                self.stream.close()
                self.stream = None


class BootstrapControlLock:
    """Serialize no-owner control intent publication and state changes."""

    def __init__(self, run_dir: Path):
        self.controls_dir = Path(run_dir) / "controls"
        self.path = self.controls_dir / ".bootstrap.lock"
        self.stream = None

    def __enter__(self):
        try:
            loop_mod.ensure_real_directory(
                self.controls_dir, "parallel bootstrap controls")
            fd = loop_mod._open_regular(self.path, os.O_RDWR | os.O_CREAT)
            stream = os.fdopen(fd, "a+b", closefd=True)
            compat.lock_file(stream, blocking=True)
        except (OSError, ValueError, BlockingIOError, PermissionError) as exc:
            try:
                stream.close()
            except (NameError, OSError):
                pass
            raise ParallelError(
                f"bootstrap control lock 無法取得：{exc}") from exc
        self.stream = stream
        return self

    def __exit__(self, _kind, _value, _traceback):
        if self.stream is not None:
            try:
                compat.unlock_file(self.stream)
            finally:
                self.stream.close()
                self.stream = None


def parallel_run_status(state: Mapping[str, object]) -> str | None:
    """Return a validated base parallel status, or ``None`` for another runner."""
    if state.get("runner") != SUPERVISOR_RUNNER:
        return None
    parallel = state.get("parallel")
    if not isinstance(parallel, Mapping):
        raise ParallelError("parallel-supervisor state 缺少 parallel object")
    status = parallel.get("status")
    if status not in RUN_NONTERMINAL | RUN_TERMINAL:
        raise ParallelError(f"parallel run status 不合法：{status!r}")
    return str(status)


def assert_base_mutation_allowed(state: Mapping[str, object], operation: str) -> None:
    """Fail before any ordinary mutation of a nonterminal base workspace."""
    status = parallel_run_status(state)
    if status in RUN_NONTERMINAL:
        raise ParallelError(
            f"parallel workspace 目前為 {status}；{operation} 必須改走 "
            "parallel resume/pause/abort，不可由普通 Loop 修改")


def save_aggregate(run_dir: Path, aggregate: Mapping[str, object], plan: Sequence[Mapping]) -> dict:
    """Validate and atomically checkpoint the supervisor's single-writer truth."""
    materialized = dict(aggregate)
    parallel_state.validate_aggregate(
        materialized, run_id=materialized.get("run_id"), plan=plan)
    materialized["version"] += 1
    parallel_state.validate_aggregate(
        materialized, run_id=materialized.get("run_id"), plan=plan)
    parallel_state.atomic_write_json(run_dir, "aggregate.json", materialized)
    return materialized


def load_aggregate(artifacts: parallel_state.ValidatedRunArtifacts) -> dict:
    """Read the aggregate without repairing or guessing malformed state."""
    value = parallel_state.read_canonical_json(artifacts.run_dir, "aggregate.json")
    return parallel_state.validate_aggregate(
        value, run_id=artifacts.manifest["run_id"], plan=artifacts.plan)


_SUPERVISOR_GENERATION_FIELDS = {
    "schema", "run_id", "generation", "session", "claimed_at",
}


def _read_supervisor_generation(
    artifacts: parallel_state.ValidatedRunArtifacts,
) -> dict:
    value = parallel_state.read_canonical_json(
        artifacts.run_dir, "supervisor-generation.json")
    if (not isinstance(value, dict)
            or set(value) != _SUPERVISOR_GENERATION_FIELDS
            or value.get("schema") != 1
            or value.get("run_id") != artifacts.manifest["run_id"]
            or not isinstance(value.get("generation"), int)
            or isinstance(value.get("generation"), bool)
            or value["generation"] < 1
            or not isinstance(value.get("session"), str)
            or len(value["session"]) != 32
            or any(ch not in "0123456789abcdef" for ch in value["session"])
            or not isinstance(value.get("claimed_at"), str)
            or not value["claimed_at"]):
        raise ParallelError("supervisor generation authority/schema mismatch")
    return value


def _initialize_supervisor_generation(
    artifacts: parallel_state.ValidatedRunArtifacts,
    *, session: str,
) -> None:
    parallel_state.write_or_verify_immutable_json(
        artifacts.run_dir, "supervisor-generation.json", {
            "schema": 1,
            "run_id": artifacts.manifest["run_id"],
            "generation": 1,
            "session": session,
            "claimed_at": datetime.now().astimezone().isoformat(
                timespec="microseconds"),
        })


def _claim_supervisor_generation(
    artifacts: parallel_state.ValidatedRunArtifacts,
    *, expected_generation: int, generation: int, session: str,
) -> None:
    current = _read_supervisor_generation(artifacts)
    if (current["generation"] != expected_generation
            or generation != expected_generation + 1):
        raise ParallelError(
            "supervisor generation changed before durable recovery claim")
    parallel_state.atomic_write_json(
        artifacts.run_dir, "supervisor-generation.json", {
            "schema": 1,
            "run_id": artifacts.manifest["run_id"],
            "generation": generation,
            "session": session,
            "claimed_at": datetime.now().astimezone().isoformat(
                timespec="microseconds"),
        })


def project_base_state(
    workspace: loop_mod.Workspace,
    artifacts: parallel_state.ValidatedRunArtifacts,
    aggregate: Mapping[str, object],
    receipts: Sequence[Mapping[str, object]],
    *,
    supervisor_pid: int | None,
    supervisor_session: str | None,
    supervisor_generation: int,
    worker_rounds: Mapping[int, int] | None = None,
) -> dict:
    """Project durable parallel truth into the existing Dashboard state schema."""
    aggregate = parallel_state.validate_aggregate(
        dict(aggregate), run_id=artifacts.manifest["run_id"], plan=artifacts.plan)
    generation = _require_positive_integer(
        supervisor_generation, "supervisor_generation")
    if supervisor_pid is not None and (
            not isinstance(supervisor_session, str) or not supervisor_session):
        raise ParallelError("active supervisor projection 需要 session")
    receipt_chain = parallel_state.validate_receipt_chain(receipts, artifacts)
    completed = parallel_state.project_completed_from_receipts(receipt_chain, artifacts)
    current_tip = (receipt_chain[-1]["validated_sha"] if receipt_chain
                   else artifacts.manifest["integration_start_sha"])
    pending = [
        task for task in aggregate["tasks"]
        if task["outcome"] == "pending"
        and (aggregate["batch"] is None or task["batch"] == aggregate["batch"])
    ]
    current_order = min((task["order"] for task in pending), default=None)
    state = workspace.fresh_state()
    state.update({
        "runner": SUPERVISOR_RUNNER,
        "phase": "done" if aggregate["status"] == "completed" else "exec",
        "plan": [dict(task) for task in artifacts.plan],
        "plan_version": 1,
        "completed": completed,
        "current_order": current_order,
        "current_task_base_sha": current_tip if current_order is not None else None,
        "last_green_sha": current_tip,
        "round": max((worker_rounds or {}).values(), default=0),
        "done_count": 0,
        "repo_binding": artifacts.run_config["primary_repo"],
        "config": {
            key: artifacts.run_config[key]
            for key in (
                "repo", "goal", "plan_doc", "agent_cmd", "validate_cmd",
                "flag_threshold", "done_threshold", "red_limit", "stall_limit",
                "stuck_stop", "stuck_stop_count", "round_timeout",
                "agent_backoff_max", "validate_timeout", "notify_cmd",
            )
        },
        "loop": ({
            "pid": supervisor_pid,
            "session_id": supervisor_session,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        } if supervisor_pid is not None and supervisor_session else {"pid": None}),
        "parallel": {
            "run_id": artifacts.manifest["run_id"],
            "manifest_hash": artifacts.manifest_hash,
            "aggregate_version": aggregate["version"],
            "control_generation": aggregate["control_generation"],
            # Retain the last generation while quiesced; a new owner increments
            # it before publishing any fresh one-shot launch reservation.
            "supervisor_generation": generation,
            "status": aggregate["status"],
            "terminal_intent": aggregate["terminal_intent"],
            "pause_generation": aggregate["pause_generation"],
            "batch": aggregate["batch"],
            "tasks": [dict(task) for task in aggregate["tasks"]],
            "error": aggregate["error"],
        },
    })
    loop_mod.validate_state_shape(state, "parallel base projection")
    return state


def normalize_expected_plan_sha256(value: object) -> str | None:
    """Validate the optional raw-byte digest carried by a plan handoff."""
    if value is None:
        return None
    if (not isinstance(value, str) or len(value) != 64
            or any(ch not in "0123456789abcdef" for ch in value)):
        raise ParallelError(
            "expected plan SHA-256 必須是 64 字元小寫十六進位字串")
    return value


def load_frozen_plan(
        path: Path, *, expected_raw_sha256: str | None = None) -> list[dict]:
    """Safely read once, bind raw bytes when requested, then validate JSON."""
    expected = normalize_expected_plan_sha256(expected_raw_sha256)
    try:
        fd = loop_mod._open_regular(
            Path(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb", closefd=True) as stream:
            raw = stream.read()
    except (OSError, ValueError) as exc:
        raise ParallelError(f"無法讀取 frozen plan：{exc}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if expected is not None and actual != expected:
        raise ParallelError(
            "frozen plan raw SHA-256 mismatch："
            f"expected={expected} actual={actual}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ParallelError(f"無法解析 frozen plan JSON：{exc}") from exc
    normalized, errors = validate_plan(value)
    if errors:
        raise ParallelError("plan.json 校驗未過：\n- " + "\n- ".join(errors))
    assert normalized is not None
    return normalized


def partition_batches(plan: Sequence[Mapping]) -> tuple[Batch, ...]:
    """Map contiguous ``stack`` groups to batches without inventing a DAG.

    Tasks without ``stack`` remain serial one-item batches.  Consecutive tasks
    carrying the same positive stack value share a batch.  ``validate_plan``
    already rejects a stack value that reappears after its group was closed;
    this helper validates again because manifests and resume artifacts are
    independent trust boundaries.
    """
    normalized, errors = validate_plan(list(plan))
    if errors:
        raise ParallelError("frozen plan 不合法：" + "；".join(errors))
    assert normalized is not None
    batches: list[Batch] = []
    current_key: tuple[str, int] | None = None
    current_orders: list[int] = []
    for task in normalized:
        order = task["order"]
        key = (("stack", task["stack"]) if "stack" in task
               else ("serial", order))
        if current_key is not None and key != current_key:
            batches.append(Batch(len(batches) + 1, tuple(current_orders)))
            current_orders = []
        current_key = key
        current_orders.append(order)
    if current_orders:
        batches.append(Batch(len(batches) + 1, tuple(current_orders)))
    return tuple(batches)


def _require_positive_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ParallelError(f"{label} 必須是正整數")
    return value


def _require_finite_number(value: object, label: str, *, positive=False) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or float(value) < 0
            or (positive and float(value) == 0)):
        rule = "> 0" if positive else "≥ 0"
        raise ParallelError(f"{label} 必須是有限數字且 {rule}")
    return float(value)


def canonical_run_config(values: Mapping[str, object]) -> dict:
    """Return the immutable, non-secret configuration shared by all workers."""
    required_strings = ("repo", "goal", "agent_cmd", "validate_cmd")
    config: dict[str, object] = {}
    for key in required_strings:
        value = values.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ParallelError(f"run config {key} 必須是非空字串")
        config[key] = value.strip()
    plan_doc = values.get("plan_doc", "")
    notify_cmd = values.get("notify_cmd", "")
    for key, value in (("plan_doc", plan_doc), ("notify_cmd", notify_cmd)):
        if not isinstance(value, str):
            raise ParallelError(f"run config {key} 必須是字串")
        config[key] = value.strip()
    for key in ("flag_threshold", "done_threshold", "red_limit", "stall_limit",
                "stuck_stop_count", "max_parallel", "worker_restart_limit"):
        config[key] = _require_positive_integer(values.get(key), key)
    for key, positive in (("round_timeout", False), ("agent_backoff_max", False),
                          ("validate_timeout", True)):
        config[key] = _require_finite_number(values.get(key), key, positive=positive)
    stuck_stop = values.get("stuck_stop", False)
    if not isinstance(stuck_stop, bool):
        raise ParallelError("run config stuck_stop 必須是 boolean")
    config["stuck_stop"] = stuck_stop
    config["repo"] = str(Path(str(config["repo"])).expanduser().resolve())
    environment = values.get("environment")
    if isinstance(environment, Mapping):
        environment = dict(environment)
        additions = environment.get("path_additions")
        if isinstance(additions, list):
            environment["path_additions"] = [
                str(Path(os.path.expandvars(item)).expanduser().resolve())
                if isinstance(item, str) and item and "\x00" not in item else item
                for item in additions
            ]
    try:
        config["environment"] = parallel_state.normalize_environment_contract(
            environment)
    except parallel_state.ParallelStateError as exc:
        raise ParallelError(str(exc)) from exc
    return config


def missing_required_secret_names(
        environment: Mapping[str, object], *,
        ambient: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return required secret names whose current ambient value is empty."""
    try:
        contract = parallel_state.normalize_environment_contract(environment)
    except parallel_state.ParallelStateError as exc:
        raise ParallelError(str(exc)) from exc
    source = os.environ if ambient is None else ambient
    ambient_by_name = {
        str(key).upper(): value for key, value in source.items()
        if isinstance(key, str)
    }
    return tuple(
        name for name in contract["required_secret_names"]
        if (not isinstance(ambient_by_name.get(name.upper()), str)
            or not ambient_by_name[name.upper()])
    )


def require_required_secrets(
        environment: Mapping[str, object], *,
        ambient: Mapping[str, str] | None = None) -> None:
    """Fail before mutation when an immutable secret presence rule is unmet."""
    missing = missing_required_secret_names(environment, ambient=ambient)
    if missing:
        raise ParallelError(
            "resume/start 缺少必要 secret 環境變數：" + ", ".join(missing))


def build_worker_environment(
        environment: Mapping[str, object], *, workspace_root: Path,
        ambient: Mapping[str, str] | None = None) -> dict[str, str]:
    """Apply one immutable environment contract to a worker process.

    The durable contract never contains a secret value or a complete inherited
    environment.  Required secret values are read from the current supervisor
    process by name, while explicit non-secret values and PATH additions always
    come from ``run-config.json`` on both initial launch and recovery.
    """
    try:
        contract = parallel_state.normalize_environment_contract(environment)
    except parallel_state.ParallelStateError as exc:
        raise ParallelError(str(exc)) from exc
    source = os.environ if ambient is None else ambient
    require_required_secrets(contract, ambient=source)
    inherited = loop_mod.expose_project_package(dict(source))
    inherited["LOOP_AGENT_WORKSPACE_ROOT"] = str(Path(workspace_root).resolve())
    ambient_by_name = {
        str(key).upper(): value for key, value in source.items()
        if isinstance(key, str)
    }
    for name in contract["required_secret_names"]:
        value = ambient_by_name.get(name.upper())
        inherited[name] = value
    for key, value in contract["non_secret"].items():
        if value is None:
            inherited[key] = ""
        elif isinstance(value, bool):
            inherited[key] = "true" if value else "false"
        else:
            inherited[key] = str(value)
    existing = [
        value for value in inherited.get("PATH", "").split(os.pathsep)
        if value
    ]
    ordered_paths: list[str] = []
    seen_paths: set[str] = set()
    for value in [*contract["path_additions"], *existing]:
        identity = os.path.normcase(os.path.normpath(value))
        if identity not in seen_paths:
            seen_paths.add(identity)
            ordered_paths.append(value)
    inherited["PATH"] = os.pathsep.join(ordered_paths)
    inherited.setdefault("PYTHONUTF8", "1")
    return inherited


def publish_launch_reservation(
    artifacts: parallel_state.ValidatedRunArtifacts,
    order: int,
    *,
    supervisor_session: str,
    supervisor_generation: int,
    attempt: int,
    resume: bool,
    request_id: str | None = None,
) -> dict:
    """Publish one one-shot worker launch authority.

    The immutable dispatch token identifies the assignment, but it does not
    mean that a queued task is dispatchable forever.  This pending spool entry
    is the dynamic authority.  The worker must atomically claim it before any
    agent/validator/repository payload can start; Pause/Abort race by cancelling
    the same entry.
    """
    if not isinstance(artifacts, parallel_state.ValidatedRunArtifacts):
        raise ParallelError("launch reservation 需要 validated run artifacts")
    order = _require_positive_integer(order, "launch task")
    if order not in artifacts.assignments:
        raise ParallelError(f"launch task-{order} 不在 immutable assignments")
    if (not isinstance(supervisor_session, str)
            or len(supervisor_session) != 32
            or any(ch not in "0123456789abcdef" for ch in supervisor_session)):
        raise ParallelError("supervisor_session 必須是 32 字元小寫 hex")
    generation = _require_positive_integer(
        supervisor_generation, "supervisor_generation")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        raise ParallelError("launch attempt 必須是非負整數")
    if not isinstance(resume, bool):
        raise ParallelError("launch reservation resume 必須是 boolean")
    derived_request_id = parallel_state.canonical_json_hash({
        "kind": "worker-launch",
        "run_id": artifacts.manifest["run_id"],
        "task": order,
        "supervisor_session": supervisor_session,
        "supervisor_generation": generation,
        "attempt": attempt,
    })[:32]
    if request_id is not None and request_id != derived_request_id:
        raise ParallelError("launch request_id 必須由 session/generation/attempt 唯一推導")
    request_id = derived_request_id
    try:
        request_id = parallel_spool.require_request_id(request_id)
    except parallel_spool.InvalidRequestId as exc:
        raise ParallelError(str(exc)) from exc
    payload = {
        "schema": LAUNCH_RESERVATION_SCHEMA,
        "request_id": request_id,
        "run_id": artifacts.manifest["run_id"],
        "task": order,
        "manifest_hash": artifacts.manifest_hash,
        "run_config_hash": artifacts.run_config_hash,
        "launch_spec_hash": artifacts.assignment_hashes[order],
        "supervisor_session": supervisor_session,
        "supervisor_generation": generation,
        "attempt": attempt,
        "resume": resume,
    }
    launch_spool = parallel_spool.DurableSpool(artifacts.run_dir / "launches")
    try:
        launch_spool.publish_request(request_id, payload)
    except parallel_spool.DuplicateRequestError as exc:
        existing = launch_spool.get_request(request_id)
        if (existing is None or existing.state != "pending"
                or existing.payload != payload):
            raise ParallelError(
                f"worker launch attempt 已被 claim/cancel 或內容不符：{exc}") from exc
        # Crash after durable publish but before spawn: exact pending replay is
        # idempotent and reuses the same one-shot request id.
    except parallel_spool.SpoolError as exc:
        raise ParallelError(f"worker launch reservation publish 失敗：{exc}") from exc
    return payload


def build_gate_client_command(
    *, python_executable: str, run_dir: Path, wait_timeout: float,
) -> str:
    """Build the immutable gate client command with a bound inner deadline."""
    timeout = _require_finite_number(
        wait_timeout, "gate wait_timeout", positive=True)
    executable = str(python_executable).strip()
    if not executable or "\x00" in executable:
        raise ParallelError("python_executable 不合法")
    path = Path(run_dir).expanduser().resolve()
    return compat.join_command([
        executable, "-m", "engine.parallel_gate",
        "--run-dir", str(path),
        "--wait-timeout", f"{timeout:g}",
    ])


def cancel_launch_reservation(run_dir: Path, request_id: str):
    """CAS-cancel an unclaimed worker launch during Pause/Abort."""
    try:
        return parallel_spool.DurableSpool(
            Path(run_dir) / "launches").cancel_request(request_id)
    except parallel_spool.SpoolError as exc:
        raise ParallelError(f"worker launch reservation cancel 失敗：{exc}") from exc


def build_worker_argv(
    *,
    python_executable: str,
    assignment: Mapping[str, object],
    run_config: Mapping[str, object],
    plan_path: Path,
    dispatch_token: str,
    launch_reservation: Mapping[str, object],
    resume: bool = False,
) -> list[str]:
    """Build the sole canonical ``engine.loop`` worker command.

    The assignment is deliberately explicit.  Derived refs and hashes are
    validated before they become argv so a corrupted manifest cannot steer a
    worker into an arbitrary workspace, repo, or branch.
    """
    run_id = parallel_contract.require_run_id(assignment.get("run_id"))
    order = _require_positive_integer(assignment.get("assigned_order"), "assigned_order")
    expected_task_ref = parallel_worker.task_ref_for(run_id, order)
    expected_integration_ref = parallel_contract.integration_ref_for(run_id)
    expected_workspace = f"{assignment.get('parent_workspace')}--{run_id}-task-{order}"
    exact = {
        "task_ref": expected_task_ref,
        "integration_ref": expected_integration_ref,
        "worker_workspace": expected_workspace,
    }
    for field, expected in exact.items():
        if assignment.get(field) != expected:
            raise ParallelError(
                f"assignment {field} 不符 canonical identity："
                f"預期 {expected!r}，收到 {assignment.get(field)!r}")
    parent = assignment.get("parent_workspace")
    if not isinstance(parent, str) or not loop_mod.valid_workspace_name(parent):
        raise ParallelError("assignment parent_workspace 不合法")
    worker_repo = assignment.get("worker_repo")
    if not isinstance(worker_repo, str) or not worker_repo.strip():
        raise ParallelError("assignment worker_repo 必須是非空絕對路徑")
    worker_repo_path = Path(worker_repo).expanduser().resolve()
    if not worker_repo_path.is_absolute():  # defensive; resolve is absolute on supported hosts
        raise ParallelError("assignment worker_repo 必須是絕對路徑")
    gate_command = assignment.get("gate_command")
    if not isinstance(gate_command, str) or not gate_command.strip() or "\x00" in gate_command:
        raise ParallelError("assignment gate_command 不合法")
    hashes = {}
    for key in ("run_config_hash", "launch_spec_hash", "manifest_hash"):
        hashes[key] = parallel_contract.require_config_hash(assignment.get(key), key)
    token_hash = parallel_contract.require_config_hash(
        assignment.get("dispatch_token_hash"), "dispatch_token_hash")
    if parallel_state.dispatch_token_hash(dispatch_token) != token_hash:
        raise ParallelError("dispatch token 不符合 immutable assignment")
    if not isinstance(launch_reservation, Mapping):
        raise ParallelError("launch_reservation 必須是 object")
    attempt = launch_reservation.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        raise ParallelError("launch_reservation attempt 必須是非負整數")
    expected_reservation = {
        "schema": LAUNCH_RESERVATION_SCHEMA,
        "run_id": run_id,
        "task": order,
        "manifest_hash": hashes["manifest_hash"],
        "run_config_hash": hashes["run_config_hash"],
        "launch_spec_hash": hashes["launch_spec_hash"],
        "attempt": attempt,
        "resume": resume,
    }
    reservation = dict(launch_reservation)
    if set(reservation) != {
        *expected_reservation, "request_id", "supervisor_session",
        "supervisor_generation",
    }:
        raise ParallelError("launch_reservation schema 不符")
    for field, expected in expected_reservation.items():
        if reservation.get(field) != expected:
            raise ParallelError(f"launch_reservation {field} 不符合 assignment")
    try:
        request_id = parallel_spool.require_request_id(reservation["request_id"])
    except parallel_spool.InvalidRequestId as exc:
        raise ParallelError(str(exc)) from exc
    supervisor_session = reservation["supervisor_session"]
    if (not isinstance(supervisor_session, str)
            or len(supervisor_session) != 32
            or any(ch not in "0123456789abcdef" for ch in supervisor_session)):
        raise ParallelError("launch_reservation supervisor_session 不合法")
    generation = _require_positive_integer(
        reservation["supervisor_generation"], "supervisor_generation")

    config = canonical_run_config(run_config)
    args = [
        str(python_executable), "-m", "engine.loop",
        "--repo", str(worker_repo_path),
        "--name", expected_workspace,
        "--goal", str(config["goal"]),
        "--agent-cmd", str(config["agent_cmd"]),
        "--validate-cmd", str(config["validate_cmd"]),
        "--flag-threshold", str(config["flag_threshold"]),
        "--done-threshold", str(config["done_threshold"]),
        "--red-limit", str(config["red_limit"]),
        "--stall-limit", str(config["stall_limit"]),
        "--stuck-stop-count", str(config["stuck_stop_count"]),
        "--round-timeout", f"{config['round_timeout']:g}",
        "--agent-backoff-max", f"{config['agent_backoff_max']:g}",
        "--validate-timeout", f"{config['validate_timeout']:g}",
        "--start-task", str(order),
        "--stop-after-task",
        "--complete-gate-cmd", gate_command.strip(),
        "--integration-ref", expected_integration_ref,
        "--parent-workspace", parent,
        "--task-ref", expected_task_ref,
        "--run-config-hash", hashes["run_config_hash"],
        "--launch-spec-hash", hashes["launch_spec_hash"],
        "--manifest-hash", hashes["manifest_hash"],
        "--dispatch-token", dispatch_token,
        "--dispatch-request-id", request_id,
        "--supervisor-session", supervisor_session,
        "--supervisor-generation", str(generation),
        "--dispatch-attempt", str(reservation["attempt"]),
    ]
    if config["plan_doc"]:
        args.extend(("--plan-doc", str(config["plan_doc"])))
    if config["stuck_stop"]:
        args.append("--stuck-stop")
    if resume:
        args.append("--managed-worker-resume")
    else:
        args.extend(("--import-plan", str(Path(plan_path).resolve()),
                     "--start-phase", "exec"))
    return args


@dataclass
class WorkerHandle:
    order: int
    process: subprocess.Popen
    control: object
    reservation: dict
    resume: bool


def _operation_id(run_id: str, operation: str, *identity: object) -> str:
    return parallel_state.canonical_json_hash({
        "run_id": run_id, "operation": operation, "identity": list(identity),
    })[:32]


def _git_read(
    repo: Path, *args: str,
    owner_fence: repo_owner.RepoOwnerFence | None = None,
) -> str:
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    })
    env.pop("GIT_EXTERNAL_DIFF", None)
    argv = [
        "git", "--no-pager", "-c", "gc.auto=0",
        "-c", "core.fsmonitor=false", *args,
    ]
    if owner_fence is None:
        result = subprocess.run(
            argv, cwd=str(repo), capture_output=True, text=True,
            check=False, env=env)
    else:
        try:
            child = owner_fence.spawn_child(
                repo_owner.ChildKind.GIT, argv, cwd=str(repo),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env)
        except (OSError, repo_owner.RepoOwnerError) as exc:
            raise ParallelError(f"controlled Git read launch failed: {exc}") from exc
        try:
            stdout, stderr = child.communicate(timeout=30.0)
        except BaseException as exc:
            try:
                child.kill_containment(timeout=5.0)
                try:
                    child.communicate(timeout=1.0)
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                child.record_result(containment_timeout=5.0)
                owner_fence.checkpoint_child(child.child_generation)
            except BaseException as reap_exc:
                raise ParallelError(
                    "controlled Git read could not be proven reaped") from reap_exc
            if isinstance(exc, subprocess.TimeoutExpired):
                raise ParallelError("controlled Git read timed out") from exc
            raise
        try:
            child.record_result(containment_timeout=5.0)
            owner_fence.checkpoint_child(child.child_generation)
        except repo_owner.RepoOwnerError as exc:
            raise ParallelError(
                "controlled Git read lifecycle did not reach idle") from exc
        result = subprocess.CompletedProcess(
            argv, child.returncode, stdout, stderr)
    if result.returncode:
        raise ParallelError(
            f"Git read failed rc={result.returncode}: {' '.join(args)}｜"
            f"{result.stderr.strip()}")
    return result.stdout.strip()


def _repository_start_identity(
    repo: Path, *, owner_fence: repo_owner.RepoOwnerFence | None = None,
) -> tuple[str, str]:
    try:
        canonical = Path(repo).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ParallelError(f"repo 無法解析：{exc}") from exc
    if not canonical.is_dir():
        raise ParallelError("repo 必須是目錄")
    branch = _git_read(
        canonical, "symbolic-ref", "-q", "HEAD", owner_fence=owner_fence)
    if not branch.startswith("refs/heads/"):
        raise ParallelError("parallel start 不接受 detached HEAD")
    head = parallel_contract.require_git_sha(
        _git_read(canonical, "rev-parse", "--verify", "HEAD",
                  owner_fence=owner_fence), "HEAD")
    if _git_read(canonical, "status", "--porcelain=v1", "--untracked-files=all",
                 owner_fence=owner_fence):
        raise ParallelError("parallel start 要求 primary worktree clean")
    return branch, head


def _assignment_for_worker(
    artifacts: parallel_state.ValidatedRunArtifacts, order: int,
) -> dict:
    assignment = dict(artifacts.assignments[order])
    assignment.update({
        "launch_spec_hash": artifacts.assignment_hashes[order],
        "manifest_hash": artifacts.manifest_hash,
    })
    return assignment


def build_repo_spec(
    artifacts: parallel_state.ValidatedRunArtifacts,
    *,
    pending_launch_hash: str,
    supervisor_session: str,
    generation: int,
) -> repo_executor.ImmutableRepoSpec:
    assignments = {
        order: repo_executor.AssignmentAuthority(
            order=order,
            assignment_hash=artifacts.assignment_hashes[order],
            run_config_hash=artifacts.run_config_hash,
            launch_spec_hash=artifacts.assignment_hashes[order],
        )
        for order in artifacts.assignments
    }
    try:
        validator_argv = tuple(compat.split_command(
            artifacts.run_config["validate_cmd"]))
    except ValueError as exc:
        raise ParallelError(f"validate_cmd 無法解析：{exc}") from exc
    if not validator_argv:
        raise ParallelError("validate_cmd 不可為空")
    return repo_executor.ImmutableRepoSpec(
        primary_repo=Path(artifacts.run_config["primary_repo"]),
        workspace_root=artifacts.run_dir.parents[2],
        parent_workspace=artifacts.manifest["parent_workspace"],
        run_id=artifacts.manifest["run_id"],
        pending_launch_hash=pending_launch_hash,
        manifest_hash=artifacts.manifest_hash,
        primary_ref=artifacts.manifest["integration_branch"],
        integration_start_sha=artifacts.manifest["integration_start_sha"],
        validator_argv=validator_argv,
        validator_timeout=artifacts.run_config["validate_timeout"],
        supervisor_session=supervisor_session,
        generation=generation,
        assignments=assignments,
    )


class ParallelSupervisor:
    """Single-writer orchestration around ordinary ``engine.loop`` workers."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        workspace: loop_mod.Workspace,
        artifacts: parallel_state.ValidatedRunArtifacts,
        aggregate: Mapping[str, object],
        executor: repo_executor.RepoExecutor,
        pending_launch_hash: str,
        session: str,
        generation: int,
        bootstrap_required: bool = True,
    ):
        self.workspace_root = Path(workspace_root)
        self.workspace = workspace
        self.artifacts = artifacts
        self.aggregate = parallel_state.validate_aggregate(
            dict(aggregate), run_id=artifacts.manifest["run_id"],
            plan=artifacts.plan)
        self.executor = executor
        self.pending_launch_hash = pending_launch_hash
        self.session = session
        self.generation = _require_positive_integer(generation, "generation")
        self.bootstrap_required = bool(bootstrap_required)
        self.handles: dict[int, WorkerHandle] = {}
        self.gate_spool = parallel_spool.DurableSpool(
            artifacts.run_dir / "requests",
            responses_root=artifacts.run_dir / "responses",
        )
        self.control_spool = parallel_spool.DurableSpool(
            artifacts.run_dir / "controls")
        self.pause_requested = False
        self.abort_requested = False

    @property
    def run_id(self) -> str:
        return self.artifacts.manifest["run_id"]

    def _task(self, order: int) -> dict:
        return next(task for task in self.aggregate["tasks"] if task["order"] == order)

    def _receipts(self) -> tuple[dict, ...]:
        _artifacts, receipts = parallel_state.load_receipt_chain(
            self.artifacts.run_dir, workspace_root=self.workspace_root)
        return receipts

    def _receipts_by_task(self) -> dict[int, dict]:
        return {receipt["task"]: receipt for receipt in self._receipts()}

    def _block_run_for_task(self, order: int, reason: str) -> None:
        """Persist an invariant failure without inventing a task outcome.

        In particular, an aggregate that already says ``integrated`` cannot be
        rewritten even when its receipt is missing.  The run is blocked and the
        mismatch remains visible for recovery instead of being normalized into
        a plausible-looking terminal task.
        """
        task = self._task(order)
        if task["outcome"] == "pending":
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, outcome="blocked", error=reason)
        else:
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, error=reason)
        self.aggregate["error"] = reason
        if self.aggregate["status"] != "blocked":
            self.aggregate = parallel_state.transition_run_status(
                self.aggregate, "blocked")

    def _receipt_authorizes_integration(self, order: int) -> bool:
        return order in self._receipts_by_task()

    def _audit_success_responses(
        self, receipts: Mapping[int, Mapping[str, object]],
    ) -> None:
        """Require every durable gate success to have its exact receipt.

        A response is written after RepoExecutor has committed the canonical
        receipt.  Recovery must therefore treat ``rc=0`` without that receipt
        as corruption, even when the aggregate/resource projection lagged in
        an ``exited`` state that would otherwise be eligible for dispatch.
        """
        # Force a complete directory scan first so an orphan/malformed response
        # cannot hide merely because no request loop below names its id.
        self.gate_spool.list_responses()
        for state in ("pending", "claimed", "cancelled"):
            for record in self.gate_spool.list_requests(state):
                order, request = self._validate_gate_request(
                    record,
                    recovery=(state == "pending"),
                    historical=(state != "pending"),
                )
                try:
                    response = self.gate_spool.get_response(record.request_id)
                except parallel_spool.SpoolError as exc:
                    raise ParallelError(
                        f"task-{order} gate response cannot be audited: {exc}"
                    ) from exc
                if response is None:
                    _worker_status, worker_assignment = (
                        self._load_worker_assignment(order))
                    retained = ((worker_assignment or {}).get("gate_request")
                                if isinstance(worker_assignment, Mapping)
                                else None)
                    retained_matches = (
                        isinstance(retained, Mapping)
                        and retained.get("request_id") == record.request_id
                        and retained.get("validated_sha")
                        == request["validated_sha"]
                        and retained.get("validated_round")
                        == request["validated_round"])
                    receipt = receipts.get(order)
                    receipt_matches = (
                        isinstance(receipt, Mapping)
                        and receipt.get("request_id") == record.request_id
                        and receipt.get("validated_sha")
                        == request["validated_sha"]
                        and receipt.get("validated_round")
                        == request["validated_round"])
                    resource = self._task(order)["resource_state"]
                    unanswered_claimed = 0
                    if state == "claimed":
                        for candidate in self.gate_spool.list_requests(
                                "claimed"):
                            if (isinstance(candidate.payload, Mapping)
                                    and candidate.payload.get("task") == order
                                    and self.gate_spool.get_response(
                                        candidate.request_id) is None):
                                unanswered_claimed += 1
                    allowed_unanswered = (
                        (state == "cancelled" and not receipt_matches)
                        or retained_matches
                        or (state == "claimed" and receipt_matches)
                        or (state == "claimed" and retained is None
                            and unanswered_claimed == 1
                            and resource in {
                                "gate_claimed", "recovery_required"}))
                    if not allowed_unanswered:
                        raise ParallelError(
                            f"task-{order} {state} gate request has no response "
                            "or exact current retained/receipt authority")
                    continue
                try:
                    returncode, payload = parallel_gate._validate_response_linearization(
                        self.gate_spool, request, response)
                except parallel_gate.GateClientError as exc:
                    raise ParallelError(
                        f"task-{order} gate response authority is invalid: {exc}"
                    ) from exc
                if returncode != 0:
                    continue
                if state != "claimed":
                    raise ParallelError(
                        f"task-{order} success gate response belongs to {state} request")
                receipt = receipts.get(order)
                if receipt is None:
                    raise ParallelError(
                        f"task-{order} success gate response has no canonical receipt")
                expected = {
                    "request_id": record.request_id,
                    "validated_sha": request["validated_sha"],
                    "validated_round": request["validated_round"],
                }
                if any(receipt.get(field) != value
                       for field, value in expected.items()):
                    raise ParallelError(
                        f"task-{order} success gate response does not match canonical receipt")
                if payload.get("status") not in {"merged", "already-merged"}:
                    raise ParallelError(
                        f"task-{order} success gate response status is not merge success")

    def _expected_tip(self) -> str:
        receipts = self._receipts()
        return (receipts[-1]["validated_sha"] if receipts
                else self.artifacts.manifest["integration_start_sha"])

    def _worker_rounds(self) -> dict[int, int]:
        rounds: dict[int, int] = {}
        for order in self.artifacts.assignments:
            path = self._worker_workspace_storage_dir(order) / "state.json"
            try:
                state, _raw, _recovered = loop_mod.load_checkpointed_state(
                    path, repair=False)
            except (FileNotFoundError, OSError, ValueError, loop_mod.StateLoadError):
                continue
            value = state.get("round")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                rounds[order] = value
        return rounds

    def _worker_archive_dir(self, order: int) -> Path:
        _require_positive_integer(order, "worker archive task")
        if order not in self.artifacts.assignments:
            raise ParallelError(f"task-{order} 不在 immutable assignments")
        return self.artifacts.run_dir / "worker-archives" / f"task-{order}"

    def _worker_container_authority(self, order: int) -> dict:
        assignment = self.artifacts.assignments[order]
        return {
            "schema": 1,
            "run_id": self.run_id,
            "task": order,
            "manifest_hash": self.artifacts.manifest_hash,
            "assignment_hash": self.artifacts.assignment_hashes[order],
            "worker_workspace": assignment["worker_workspace"],
            "worker_workspace_path": assignment["worker_workspace_path"],
        }

    def _audit_worker_container_authority(self, order: int) -> None:
        try:
            observed = parallel_state.read_canonical_json(
                self.artifacts.run_dir,
                f"workspace-containers/task-{order}.json",
            )
        except (OSError, ValueError, parallel_state.ParallelStateError) as exc:
            raise ParallelError(
                f"task-{order} worker container authority unavailable: {exc}"
            ) from exc
        if observed != self._worker_container_authority(order):
            raise ParallelError(
                f"task-{order} worker container authority mismatch")

    @staticmethod
    def _checked_real_directory(path: Path, label: str) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ParallelError(f"{label} 無法檢查：{exc}") from exc
        if (stat.S_ISLNK(info.st_mode) or compat.is_reparse_point(info)
                or not stat.S_ISDIR(info.st_mode)):
            raise ParallelError(f"{label} 必須是 real directory")
        return True

    @staticmethod
    def _real_directory_identity(path: Path, label: str) -> tuple[int, int]:
        try:
            info = path.lstat()
        except OSError as exc:
            raise ParallelError(f"{label} identity unavailable: {exc}") from exc
        if (stat.S_ISLNK(info.st_mode) or compat.is_reparse_point(info)
                or not stat.S_ISDIR(info.st_mode)):
            raise ParallelError(f"{label} is not a stable real directory")
        return int(info.st_dev), int(info.st_ino)

    def _require_directory_identity(
        self, path: Path, expected: tuple[int, int], label: str,
    ) -> None:
        if self._real_directory_identity(path, label) != expected:
            raise ParallelError(f"{label} changed during archive rename")

    def _worker_workspace_locations(
            self, order: int) -> tuple[Path, Path, bool, bool]:
        source = Path(
            self.artifacts.assignments[order]["worker_workspace_path"])
        archive = self._worker_archive_dir(order)
        source_exists = self._checked_real_directory(
            source, f"task-{order} worker workspace")
        archive_root_exists = self._checked_real_directory(
            archive.parent, "parallel worker archive root")
        archive_exists = (
            self._checked_real_directory(
                archive, f"task-{order} worker archive")
            if archive_root_exists else False)
        if source_exists and archive_exists:
            raise ParallelError(
                f"task-{order} worker workspace 與 archive 同時存在")
        return source, archive, source_exists, archive_exists

    def _worker_workspace_storage_dir(self, order: int) -> Path:
        """Locate the active or terminally archived worker checkpoint."""
        source, archive, source_exists, archive_exists = (
            self._worker_workspace_locations(order))
        if archive_exists:
            return archive
        return source

    def _worker_checkpoint_hash(self, order: int) -> str | None:
        """Hash the recoverable worker checkpoint, or prove it never existed."""
        directory = self._worker_workspace_storage_dir(order)
        state_path = directory / "state.json"
        checkpoint_path = loop_mod.state_checkpoint_path(state_path)
        primary_exists = os.path.lexists(state_path)
        checkpoint_exists = os.path.lexists(checkpoint_path)
        if not primary_exists and not checkpoint_exists:
            return None
        try:
            primary = (loop_mod.read_regular_bytes(state_path, "worker state")
                       if primary_exists else None)
            checkpoint = (
                loop_mod.read_regular_bytes(
                    checkpoint_path, "worker state checkpoint")
                if checkpoint_exists else None)
        except (OSError, ValueError) as exc:
            raise ParallelError(
                f"task-{order} worker checkpoint bytes unavailable: {exc}"
            ) from exc
        if (primary is not None and checkpoint is not None
                and primary != checkpoint):
            raise ParallelError(
                f"task-{order} worker primary/checkpoint diverged before archive")
        self._audit_worker_checkpoint_authority(order)
        raw = primary if primary is not None else checkpoint
        return hashlib.sha256(raw).hexdigest()

    def _worker_archive_authority(
        self, order: int, *, had_launch: bool, checkpoint_hash: str | None,
    ) -> dict:
        return {
            "schema": 1,
            "run_id": self.run_id,
            "task": order,
            "outcome": self._task(order)["outcome"],
            "had_launch": bool(had_launch),
            "checkpoint_hash": checkpoint_hash,
            "manifest_hash": self.artifacts.manifest_hash,
            "assignment_hash": self.artifacts.assignment_hashes[order],
        }

    def _archive_authority_path(self, order: int) -> Path:
        return (self.artifacts.run_dir / "worker-archive-authority"
                / f"task-{order}.json")

    def _publish_worker_archive_authority(
        self, order: int, *, had_launch: bool,
    ) -> None:
        checkpoint_hash = self._worker_checkpoint_hash(order)
        try:
            parallel_state.write_or_verify_immutable_json(
                self.artifacts.run_dir,
                f"worker-archive-authority/task-{order}.json",
                self._worker_archive_authority(
                    order, had_launch=had_launch,
                    checkpoint_hash=checkpoint_hash),
            )
        except (OSError, ValueError, parallel_state.ParallelStateError) as exc:
            raise ParallelError(
                f"task-{order} worker archive authority publish failed: {exc}"
            ) from exc

    def _audit_worker_archive_authority(
        self, order: int, *, had_launch: bool,
    ) -> None:
        try:
            observed = parallel_state.read_canonical_json(
                self.artifacts.run_dir,
                f"worker-archive-authority/task-{order}.json")
        except (OSError, ValueError, parallel_state.ParallelStateError) as exc:
            raise ParallelError(
                f"task-{order} worker archive authority unavailable: {exc}"
            ) from exc
        checkpoint_hash = self._worker_checkpoint_hash(order)
        expected = self._worker_archive_authority(
            order, had_launch=had_launch,
            checkpoint_hash=checkpoint_hash)
        if observed != expected:
            raise ParallelError(
                f"task-{order} worker archive authority/checkpoint mismatch")

    def _audit_worker_checkpoint_authority(self, order: int) -> dict:
        """Load a worker checkpoint without following links and bind its run."""
        state_path = self._worker_workspace_storage_dir(order) / "state.json"
        try:
            state, _raw, _recovered = loop_mod.load_checkpointed_state(
                state_path, repair=False)
            parallel_worker.validate_persisted_state(state)
        except (FileNotFoundError, OSError, ValueError, loop_mod.StateLoadError,
                parallel_contract.ParallelContractError) as exc:
            raise ParallelError(
                f"task-{order} worker checkpoint unavailable: {exc}"
            ) from exc
        expected_state = {
            "run_id": self.run_id,
            "parent_workspace": self.artifacts.manifest["parent_workspace"],
            "assigned_order": order,
            "integration_ref": self.artifacts.assignments[order]["integration_ref"],
            "task_ref": self.artifacts.assignments[order]["task_ref"],
            "run_config_hash": self.artifacts.run_config_hash,
            "launch_spec_hash": self.artifacts.assignment_hashes[order],
            "manifest_hash": self.artifacts.manifest_hash,
        }
        if any(state.get(field) != value
               for field, value in expected_state.items()):
            raise ParallelError(
                f"task-{order} worker checkpoint authority mismatch")
        expected_plan = [dict(item) for item in self.artifacts.plan]
        if state.get("plan") != expected_plan:
            raise ParallelError(
                f"task-{order} integrated worker checkpoint plan mismatch")
        return state

    def _audit_integrated_worker_checkpoint(self, order: int) -> None:
        """Bind an integrated worker checkpoint to immutable run + receipt."""
        receipt = self._receipts_by_task().get(order)
        if receipt is None:
            raise ParallelError(
                f"task-{order} integrated worker archive lacks canonical receipt")
        state = self._audit_worker_checkpoint_authority(order)
        assignment = state["assignment"]
        if (assignment.get("status") != "integrated"
                or assignment.get("validated_sha") != receipt["validated_sha"]
                or assignment.get("validated_round") != receipt["validated_round"]
                or assignment.get("gate_request") is not None):
            raise ParallelError(
                f"task-{order} integrated worker checkpoint does not match receipt")

    def _archive_terminal_worker_workspaces(self) -> None:
        """Atomically hide terminal worker workspaces while preserving audit data.

        Managed worker workspaces live beside user workspaces while active.  A
        terminal run must not leave those implementation workspaces visible in
        ordinary workspace discovery, but their state/history remain valuable
        recovery evidence.  Moving each directory below the immutable run keeps
        that evidence and makes a crash between task moves idempotent.
        """
        if self.handles:
            raise ParallelError("terminal worker archive 前仍有 active handles")
        archive_root = self.artifacts.run_dir / "worker-archives"
        try:
            loop_mod.ensure_real_directory(
                archive_root, "parallel worker archive root")
        except ValueError as exc:
            raise ParallelError(str(exc)) from exc
        for task in self.aggregate["tasks"]:
            order = task["order"]
            if (task["outcome"] not in {"integrated", "cancelled"}
                    or task["resource_state"] != "cleaned"):
                raise ParallelError(
                    f"task-{order} 尚未 terminal-clean，不能 archive workspace")
            self._require_reaped_child_evidence(order)
            had_launch = self._audit_terminal_launch_child_evidence(order)
            source, archive, source_exists, archive_exists = (
                self._worker_workspace_locations(order))
            if task["outcome"] == "integrated":
                self._audit_integrated_worker_checkpoint(order)
            if archive_exists:
                self._audit_worker_archive_authority(
                    order, had_launch=had_launch)
                continue
            if not source_exists:
                # A future-batch task can be cancelled before any guardian or
                # worker workspace exists.  Every other missing workspace is
                # lost audit evidence and must remain blocked.
                if task["outcome"] == "cancelled" and not had_launch:
                    continue
                raise ParallelError(
                    f"task-{order} terminal worker workspace audit evidence 遺失")
            self._publish_worker_archive_authority(
                order, had_launch=had_launch)
            self._audit_worker_archive_authority(
                order, had_launch=had_launch)
            source_parent_identity = self._real_directory_identity(
                source.parent, f"task-{order} worker workspace parent")
            archive_root_identity = self._real_directory_identity(
                archive_root, "parallel worker archive root")
            source_identity = self._real_directory_identity(
                source, f"task-{order} worker workspace")
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    self._require_directory_identity(
                        source.parent, source_parent_identity,
                        f"task-{order} worker workspace parent")
                    self._require_directory_identity(
                        archive_root, archive_root_identity,
                        "parallel worker archive root")
                    self._require_directory_identity(
                        source, source_identity,
                        f"task-{order} worker workspace")
                    if os.path.lexists(archive):
                        raise ParallelError(
                            f"task-{order} worker archive appeared before rename")
                    os.replace(source, archive)
                    break
                except PermissionError as exc:
                    if time.monotonic() >= deadline:
                        raise ParallelError(
                            f"task-{order} worker workspace archive 被占用：{exc}") from exc
                    time.sleep(0.02)
                except OSError as exc:
                    raise ParallelError(
                        f"task-{order} worker workspace archive 失敗：{exc}") from exc
            parallel_state._fsync_directory(source.parent)
            parallel_state._fsync_directory(archive_root)
            self._require_directory_identity(
                source.parent, source_parent_identity,
                f"task-{order} worker workspace parent")
            self._require_directory_identity(
                archive_root, archive_root_identity,
                "parallel worker archive root")
            if (os.path.lexists(source)
                    or self._real_directory_identity(
                        archive, f"task-{order} worker archive")
                    != source_identity):
                raise ParallelError(
                    f"task-{order} worker archive rename 後遺失")
            self._audit_worker_archive_authority(
                order, had_launch=had_launch)

    def _audit_terminal_worker_archives(self) -> None:
        """Require terminal projections to have no visible worker workspace."""
        for task in self.aggregate["tasks"]:
            order = task["order"]
            self._require_reaped_child_evidence(order)
            had_launch = self._audit_terminal_launch_child_evidence(order)
            _source, _archive, source_exists, archive_exists = (
                self._worker_workspace_locations(order))
            if source_exists:
                raise ParallelError(
                    f"terminal task-{order} 仍有 visible worker workspace")
            if task["outcome"] == "integrated" and not archive_exists:
                raise ParallelError(
                    f"terminal integrated task-{order} 缺少 worker archive")
            if task["outcome"] == "integrated":
                self._audit_integrated_worker_checkpoint(order)
            if (task["outcome"] == "cancelled" and had_launch
                    and not archive_exists):
                raise ParallelError(
                    f"terminal launched task-{order} 缺少 worker archive")
            if archive_exists:
                self._audit_worker_archive_authority(
                    order, had_launch=had_launch)
            elif os.path.lexists(self._archive_authority_path(order)):
                raise ParallelError(
                    f"terminal task-{order} has orphan worker archive authority")

    def _audit_terminal_receipt_projection(self) -> None:
        """Bind terminal task outcomes and gate success to canonical receipts."""
        receipts = self._receipts_by_task()
        integrated = {
            task["order"] for task in self.aggregate["tasks"]
            if task["outcome"] == "integrated"
        }
        if set(receipts) != integrated:
            raise ParallelError(
                "terminal integrated task projection does not exactly match receipts")
        self._audit_success_responses(receipts)

    def checkpoint(
            self, *, active: bool = True,
            persist_aggregate: bool = True) -> None:
        if persist_aggregate:
            self.aggregate = save_aggregate(
                self.artifacts.run_dir, self.aggregate, self.artifacts.plan)
        else:
            parallel_state.validate_aggregate(
                self.aggregate, run_id=self.run_id,
                plan=self.artifacts.plan)
        state = project_base_state(
            self.workspace, self.artifacts, self.aggregate, self._receipts(),
            supervisor_pid=os.getpid() if active else None,
            supervisor_session=self.session if active else None,
            supervisor_generation=self.generation,
            worker_rounds=self._worker_rounds(),
        )
        self.workspace.save_state(state)

    def _execute(self, request: dict) -> dict:
        try:
            return self.executor.execute(request)
        except repo_executor.RepoExecutorError as exc:
            raise ParallelError(f"RepoExecutor {request['operation']} blocked：{exc}") from exc

    def _preflight_request(self) -> dict:
        branch = self.artifacts.manifest["integration_branch"]
        start = self.artifacts.manifest["integration_start_sha"]
        return {
            "operation": repo_executor.Operation.PREFLIGHT.value,
            "operation_id": _operation_id(self.run_id, "preflight"),
            "authority": {"pending_launch_hash": self.pending_launch_hash},
            "expected": {"head_ref": branch, "head_sha": start},
        }

    def _initialize_refs_request(self) -> dict:
        start = self.artifacts.manifest["integration_start_sha"]
        return {
            "operation": repo_executor.Operation.INITIALIZE_RUN_REFS.value,
            "operation_id": _operation_id(self.run_id, "initialize-refs"),
            "authority": {"manifest_hash": self.artifacts.manifest_hash},
            "expected": {
                "integration_start_sha": start,
                "sync_ref_absent": True,
            },
        }

    def preflight_and_initialize(self) -> None:
        self._execute(self._preflight_request())
        self._execute(self._initialize_refs_request())

    @staticmethod
    def _require_exact_fields(
            value: Mapping[str, object], fields: set[str], label: str) -> None:
        if set(value) != fields:
            raise ParallelError(f"{label} schema mismatch")

    def _validate_preflight_result(self, request: dict) -> str | None:
        operation_id = request["operation_id"]
        path = self.executor.results_dir / f"{operation_id}.json"
        if not os.path.lexists(path):
            return None
        try:
            result = self.executor._cached_result(
                operation_id, repo_executor.canonical_hash(request))
        except (AttributeError, repo_executor.RepoExecutorError) as exc:
            raise ParallelError(
                f"startup PREFLIGHT result audit blocked：{exc}") from exc
        if result is None:  # pragma: no cover - path existence is checked above
            raise ParallelError("startup PREFLIGHT result disappeared during audit")
        self._require_exact_fields(result, {
            "operation", "operation_id", "status", "head_ref", "head_sha",
        }, "startup PREFLIGHT result")
        expected = {
            "operation": repo_executor.Operation.PREFLIGHT.value,
            "operation_id": operation_id,
            "status": "validated",
            "head_ref": self.artifacts.manifest["integration_branch"],
            "head_sha": self.artifacts.manifest["integration_start_sha"],
        }
        if result != expected:
            raise ParallelError("startup PREFLIGHT result authority mismatch")
        return repo_executor.canonical_hash(result)

    def _validate_initialization_success(
            self, request: dict,
            init_paths: Mapping[str, Path]) -> tuple[str, str]:
        operation_id = request["operation_id"]
        start = self.artifacts.manifest["integration_start_sha"]
        authority = {
            "schema_version": 1,
            "kind": repo_executor.Operation.INITIALIZE_RUN_REFS.value,
            "operation_id": operation_id,
            "manifest_hash": self.artifacts.manifest_hash,
            "sync_ref": self.executor.sync_ref,
            "start_sha": start,
        }
        try:
            intent = self.executor._read_json(
                init_paths["intent"], "startup init intent")
            receipt = self.executor._read_json(
                init_paths["receipt"], "startup init receipt")
            result = self.executor._cached_result(
                operation_id, repo_executor.canonical_hash(request))
        except (AttributeError, repo_executor.RepoExecutorError) as exc:
            raise ParallelError(
                f"startup initialization success audit blocked：{exc}") from exc
        if result is None:  # pragma: no cover - result path existence is checked above
            raise ParallelError("startup initialization result disappeared during audit")

        self._require_exact_fields(intent, set(authority) | {
            "state", "prepared_at", "committed_at", "receipt_hash",
        }, "startup init intent")
        self._require_exact_fields(receipt, set(authority) | {
            "created_at", "receipt_hash",
        }, "startup init receipt")
        self._require_exact_fields(result, {
            "operation", "operation_id", "status", "sync_ref", "sync_sha",
            "receipt_hash",
        }, "startup init result")
        receipt_body = {
            key: value for key, value in receipt.items()
            if key != "receipt_hash"
        }
        receipt_hash = repo_executor.canonical_hash(receipt_body)
        timestamps = (
            intent["prepared_at"], intent["committed_at"], receipt["created_at"])
        if (any(intent.get(key) != value for key, value in authority.items())
                or any(receipt.get(key) != value for key, value in authority.items())
                or intent["state"] != "committed"
                or any(not isinstance(value, str) or not value for value in timestamps)
                or receipt["receipt_hash"] != receipt_hash
                or intent["receipt_hash"] != receipt_hash
                or result != {
                    "operation": repo_executor.Operation.INITIALIZE_RUN_REFS.value,
                    "operation_id": operation_id,
                    "status": result["status"],
                    "sync_ref": self.executor.sync_ref,
                    "sync_sha": start,
                    "receipt_hash": receipt_hash,
                }
                or result["status"] not in {
                    "initialized", "already-initialized"}):
            raise ParallelError(
                "startup initialization success evidence mismatch")

        return result["status"], repo_executor.canonical_hash(result)

    def _reject_conflicting_initialization_artifacts(
            self, initialize_id: str) -> None:
        """Reject non-canonical init journals/results for this run authority."""
        expected_name = f"init-{initialize_id}.json"
        current_manifest = self.artifacts.manifest_hash
        current_sync = self.executor.sync_ref
        try:
            for directory, label in (
                    (self.executor.intents_dir, "intent"),
                    (self.executor.receipts_dir, "receipt")):
                for path in directory.glob("init-*.json"):
                    if path.name == expected_name:
                        continue
                    operation_id = path.name.removeprefix("init-").removesuffix(
                        ".json")
                    if (len(operation_id) != 32
                            or any(ch not in "0123456789abcdef"
                                   for ch in operation_id)):
                        raise ParallelError(
                            f"startup init {label} has a non-canonical filename")
                    artifact = self.executor._read_json(
                        path, f"startup non-canonical init {label}")
                    identity = {
                        "schema_version", "kind", "operation_id", "manifest_hash",
                        "sync_ref", "start_sha",
                    }
                    if (not identity.issubset(artifact)
                            or artifact["schema_version"] != 1
                            or artifact["kind"]
                            != repo_executor.Operation.INITIALIZE_RUN_REFS.value
                            or artifact["operation_id"] != operation_id):
                        raise ParallelError(
                            f"startup non-canonical init {label} is malformed")
                    if (artifact["manifest_hash"] == current_manifest
                            or artifact["sync_ref"] == current_sync):
                        raise ParallelError(
                            "startup initialization has a conflicting "
                            f"same-run {label}")

            for path in self.executor.results_dir.glob("*.json"):
                if path.name == f"{initialize_id}.json":
                    continue
                artifact = self.executor._read_json(
                    path, "startup non-canonical operation result")
                result = artifact.get("result")
                if (not isinstance(result, Mapping)
                        or result.get("operation")
                        != repo_executor.Operation.INITIALIZE_RUN_REFS.value):
                    continue
                if not isinstance(result.get("sync_ref"), str):
                    raise ParallelError(
                        "startup non-canonical init result is malformed")
                if result["sync_ref"] == current_sync:
                    raise ParallelError(
                        "startup initialization has a conflicting same-run result")
        except repo_executor.RepoExecutorError as exc:
            raise ParallelError(
                f"startup initialization journal audit blocked：{exc}") from exc

    def recover_startup_initialization(
            self, *, reconcile_pending: bool = False,
            initialize_pristine: bool = True) -> bool | None:
        """Replay pristine startup initialization, but reject partial evidence.

        A supervisor can disappear after publishing the immutable run and base
        projection but before reserving ``INITIALIZE_RUN_REFS``.  That state is
        distinguishable from an interrupted/unknown initialization only while
        every init artifact and derived resource is absent and primary remains
        at the immutable start identity.  Exact PREFLIGHT and initialization
        operation ids make the replay idempotent, including the window between
        those two operations.
        """
        preflight_request = self._preflight_request()
        initialize_request = self._initialize_refs_request()
        initialize_id = initialize_request["operation_id"]
        init_paths = {
            "intent": self.executor.intents_dir / f"init-{initialize_id}.json",
            "receipt": self.executor.receipts_dir / f"init-{initialize_id}.json",
            "result": self.executor.results_dir / f"{initialize_id}.json",
        }
        try:
            self.executor._start()
            self._reject_conflicting_initialization_artifacts(initialize_id)
            present = {
                label: os.path.lexists(path)
                for label, path in init_paths.items()
            }
            sync_tip = self.executor._ref_tip(self.executor.sync_ref)
            lease = None
            if os.path.lexists(self.executor.lease_path):
                lease = self.executor._read_json(
                    self.executor.lease_path, "startup recovery operation lease")
                self.executor._validate_lease_shape(lease)
        except (AttributeError, repo_executor.RepoExecutorError) as exc:
            raise ParallelError(
                f"startup initialization evidence audit blocked：{exc}") from exc

        exact_init_lease = (
            lease is not None
            and lease["operation"]
            == repo_executor.Operation.INITIALIZE_RUN_REFS.value
            and lease["operation_id"] == initialize_id
        )
        if (lease is not None and lease["operation_id"] == initialize_id
                and not exact_init_lease):
            raise ParallelError(
                "startup initialization operation id is bound to another operation")

        same_run_lease = (
            lease is not None
            and lease["immutable_spec_hash"] == self.executor.authority_hash
        )
        canonical_preflight_lease = (
            same_run_lease
            and lease["operation"] == repo_executor.Operation.PREFLIGHT.value
            and lease["operation_id"] == preflight_request["operation_id"]
            and lease["request"] == preflight_request
            and lease["request_hash"]
            == repo_executor.canonical_hash(preflight_request)
        )
        canonical_init_lease = (
            same_run_lease
            and exact_init_lease
            and lease["request"] == initialize_request
            and lease["request_hash"]
            == repo_executor.canonical_hash(initialize_request)
        )

        complete = all(present.values()) and sync_tip is not None
        if complete:
            init_status, init_result_hash = self._validate_initialization_success(
                initialize_request, init_paths)
            if exact_init_lease and (
                    lease["state"] != "terminal"
                    or not canonical_init_lease
                    or lease["terminal_status"] != init_status
                    or lease["result_hash"] != init_result_hash):
                raise ParallelError(
                    "startup initialization has a non-success terminal lease")
            return False

        if lease is not None and lease["state"] != "terminal":
            allowed_pending = canonical_preflight_lease or canonical_init_lease
            if (not reconcile_pending or not allowed_pending
                    or (canonical_preflight_lease
                        and (any(present.values()) or sync_tip is not None))):
                raise ParallelError(
                    "partial/unknown startup initialization has a non-canonical "
                    "pending lease")
            if (canonical_init_lease
                    and self._validate_preflight_result(
                        preflight_request) is None):
                raise ParallelError(
                    "pending startup initialization lacks exact PREFLIGHT proof")
            try:
                result = self.executor.reconcile_pending_operation(
                    recovery_authorizer=(
                        repo_executor.RepoExecutor.fence_recovery_lease))
            except repo_executor.RepoExecutorError as exc:
                raise ParallelError(
                    f"startup operation recovery blocked：{exc}") from exc
            if result is None or result.get("operation") not in {
                    repo_executor.Operation.PREFLIGHT.value,
                    repo_executor.Operation.INITIALIZE_RUN_REFS.value}:
                raise ParallelError(
                    "startup operation recovery returned unexpected result")
            return self.recover_startup_initialization(
                reconcile_pending=False,
                initialize_pristine=initialize_pristine)

        pristine = (
            not any(present.values())
            and sync_tip is None
            and not exact_init_lease
        )
        if not pristine:
            evidence = ",".join(
                label for label, exists in present.items() if exists)
            if sync_tip is not None:
                evidence = f"{evidence},sync-ref" if evidence else "sync-ref"
            if exact_init_lease:
                evidence = f"{evidence},init-lease" if evidence else "init-lease"
            raise ParallelError(
                "partial/unknown startup initialization evidence："
                f"{evidence or 'unclassified'}")

        if same_run_lease:
            canonical_terminal_preflight = (
                canonical_preflight_lease
                and lease["state"] == "terminal"
                and lease["terminal_status"] == "validated"
            )
            if not canonical_terminal_preflight:
                raise ParallelError(
                    "partial/unknown startup initialization has a non-PREFLIGHT "
                    "same-run lease")
        preflight_result_hash = self._validate_preflight_result(
            preflight_request)
        if same_run_lease:
            if preflight_result_hash is None:
                raise ParallelError(
                    "startup PREFLIGHT lease lacks its exact durable result")
            if lease["result_hash"] != preflight_result_hash:
                raise ParallelError(
                    "startup PREFLIGHT lease/result hash mismatch")

        if self._receipts():
            raise ParallelError(
                "missing startup initialization conflicts with canonical receipts")
        try:
            for state in ("pending", "claimed", "cancelled"):
                if self.gate_spool.list_requests(state):
                    raise ParallelError(
                        "missing startup initialization conflicts with gate requests")
            if self.gate_spool.list_responses():
                raise ParallelError(
                    "missing startup initialization conflicts with gate responses")
            launch_spool = parallel_spool.DurableSpool(
                self.artifacts.run_dir / "launches")
            for state in ("pending", "claimed", "cancelled"):
                if launch_spool.list_requests(state):
                    raise ParallelError(
                        "missing startup initialization conflicts with worker launches")
            if launch_spool.list_responses():
                raise ParallelError(
                    "missing startup initialization conflicts with launch responses")
            for task in self.aggregate["tasks"]:
                order = task["order"]
                if self._task_child_records(order):
                    raise ParallelError(
                        "missing startup initialization conflicts with child evidence")
                if os.path.lexists(
                        self.artifacts.run_dir
                        / f"workspace-containers/task-{order}.json"):
                    raise ParallelError(
                        "missing startup initialization conflicts with worker container authority")
                if self.executor._ref_tip(self.executor.task_ref(order)) is not None:
                    raise ParallelError(
                        f"missing startup initialization conflicts with task-{order} ref")
                observation = self.executor.observe_worktree(order)
                if observation.get("exists") or observation.get("registered"):
                    raise ParallelError(
                        "missing startup initialization conflicts with "
                        f"task-{order} worktree")
            self.executor._require_primary(
                head=self.artifacts.manifest["integration_start_sha"])
        except (AttributeError, repo_executor.RepoExecutorError) as exc:
            raise ParallelError(
                f"pristine startup initialization requires exact primary start：{exc}"
            ) from exc

        if not initialize_pristine:
            # Missing worker secrets may not run the configured validator just
            # to discover that no startup mutation ever began.  The exhaustive
            # pristine audit above proves there are no refs, receipts, gate
            # requests, task resources, or noncanonical operation evidence.
            return None
        self.preflight_and_initialize()
        return True

    def _worker_environment(self) -> dict[str, str]:
        return build_worker_environment(
            self.artifacts.run_config["environment"],
            workspace_root=self.workspace_root,
        )

    def process_controls(self) -> bool:
        progressed = False
        for record in self.control_spool.list_requests("pending"):
            request = record.payload
            expected_fields = {
                "schema", "request_id", "run_id", "action",
                "supervisor_session", "supervisor_generation",
                "control_generation", "aggregate_version",
            }
            if (not isinstance(request, dict) or set(request) != expected_fields
                    or request.get("schema") != 1
                    or request.get("request_id") != record.request_id
                    or request.get("run_id") != self.run_id
                    or request.get("action") not in {"pause", "abort"}):
                raise ParallelError("parallel control request authority/schema 不符")
            request_session = request.get("supervisor_session")
            request_generation = request.get("supervisor_generation")
            control_generation = request.get("control_generation")
            aggregate_version = request.get("aggregate_version")
            if (not isinstance(request_session, str)
                    or len(request_session) != 32
                    or any(ch not in "0123456789abcdef"
                           for ch in request_session)
                    or not isinstance(request_generation, int)
                    or isinstance(request_generation, bool)
                    or request_generation < 1
                    or not isinstance(control_generation, int)
                    or isinstance(control_generation, bool)
                    or control_generation < 1
                    or not isinstance(aggregate_version, int)
                    or isinstance(aggregate_version, bool)
                    or aggregate_version < 0):
                raise ParallelError("parallel control owner identity 不合法")
            if (request_session != self.session
                    or request_generation != self.generation
                    or control_generation
                    != self.aggregate["control_generation"] + 1
                    or aggregate_version != self.aggregate["version"]):
                cancelled = self.control_spool.cancel_request(record.request_id)
                if cancelled.transitioned:
                    self.control_spool.publish_response(record.request_id, {
                        "schema": 1,
                        "request_id": record.request_id,
                        "status": "stale",
                        "action": request.get("action"),
                        "run_id": request.get("run_id"),
                    })
                progressed = True
                continue
            action = request["action"]
            try:
                _validate_recovery_action_legality(self.aggregate, action)
            except ParallelError:
                # State may have advanced after the caller observed the owner.
                # Reject before claim: claim is the control linearization point,
                # so an illegal request must not consume durable authority.
                cancelled = self.control_spool.cancel_request(record.request_id)
                if cancelled.transitioned:
                    self.control_spool.publish_response(record.request_id, {
                        "schema": 1,
                        "request_id": record.request_id,
                        "status": "rejected",
                        "action": action,
                        "run_id": self.run_id,
                    })
                progressed = True
                continue
            claimed = self.control_spool.claim_request(record.request_id)
            if not claimed.transitioned:
                continue
            self.aggregate = dict(self.aggregate)
            self.aggregate["control_generation"] = control_generation
            if action == "pause":
                if self.aggregate["status"] in {"initializing", "running"}:
                    self.aggregate = parallel_state.transition_run_status(
                        self.aggregate, "pause_requested")
                    self.aggregate = parallel_state.advance_pause_generation(
                        self.aggregate)
                    self.pause_requested = True
            else:
                if self.aggregate["terminal_intent"] is None:
                    self.aggregate = parallel_state.set_terminal_intent(
                        self.aggregate, "cancelled")
                if self.aggregate["status"] not in {
                        "cancel_requested", "finalizing_cancel", "cancelled"}:
                    self.aggregate = parallel_state.transition_run_status(
                        self.aggregate, "cancel_requested")
                self.abort_requested = True
            self.checkpoint()
            self.control_spool.publish_response(record.request_id, {
                "schema": 1,
                "request_id": record.request_id,
                "status": "accepted",
                "action": action,
                "run_id": self.run_id,
            })
            progressed = True
        return progressed

    def _cancel_pending_launches(self) -> None:
        launch_spool = parallel_spool.DurableSpool(
            self.artifacts.run_dir / "launches")
        for record in launch_spool.list_requests("pending"):
            launch_spool.cancel_request(record.request_id)

    def _cancel_pending_gates(
            self, *, abort: bool, recovery: bool = False) -> None:
        for record in self.gate_spool.list_requests("pending"):
            order, request = self._validate_gate_request(
                record, historical=recovery)
            _status, assignment = self._load_worker_assignment(order)
            retained = (assignment.get("gate_request")
                        if isinstance(assignment, Mapping) else None)
            if (not isinstance(retained, Mapping)
                    or retained.get("request_id") != record.request_id
                    or retained.get("validated_sha")
                    != request["validated_sha"]
                    or retained.get("validated_round")
                    != request["validated_round"]):
                raise ParallelError(
                    f"task-{order} pending gate lacks exact retained worker authority")
            cancelled = self.gate_spool.cancel_request(record.request_id)
            if not cancelled.transitioned:
                continue
            task = self._task(order)
            if task["resource_state"] in {"running", "gate_pending"}:
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="pausing")
            if abort:
                self._publish_gate_response(
                    request, returncode=21, status="cancelled",
                    reason="parent supervisor received Abort")
            else:
                self._publish_gate_response(
                    request, returncode=20, status="paused",
                    reason="parent supervisor received Pause")
        self.checkpoint()

    def _request_worker_stop(self, order: int, *, abort: bool) -> None:
        workspace_dir = Path(
            self.artifacts.assignments[order]["worker_workspace_path"])
        try:
            state, _raw, _recovered = loop_mod.load_checkpointed_state(
                workspace_dir / "state.json", repair=False)
        except (FileNotFoundError, OSError, ValueError, loop_mod.StateLoadError):
            return
        loop_state = state.get("loop")
        if not isinstance(loop_state, dict):
            return
        pid = loop_state.get("pid")
        session_id = loop_state.get("session_id")
        if (not isinstance(pid, int) or isinstance(pid, bool)
                or not isinstance(session_id, str) or not session_id):
            return
        loop_mod.atomic_write_bytes(
            workspace_dir / loop_mod.STOP_AFTER_ROUND_FILE,
            json.dumps({
                "pid": pid,
                "session_id": session_id,
                "action": "abort" if abort else "pause",
                "pause_generation": self.aggregate["pause_generation"],
                "requested_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"),
            }, ensure_ascii=False).encode("utf-8"),
        )

    def _project_reaped_worker_paused(self, order: int) -> None:
        """Repair the narrow child-exit -> paused-checkpoint crash window."""
        worker = loop_mod.Workspace(
            self.artifacts.assignments[order]["worker_workspace"])
        try:
            state = worker.load_state()
        except FileNotFoundError:
            return
        try:
            parallel_worker.validate_persisted_state(state)
            assignment = state["assignment"]
            if assignment["status"] not in {"running", "paused"}:
                return
            if assignment.get("gate_request") is not None:
                # Retained gate identity is stronger evidence and must be
                # reconciled on Resume; never erase it to manufacture Pause.
                return
            paused = parallel_worker.mark_supervisor_paused(
                state,
                pause_generation=self.aggregate["pause_generation"],
            )
            worker.save_state(paused)
        except (OSError, ValueError, loop_mod.StateLoadError,
                parallel_contract.ParallelContractError) as exc:
            raise ParallelError(
                f"task-{order} paused worker checkpoint repair failed: {exc}") from exc

    def _project_reaped_worker_cancelled(self, order: int) -> None:
        worker = loop_mod.Workspace(
            self.artifacts.assignments[order]["worker_workspace"])
        try:
            state = worker.load_state()
        except FileNotFoundError:
            return
        try:
            parallel_worker.validate_persisted_state(state)
            assignment = state["assignment"]
            if assignment["status"] not in {
                    "running", "paused", "cancelled"}:
                return
            if assignment.get("gate_request") is not None:
                return
            worker.save_state(
                parallel_worker.mark_supervisor_cancelled(state))
        except (OSError, ValueError, loop_mod.StateLoadError,
                parallel_contract.ParallelContractError) as exc:
            raise ParallelError(
                f"task-{order} cancelled worker checkpoint repair failed: {exc}") from exc

    def _mark_active_pausing(self, *, abort: bool) -> None:
        receipts = self._receipts_by_task()
        for order in self.handles:
            task = self._task(order)
            uncertain_gate = task["resource_state"] in {
                "gate_claimed", "recovery_required",
            } and order not in receipts
            if abort and task["outcome"] != "integrated" and not uncertain_gate:
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, outcome="cancelled",
                    explicit_abort=True)
                task = self._task(order)
            if task["resource_state"] in {
                    "provisioning", "running", "gate_pending", "crashed"}:
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="pausing")
        self.checkpoint()

    def _settle_reaped_worker(
        self, order: int, *, status: str | None,
        assignment: Mapping[str, object] | None, abort: bool,
    ) -> bool:
        """Project a locally reaped child using receipts as sole merge proof."""
        task = self._task(order)
        has_receipt = self._receipt_authorizes_integration(order)
        if has_receipt:
            if task["outcome"] == "pending":
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, outcome="integrated")
            elif task["outcome"] != "integrated":
                self._block_run_for_task(
                    order, f"task-{order} receipt conflicts with {task['outcome']} outcome")
                return False
            task = self._task(order)
            if task["resource_state"] != "exited":
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="exited")
            return True

        if task["outcome"] == "integrated" or status == "integrated":
            self._block_run_for_task(
                order, f"task-{order} reports integrated without canonical receipt")
            task = self._task(order)
            if task["resource_state"] not in {
                    "gate_claimed", "recovery_required", "exited", "cleaning",
                    "cleaned", "cleanup_failed"}:
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="exited")
            return False

        task = self._task(order)
        if task["resource_state"] in {"gate_claimed", "recovery_required"}:
            if task["resource_state"] == "gate_claimed":
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="recovery_required")
            reason = (
                f"task-{order} claimed gate has no receipt; reconciliation required")
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, error=reason)
            self.aggregate["error"] = reason
            if self.aggregate["status"] != "blocked":
                self.aggregate = parallel_state.transition_run_status(
                    self.aggregate, "blocked")
            return False

        if status == "blocked" and not abort:
            reason = ((assignment or {}).get("exit_reason")
                      or "worker blocked while pausing")
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, outcome="blocked",
                resource_state="exited", error=reason)
            self.aggregate["error"] = reason
            return False
        if abort:
            self._project_reaped_worker_cancelled(order)
            task = self._task(order)
            if task["outcome"] != "cancelled":
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, outcome="cancelled",
                    explicit_abort=True)
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="exited")
            return True

        current = self._task(order)["resource_state"]
        if current != "pausing":
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="pausing")
        self._project_reaped_worker_paused(order)
        self.aggregate = parallel_state.transition_task(
            self.aggregate, order, resource_state="paused")
        return True

    def _wait_for_quiescence(self, *, abort: bool) -> bool:
        self._mark_active_pausing(abort=abort)
        safe = True
        for order in list(self.handles):
            self._request_worker_stop(order, abort=abort)
        deadline = time.monotonic() + 30.0
        while self.handles and time.monotonic() < deadline:
            for order, handle in list(self.handles.items()):
                if handle.process.poll() is None:
                    continue
                returncode = int(handle.process.wait())
                self._close_worker_control(handle)
                compat.close_process_group(handle.process)
                try:
                    self._terminalize_child_from_wait(handle, returncode)
                except ParallelError as exc:
                    self._block_run_for_task(order, str(exc))
                    safe = False
                    self.checkpoint()
                    continue
                del self.handles[order]
                status, assignment = self._load_worker_assignment(order)
                safe = self._settle_reaped_worker(
                    order, status=status, assignment=assignment,
                    abort=abort) and safe
                self.checkpoint()
            if self.handles:
                time.sleep(0.05)
        for order, handle in list(self.handles.items()):
            fence_ok = True
            try:
                returncode = self._fence_worker_handle(handle)
                self._terminalize_child_from_wait(handle, returncode)
            except (OSError, subprocess.SubprocessError, ParallelError) as exc:
                self._block_run_for_task(
                    order, f"task-{order} child fence/reap proof failed: {exc}")
                safe = False
                fence_ok = False
            if not fence_ok:
                self.checkpoint()
                continue
            del self.handles[order]
            status, assignment = self._load_worker_assignment(order)
            safe = self._settle_reaped_worker(
                order, status=status, assignment=assignment,
                abort=abort) and safe
            self.checkpoint()
        return safe

    def pause(self) -> int:
        self._cancel_pending_launches()
        self._cancel_pending_gates(abort=False)
        safe = self._wait_for_quiescence(abort=False)
        # Close the publish-after-first-scan race.  The child is now reaped, so
        # every late pending request must already be durably retained in its
        # worker checkpoint and can be cancelled without another spawn.
        self._cancel_pending_gates(abort=False, recovery=True)
        for original in list(self.aggregate["tasks"]):
            order = original["order"]
            task = self._task(order)
            if task["resource_state"] == "crashed":
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="pausing")
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="paused")
            elif task["resource_state"] in {
                    "gate_claimed", "recovery_required"}:
                self._block_run_for_task(
                    order,
                    f"task-{order} gate 尚未完成 recovery，不能投影 paused",
                )
                safe = False
        if (not safe or self.aggregate["status"] == "blocked"
                or any(task["outcome"] == "blocked"
                       for task in self.aggregate["tasks"])):
            if self.aggregate["status"] != "blocked":
                self.aggregate = parallel_state.transition_run_status(
                    self.aggregate, "blocked")
            # A failed fence leaves this process as the last known owner.
            # Preserve a stale active projection so recovery must prove the
            # exact child/guardian is gone instead of trusting a false idle.
            self.checkpoint(active=bool(self.handles))
            return 2
        self.aggregate = parallel_state.transition_run_status(
            self.aggregate, "paused")
        self.checkpoint(active=False)
        return 0

    def quiesce_blocked(self) -> None:
        """Fence every locally-owned child before relinquishing run authority."""
        self._cancel_pending_launches()
        self._cancel_pending_gates(abort=False)
        self._wait_for_quiescence(abort=False)
        self._cancel_pending_gates(abort=False, recovery=True)
        if self.handles:
            raise ParallelError("blocked run still owns unreaped worker handles")
        self.checkpoint(active=False)

    def _cleanup_terminal_task(self, order: int) -> None:
        task = self._task(order)
        if task["resource_state"] != "exited":
            return
        outcome = task["outcome"]
        self.aggregate = parallel_state.transition_task(
            self.aggregate, order, resource_state="cleaning")
        self.checkpoint()
        try:
            observation = self.executor.observe_worktree(order)
            if (not observation["exists"] and not observation["registered"]
                    and observation.get("task_ref_tip") is None):
                # Provisioning can fail before CREATE_WORKTREE publishes a resource.
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="cleaned")
                self.checkpoint()
                return
            self._execute({
                "operation": repo_executor.Operation.REMOVE_WORKTREE.value,
                "operation_id": _operation_id(
                    self.run_id, "remove", order, self.generation),
                "task": order,
                "authority": {
                    "manifest_hash": self.artifacts.manifest_hash,
                    "assignment_hash": self.artifacts.assignment_hashes[order],
                },
                "expected": {
                    "terminal_outcome": outcome,
                    "observation_token": observation["observation_token"],
                },
            })
        except (ParallelError, repo_executor.RepoExecutorError) as exc:
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="cleanup_failed",
                error=str(exc))
            self.checkpoint()
            raise ParallelError(f"task-{order} cleanup failed：{exc}") from exc
        self.aggregate = parallel_state.transition_task(
            self.aggregate, order, resource_state="cleaned")
        self.checkpoint()

    def _durable_finalize(self, terminal_status: str) -> None:
        """Replay a deterministic report and at-least-once notify outbox."""
        if terminal_status not in {"completed", "cancelled"}:
            raise ParallelError("finalization terminal status 不合法")
        fields = {
            "schema", "run_id", "terminal_status", "event_id", "created_at",
            "report_hash", "report_written", "notify_required",
            "notify_acked", "state",
        }
        finalization_path = self.artifacts.run_dir / "finalization.json"
        try:
            finalization_path.lstat()
            finalization_exists = True
        except FileNotFoundError:
            finalization_exists = False
        except OSError as exc:
            raise ParallelError(f"finalization outbox 無法檢查：{exc}") from exc
        try:
            record = (parallel_state.read_canonical_json(
                self.artifacts.run_dir, "finalization.json")
                      if finalization_exists else None)
            if record is None:
                record = {
                    "schema": 1,
                    "run_id": self.run_id,
                    "terminal_status": terminal_status,
                    "event_id": parallel_state.canonical_json_hash({
                        "kind": "parallel-finalization",
                        "run_id": self.run_id,
                        "terminal_status": terminal_status,
                    })[:32],
                    "created_at": datetime.now().astimezone().isoformat(
                        timespec="seconds"),
                    "report_hash": None,
                    "report_written": False,
                    "notify_required": bool(
                        self.artifacts.run_config["notify_cmd"]),
                    "notify_acked": not bool(
                        self.artifacts.run_config["notify_cmd"]),
                    "state": "pending",
                }
                parallel_state.atomic_write_json(
                    self.artifacts.run_dir, "finalization.json", record)
        except parallel_state.ParallelStateError as exc:
            raise ParallelError(f"finalization outbox 無法讀取：{exc}") from exc
        if (not isinstance(record, dict) or set(record) != fields
                or record.get("schema") != 1
                or record.get("run_id") != self.run_id
                or record.get("terminal_status") != terminal_status
                or record.get("event_id") != parallel_state.canonical_json_hash({
                    "kind": "parallel-finalization", "run_id": self.run_id,
                    "terminal_status": terminal_status,
                })[:32]
                or not isinstance(record.get("created_at"), str)
                or not record["created_at"]
                or record.get("state") not in {"pending", "complete"}
                or not isinstance(record.get("report_written"), bool)
                or not isinstance(record.get("notify_required"), bool)
                or not isinstance(record.get("notify_acked"), bool)
                or record["notify_required"] != bool(
                    self.artifacts.run_config["notify_cmd"])
                or (record["report_hash"] is not None
                    and (not isinstance(record["report_hash"], str)
                         or len(record["report_hash"]) != 64
                         or any(ch not in "0123456789abcdef"
                                for ch in record["report_hash"])) )):
            raise ParallelError("finalization outbox authority/schema 不符")

        state = self.workspace.load_state()
        report_path = loop_mod.write_run_report(
            Path(self.artifacts.run_config["primary_repo"]),
            self.workspace, state,
            ended_at=record["created_at"], run_status=terminal_status,
        )
        report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
        if record["report_hash"] not in {None, report_hash}:
            raise ParallelError("finalization report 與 durable hash 分歧")
        if not record["report_written"] or record["report_hash"] is None:
            record["report_hash"] = report_hash
            record["report_written"] = True
            parallel_state.atomic_write_json(
                self.artifacts.run_dir, "finalization.json", record)

        if record["notify_required"] and not record["notify_acked"]:
            delivered = loop_mod.notify(
                self.artifacts.run_config["notify_cmd"], terminal_status,
                self.workspace.name, event_id=record["event_id"])
            if not delivered:
                raise ParallelError(
                    "finalization notify 尚未 ack；Resume 將以同一 event_id 重播")
            record["notify_acked"] = True
            parallel_state.atomic_write_json(
                self.artifacts.run_dir, "finalization.json", record)
        record["state"] = "complete"
        parallel_state.atomic_write_json(
            self.artifacts.run_dir, "finalization.json", record)

    def _audit_durable_finalization(self, terminal_status: str) -> None:
        """Read-only proof that report/outbox completed before SHUTDOWN."""
        if terminal_status not in {"completed", "cancelled"}:
            raise ParallelError("finalization audit terminal status 不合法")
        try:
            record = parallel_state.read_canonical_json(
                self.artifacts.run_dir, "finalization.json")
        except parallel_state.ParallelStateError as exc:
            raise ParallelError(
                f"terminal finalization outbox audit blocked：{exc}") from exc
        fields = {
            "schema", "run_id", "terminal_status", "event_id", "created_at",
            "report_hash", "report_written", "notify_required",
            "notify_acked", "state",
        }
        expected_event = parallel_state.canonical_json_hash({
            "kind": "parallel-finalization",
            "run_id": self.run_id,
            "terminal_status": terminal_status,
        })[:32]
        if (not isinstance(record, dict) or set(record) != fields
                or record.get("schema") != 1
                or record.get("run_id") != self.run_id
                or record.get("terminal_status") != terminal_status
                or record.get("event_id") != expected_event
                or not isinstance(record.get("created_at"), str)
                or not record["created_at"]
                or record.get("state") != "complete"
                or record.get("report_written") is not True
                or record.get("notify_required") != bool(
                    self.artifacts.run_config["notify_cmd"])
                or record.get("notify_acked") is not True
                or not isinstance(record.get("report_hash"), str)
                or len(record["report_hash"]) != 64
                or any(ch not in "0123456789abcdef"
                       for ch in record["report_hash"])):
            raise ParallelError(
                "terminal finalization outbox authority/schema incomplete")
        report_path = self.workspace.dir / "REPORT.md"
        try:
            report = loop_mod.read_regular_bytes(report_path, "REPORT.md")
        except (OSError, ValueError) as exc:
            raise ParallelError(
                f"terminal report audit blocked：{exc}") from exc
        if hashlib.sha256(report).hexdigest() != record["report_hash"]:
            raise ParallelError(
                "terminal report hash does not match finalization outbox")

    def abort(self) -> int:
        self._cancel_pending_launches()
        self._cancel_pending_gates(abort=True)
        safe = self._wait_for_quiescence(abort=True)
        self._cancel_pending_gates(abort=True, recovery=True)
        for task in self.aggregate["tasks"]:
            if task["resource_state"] in {
                    "gate_claimed", "recovery_required"}:
                self._block_run_for_task(
                    task["order"],
                    f"task-{task['order']} gate 尚未完成 recovery，不能 Abort cleanup",
                )
                safe = False
        if not safe or self.aggregate["status"] == "blocked":
            if self.aggregate["status"] != "blocked":
                self.aggregate = parallel_state.transition_run_status(
                    self.aggregate, "blocked")
            self.checkpoint(active=bool(self.handles))
            return 2
        for original in list(self.aggregate["tasks"]):
            order = original["order"]
            task = self._task(order)
            if task["resource_state"] == "queued":
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, outcome="cancelled",
                    resource_state="cleaned", explicit_abort=True)
                self.checkpoint()
                continue
            if task["outcome"] not in {"integrated", "cancelled"}:
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, outcome="cancelled",
                    explicit_abort=True)
                task = self._task(order)
            if task["resource_state"] in {
                    "paused", "crashed", "provisioning", "running",
                    "gate_pending", "pausing"}:
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="exited")
                self.checkpoint()
            self._cleanup_terminal_task(order)
        if not all(
                task["resource_state"] in {"cleaned", "cleanup_failed"}
                for task in self.aggregate["tasks"]):
            self.aggregate["error"] = "Abort 尚有未完成 resource cleanup"
            self.aggregate = parallel_state.transition_run_status(
                self.aggregate, "blocked")
            self.checkpoint(active=False)
            return 2
        if any(task["resource_state"] == "cleanup_failed"
               for task in self.aggregate["tasks"]):
            self.aggregate["error"] = "Abort resource cleanup failed"
            self.aggregate = parallel_state.transition_run_status(
                self.aggregate, "blocked")
            self.checkpoint(active=False)
            return 2
        self._audit_terminal_receipt_projection()
        self._archive_terminal_worker_workspaces()
        self._audit_terminal_worker_archives()
        self.aggregate = parallel_state.transition_run_status(
            self.aggregate, "finalizing_cancel")
        self.checkpoint()
        self._durable_finalize("cancelled")
        self._audit_durable_finalization("cancelled")
        self._execute({
            "operation": repo_executor.Operation.SHUTDOWN.value,
            "operation_id": _operation_id(
                self.run_id, "shutdown", self.generation),
            "authority": {
                "supervisor_session": self.session,
                "generation": self.generation,
            },
            "expected": {"idle": True},
        })
        self.aggregate = parallel_state.transition_run_status(
            self.aggregate, "cancelled")
        self.checkpoint(active=False)
        return 0

    def _create_worktree(self, order: int, *, base_sha: str | None = None) -> None:
        assignment_hash = self.artifacts.assignment_hashes[order]
        self._execute({
            "operation": repo_executor.Operation.CREATE_WORKTREE.value,
            "operation_id": _operation_id(self.run_id, "create", order),
            "task": order,
            "authority": {
                "manifest_hash": self.artifacts.manifest_hash,
                "assignment_hash": assignment_hash,
            },
            "expected": {
                "base_sha": base_sha or self._expected_tip(),
                "task_ref_absent": True,
                "worktree_absent": True,
            },
        })

    def _ensure_worker_workspace_container(self, order: int) -> None:
        """Materialize the audit container before a payload can cross ACK."""
        assignment = self.artifacts.assignments[order]
        try:
            parallel_state.write_or_verify_immutable_json(
                self.artifacts.run_dir,
                f"workspace-containers/task-{order}.json",
                self._worker_container_authority(order),
            )
        except (OSError, ValueError, parallel_state.ParallelStateError) as exc:
            raise ParallelError(
                f"task-{order} worker container authority publish failed: {exc}"
            ) from exc
        worker = loop_mod.Workspace(assignment["worker_workspace"])
        expected = Path(assignment["worker_workspace_path"])
        try:
            actual = worker.dir.resolve(strict=True)
            canonical_expected = expected.resolve(strict=True)
        except OSError as exc:
            raise ParallelError(
                f"task-{order} worker workspace 無法 canonicalize：{exc}") from exc
        if actual != canonical_expected:
            raise ParallelError(
                f"task-{order} worker workspace path 不符 immutable assignment")
        parallel_state._fsync_directory(expected)
        parallel_state._fsync_directory(expected.parent)

    def _recover_or_create_unstarted_worktree(self, order: int) -> None:
        """Prove a pre-spawn CREATE result, or create the resource once.

        A supervisor can die after CREATE_WORKTREE commits but before a worker
        checkpoint exists.  Reconstruct the original request from the exact
        observed task-ref tip so RepoExecutor's operation-id/request hash check
        proves the existing worktree came from that durable operation.
        """
        try:
            observation = self.executor.observe_worktree(order)
        except repo_executor.RepoExecutorError as exc:
            raise ParallelError(
                f"task-{order} provisioning observation blocked：{exc}") from exc
        exists = observation.get("exists") is True
        registered = observation.get("registered") is True
        if exists != registered:
            raise ParallelError(
                f"task-{order} provisioning path/registry identity is partial")
        if not exists and observation.get("task_ref_tip") is not None:
            raise ParallelError(
                f"task-{order} provisioning has an orphan task ref")
        if not exists:
            self._create_worktree(order)
            return
        expected_ref = self.artifacts.assignments[order]["task_ref"]
        base_sha = observation.get("head")
        if (not isinstance(base_sha, str) or not base_sha
                or observation.get("head_ref") != expected_ref
                or observation.get("status")
                or observation.get("locked")
                or observation.get("live_locks")):
            raise ParallelError(
                f"task-{order} recovered provisioning worktree invariant failed")
        self._create_worktree(order, base_sha=base_sha)

    @staticmethod
    def _close_worker_control(handle: WorkerHandle) -> None:
        control = handle.control
        if control is not None:
            try:
                control.close()
            except OSError:
                pass
            handle.control = None

    def _child_record_for(self, handle: WorkerHandle) -> dict:
        try:
            return parallel_child.read_child_record(
                self.artifacts.run_dir, handle.order,
                handle.reservation["request_id"],
            )
        except parallel_child.ParallelChildError as exc:
            raise ParallelError(
                f"task-{handle.order} durable child record 不合法：{exc}") from exc

    def _terminalize_child_from_wait(
        self, handle: WorkerHandle, returncode: int,
    ) -> dict:
        """Require or publish terminal child evidence after local wait()."""
        record = self._child_record_for(handle)
        if record["state"] != "reaped":
            if record["state"] != "guardian_ready":
                raise ParallelError(
                    f"task-{handle.order} guardian exited after ACK without durable reap proof")
            reaped = dict(record)
            reaped["state"] = "reaped"
            reaped["returncode"] = int(returncode)
            try:
                parallel_child.write_child_record(
                    self.artifacts.run_dir, reaped)
            except parallel_child.ParallelChildError as exc:
                raise ParallelError(
                    f"task-{handle.order} child reap publication 失敗：{exc}") from exc
            record = self._child_record_for(handle)
        if record["returncode"] != int(returncode):
            raise ParallelError(
                f"task-{handle.order} guardian returncode 與 durable record 分歧")
        return record

    def _fence_worker_handle(self, handle: WorkerHandle) -> int:
        """Close the parent lease, then wait/interrupt/kill the guardian."""
        self._close_worker_control(handle)
        try:
            # Do not SIGKILL the guardian: its payload uses a distinct process
            # group.  EOF is the guardian protocol and only the guardian may
            # publish acked -> reaped after fencing that group.
            return int(compat.wait_process(handle.process))
        finally:
            compat.close_process_group(handle.process)

    def _wait_for_child_ack(self, handle: WorkerHandle) -> dict:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            record = self._child_record_for(handle)
            if record["state"] in {"acked", "reaped"}:
                return record
            if handle.process.poll() is not None:
                break
            time.sleep(0.01)
        raise ParallelError(
            f"task-{handle.order} guardian 未建立 durable ACK barrier")

    def dispatch(self, order: int, *, resume: bool = False) -> None:
        task = self._task(order)
        if task["outcome"] != "pending":
            raise ParallelError(f"task-{order} outcome 已非 pending，不可派工")
        if task["batch"] != self.aggregate["batch"]:
            raise ParallelError(f"task-{order} 不屬於目前 batch")
        effective_resume = resume
        if resume:
            worker_status, _worker_assignment = self._load_worker_assignment(order)
            if worker_status is None:
                effective_resume = False
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="provisioning",
                explicit_resume=True)
            if not effective_resume:
                self.checkpoint()
                self._recover_or_create_unstarted_worktree(order)
        else:
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="provisioning")
            self.checkpoint()
            self._create_worktree(order)
        # Create the workspace directory before publishing/releasing the child
        # ACK barrier.  Therefore any durable payload identity has a canonical
        # audit container to archive, even if Abort wins before loop.py starts.
        self._ensure_worker_workspace_container(order)
        self.checkpoint()
        task = self._task(order)
        reservation = None
        handle = None
        process = None
        child_record_published = False
        try:
            reservation = publish_launch_reservation(
                self.artifacts, order,
                supervisor_session=self.session,
                supervisor_generation=self.generation,
                attempt=task["restart_count"],
                resume=effective_resume,
            )
            assignment = _assignment_for_worker(self.artifacts, order)
            token = parallel_state.read_dispatch_token(
                self.artifacts.run_dir, order,
                expected_hash=assignment["dispatch_token_hash"],
            )
            argv = build_worker_argv(
                python_executable=sys.executable,
                assignment=assignment,
                run_config=self.artifacts.run_config,
                plan_path=self.artifacts.run_dir / "plan.json",
                dispatch_token=token,
                launch_reservation=reservation,
                resume=effective_resume,
            )
            guardian_argv = parallel_child.build_guardian_argv(
                sys.executable, self.artifacts.run_dir, order,
                reservation["request_id"], argv)
            process = subprocess.Popen(
                guardian_argv,
                cwd=str(Path(__file__).resolve().parent.parent),
                env=self._worker_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **compat.popen_group_kwargs(),
            )
            if process.stdin is None:
                raise ParallelError("guardian control pipe 建立失敗")
            if compat.attach_process_group(process) is not True:
                raise ParallelError("guardian process-group containment 建立失敗")
            ready = parallel_child.child_record(
                run_id=self.run_id,
                task=order,
                child_id=reservation["request_id"],
                supervisor_session=self.session,
                supervisor_generation=self.generation,
                attempt=reservation["attempt"],
                resume=effective_resume,
                guardian_pid=process.pid,
                argv_hash=parallel_child.payload_argv_hash(argv),
                state="guardian_ready",
            )
            parallel_child.write_child_record(
                self.artifacts.run_dir, ready)
            child_record_published = True
            handle = WorkerHandle(
                order, process, process.stdin, reservation, effective_resume)
            self.handles[order] = handle
            process.stdin.write(parallel_child.ACK_BYTE)
            process.stdin.flush()
            self._wait_for_child_ack(handle)
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="running")
            self.checkpoint()
        except BaseException as exc:
            if handle is None and process is not None:
                handle = WorkerHandle(
                    order, process, process.stdin, reservation, effective_resume)
            if handle is not None:
                # Ownership remains registered until fence/reap proof succeeds.
                # A failed rollback must keep the handle visible so the outer
                # blocked-quiesce path cannot publish an inactive owner.
                self.handles[order] = handle
                try:
                    returncode = self._fence_worker_handle(handle)
                    if child_record_published:
                        self._terminalize_child_from_wait(handle, returncode)
                    self.handles.pop(order, None)
                except (OSError, subprocess.SubprocessError, ParallelError) as fence_exc:
                    task = self._task(order)
                    if task["resource_state"] in {
                            "provisioning", "running", "gate_pending",
                            "pausing", "crashed"}:
                        self.aggregate = parallel_state.transition_task(
                            self.aggregate, order,
                            resource_state="recovery_required",
                            error=str(fence_exc))
                        self.checkpoint()
                    raise ParallelError(
                        f"task-{order} dispatch rollback 無法證明 child 已 fence："
                        f"{fence_exc}") from exc
            if reservation is not None:
                try:
                    cancel_launch_reservation(
                        self.artifacts.run_dir, reservation["request_id"])
                except ParallelError as cancel_exc:
                    raise ParallelError(
                        f"task-{order} dispatch rollback 無法取消 launch："
                        f"{cancel_exc}") from exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, ParallelError):
                raise
            raise ParallelError(f"task-{order} worker spawn 失敗：{exc}") from exc

    def _validate_gate_request(
        self, record, *, recovery: bool = False,
        historical: bool = False,
    ) -> tuple[int, dict]:
        request = record.payload
        fields = {
            "schema", "run_id", "task", "request_id", "validated_sha",
            "validated_round", "run_config_hash", "launch_spec_hash",
            "manifest_hash", "deadline_at",
        }
        if not isinstance(request, dict) or set(request) != fields:
            raise ParallelError("gate request schema 不符")
        order = request.get("task")
        if (not isinstance(order, int) or isinstance(order, bool)
                or order not in self.artifacts.assignments):
            raise ParallelError("gate request task 不合法")
        assignment = self.artifacts.assignments[order]
        expected = {
            "schema": 1,
            "run_id": self.run_id,
            "task": order,
            "request_id": record.request_id,
            "run_config_hash": self.artifacts.run_config_hash,
            "launch_spec_hash": self.artifacts.assignment_hashes[order],
            "manifest_hash": self.artifacts.manifest_hash,
        }
        for field, value in expected.items():
            if request.get(field) != value:
                raise ParallelError(f"gate request {field} authority 不符")
        parallel_contract.require_git_sha(request["validated_sha"])
        _require_positive_integer(request["validated_round"], "validated_round")
        if not isinstance(request["deadline_at"], str) or not request["deadline_at"]:
            raise ParallelError("gate request deadline_at 不合法")
        task = self._task(order)
        if recovery and historical:
            raise ParallelError(
                "gate request validation mode must be recovery or historical")
        if historical:
            # Historical spool evidence outlives the task resource.  Its
            # cryptographic/run authority remains auditable after a worktree
            # has reached ``cleaned`` and must not be coupled to the current
            # scheduling projection.
            pass
        elif recovery:
            if (task["outcome"] not in {"pending", "integrated"}
                    or task["resource_state"] not in {
                        "provisioning", "running", "gate_pending",
                        "gate_claimed", "pausing", "crashed",
                        "recovery_required", "exited"}):
                raise ParallelError(
                    "recovery gate request 不符合 task 目前 aggregate 狀態")
        elif (task["outcome"] != "pending"
              or task["batch"] != self.aggregate["batch"]
              or task["resource_state"] not in {"running", "gate_pending"}):
            raise ParallelError("gate request 不符合 task 目前 aggregate 狀態")
        return order, request

    def _publish_gate_response(
        self, request: Mapping[str, object], *, returncode: int,
        status: str, reason: str | None = None,
    ) -> None:
        envelope = parallel_gate.durable_response_envelope(
            request, returncode=returncode, status=status, reason=reason)
        self.gate_spool.publish_response(str(request["request_id"]), envelope)

    def process_gate_requests(self) -> bool:
        progressed = False
        for record in self.gate_spool.list_requests("pending"):
            order, request = self._validate_gate_request(record)
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="gate_pending")
            self.checkpoint()
            claimed = self.gate_spool.claim_request(record.request_id)
            if not claimed.transitioned:
                continue
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="gate_claimed")
            self.checkpoint()
            before = self._expected_tip()
            request_hash = repo_executor.canonical_hash(request)
            try:
                result = self._execute({
                    "operation": repo_executor.Operation.GATE_MERGE.value,
                    "operation_id": repo_executor.gate_operation_id(
                        self.run_id, record.request_id),
                    "task": order,
                    "authority": {
                        "manifest_hash": self.artifacts.manifest_hash,
                        "assignment_hash": self.artifacts.assignment_hashes[order],
                        "request_hash": request_hash,
                    },
                    "expected": {
                        "request_id": record.request_id,
                        "validated_sha": request["validated_sha"],
                        "validated_round": request["validated_round"],
                        "integration_before": before,
                        "sync_before": before,
                    },
                })
            except ParallelError as exc:
                self.aggregate = parallel_state.transition_task(
                    self.aggregate,
                    order,
                    resource_state="recovery_required",
                    error=str(exc),
                )
                self.aggregate["error"] = str(exc)
                self.aggregate = parallel_state.transition_run_status(
                    self.aggregate, "blocked")
                self.checkpoint()
                # rc=31 is a local client timeout/result, not a terminal spool
                # response.  Leaving the claimed request response-less lets an
                # authorized recovery owner publish the eventual exact
                # success/stale/fatal result without overwriting immutable
                # evidence.
                return True
            status = result["status"]
            if status in {"merged", "already-merged"}:
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, outcome="integrated")
                self.checkpoint()
                self._publish_gate_response(
                    request, returncode=0, status=status)
            elif status == "stale-integration":
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="running")
                self.checkpoint()
                self._publish_gate_response(
                    request, returncode=10, status=status,
                    reason="integration 已由同 batch 的合法 receipt 前進")
            else:
                raise ParallelError(f"RepoExecutor gate 回傳未知 status：{status!r}")
            progressed = True
        return progressed

    def _load_worker_assignment(self, order: int) -> tuple[str | None, dict | None]:
        source, archive, source_exists, archive_exists = (
            self._worker_workspace_locations(order))
        if not source_exists and not archive_exists:
            return None, None
        path = (archive if archive_exists else source) / "state.json"
        try:
            state, _raw, _recovered = loop_mod.load_checkpointed_state(
                path, repair=False)
            parallel_worker.validate_persisted_state(state)
        except FileNotFoundError:
            return None, None
        except (OSError, ValueError, loop_mod.StateLoadError,
                parallel_contract.ParallelContractError) as exc:
            raise ParallelError(
                f"task-{order} worker checkpoint malformed：{exc}") from exc
        assignment = state.get("assignment")
        return assignment.get("status"), assignment

    def _task_child_records(self, order: int) -> tuple[dict, ...]:
        children_root = self.artifacts.run_dir / "children"
        if not self._checked_real_directory(
                children_root, "parallel children root"):
            return ()
        directory = children_root / f"task-{order}"
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise ParallelError(
                f"task-{order} child record directory 無法檢查：{exc}") from exc
        if (directory.is_symlink() or compat.is_reparse_point(info)
                or not directory.is_dir()):
            raise ParallelError(
                f"task-{order} child record directory 不是安全 real directory")
        records = []
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ParallelError(
                f"task-{order} child records 無法列舉：{exc}") from exc
        for entry in entries:
            if (entry.suffix != ".json" or len(entry.stem) != 32
                    or any(ch not in "0123456789abcdef" for ch in entry.stem)):
                raise ParallelError(
                    f"task-{order} children 含非 canonical artifact：{entry.name}")
            try:
                record = parallel_child.read_child_record(
                    self.artifacts.run_dir, order, entry.stem)
            except parallel_child.ParallelChildError as exc:
                raise ParallelError(
                    f"task-{order} child record 不合法：{exc}") from exc
            if record["run_id"] != self.run_id:
                raise ParallelError(f"task-{order} child record run_id 不符")
            records.append(record)
        return tuple(records)

    def _validated_launch_records(self) -> tuple[object, ...]:
        """Return all launch reservations after immutable authority checks."""
        spool = parallel_spool.DurableSpool(
            self.artifacts.run_dir / "launches")
        try:
            records = spool.list_requests()
            spool.list_responses()
        except parallel_spool.SpoolError as exc:
            raise ParallelError(f"worker launch spool audit failed: {exc}") from exc
        fields = {
            "schema", "request_id", "run_id", "task", "manifest_hash",
            "run_config_hash", "launch_spec_hash", "supervisor_session",
            "supervisor_generation", "attempt", "resume",
        }
        for record in records:
            payload = record.payload
            if not isinstance(payload, dict) or set(payload) != fields:
                raise ParallelError(
                    f"launch {record.request_id} reservation schema mismatch")
            order = payload.get("task")
            session = payload.get("supervisor_session")
            generation = payload.get("supervisor_generation")
            attempt = payload.get("attempt")
            if (payload.get("schema") != LAUNCH_RESERVATION_SCHEMA
                    or payload.get("request_id") != record.request_id
                    or payload.get("run_id") != self.run_id
                    or not isinstance(order, int) or isinstance(order, bool)
                    or order not in self.artifacts.assignments
                    or payload.get("manifest_hash") != self.artifacts.manifest_hash
                    or payload.get("run_config_hash") != self.artifacts.run_config_hash
                    or payload.get("launch_spec_hash")
                    != self.artifacts.assignment_hashes.get(order)
                    or not isinstance(session, str) or len(session) != 32
                    or any(ch not in "0123456789abcdef" for ch in session)
                    or not isinstance(generation, int)
                    or isinstance(generation, bool) or generation < 1
                    or not isinstance(attempt, int) or isinstance(attempt, bool)
                    or attempt < 0
                    or not isinstance(payload.get("resume"), bool)):
                raise ParallelError(
                    f"launch {record.request_id} reservation authority mismatch")
            expected_id = parallel_state.canonical_json_hash({
                "kind": "worker-launch",
                "run_id": self.run_id,
                "task": order,
                "supervisor_session": session,
                "supervisor_generation": generation,
                "attempt": attempt,
            })[:32]
            if record.request_id != expected_id:
                raise ParallelError(
                    f"launch {record.request_id} request id is not canonical")
        return records

    def _audit_terminal_launch_child_evidence(self, order: int) -> bool:
        """Cross-audit every terminal reservation, response and child record."""
        spool = parallel_spool.DurableSpool(
            self.artifacts.run_dir / "launches")
        launches = {
            record.request_id: record
            for record in self._validated_launch_records()
            if record.payload["task"] == order
        }
        children = {
            record["child_id"]: record
            for record in self._task_child_records(order)
        }
        orphan_children = sorted(set(children) - set(launches))
        if orphan_children:
            raise ParallelError(
                f"task-{order} child record lacks exact launch reservation: "
                + ",".join(orphan_children))
        authorized_count = 0
        for request_id, launch in launches.items():
            payload = launch.payload
            child = children.get(request_id)
            if launch.state == "pending":
                raise ParallelError(
                    f"task-{order} terminal launch remains pending: {request_id}")
            if launch.state == "claimed" and (
                    child is None or child.get("payload_pid") is None):
                raise ParallelError(
                    f"task-{order} claimed launch lacks reaped payload evidence: "
                    f"{request_id}")
            if child is not None:
                expected_child = {
                    "run_id": self.run_id,
                    "task": order,
                    "child_id": request_id,
                    "supervisor_session": payload["supervisor_session"],
                    "supervisor_generation": payload["supervisor_generation"],
                    "attempt": payload["attempt"],
                    "resume": payload["resume"],
                }
                if (child.get("state") != "reaped"
                        or any(child.get(field) != value
                               for field, value in expected_child.items())):
                    raise ParallelError(
                        f"task-{order} child does not match launch reservation: "
                        f"{request_id}")
            try:
                response = spool.get_response(request_id)
            except parallel_spool.SpoolError as exc:
                raise ParallelError(
                    f"task-{order} launch response audit failed: {exc}") from exc
            if response is None:
                continue
            if launch.state != "claimed" or child is None:
                raise ParallelError(
                    f"task-{order} launch response lacks claimed child authority")
            result = response.payload
            status = result.get("status") if isinstance(result, dict) else None
            if status == "authorized":
                expected = {
                    "schema": 1,
                    "request_id": request_id,
                    "status": "authorized",
                    "supervisor_session": payload["supervisor_session"],
                    "supervisor_generation": payload["supervisor_generation"],
                    "attempt": payload["attempt"],
                }
                if (set(result) != {*expected, "pid"}
                        or any(result.get(field) != value
                               for field, value in expected.items())
                        or not isinstance(result.get("pid"), int)
                        or isinstance(result.get("pid"), bool)
                        or result["pid"] < 2):
                    raise ParallelError(
                        f"task-{order} authorized launch response mismatch")
                authorized_count += 1
            elif status == "rejected":
                if (set(result) != {
                        "schema", "request_id", "status", "pid", "reason"}
                        or result.get("schema") != 1
                        or result.get("request_id") != request_id
                        or not isinstance(result.get("pid"), int)
                        or isinstance(result.get("pid"), bool)
                        or result["pid"] < 2
                        or not isinstance(result.get("reason"), str)
                        or not result["reason"]):
                    raise ParallelError(
                        f"task-{order} rejected launch response mismatch")
            else:
                raise ParallelError(
                    f"task-{order} launch response status is unknown")
        storage = self._worker_workspace_storage_dir(order)
        state_path = storage / "state.json"
        checkpoint_path = loop_mod.state_checkpoint_path(state_path)
        has_checkpoint = (
            os.path.lexists(state_path) or os.path.lexists(checkpoint_path))
        if has_checkpoint:
            state = self._audit_worker_checkpoint_authority(order)
            if (self._task(order)["outcome"] == "cancelled"
                    and state["assignment"].get("status") == "integrated"):
                raise ParallelError(
                    f"task-{order} cancelled outcome conflicts with worker checkpoint")
        requires_authorized = (
            self._task(order)["outcome"] == "integrated"
            or has_checkpoint
        )
        if requires_authorized and authorized_count < 1:
            raise ParallelError(
                f"task-{order} worker checkpoint lacks authorized launch response")
        return bool(launches)

    def _task_has_payload_evidence(self, order: int) -> bool:
        """Whether a guardian durably authorized payload release for the task."""
        spool = parallel_spool.DurableSpool(
            self.artifacts.run_dir / "launches")
        launches = {
            record.request_id: record
            for record in self._validated_launch_records()
            if record.payload["task"] == order
        }
        for child in self._task_child_records(order):
            payload_pid = child.get("payload_pid")
            if payload_pid is None:
                continue
            if "child_id" not in child:
                # Recovery unit doubles created before the launch journal was
                # introduced model an already-authorized payload with only its
                # durable PID.  Real records pass _task_child_records' strict
                # child schema and always contain child_id.
                return True
            launch = launches.get(child["child_id"])
            if launch is None or launch.state != "claimed":
                continue
            try:
                response = spool.get_response(child["child_id"])
            except parallel_spool.SpoolError as exc:
                raise ParallelError(
                    f"task-{order} launch response audit failed: {exc}") from exc
            if response is None:
                continue
            expected = {
                "schema": 1,
                "request_id": child["child_id"],
                "status": "authorized",
                "pid": payload_pid,
                "supervisor_session": launch.payload["supervisor_session"],
                "supervisor_generation": launch.payload["supervisor_generation"],
                "attempt": launch.payload["attempt"],
            }
            if response.payload == expected:
                return True
            if response.payload.get("status") == "authorized":
                raise ParallelError(
                    f"task-{order} authorized launch response mismatch")
        return False

    def _require_pristine_pre_payload_workspace(self, order: int) -> None:
        """Prove an unlaunched workspace is only the supervisor-made skeleton.

        The supervisor materializes this container before publishing a child
        reservation, so Abort can legitimately encounter a workspace without
        any child record.  With no payload identity, however, no process was
        authorized to add state or output.  Accept only Workspace's exact empty
        directory skeleton; anything else is unexplained audit evidence.
        """
        directory = self._worker_workspace_storage_dir(order)
        expected = {"logs", "prompts", "snapshots"}
        try:
            entries = {entry.name: entry for entry in directory.iterdir()}
        except OSError as exc:
            raise ParallelError(
                f"task-{order} pre-payload workspace cannot be enumerated: {exc}"
            ) from exc
        if set(entries) != expected:
            raise ParallelError(
                f"task-{order} pre-payload worker workspace is not pristine")
        for name in sorted(expected):
            child = entries[name]
            try:
                info = child.lstat()
            except OSError as exc:
                raise ParallelError(
                    f"task-{order} pre-payload {name} cannot be audited: {exc}"
                ) from exc
            if (stat.S_ISLNK(info.st_mode) or compat.is_reparse_point(info)
                    or not stat.S_ISDIR(info.st_mode)):
                raise ParallelError(
                    f"task-{order} pre-payload worker workspace is not pristine")
            try:
                contents = tuple(child.iterdir())
            except OSError as exc:
                raise ParallelError(
                    f"task-{order} pre-payload {name} cannot be audited: {exc}"
                ) from exc
            if contents:
                raise ParallelError(
                    f"task-{order} pre-payload worker workspace is not pristine")

    def _reconcile_orphan_children(
            self, order: int, *, wait_timeout: float = 5.0) -> None:
        """Wait for guardian EOF cleanup, then exact-fence any dead guardian.

        A replacement supervisor never adopts an old guardian.  The old
        guardian either observes parent-pipe EOF and publishes ``reaped``, or
        this recovery owner proves its exact birth identity is gone and uses
        the durable child-record CAS recovery path.
        """
        deadline = time.monotonic() + float(wait_timeout)
        while True:
            records = self._task_child_records(order)
            pending = [record for record in records
                       if record["state"] != "reaped"]
            if not pending:
                return
            waiting_for_guardian = False
            for record in pending:
                if compat.process_matches_identity(
                        record["guardian_pid"],
                        record["guardian_start_token"]):
                    waiting_for_guardian = True
                    continue
                try:
                    parallel_child.recover_orphan_child(
                        self.artifacts.run_dir, order, record["child_id"],
                        expected_record=record,
                    )
                except parallel_child.ParallelChildError as exc:
                    raise ParallelError(
                        f"task-{order} orphan guardian recovery blocked：{exc}"
                    ) from exc
            if time.monotonic() >= deadline:
                identities = ", ".join(
                    f"{record['child_id']}:{record['state']}"
                    for record in self._task_child_records(order)
                    if record["state"] != "reaped")
                raise ParallelError(
                    f"task-{order} guardian did not quiesce after owner loss："
                    f"{identities}")
            if waiting_for_guardian:
                time.sleep(0.02)

    def _require_reaped_child_evidence(self, order: int) -> None:
        records = self._task_child_records(order)
        unsafe = [record for record in records if record["state"] != "reaped"]
        if unsafe:
            identities = ", ".join(
                f"{record['child_id']}:{record['state']}" for record in unsafe)
            raise ParallelError(
                f"task-{order} 尚有未 terminal guardian evidence：{identities}")
        _source, _archive, source_exists, archive_exists = (
            self._worker_workspace_locations(order))
        if records or source_exists or archive_exists:
            self._audit_worker_container_authority(order)
        # An ACKed PID can still be an inert bootstrap whose launch reservation
        # lost the cancel-vs-claim race.  Only an exact claimed + authorized
        # response proves that GO was released and permits non-pristine worker
        # workspace evidence.
        has_payload = self._task_has_payload_evidence(order)
        if (self._task(order)["outcome"] == "integrated"
                and not has_payload):
            raise ParallelError(
                f"task-{order} integrated outcome lacks authorized launch "
                "response/payload evidence")
        if not source_exists and not archive_exists:
            if records:
                raise ParallelError(
                    f"task-{order} worker workspace evidence 遺失")
            return
        if not has_payload:
            self._require_pristine_pre_payload_workspace(order)
        try:
            owner = loop_mod.active_run_lock_owner(
                self._worker_workspace_storage_dir(order) / ".run.lock")
        except (OSError, ValueError) as exc:
            raise ParallelError(
                f"task-{order} worker lock 無法驗證：{exc}") from exc
        if owner is not None:
            raise ParallelError(f"task-{order} worker lock 仍由 live owner 持有")

    def _current_claimed_gate_record(
        self, order: int, receipt: Mapping[str, object] | None,
    ):
        records = []
        for record in self.gate_spool.list_requests("claimed"):
            if not isinstance(record.payload, Mapping):
                raise ParallelError("claimed gate request payload 必須是 object")
            if record.payload.get("task") != order:
                continue
            self._validate_gate_request(record, recovery=True)
            records.append(record)
        if receipt is not None:
            matches = [
                record for record in records
                if record.request_id == receipt["request_id"]]
        else:
            _status, assignment = self._load_worker_assignment(order)
            gate_request = ((assignment or {}).get("gate_request")
                            if isinstance(assignment, Mapping) else None)
            request_id = (gate_request.get("request_id")
                          if isinstance(gate_request, Mapping) else None)
            if request_id is not None:
                matches = [
                    record for record in records
                    if record.request_id == request_id]
            else:
                matches = []
                for record in records:
                    try:
                        response = self.gate_spool.get_response(record.request_id)
                    except parallel_spool.SpoolError as exc:
                        raise ParallelError(
                            f"task-{order} gate response 無法讀取：{exc}") from exc
                    if response is None:
                        matches.append(record)
        if len(matches) != 1:
            raise ParallelError(
                f"task-{order} claimed gate 無法唯一綁定 current request")
        return matches[0]

    def _project_recovered_worker_gate(
        self, order: int, request: Mapping[str, object], *, integrated: bool,
        cancelled: bool = False,
    ) -> None:
        worker_workspace = loop_mod.Workspace(
            self.artifacts.assignments[order]["worker_workspace"])
        worker_state = worker_workspace.load_state()
        try:
            parallel_worker.validate_persisted_state(worker_state)
            assignment = worker_state["assignment"]
            if integrated:
                already_projected = (
                    assignment.get("status") == "integrated"
                    and assignment.get("validated_sha") == request["validated_sha"]
                    and assignment.get("validated_round") == request["validated_round"]
                    and assignment.get("gate_request") is None
                )
                if already_projected:
                    return
                worker_state = parallel_worker.resolve_recovered_integrated_gate(
                    worker_state,
                    request_id=str(request["request_id"]),
                    validated_sha=str(request["validated_sha"]),
                    validated_round=int(request["validated_round"]),
                )
            else:
                already_projected = (
                    assignment.get("status")
                    == ("cancelled" if cancelled else "running")
                    and assignment.get("validated_sha") is None
                    and assignment.get("validated_round") is None
                    and assignment.get("gate_request") is None
                    and ((cancelled and isinstance(
                        assignment.get("exit_reason"), str))
                         or (not cancelled
                             and assignment.get("exit_reason") is None))
                )
                if already_projected:
                    return
                worker_state = parallel_worker.resolve_recovered_stale_gate(
                    worker_state,
                    request_id=str(request["request_id"]),
                    validated_sha=str(request["validated_sha"]),
                    validated_round=int(request["validated_round"]),
                )
                if cancelled:
                    worker_state["assignment"].update({
                        "status": "cancelled",
                        "exit_reason": "parent Abort after stale gate recovery",
                    })
                    parallel_worker.validate_persisted_state(worker_state)
        except (KeyError, TypeError, ValueError,
                parallel_contract.ParallelContractError) as exc:
            label = "integrated" if integrated else "stale"
            raise ParallelError(
                f"task-{order} {label} worker state recovery blocked：{exc}") from exc
        worker_workspace.save_state(worker_state)

    def _normalize_claimed_gate_resource(
            self, order: int, *, explicit_abort: bool = False) -> None:
        """Recover aggregate lag without weakening claimed-request authority."""
        task = self._task(order)
        if task["outcome"] == "integrated":
            return
        resource = task["resource_state"]
        if explicit_abort and resource in {
                "provisioning", "running", "gate_pending", "pausing",
                "crashed"}:
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="recovery_required")
            resource = "recovery_required"
        if explicit_abort:
            if resource not in {"gate_claimed", "recovery_required"}:
                raise ParallelError(
                    f"task-{order} claimed Abort recovery conflicts with {resource}")
            self.checkpoint()
            return
        if resource == "provisioning":
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="running")
            resource = "running"
        if resource == "running":
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="gate_pending")
            resource = "gate_pending"
        if resource == "gate_pending":
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="gate_claimed")
            resource = "gate_claimed"
        if resource in {"pausing", "crashed"}:
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="recovery_required")
            resource = "recovery_required"
        if resource == "recovery_required":
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="gate_claimed")
            resource = "gate_claimed"
        if resource != "gate_claimed":
            raise ParallelError(
                f"task-{order} claimed request 與 resource {resource} 衝突")
        self.checkpoint()

    def _reconcile_cancelled_gate(self, order: int, record) -> None:
        request = record.payload
        if self._receipt_authorizes_integration(order):
            raise ParallelError(
                f"task-{order} cancelled gate request 不可帶 canonical receipt")
        try:
            response = self.gate_spool.get_response(record.request_id)
        except parallel_spool.SpoolError as exc:
            raise ParallelError(
                f"task-{order} cancelled gate response 無法讀取：{exc}") from exc
        if response is None:
            self._publish_gate_response(
                request, returncode=11,
                status="supervisor-lost-before-claim",
                reason="recovery owner 證明 request 已 durable cancelled；可安全重試",
            )
        else:
            try:
                rc, payload = parallel_gate._validate_durable_response(
                    response, request)
            except parallel_gate.GateClientError as exc:
                raise ParallelError(
                    f"task-{order} cancelled gate response 不合法：{exc}") from exc
            if (rc, payload.get("status")) not in {
                    (11, "busy"), (11, "supervisor-lost-before-claim"),
                    (20, "paused"), (21, "cancelled")}:
                raise ParallelError(
                    f"task-{order} cancelled request 帶非 cancelled terminal response")
        if not self._task_child_records(order):
            raise ParallelError(
                f"task-{order} retained gate 缺少 child identity/reap evidence")
        self._require_reaped_child_evidence(order)
        self._project_recovered_worker_gate(order, request, integrated=False)
        resource = self._task(order)["resource_state"]
        if resource in {"provisioning", "running", "gate_pending", "crashed"}:
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="pausing")
            resource = "pausing"
        if resource == "pausing":
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="paused")
            resource = "paused"
        if resource == "recovery_required":
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="paused")
            resource = "paused"
        if resource != "paused":
            raise ParallelError(
                f"task-{order} cancelled gate 無法收斂 resource {resource}")
        self.checkpoint()

    def _reconcile_retained_gate(
        self, order: int, receipt: Mapping[str, object] | None,
        *, explicit_abort: bool = False,
    ) -> bool:
        _status, assignment = self._load_worker_assignment(order)
        gate_request = (assignment.get("gate_request")
                        if isinstance(assignment, Mapping) else None)
        if not isinstance(gate_request, Mapping):
            return False
        request_id = gate_request.get("request_id")
        if not isinstance(request_id, str):
            raise ParallelError(f"task-{order} retained gate request_id 不合法")
        try:
            record = self.gate_spool.get_request(request_id)
        except parallel_spool.SpoolError as exc:
            raise ParallelError(
                f"task-{order} retained gate request 無法讀取：{exc}") from exc
        if record is None:
            raise ParallelError(
                f"task-{order} worker retained gate 缺 durable request")
        self._validate_gate_request(record, recovery=True)
        if record.state == "pending":
            try:
                transition = self.gate_spool.cancel_request(request_id)
            except parallel_spool.SpoolError as exc:
                raise ParallelError(
                    f"task-{order} recovery cancel gate 失敗：{exc}") from exc
            record = transition.record
        if record.state == "cancelled":
            self._reconcile_cancelled_gate(order, record)
            return True
        if record.state == "claimed":
            self._normalize_claimed_gate_resource(
                order, explicit_abort=explicit_abort)
            self._reconcile_claimed_gate(
                order, receipt, explicit_abort=explicit_abort)
            return True
        raise ParallelError(
            f"task-{order} retained gate durable state 不合法：{record.state}")

    def _reconcile_claimed_gate(
        self, order: int, receipt: Mapping[str, object] | None,
        *, explicit_abort: bool = False,
    ) -> None:
        record = self._current_claimed_gate_record(order, receipt)
        request = record.payload
        # The old managed worker must be mechanically fenced before a recovery
        # owner may replay a gate transaction or mutate its checkpoint.
        if not self._task_child_records(order):
            raise ParallelError(
                f"task-{order} claimed gate 缺少 child identity/reap evidence")
        self._require_reaped_child_evidence(order)
        before = (receipt["integration_before"] if receipt is not None
                  else self._expected_tip())
        operation_request = {
            "operation": repo_executor.Operation.GATE_MERGE.value,
            "operation_id": repo_executor.gate_operation_id(
                self.run_id, record.request_id),
            "task": order,
            "authority": {
                "manifest_hash": self.artifacts.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[order],
                "request_hash": repo_executor.canonical_hash(request),
            },
            "expected": {
                "request_id": record.request_id,
                "validated_sha": request["validated_sha"],
                "validated_round": request["validated_round"],
                "integration_before": before,
                "sync_before": before,
            },
        }
        try:
            result = self.executor.reconcile_claimed_gate(operation_request)
        except (AttributeError, repo_executor.RepoExecutorError) as exc:
            raise ParallelError(
                f"RepoExecutor claimed gate recovery blocked：{exc}") from exc
        status = result.get("status")
        try:
            existing_response = self.gate_spool.get_response(record.request_id)
        except parallel_spool.SpoolError as exc:
            raise ParallelError(
                f"task-{order} gate response 無法讀取：{exc}") from exc
        if status in {"merged", "already-merged"}:
            receipts = self._receipts_by_task()
            if order not in receipts:
                raise ParallelError(
                    f"task-{order} gate success 沒有 canonical receipt")
            task = self._task(order)
            if task["outcome"] == "pending":
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, outcome="integrated")
            elif task["outcome"] != "integrated":
                raise ParallelError(
                    f"task-{order} receipt 與 {task['outcome']} outcome 衝突")
            if existing_response is None:
                self._publish_gate_response(
                    request, returncode=0, status=status)
            else:
                try:
                    rc, payload = parallel_gate._validate_durable_response(
                        existing_response, request)
                except parallel_gate.GateClientError as exc:
                    raise ParallelError(
                        f"task-{order} existing gate response 不合法：{exc}") from exc
                if (rc, payload.get("status")) != (0, status):
                    raise ParallelError(
                        f"task-{order} existing gate response 與 recovered success 衝突")
            self._require_reaped_child_evidence(order)
            self._project_recovered_worker_gate(
                order, request, integrated=True)
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="exited")
            return
        if status == "stale-integration":
            if existing_response is None:
                self._publish_gate_response(
                    request, returncode=10, status=status,
                    reason="integration 已由同 batch 其他 receipt 推進")
            else:
                try:
                    rc, payload = parallel_gate._validate_durable_response(
                        existing_response, request)
                except parallel_gate.GateClientError as exc:
                    raise ParallelError(
                        f"task-{order} existing gate response 不合法：{exc}") from exc
                if (rc, payload.get("status")) != (10, status):
                    raise ParallelError(
                        f"task-{order} existing gate response 與 recovered stale 衝突")
            task = self._task(order)
            if explicit_abort:
                self._require_reaped_child_evidence(order)
                self._project_recovered_worker_gate(
                    order, request, integrated=False, cancelled=True)
                if task["outcome"] != "cancelled":
                    self.aggregate = parallel_state.transition_task(
                        self.aggregate, order, outcome="cancelled",
                        explicit_abort=True)
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="exited")
                return
            if task["resource_state"] == "recovery_required":
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="gate_claimed")
            self._require_reaped_child_evidence(order)
            self._project_recovered_worker_gate(
                order, request, integrated=False)
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="running")
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="crashed",
                error="recovered stale gate requires worker resume")
            self.aggregate = parallel_state.increment_restart_count(
                self.aggregate, order,
                limit=self.artifacts.run_config["worker_restart_limit"])
            return
        raise ParallelError(
            f"task-{order} claimed gate recovery result 不合法：{status!r}")

    def _cleanup_integrated(self, order: int) -> None:
        task = self._task(order)
        if task["resource_state"] != "exited":
            return
        self.aggregate = parallel_state.transition_task(
            self.aggregate, order, resource_state="cleaning")
        self.checkpoint()
        try:
            observation = self.executor.observe_worktree(order)
            if (not observation["exists"] and not observation["registered"]
                    and observation.get("task_ref_tip") is None):
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="cleaned")
                self.checkpoint()
                return
            self._execute({
                "operation": repo_executor.Operation.REMOVE_WORKTREE.value,
                "operation_id": _operation_id(
                    self.run_id, "remove", order, self.generation),
                "task": order,
                "authority": {
                    "manifest_hash": self.artifacts.manifest_hash,
                    "assignment_hash": self.artifacts.assignment_hashes[order],
                },
                "expected": {
                    "terminal_outcome": "integrated",
                    "observation_token": observation["observation_token"],
                },
            })
        except (ParallelError, repo_executor.RepoExecutorError) as exc:
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="cleanup_failed",
                error=str(exc))
            self.checkpoint()
            raise ParallelError(f"task-{order} cleanup failed：{exc}") from exc
        self.aggregate = parallel_state.transition_task(
            self.aggregate, order, resource_state="cleaned")
        self.checkpoint()

    def _reconcile_cleanup(self, order: int) -> None:
        task = self._task(order)
        if task["resource_state"] == "cleanup_failed":
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="cleaning",
                cleanup_retry=True, error=None)
            self.checkpoint()
        elif task["resource_state"] != "cleaning":
            return
        try:
            observation = self.executor.observe_worktree(order)
            if (observation["exists"] or observation["registered"]
                    or observation.get("task_ref_tip") is not None):
                self._execute({
                    "operation": repo_executor.Operation.REMOVE_WORKTREE.value,
                    "operation_id": _operation_id(
                        self.run_id, "remove", order, self.generation),
                    "task": order,
                    "authority": {
                        "manifest_hash": self.artifacts.manifest_hash,
                        "assignment_hash": self.artifacts.assignment_hashes[order],
                    },
                    "expected": {
                        "terminal_outcome": self._task(order)["outcome"],
                        "observation_token": observation["observation_token"],
                    },
                })
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="cleaned")
            self.checkpoint()
        except (ParallelError, repo_executor.RepoExecutorError) as exc:
            self.aggregate = parallel_state.transition_task(
                self.aggregate, order, resource_state="cleanup_failed",
                error=str(exc))
            self.checkpoint()
            raise ParallelError(
                f"task-{order} cleanup recovery failed：{exc}") from exc

    def reap_workers(self) -> bool:
        progressed = False
        for order, handle in list(self.handles.items()):
            returncode = handle.process.poll()
            if returncode is None:
                continue
            returncode = int(handle.process.wait())
            self._close_worker_control(handle)
            compat.close_process_group(handle.process)
            try:
                self._terminalize_child_from_wait(handle, returncode)
            except ParallelError as exc:
                task = self._task(order)
                if task["resource_state"] not in {
                        "gate_claimed", "recovery_required", "exited",
                        "cleaning", "cleaned", "cleanup_failed"}:
                    self.aggregate = parallel_state.transition_task(
                        self.aggregate, order,
                        resource_state="recovery_required")
                self._block_run_for_task(order, str(exc))
                self.checkpoint()
                progressed = True
                continue
            del self.handles[order]
            status, assignment = self._load_worker_assignment(order)
            task = self._task(order)
            has_receipt = self._receipt_authorizes_integration(order)
            if has_receipt:
                if task["outcome"] == "pending":
                    self.aggregate = parallel_state.transition_task(
                        self.aggregate, order, outcome="integrated")
                elif task["outcome"] != "integrated":
                    self._block_run_for_task(
                        order,
                        f"task-{order} receipt conflicts with {task['outcome']} outcome",
                    )
                    self.checkpoint()
                    progressed = True
                    continue
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="exited")
                self.checkpoint()
                self._cleanup_integrated(order)
            elif task["outcome"] == "integrated" or status == "integrated":
                reason = f"task-{order} reports integrated without canonical receipt"
                if task["resource_state"] not in {
                        "gate_claimed", "recovery_required", "exited",
                        "cleaning", "cleaned", "cleanup_failed"}:
                    self.aggregate = parallel_state.transition_task(
                        self.aggregate, order, resource_state="exited")
                self._block_run_for_task(order, reason)
                self.checkpoint()
            elif status == "blocked":
                reason = assignment.get("exit_reason") or f"worker rc={returncode}"
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, outcome="blocked",
                    resource_state="exited", error=reason)
                self.aggregate["error"] = reason
                self.aggregate = parallel_state.transition_run_status(
                    self.aggregate, "blocked")
                self.checkpoint()
            elif status == "recovery-required":
                reason = assignment.get("exit_reason") or "worker gate recovery required"
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, resource_state="recovery_required",
                    error=reason)
                self.aggregate["error"] = reason
                self.aggregate = parallel_state.transition_run_status(
                    self.aggregate, "blocked")
                self.checkpoint()
            else:
                ready = (Path(self.artifacts.assignments[order]["worker_workspace_path"])
                         / "startup_ready.json").exists()
                reason = (f"task-{order} provisioning failure rc={returncode}"
                          if not ready else f"task-{order} unexpected crash rc={returncode}")
                if not ready:
                    self.aggregate = parallel_state.transition_task(
                        self.aggregate, order, outcome="blocked",
                        resource_state="exited", error=reason)
                    self.aggregate["error"] = reason
                    self.aggregate = parallel_state.transition_run_status(
                        self.aggregate, "blocked")
                    self.checkpoint()
                else:
                    try:
                        self.aggregate = parallel_state.transition_task(
                            self.aggregate, order, resource_state="crashed", error=reason)
                        self.aggregate = parallel_state.increment_restart_count(
                            self.aggregate, order,
                            limit=self.artifacts.run_config["worker_restart_limit"])
                        self.checkpoint()
                        self.dispatch(order, resume=True)
                    except (ParallelError, parallel_state.ParallelStateError) as exc:
                        self.aggregate["error"] = str(exc)
                        self.aggregate = parallel_state.transition_run_status(
                            self.aggregate, "blocked")
                        self.checkpoint()
            progressed = True
        return progressed

    def _advance_batch(self) -> bool:
        current = self.aggregate["batch"]
        current_tasks = [
            task for task in self.aggregate["tasks"] if task["batch"] == current]
        if any(task["outcome"] == "blocked" for task in current_tasks):
            return False
        if not current_tasks or not all(
                task["outcome"] == "integrated"
                and task["resource_state"] == "cleaned"
                for task in current_tasks):
            return False
        batches = [item["index"] for item in self.artifacts.manifest["batches"]]
        next_batches = [item for item in batches if item > current]
        if next_batches:
            self.aggregate["batch"] = min(next_batches)
            self.checkpoint()
            return True
        return False

    def _dispatch_available(self) -> bool:
        if self.aggregate["status"] != "running":
            return False
        available = self.artifacts.run_config["max_parallel"] - len(self.handles)
        if available <= 0:
            return False
        resumable = [
            task for task in self.aggregate["tasks"]
            if task["batch"] == self.aggregate["batch"]
            and task["outcome"] == "pending"
            and task["resource_state"] in {"paused", "crashed"}
        ]
        for task in resumable[:available]:
            self.dispatch(task["order"], resume=True)
        available -= len(resumable[:available])
        if available <= 0:
            return bool(resumable)
        queued = [
            task for task in self.aggregate["tasks"]
            if task["batch"] == self.aggregate["batch"]
            and task["outcome"] == "pending"
            and task["resource_state"] == "queued"
        ]
        for task in queued[:available]:
            self.dispatch(task["order"])
        return bool(resumable or queued[:available])

    def reconcile_existing(self, *, explicit_abort: bool = False) -> None:
        """Re-establish exact primary/resource truth before a resumed dispatch."""
        # A pending launch belongs to the vanished supervisor generation.  If
        # claim already won, its guardian record below is the authority;
        # otherwise cancellation prevents a late ghost payload.
        self._cancel_pending_launches()
        for task in self.aggregate["tasks"]:
            self._reconcile_orphan_children(task["order"])
        abort_recovery = (
            explicit_abort
            or self.aggregate["terminal_intent"] == "cancelled"
            or self.aggregate["status"] in {
                "cancel_requested", "finalizing_cancel", "cancelled"})
        receipts = {receipt["task"]: receipt for receipt in self._receipts()}
        self._audit_success_responses(receipts)
        for order, receipt in receipts.items():
            task = self._task(order)
            if task["outcome"] == "pending":
                self.aggregate = parallel_state.transition_task(
                    self.aggregate, order, outcome="integrated")
            elif task["outcome"] != "integrated":
                raise ParallelError(
                    f"task-{order} canonical receipt 與 outcome 衝突")
        for task in self.aggregate["tasks"]:
            order = task["order"]
            if task["outcome"] == "integrated" and order not in receipts:
                raise ParallelError(
                    f"task-{order} aggregate integrated 但缺 canonical receipt")
            if task["outcome"] == "blocked" and not abort_recovery:
                raise ParallelError(
                    f"task-{order} outcome blocked 需要人工修復或明確 Abort")
        reconciled_gates: set[int] = set()
        # Worker checkpoints retain the exact gate identity across the two
        # important lag windows: aggregate already returned to ``running``
        # after a stale result, or request cancellation won before its terminal
        # response/state projection became durable.
        for task in list(self.aggregate["tasks"]):
            order = task["order"]
            if self._reconcile_retained_gate(
                    order, receipts.get(order),
                    explicit_abort=abort_recovery):
                reconciled_gates.add(order)
        for task in list(self.aggregate["tasks"]):
            if (task["order"] not in reconciled_gates
                    and task["resource_state"] in {
                        "gate_claimed", "recovery_required"}):
                self._reconcile_claimed_gate(
                    task["order"], receipts.get(task["order"]),
                    explicit_abort=abort_recovery)
        receipts = {receipt["task"]: receipt for receipt in self._receipts()}
        expected_tip = self._expected_tip()
        allowed_blocked_removes = {
            task["order"]: task["outcome"]
            for task in self.aggregate["tasks"]
            if task["resource_state"] in {"cleaning", "cleanup_failed"}
        }
        try:
            audit = self.executor.audit_recovery_state(
                allowed_blocked_removes=allowed_blocked_removes)
        except (AttributeError, repo_executor.RepoExecutorError) as exc:
            raise ParallelError(f"RepoExecutor recovery audit blocked：{exc}") from exc
        if (audit.get("receipt_tip") != expected_tip
                or audit.get("primary_sha") != expected_tip
                or audit.get("sync_sha") != expected_tip):
            raise ParallelError("resume 時 primary/sync 不等於 receipt expected tip")
        for task in list(self.aggregate["tasks"]):
            order = task["order"]
            resource = task["resource_state"]
            receipt = receipts.get(order)
            if resource in {"cleaning", "cleanup_failed"}:
                self._reconcile_cleanup(order)
                continue
            if resource in {"gate_claimed", "recovery_required"}:
                raise ParallelError(
                    f"task-{order} gate recovery 未收斂到 stable resource state")
            if resource in {
                    "provisioning", "running", "gate_pending", "pausing"}:
                self._require_reaped_child_evidence(order)
                if receipt is not None:
                    self.aggregate = parallel_state.transition_task(
                        self.aggregate, order, resource_state="exited")
                else:
                    records = self._task_child_records(order)
                    if not records and resource != "provisioning":
                        raise ParallelError(
                            f"task-{order} active resource lacks guardian evidence")
                    payload_started = self._task_has_payload_evidence(order)
                    worker_status, assignment = self._load_worker_assignment(order)

                    if abort_recovery:
                        current = self._task(order)
                        if current["outcome"] != "cancelled":
                            self.aggregate = parallel_state.transition_task(
                                self.aggregate, order, outcome="cancelled",
                                explicit_abort=True)
                        self.aggregate = parallel_state.transition_task(
                            self.aggregate, order, resource_state="exited")
                        continue

                    if worker_status == "integrated":
                        raise ParallelError(
                            f"task-{order} worker reports integrated without canonical receipt")
                    if worker_status == "blocked":
                        reason = ((assignment or {}).get("exit_reason")
                                  or f"task-{order} worker blocked before supervisor recovery")
                        self.aggregate = parallel_state.transition_task(
                            self.aggregate, order, outcome="blocked",
                            resource_state="exited", error=reason)
                        self.aggregate["error"] = reason
                        if self.aggregate["status"] != "blocked":
                            self.aggregate = parallel_state.transition_run_status(
                                self.aggregate, "blocked")
                        continue
                    if worker_status == "recovery-required":
                        reason = ((assignment or {}).get("exit_reason")
                                  or f"task-{order} worker requires gate recovery")
                        self.aggregate = parallel_state.transition_task(
                            self.aggregate, order,
                            resource_state="recovery_required", error=reason)
                        self.aggregate["error"] = reason
                        if self.aggregate["status"] != "blocked":
                            self.aggregate = parallel_state.transition_run_status(
                                self.aggregate, "blocked")
                        continue
                    if worker_status == "paused":
                        worker_pause_generation = (assignment or {}).get(
                            "pause_generation")
                        aggregate_pause_generation = self.aggregate[
                            "pause_generation"]
                        if worker_pause_generation > aggregate_pause_generation:
                            raise ParallelError(
                                f"task-{order} worker pause_generation is ahead of aggregate")
                        if worker_pause_generation < aggregate_pause_generation:
                            self._project_reaped_worker_paused(order)
                        if resource != "pausing":
                            self.aggregate = parallel_state.transition_task(
                                self.aggregate, order, resource_state="pausing")
                        self.aggregate = parallel_state.transition_task(
                            self.aggregate, order, resource_state="paused")
                        continue
                    if worker_status == "cancelled":
                        reason = f"task-{order} worker cancelled without Abort intent"
                        self.aggregate = parallel_state.transition_task(
                            self.aggregate, order, outcome="blocked",
                            resource_state="exited", error=reason)
                        self.aggregate["error"] = reason
                        if self.aggregate["status"] != "blocked":
                            self.aggregate = parallel_state.transition_run_status(
                                self.aggregate, "blocked")
                        continue

                    if resource == "provisioning" and not payload_started:
                        if worker_status is not None:
                            raise ParallelError(
                                f"task-{order} worker state exists without payload evidence")
                        # No guardian authorized payload release.  Preserve
                        # restart budget and let dispatch reconstruct the exact
                        # CREATE request before an initial (non-resume) launch.
                        self.aggregate = parallel_state.transition_task(
                            self.aggregate, order, resource_state="crashed",
                            error="supervisor resume observed pre-spawn provisioning")
                        continue

                    ready = (Path(
                        self.artifacts.assignments[order]["worker_workspace_path"])
                        / "startup_ready.json").exists()
                    if resource == "provisioning" and not ready:
                        reason = (
                            f"task-{order} provisioning worker exited before startup")
                        self.aggregate = parallel_state.transition_task(
                            self.aggregate, order, outcome="blocked",
                            resource_state="exited", error=reason)
                        self.aggregate["error"] = reason
                        if self.aggregate["status"] != "blocked":
                            self.aggregate = parallel_state.transition_run_status(
                                self.aggregate, "blocked")
                        continue

                    self.aggregate = parallel_state.transition_task(
                        self.aggregate, order, resource_state="crashed",
                        error="supervisor resume observed reaped worker")
                    self.aggregate = parallel_state.increment_restart_count(
                        self.aggregate, order,
                        limit=self.artifacts.run_config["worker_restart_limit"])
        if self.aggregate["status"] != "blocked":
            self.aggregate["error"] = None
        self.checkpoint()
        for task in list(self.aggregate["tasks"]):
            if (task["outcome"] == "integrated"
                    and task["resource_state"] == "exited"):
                self._cleanup_integrated(task["order"])

    def _all_complete(self) -> bool:
        return all(
            task["outcome"] == "integrated"
            and task["resource_state"] == "cleaned"
            for task in self.aggregate["tasks"])

    def finalize_completed(self) -> None:
        self.aggregate = parallel_state.set_terminal_intent(
            self.aggregate, "completed")
        self.aggregate = parallel_state.transition_run_status(
            self.aggregate, "finalizing")
        self.checkpoint()
        self._audit_terminal_receipt_projection()
        self._archive_terminal_worker_workspaces()
        self._audit_terminal_worker_archives()
        self._durable_finalize("completed")
        self._audit_durable_finalization("completed")
        self._execute({
            "operation": repo_executor.Operation.SHUTDOWN.value,
            "operation_id": _operation_id(
                self.run_id, "shutdown", self.generation),
            "authority": {
                "supervisor_session": self.session,
                "generation": self.generation,
            },
            "expected": {"idle": True},
        })
        self.aggregate = parallel_state.transition_run_status(
            self.aggregate, "completed")
        self.checkpoint(active=False)

    def run(self) -> int:
        try:
            if self.aggregate["status"] == "initializing":
                if self.bootstrap_required:
                    self.preflight_and_initialize()
                else:
                    self.reconcile_existing()
                if self.aggregate["status"] == "blocked":
                    self.quiesce_blocked()
                    return 2
                self.aggregate = parallel_state.transition_run_status(
                    self.aggregate, "running")
                self.checkpoint()
            while True:
                if self.aggregate["status"] == "blocked":
                    self.quiesce_blocked()
                    return 2
                progressed = self.process_controls()
                if self.aggregate["status"] == "pause_requested":
                    return self.pause()
                if self.aggregate["status"] == "cancel_requested":
                    return self.abort()
                progressed |= self.process_gate_requests()
                progressed |= self.reap_workers()
                progressed |= self._advance_batch()
                if self._all_complete():
                    self.finalize_completed()
                    return 0
                progressed |= self._dispatch_available()
                if not progressed:
                    time.sleep(0.05)
        except KeyboardInterrupt:
            if self.aggregate["terminal_intent"] is not None:
                # Durable finalization cannot be converted into Pause.  Keep
                # its immutable terminal intent and leave a replayable blocked
                # checkpoint after fencing any still-owned children.
                self.aggregate["error"] = "finalization interrupted; Resume will replay"
                if self.aggregate["status"] not in RUN_TERMINAL | {"blocked"}:
                    self.aggregate = parallel_state.transition_run_status(
                        self.aggregate, "blocked")
                try:
                    self.quiesce_blocked()
                except (OSError, ValueError, ParallelError,
                        parallel_state.ParallelStateError,
                        parallel_spool.SpoolError):
                    self.checkpoint(active=True)
                return 2
            self.aggregate = parallel_state.transition_run_status(
                self.aggregate, "pause_requested")
            self.aggregate = parallel_state.advance_pause_generation(
                self.aggregate)
            self.checkpoint()
            return self.pause()
        except (OSError, ValueError, ParallelError,
                parallel_state.ParallelStateError,
                parallel_spool.SpoolError) as exc:
            reason = str(exc) or exc.__class__.__name__
            self.aggregate["error"] = reason
            if self.aggregate["status"] not in RUN_TERMINAL | {"blocked"}:
                self.aggregate = parallel_state.transition_run_status(
                    self.aggregate, "blocked")
            self.quiesce_blocked()
            return 2


def _new_run_id(workspace_root: Path, workspace_name: str) -> str:
    for _ in range(100):
        run_id = uuid.uuid4().hex[:parallel_contract.RUN_ID_HEX_LENGTH]
        candidate = workspace_root / workspace_name / "parallel" / run_id
        if not candidate.exists():
            return run_id
    raise ParallelError("無法配置不衝突的 run_id")


def _non_secret_environment_from_args(values: Sequence[str]) -> dict:
    result: dict[str, object] = {}
    identities: set[str] = set()
    for raw in values:
        name, separator, encoded = raw.partition("=")
        if not separator or not name:
            raise ParallelError(
                "--non-secret-env 必須使用 NAME=JSON_SCALAR 格式")
        identity = name.upper()
        if identity in identities:
            raise ParallelError("--non-secret-env 變數名稱不可重複")
        identities.add(identity)
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError:
            value = encoded
        if isinstance(value, (dict, list)):
            raise ParallelError("--non-secret-env value 必須是 JSON scalar")
        result[name] = value
    return result


def _runtime_config_from_args(args, repo: Path) -> dict:
    source = canonical_run_config({
        "repo": str(repo),
        "goal": args.goal,
        "plan_doc": args.plan_doc,
        "agent_cmd": args.agent_cmd,
        "validate_cmd": args.validate_cmd,
        "flag_threshold": args.flag_threshold,
        "done_threshold": args.done_threshold,
        "red_limit": args.red_limit,
        "stall_limit": args.stall_limit,
        "stuck_stop": args.stuck_stop,
        "stuck_stop_count": args.stuck_stop_count,
        "round_timeout": args.round_timeout,
        "agent_backoff_max": args.agent_backoff_max,
        "validate_timeout": args.validate_timeout,
        "notify_cmd": args.notify_cmd,
        "max_parallel": args.max_parallel,
        "worker_restart_limit": args.worker_restart_limit,
        "environment": {
            "path_additions": list(getattr(args, "path_addition", ()) or ()),
            "non_secret": _non_secret_environment_from_args(
                list(getattr(args, "non_secret_env", ()) or ())),
            "required_secret_names": list(
                getattr(args, "required_secret_name", ()) or ()),
        },
    })
    source.update({
        "primary_repo": str(repo),
        "max_rounds": 0,
        "pause_after_plan": False,
        "allow_serial_stack": False,
    })
    return parallel_state.normalize_run_config(source)


def _pending_launch_hash(
    artifacts: parallel_state.ValidatedRunArtifacts,
) -> str:
    return parallel_state.canonical_json_hash({
        "run_id": artifacts.manifest["run_id"],
        "plan_hash": artifacts.plan_hash,
        "run_config_hash": artifacts.run_config_hash,
        "integration_branch": artifacts.manifest["integration_branch"],
        "integration_start_sha": artifacts.manifest["integration_start_sha"],
    })


@dataclass(frozen=True)
class ExistingParallelRun:
    workspace: loop_mod.Workspace
    state: dict
    artifacts: parallel_state.ValidatedRunArtifacts
    aggregate: dict
    generation: int
    pending_launch_hash: str


def _load_existing_parallel_run(
    workspace_root: Path, name: str,
) -> ExistingParallelRun:
    if not loop_mod.valid_workspace_name(name):
        raise ParallelError(f"workspace name 不合法：{name!r}")
    workspace = loop_mod.Workspace(name)
    try:
        state = workspace.load_state()
    except (OSError, ValueError, loop_mod.StateLoadError) as exc:
        raise ParallelError(f"無法讀取 parallel workspace {name}：{exc}") from exc
    status = parallel_run_status(state)
    if status is None:
        raise ParallelError(f"workspace {name} 不是 parallel-supervisor run")
    projection = state.get("parallel")
    if not isinstance(projection, Mapping):
        raise ParallelError("parallel base projection 缺少 parallel object")
    try:
        run_id = parallel_contract.require_run_id(projection.get("run_id"))
        generation = _require_positive_integer(
            projection.get("supervisor_generation"),
            "parallel.supervisor_generation",
        )
        run_dir = parallel_state.derive_run_directory(
            workspace_root, name, run_id)
        artifacts = parallel_state.validate_run_artifacts(
            run_dir, workspace_root=workspace_root)
        aggregate = load_aggregate(artifacts)
        generation_authority = _read_supervisor_generation(artifacts)
    except (parallel_contract.ParallelContractError,
            parallel_state.ParallelStateError, ParallelError) as exc:
        raise ParallelError(f"parallel durable artifacts 不合法：{exc}") from exc
    if projection.get("manifest_hash") != artifacts.manifest_hash:
        raise ParallelError("base projection manifest_hash 與 immutable run 不符")
    if artifacts.manifest["parent_workspace"] != name:
        raise ParallelError("immutable run 不屬於指定 workspace")
    if generation_authority["generation"] < generation:
        raise ParallelError(
            "supervisor generation authority trails base projection")
    return ExistingParallelRun(
        workspace=workspace,
        state=state,
        artifacts=artifacts,
        aggregate=aggregate,
        generation=generation_authority["generation"],
        pending_launch_hash=_pending_launch_hash(artifacts),
    )


def _active_supervisor_owner(existing: ExistingParallelRun) -> dict | None:
    try:
        owner = loop_mod.active_run_lock_owner(
            existing.workspace.dir / ".run.lock")
    except (OSError, ValueError) as exc:
        raise ParallelError(f"無法驗證 parallel supervisor owner：{exc}") from exc
    if owner is None:
        return None
    session = owner.get("session_id")
    generation = owner.get("generation")
    if (not isinstance(session, str) or len(session) != 32
            or any(ch not in "0123456789abcdef" for ch in session)
            or not isinstance(generation, int) or isinstance(generation, bool)
            or generation < 1):
        raise ParallelError("active parallel supervisor lock identity 不合法")
    authority = _read_supervisor_generation(existing.artifacts)
    if (generation != existing.generation
            or authority["generation"] != generation
            or authority["session"] != session):
        raise ParallelError(
            "active supervisor lock does not match generation authority")
    return owner


_BOOTSTRAP_CONTROL_FIELDS = frozenset({
    "schema", "run_id", "request_id", "action", "state",
    "expected_supervisor_generation", "expected_aggregate_version",
    "expected_control_generation", "created_at", "claimed_by",
    "assigned_control_generation", "applied",
})
_BOOTSTRAP_CLAIM_FIELDS = frozenset({
    "session", "generation", "claimed_at",
})
_BOOTSTRAP_APPLIED_FIELDS = frozenset({
    "aggregate_version", "control_generation", "applied_at",
})


def _read_bootstrap_control(
    artifacts: parallel_state.ValidatedRunArtifacts,
    *,
    missing_ok: bool = False,
) -> dict | None:
    """Read and fully validate the sole no-owner control intent."""
    path = artifacts.run_dir / "controls" / "bootstrap.json"
    try:
        path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ParallelError("bootstrap control intent 不存在")
    except OSError as exc:
        raise ParallelError(f"bootstrap control intent 無法檢查：{exc}") from exc
    try:
        value = parallel_state.read_canonical_json(
            artifacts.run_dir, "controls/bootstrap.json")
    except (OSError, parallel_state.ParallelStateError) as exc:
        raise ParallelError(f"bootstrap control intent 損壞：{exc}") from exc
    if (not isinstance(value, dict)
            or set(value) != _BOOTSTRAP_CONTROL_FIELDS
            or value.get("schema") != 1
            or value.get("run_id") != artifacts.manifest["run_id"]
            or value.get("action") not in {"resume", "pause", "abort"}
            or value.get("state") not in {"pending", "claimed", "applied"}):
        raise ParallelError("bootstrap control intent authority/schema mismatch")
    request_id = value.get("request_id")
    if (not isinstance(request_id, str) or len(request_id) != 32
            or any(ch not in "0123456789abcdef" for ch in request_id)):
        raise ParallelError("bootstrap control request_id 不合法")
    expected_generation = value.get("expected_supervisor_generation")
    expected_version = value.get("expected_aggregate_version")
    expected_control = value.get("expected_control_generation")
    if (not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool) or expected_generation < 1
            or not isinstance(expected_version, int)
            or isinstance(expected_version, bool) or expected_version < 0
            or not isinstance(expected_control, int)
            or isinstance(expected_control, bool) or expected_control < 0
            or not isinstance(value.get("created_at"), str)
            or not value["created_at"]):
        raise ParallelError("bootstrap control expected authority 不合法")

    claimed = value.get("claimed_by")
    assigned = value.get("assigned_control_generation")
    applied = value.get("applied")
    if value["state"] == "pending":
        if claimed is not None or assigned is not None or applied is not None:
            raise ParallelError("pending bootstrap control 含有提前 claim/apply 證據")
        return value
    if (not isinstance(claimed, dict)
            or set(claimed) != _BOOTSTRAP_CLAIM_FIELDS
            or not isinstance(claimed.get("session"), str)
            or len(claimed["session"]) != 32
            or any(ch not in "0123456789abcdef" for ch in claimed["session"])
            or not isinstance(claimed.get("generation"), int)
            or isinstance(claimed["generation"], bool)
            or claimed["generation"] <= expected_generation
            or not isinstance(claimed.get("claimed_at"), str)
            or not claimed["claimed_at"]
            or not isinstance(assigned, int) or isinstance(assigned, bool)
            or assigned <= expected_control):
        raise ParallelError("claimed bootstrap control authority 不合法")
    if value["state"] == "claimed":
        if applied is not None:
            raise ParallelError("claimed bootstrap control 含有提前 apply 證據")
        return value
    if (not isinstance(applied, dict)
            or set(applied) != _BOOTSTRAP_APPLIED_FIELDS
            or not isinstance(applied.get("aggregate_version"), int)
            or isinstance(applied["aggregate_version"], bool)
            or applied["aggregate_version"] < expected_version
            or applied.get("control_generation") != assigned
            or not isinstance(applied.get("applied_at"), str)
            or not applied["applied_at"]):
        raise ParallelError("applied bootstrap control evidence 不合法")
    return value


def _write_bootstrap_control(
    artifacts: parallel_state.ValidatedRunArtifacts,
    record: Mapping[str, object],
) -> None:
    try:
        parallel_state.atomic_write_json(
            artifacts.run_dir, "controls/bootstrap.json", dict(record))
    except (OSError, parallel_state.ParallelStateError) as exc:
        raise ParallelError(f"bootstrap control intent 無法持久化：{exc}") from exc


def _install_bootstrap_control(
    existing: ExistingParallelRun,
    action: str,
) -> dict:
    """Publish the winning no-owner intent before a recovery owner starts."""
    if action not in {"resume", "pause", "abort"}:
        raise ParallelError(f"unknown bootstrap control action：{action}")
    workspace_root = existing.artifacts.run_dir.parents[2]
    with BootstrapControlLock(existing.artifacts.run_dir):
        current = _load_existing_parallel_run(
            workspace_root, existing.workspace.name)
        if _active_supervisor_owner(current) is not None:
            raise ParallelError(
                "parallel supervisor 在 bootstrap control 發布前已出現；請重試")
        _validate_recovery_action_legality(current.aggregate, action)
        prior = _read_bootstrap_control(
            current.artifacts, missing_ok=True)
        if prior is not None and prior["state"] == "pending":
            expected = (
                prior["expected_supervisor_generation"],
                prior["expected_aggregate_version"],
                prior["expected_control_generation"],
            )
            observed = (
                current.generation,
                current.aggregate["version"],
                current.aggregate["control_generation"],
            )
            if any(left > right for left, right in zip(expected, observed)):
                raise ParallelError(
                    "pending bootstrap control expected future durable authority")
            if expected != observed:
                # Publish is not the action linearization point.  A pending
                # intent that never claimed its exact version/generation can
                # be superseded under this same short lock; retaining it would
                # permanently poison every later control.
                prior = None
            else:
                _validate_recovery_action_legality(
                    current.aggregate, str(prior["action"]))
                if prior["action"] != action:
                    raise ParallelError(
                        "bootstrap control conflict："
                        f"pending {prior['action']} 已先發布")
                return prior
        if prior is not None and prior["state"] == "claimed":
            # The first durable publisher wins.  A different pending action
            # is handled above.  A claimed Abort is the safety exception: it already
            # linearized and must be replayed even when the recovery command
            # happens to be Resume.
            if (prior["expected_supervisor_generation"]
                    > current.generation):
                raise ParallelError(
                    "bootstrap control expected future supervisor generation")
            _validate_recovery_action_legality(
                current.aggregate, str(prior["action"]))
            if prior["action"] != action:
                if not (prior["action"] == "abort"
                        and action in {"resume", "pause"}):
                    raise ParallelError(
                        "bootstrap control conflict："
                        f"{prior['state']} {prior['action']} 已先發布")
            return prior
        if prior is not None:
            applied = prior["applied"]
            claimed = prior["claimed_by"]
            if (applied["aggregate_version"] > current.aggregate["version"]
                    or applied["control_generation"]
                    > current.aggregate["control_generation"]
                    or claimed["generation"] > current.generation):
                raise ParallelError(
                    "applied bootstrap control evidence 超前 durable run state")
        record = {
            "schema": 1,
            "run_id": current.artifacts.manifest["run_id"],
            "request_id": uuid.uuid4().hex,
            "action": action,
            "state": "pending",
            "expected_supervisor_generation": current.generation,
            "expected_aggregate_version": current.aggregate["version"],
            "expected_control_generation": (
                current.aggregate["control_generation"]),
            "created_at": datetime.now().astimezone().isoformat(
                timespec="microseconds"),
            "claimed_by": None,
            "assigned_control_generation": None,
            "applied": None,
        }
        _write_bootstrap_control(current.artifacts, record)
        return record


def _claim_bootstrap_control(
    current: ExistingParallelRun,
    request_id: str,
    *,
    session: str,
    generation: int,
    assigned_control_generation: int,
) -> dict:
    """Durably claim a bootstrap intent before advancing owner generation."""
    with BootstrapControlLock(current.artifacts.run_dir):
        record = _read_bootstrap_control(current.artifacts)
        if record["request_id"] != request_id:
            raise ParallelError(
                "bootstrap control winner changed before recovery claim")
        if record["state"] == "applied":
            return record
        if record["state"] == "claimed":
            if (record["expected_supervisor_generation"]
                    > current.generation
                    or record["expected_aggregate_version"]
                    > current.aggregate["version"]
                    or record["expected_control_generation"]
                    > current.aggregate["control_generation"]
                    or record["assigned_control_generation"]
                    < current.aggregate["control_generation"]):
                raise ParallelError(
                    "claimed bootstrap control 不符合目前 durable authority")
            return record
        if (record["expected_supervisor_generation"] != current.generation
                or record["expected_aggregate_version"]
                != current.aggregate["version"]
                or record["expected_control_generation"]
                != current.aggregate["control_generation"]):
            raise ParallelError(
                "pending bootstrap control 的 generation/version 已過期")
        assigned = _require_positive_integer(
            assigned_control_generation,
            "bootstrap assigned_control_generation")
        if assigned <= current.aggregate["control_generation"]:
            raise ParallelError(
                "bootstrap assigned control generation 沒有前進")
        claimed = dict(record)
        claimed.update({
            "state": "claimed",
            "claimed_by": {
                "session": session,
                "generation": generation,
                "claimed_at": datetime.now().astimezone().isoformat(
                    timespec="microseconds"),
            },
            "assigned_control_generation": assigned,
        })
        _write_bootstrap_control(current.artifacts, claimed)
        return claimed


def _apply_bootstrap_control(aggregate: dict, record: Mapping[str, object]) -> dict:
    """Apply one claimed no-owner action exactly once in aggregate memory."""
    action = record["action"]
    assigned = record["assigned_control_generation"]
    if action in {"pause", "abort"}:
        return _apply_claimed_control(aggregate, str(action), int(assigned))
    current = aggregate["control_generation"]
    if assigned == current + 1:
        aggregate = dict(aggregate)
        aggregate["control_generation"] = assigned
    elif assigned != current:
        raise ParallelError(
            "claimed Resume control_generation cannot be applied monotonically")
    return aggregate


def _mark_bootstrap_control_applied(
    artifacts: parallel_state.ValidatedRunArtifacts,
    request_id: str,
    aggregate: Mapping[str, object],
) -> dict:
    """Acknowledge a claim only after its aggregate/base checkpoint lands."""
    with BootstrapControlLock(artifacts.run_dir):
        record = _read_bootstrap_control(artifacts)
        if record["request_id"] != request_id:
            raise ParallelError(
                "bootstrap control changed before durable apply acknowledgement")
        try:
            durable = load_aggregate(artifacts)
            base_dir = artifacts.run_dir.parents[1]
            base_state, _raw, _recovered = loop_mod.load_checkpointed_state(
                base_dir / "state.json", repair=False)
            generation_authority = _read_supervisor_generation(artifacts)
        except (OSError, ValueError, loop_mod.StateLoadError,
                parallel_state.ParallelStateError, ParallelError) as exc:
            raise ParallelError(
                f"bootstrap durable checkpoint 無法重讀：{exc}") from exc
        try:
            caller_bytes = parallel_state.canonical_json_bytes(dict(aggregate))
            durable_bytes = parallel_state.canonical_json_bytes(durable)
        except parallel_state.ParallelStateError as exc:
            raise ParallelError(
                f"bootstrap aggregate checkpoint 不合法：{exc}") from exc
        projection = base_state.get("parallel")
        assigned = record["assigned_control_generation"]
        if (record["state"] == "pending" or assigned is None
                or caller_bytes != durable_bytes
                or durable.get("control_generation") != assigned
                or not isinstance(durable.get("version"), int)
                or durable["version"]
                <= record["expected_aggregate_version"]
                or base_state.get("runner") != SUPERVISOR_RUNNER
                or not isinstance(projection, Mapping)
                or projection.get("run_id")
                != artifacts.manifest["run_id"]
                or projection.get("aggregate_version") != durable["version"]
                or projection.get("control_generation") != assigned
                or projection.get("status") != durable["status"]
                or projection.get("terminal_intent")
                != durable["terminal_intent"]
                or projection.get("supervisor_generation")
                != generation_authority["generation"]):
            raise ParallelError(
                "bootstrap control 沒有可驗證的 durable aggregate checkpoint")
        action = record["action"]
        if (action == "abort"
                and durable["terminal_intent"] != "cancelled"):
            raise ParallelError(
                "bootstrap Abort 缺少 durable cancelled intent")
        if (action == "pause"
                and durable["terminal_intent"] is None
                and durable["status"] not in {
                    "pause_requested", "paused", "blocked"}):
            raise ParallelError(
                "bootstrap Pause 缺少 durable pause checkpoint")
        if (action == "resume"
                and durable["terminal_intent"] is None
                and durable["status"] not in {
                    "initializing", "running", "blocked"}):
            raise ParallelError(
                "bootstrap Resume 缺少 durable resume checkpoint")
        if record["state"] == "applied":
            if (record["applied"]["control_generation"] != assigned
                    or record["applied"]["aggregate_version"]
                    > durable["version"]):
                raise ParallelError(
                    "bootstrap applied acknowledgement 與 aggregate 衝突")
            return record
        applied = dict(record)
        applied.update({
            "state": "applied",
            "applied": {
                "aggregate_version": durable["version"],
                "control_generation": assigned,
                "applied_at": datetime.now().astimezone().isoformat(
                    timespec="microseconds"),
            },
        })
        _write_bootstrap_control(artifacts, applied)
        return applied


def _control_request(
    existing: ExistingParallelRun, owner: Mapping[str, object], action: str,
) -> int:
    # Avoid publishing an intent that is already illegal in the caller's
    # durable snapshot. process_controls() repeats this validation before its
    # claim to close the observation-to-claim race.
    _validate_recovery_action_legality(existing.aggregate, action)
    request_id = uuid.uuid4().hex
    payload = {
        "schema": 1,
        "request_id": request_id,
        "run_id": existing.artifacts.manifest["run_id"],
        "action": action,
        "supervisor_session": owner["session_id"],
        "supervisor_generation": owner["generation"],
        "control_generation": existing.aggregate["control_generation"] + 1,
        "aggregate_version": existing.aggregate["version"],
    }
    try:
        spool = parallel_spool.DurableSpool(
            existing.artifacts.run_dir / "controls")
        spool.publish_request(request_id, payload)
    except parallel_spool.SpoolError as exc:
        raise ParallelError(f"parallel {action} control publish 失敗：{exc}") from exc
    deadline = time.monotonic() + 10.0
    response = None
    while time.monotonic() < deadline:
        try:
            response = spool.get_response(request_id)
        except parallel_spool.SpoolError as exc:
            raise ParallelError(f"parallel {action} control response 損壞：{exc}") from exc
        if response is not None:
            break
        time.sleep(0.05)
    if response is None:
        try:
            cancellation = spool.cancel_request(request_id)
            response = spool.get_response(request_id)
        except parallel_spool.SpoolError as exc:
            raise ParallelError(
                f"parallel {action} deadline transition 失敗：{exc}") from exc
        if response is None and cancellation.transitioned:
            raise ParallelError(
                f"parallel supervisor 未在期限內 claim {action}；request 已原子取消，"
                "不會稍後 ghost 執行")
        if response is None and cancellation.record.state == "claimed":
            # Claim is the action's linearization point.  Wait for its durable
            # response race, but never report this case as safely cancelled.
            response_deadline = time.monotonic() + 10.0
            while time.monotonic() < response_deadline:
                try:
                    response = spool.get_response(request_id)
                except parallel_spool.SpoolError as exc:
                    raise ParallelError(
                        f"parallel {action} claimed response 損壞：{exc}") from exc
                if response is not None:
                    break
                time.sleep(0.05)
        if response is None:
            owner_state = ("gone" if _active_supervisor_owner(existing) is None
                           else "live")
            raise ParallelError(
                f"parallel {action} request 已 {cancellation.record.state}、"
                f"owner={owner_state}，但沒有可驗 terminal response；請 recovery")
    expected_response = {
        "schema": 1,
        "request_id": request_id,
        "status": "accepted",
        "action": action,
        "run_id": existing.artifacts.manifest["run_id"],
    }
    if response.payload != expected_response:
        stale_response = {
            "schema": 1,
            "request_id": request_id,
            "status": "stale",
            "action": action,
            "run_id": existing.artifacts.manifest["run_id"],
        }
        if response.payload == stale_response:
            raise ParallelError(
                f"parallel {action} request 屬於已結束的 supervisor generation")
        rejected_response = {
            "schema": 1,
            "request_id": request_id,
            "status": "rejected",
            "action": action,
            "run_id": existing.artifacts.manifest["run_id"],
        }
        if response.payload == rejected_response:
            raise ParallelError(
                f"parallel {action} is invalid for the supervisor's current state")
        raise ParallelError(f"parallel {action} control response authority/schema 不符")
    durable = spool.get_request(request_id)
    if durable is None or durable.state != "claimed":
        raise ParallelError(f"parallel {action} response 沒有 claimed request authority")

    terminal = ({"paused", "blocked", "completed", "cancelled"}
                if action == "pause" else {"cancelled", "blocked"})
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        current = _load_existing_parallel_run(
            existing.artifacts.run_dir.parents[2], existing.workspace.name)
        status = current.aggregate["status"]
        if status in terminal:
            if _active_supervisor_owner(current) is None:
                return 2 if status == "blocked" else 0
        time.sleep(0.1)
    raise ParallelError(f"parallel {action} 已接受，但未在期限內完成 quiesce")


def _prepare_recovery_status(aggregate: dict, action: str) -> dict:
    status = aggregate["status"]
    intent = aggregate["terminal_intent"]
    if action == "resume":
        if status in RUN_TERMINAL:
            raise ParallelError(f"terminal parallel run {status} 不可 Resume")
        if intent == "completed":
            if status == "finalizing":
                return aggregate
            if status != "blocked":
                raise ParallelError("completion finalization 只能由 blocked 狀態重播")
            return parallel_state.transition_run_status(aggregate, "finalizing")
        if intent == "cancelled":
            if status == "blocked":
                return parallel_state.transition_run_status(
                    aggregate, "finalizing_cancel")
            if status in {"cancel_requested", "finalizing_cancel"}:
                return aggregate
            raise ParallelError("cancelled intent 禁止 Resume worker")
        if status in {"initializing", "running", "pause_requested"}:
            aggregate["error"] = "previous supervisor owner disappeared"
            aggregate = parallel_state.transition_run_status(
                aggregate, "blocked")
            status = "blocked"
        if status not in {"paused", "blocked"}:
            raise ParallelError(f"parallel {status} 不可 Resume")
        return parallel_state.transition_run_status(aggregate, "initializing")

    if action == "pause":
        if status in RUN_TERMINAL | {"paused", "blocked"}:
            return aggregate
        if status in {"cancel_requested", "finalizing_cancel"}:
            return aggregate
        if status == "finalizing":
            raise ParallelError("completion finalization 不可被 Pause 中斷")
        if status in {"initializing", "running"}:
            aggregate = parallel_state.transition_run_status(
                aggregate, "pause_requested")
            return parallel_state.advance_pause_generation(aggregate)
        if status == "pause_requested":
            return aggregate
        raise ParallelError(f"parallel {status} 不可 Pause")

    if action != "abort":
        raise ParallelError(f"unknown parallel recovery action：{action}")
    if status in RUN_TERMINAL:
        raise ParallelError(f"terminal parallel run {status} 不可 Abort")
    if intent == "completed" or status == "finalizing":
        raise ParallelError("completion finalization 已開始，不可改成 Abort")
    if intent is None:
        aggregate = parallel_state.set_terminal_intent(
            aggregate, "cancelled")
    if status not in {"cancel_requested", "finalizing_cancel"}:
        aggregate = parallel_state.transition_run_status(
            aggregate, "cancel_requested")
    return aggregate


def _claimed_control_recovery(
    artifacts: parallel_state.ValidatedRunArtifacts,
    aggregate: Mapping[str, object],
    *, next_generation: int,
) -> tuple[
    parallel_spool.DurableSpool, tuple[object, ...], str | None, int | None,
]:
    """Audit claimed control history and return unacknowledged linearizations.

    Claiming a control request is its durable linearization point.  A prior
    supervisor may die after that rename but before projecting the intent or
    publishing the response.  Such a claim must dominate a later Resume.
    Responded historical claims are audited but are not re-applied.
    """
    spool = parallel_spool.DurableSpool(artifacts.run_dir / "controls")
    spool.list_responses()
    unresolved = []
    actions: set[str] = set()
    expected_fields = {
        "schema", "request_id", "run_id", "action",
        "supervisor_session", "supervisor_generation",
        "control_generation", "aggregate_version",
    }
    current_control_generation = aggregate.get("control_generation")
    current_version = aggregate.get("version")
    if (not isinstance(current_control_generation, int)
            or isinstance(current_control_generation, bool)
            or current_control_generation < 0
            or not isinstance(current_version, int)
            or isinstance(current_version, bool) or current_version < 0):
        raise ParallelError("aggregate control/version authority is invalid")
    seen_control_generations: set[int] = set()
    for record in spool.list_requests("claimed"):
        request = record.payload
        if (not isinstance(request, dict) or set(request) != expected_fields
                or request.get("schema") != 1
                or request.get("request_id") != record.request_id
                or request.get("run_id") != artifacts.manifest["run_id"]
                or request.get("action") not in {"pause", "abort"}):
            raise ParallelError(
                "claimed parallel control request authority/schema mismatch")
        session = request.get("supervisor_session")
        generation = request.get("supervisor_generation")
        control_generation = request.get("control_generation")
        aggregate_version = request.get("aggregate_version")
        if (not isinstance(session, str) or len(session) != 32
                or any(ch not in "0123456789abcdef" for ch in session)
                or not isinstance(generation, int)
                or isinstance(generation, bool) or generation < 1
                or generation >= next_generation
                or not isinstance(control_generation, int)
                or isinstance(control_generation, bool)
                or control_generation < 1
                or not isinstance(aggregate_version, int)
                or isinstance(aggregate_version, bool)
                or aggregate_version < 0):
            raise ParallelError(
                "claimed parallel control owner generation is not historical")
        if control_generation in seen_control_generations:
            raise ParallelError(
                "claimed parallel controls reuse control_generation")
        seen_control_generations.add(control_generation)
        try:
            response = spool.get_response(record.request_id)
        except parallel_spool.SpoolError as exc:
            raise ParallelError(
                f"claimed parallel control response cannot be audited: {exc}") from exc
        if response is not None:
            expected = {
                "schema": 1,
                "request_id": record.request_id,
                "status": "accepted",
                "action": request["action"],
                "run_id": artifacts.manifest["run_id"],
            }
            if response.payload != expected:
                raise ParallelError(
                    "claimed parallel control response authority/schema mismatch")
            if control_generation > current_control_generation:
                raise ParallelError(
                    "responded control is newer than aggregate authority")
            continue
        unresolved.append(record)
        actions.add(str(request["action"]))
    if len(unresolved) > 1:
        raise ParallelError(
            "multiple unacknowledged claimed controls violate single-writer order")
    recovered_generation = None
    if unresolved:
        request = unresolved[0].payload
        recovered_generation = request["control_generation"]
        request_version = request["aggregate_version"]
        if recovered_generation == current_control_generation + 1:
            if request_version != current_version:
                raise ParallelError(
                    "claimed control aggregate version changed before projection")
        elif recovered_generation == current_control_generation:
            if current_version <= request_version:
                raise ParallelError(
                    "claimed control lacks a durable aggregate checkpoint")
        else:
            raise ParallelError(
                "claimed control_generation is not the current linearization")
    action = ("abort" if "abort" in actions
              else "pause" if "pause" in actions else None)
    return spool, tuple(unresolved), action, recovered_generation


def _apply_claimed_control(
    aggregate: dict,
    action: str | None,
    control_generation: int | None = None,
) -> dict:
    """Fold an unacknowledged prior control into aggregate truth once."""
    if action is None:
        return aggregate
    current_control_generation = aggregate["control_generation"]
    if control_generation == current_control_generation + 1:
        aggregate = dict(aggregate)
        aggregate["control_generation"] = control_generation
    elif control_generation != current_control_generation:
        raise ParallelError(
            "claimed control_generation cannot be applied monotonically")
    status = aggregate["status"]
    intent = aggregate["terminal_intent"]
    if action == "abort":
        if intent == "completed" or status in {"finalizing", "completed"}:
            raise ParallelError(
                "claimed Abort conflicts with completion finalization")
        if intent is None:
            aggregate = parallel_state.set_terminal_intent(
                aggregate, "cancelled")
        if status in {
                "initializing", "running", "pause_requested", "paused",
                "blocked"}:
            aggregate = parallel_state.transition_run_status(
                aggregate, "cancel_requested")
        elif status not in {
                "cancel_requested", "finalizing_cancel", "cancelled"}:
            raise ParallelError(
                f"claimed Abort cannot be replayed from {status}")
        return aggregate

    if intent == "cancelled" or status in {
            "cancel_requested", "finalizing_cancel", "cancelled"}:
        return aggregate
    if intent == "completed" and status == "blocked":
        # Pause is an idempotent quiescence request at a failed completion
        # boundary.  It must preserve the completion intent; only Resume may
        # replay finalization and Abort remains a conflict.
        return aggregate
    if intent == "completed" or status in {"finalizing", "completed"}:
        raise ParallelError(
            "claimed Pause conflicts with completion finalization")
    if status in {"initializing", "running"}:
        aggregate = parallel_state.transition_run_status(
            aggregate, "pause_requested")
        return parallel_state.advance_pause_generation(aggregate)
    if status in {"pause_requested", "paused", "blocked"}:
        return aggregate
    raise ParallelError(f"claimed Pause cannot be replayed from {status}")


def _validate_recovery_action_legality(
    aggregate: Mapping[str, object], action: str,
) -> None:
    """Purely validate a control before publishing or claiming authority."""
    preview = dict(aggregate)
    if action in {"pause", "abort"}:
        preview = _apply_claimed_control(
            preview, action, preview["control_generation"] + 1)
    _prepare_recovery_status(preview, action)


def _effective_recovery_action(
        requested: str, claimed: str | None) -> str:
    if requested == "abort" or claimed == "abort":
        return "abort"
    if claimed == "pause":
        return "pause"
    return requested


def _publish_recovered_control_responses(
    spool: parallel_spool.DurableSpool,
    records: Sequence[object],
    *, run_id: str,
) -> None:
    """Acknowledge claims only after their aggregate checkpoint is durable."""
    for record in records:
        request = record.payload
        spool.publish_response(record.request_id, {
            "schema": 1,
            "request_id": record.request_id,
            "status": "accepted",
            "action": request["action"],
            "run_id": run_id,
        })


def _reconcile_executor_lease(
    spec: repo_executor.ImmutableRepoSpec,
    executor: repo_executor.RepoExecutor,
) -> tuple[repo_executor.RepoExecutor, dict | None]:
    """Fence and replay the exact nonterminal operation before state recovery.

    SHUTDOWN closes its executor by contract.  Re-open and re-audit the global
    lock immediately so the base projection never proceeds through an
    unaudited handoff window.
    """
    try:
        result = executor.reconcile_pending_operation(
            recovery_authorizer=repo_executor.RepoExecutor.fence_recovery_lease)
    except repo_executor.RepoExecutorError as exc:
        raise ParallelError(
            f"RepoExecutor pending operation recovery blocked：{exc}") from exc
    if result is None:
        return executor, None
    operation = result.get("operation")
    if operation not in {item.value for item in repo_executor.Operation}:
        raise ParallelError(
            "RepoExecutor pending operation recovery returned unknown operation")
    if operation != repo_executor.Operation.SHUTDOWN.value:
        return executor, result

    executor.close()
    replacement = repo_executor.RepoExecutor(spec)
    try:
        unexpected = replacement.reconcile_pending_operation(
            recovery_authorizer=repo_executor.RepoExecutor.fence_recovery_lease)
    except repo_executor.RepoExecutorError as exc:
        replacement.close()
        raise ParallelError(
            f"RepoExecutor post-SHUTDOWN audit blocked：{exc}") from exc
    if unexpected is not None:
        replacement.close()
        raise ParallelError(
            "RepoExecutor SHUTDOWN recovery did not become terminal")
    return replacement, result


def _recover_existing_parallel(
    workspace_root: Path, name: str, action: str,
) -> int:
    existing = _load_existing_parallel_run(workspace_root, name)
    owner = _active_supervisor_owner(existing)
    if owner is not None:
        if action == "resume":
            raise ParallelError("parallel supervisor 已在執行，不能重複 Resume")
        return _control_request(existing, owner, action)

    prior_bootstrap = _read_bootstrap_control(
        existing.artifacts, missing_ok=True)
    unresolved_bootstrap = (
        prior_bootstrap is not None
        and prior_bootstrap["state"] in {"pending", "claimed"}
    )
    aggregate_status = existing.aggregate["status"]
    projection_status = parallel_run_status(existing.state)
    if (action == "pause"
            and aggregate_status in RUN_TERMINAL | {"paused"}
            and projection_status == aggregate_status
            and not unresolved_bootstrap):
        # A fully projected terminal/paused run is already quiescent.  Blocked
        # deliberately does not use this shortcut because its owner may have
        # died between the blocked checkpoint and child fencing.
        return 0
    if (aggregate_status in RUN_TERMINAL
            and projection_status != aggregate_status
            and not unresolved_bootstrap):
        if projection_status in RUN_TERMINAL:
            raise ParallelError(
                "terminal aggregate 與 base projection 的 terminal status 衝突")
        if action not in {"resume", "pause"}:
            raise ParallelError(
                f"terminal parallel run {aggregate_status} 不可 {action.title()}")
        # SHUTDOWN precedes the terminal aggregate checkpoint.  Therefore a
        # terminal aggregate with a lagging base projection is a pure two-file
        # projection crash and can be repaired without replaying Git work.
        session = uuid.uuid4().hex
        generation = existing.generation + 1
        with SupervisorRunLock(
                existing.workspace.dir / ".run.lock",
                session=session, generation=generation):
            current = _load_existing_parallel_run(workspace_root, name)
            if (current.generation != existing.generation
                    or current.artifacts.manifest_hash
                    != existing.artifacts.manifest_hash):
                raise ParallelError(
                    "terminal projection repair 前 generation/authority 已改變")
            current_status = current.aggregate["status"]
            current_projection = parallel_run_status(current.state)
            if current_status not in RUN_TERMINAL:
                raise ParallelError(
                    "terminal projection repair 時 aggregate 已不再 terminal")
            if current_projection in RUN_TERMINAL and current_projection != current_status:
                raise ParallelError(
                    "terminal projection repair 發現互斥 terminal status")
            _claim_supervisor_generation(
                current.artifacts,
                expected_generation=existing.generation,
                generation=generation,
                session=session,
            )
            spec = build_repo_spec(
                current.artifacts,
                pending_launch_hash=current.pending_launch_hash,
                supervisor_session=session,
                generation=generation,
            )
            executor = repo_executor.RepoExecutor(spec)
            try:
                executor, recovered_operation = _reconcile_executor_lease(
                    spec, executor)
                if (recovered_operation is not None
                        and recovered_operation.get("operation")
                        != repo_executor.Operation.SHUTDOWN.value):
                    raise ParallelError(
                        "terminal aggregate has a non-SHUTDOWN pending operation")
                supervisor = ParallelSupervisor(
                    workspace_root=workspace_root,
                    workspace=current.workspace,
                    artifacts=current.artifacts,
                    aggregate=current.aggregate,
                    executor=executor,
                    pending_launch_hash=current.pending_launch_hash,
                    session=session,
                    generation=generation,
                    bootstrap_required=False,
                )
                expected_tip = supervisor._expected_tip()
                try:
                    audit = executor.audit_recovery_state()
                except repo_executor.RepoExecutorError as exc:
                    raise ParallelError(
                        f"terminal projection repository audit blocked: {exc}") from exc
                if (audit.get("receipt_tip") != expected_tip
                        or audit.get("primary_sha") != expected_tip
                        or audit.get("sync_sha") != expected_tip):
                    raise ParallelError(
                        "terminal projection repository does not match receipt tip")
                latest = audit.get("latest_operation")
                latest_result = (latest.get("result")
                                 if isinstance(latest, Mapping) else None)
                shutdown_generation = (latest_result.get("generation")
                                       if isinstance(latest_result, Mapping)
                                       else None)
                projected_shutdown_generation = current.state["parallel"].get(
                    "supervisor_generation")
                expected_shutdown_id = (
                    _operation_id(
                        current.artifacts.manifest["run_id"],
                        "shutdown", shutdown_generation)
                    if isinstance(shutdown_generation, int)
                    and not isinstance(shutdown_generation, bool)
                    and shutdown_generation > 0
                    else None)
                if (not isinstance(latest, Mapping)
                        or latest.get("operation")
                        != repo_executor.Operation.SHUTDOWN.value
                        or latest.get("operation_id") != expected_shutdown_id
                        or latest.get("terminal_status") != "shutdown"
                        or latest.get("generation") != shutdown_generation
                        or shutdown_generation
                        != projected_shutdown_generation
                        or not isinstance(latest_result, Mapping)
                        or latest_result.get("operation")
                        != repo_executor.Operation.SHUTDOWN.value
                        or latest_result.get("operation_id")
                        != expected_shutdown_id
                        or latest_result.get("status") != "shutdown"
                        or not isinstance(
                            latest_result.get("supervisor_session"), str)
                        or len(latest_result["supervisor_session"]) != 32
                        or any(ch not in "0123456789abcdef"
                               for ch in latest_result["supervisor_session"])):
                    raise ParallelError(
                        "terminal projection lacks exact SHUTDOWN evidence")
                for task in supervisor.aggregate["tasks"]:
                    try:
                        observation = executor.observe_worktree(task["order"])
                    except repo_executor.RepoExecutorError as exc:
                        raise ParallelError(
                            f"terminal task-{task['order']} resource audit blocked: {exc}"
                        ) from exc
                    if (observation.get("exists")
                            or observation.get("registered")
                            or observation.get("task_ref_tip") is not None):
                        raise ParallelError(
                            f"terminal task-{task['order']} still has worktree/ref resources")
                supervisor._audit_terminal_receipt_projection()
                supervisor._audit_terminal_worker_archives()
                supervisor._audit_durable_finalization(current_status)
                supervisor.checkpoint(
                    active=False, persist_aggregate=False)
            finally:
                executor.close()
        return 0

    _validate_recovery_action_legality(existing.aggregate, action)
    bootstrap = _install_bootstrap_control(existing, action)
    bootstrap_request_id = bootstrap["request_id"]
    session = uuid.uuid4().hex
    generation = existing.generation + 1
    with SupervisorRunLock(
            existing.workspace.dir / ".run.lock",
            session=session, generation=generation):
        current = _load_existing_parallel_run(workspace_root, name)
        if current.generation != existing.generation:
            raise ParallelError("parallel generation 在 recovery claim 前已改變")
        if current.artifacts.manifest_hash != existing.artifacts.manifest_hash:
            raise ParallelError("parallel immutable run 在 recovery claim 前已改變")
        (control_spool, recovered_controls, claimed_action,
         recovered_control_generation) = (
            _claimed_control_recovery(
                current.artifacts, current.aggregate,
                next_generation=generation))
        aggregate = _apply_claimed_control(
            dict(current.aggregate), claimed_action,
            recovered_control_generation)
        _validate_recovery_action_legality(
            aggregate, str(bootstrap["action"]))
        assigned_control_generation = (
            current.aggregate["control_generation"] + 1)
        if (recovered_control_generation
                == current.aggregate["control_generation"] + 1):
            assigned_control_generation += 1
        bootstrap = _claim_bootstrap_control(
            current,
            bootstrap_request_id,
            session=session,
            generation=generation,
            assigned_control_generation=assigned_control_generation,
        )
        if bootstrap["state"] == "applied":
            return 0
        _claim_supervisor_generation(
            current.artifacts,
            expected_generation=existing.generation,
            generation=generation,
            session=session,
        )
        effective_action = _effective_recovery_action(
            str(bootstrap["action"]), claimed_action)
        missing_secrets = ()
        if (effective_action == "resume"
                and aggregate["terminal_intent"] is None):
            missing_secrets = missing_required_secret_names(
                current.artifacts.run_config["environment"])
        spec = build_repo_spec(
            current.artifacts,
            pending_launch_hash=current.pending_launch_hash,
            supervisor_session=session,
            generation=generation,
        )
        executor = repo_executor.RepoExecutor(spec)
        try:
            supervisor = ParallelSupervisor(
                workspace_root=workspace_root,
                workspace=current.workspace,
                artifacts=current.artifacts,
                aggregate=aggregate,
                executor=executor,
                pending_launch_hash=current.pending_launch_hash,
                session=session,
                generation=generation,
                bootstrap_required=False,
            )
            # Classify incomplete startup before the generic replay path.  A
            # CREATE/GATE/REMOVE lease must never mutate repository state when
            # initialization authority is absent or partial; only the exact
            # canonical PREFLIGHT/INITIALIZE requests may be fenced and replayed.
            startup_state = supervisor.recover_startup_initialization(
                reconcile_pending=True,
                initialize_pristine=not bool(missing_secrets))
            executor, _recovered_operation = _reconcile_executor_lease(
                spec, executor)
            supervisor.executor = executor
            # First commit any previously claimed live-owner control.  Its
            # response must land before the later bootstrap generation is
            # projected, otherwise a crash would leave an older unanswered
            # claim hidden behind a newer aggregate control_generation.
            supervisor.checkpoint()
            _publish_recovered_control_responses(
                control_spool, recovered_controls,
                run_id=current.artifacts.manifest["run_id"])
            supervisor.aggregate = _apply_bootstrap_control(
                supervisor.aggregate, bootstrap)
            supervisor.aggregate = _prepare_recovery_status(
                supervisor.aggregate, effective_action)
            try:
                if missing_secrets:
                    # A missing launch secret forbids new validator/worker
                    # payloads, but it cannot bypass orphan/lease fencing.  A
                    # fully initialized run is reconciled without dispatch;
                    # an exhaustively proven pristine startup needs no refs or
                    # RepoExecutor operation at all.
                    if startup_state is not None:
                        supervisor.reconcile_existing()
                    supervisor.aggregate["error"] = (
                        "Resume 缺少必要 secret 環境變數："
                        + ", ".join(missing_secrets))
                    if supervisor.aggregate["status"] != "blocked":
                        supervisor.aggregate = parallel_state.transition_run_status(
                            supervisor.aggregate, "blocked")
                    supervisor.quiesce_blocked()
                    _mark_bootstrap_control_applied(
                        current.artifacts,
                        bootstrap_request_id,
                        supervisor.aggregate,
                    )
                    return 2
                # This aggregate/base checkpoint is the durable commit for the
                # no-owner action.  Only then may bootstrap.json acknowledge it.
                supervisor.checkpoint()
                _mark_bootstrap_control_applied(
                    current.artifacts,
                    bootstrap_request_id,
                    supervisor.aggregate,
                )
                supervisor.recover_startup_initialization()
                if effective_action == "resume":
                    if supervisor.aggregate["status"] == "finalizing":
                        supervisor.reconcile_existing()
                        supervisor.finalize_completed()
                        return 0
                    if supervisor.aggregate["status"] in {
                            "cancel_requested", "finalizing_cancel"}:
                        supervisor.reconcile_existing(explicit_abort=True)
                        return supervisor.abort()
                    return supervisor.run()
                supervisor.reconcile_existing(
                    explicit_abort=(
                        effective_action == "abort"
                        or supervisor.aggregate["status"] in {
                            "cancel_requested", "finalizing_cancel"}
                    ))
                if effective_action == "pause":
                    if supervisor.aggregate["status"] in {
                            "cancel_requested", "finalizing_cancel"}:
                        return supervisor.abort()
                    return supervisor.pause()
                return supervisor.abort()
            except KeyboardInterrupt:
                supervisor.aggregate["error"] = (
                    "supervisor interrupted; durable terminal intent preserved")
                if supervisor.aggregate["status"] not in RUN_TERMINAL | {"blocked"}:
                    supervisor.aggregate = parallel_state.transition_run_status(
                        supervisor.aggregate, "blocked")
                try:
                    supervisor.quiesce_blocked()
                except (OSError, ValueError, ParallelError,
                        parallel_state.ParallelStateError,
                        parallel_spool.SpoolError):
                    supervisor.checkpoint(active=True)
                return 2
            except (OSError, ValueError, ParallelError,
                    parallel_state.ParallelStateError,
                    parallel_spool.SpoolError) as exc:
                supervisor.aggregate["error"] = str(exc)
                if supervisor.aggregate["status"] not in RUN_TERMINAL | {"blocked"}:
                    supervisor.aggregate = parallel_state.transition_run_status(
                        supervisor.aggregate, "blocked")
                try:
                    supervisor.quiesce_blocked()
                except (OSError, ValueError, ParallelError,
                        parallel_state.ParallelStateError,
                        parallel_spool.SpoolError):
                    # Fence proof failed: keep the durable owner projection
                    # active/stale so a later recovery cannot mistake this for
                    # a clean handoff merely because the run lock is released.
                    supervisor.checkpoint(active=True)
                return 2
        finally:
            executor.close()


def control_existing_parallel(
    workspace_root: Path, name: str, action: str,
) -> int:
    existing = _load_existing_parallel_run(workspace_root, name)
    owner = _active_supervisor_owner(existing)
    if owner is not None:
        if action == "resume":
            raise ParallelError("parallel supervisor 已在執行，不能重複 Resume")
        return _control_request(existing, owner, action)
    return _recover_existing_parallel(workspace_root, name, action)


def start_parallel(args, workspace_root: Path) -> int:
    repo = Path(args.repo).expanduser().resolve(strict=True)
    name = args.name or repo.name
    if not loop_mod.valid_workspace_name(name):
        raise ParallelError(f"workspace name 不合法：{name!r}")
    config = _runtime_config_from_args(args, repo)
    require_required_secrets(config["environment"])
    workspace = loop_mod.Workspace(name)
    if workspace.state_path.exists() or workspace.checkpoint_path.exists():
        state = workspace.load_state()
        status = parallel_run_status(state)
        if status in RUN_NONTERMINAL:
            raise ParallelError(
                f"workspace {name} 仍有 {status} parallel run；請 resume/abort")
        raise ParallelError(f"workspace {name} 已有 state；請使用新的 --name")
    run_id = _new_run_id(workspace_root, name)
    session = uuid.uuid4().hex
    generation = 1
    with SupervisorRunLock(
            workspace.dir / ".run.lock", session=session,
            generation=generation):
        # The optimistic check above is only a fast path.  This locked re-read
        # is the ownership linearization point and prevents a delayed second
        # starter from overwriting the first run's terminal/base projection.
        if workspace.state_path.exists() or workspace.checkpoint_path.exists():
            state = workspace.load_state()
            status = parallel_run_status(state)
            if status in RUN_NONTERMINAL:
                raise ParallelError(
                    f"workspace {name} 仍有 {status} parallel run；請 resume/abort")
            raise ParallelError(f"workspace {name} 已有 state；請使用新的 --name")
        # The plan handoff is consumed only after the base single-writer lock
        # linearizes this starter.  One no-follow regular-file open/read binds
        # the exact raw bytes supplied by Dashboard before JSON validation and
        # before any immutable run artifact or repository mutation.
        plan = load_frozen_plan(
            Path(args.import_plan),
            expected_raw_sha256=getattr(args, "expected_plan_sha256", None),
        )
        try:
            launcher_fence = repo_owner.RepoOwnerFence.claim(
                repo,
                owner_kind=repo_owner.OwnerKind.PARALLEL_LAUNCHER,
                workspace=workspace.dir,
                state_path=workspace.state_path,
                session=session,
            )
        except repo_owner.RepoOwnerError as exc:
            raise ParallelError(
                f"parallel launcher repository owner audit blocked: {exc}"
            ) from exc
        try:
            # No immutable run artifact or base projection is published until
            # the same-repository common-dir owner audit has linearized.
            branch, start_sha = _repository_start_identity(
                repo, owner_fence=launcher_fence)
            run_dir = parallel_state.derive_run_directory(
                workspace_root, name, run_id)
            gate_command = build_gate_client_command(
                python_executable=sys.executable,
                run_dir=run_dir,
                wait_timeout=config["validate_timeout"],
            )
            artifacts = parallel_state.materialize_run_artifacts(
                workspace_root, name, run_id, plan, config, start_sha, branch,
                gate_command,
            )
            aggregate = parallel_state.build_initial_aggregate(run_id, plan)
            parallel_state.atomic_write_json(
                artifacts.run_dir, "aggregate.json", aggregate)
            _initialize_supervisor_generation(
                artifacts, session=session)
            workspace.save_state(project_base_state(
                workspace,
                artifacts,
                aggregate,
                (),
                supervisor_pid=os.getpid(),
                supervisor_session=session,
                supervisor_generation=generation,
            ))
        except BaseException:
            try:
                launcher_fence.terminalize(
                    "parallel-launch-failed-before-handoff")
            finally:
                launcher_fence.close()
            raise
        try:
            launcher_fence.terminalize(
                "parallel-launch-ready-for-executor")
        finally:
            launcher_fence.close()
        pending_launch_hash = _pending_launch_hash(artifacts)
        spec = build_repo_spec(
            artifacts,
            pending_launch_hash=pending_launch_hash,
            supervisor_session=session,
            generation=generation,
        )
        executor = repo_executor.RepoExecutor(spec)
        supervisor = ParallelSupervisor(
            workspace_root=workspace_root,
            workspace=workspace,
            artifacts=artifacts,
            aggregate=aggregate,
            executor=executor,
            pending_launch_hash=pending_launch_hash,
            session=session,
            generation=generation,
        )
        supervisor.checkpoint()
        try:
            return supervisor.run()
        finally:
            executor.close()


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--name", default=None)
    parser.add_argument("--goal", default="goal.md")
    parser.add_argument("--plan-doc", default="")
    parser.add_argument("--agent-cmd", required=True)
    parser.add_argument("--validate-cmd", required=True)
    parser.add_argument("--flag-threshold", type=int, default=loop_mod.FLAG_THRESHOLD)
    parser.add_argument("--done-threshold", type=int, default=loop_mod.DONE_THRESHOLD)
    parser.add_argument("--red-limit", type=int, default=loop_mod.RED_LIMIT)
    parser.add_argument("--stall-limit", type=int, default=loop_mod.STALL_LIMIT)
    parser.add_argument("--stuck-stop", action="store_true")
    parser.add_argument("--stuck-stop-count", type=int, default=loop_mod.STUCK_STOP_COUNT)
    parser.add_argument("--round-timeout", type=float, default=loop_mod.ROUND_TIMEOUT_MIN)
    parser.add_argument("--agent-backoff-max", type=float, default=loop_mod.AGENT_BACKOFF_MAX_SEC)
    parser.add_argument("--validate-timeout", type=float, default=loop_mod.VALIDATE_TIMEOUT_SEC)
    parser.add_argument("--notify-cmd", default="")
    parser.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL)
    parser.add_argument("--worker-restart-limit", type=int,
                        default=DEFAULT_WORKER_RESTART_LIMIT)
    parser.add_argument(
        "--path-addition", action="append", default=[], metavar="ABS_PATH",
        help="freeze one explicit PATH prefix in immutable run-config.json")
    parser.add_argument(
        "--non-secret-env", action="append", default=[], metavar="NAME=VALUE",
        help="freeze one explicitly non-secret JSON scalar environment value")
    parser.add_argument(
        "--required-secret-name", action="append", default=[], metavar="NAME",
        help="require a non-empty inherited secret by name; never stores its value")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="loop-agent-lite frozen-plan parallel supervisor")
    parser.add_argument("--workspace-root", default=None, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="建立新的 parallel run")
    _add_runtime_arguments(start)
    start.add_argument("--import-plan", required=True)
    start.add_argument("--expected-plan-sha256", default=None)
    for action in ("resume", "pause", "abort"):
        command = commands.add_parser(action, help=f"{action} 既有 parallel run")
        command.add_argument("name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the durable supervisor lifecycle."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    workspace_root = (Path(args.workspace_root).expanduser().resolve()
                      if args.workspace_root else default_workspace_root())
    loop_mod.WORKSPACE_ROOT = workspace_root
    if args.command == "start":
        return start_parallel(args, workspace_root)
    return control_existing_parallel(workspace_root, args.name, args.command)


if __name__ == "__main__":
    def _graceful_stop_signal(*_):
        """Route SIGTERM/CTRL+BREAK through the supervisor's safe Pause path."""
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _graceful_stop_signal)
    compat.register_windows_break_handler(_graceful_stop_signal)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("⏸ parallel supervisor 已安全中斷；可使用 Resume 繼續", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, ParallelError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        raise SystemExit(1)
