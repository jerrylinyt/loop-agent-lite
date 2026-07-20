#!/usr/bin/env python3
"""engine.ralph 監督層 CLI 端到端測試(真 subprocess + 真 git,fake ralph.sh)。

跑法:  python3 -m unittest tests.test_ralph_integration      # 或
       python3 tests/test_ralph_integration.py

用 tests/fixtures/fake_ralph.sh 取代真正的 snarktank ralph.sh:同樣的位置參數
`<iters> <tool> <model>`、同樣每輪標記一個 story、寫 progress.txt、git commit、
全部完成時印 `<promise>COMPLETE</promise>` 並 exit 0,達 max 未完成則 exit 1。
純函式層(parse_prd_json/build_ralph_argv/…)的單元測試見 tests/test_ralph.py。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from engine import loop as loop_mod  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RALPH_CMD = [sys.executable, "-m", "engine.ralph"]

_HAS_BASH_AND_GIT = shutil.which("bash") is not None and shutil.which("git") is not None
_SKIP_REASON = "需要 PATH 上有 bash 與 git 才能跑 ralph 監督層 CLI 端到端測試"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def _make_ralph_repo(root: Path):
    """建立一個 git repo,把 fixtures 的 fake_ralph.sh + prd.json 放進 repo 內的
    ralph 子目錄(貼近真正 ralph.sh 不一定放在 repo root 的情境)並 commit,
    讓監督層啟動時捕捉到的 base_sha 乾淨(不含 fake ralph script 本身)。"""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ralph-integration@example.invalid")
    _git(repo, "config", "user.name", "Ralph Integration Test")
    ralph_dir = repo / "ralph"
    ralph_dir.mkdir()
    shutil.copy(FIXTURES / "fake_ralph.sh", ralph_dir / "fake_ralph.sh")
    shutil.copy(FIXTURES / "prd.json", ralph_dir / "prd.json")
    (ralph_dir / "fake_ralph.sh").chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed ralph dir")
    return repo, ralph_dir


def _run_supervisor(repo, ralph_dir, workspace_root, name, iterations):
    env = {
        **os.environ,
        "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root),
        "FAKE_RALPH_SLEEP": "0",
        "PYTHONPATH": str(REPO_ROOT),
    }
    return subprocess.run(
        [*RALPH_CMD,
         "--repo", str(repo),
         "--name", name,
         "--ralph-cmd", f"bash {ralph_dir}/fake_ralph.sh",
         "--ralph-dir", str(ralph_dir),
         "--iterations", str(iterations),
         "--tool", "claude",
         "--args-style", "positional"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, env=env, timeout=120,
    )


@unittest.skipUnless(_HAS_BASH_AND_GIT, _SKIP_REASON)
class TestRalphSupervisorCli(unittest.TestCase):
    """`python -m engine.ralph` 完整跑一輪 fake ralph,驗證 state.json 投影與 log 落地。"""

    def test_completes_all_stories_and_writes_valid_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo, ralph_dir = _make_ralph_repo(root)
            workspace_root = root / "workspaces"

            result = _run_supervisor(repo, ralph_dir, workspace_root, "t", iterations=5)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            workspace_dir = workspace_root / "t"
            state_path = workspace_dir / "state.json"
            self.assertTrue(state_path.is_file())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            loop_mod.validate_state_shape(state, "state.json")  # 不得丟例外

            self.assertEqual(state["runner"], "ralph")
            self.assertEqual(state["phase"], "done")
            self.assertIsNone(state["loop"]["pid"])

            ralph_block = state["ralph"]
            self.assertEqual(ralph_block["stories_total"], 2)
            self.assertEqual(ralph_block["stories_done"], 2)
            self.assertEqual(ralph_block["exit_reason"], "completed")
            self.assertTrue(ralph_block["sentinel_complete"])
            self.assertGreaterEqual(ralph_block["commit_count"], 2)

            console_path = workspace_dir / "console.log"
            run_log_path = workspace_dir / "logs" / "ralph-run.log"
            self.assertTrue(console_path.is_file())
            self.assertTrue(run_log_path.is_file())
            console_log = console_path.read_text(encoding="utf-8")
            run_log = run_log_path.read_text(encoding="utf-8")
            self.assertIn("Ralph Iteration", console_log)
            self.assertIn("Ralph Iteration", run_log)

    def test_single_iteration_cannot_finish_two_stories(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo, ralph_dir = _make_ralph_repo(root)
            workspace_root = root / "workspaces"

            result = _run_supervisor(repo, ralph_dir, workspace_root, "t-one", iterations=1)

            # iterations_exhausted/failed 都回傳 1;fake_ralph.sh 本身在耗盡迭代時也 exit 1。
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

            state = json.loads(
                (workspace_root / "t-one" / "state.json").read_text(encoding="utf-8"))
            loop_mod.validate_state_shape(state, "state.json")

            self.assertEqual(state["phase"], "done")
            ralph_block = state["ralph"]
            self.assertEqual(ralph_block["exit_reason"], "iterations_exhausted")
            self.assertEqual(ralph_block["stories_total"], 2)
            self.assertEqual(ralph_block["stories_done"], 1)


if __name__ == "__main__":
    unittest.main()
