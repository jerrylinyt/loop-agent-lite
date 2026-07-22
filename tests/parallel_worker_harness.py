"""Shared real-Git authority harness for managed worker subprocess tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from engine import loop as loop_mod
from engine import parallel
from engine import parallel_state
from engine import platform_compat as compat


def git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@dataclass
class AuthorizedWorkerHarness:
    command: list[str]
    worker_repo: Path
    worker_workspace: str
    workspace_root: Path
    artifacts: parallel_state.ValidatedRunArtifacts
    launch_reservation: dict
    base_lock: object

    def close(self) -> None:
        try:
            compat.unlock_file(self.base_lock)
        finally:
            self.base_lock.close()


def prepare_authorized_worker(
    *,
    root: Path,
    primary_repo: Path,
    plan: list[dict],
    order: int,
    agent_cmd: str,
    validate_cmd: str,
    gate_cmd: str,
    run_id: str = "a1b2c3d4",
    parent: str = "base",
    dispatch_token: str = "supervisor-dispatch-token",
    flag_threshold: int = 1,
    done_threshold: int = 1,
    red_limit: int = 3,
    stall_limit: int = 20,
    stuck_stop: bool = False,
    stuck_stop_count: int = 100,
    round_timeout: float = 1,
    agent_backoff_max: float = 60,
    validate_timeout: float = 20,
) -> AuthorizedWorkerHarness:
    """Create immutable authority, canonical worktree, and a live parent lock."""
    workspace_root = root / "workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    head = git(primary_repo, "rev-parse", "HEAD").stdout.strip()
    integration_branch = git(primary_repo, "symbolic-ref", "-q", "HEAD").stdout.strip()
    config = {
        "repo": str(primary_repo.resolve()),
        "primary_repo": str(primary_repo.resolve()),
        "goal": "goal.md",
        "plan_doc": "",
        "agent_cmd": agent_cmd,
        "validate_cmd": validate_cmd,
        "flag_threshold": flag_threshold,
        "done_threshold": done_threshold,
        "red_limit": red_limit,
        "stall_limit": stall_limit,
        "stuck_stop": stuck_stop,
        "stuck_stop_count": stuck_stop_count,
        "round_timeout": round_timeout,
        "validate_timeout": validate_timeout,
        "agent_backoff_max": agent_backoff_max,
        "notify_cmd": "",
        "max_parallel": 2,
        "worker_restart_limit": 3,
        "environment": {
            "path_additions": [], "non_secret": {}, "required_secret_names": [],
        },
        "max_rounds": 0,
        "pause_after_plan": False,
        "allow_serial_stack": False,
    }
    tokens = {
        task["order"]: (dispatch_token if task["order"] == order
                        else f"{dispatch_token}-task-{task['order']}")
        for task in plan
    }
    artifacts = parallel_state.materialize_run_artifacts(
        workspace_root, parent, run_id, plan, config, head,
        integration_branch, gate_cmd, dispatch_tokens=tokens,
    )
    assignment = dict(artifacts.assignments[order])
    assignment.update({
        "launch_spec_hash": artifacts.assignment_hashes[order],
        "manifest_hash": artifacts.manifest_hash,
    })
    worker_repo = Path(assignment["worker_repo"])
    worker_repo.parent.mkdir(parents=True, exist_ok=True)
    git(primary_repo, "update-ref", artifacts.manifest["integration_ref"], head)
    git(primary_repo, "update-ref", assignment["task_ref"], head)
    branch_name = assignment["task_ref"].removeprefix("refs/heads/")
    git(primary_repo, "worktree", "add", "--force", str(worker_repo), branch_name)
    attached = git(worker_repo, "symbolic-ref", "HEAD").stdout.strip()
    if attached != assignment["task_ref"]:
        raise AssertionError(
            f"worker worktree is not attached to {assignment['task_ref']}: {attached!r}")

    base_dir = workspace_root / parent
    supervisor_session = "a" * 32
    supervisor_generation = 1
    aggregate = parallel_state.build_initial_aggregate(run_id, artifacts.plan)
    aggregate = parallel_state.transition_run_status(aggregate, "running")
    aggregate = parallel_state.transition_task(
        aggregate, order, resource_state="provisioning")
    parallel_state.atomic_write_json(
        artifacts.run_dir, "aggregate.json", aggregate)
    base_state = {
        "phase": "exec",
        "runner": "parallel-supervisor",
        "plan": [dict(task) for task in artifacts.plan],
        "completed": [],
        "loop": {
            "pid": os.getpid(),
            "session_id": supervisor_session,
            "started_at": "2026-01-01T00:00:00+00:00",
        },
        "parallel": {
            "run_id": run_id,
            "manifest_hash": artifacts.manifest_hash,
            "status": "running",
            "supervisor_generation": supervisor_generation,
            "batch": assignment["batch_index"],
            "tasks": [dict(task) for task in aggregate["tasks"]],
        },
    }
    state_bytes = json.dumps(base_state, ensure_ascii=False, indent=2).encode("utf-8")
    loop_mod.write_checkpointed_state(base_dir / "state.json", state_bytes)
    lock_fd = loop_mod._open_regular(base_dir / ".run.lock", os.O_RDWR | os.O_CREAT)
    base_lock = os.fdopen(lock_fd, "a+b", closefd=True)
    compat.lock_file(base_lock, blocking=False)
    base_lock.seek(0)
    base_lock.truncate()
    base_lock.write(json.dumps({
        "pid": os.getpid(),
        "session_id": supervisor_session,
        "generation": supervisor_generation,
    }).encode("utf-8"))
    base_lock.flush()

    launch_reservation = parallel.publish_launch_reservation(
        artifacts, order,
        supervisor_session=supervisor_session,
        supervisor_generation=supervisor_generation,
        attempt=0,
        resume=False,
    )

    payload_command = parallel.build_worker_argv(
        python_executable=sys.executable,
        assignment=assignment,
        run_config=artifacts.run_config,
        plan_path=artifacts.run_dir / "plan.json",
        dispatch_token=dispatch_token,
        launch_reservation=launch_reservation,
    )
    command = [
        sys.executable, "-m", "tests.authorized_worker_launcher",
        "--run-dir", str(artifacts.run_dir),
        "--run-id", run_id,
        "--task", str(order),
        "--child-id", launch_reservation["request_id"],
        "--supervisor-session", supervisor_session,
        "--supervisor-generation", str(supervisor_generation),
        "--attempt", "0",
        "--",
        *payload_command,
    ]
    return AuthorizedWorkerHarness(
        command=command,
        worker_repo=worker_repo,
        worker_workspace=assignment["worker_workspace"],
        workspace_root=workspace_root,
        artifacts=artifacts,
        launch_reservation=launch_reservation,
        base_lock=base_lock,
    )
