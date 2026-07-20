#!/usr/bin/env python3
"""engine.ralph 的 usage-limit 偵測／自動重啟／模型降級測試。

- 純函式:parse_reset_target 各種格式、compile_limit_patterns 分層。
- CLI 端到端(真 subprocess + git + fake ralph):偵測→降級→完成、never-recover→giveup、
  以及 no-progress gate 防誤判(agent 寫 rate-limit 程式碼且有 commit 不觸發)。

fake_ralph_limit.sh:model 含 "limited" 時每輪印 tier-1 用量上限字樣且不做任何變更
(no-progress);否則正常推進 story(並刻意輸出含 tier-2 字樣的一行,驗證有進展不誤判)。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from engine import loop as loop_mod  # noqa: E402
from engine import ralph as ralph_mod  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RALPH_CMD = [sys.executable, "-m", "engine.ralph"]
_HAS_BASH_AND_GIT = shutil.which("bash") is not None and shutil.which("git") is not None
_SKIP_REASON = "需要 PATH 上有 bash 與 git 才能跑 usage-limit 端到端測試"


class TestParseResetTarget(unittest.TestCase):
    """reset 時間解析:五種格式、過去時間與無匹配都要正確。"""

    def test_epoch_seconds(self):
        now = 1_700_000_000.0
        target = ralph_mod.parse_reset_target("Claude usage limit reached|1700003600", now)
        self.assertEqual(target, 1700003600)

    def test_epoch_millis(self):
        now = 1_700_000_000.0
        target = ralph_mod.parse_reset_target("usage limit reached|1700003600000", now)
        self.assertEqual(target, 1700003600)

    def test_relative_minutes(self):
        now = 1000.0
        target = ralph_mod.parse_reset_target("please try again in 5 minutes", now)
        self.assertAlmostEqual(target, 1000.0 + 300, delta=1)

    def test_relative_seconds(self):
        now = 1000.0
        target = ralph_mod.parse_reset_target("retry after 30 seconds", now)
        self.assertAlmostEqual(target, 1030, delta=1)

    def test_retry_after_header(self):
        now = 2000.0
        target = ralph_mod.parse_reset_target("Retry-After: 120", now)
        self.assertAlmostEqual(target, 2120, delta=1)

    def test_iso(self):
        now = 0.0
        target = ralph_mod.parse_reset_target("limit resets at 2030-01-01T00:00:00+00:00", now)
        self.assertIsNotNone(target)
        self.assertGreater(target, now)

    def test_past_epoch_returns_none(self):
        now = 1_800_000_000.0
        self.assertIsNone(ralph_mod.parse_reset_target("usage limit reached|1700000000", now))

    def test_no_match_returns_none(self):
        self.assertIsNone(ralph_mod.parse_reset_target("nothing relevant here", 1000.0))


class TestCompileLimitPatterns(unittest.TestCase):
    """內建 tier-1/tier-2 pattern 與 config 追加 pattern。"""

    def test_builtin_tiers_present(self):
        compiled = ralph_mod.compile_limit_patterns()
        tiers = {tier for _re, tier, _src in compiled}
        self.assertEqual(tiers, {1, 2})
        self.assertTrue(any(src == "builtin" for _re, _t, src in compiled))

    def test_tier1_matches_claude_epoch_line(self):
        compiled = ralph_mod.compile_limit_patterns()
        line = "Claude usage limit reached|1893456000"
        hit = [(tier, src) for pat, tier, src in compiled if pat.search(line)]
        self.assertTrue(any(tier == 1 for tier, _src in hit))

    def test_extra_custom_pattern_is_tier2_custom(self):
        compiled = ralph_mod.compile_limit_patterns(["opencode: quota drained"])
        self.assertTrue(any(src == "custom" and tier == 2 for _re, tier, src in compiled))

    def test_bad_custom_regex_skipped(self):
        compiled = ralph_mod.compile_limit_patterns(["(unclosed"])
        self.assertFalse(any(src == "custom" for _re, _t, src in compiled))


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _make_repo(root: Path, script_name: str):
    """建 git repo,把指定 fake ralph 與 prd.json 放 repo root 並 commit。"""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ul@example.invalid")
    _git(repo, "config", "user.name", "UL Test")
    shutil.copy(FIXTURES / script_name, repo / "ralph.sh")
    shutil.copy(FIXTURES / "prd.json", repo / "prd.json")
    (repo / "ralph.sh").chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _run(repo, workspace_root, name, *, model, action, iterations=5, fallback=None,
         auto_restart_max=4, extra_env=None, timeout=200):
    env = {
        **os.environ,
        "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root),
        "PYTHONPATH": str(REPO_ROOT),
        "FAKE_RALPH_SLEEP": "0",
        "RALPH_KILL_GRACE_SEC": "1",
        "RALPH_TEST_WAIT_CAP_SEC": "0.4",
        "RALPH_SETTLE_SEC": "0",
    }
    env.update(extra_env or {})
    cmd = [*RALPH_CMD, "--repo", str(repo), "--name", name,
           "--ralph-cmd", f"bash {repo}/ralph.sh", "--ralph-dir", str(repo),
           "--iterations", str(iterations), "--tool", "claude", "--model", model,
           "--args-style", "positional", "--usage-limit-action", action,
           "--auto-restart-max", str(auto_restart_max),
           "--fallback-models", json.dumps(fallback or [])]
    return subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True,
                          env=env, timeout=timeout)


@unittest.skipUnless(_HAS_BASH_AND_GIT, _SKIP_REASON)
class TestUsageLimitEndToEnd(unittest.TestCase):
    """真 subprocess 驗證偵測 → 降級/等待/放棄 的外圈行為。"""

    def _state(self, workspace_root, name):
        state = json.loads((workspace_root / name / "state.json").read_text(encoding="utf-8"))
        loop_mod.validate_state_shape(dict(state), "state.json")
        return state

    def test_detect_then_downgrade_completes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = _make_repo(root, "fake_ralph_limit.sh")
            ws = root / "ws"
            result = _run(repo, ws, "dg", model="opus-limited", action="downgrade",
                          fallback=["sonnet-ok"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = self._state(ws, "dg")
            block = state["ralph"]
            self.assertEqual(state["phase"], "done")
            self.assertEqual(block["exit_reason"], "completed")
            self.assertEqual(block["stories_done"], 2)
            self.assertEqual(block["active_model"], "sonnet-ok")
            self.assertGreaterEqual(block["restart_attempt"], 1)
            self.assertIsInstance(block["usage_limit"], dict)
            self.assertEqual(block["usage_limit"]["to_model"], "sonnet-ok")
            self.assertEqual(block["usage_limit"]["detection"], "heuristic")
            self.assertEqual(block["usage_limit"]["matches"][-1]["tier"], 1)

    def test_never_recovers_gives_up(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = _make_repo(root, "fake_ralph_limit.sh")
            ws = root / "ws"
            result = _run(repo, ws, "gv", model="opus-limited", action="restart",
                          auto_restart_max=2)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            block = self._state(ws, "gv")["ralph"]
            self.assertEqual(block["exit_reason"], "usage_limit_giveup")
            self.assertGreater(block["restart_attempt"], 2)
            self.assertEqual(block["usage_limit"]["state"], "giveup")

    def test_progress_gate_suppresses_false_positive(self):
        # 健康 model:每輪輸出含 tier-2「rate limit」字樣但有 commit(有進展)→ 不得誤判。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = _make_repo(root, "fake_ralph_limit.sh")
            ws = root / "ws"
            result = _run(repo, ws, "fp", model="sonnet-normal", action="downgrade",
                          fallback=["haiku-ok"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            block = self._state(ws, "fp")["ralph"]
            self.assertEqual(block["exit_reason"], "completed")
            self.assertEqual(block["stories_done"], 2)
            self.assertEqual(block["restart_attempt"], 0)
            self.assertIsNone(block["usage_limit"])
            self.assertEqual(block["active_model"], "sonnet-normal")


if __name__ == "__main__":
    unittest.main()
