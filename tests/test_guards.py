#!/usr/bin/env python3
"""協調層防線的回歸測試(stdlib only,無外部依賴)。

對應複審發現的三個 correctness 缺口,全部用真 git + 真 loop.py/work.py 驗證,不做 mock:
- #1 綠點錨定 fail-closed:green 未驗可達性/一致性,reset 回去會弄髒工作樹或還原錯版 goal。
- #2 竄改輪整輪作廢:同一輪偷改 protected + create-plan,竄改的 plan 不得存活。
- #3 原子寫並發:ThreadingHTTPServer 下多執行緒共用 tmp 會 truncate / FileNotFoundError。

跑法:  python3 -m unittest tests.test_guards      # 或  python3 tests/test_guards.py
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import loop as L  # noqa: E402
import dashboard as D  # noqa: E402

WORK_PY = str(REPO_ROOT / "work.py")
LOOP_PY = str(REPO_ROOT / "loop.py")
WS_ROOT = REPO_ROOT / "workspace"


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def make_repo(d):
    """建一個最小 git repo:固定 branch=main、goal.md 已 commit。"""
    repo = Path(d) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo, "config", "user.email", "a@b.c")
    git(repo, "config", "user.name", "t")
    (repo / "goal.md").write_text("GOAL v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "c0")
    return repo


class TestGreenAnchorValid(unittest.TestCase):
    """#1 green_anchor_valid:綠點錨定必須逐項 fail-closed。"""

    def test_matrix(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            head = git(repo, "rev-parse", "HEAD").stdout.strip()
            snap = Path(d) / "snap"
            snap.mkdir()
            (snap / "goal.md").write_bytes((repo / "goal.md").read_bytes())
            prot = ["goal.md"]

            self.assertTrue(L.green_anchor_valid(repo, head, snap, prot),
                            "一致的綠點(HEAD 且 blob 相符)應放行")
            self.assertFalse(L.green_anchor_valid(repo, "0" * 40, snap, prot),
                             "不存在的 sha 應擋下")
            self.assertFalse(L.green_anchor_valid(repo, None, snap, prot),
                             "None 應擋下")

            # 非 HEAD 祖先:另一 branch 的 commit
            git(repo, "checkout", "-q", "-b", "other")
            (repo / "x.txt").write_text("x")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "cx")
            other = git(repo, "rev-parse", "HEAD").stdout.strip()
            git(repo, "checkout", "-q", "main")
            self.assertFalse(L.green_anchor_valid(repo, other, snap, prot),
                             "非 HEAD 祖先的 sha 應擋下")

            # protected blob 與啟動快照分歧
            (snap / "goal.md").write_bytes(b"DIFFERENT CONTENT\n")
            self.assertFalse(L.green_anchor_valid(repo, head, snap, prot),
                             "green 的 protected 與啟動快照分歧應擋下")


class TestRestoreProtected(unittest.TestCase):
    """#1 restore_protected:green 不含該子目錄時不得 FileNotFoundError。"""

    def test_missing_subdir_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            g0 = git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "sub").mkdir()
            (repo / "sub" / "plan.md").write_text("p\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "c1")

            ws = L.Workspace.__new__(L.Workspace)
            ws.dir = Path(d) / "ws"
            (ws.dir / "snapshots").mkdir(parents=True)
            prot = ["goal.md", "sub/plan.md"]
            ws.snapshot_protected(repo, prot)

            git(repo, "reset", "--hard", g0)   # 回到不含 sub/ 的 commit
            git(repo, "clean", "-fdq")
            try:
                ws.restore_protected(repo, prot)
            except FileNotFoundError as e:
                self.fail(f"restore_protected 對不存在的子目錄崩了:{e!r}")


class TestAtomicWriteConcurrency(unittest.TestCase):
    """#3 atomic_write_bytes:多執行緒並發寫同一檔不得共用 tmp。"""

    def test_no_exception_and_intact(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "state.json"
            target.write_text("{}")
            errs = []

            def worker(tag):
                payload = json.dumps({"tag": tag, "pad": "x" * 500000}).encode()
                for _ in range(40):
                    try:
                        L.atomic_write_bytes(target, payload)
                    except Exception as e:  # noqa: BLE001
                        errs.append(type(e).__name__)

            ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()

            self.assertEqual(errs, [], f"並發原子寫不應丟例外,實得:{errs[:5]}...")
            json.loads(target.read_bytes())  # 最終檔完整、未被 truncate


class TestConsoleRotation(unittest.TestCase):
    """完整 console 必須在上限前輪替，且按新舊順序保留固定份數。"""

    def test_rotates_and_keeps_bounded_backups(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "console.log"
            L.append_console(target, "a" * 30, max_bytes=40, backups=2)
            L.append_console(target, "b" * 20, max_bytes=40, backups=2)
            L.append_console(target, "c" * 30, max_bytes=40, backups=2)

            self.assertEqual(target.read_text().strip(), "c" * 30)
            self.assertEqual((Path(d) / "console.log.1").read_text().strip(), "b" * 20)
            self.assertEqual((Path(d) / "console.log.2").read_text().strip(), "a" * 30)
            self.assertFalse((Path(d) / "console.log.3").exists())


class TestDashboardStateLockCoverage(unittest.TestCase):
    """#3 run/launch 必須和 edit/phase 共用 workspace lock,不能在 stopped check 後競態。"""

    def test_all_workspace_mutations_are_decorated(self):
        for method in ("api_launch", "api_run", "api_edit_state", "api_edit_config", "api_phase", "api_set_task"):
            self.assertTrue(hasattr(getattr(D.Handler, method), "__wrapped__"), f"{method} 必須套 workspace lock")

    def test_launch_blank_name_locks_repo_basename(self):
        entered = threading.Event()

        @D.with_state_lock(repo_fallback=True)
        def action(_self, _body):
            entered.set()

        lock = D._state_lock("demo-repo")
        lock.acquire()
        thread = threading.Thread(target=action, args=(object(), {"name": "", "repo": "/tmp/demo-repo"}))
        thread.start()
        try:
            self.assertFalse(entered.wait(0.05), "name 留空的 launch 應被 repo basename 對應的鎖擋住")
        finally:
            lock.release()
        thread.join(timeout=1)
        self.assertTrue(entered.is_set())


class TestValidateMustStayClean(unittest.TestCase):
    """preflight validate 不得靠副作用修改原始碼後仍放行,不論 rc 綠紅。"""

    def _assert_dirty_validator_blocked(self, rc):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            (repo / "tracked.txt").write_text("clean\n")
            git(repo, "add", "tracked.txt")
            git(repo, "commit", "-qm", "tracked")
            name = f"verifytmp_validate_dirty_{Path(d).name}_{rc}"
            wsd = WS_ROOT / name
            shutil.rmtree(wsd, ignore_errors=True)
            try:
                common = [sys.executable, LOOP_PY, "--repo", str(repo), "--name", name,
                          "--goal", "goal.md", "--agent-cmd", "true", "--max-rounds", "1"]
                if rc:
                    # 先建立一個可信 last_green_sha；舊行為會因 green 合法而放行紅色 dirty validator。
                    seeded = subprocess.run(common + ["--validate-cmd", "true"], capture_output=True, text=True)
                    self.assertEqual(seeded.returncode, 0, seeded.stdout + seeded.stderr)
                validator = Path(d) / "dirty_validator.py"
                validator.write_text(
                    "from pathlib import Path\n"
                    "p = Path('tracked.txt')\n"
                    "p.write_text(p.read_text() + 'dirty\\n')\n"
                    f"raise SystemExit({rc})\n"
                )
                result = subprocess.run(common + ["--validate-cmd", shlex.join([sys.executable, str(validator)])],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, "validate 弄髒工作樹必須 fail-closed")
                self.assertIn("執行後弄髒工作樹", result.stdout + result.stderr)
            finally:
                shutil.rmtree(wsd, ignore_errors=True)

    def test_green_validator_that_dirties_tree_is_blocked(self):
        self._assert_dirty_validator_blocked(0)

    def test_red_validator_with_old_green_that_dirties_tree_is_blocked(self):
        self._assert_dirty_validator_blocked(1)


# fake agent 腳本:被 loop 當一輪 agent spawn(cwd=repo,LOOP_WS 由 loop 設)。
_AGENT_TAMPER = f'''import os, subprocess, sys
sys.stdin.read()
open("goal.md", "a").write("\\nTAMPERED BY AGENT\\n")   # 竄改受保護檔
subprocess.run([sys.executable, {WORK_PY!r}, "create-plan"],
               input='[{{"order":1,"task":"stolen dirty task"}}]', text=True, env=dict(os.environ))
'''
_AGENT_CLEAN = f'''import os, subprocess, sys
sys.stdin.read()
subprocess.run([sys.executable, {WORK_PY!r}, "create-plan"],
               input='[{{"order":1,"task":"legit planned task"}}]', text=True, env=dict(os.environ))
'''


class TestTamperRoundVoided(unittest.TestCase):
    """#2 竄改輪整輪作廢:同輪偷改 goal + create-plan → plan 不存活;正常 create-plan 不誤殺。"""

    def _run_one_round(self, name, agent_body):
        wsd = WS_ROOT / name
        shutil.rmtree(wsd, ignore_errors=True)
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            head0 = git(repo, "rev-parse", "HEAD").stdout.strip()
            agent = Path(d) / "agent.py"
            agent.write_text(agent_body)
            subprocess.run(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", name,
                 "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {agent}",
                 "--validate-cmd", "true", "--max-rounds", "1"],
                capture_output=True, text=True)
            st = json.loads((wsd / "state.json").read_text())
            goal_after = (repo / "goal.md").read_text()
            head_after = git(repo, "rev-parse", "HEAD").stdout.strip()
            clean = not git(repo, "status", "--porcelain").stdout.strip()
        shutil.rmtree(wsd, ignore_errors=True)
        return st, goal_after, (head_after == head0), clean

    def test_tampered_plan_does_not_survive(self):
        st, goal, head_unchanged, clean = self._run_one_round("verifytmp_f2_tamper", _AGENT_TAMPER)
        self.assertEqual(st["plan"], [], "竄改輪偷渡的 plan 不得存活")
        self.assertEqual(st["plan_version"], 0, "竄改輪不得推進 plan_version")
        self.assertNotIn("TAMPERED", goal, "受保護的 goal.md 應被還原")
        self.assertTrue(head_unchanged and clean, "整輪 reset 後 HEAD 不前進、工作樹乾淨")

    def test_clean_create_plan_not_killed(self):
        st, _, _, _ = self._run_one_round("verifytmp_f2_clean", _AGENT_CLEAN)
        self.assertEqual(len(st["plan"]), 1, "正常 create-plan 應生效")
        self.assertEqual(st["plan_version"], 1, "正常 create-plan 應推進 plan_version")


if __name__ == "__main__":
    unittest.main(verbosity=2)
