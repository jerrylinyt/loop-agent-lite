"""One independent Dashboard-to-supervisor contender for the handoff race."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from engine import dashboard
from engine import loop as loop_mod
from engine import platform_compat as compat
from engine import repo_owner


def _runtime_config(
        repo: Path, agent: Path, validator: Path, label: str,
        agent_ready: Path, agent_finish: Path) -> dict:
    return {
        "repo": str(repo.resolve()),
        "goal": "goal.md",
        "plan_doc": "",
        "agent_cmd": compat.join_command([
            sys.executable, str(agent), label,
            str(agent_ready), str(agent_finish),
        ]),
        "validate_cmd": compat.join_command([
            sys.executable, str(validator),
        ]),
        "flag_threshold": 2,
        "done_threshold": 1,
        "red_limit": 3,
        "stall_limit": 20,
        "stuck_stop": False,
        "stuck_stop_count": 20,
        "round_timeout": 1.0,
        "agent_backoff_max": 1.0,
        "validate_timeout": 10.0,
        "notify_cmd": "",
        "max_parallel": 1,
        "worker_restart_limit": 1,
        "environment": {
            "path_additions": [],
            "non_secret": {},
            "required_secret_names": [],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--go", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--validator", required=True)
    parser.add_argument("--agent-ready", required=True)
    parser.add_argument("--agent-finish", required=True)
    parser.add_argument("--barrier-timeout", type=float, default=30.0)
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve(strict=True)
    workspace_root = Path(args.workspace_root).expanduser().resolve(strict=True)
    ready_path = Path(args.ready).expanduser().resolve()
    go_path = Path(args.go).expanduser().resolve()
    agent = Path(args.agent).expanduser().resolve(strict=True)
    validator = Path(args.validator).expanduser().resolve(strict=True)
    agent_ready = Path(args.agent_ready).expanduser().resolve()
    agent_finish = Path(args.agent_finish).expanduser().resolve()
    dashboard.ROOT = workspace_root
    loop_mod.WORKSPACE_ROOT = workspace_root

    plan = [{
        "order": 1,
        "task": args.task,
        "ref": None,
        "stack": 1,
    }]

    def stage(fence, claimed_workspace):
        dashboard._assert_launcher_repo_clean(fence, repo)
        dashboard._prepare_launcher_workspace(claimed_workspace)
        staged_path, staged_hash = dashboard._stage_parallel_plan(
            claimed_workspace, plan)
        primary_ref, primary_sha = dashboard.parallel_mod._repository_start_identity(
            repo, owner_fence=fence)
        return staged_path, staged_hash, primary_ref, primary_sha

    staged_path, staged_hash, primary_ref, primary_sha = (
        dashboard._run_dashboard_launcher(
            repo, args.name, repo_owner.OwnerKind.PARALLEL_LAUNCHER, stage))
    command = dashboard.build_parallel_command(
        "start",
        args.name,
        repo=repo,
        import_plan=staged_path,
        expected_plan_sha256=staged_hash,
        expected_primary_ref=primary_ref,
        expected_primary_sha=primary_sha,
        config=_runtime_config(
            repo, agent, validator, args.label,
            agent_ready, agent_finish),
    )
    ready_record = {
        "label": args.label,
        "task": args.task,
        "staged_path": str(staged_path),
        "staged_sha256": staged_hash,
        "expected_primary_ref": primary_ref,
        "expected_primary_sha": primary_sha,
        "argv": command,
    }
    loop_mod.atomic_write_bytes(
        ready_path,
        json.dumps(
            ready_record, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n",
    )

    deadline = time.monotonic() + args.barrier_timeout
    while not go_path.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"go barrier was not released:{go_path}")
        time.sleep(0.01)

    environment = dict(os.environ)
    environment.update({
        "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root),
        "PYTHONUTF8": "1",
    })
    # Use list-form spawning so Windows preserves the two shell-command values
    # as single argv entries.  stdout/stderr remain inherited by the harness,
    # and the child remains in the same POSIX process group / Windows Job.
    return subprocess.run(command, env=environment).returncode


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(97)
    raise SystemExit(result)
