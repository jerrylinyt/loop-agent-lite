#!/usr/bin/env python3
"""協調層防線的回歸測試(stdlib only,無外部依賴)。

對應複審發現的三個 correctness 缺口,全部用真 git + 真 loop.py/work.py 驗證,不做 mock:
- #1 綠點錨定 fail-closed:green 未驗可達性/一致性,reset 回去會弄髒工作樹或還原錯版 goal。
- #2 竄改輪整輪作廢:同一輪偷改 protected + create-plan,竄改的 plan 不得存活。
- #3 原子寫並發:ThreadingHTTPServer 下多執行緒共用 tmp 會 truncate / FileNotFoundError。

跑法:  python3 -m unittest tests.test_guards      # 或  python3 tests/test_guards.py
"""
import io
import json
import os
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import loop as L  # noqa: E402
import dashboard as D  # noqa: E402
import work as W  # noqa: E402

WORK_PY = str(REPO_ROOT / "work.py")
LOOP_PY = str(REPO_ROOT / "loop.py")
STATUS_PY = str(REPO_ROOT / "status.py")
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


class TestStateCheckpointRecovery(unittest.TestCase):
    """主 state 不可讀時由 last-good 復原；兩份都壞必須 fail-closed。"""

    def test_workspace_recovers_corrupt_primary_and_protects_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                ws = L.Workspace("recover")
                state = ws.fresh_state()
                state["round"] = 17
                ws.save_state(state)
                self.assertEqual(ws.state_path.read_bytes(), ws.checkpoint_path.read_bytes())
                ws.state_path.write_text("{broken")

                resumed = L.Workspace("recover")
                loaded = resumed.load_state()

                self.assertTrue(resumed.state_recovered)
                self.assertEqual(loaded["round"], 17)
                self.assertEqual(loaded["state_recovery_count"], 1)
                self.assertEqual(resumed.state_path.read_bytes(), resumed.checkpoint_path.read_bytes())
                json.loads(resumed.state_path.read_text())
                resumed.checkpoint_path.write_text("{}")
                self.assertTrue(resumed.state_tampered(), "agent 動 recovery copy 也必須使該輪作廢")
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_missing_primary_recovers_but_both_corrupt_raise(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                ws = L.Workspace("missing")
                state = ws.fresh_state()
                state["round"] = 9
                ws.save_state(state)
                ws.state_path.unlink()
                self.assertEqual(L.Workspace("missing").load_state()["round"], 9)

                broken = L.Workspace("broken")
                broken.state_path.write_text("[]")
                broken.checkpoint_path.write_text("not-json")
                with self.assertRaises(L.StateLoadError):
                    broken.load_state()
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_dashboard_readonly_falls_back_without_repair_then_writable_repairs(self):
        with tempfile.TemporaryDirectory() as d:
            old_values = (L.WORKSPACE_ROOT, D.ROOT)
            try:
                L.WORKSPACE_ROOT = Path(d)
                D.ROOT = Path(d)
                ws = L.Workspace("dashboard-recover")
                state = ws.fresh_state()
                state["round"] = 23
                ws.save_state(state)
                ws.state_path.write_text("broken")

                readonly, err = D.read_state("dashboard-recover", repair=False)
                self.assertIsNone(err)
                self.assertEqual(readonly["round"], 23)
                self.assertTrue(readonly["state_recovery_pending"])
                self.assertEqual(ws.state_path.read_text(), "broken", "唯讀 Dashboard 不得修檔")

                repaired, err = D.read_state("dashboard-recover", repair=True)
                self.assertIsNone(err)
                self.assertEqual(repaired["state_recovery_count"], 1)
                self.assertEqual(json.loads(ws.state_path.read_text())["round"], 23)
                again, err = D.read_state("dashboard-recover", repair=True)
                self.assertIsNone(err)
                self.assertEqual(again["state_recovery_count"], 1, "同一事故不得重複計數")
                self.assertIn("state.json 已從 last-good checkpoint 復原",
                              (ws.dir / "console.log").read_text())
            finally:
                L.WORKSPACE_ROOT, D.ROOT = old_values

    def test_loop_resume_recovers_checkpoint_and_continues(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            common = [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "resume-recover",
                      "--agent-cmd", "true", "--validate-cmd", "true"]
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            seeded = subprocess.run(common + ["--max-rounds", "1"], capture_output=True, text=True, env=env)
            self.assertEqual(seeded.returncode, 0, seeded.stdout + seeded.stderr)
            state_path = workspace_root / "resume-recover" / "state.json"
            state_path.write_text("{truncated")

            resumed = subprocess.run(common + ["--max-rounds", "2"], capture_output=True, text=True, env=env)

            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            state = json.loads(state_path.read_text())
            self.assertEqual(state["round"], 2)
            self.assertEqual(state["state_recovery_count"], 1)
            self.assertIn("state.json 已從 last-good checkpoint 復原", resumed.stdout)


class TestRoundIsolation(unittest.TestCase):
    """舊 round 的延遲 coordinator 命令/訊號不得污染目前 round。"""

    def test_stale_work_command_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ws_dir = Path(d)
            ws = L.Workspace.__new__(L.Workspace)
            ws.dir = ws_dir
            current = "b" * 32
            stale = "a" * 32
            ws.write_dispatch("exec", "task-1", current)
            env = {**os.environ, "LOOP_WS": str(ws_dir), "LOOP_ROUND_TOKEN": stale}

            result = subprocess.run([sys.executable, WORK_PY, "done", "task-1"],
                                    capture_output=True, text=True, env=env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("已結束的 round", result.stderr)
            self.assertFalse(ws.signal("signal_done", current))
            self.assertEqual(list(ws_dir.glob("signal_done.*")), [])

    def test_old_token_file_does_not_match_current_round(self):
        with tempfile.TemporaryDirectory() as d:
            ws = L.Workspace.__new__(L.Workspace)
            ws.dir = Path(d)
            (ws.dir / "signal_done.old-token").write_text("")
            self.assertFalse(ws.signal("signal_done", "current-token"))


class TestSingleWriterLock(unittest.TestCase):
    """同一 state/worktree 的第二個 loop 必須立即失敗，不能競寫。"""

    def test_lock_is_exclusive_and_released_explicitly(self):
        with tempfile.TemporaryDirectory() as d:
            lock_path = Path(d) / "run.lock"
            try:
                L.acquire_run_lock(lock_path, "test target")
                with self.assertRaises(SystemExit):
                    L.acquire_run_lock(lock_path, "test target")
            finally:
                L.release_run_locks()

            try:
                L.acquire_run_lock(lock_path, "test target")
            finally:
                L.release_run_locks()


class TestAgentProcessIsolation(unittest.TestCase):
    """CLI 主程序退出即封口；同 process-group 背景子行程不能拖住/污染下一輪。"""

    def test_normal_exit_kills_background_child_holding_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            prompt = root / "prompt.md"
            prompt.write_text("test\n")
            child = "import time; time.sleep(10)"
            parent = ("import subprocess, sys; "
                      f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                      "print('parent done', flush=True)")
            started = time.monotonic()

            rc, _secs, timed_out = L.run_agent(
                [sys.executable, "-c", parent], prompt, root, os.environ.copy(),
                root / "agent.log", 0,
            )

            self.assertEqual(rc, 0)
            self.assertFalse(timed_out)
            self.assertLess(time.monotonic() - started, 2,
                            "背景 child 繼承 stdout 也不得把 round 卡到 child 自行結束")


class TestAbnormalRoundVoided(unittest.TestCase):
    """Agent crash 前打出的共識訊號不得被採信。"""

    def test_plan_ok_then_nonzero_exit_does_not_advance_consensus(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            plan = root / "plan.json"
            plan.write_text('[{"order": 1, "task": "only task"}]')
            agent = root / "agent.py"
            agent.write_text(
                "import os, subprocess, sys\n"
                "sys.stdin.read()\n"
                f"subprocess.run([sys.executable, {WORK_PY!r}, 'plan-ok'], env=dict(os.environ))\n"
                "raise SystemExit(7)\n"
            )
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}

            result = subprocess.run(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "abnormal-vote",
                 "--agent-cmd", shlex.join([sys.executable, str(agent)]),
                 "--validate-cmd", "true", "--import-plan", str(plan),
                 "--start-phase", "plan", "--max-rounds", "1"],
                capture_output=True, text=True, env=env,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads((workspace_root / "abnormal-vote" / "state.json").read_text())
            self.assertEqual(state["flag"], 0)
            self.assertIn("coordinator 訊號已全部作廢", result.stdout)


class TestAgentFailureBackoff(unittest.TestCase):
    """Agent CLI 秒退時要節流；成功後 failure streak 立即復原。"""

    def test_exponential_backoff_is_capped_and_disableable(self):
        self.assertEqual([L.agent_failure_backoff(n, 10) for n in range(1, 6)], [1, 2, 4, 8, 10])
        self.assertEqual(L.agent_failure_backoff(99, 0), 0)
        self.assertEqual(L.agent_failure_backoff(0, 60), 0)

    def test_failure_then_success_resets_streak(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            agent = root / "flaky_agent.py"
            agent.write_text(
                "from pathlib import Path\n"
                "marker = Path(__file__).with_suffix('.once')\n"
                "if not marker.exists():\n"
                "    marker.write_text('failed')\n"
                "    raise SystemExit(7)\n"
            )
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            started = time.monotonic()

            result = subprocess.run(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "flaky-backoff",
                 "--agent-cmd", shlex.join([sys.executable, str(agent)]),
                 "--validate-cmd", "true", "--agent-backoff-max", "0.05", "--max-rounds", "2"],
                capture_output=True, text=True, env=env,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertGreaterEqual(time.monotonic() - started, 0.04)
            state = json.loads((workspace_root / "flaky-backoff" / "state.json").read_text())
            self.assertEqual(state["agent_failure_streak"], 0)
            self.assertEqual(state["agent_backoff_seconds"], 0)
            self.assertIsNone(state["agent_backoff_until"])
            self.assertIn("0.05 秒後重試", result.stdout)
            self.assertIn("Agent CLI 已恢復", result.stdout)

    def test_backoff_is_visible_in_state_and_interruptible(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            state_path = workspace_root / "visible-backoff" / "state.json"
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            process = subprocess.Popen(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "visible-backoff",
                 "--agent-cmd", "false", "--validate-cmd", "true",
                 "--agent-backoff-max", "2", "--max-rounds", "2"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
            )
            try:
                deadline = time.monotonic() + 3
                observed = None
                while time.monotonic() < deadline:
                    try:
                        candidate = json.loads(state_path.read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        time.sleep(0.02)
                        continue
                    if candidate.get("agent_backoff_seconds") == 1:
                        observed = candidate
                        break
                    time.sleep(0.02)

                self.assertIsNotNone(observed, "退避開始前必須先把等待狀態落進 state.json")
                self.assertEqual(observed["agent_failure_streak"], 1)
                self.assertIsNotNone(observed["agent_backoff_until"])
                process.send_signal(signal.SIGINT)
                output, _ = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 130, output)
                stopped = json.loads(state_path.read_text())
                self.assertEqual(stopped["agent_backoff_seconds"], 0)
                self.assertIsNone(stopped["agent_backoff_until"])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


class TestStateSchemaGuard(unittest.TestCase):
    """合法 JSON 但核心欄位錯型時，應 fail-closed 或由合法 checkpoint 復原。"""

    def test_invalid_primary_shape_recovers_valid_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                ws = L.Workspace("schema-recover")
                state = ws.fresh_state()
                state["round"] = 12
                ws.save_state(state)
                ws.state_path.write_text(json.dumps({"phase": "not-a-phase", "round": "12"}), encoding="utf-8")
                resumed = L.Workspace("schema-recover")
                loaded = resumed.load_state()
                self.assertTrue(resumed.state_recovered)
                self.assertEqual(loaded["round"], 12)
                self.assertEqual(json.loads(resumed.state_path.read_text())["round"], 12)
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_invalid_primary_and_checkpoint_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                ws = L.Workspace("schema-broken")
                ws.state_path.write_text(json.dumps({"phase": "exec", "plan": {}}), encoding="utf-8")
                ws.checkpoint_path.write_text(json.dumps({"phase": "done", "issues": "bad"}), encoding="utf-8")
                with self.assertRaises(L.StateLoadError):
                    ws.load_state()
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_invalid_loop_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                ws = L.Workspace("schema-loop")
                invalid = {"phase": "plan", "loop": {"pid": "123", "session_id": 99}}
                ws.state_path.write_text(json.dumps(invalid), encoding="utf-8")
                ws.checkpoint_path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(L.StateLoadError):
                    ws.load_state()
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_invalid_temporal_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                ws = L.Workspace("schema-time")
                invalid = {"phase": "plan", "agent_backoff_until": 123,
                           "last_state_recovery": {"bad": True}}
                ws.state_path.write_text(json.dumps(invalid), encoding="utf-8")
                ws.checkpoint_path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(L.StateLoadError):
                    ws.load_state()
            finally:
                L.WORKSPACE_ROOT = old_root


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


class TestWorkspaceArtifactGuards(unittest.TestCase):
    """workspace 內部的 python-owned artifact 不得藉 symlink 逸出或讀取外部內容。"""

    def test_workspace_constructor_rejects_symlinked_internal_directories(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workspace_root = root / "workspace"
            outside = root / "outside"
            outside.mkdir()
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = workspace_root
                L.Workspace("safe")
                for child in ("logs", "prompts", "snapshots"):
                    path = workspace_root / "safe" / child
                    shutil.rmtree(path)
                    path.symlink_to(outside, target_is_directory=True)
                    with self.subTest(child=child), self.assertRaises(ValueError):
                        L.Workspace("safe")
                    self.assertFalse((outside / "round-0001.md").exists())
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_state_console_report_and_stream_reads_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workspace_root = root / "workspace"
            workspace = workspace_root / "safe"
            workspace.mkdir(parents=True)
            outside = root / "outside"
            outside.write_text("outside secret\n", encoding="utf-8")
            old_values = L.WORKSPACE_ROOT, D.ROOT
            try:
                L.WORKSPACE_ROOT = workspace_root
                D.ROOT = workspace_root
                ws = L.Workspace("safe")
                ws.save_state(ws.fresh_state())

                ws.state_path.unlink()
                ws.state_path.symlink_to(outside)
                with self.assertRaises(ValueError):
                    L.Workspace("safe")
                state, error = D.read_state("safe", repair=False)
                self.assertIsNone(state)
                self.assertIn("不安全", error)
                self.assertNotIn("outside secret", error)

                (workspace / "console.log").symlink_to(outside)
                D.workspace_console_log("safe", "must not escape")
                self.assertEqual(outside.read_text(encoding="utf-8"), "outside secret\n")

                (workspace / "REPORT.md").symlink_to(outside)
                report = D.read_report("safe")
                self.assertIn("error", report)
                self.assertNotIn("outside secret", json.dumps(report, ensure_ascii=False))

                prompts = workspace / "prompts"
                (prompts / "round-0001.md").symlink_to(outside)
                prompt = D.read_prompt("safe")
                self.assertIn("error", prompt)
                self.assertNotIn("outside secret", json.dumps(prompt, ensure_ascii=False))

                logs = workspace / "logs"
                (logs / "round-0001.log").symlink_to(outside)
                tail = D.read_incremental(logs / "round-0001.log", -1)
                self.assertIn("error", tail)
                self.assertNotIn("outside secret", json.dumps(tail, ensure_ascii=False))

                (workspace / "history.log").symlink_to(outside)
                history = D.read_incremental(workspace / "history.log", -1)
                self.assertIn("error", history)
                self.assertNotIn("outside secret", json.dumps(history, ensure_ascii=False))
            finally:
                L.WORKSPACE_ROOT, D.ROOT = old_values


class TestWorkCliArtifactGuards(unittest.TestCase):
    """work.py 的 agent 寫入口也必須拒絕 workspace 內的 symlink artifact。"""

    def run_work(self, workspace, token, command, *args):
        env = {**os.environ, "LOOP_WS": str(workspace), "LOOP_ROUND_TOKEN": token}
        return subprocess.run([sys.executable, WORK_PY, command, *args],
                              capture_output=True, text=True, env=env)

    def test_dispatch_symlink_is_rejected_without_reading_outside(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workspace = root / "workspace"
            workspace.mkdir()
            outside = root / "outside"
            outside.write_text(json.dumps({"phase": "plan", "round_token": "tok"}), encoding="utf-8")
            (workspace / "dispatch.json").symlink_to(outside)
            result = self.run_work(workspace, "tok", "plan-ok")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("派工資訊不存在或損壞", result.stderr)
            self.assertFalse((workspace / "signal_plan_ok.tok").exists())

    def test_signal_and_issue_symlinks_are_rejected_without_writing_outside(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workspace = root / "workspace"
            workspace.mkdir()
            outside = root / "outside"
            outside.write_text("outside secret\n", encoding="utf-8")
            (workspace / "dispatch.json").write_text(
                json.dumps({"phase": "plan", "round_token": "tok"}), encoding="utf-8")
            (workspace / "signal_plan_ok.tok").symlink_to(outside)
            result = self.run_work(workspace, "tok", "plan-ok")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("協調檔案不安全", result.stderr)
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside secret\n")


            (workspace / "signal_plan_ok.tok").unlink()
            (workspace / "pending_issues.tok").symlink_to(outside)
            result = self.run_work(workspace, "tok", "issue", "must", "not", "escape")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("協調檔案不安全", result.stderr)
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside secret\n")

    def test_single_writer_lock_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            outside = root / "outside.lock"
            outside.write_text("outside secret\n", encoding="utf-8")
            link = root / "loop.lock"
            link.symlink_to(outside)
            old_console = L._CONSOLE_PATH
            try:
                L._CONSOLE_PATH = None
                with self.assertRaises(SystemExit):
                    L.acquire_run_lock(link, "test lock")
            finally:
                L._CONSOLE_PATH = old_console
                L.release_run_locks()
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside secret\n")


class TestStatusCli(unittest.TestCase):
    """status.py 是純唯讀 projection，JSON 可供 shell/CI 消費。"""

    def test_json_status_and_checkpoint_projection_do_not_repair(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = root / "workspace"
                ws = L.Workspace("cli-status")
                state = ws.fresh_state()
                state.update(round=4, plan=[{"order": 1, "task": "one"}], current_order=1)
                ws.save_state(state)
                ws.state_path.write_text("{broken", encoding="utf-8")
                before = ws.state_path.read_bytes()
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                result = subprocess.run(
                    [sys.executable, STATUS_PY, "--name", "cli-status", "--json"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["schema_version"], 1)
                self.assertEqual(payload["round"], 4)
                self.assertEqual(payload["plan_len"], 1)
                self.assertEqual(payload["current_task"], "one")
                self.assertTrue(payload["state_recovery_pending"])
                self.assertEqual(ws.state_path.read_bytes(), before, "status CLI 不得修復 primary state")
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_missing_workspace_returns_machine_readable_error(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(Path(d) / "workspace")}
            result = subprocess.run(
                [sys.executable, STATUS_PY, "--name", "missing", "--json"],
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertIn("不存在", payload["error"])

    def test_watch_emits_repeated_json_and_stops_with_ctrl_c(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = root / "workspace"
                ws = L.Workspace("watch-status")
                ws.save_state(ws.fresh_state())
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                process = subprocess.Popen(
                    [sys.executable, STATUS_PY, "--name", "watch-status", "--json",
                     "--watch", "--interval", "0.01"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                try:
                    time.sleep(0.08)
                    process.send_signal(signal.SIGINT)
                    output, error = process.communicate(timeout=2)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                self.assertEqual(process.returncode, 130, error)
                lines = [line for line in output.splitlines() if line.strip()]
                self.assertGreaterEqual(len(lines), 2)
                self.assertTrue(all(json.loads(line)["name"] == "watch-status" for line in lines))
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_watch_on_change_suppresses_duplicate_json_until_state_changes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = root / "workspace"
                ws = L.Workspace("watch-change")
                state = ws.fresh_state()
                ws.save_state(state)
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                process = subprocess.Popen(
                    [sys.executable, STATUS_PY, "--name", "watch-change", "--json",
                     "--watch", "--on-change", "--interval", "0.01"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                try:
                    time.sleep(0.08)
                    state["round"] = 1
                    ws.save_state(state)
                    time.sleep(0.08)
                    process.send_signal(signal.SIGINT)
                    output, error = process.communicate(timeout=2)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                self.assertEqual(process.returncode, 130, error)
                lines = [line for line in output.splitlines() if line.strip()]
                payloads = [json.loads(line) for line in lines]
                self.assertEqual(len(payloads), 2)
                self.assertEqual([payload["round"] for payload in payloads], [0, 1])
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_on_change_requires_watch(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(Path(d) / "workspace")}
            result = subprocess.run(
                [sys.executable, STATUS_PY, "--name", "missing", "--on-change"],
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("必須搭配 --watch", result.stderr)

    def test_check_returns_nonzero_for_attention_but_keeps_json_projection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = root / "workspace"
                ws = L.Workspace("check-status")
                state = ws.fresh_state()
                state.update(agent_failure_streak=1, state_recovery_count=2, goal_changed=True,
                             loop={"pid": 99999999, "session_id": "stale", "started_at": "2026-07-10T20:00:00"})
                ws.save_state(state)
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                result = subprocess.run(
                    [sys.executable, STATUS_PY, "--all", "--json", "--check"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["summary"]["attention"], 1)
                self.assertEqual(payload["summary"]["error_count"], 0)
                self.assertEqual(payload["summary"]["agent_failures"], 1)
                self.assertEqual(payload["summary"]["state_recoveries"], 2)
                self.assertEqual(payload["summary"]["goal_changes"], 1)
                projection = payload["workspaces"][0]
                self.assertEqual(projection["agent_failure_streak"], 1)
                self.assertEqual(projection["state_recovery_count"], 2)
                self.assertTrue(projection["goal_changed"])
                self.assertTrue(projection["stale_loop_pid"])
                self.assertEqual(projection["loop_started_at"], "2026-07-10T20:00:00")
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_check_rejects_watch_mode(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(Path(d) / "workspace")}
            result = subprocess.run(
                [sys.executable, STATUS_PY, "--name", "missing", "--check", "--watch"],
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("不可搭配 --watch", result.stderr)

    def test_all_sort_attention_places_alerts_first(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = root / "workspace"
                for name, stalled in (("alpha", 0), ("beta", 1)):
                    ws = L.Workspace(name)
                    state = ws.fresh_state()
                    state["stall_rounds"] = stalled
                    ws.save_state(state)
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                result = subprocess.run(
                    [sys.executable, STATUS_PY, "--all", "--json", "--sort", "attention"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual([item["name"] for item in payload["workspaces"]], ["beta", "alpha"])
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_human_status_shows_attention_context(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = root / "workspace"
                ws = L.Workspace("human-status")
                state = ws.fresh_state()
                state.update(agent_failure_streak=2, agent_backoff_seconds=4,
                             state_recovery_count=1, goal_changed=True)
                ws.save_state(state)
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                result = subprocess.run(
                    [sys.executable, STATUS_PY, "--name", "human-status"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Agent 異常 2", result.stdout)
                self.assertIn("state 復原 1", result.stdout)
                self.assertIn("goal 已變更", result.stdout)
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_sort_requires_all_mode(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(Path(d) / "workspace")}
            result = subprocess.run(
                [sys.executable, STATUS_PY, "--name", "missing", "--sort", "round"],
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("只有搭配 --all", result.stderr)

    def test_all_json_lists_fleet_without_starting_or_repairing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = root / "workspace"
                for name, round_number in (("alpha", 2), ("beta", 5)):
                    ws = L.Workspace(name)
                    state = ws.fresh_state()
                    state["round"] = round_number
                    ws.save_state(state)
                (L.WORKSPACE_ROOT / "reserved-empty").mkdir(parents=True)
                broken = L.Workspace("broken")
                broken.state_path.write_text("{broken", encoding="utf-8")
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                result = subprocess.run(
                    [sys.executable, STATUS_PY, "--all", "--json"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["schema_version"], 1)
                self.assertEqual([item["name"] for item in payload["workspaces"]], ["alpha", "beta", "broken"])
                self.assertEqual([item["round"] for item in payload["workspaces"] if "round" in item], [2, 5])
                self.assertIn("error", payload["workspaces"][-1])
                self.assertEqual(payload["summary"], {
                    "workspace_count": 3,
                    "valid_count": 2,
                    "error_count": 1,
                    "running": 0,
                    "planning": 2,
                    "executing": 0,
                    "done": 0,
                    "attention": 0,
                    "issues": 0,
                    "agent_failures": 0,
                    "state_recoveries": 0,
                    "goal_changes": 0,
                    "stale_loops": 0,
                    "tasks_completed": 0,
                    "tasks_total": 0,
                    "task_completion_pct": 0,
                })
            finally:
                L.WORKSPACE_ROOT = old_root


class TestLoopSignalIngestionGuards(unittest.TestCase):
    """loop 讀 agent signal/proposal 時不跟隨 workspace 內 symlink。"""

    def test_symlinked_signal_and_pending_plan_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = root / "workspace"
                ws = L.Workspace("signals")
                outside = root / "outside.json"
                outside.write_text(json.dumps([{"order": 1, "task": "outside"}]), encoding="utf-8")
                token = "a" * 32
                (ws.dir / f"pending_plan.{token}.json").symlink_to(outside)
                (ws.dir / f"signal_plan_ok.{token}").symlink_to(outside)
                self.assertIsNone(ws.take_pending_plan(token))
                self.assertFalse(ws.signal("signal_plan_ok", token))
                self.assertEqual(outside.read_text(encoding="utf-8"),
                                 json.dumps([{"order": 1, "task": "outside"}]))
            finally:
                L.WORKSPACE_ROOT = old_root


class TestPreflightConsole(unittest.TestCase):
    """preflight 立刻失敗時，原因仍必須落進 dashboard 正在看的 console.log。"""

    def test_dirty_repo_failure_is_visible_in_workspace_console(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            (repo / "wip.txt").write_text("dirty\n")
            workspace_root = Path(d) / "workspace"
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            result = subprocess.run(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "preflight-console",
                 "--agent-cmd", "true", "--validate-cmd", "true", "--max-rounds", "1"],
                capture_output=True, text=True, env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            console = (workspace_root / "preflight-console" / "console.log").read_text()
            self.assertIn("流程停止｜preflight：工作樹不乾淨", console)


class TestTransactionalReset(unittest.TestCase):
    """reset 必須等 preflight 綠才取代 state，失敗不得留下無 state 的 workspace。"""

    def test_failed_reset_preserves_previous_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            workspace = workspace_root / "reset-safe"
            workspace.mkdir(parents=True)
            (workspace / "snapshots").mkdir()
            snapshot_path = workspace / "snapshots" / "goal.md"
            snapshot_path.write_text("old snapshot\n")
            snapshot_before = snapshot_path.read_bytes()
            previous = L.Workspace.__new__(L.Workspace).fresh_state()
            previous["round"] = 77
            previous["plan"] = [{"order": 1, "task": "must survive failed reset"}]
            previous["plan_version"] = 9
            state_path = workspace / "state.json"
            state_path.write_text(json.dumps(previous))
            before = state_path.read_bytes()
            checkpoint_path = workspace / "state.last-good.json"
            checkpoint_path.write_bytes(before)
            checkpoint_before = checkpoint_path.read_bytes()
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}

            result = subprocess.run(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "reset-safe",
                 "--agent-cmd", "true", "--validate-cmd", "false", "--reset-state", "--max-rounds", "1"],
                capture_output=True, text=True, env=env,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(state_path.read_bytes(), before, "validate 失敗時舊 state 必須原封不動")
            self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before,
                             "validate 失敗時 recovery checkpoint 也必須原封不動")
            self.assertEqual(snapshot_path.read_bytes(), snapshot_before,
                             "Validate 失敗時舊 protected snapshot 也必須保留")
            self.assertIn("啟動前檢查通過後才會正式清除", result.stdout)

    def test_successful_reset_replaces_previous_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            workspace = workspace_root / "reset-success"
            workspace.mkdir(parents=True)
            prompts = workspace / "prompts"
            prompts.mkdir()
            for round_number in range(34, 39):
                (prompts / f"round-{round_number:04d}.md").write_text("old prompt\n")
            (workspace / "pending_issues").write_text("old crashed issue\n")
            previous = L.Workspace.__new__(L.Workspace).fresh_state()
            previous["round"] = 77
            previous["plan"] = [{"order": 1, "task": "must be cleared"}]
            previous["plan_version"] = 9
            (workspace / "state.json").write_text(json.dumps(previous))
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}

            result = subprocess.run(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "reset-success",
                 "--agent-cmd", "true", "--validate-cmd", "true", "--reset-state", "--max-rounds", "1"],
                capture_output=True, text=True, env=env,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            current = json.loads((workspace / "state.json").read_text())
            self.assertEqual((workspace / "state.json").read_bytes(),
                             (workspace / "state.last-good.json").read_bytes())
            self.assertEqual(current["plan"], [])
            self.assertEqual(current["plan_version"], 0)
            self.assertEqual(current["round"], 1)
            self.assertTrue((workspace / "prompts" / "round-0001.md").is_file(),
                            "reset 後的當前 prompt 不得被舊高輪號 prompt 清理掉")
            self.assertEqual(current["issues"], [], "reset 不得把舊 session pending issue 算進新 round")
            self.assertFalse((workspace / "pending_issues").exists())
            marker = json.loads((workspace / "startup_ready.json").read_text())
            self.assertIsInstance(marker.get("pid"), int)


class TestValidateTimeout(unittest.TestCase):
    """正式 preflight validator 必須有 timeout，不能讓 starting 永久卡住。"""

    def test_preflight_validator_timeout_stops_process_group(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            slow = shlex.join([sys.executable, "-c", "import time; time.sleep(10)"])
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "validate-timeout",
                 "--agent-cmd", "true", "--validate-cmd", slow,
                 "--validate-timeout", "0.1", "--max-rounds", "1"],
                capture_output=True, text=True, env=env, timeout=3,
            )
            elapsed = time.monotonic() - started
            self.assertNotEqual(result.returncode, 0)
            self.assertLess(elapsed, 2)
            self.assertIn("逾時 0.1 秒", result.stdout)


class TestStartupHandshake(unittest.TestCase):
    """啟動狀態以 ready marker/process exit 為準，不再用固定 0.6 秒猜測。"""

    def test_slow_failure_transitions_from_starting_to_failed(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = D.ROOT
            name = "slow-startup-failure"
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(0.8); raise SystemExit(7)"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                D.ROOT = Path(d)
                (D.ROOT / name).mkdir()
                D.JOBS[name] = D.Job(name, d, process)
                self.assertEqual(D.job_startup_status(name, process.pid)["status"], "starting")
                process.wait(timeout=2)
                status = D.job_startup_status(name, process.pid)
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["rc"], 7)
            finally:
                if process.poll() is None:
                    process.kill()
                D.JOBS.pop(name, None)
                D.ROOT = old_root

    def test_matching_ready_marker_reports_ready(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = D.ROOT
            name = "startup-ready"
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                D.ROOT = Path(d)
                workspace = D.ROOT / name
                workspace.mkdir()
                (workspace / "startup_ready.json").write_text(json.dumps({"pid": process.pid}))
                D.JOBS[name] = D.Job(name, d, process)
                self.assertEqual(D.job_startup_status(name, process.pid)["status"], "ready")
            finally:
                process.kill()
                process.wait()
                D.JOBS.pop(name, None)
                D.ROOT = old_root


class TestTransactionalDashboardPlanImport(unittest.TestCase):
    """Dashboard plan import 也必須等 loop preflight 綠才取代舊 state。"""

    def test_failed_import_launch_preserves_old_state(self):
        class ResponseCapture:
            response = None

            def _out(self, code, body, _ctype="application/json; charset=utf-8"):
                self.response = code, json.loads(body)

            def _err(self, msg, code=400):
                self._out(code, json.dumps({"error": msg}, ensure_ascii=False))

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            workspace = workspace_root / "import-safe"
            workspace.mkdir(parents=True)
            previous = L.Workspace.__new__(L.Workspace).fresh_state()
            previous["round"] = 77
            previous["plan"] = [{"order": 1, "task": "old plan survives"}]
            previous["plan_version"] = 4
            state_path = workspace / "state.json"
            state_path.write_text(json.dumps(previous))
            before = state_path.read_bytes()
            checkpoint_path = workspace / "state.last-good.json"
            checkpoint_path.write_bytes(before)
            checkpoint_before = checkpoint_path.read_bytes()
            (workspace / "snapshots").mkdir()
            snapshot_path = workspace / "snapshots" / "goal.md"
            snapshot_path.write_text("old snapshot\n")
            snapshot_before = snapshot_path.read_bytes()
            config = {
                "agent_cmds": [{"label": "true", "cmd": "true"}],
                "validate_cmds": [{"label": "false", "cmd": "false"}],
                "extra_path_dirs": [],
                "notify_cmd": "",
                "defaults": {"validate_timeout": 1},
            }
            old_values = (D.ROOT, L.WORKSPACE_ROOT, D.load_config, os.environ.get("LOOP_AGENT_WORKSPACE_ROOT"))
            try:
                D.ROOT = workspace_root
                L.WORKSPACE_ROOT = workspace_root
                D.load_config = lambda: config
                os.environ["LOOP_AGENT_WORKSPACE_ROOT"] = str(workspace_root)
                handler = ResponseCapture()
                D.Handler.api_launch(handler, {
                    "repo": str(repo), "name": "import-safe", "agent_idx": 0, "validate_idx": 0,
                    "plan_json": '[{"order":1,"task":"new imported plan"}]', "start_phase": "plan",
                })
                self.assertEqual(handler.response[0], 200)
                job = D.JOBS["import-safe"]
                job.popen.wait(timeout=3)
                job.reader.join(timeout=1)
                self.assertEqual(D.job_startup_status("import-safe", job.popen.pid)["status"], "failed")
                self.assertEqual(state_path.read_bytes(), before, "失敗 import 不得覆寫舊 state")
                self.assertEqual(checkpoint_path.read_bytes(), checkpoint_before,
                                 "失敗 import 不得覆寫 recovery checkpoint")
                self.assertEqual(snapshot_path.read_bytes(), snapshot_before,
                                 "失敗 import 不得覆寫舊 protected snapshot")
            finally:
                job = D.JOBS.pop("import-safe", None)
                if job and job.alive():
                    job.stop(wait=True)
                D.ROOT, L.WORKSPACE_ROOT, D.load_config = old_values[:3]
                if old_values[3] is None:
                    os.environ.pop("LOOP_AGENT_WORKSPACE_ROOT", None)
                else:
                    os.environ["LOOP_AGENT_WORKSPACE_ROOT"] = old_values[3]


class TestWorkspaceFleetValidity(unittest.TestCase):
    """只有 log、沒有 state.json 的失敗啟動目錄不應顯示成可操作 workspace。"""

    def test_state_less_directory_is_not_listed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "ghost").mkdir()
            (root / "ghost" / "console.log").write_text("failed\n")
            valid = root / "valid"
            valid.mkdir()
            valid_state = L.Workspace.__new__(L.Workspace).fresh_state()
            valid_state.update(agent_failure_streak=2, agent_backoff_seconds=4,
                               state_recovery_count=3, state_recovery_pending=True,
                               goal_changed=True, loop={"pid": 99999999, "session_id": "stale", "started_at": "2026-07-10T20:00:00"})
            (valid / "state.json").write_text(json.dumps(valid_state))
            checkpoint_only = root / "checkpoint-only"
            checkpoint_only.mkdir()
            checkpoint_state = L.Workspace.__new__(L.Workspace).fresh_state()
            checkpoint_state["round"] = 8
            (checkpoint_only / "state.last-good.json").write_text(json.dumps(checkpoint_state))
            broken = root / "broken"
            broken.mkdir()
            (broken / "state.json").write_text("{broken", encoding="utf-8")
            old_root = D.ROOT
            try:
                D.ROOT = root
                fleet = D.list_workspaces()
                self.assertEqual([item["name"] for item in fleet], ["broken", "checkpoint-only", "valid"])
                self.assertIn("error", fleet[0])
                self.assertEqual(fleet[1]["round"], 8)
                valid_item = fleet[2]
                self.assertEqual(valid_item["agent_failure_streak"], 2)
                self.assertEqual(valid_item["agent_backoff_seconds"], 4)
                self.assertEqual(valid_item["state_recovery_count"], 3)
                self.assertTrue(valid_item["state_recovery_pending"])
                self.assertTrue(valid_item["goal_changed"])
                self.assertTrue(valid_item["stale_loop_pid"])
                self.assertEqual(valid_item["loop_started_at"], "2026-07-10T20:00:00")
                self.assertFalse((checkpoint_only / "state.json").exists(), "fleet 掃描必須保持唯讀")
            finally:
                D.ROOT = old_root


class TestHistoryRetention(unittest.TestCase):
    """當前 run 的 history 不得無限成長，且裁切保留最新紀錄。"""

    def test_append_history_keeps_latest_tail_without_touching_previous_run(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.log"
            previous = Path(d) / "history.log.1"
            previous.write_text("previous-run\n", encoding="utf-8")
            for index in range(6):
                L.append_history(path, f"round={index} event=latest-{index}\n", max_bytes=64)
            current = path.read_text(encoding="utf-8")
            self.assertLessEqual(path.stat().st_size, 64)
            self.assertIn("round=5", current)
            self.assertNotIn("round=0", current)
            self.assertEqual(previous.read_text(encoding="utf-8"), "previous-run\n")


class TestIssueRetention(unittest.TestCase):
    """Agent issue 輸入與 coordinator state 都有界，避免異常輸出撐大 state。"""

    def test_work_issue_rejects_oversized_text_and_pending_flood(self):
        with tempfile.TemporaryDirectory() as d:
            old_env = {key: os.environ.get(key) for key in ("LOOP_WS", "LOOP_ROUND_TOKEN")}
            try:
                ws = L.Workspace("issue-limits")
                token = "issue-token"
                ws.write_dispatch("exec", "task-1", token)
                os.environ["LOOP_WS"] = str(ws.dir)
                os.environ["LOOP_ROUND_TOKEN"] = token
                with self.assertRaises(SystemExit):
                    W.cmd_issue(ws.dir, ["x" * (L.ISSUE_MAX_CHARS + 1)])
                pending = ws.pending_issues(token)
                for _ in range(L.ISSUES_MAX_PENDING):
                    L.append_regular_text(pending, "existing\n")
                with self.assertRaises(SystemExit):
                    W.cmd_issue(ws.dir, ["one more"])
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


class TestStopIdempotency(unittest.TestCase):
    """fleet 狀態稍舊時重複 stop 不應報「沒有在執行中」。"""

    def test_already_stopped_returns_success(self):
        class ResponseCapture:
            response = None

            def _out(self, code, body, _ctype="application/json; charset=utf-8"):
                self.response = code, json.loads(body)

        handler = ResponseCapture()
        D.Handler.api_stop(handler, {"name": "verifytmp-not-running"})
        self.assertEqual(handler.response[0], 200)
        self.assertTrue(handler.response[1]["already_stopped"])


class TestGracefulRoundStop(unittest.TestCase):
    """平順停止必須封完目前 round，且控制檔不可跨 session 誤觸發。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_request_finishes_current_round_before_stopping(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            marker = root / "agent-started"
            agent = root / "slow_agent.py"
            agent.write_text(
                "import sys, time\n"
                "from pathlib import Path\n"
                "sys.stdin.read()\n"
                f"Path({str(marker)!r}).write_text('started')\n"
                "time.sleep(0.45)\n"
            )
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            process = subprocess.Popen(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "graceful-stop",
                 "--agent-cmd", shlex.join([sys.executable, str(agent)]),
                 "--validate-cmd", "true", "--max-rounds", "5"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
            )
            try:
                state_path = workspace_root / "graceful-stop" / "state.json"
                deadline = time.monotonic() + 3
                state = None
                while time.monotonic() < deadline:
                    try:
                        candidate = json.loads(state_path.read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        time.sleep(0.02)
                        continue
                    if marker.exists() and (candidate.get("loop") or {}).get("session_id"):
                        state = candidate
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(state, "Agent 啟動後應公開本次 loop session")
                loop_state = state["loop"]
                request = {"pid": loop_state["pid"], "session_id": loop_state["session_id"]}
                L.atomic_write_bytes(
                    workspace_root / "graceful-stop" / L.STOP_AFTER_ROUND_FILE,
                    json.dumps(request).encode(),
                )
                requested_at = time.monotonic()
                self.assertIsNone(process.poll(), "本輪後停止不得立刻殺掉仍在執行的 Agent")
                output, _ = process.communicate(timeout=3)

                self.assertEqual(process.returncode, 0, output)
                self.assertGreaterEqual(time.monotonic() - requested_at, 0.25)
                stopped = json.loads(state_path.read_text())
                self.assertEqual(stopped["round"], 1)
                self.assertIsNone(stopped["loop"]["pid"])
                self.assertFalse((workspace_root / "graceful-stop" / L.STOP_AFTER_ROUND_CLAIMED_FILE).exists())
                history = (workspace_root / "graceful-stop" / "history.log").read_text().splitlines()
                self.assertEqual(len(history), 1, "完整落盤目前 round 後不得再 spawn 下一輪")
                self.assertIn("已依要求停止", output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_stale_request_does_not_stop_new_session(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            workspace = workspace_root / "stale-stop"
            workspace.mkdir(parents=True)
            (workspace / L.STOP_AFTER_ROUND_FILE).write_text(
                json.dumps({"pid": os.getpid(), "session_id": "old-session"})
            )
            (workspace / L.STOP_AFTER_ROUND_CLAIMED_FILE).write_text(
                json.dumps({"pid": os.getpid(), "session_id": "old-session"})
            )
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            result = subprocess.run(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "stale-stop",
                 "--agent-cmd", "true", "--validate-cmd", "true", "--max-rounds", "1"],
                capture_output=True, text=True, env=env,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads((workspace / "state.json").read_text())
            self.assertEqual(state["round"], 1)
            self.assertFalse((workspace / L.STOP_AFTER_ROUND_FILE).exists())
            self.assertFalse((workspace / L.STOP_AFTER_ROUND_CLAIMED_FILE).exists())

    def test_claim_marker_is_session_bound(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d)
            L.atomic_write_bytes(
                workspace / L.STOP_AFTER_ROUND_FILE,
                json.dumps({"pid": 123, "session_id": "this-session"}).encode(),
            )
            self.assertTrue(L.claim_stop_after_round(workspace, 123, "this-session"))
            self.assertFalse((workspace / L.STOP_AFTER_ROUND_FILE).exists())
            self.assertTrue(L.stop_after_round_claimed(workspace, 123, "this-session"))
            self.assertFalse(L.stop_after_round_claimed(workspace, 123, "other-session"))
            L.clear_stop_after_round_claimed(workspace, 123, "this-session")
            self.assertFalse((workspace / L.STOP_AFTER_ROUND_CLAIMED_FILE).exists())

    def test_cancel_request_allows_the_next_round_to_start(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            agent = root / "counting_agent.py"
            agent.write_text(
                "import os, time\n"
                "from pathlib import Path\n"
                "ws = Path(os.environ['LOOP_WS'])\n"
                "marker = ws / 'agent-count'\n"
                "count = int(marker.read_text()) if marker.exists() else 0\n"
                "marker.write_text(str(count + 1))\n"
                "time.sleep(0.6)\n"
            )
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            process = subprocess.Popen(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "cancel-drain",
                 "--agent-cmd", shlex.join([sys.executable, str(agent)]),
                 "--validate-cmd", "true", "--max-rounds", "2"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
            )
            old_root = D.ROOT
            D.ROOT = workspace_root
            try:
                state_path = workspace_root / "cancel-drain" / "state.json"
                marker = workspace_root / "cancel-drain" / "agent-count"
                deadline = time.monotonic() + 3
                state = None
                while time.monotonic() < deadline:
                    try:
                        candidate = json.loads(state_path.read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        time.sleep(0.02)
                        continue
                    if marker.exists() and (candidate.get("loop") or {}).get("session_id"):
                        state = candidate
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(state, "Agent 啟動後必須能由 Dashboard 看見目前 session")

                handler = self.ResponseCapture()
                D.Handler.api_drain(handler, {"name": "cancel-drain"})
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertTrue(handler.response[1]["requested"])
                request_path = workspace_root / "cancel-drain" / L.STOP_AFTER_ROUND_FILE
                self.assertTrue(request_path.exists())

                handler = self.ResponseCapture()
                D.Handler.api_cancel_drain(handler, {"name": "cancel-drain"})
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertTrue(handler.response[1]["cancelled"])
                self.assertFalse(request_path.exists())

                output, _ = process.communicate(timeout=4)
                self.assertEqual(process.returncode, 0, output)
                stopped = json.loads(state_path.read_text())
                self.assertEqual(stopped["round"], 2)
                self.assertEqual(marker.read_text(), "2", "撤銷後必須真的 spawn 下一輪")
                self.assertNotIn("已依要求停止", output)
            finally:
                D.ROOT = old_root
                if process.poll() is None:
                    process.kill()
                    process.wait()


class TestClaimedDrainProjection(unittest.TestCase):
    """loop 接手後的 marker 必須讓 fleet 與取消 API 明確顯示「太晚」。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_claimed_marker_projects_and_refuses_cancel(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workspace = root / "demo"
            workspace.mkdir()
            (workspace / "state.json").write_text(json.dumps({
                "phase": "plan", "loop": {"pid": 4242, "session_id": "current-session"},
            }))
            L.atomic_write_bytes(
                workspace / L.STOP_AFTER_ROUND_CLAIMED_FILE,
                json.dumps({"pid": 4242, "session_id": "current-session"}).encode(),
            )
            old_root, old_alive = D.ROOT, D.loop_pid_alive
            D.ROOT = root
            D.loop_pid_alive = lambda _pid: True
            try:
                fleet = D.list_workspaces()
                self.assertTrue(fleet[0]["draining"])
                self.assertTrue(fleet[0]["drain_claimed"])
                handler = self.ResponseCapture()
                D.Handler.api_cancel_drain(handler, {"name": "demo"})
                self.assertEqual(handler.response[0], 409)
                self.assertIn("已被 loop 取走", handler.response[1]["error"])
            finally:
                D.ROOT, D.loop_pid_alive = old_root, old_alive


class TestPortableDashboardConfig(unittest.TestCase):
    """GUI/IDE 沒載入 shell profile 時，個人 PATH 與團隊/個人分層仍應生效。"""

    def test_home_relative_extra_path_finds_cli(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            bindir = home / ".local" / "bin"
            bindir.mkdir(parents=True)
            cli = bindir / "portable-cli"
            cli.write_text("#!/bin/sh\necho portable-ok\n")
            cli.chmod(0o755)
            old_home, old_path = os.environ.get("HOME"), os.environ.get("PATH")
            try:
                os.environ["HOME"] = str(home)
                os.environ["PATH"] = "/usr/bin:/bin"
                env = D.command_env({"extra_path_dirs": ["~/.local/bin"]})
                result = subprocess.run(["portable-cli"], capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0)
                self.assertIn("portable-ok", result.stdout)
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path

    def test_personal_save_never_rewrites_project_config(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            project_path = root / "dashboard.config.shared.json"
            personal_path = root / "dashboard.config.local.json"
            legacy_path = root / "dashboard.config.json"
            project = {
                "agent_cmds": [{"label": "shared", "cmd": "shared-cli"}],
                "validate_cmds": [{"label": "green", "cmd": "true"}],
                "repo_roots": ["~/shared"],
                "defaults": {"flag_threshold": 7},
            }
            project_path.write_text(json.dumps(project))
            original = project_path.read_bytes()
            old_values = (D.CONFIG_OVERRIDE, D.PROJECT_CONFIG_PATH, D.PERSONAL_CONFIG_PATH,
                          D.LEGACY_CONFIG_PATH, D.CONFIG_PATH)
            try:
                D.CONFIG_OVERRIDE = None
                D.PROJECT_CONFIG_PATH = project_path
                D.PERSONAL_CONFIG_PATH = personal_path
                D.LEGACY_CONFIG_PATH = legacy_path
                D.CONFIG_PATH = personal_path
                D.save_personal_config({
                    "agent_cmds": [{"label": "mine", "cmd": "my-cli"}],
                    "extra_path_dirs": ["~/.local/bin"],
                })
                effective = D.load_config()
                self.assertEqual(project_path.read_bytes(), original)
                self.assertEqual(effective["validate_cmds"], project["validate_cmds"])
                self.assertEqual(effective["agent_cmds"][0]["cmd"], "my-cli")
                self.assertEqual(set(json.loads(personal_path.read_text())),
                                 {"agent_cmds", "extra_path_dirs"})
            finally:
                (D.CONFIG_OVERRIDE, D.PROJECT_CONFIG_PATH, D.PERSONAL_CONFIG_PATH,
                 D.LEGACY_CONFIG_PATH, D.CONFIG_PATH) = old_values


class TestDashboardStateLockCoverage(unittest.TestCase):
    """#3 run/launch 必須和 edit/phase 共用 workspace lock,不能在 stopped check 後競態。"""

    def test_all_workspace_mutations_are_decorated(self):
        for method in ("api_launch", "api_run", "api_drain", "api_cancel_drain", "api_edit_state", "api_edit_config", "api_validate", "api_preflight", "api_test_agent", "api_phase", "api_set_task"):
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


class TestReportProjection(unittest.TestCase):
    """REPORT.md 唯讀投影:存在回內容、不存在回明確 error,不寫任何 truth。"""

    def test_missing_then_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            (root / "demo").mkdir(parents=True)
            old_root = D.ROOT
            D.ROOT = root
            try:
                self.assertIn("error", D.read_report("demo"), "沒有 REPORT.md 應回明確 error")
                report_text = "# loop-agent-lite RUN REPORT\n- task-1 @ abcd1234\n"
                (root / "demo" / "REPORT.md").write_text(report_text, encoding="utf-8")
                self.assertEqual(D.read_report("demo"), {"content": report_text})
            finally:
                D.ROOT = old_root


class TestWorkspaceArchive(unittest.TestCase):
    """封存=軟刪除:停止狀態整目錄搬進 .archive/;執行中或單 writer 鎖被持有時 fail-closed 拒絕。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_refuses_running_and_held_lock_then_archives(self):
        import fcntl
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            (root / "demo").mkdir(parents=True)
            (root / "demo" / "state.json").write_text(
                json.dumps({"phase": "done", "loop": {"pid": None}}), encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            try:
                # 執行中 → 拒絕,目錄不動
                old_running = D.ws_running
                D.ws_running = lambda *a, **k: True
                try:
                    handler = self.ResponseCapture()
                    D.Handler.api_archive_workspace(handler, {"name": "demo"})
                finally:
                    D.ws_running = old_running
                self.assertEqual(handler.response[0], 400)
                self.assertTrue((root / "demo").exists())

                # 單 writer 鎖被持有 → fail-closed 拒絕(pid 偵測失準的兜底)
                holder = open(root / "demo" / ".run.lock", "a+b")
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    handler = self.ResponseCapture()
                    D.Handler.api_archive_workspace(handler, {"name": "demo"})
                finally:
                    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                    holder.close()
                self.assertEqual(handler.response[0], 409)
                self.assertIn("單 writer 鎖", handler.response[1]["error"])
                self.assertTrue((root / "demo").exists())

                # 停止且無鎖 → 搬進 .archive/,原目錄消失、內容完整
                handler = self.ResponseCapture()
                D.Handler.api_archive_workspace(handler, {"name": "demo"})
                self.assertEqual(handler.response[0], 200)
                self.assertFalse((root / "demo").exists())
                archived = [d for d in (root / ".archive").iterdir() if d.is_dir()]
                self.assertEqual(len(archived), 1)
                self.assertTrue((archived[0] / "state.json").exists())
                # .archive 不得冒充 workspace 出現在 fleet
                self.assertEqual(D.list_workspaces(), [])
            finally:
                D.ROOT = old_root


class TestWorkspaceArchiveRestore(unittest.TestCase):
    """封存還原必須只搬真實目錄、不覆寫既有目標，且不會暗中啟動 loop。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    @staticmethod
    def _seed_workspace(root, name="demo", *, round_number=7):
        workspace = root / name
        workspace.mkdir(parents=True)
        (workspace / "state.json").write_text(
            json.dumps({"phase": "done", "round": round_number, "loop": {"pid": None}}), encoding="utf-8")
        (workspace / "nested").mkdir()
        (workspace / "nested" / "kept.txt").write_text("preserve me\n", encoding="utf-8")
        return workspace

    def test_archive_list_and_restore_preserve_content_without_starting_loop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            workspace = self._seed_workspace(root)
            old_root = D.ROOT
            try:
                D.ROOT = root
                archive = self.ResponseCapture()
                D.Handler.api_archive_workspace(archive, {"name": "demo"})
                self.assertEqual(archive.response[0], 200, archive.response)
                archive_id = archive.response[1]["archive_id"]
                self.assertRegex(archive_id, r"^demo--\d{8}T\d{6}Z--[0-9a-f]{32}$")
                self.assertFalse(workspace.exists())

                listed = D.list_archives()
                self.assertNotIn("error", listed)
                self.assertEqual(len(listed["archives"]), 1)
                self.assertEqual(listed["archives"][0]["id"], archive_id)
                self.assertEqual(listed["archives"][0]["name"], "demo")
                self.assertEqual(listed["archives"][0]["phase"], "done")
                self.assertEqual(listed["archives"][0]["round"], 7)

                restore = self.ResponseCapture()
                D.Handler.api_restore_workspace(restore, {"archive_id": archive_id})
                self.assertEqual(restore.response[0], 200, restore.response)
                self.assertEqual(restore.response[1], {"ok": True, "name": "demo", "archive_id": archive_id})
                self.assertEqual((root / "demo" / "nested" / "kept.txt").read_text(), "preserve me\n")
                self.assertEqual(D.list_archives()["archives"], [])
                self.assertEqual([item["name"] for item in D.list_workspaces()], ["demo"])
                self.assertFalse(D.ws_running("demo"), "還原只恢復 state，不得自動啟動 loop")
                with D.JOBS_LOCK:
                    self.assertNotIn("demo", D.JOBS)
            finally:
                D.ROOT = old_root

    def test_restores_legacy_archive_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            archive_id = "legacy-orders-20250102-030405"
            self._seed_workspace(root / ".archive", archive_id, round_number=3)
            old_root = D.ROOT
            try:
                D.ROOT = root
                listed = D.list_archives()["archives"]
                self.assertEqual([(item["id"], item["name"]) for item in listed], [(archive_id, "legacy-orders")])
                handler = self.ResponseCapture()
                D.Handler.api_restore_workspace(handler, {"archive_id": archive_id})
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertTrue((root / "legacy-orders" / "state.json").is_file())
            finally:
                D.ROOT = old_root

    def test_restore_rejects_invalid_or_colliding_destination_without_moving_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            archive_id = "collision--20250102T030405Z--" + "a" * 32
            archived = self._seed_workspace(root / ".archive", archive_id)
            old_root = D.ROOT
            try:
                D.ROOT = root
                for bad_id in ("", "..", "../" + archive_id, "/tmp/" + archive_id, "not-an-archive"):
                    handler = self.ResponseCapture()
                    D.Handler.api_restore_workspace(handler, {"archive_id": bad_id})
                    with self.subTest(bad_id=bad_id):
                        self.assertEqual(handler.response[0], 400)
                        self.assertTrue(archived.exists())

                with L.workspace_operation_lock(root, "collision"):
                    handler = self.ResponseCapture()
                    D.Handler.api_restore_workspace(handler, {"archive_id": archive_id})
                    self.assertEqual(handler.response[0], 409, handler.response)
                    self.assertTrue(archived.exists(), "CLI 建立 workspace 的 root lock 存在時不得搬移")

                destination = root / "collision"
                cases = {
                    "directory": lambda: destination.mkdir(),
                    "file": lambda: destination.write_text("occupied", encoding="utf-8"),
                    "dangling symlink": lambda: destination.symlink_to(root / "missing-target"),
                }
                for kind, create in cases.items():
                    create()
                    handler = self.ResponseCapture()
                    D.Handler.api_restore_workspace(handler, {"archive_id": archive_id})
                    with self.subTest(destination=kind):
                        self.assertEqual(handler.response[0], 409, handler.response)
                        self.assertTrue(archived.exists(), "衝突不得讓來源消失")
                    if destination.is_dir() and not destination.is_symlink():
                        destination.rmdir()
                    else:
                        destination.unlink(missing_ok=True)
            finally:
                D.ROOT = old_root

    def test_archive_refuses_held_locks_and_symlink_archive_root(self):
        import fcntl
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            workspace = self._seed_workspace(root)
            old_root = D.ROOT
            try:
                D.ROOT = root
                holder = open(workspace / ".run.lock", "a+b")
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    handler = self.ResponseCapture()
                    D.Handler.api_archive_workspace(handler, {"name": "demo"})
                finally:
                    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                    holder.close()
                self.assertEqual(handler.response[0], 409, handler.response)
                self.assertTrue(workspace.exists())

                archive_root = root / ".archive"
                archive_root.mkdir(exist_ok=True)
                holder = open(archive_root / ".ops.lock", "a+b")
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    handler = self.ResponseCapture()
                    D.Handler.api_archive_workspace(handler, {"name": "demo"})
                finally:
                    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                    holder.close()
                self.assertEqual(handler.response[0], 409, handler.response)
                self.assertTrue(workspace.exists())

                shutil.rmtree(archive_root)
                outside = Path(td) / "outside-archive"
                outside.mkdir()
                archive_root.symlink_to(outside, target_is_directory=True)
                handler = self.ResponseCapture()
                D.Handler.api_archive_workspace(handler, {"name": "demo"})
                self.assertEqual(handler.response[0], 409, handler.response)
                self.assertTrue(workspace.exists())
                self.assertEqual(list(outside.iterdir()), [])

                archive_root.unlink()
                archive_root.mkdir()
                outside_lock = Path(td) / "outside-run.lock"
                (workspace / ".run.lock").unlink()
                (workspace / ".run.lock").symlink_to(outside_lock)
                handler = self.ResponseCapture()
                D.Handler.api_archive_workspace(handler, {"name": "demo"})
                self.assertEqual(handler.response[0], 409, handler.response)
                self.assertTrue(workspace.exists())
                self.assertFalse(outside_lock.exists(), "descriptor-relative lock 不得跟隨 workspace 內 symlink")

                archive_id = "symlinked--20250102T030405Z--" + "b" * 32
                outside_entry = Path(td) / "outside-entry"
                outside_entry.mkdir()
                (archive_root / archive_id).symlink_to(outside_entry, target_is_directory=True)
                self.assertEqual(D.list_archives()["archives"], [], "封存列表不得投影 symlink entry")
                handler = self.ResponseCapture()
                D.Handler.api_restore_workspace(handler, {"archive_id": archive_id})
                self.assertEqual(handler.response[0], 409, handler.response)
                self.assertFalse((root / "symlinked").exists())
            finally:
                D.ROOT = old_root


class TestPreflightOnly(unittest.TestCase):
    """--preflight-only:只健檢不啟動——不建 state.json、不動 snapshots,依結果回 exit code。"""

    def _run(self, repo, workspace_root, validate_cmd, name):
        env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
        return subprocess.run(
            [sys.executable, LOOP_PY, "--repo", str(repo), "--name", name,
             "--validate-cmd", validate_cmd, "--preflight-only"],
            capture_output=True, text=True, env=env)

    def test_green_passes_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            workspace_root = Path(d) / "ws"
            r = self._run(repo, workspace_root, "true", "pfonly-green")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("preflight-only 全部通過", r.stdout)
            wsd = workspace_root / "pfonly-green"
            self.assertFalse((wsd / "state.json").exists(), "健檢不得建立 state")
            self.assertFalse(list((wsd / "snapshots").iterdir()), "健檢不得寫 snapshots")

    def test_red_validate_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            workspace_root = Path(d) / "ws"
            r = self._run(repo, workspace_root, "false", "pfonly-red")
            self.assertEqual(r.returncode, 1)
            self.assertIn("驗證失敗", r.stdout)
            self.assertFalse((workspace_root / "pfonly-red" / "state.json").exists())

    def test_dirty_tree_fails_before_validate(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            (repo / "wip.txt").write_text("uncommitted\n")
            workspace_root = Path(d) / "ws"
            r = self._run(repo, workspace_root, "true", "pfonly-dirty")
            self.assertEqual(r.returncode, 1)
            self.assertIn("工作樹不乾淨", r.stdout)


class TestCliArgumentGuards(unittest.TestCase):
    """直接 CLI 不得用非法數值繞過共識或在 preflight 後才 runtime crash。"""

    def test_invalid_numbers_fail_before_workspace_or_agent_spawn(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            marker = root / "agent-started"
            agent = root / "agent.py"
            agent.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n")
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            cases = [
                ("--flag-threshold", "0"), ("--done-threshold", "0"),
                ("--red-limit", "0"), ("--stall-limit", "0"),
                ("--stuck-stop-count", "0"), ("--max-rounds", "-1"),
                ("--round-timeout", "-0.1"), ("--round-timeout", "nan"),
                ("--agent-backoff-max", "-1"), ("--agent-backoff-max", "inf"),
                ("--validate-timeout", "0"), ("--validate-timeout", "nan"),
            ]
            for index, (option, value) in enumerate(cases):
                name = f"bad-number-{index}"
                result = subprocess.run(
                    [sys.executable, LOOP_PY, "--repo", str(repo), "--name", name,
                     "--agent-cmd", shlex.join([sys.executable, str(agent)]),
                     "--validate-cmd", "true", option, value],
                    capture_output=True, text=True, env=env,
                )
                with self.subTest(option=option, value=value):
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(option, result.stderr)
                    self.assertFalse((workspace_root / name).exists(), "參數錯誤不得先建立 workspace")
            self.assertFalse(marker.exists(), "參數錯誤不得 spawn Agent")

    def test_zero_done_threshold_cannot_complete_imported_exec_plan(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            plan = root / "plan.json"
            plan.write_text(json.dumps([{"order": 1, "task": "不得被零門檻跳過"}]))
            marker = root / "agent-started"
            agent = root / "noop_agent.py"
            agent.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n")
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            result = subprocess.run(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "zero-done",
                 "--agent-cmd", shlex.join([sys.executable, str(agent)]), "--validate-cmd", "true",
                 "--import-plan", str(plan), "--start-phase", "exec", "--done-threshold", "0"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("--done-threshold", result.stderr)
            self.assertFalse((workspace_root / "zero-done" / "state.json").exists())
            self.assertFalse(marker.exists(), "零門檻不得有機會繞過 work.py done 共識")


class TestWorkspaceNameGuards(unittest.TestCase):
    """workspace 名稱是 coordinator root 的安全邊界，dot-leading 不得逸出或碰保留目錄。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_workspace_constructor_rejects_unsafe_names_without_creating_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workspace_root = root / "workspace"
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = workspace_root
                for name in ("", ".", "..", ".archive", ".hidden", "a/b", r"a\b"):
                    with self.subTest(name=name), self.assertRaises(ValueError):
                        L.Workspace(name)
                self.assertFalse(workspace_root.exists(), "非法名稱不得先建立 coordinator 根目錄")
                self.assertFalse((root / "logs").exists(), ".. 不得把 logs 寫到 workspace 外")
                workspace_root.mkdir()
                outside = root / "outside"
                outside.mkdir()
                (workspace_root / "linked").symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ValueError):
                    L.Workspace("linked")
                self.assertFalse((outside / "logs").exists(), "symlink workspace 不得把 coordinator 寫到 root 外")
                for name in ("legacy-orders", "a.b", "_scratch", "-scratch"):
                    with self.subTest(valid_name=name):
                        self.assertEqual(L.Workspace(name).dir, workspace_root / name)
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_cli_rejects_dot_leading_names_before_workspace_or_agent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            marker = root / "agent-started"
            agent = root / "agent.py"
            agent.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n")
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            for name in (".", "..", ".archive", ".hidden"):
                result = subprocess.run(
                    [sys.executable, LOOP_PY, "--repo", str(repo), "--name", name,
                     "--agent-cmd", shlex.join([sys.executable, str(agent)]),
                     "--validate-cmd", "true", "--preflight-only"],
                    capture_output=True, text=True, env=env,
                )
                with self.subTest(name=name):
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("--name", result.stderr)
                    self.assertFalse(workspace_root.exists(), "非法名稱不得建立 workspace")
                    self.assertFalse((root / "logs").exists(), ".. 不得在 workspace 外建立 logs")
            workspace_root.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (workspace_root / "linked").symlink_to(outside, target_is_directory=True)
            result = subprocess.run(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "linked",
                 "--agent-cmd", shlex.join([sys.executable, str(agent)]),
                 "--validate-cmd", "true", "--preflight-only"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("symbolic link", result.stderr)
            self.assertFalse((outside / "logs").exists(), "CLI 不得經由 symlink 寫出 workspace root")
            self.assertFalse(marker.exists(), "非法名稱不得 spawn Agent")

    def test_dashboard_rejects_dot_leading_names_and_hides_legacy_hidden_directories(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            valid = workspace_root / "valid"
            hidden = workspace_root / ".hidden"
            archived = workspace_root / ".archive"
            for directory in (valid, hidden, archived):
                directory.mkdir(parents=True)
                (directory / "state.json").write_text(json.dumps({"phase": "done"}), encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            (outside / "state.json").write_text(json.dumps({"phase": "done"}), encoding="utf-8")
            (workspace_root / "linked").symlink_to(outside, target_is_directory=True)
            old_values = (D.ROOT, L.WORKSPACE_ROOT, D.load_config)
            try:
                D.ROOT = workspace_root
                L.WORKSPACE_ROOT = workspace_root
                D.load_config = lambda: {
                    "agent_cmds": [{"label": "true", "cmd": "true"}],
                    "validate_cmds": [{"label": "true", "cmd": "true"}],
                    "extra_path_dirs": [], "notify_cmd": "", "defaults": {"validate_timeout": 5},
                }
                state, error = D.read_state("..")
                self.assertIsNone(state)
                self.assertIn("不合法", error)
                D.workspace_console_log("..", "不得寫到 workspace 外")
                self.assertFalse((root / "console.log").exists())
                with self.assertRaises(ValueError):
                    D.write_state("..", {"phase": "done"})
                state, error = D.read_state("linked")
                self.assertIsNone(state)
                self.assertIn("symbolic link", error)
                D.workspace_console_log("linked", "不得經由連結寫出 root")
                self.assertFalse((outside / "console.log").exists())

                handler = self.ResponseCapture()
                D.Handler.api_launch(handler, {
                    "repo": str(repo), "name": ".archive", "agent_idx": 0, "validate_idx": 0,
                })
                self.assertEqual(handler.response[0], 400)
                self.assertIn("不合法", handler.response[1]["error"])
                self.assertNotIn(".archive", D.JOBS, "非法名稱不得建立 dashboard job")

                handler = self.ResponseCapture()
                D.Handler.api_launch(handler, {
                    "repo": str(repo), "name": "linked", "agent_idx": 0, "validate_idx": 0,
                })
                self.assertEqual(handler.response[0], 400)
                self.assertIn("symbolic link", handler.response[1]["error"])
                self.assertNotIn("linked", D.JOBS)

                handler = self.ResponseCapture()
                self.assertIsNone(D.Handler._ws_dir(handler, {"ws": [".archive"]}))
                self.assertEqual(handler.response[0], 400)
                self.assertEqual([item["name"] for item in D.list_workspaces()], ["valid"])
            finally:
                D.ROOT, L.WORKSPACE_ROOT, D.load_config = old_values


class TestProtectedPathGuards(unittest.TestCase):
    """goal/plan-doc 只能落在 target repo 內的 regular file，不得穿越或跟隨 symlink。"""

    def test_cli_rejects_escape_absolute_and_symlink_paths_before_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            outside = root / "outside.md"
            outside.write_text("outside secret\n", encoding="utf-8")
            (repo / "goal-link").symlink_to(outside)
            workspace_root = root / "workspace"
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            cases = [
                ("--goal", "../outside.md"),
                ("--goal", str(outside)),
                ("--goal", "goal-link"),
                ("--plan-doc", "../outside.md"),
            ]
            for index, (option, value) in enumerate(cases):
                result = subprocess.run(
                    [sys.executable, LOOP_PY, "--repo", str(repo), "--name", f"protected-{index}",
                     "--agent-cmd", "true", "--validate-cmd", "true", "--preflight-only",
                     option, value],
                    capture_output=True, text=True, env=env,
                )
                with self.subTest(option=option, value=value):
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(option, result.stderr)
                    self.assertFalse(workspace_root.exists(), "非法 protected path 不得建立 workspace")
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside secret\n")

    def test_workspace_and_dashboard_reject_unsafe_goal_projection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            outside = root / "outside.md"
            outside.write_text("do not expose\n", encoding="utf-8")
            workspace_root = root / "workspace"
            old_values = (L.WORKSPACE_ROOT, D.ROOT)
            try:
                L.WORKSPACE_ROOT = workspace_root
                ws = L.Workspace("protected")
                with self.assertRaises(ValueError):
                    ws.snapshot_protected(repo, ["../outside.md"])
                with self.assertRaises(ValueError):
                    ws.snapshot_protected(repo, [str(outside)])

                D.ROOT = workspace_root
                dashboard_workspace = workspace_root / "dashboard"
                dashboard_workspace.mkdir(parents=True)
                (dashboard_workspace / "state.json").write_text(json.dumps({
                    "phase": "done",
                    "config": {"repo": str(repo), "goal": "../outside.md"},
                }), encoding="utf-8")
                projection = D.read_goal("dashboard")
                self.assertIn("goal 路徑不合法", projection["error"])
                self.assertNotIn("do not expose", json.dumps(projection, ensure_ascii=False))
            finally:
                L.WORKSPACE_ROOT, D.ROOT = old_values


class TestGoalProjection(unittest.TestCase):
    """goal 唯讀投影:從 state.config 的 repo+goal 讀人類真相;缺 config/缺檔回明確 error。"""

    def test_goal_projection_states(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            root = Path(td) / "workspace"
            (root / "demo").mkdir(parents=True)
            old_root = D.ROOT
            D.ROOT = root
            try:
                # 舊版 state 缺 config.repo → 明確 error
                (root / "demo" / "state.json").write_text(json.dumps({"phase": "plan"}), encoding="utf-8")
                self.assertIn("缺 repo 設定", D.read_goal("demo")["error"])
                # 正常:回 goal 內容與路徑,goal_changed 透傳
                (root / "demo" / "state.json").write_text(json.dumps(
                    {"phase": "plan", "goal_changed": True,
                     "config": {"repo": str(repo), "goal": "goal.md"}}), encoding="utf-8")
                result = D.read_goal("demo")
                self.assertEqual(result["content"], "GOAL v1\n")
                self.assertTrue(result["goal_changed"])
                # goal 檔被移走 → 明確 error,不 crash
                (repo / "goal.md").unlink()
                self.assertIn("goal 檔不存在", D.read_goal("demo")["error"])
            finally:
                D.ROOT = old_root


class TestPromptProjection(unittest.TestCase):
    """prompt 唯讀投影:取 round 編號最大的一份;無紀錄回明確 error。"""

    def test_latest_prompt_by_round_number(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            prompts = root / "demo" / "prompts"
            prompts.mkdir(parents=True)
            old_root = D.ROOT
            D.ROOT = root
            try:
                self.assertIn("尚無 prompt", D.read_prompt("demo")["error"])
                (prompts / "round-0002.md").write_text("prompt r2", encoding="utf-8")
                (prompts / "round-0010.md").write_text("prompt r10", encoding="utf-8")
                result = D.read_prompt("demo")
                self.assertEqual(result["content"], "prompt r10")
                self.assertEqual(result["round"], 10)
                self.assertEqual(result["file"], "round-0010.md")
            finally:
                D.ROOT = old_root


class TestDashboardPreflight(unittest.TestCase):
    """Dashboard 必須復用唯一的 --preflight-only 實作，不誤建 coordinator state。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_green_and_held_lock_are_reported_without_state(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            workspace_root = Path(d) / "workspace"
            old_root, old_workspace_root, old_load = D.ROOT, L.WORKSPACE_ROOT, D.load_config
            old_env = os.environ.get("LOOP_AGENT_WORKSPACE_ROOT")
            D.ROOT = workspace_root
            L.WORKSPACE_ROOT = workspace_root
            D.load_config = lambda: {
                "validate_cmds": [{"label": "green", "cmd": "true"}],
                "extra_path_dirs": [], "defaults": {"validate_timeout": 5},
            }
            os.environ["LOOP_AGENT_WORKSPACE_ROOT"] = str(workspace_root)
            try:
                body = {"repo": str(repo), "name": "dashboard-preflight", "validate_idx": 0,
                        "validate_timeout": 5}
                handler = self.ResponseCapture()
                D.Handler.api_preflight(handler, body)
                self.assertEqual(handler.response[0], 200)
                self.assertTrue(handler.response[1]["ok"], handler.response)
                workspace = workspace_root / "dashboard-preflight"
                self.assertFalse((workspace / "state.json").exists())
                self.assertFalse((workspace / "state.last-good.json").exists())
                self.assertFalse((workspace / "dispatch.json").exists())
                self.assertFalse(list((workspace / "snapshots").iterdir()))

                import fcntl
                holder = open(workspace / ".run.lock", "a+b")
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    handler = self.ResponseCapture()
                    D.Handler.api_preflight(handler, body)
                finally:
                    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                    holder.close()
                self.assertEqual(handler.response[0], 200)
                self.assertFalse(handler.response[1]["ok"])
                self.assertIn("單 writer 鎖", handler.response[1]["tail"])
                self.assertFalse((workspace / "state.json").exists())
            finally:
                D.ROOT, L.WORKSPACE_ROOT, D.load_config = old_root, old_workspace_root, old_load
                if old_env is None:
                    os.environ.pop("LOOP_AGENT_WORKSPACE_ROOT", None)
                else:
                    os.environ["LOOP_AGENT_WORKSPACE_ROOT"] = old_env



class TestNotifyEndpoints(unittest.TestCase):
    """通知管理:test-notify 以 {status}=test 實跑;edit-notify 只寫個人設定檔。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_test_notify_substitutes_placeholders(self):
        old_load = D.load_config
        D.load_config = lambda: json.loads(json.dumps(D.DEFAULT_CONFIG))
        try:
            handler = self.ResponseCapture()
            D.Handler.api_test_notify(handler, {"notify_cmd": "echo ping-{status}-{name}"})
        finally:
            D.load_config = old_load
        self.assertEqual(handler.response[0], 200)
        self.assertTrue(handler.response[1]["ok"])
        self.assertIn("ping-test-dashboard-test", handler.response[1]["output"])

    def test_edit_notify_persists_to_personal_config(self):
        with tempfile.TemporaryDirectory() as td:
            personal = Path(td) / "personal.json"
            old = (D.PERSONAL_CONFIG_PATH, D.CONFIG_OVERRIDE)
            D.PERSONAL_CONFIG_PATH, D.CONFIG_OVERRIDE = personal, None
            try:
                handler = self.ResponseCapture()
                D.Handler.api_edit_notify(handler, {"notify_cmd": "echo done-{status}"})
                self.assertEqual(handler.response[0], 200)
                saved = json.loads(personal.read_text(encoding="utf-8"))
                self.assertEqual(saved["notify_cmd"], "echo done-{status}")
                self.assertEqual(handler.response[1]["notify_cmd"], "echo done-{status}")
                # 空字串=停用,合法
                handler = self.ResponseCapture()
                D.Handler.api_edit_notify(handler, {"notify_cmd": ""})
                self.assertEqual(handler.response[0], 200)
                self.assertEqual(json.loads(personal.read_text(encoding="utf-8"))["notify_cmd"], "")
            finally:
                D.PERSONAL_CONFIG_PATH, D.CONFIG_OVERRIDE = old


class TestFleetHistoryProjection(unittest.TestCase):
    """fleet 事件流投影:回各 workspace history.log 尾段;無 history 的 workspace 跳過,不動 truth。"""

    def test_tail_and_current_task_projection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            (root / "alpha").mkdir(parents=True)
            (root / "beta").mkdir(parents=True)
            line = ("2026-07-10T10:00:00 round=1 phase=exec task=task-1 rc=0 changed=False "
                    "signal=done tamper=False agent_ok=True validate=PASS flag=0 done=1")
            (root / "alpha" / "history.log").write_text(line + "\n", encoding="utf-8")
            (root / "alpha" / "state.json").write_text(json.dumps({
                "phase": "exec", "current_order": 2,
                "plan": [{"order": 1, "task": "第一項", "ref": None},
                         {"order": 2, "task": "第二項很長" + "x" * 200, "ref": None}],
            }), encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            try:
                entries = D.read_fleet_history()
                self.assertEqual([e["name"] for e in entries], ["alpha"], "沒 history 的 beta 應跳過")
                self.assertIn("task=task-1", entries[0]["data"])
                fleet = D.list_workspaces()
                alpha = next(w for w in fleet if w["name"] == "alpha")
                self.assertEqual(alpha["current_order"], 2)
                self.assertTrue(alpha["current_task"].startswith("第二項很長"))
                self.assertLessEqual(len(alpha["current_task"]), 121, "任務文字應截斷")
            finally:
                D.ROOT = old_root


class TestFreshStartClearsRoundArtifacts(unittest.TestCase):
    """reset/import 是交易式「從頭跑」:preflight 通過後舊 run 的 history/REPORT/prompt/log
    不得混進新 run(history 輪替保留 .1);preflight 失敗時全數保留。"""

    def _seed(self, workspace_root, name):
        wsd = workspace_root / name
        (wsd / "prompts").mkdir(parents=True)
        (wsd / "logs").mkdir(parents=True)
        (wsd / "state.json").write_text(json.dumps({"phase": "plan", "round": 7}), encoding="utf-8")
        (wsd / "history.log").write_text(
            "2026-07-09T22:00:00 round=7 phase=plan task=- rc=0 changed=False "
            "signal=- tamper=False agent_ok=True validate=- flag=3 done=0\n", encoding="utf-8")
        (wsd / "REPORT.md").write_text("stale report\n", encoding="utf-8")
        (wsd / "prompts" / "round-0007.md").write_text("stale prompt\n", encoding="utf-8")
        (wsd / "logs" / "round-0007.log").write_text("stale log\n", encoding="utf-8")
        return wsd

    def _run_reset(self, repo, workspace_root, name, validate_cmd):
        env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
        agent = f"{sys.executable} -c pass"
        return subprocess.run(
            [sys.executable, LOOP_PY, "--repo", str(repo), "--name", name,
             "--agent-cmd", agent, "--validate-cmd", validate_cmd,
             "--reset-state", "--max-rounds", "1"],
            capture_output=True, text=True, env=env)

    def test_successful_reset_rotates_history_and_clears_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            workspace_root = Path(d) / "ws"
            wsd = self._seed(workspace_root, "fresh-clear")
            r = self._run_reset(repo, workspace_root, "fresh-clear", "true")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            history = (wsd / "history.log").read_text(encoding="utf-8")
            self.assertIn("round=1", history, "新 run 應從 round=1 開始")
            self.assertNotIn("round=7", history, "舊 run 的輪次不得混入")
            self.assertIn("round=7", (wsd / "history.log.1").read_text(encoding="utf-8"),
                          "舊 history 應輪替保留一代")
            self.assertFalse((wsd / "REPORT.md").exists(), "過期 REPORT 應清除")
            prompts = sorted(p.name for p in (wsd / "prompts").glob("round-*.md"))
            self.assertEqual(prompts, ["round-0001.md"], "舊 prompt 不得蓋過新 run 的投影")

    def test_failed_preflight_preserves_previous_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            workspace_root = Path(d) / "ws"
            wsd = self._seed(workspace_root, "fresh-keep")
            r = self._run_reset(repo, workspace_root, "fresh-keep", "false")
            self.assertEqual(r.returncode, 1)
            self.assertIn("round=7", (wsd / "history.log").read_text(encoding="utf-8"),
                          "preflight 失敗時舊 history 原封不動")
            self.assertFalse((wsd / "history.log.1").exists())
            self.assertTrue((wsd / "REPORT.md").exists())
            self.assertTrue((wsd / "prompts" / "round-0007.md").exists())
class TestDashboardFileBoundaries(unittest.TestCase):
    """Dashboard 的設定檔與 goal 匯入不應跟隨 symlink 讀寫外部檔案。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_config_symlink_is_rejected_for_read_and_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside-config.json"
            outside.write_text(json.dumps({"notify_cmd": "secret"}), encoding="utf-8")
            personal = root / "personal.json"
            personal.symlink_to(outside)
            old = (D.PERSONAL_CONFIG_PATH, D.PROJECT_CONFIG_PATH,
                   D.LEGACY_CONFIG_PATH, D.CONFIG_OVERRIDE)
            try:
                D.PERSONAL_CONFIG_PATH = personal
                D.PROJECT_CONFIG_PATH = root / "project.json"
                D.LEGACY_CONFIG_PATH = root / "legacy.json"
                D.CONFIG_OVERRIDE = True
                cfg = D.load_config()
                self.assertIn("error", cfg)
                self.assertIn("symbolic link", cfg["error"])
                with self.assertRaises(ValueError):
                    D.save_personal_config({"notify_cmd": "changed"})
                self.assertEqual(json.loads(outside.read_text(encoding="utf-8"))["notify_cmd"], "secret")
            finally:
                (D.PERSONAL_CONFIG_PATH, D.PROJECT_CONFIG_PATH,
                 D.LEGACY_CONFIG_PATH, D.CONFIG_OVERRIDE) = old

    def test_goal_import_rejects_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(td)
            outside = root / "outside-goal.md"
            outside.write_text("do not overwrite\n", encoding="utf-8")
            goal = repo / "goal.md"
            goal.unlink()
            goal.symlink_to(outside)
            workspace_root = root / "workspace"
            old_values = (D.ROOT, L.WORKSPACE_ROOT, D.load_config)
            try:
                D.ROOT = workspace_root
                L.WORKSPACE_ROOT = workspace_root
                D.load_config = lambda: {
                    "agent_cmds": [{"label": "true", "cmd": "true"}],
                    "validate_cmds": [{"label": "true", "cmd": "true"}],
                    "extra_path_dirs": [], "notify_cmd": "",
                    "defaults": {"validate_timeout": 5},
                }
                handler = self.ResponseCapture()
                D.Handler.api_launch(handler, {
                    "repo": str(repo), "name": "goal-link", "agent_idx": 0,
                    "validate_idx": 0, "goal_content": "must not escape\n",
                })
                self.assertEqual(handler.response[0], 400)
                self.assertIn("goal.md 不安全", handler.response[1]["error"])
                self.assertEqual(outside.read_text(encoding="utf-8"), "do not overwrite\n")
                self.assertNotIn("goal-link", D.JOBS)
            finally:
                D.ROOT, L.WORKSPACE_ROOT, D.load_config = old_values

    def test_goal_precheck_happens_before_new_branch_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(td)
            outside = root / "outside-goal.md"
            outside.write_text("do not overwrite\n", encoding="utf-8")
            goal = repo / "goal.md"
            goal.unlink()
            goal.symlink_to(outside)
            old_values = (D.ROOT, L.WORKSPACE_ROOT, D.load_config)
            try:
                D.ROOT = root / "workspace"
                L.WORKSPACE_ROOT = D.ROOT
                D.load_config = lambda: {
                    "agent_cmds": [{"label": "true", "cmd": "true"}],
                    "validate_cmds": [{"label": "true", "cmd": "true"}],
                    "extra_path_dirs": [], "notify_cmd": "",
                    "defaults": {"validate_timeout": 5},
                }
                handler = self.ResponseCapture()
                D.Handler.api_launch(handler, {
                    "repo": str(repo), "name": "goal-link-branch", "agent_idx": 0,
                    "validate_idx": 0, "new_branch": True, "goal_content": "must not escape\n",
                })
                self.assertEqual(handler.response[0], 400)
                self.assertEqual(git(repo, "branch", "--show-current").stdout.strip(), "main")
                self.assertNotEqual(git(repo, "rev-parse", "--verify", "--quiet", "loop/goal-link-branch").returncode, 0)
            finally:
                D.ROOT, L.WORKSPACE_ROOT, D.load_config = old_values


class TestJobHistoryRetention(unittest.TestCase):
    """Dashboard 長跑時只保留有限已結束 job，活躍 job 與 workspace 真相不受影響。"""

    class DummyJob:
        def __init__(self, alive):
            self._alive = alive

        def alive(self):
            return self._alive

    def test_prunes_oldest_finished_but_keeps_active(self):
        old_jobs = D.JOBS
        try:
            D.JOBS = {
                "active": self.DummyJob(True),
                "finished-1": self.DummyJob(False),
                "finished-2": self.DummyJob(False),
                "finished-3": self.DummyJob(False),
            }
            D.prune_finished_jobs(max_finished=2)
            self.assertEqual(list(D.JOBS), ["active", "finished-2", "finished-3"])
        finally:
            D.JOBS = old_jobs


class TestDashboardRequestLimit(unittest.TestCase):
    """POST body 過大時應在讀取 payload 前拒絕，避免無界記憶體使用。"""

    class FakeHandler:
        readonly = False
        path = "/api/launch"

        def __init__(self, length):
            self.headers = {"Content-Length": str(length)}
            self.rfile = io.BytesIO(b"should not be read")
            self.response = None
            self.close_connection = False

        def _err(self, message, code=400):
            self.response = code, message

    def test_oversized_body_returns_413_without_reading(self):
        handler = self.FakeHandler(D.MAX_REQUEST_BYTES + 1)
        D.Handler.do_POST(handler)
        self.assertEqual(handler.response[0], 413)
        self.assertIn("8 MiB", handler.response[1])
        self.assertEqual(handler.rfile.tell(), 0)
        self.assertTrue(handler.close_connection)

    def test_negative_content_length_is_rejected_as_json_error(self):
        handler = self.FakeHandler(-1)
        D.Handler.do_POST(handler)
        self.assertEqual(handler.response[0], 400)
        self.assertIn("JSON", handler.response[1])


class TestPreviousRunHistoryProjection(unittest.TestCase):
    """history.log.1 只讀投影可供 UI 稽核，且 run 參數不允許任意檔名。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_previous_run_is_projected_and_invalid_run_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            workspace = root / "demo"
            workspace.mkdir(parents=True)
            (workspace / "history.log").write_text("current\n", encoding="utf-8")
            (workspace / "history.log.1").write_text("previous\n", encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            try:
                def invoke(query):
                    handler = self.ResponseCapture()
                    handler.path = f"/api/history?ws=demo&offset=-1{query}"
                    handler._ws_dir = lambda _q: workspace
                    D.Handler.do_GET(handler)
                    return handler.response

                code, body = invoke("&run=previous")
                self.assertEqual(code, 200)
                self.assertEqual(body["run"], "previous")
                self.assertIn("previous", body["data"])
                code, body = invoke("&run=other")
                self.assertEqual(code, 400)
                self.assertIn("current 或 previous", body["error"])
            finally:
                D.ROOT = old_root


if __name__ == "__main__":
    unittest.main(verbosity=2)
