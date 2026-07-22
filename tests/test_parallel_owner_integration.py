"""Common-dir owner fencing at the Parallel launcher boundary."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engine import loop as loop_mod
from engine import parallel
from engine import repo_owner
from engine import platform_compat as compat


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True,
    )


@unittest.skipUnless(shutil.which("git"), "requires git")
class TestParallelOwnerIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.workspace_root = self.root / "workspaces"
        self.repo.mkdir()
        self.workspace_root.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Parallel Owner Test")
        git(self.repo, "config", "user.email", "owner@example.invalid")
        (self.repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
        git(self.repo, "add", "goal.md")
        git(self.repo, "commit", "-qm", "initial")
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps([
            {"order": 1, "task": "owner audit", "stack": 1},
        ]), encoding="utf-8")
        self.args = SimpleNamespace(
            repo=str(self.repo), name="base", import_plan=str(self.plan),
            goal="goal.md", plan_doc="",
            agent_cmd=compat.join_command([sys.executable, "-c", "pass"]),
            validate_cmd=compat.join_command([sys.executable, "-c", "pass"]),
            flag_threshold=2, done_threshold=1, red_limit=3,
            stall_limit=4, stuck_stop=False, stuck_stop_count=5,
            round_timeout=1.0, agent_backoff_max=1.0,
            validate_timeout=5.0, notify_cmd="", max_parallel=1,
            worker_restart_limit=1,
        )

    def test_foreign_active_owner_blocks_before_run_artifacts_or_base_state(self):
        foreign_workspace = self.workspace_root / "ordinary"
        foreign_workspace.mkdir()
        foreign_state = foreign_workspace / "state.json"
        owner = repo_owner.RepoOwnerFence.claim(
            self.repo,
            owner_kind=repo_owner.OwnerKind.LOOP,
            workspace=foreign_workspace,
            state_path=foreign_state,
        )
        self.addCleanup(owner.close)
        with mock.patch.object(loop_mod, "WORKSPACE_ROOT", self.workspace_root):
            with self.assertRaisesRegex(
                    parallel.ParallelError, "owner audit blocked"):
                parallel.start_parallel(self.args, self.workspace_root)

        base = self.workspace_root / "base"
        self.assertFalse((base / "state.json").exists())
        self.assertFalse((base / "state.last-good.json").exists())
        self.assertFalse((base / "parallel").exists())
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker, owner.marker)


if __name__ == "__main__":
    unittest.main()
