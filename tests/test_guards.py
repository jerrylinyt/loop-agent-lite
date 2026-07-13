#!/usr/bin/env python3
"""協調層防線的回歸測試(stdlib only,無外部依賴)。

對應複審發現的三個 correctness 缺口,全部用真 git + 真 loop.py/work.py 驗證,不做 mock:
- #1 綠點錨定 fail-closed:green 未驗可達性/一致性,reset 回去會弄髒工作樹或還原錯版 goal。
- #2 竄改輪整輪作廢:同一輪偷改 protected + create-plan,竄改的 plan 不得存活。
- #3 原子寫並發:ThreadingHTTPServer 下多執行緒共用 tmp 會 truncate / FileNotFoundError。

跑法:  python3 -m unittest tests.test_guards      # 或  python3 tests/test_guards.py
"""
import io
import hashlib
import json
import os
import re
import select
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from engine import dashboard as D  # noqa: E402
from engine import loop as L  # noqa: E402
from engine import status as S  # noqa: E402
from engine import work as W  # noqa: E402

WORK_CMD = [sys.executable, "-m", "engine.work"]
LOOP_CMD = [sys.executable, "-m", "engine.loop"]
STATUS_CMD = [sys.executable, "-m", "engine.status"]
WS_ROOT = Path(os.environ.get("LOOP_AGENT_WORKSPACE_ROOT", REPO_ROOT / "workspace"))


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

    def test_dashboard_projection_and_mutation_load_do_not_repair_primary(self):
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

                projected, err = D.read_state("dashboard-recover", repair=False)
                self.assertIsNone(err)
                self.assertEqual(projected["round"], 23)
                self.assertTrue(projected["state_recovery_pending"])
                self.assertEqual(ws.state_path.read_text(), "broken", "唯讀 Dashboard 不得修檔")

                class Handler:
                    error = None

                    def _err(self, message, code=400):
                        self.error = code, message

                mutation_state = D._load_state_or_err(Handler(), "dashboard-recover")
                self.assertTrue(mutation_state["state_recovery_pending"])
                self.assertEqual(ws.state_path.read_text(), "broken",
                                 "Dashboard mutation pre-read 不得在 writer lock 外修檔")

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
            common = [*LOOP_CMD, "--repo", str(repo), "--name", "resume-recover",
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

            result = subprocess.run([*WORK_CMD, "done", "task-1"],
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


class TestRoundTelemetry(unittest.TestCase):
    """每輪 Agent 耗時／逾時必須落進 state 與可追溯 history。"""

    def _run(self, root, name, agent_body, *extra):
        repo = make_repo(root)
        workspace_root = Path(root) / "workspace"
        agent = Path(root) / "agent.py"
        agent.write_text(agent_body)
        env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
        result = subprocess.run(
            [*LOOP_CMD, "--repo", str(repo), "--name", name,
             "--agent-cmd", shlex.join([sys.executable, str(agent)]),
             "--validate-cmd", "true", "--agent-backoff-max", "0",
             "--max-rounds", "1", *extra],
            capture_output=True, text=True, env=env,
        )
        state = json.loads((workspace_root / name / "state.json").read_text())
        history = (workspace_root / name / "history.log").read_text()
        return result, state, history

    def test_successful_round_records_duration(self):
        with tempfile.TemporaryDirectory() as d:
            result, state, history = self._run(
                d, "round-duration", "import time\ntime.sleep(0.02)\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertGreater(state["last_round_seconds"], 0)
            self.assertFalse(state["last_round_timed_out"])
            self.assertIsNone(state["round_started_at"])
            self.assertIsNone(state["round_deadline_at"])
            self.assertIsNone(state["round_interrupted_at"])
            self.assertRegex(history, r" secs=\d+\.\d{3} timeout=False ")
            self.assertIn("done_missing=True", history, "Plan 結束但沒有 create-plan/plan-ok 應記為異常")

    def test_timed_out_round_records_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            result, state, history = self._run(
                d, "round-timeout", "import time\ntime.sleep(1)\n", "--round-timeout", "0.001")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertGreater(state["last_round_seconds"], 0)
            self.assertTrue(state["last_round_timed_out"])
            self.assertIsNone(state["round_started_at"])
            self.assertIsNone(state["round_deadline_at"])
            self.assertIsNone(state["round_interrupted_at"])
            self.assertIn("timeout=True", history)

    def test_exec_progress_without_done_is_anomaly(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            plan = root / "plan.json"
            plan.write_text('[{"order": 1, "task": "implement feature", "track": "main"}]')
            agent = root / "progress_agent.py"
            agent.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "sys.stdin.read()\n"
                "print('anomaly-log-marker', flush=True)\n"
                "Path('feature.txt').write_text('implemented')\n"
            )
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            result = subprocess.run(
                [*LOOP_CMD, "--repo", str(repo), "--name", "normal-progress",
                 "--agent-cmd", shlex.join([sys.executable, str(agent)]),
                 "--validate-cmd", "true", "--import-plan", str(plan),
                 "--start-phase", "exec", "--max-rounds", "1"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            history = (workspace_root / "normal-progress" / "history.log").read_text()
            self.assertIn("changed=True signal=-", history)
            self.assertIn("done_missing=True", history)
            anomaly_dir = workspace_root / "normal-progress" / "logs" / "anomalies"
            metadata_files = list(anomaly_dir.glob("*.json"))
            log_files = list(anomaly_dir.glob("*.log"))
            self.assertEqual(len(metadata_files), 1)
            self.assertEqual(len(log_files), 1)
            metadata = json.loads(metadata_files[0].read_text())
            self.assertEqual(metadata["round"], 1)
            self.assertEqual(metadata["phase"], "exec")
            self.assertEqual(metadata["task"], "task-1")
            self.assertIn("anomaly-log-marker", log_files[0].read_text())

    def test_anomaly_log_retention_is_capped_and_tail_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d) / "workspace"
            logs = workspace / "logs"
            logs.mkdir(parents=True)
            old_max_bytes = L.ANOMALY_LOG_MAX_BYTES
            try:
                L.ANOMALY_LOG_MAX_BYTES = 8
                for index in range(L.ANOMALY_LOG_MAX_COUNT + 2):
                    current = logs / f"round-{index:04d}.log"
                    current.write_text(f"prefix-{index:03d}-tail")
                    L.preserve_anomaly_log(
                        workspace, current, round_number=index, phase="exec",
                        task="task-1", timestamp=(datetime(2026, 7, 10) + timedelta(seconds=index)).isoformat(),
                    )
            finally:
                L.ANOMALY_LOG_MAX_BYTES = old_max_bytes
            anomaly_dir = logs / "anomalies"
            metadata_files = sorted(anomaly_dir.glob("*.json"))
            log_files = sorted(anomaly_dir.glob("*.log"))
            self.assertEqual(len(metadata_files), L.ANOMALY_LOG_MAX_COUNT)
            self.assertEqual(len(log_files), L.ANOMALY_LOG_MAX_COUNT)
            latest = json.loads(metadata_files[-1].read_text())
            self.assertTrue(latest["truncated"])
            self.assertEqual(latest["retained_size"], 8)
            self.assertTrue((anomaly_dir / latest["log_file"]).read_text().endswith("101-tail"))

    def test_anomaly_retention_ignores_unrelated_json_and_log_files(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d) / "workspace"
            logs = workspace / "logs"
            logs.mkdir(parents=True)
            anomaly_dir = logs / "anomalies"
            anomaly_dir.mkdir()
            unrelated_json = anomaly_dir / "zzzz.json"
            unrelated_log = anomaly_dir / "notes.log"
            unrelated_json.write_text("{}", encoding="utf-8")
            unrelated_log.write_text("do not manage", encoding="utf-8")
            old_max = L.ANOMALY_LOG_MAX_COUNT
            try:
                L.ANOMALY_LOG_MAX_COUNT = 2
                for index in range(3):
                    round_log = logs / f"round-{index:04d}.log"
                    round_log.write_text(f"round {index}", encoding="utf-8")
                    L.preserve_anomaly_log(
                        workspace, round_log, round_number=index, phase="exec",
                        task="task-1", timestamp=f"2026-07-10T10:00:0{index}",
                    )
            finally:
                L.ANOMALY_LOG_MAX_COUNT = old_max
            managed_json = [
                path for path in anomaly_dir.glob("*.json")
                if L.ANOMALY_ID_RE.fullmatch(path.stem)
            ]
            managed_logs = [
                path for path in anomaly_dir.glob("*.log")
                if L.ANOMALY_ID_RE.fullmatch(path.stem)
            ]
            self.assertEqual(len(managed_json), 2)
            self.assertEqual(len(managed_logs), 2)
            self.assertEqual(unrelated_json.read_text(encoding="utf-8"), "{}")
            self.assertEqual(unrelated_log.read_text(encoding="utf-8"), "do not manage")


class TestLiveRoundTiming(unittest.TestCase):
    """進行中 round 可觀測；正常輪末清除，立即停止則保留凍結的中斷上下文。"""

    def test_status_timing_projection_handles_deadline_and_invalid_timestamp(self):
        started = datetime.now().replace(microsecond=0)
        projection = S.round_timing_projection(
            started.isoformat(),
            (started + timedelta(seconds=90)).isoformat(),
            (started + timedelta(seconds=30)).isoformat(),
            now=started + timedelta(seconds=60),
        )
        self.assertEqual(projection["round_elapsed_seconds"], 30)
        self.assertEqual(projection["round_remaining_seconds"], 60)
        self.assertEqual(S.round_timing_projection("not-a-time"), {
            "round_elapsed_seconds": None, "round_remaining_seconds": None})

    def test_interrupt_preserves_started_deadline_and_frozen_elapsed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            state_path = workspace_root / "live-round" / "state.json"
            agent = root / "slow_agent.py"
            agent.write_text("import time\ntime.sleep(10)\n")
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            process = subprocess.Popen(
                [*LOOP_CMD, "--repo", str(repo), "--name", "live-round",
                 "--agent-cmd", shlex.join([sys.executable, str(agent)]),
                 "--validate-cmd", "true", "--round-timeout", "1",
                 "--agent-backoff-max", "0", "--max-rounds", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
            )
            try:
                deadline = time.monotonic() + 5
                observed = None
                while time.monotonic() < deadline:
                    try:
                        candidate = json.loads(state_path.read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        time.sleep(0.02)
                        continue
                    if candidate.get("round_started_at") and candidate.get("loop", {}).get("pid"):
                        observed = candidate
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(observed, "Agent spawn 前必須先公開 round 計時")
                started_at = datetime.fromisoformat(observed["round_started_at"])
                deadline_at = datetime.fromisoformat(observed["round_deadline_at"])
                self.assertGreaterEqual((deadline_at - started_at).total_seconds(), 59)

                live = subprocess.run(
                    [*STATUS_CMD, "--name", "live-round", "--json"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
                live_projection = json.loads(live.stdout)
                self.assertTrue(live_projection["round_active"])
                self.assertFalse(live_projection["round_interrupted"])
                self.assertGreaterEqual(live_projection["round_elapsed_seconds"], 0)
                self.assertGreater(live_projection["round_remaining_seconds"], 0)

                process.send_signal(signal.SIGINT)
                output, _ = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130, output)
                stopped = json.loads(state_path.read_text())
                self.assertEqual(stopped["round_started_at"], observed["round_started_at"])
                self.assertEqual(stopped["round_deadline_at"], observed["round_deadline_at"])
                self.assertIsNotNone(stopped["round_interrupted_at"])
                self.assertIsNone(stopped["loop"]["pid"])
                history_path = workspace_root / "live-round" / "history.log"
                self.assertFalse(history_path.exists(), "人工中斷輪不得寫入已結束輪次或異常統計")
                interrupted_metrics = L.read_round_metrics(history_path, 100)
                self.assertEqual(interrupted_metrics["sample_count"], 0)
                self.assertEqual(interrupted_metrics["missing_done_count"], 0)
                self.assertFalse((workspace_root / "live-round" / "logs" / "anomalies").exists())

                stopped_status = subprocess.run(
                    [*STATUS_CMD, "--name", "live-round"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(stopped_status.returncode, 0, stopped_status.stdout + stopped_status.stderr)
                self.assertIn("round 1 中斷", stopped_status.stdout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


class TestRoundMetrics(unittest.TestCase):
    """history metrics 只聚合有效近期樣本，並維持 bounded/safe read。"""

    HISTORY = (
        "2026-07-10T10:00:00 round=1 phase=plan task=- changed=False\n"
        "2026-07-10T10:01:00 round=1 phase=plan task=- secs=1.000 timeout=False changed=False signal=ok\n"
        "2026-07-10T10:02:00 round=9 phase=exec task=- secs=nan timeout=True changed=False signal=-\n"
        "2026-07-10T10:03:00 round=2 phase=exec task=task-1 secs=4.000 timeout=False changed=True signal=- done_missing=True agent_ok=True tamper=False validate=PASS\n"
        "2026-07-10T10:04:00 round=3 phase=exec task=task-1 secs=2.000 timeout=False changed=False signal=done\n"
        "2026-07-10T10:05:00 round=4 phase=exec task=task-1 secs=8.000 timeout=True changed=False signal=- done_missing=True\n"
    )

    class ResponseCapture:
        response = None
        _ws_dir = D.Handler._ws_dir

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_recent_metrics_skip_old_and_invalid_rows(self):
        metrics = L.round_metrics_from_history(self.HISTORY, 3)
        self.assertEqual([sample["round"] for sample in metrics["samples"]], [2, 3, 4])
        self.assertEqual(metrics["sample_count"], 3)
        self.assertEqual(metrics["average_seconds"], 4.667)
        self.assertEqual(metrics["p50_seconds"], 4)
        self.assertEqual(metrics["p95_seconds"], 8)
        self.assertEqual(metrics["slowest_round"], 4)
        self.assertEqual(metrics["timeout_count"], 1)
        self.assertEqual(metrics["timeout_rate_pct"], 33.3)
        self.assertEqual([sample["missing_done"] for sample in metrics["samples"]], [True, False, True])
        self.assertEqual(metrics["missing_done_count"], 2)
        self.assertEqual(metrics["missing_done_rate_pct"], 66.7)
        including_plan = L.round_metrics_from_history(self.HISTORY, 5)
        self.assertEqual(including_plan["sample_count"], 4)
        self.assertEqual(including_plan["missing_done_count"], 2, "plan-ok 不得算未回 DONE")
        self.assertEqual(including_plan["missing_done_rate_pct"], 50)

    def test_reader_is_bounded_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            history = root / "history.log"
            history.write_text("x" * 256 + "\n" + self.HISTORY)
            old_scan = L.ROUND_METRICS_SCAN_BYTES
            try:
                L.ROUND_METRICS_SCAN_BYTES = 220
                metrics = L.read_round_metrics(history, 2)
            finally:
                L.ROUND_METRICS_SCAN_BYTES = old_scan
            self.assertTrue(metrics["history_truncated"])
            self.assertEqual([sample["round"] for sample in metrics["samples"]], [3, 4])
            outside = root / "outside.log"
            outside.write_text(self.HISTORY)
            history.unlink()
            history.symlink_to(outside)
            with self.assertRaises(ValueError):
                L.read_round_metrics(history, 10)

    def test_dashboard_endpoint_supports_runs_and_rejects_unsafe_input(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workspace = root / "metrics"
            workspace.mkdir()
            (workspace / "history.log").write_text(self.HISTORY)
            (workspace / "history.log.1").write_text(self.HISTORY.splitlines()[1] + "\n")
            old_root = D.ROOT
            D.ROOT = root
            try:
                current = self.ResponseCapture()
                current.path = "/api/round-metrics?ws=metrics&limit=3"
                D.Handler.do_GET(current)
                self.assertEqual(current.response[0], 200)
                self.assertEqual(current.response[1]["run"], "current")
                self.assertEqual(current.response[1]["sample_count"], 3)

                previous = self.ResponseCapture()
                previous.path = "/api/round-metrics?ws=metrics&run=previous&limit=10"
                D.Handler.do_GET(previous)
                self.assertEqual(previous.response[0], 200)
                self.assertEqual(previous.response[1]["run"], "previous")
                self.assertEqual(previous.response[1]["sample_count"], 1)
                self.assertEqual(previous.response[1]["missing_done_count"], 0)
                self.assertEqual(previous.response[1]["missing_done_rate_pct"], 0)

                for path in (
                    "/api/round-metrics?ws=metrics&limit=0",
                    "/api/round-metrics?ws=metrics&limit=bad",
                    "/api/round-metrics?ws=metrics&run=ancient",
                ):
                    invalid = self.ResponseCapture()
                    invalid.path = path
                    D.Handler.do_GET(invalid)
                    with self.subTest(path=path):
                        self.assertEqual(invalid.response[0], 400)

                outside = root / "outside.log"
                outside.write_text(self.HISTORY)
                (workspace / "history.log").unlink()
                (workspace / "history.log").symlink_to(outside)
                unsafe = self.ResponseCapture()
                unsafe.path = "/api/round-metrics?ws=metrics"
                D.Handler.do_GET(unsafe)
                self.assertEqual(unsafe.response[0], 400)
                self.assertIn("symbolic link", unsafe.response[1]["error"])
            finally:
                D.ROOT = old_root


class TestAbnormalRoundVoided(unittest.TestCase):
    """Agent crash 前打出的共識訊號不得被採信。"""

    def test_plan_ok_then_nonzero_exit_does_not_advance_consensus(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            plan = root / "plan.json"
            plan.write_text('[{"order": 1, "task": "only task", "track": "main"}]')
            agent = root / "agent.py"
            agent.write_text(
                "import os, subprocess, sys\n"
                "sys.stdin.read()\n"
                "subprocess.run([sys.executable, '-m', 'engine.work', 'plan-ok'], env=dict(os.environ))\n"
                "raise SystemExit(7)\n"
            )
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}

            result = subprocess.run(
                [*LOOP_CMD, "--repo", str(repo), "--name", "abnormal-vote",
                 "--agent-cmd", shlex.join([sys.executable, str(agent)]),
                 "--validate-cmd", "true", "--import-plan", str(plan),
                 "--start-phase", "plan", "--max-rounds", "1"],
                capture_output=True, text=True, env=env,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads((workspace_root / "abnormal-vote" / "state.json").read_text())
            self.assertEqual(state["flag"], 0)
            self.assertIn("coordinator 訊號已全部作廢", result.stdout)


class TestPauseAfterPlan(unittest.TestCase):
    """規劃收斂後暫停:loop 停在執行期起點,人工按「▶ 運行」才開始執行輪。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_fleet_resume_command_uses_all_frozen_runtime_settings(self):
        command = D.fleet_resume_command("parallel", {"integration_worktree": "/repo", "config": {
            "repo": "/repo", "goal": "goal.md", "agent_cmd": "agent", "validate_cmd": "validate",
            "max_parallel": 7, "merge_threshold": 4, "done_threshold": 5, "flag_threshold": 6,
            "red_limit": 31, "stall_limit": 401, "round_timeout": 9, "validate_timeout": 88,
            "agent_backoff_max": 17, "max_child_restarts": 3, "pause_after_plan": True,
            "notify_cmd": "notify", "plan_doc": "plan.md",
        }})
        pairs = dict(zip(command, command[1:]))
        self.assertEqual(pairs["--max-parallel"], "7")
        self.assertEqual(pairs["--red-limit"], "31")
        self.assertEqual(pairs["--stall-limit"], "401")
        self.assertEqual(pairs["--agent-backoff-max"], "17")
        self.assertEqual(pairs["--max-child-restarts"], "3")
        self.assertIn("--pause-after-plan", command)
        self.assertEqual(pairs["--notify-cmd"], "notify")
        self.assertEqual(pairs["--plan-doc"], "plan.md")

    def test_loop_pauses_at_exec_start_and_resumes_into_exec(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            plan = root / "plan.json"
            plan.write_text('[{"order": 1, "task": "only task", "track": "main"}]')
            agent = root / "agent.py"
            agent.write_text(
                "import os, subprocess, sys\n"
                "sys.stdin.read()\n"
                "subprocess.run([sys.executable, '-m', 'engine.work', 'plan-ok'], env=dict(os.environ))\n"
            )
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}

            converged = subprocess.run(
                [*LOOP_CMD, "--repo", str(repo), "--name", "pause-after-plan",
                 "--agent-cmd", shlex.join([sys.executable, str(agent)]),
                 "--validate-cmd", "true", "--import-plan", str(plan),
                 "--start-phase", "plan", "--flag-threshold", "1",
                 "--pause-after-plan", "--max-rounds", "10"],
                capture_output=True, text=True, env=env,
            )

            self.assertEqual(converged.returncode, 0, converged.stdout + converged.stderr)
            state_path = workspace_root / "pause-after-plan" / "state.json"
            state = json.loads(state_path.read_text())
            self.assertEqual(state["phase"], "exec", "收斂後 state 應停在執行期起點")
            self.assertEqual(state["current_order"], 1)
            self.assertEqual(state["round"], 2, "收斂輪之後不得再啟動執行輪")
            self.assertIsNone(state["loop"]["pid"])
            self.assertTrue(state["config"]["pause_after_plan"])
            self.assertIn("規劃已收斂", converged.stdout)
            self.assertIn("依「規劃後暫停」設定停止", converged.stdout)

            # ▶ 運行等價:同 workspace 續跑(api_run 會再帶同一 flag),必須直接進執行輪,不得再次暫停。
            resumed = subprocess.run(
                [*LOOP_CMD, "--repo", str(repo), "--name", "pause-after-plan",
                 "--agent-cmd", "true", "--validate-cmd", "true",
                 "--pause-after-plan", "--max-rounds", "3"],
                capture_output=True, text=True, env=env,
            )

            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            state = json.loads(state_path.read_text())
            self.assertEqual(state["phase"], "exec")
            self.assertEqual(state["round"], 3, "續跑必須實際執行執行輪")
            self.assertIn("階段：執行期", resumed.stdout)
            self.assertNotIn("依「規劃後暫停」設定停止", resumed.stdout)

    def test_dashboard_launch_run_and_edit_config_propagate_pause_flag(self):
        self.assertFalse(D.DEFAULT_CONFIG["defaults"]["pause_after_plan"])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            config = {
                "agent_cmds": [{"label": "true", "cmd": "true"}],
                "validate_cmds": [{"label": "true", "cmd": "true"}],
                "extra_path_dirs": [], "notify_cmd": "",
                "defaults": {"pause_after_plan": True},
            }
            captured = {}

            def fake_spawn(name, *args, **kwargs):
                captured.clear()
                captured.update(kwargs, name=name)

                class FakePopen:
                    pid = 4321
                return FakePopen()

            old_values = D.ROOT, L.WORKSPACE_ROOT, D.load_config, D.spawn_loop
            D.ROOT, L.WORKSPACE_ROOT = workspace_root, workspace_root
            D.load_config = lambda: config
            D.spawn_loop = fake_spawn
            try:
                # launch:表單值優先於團隊 defaults
                handler = self.ResponseCapture()
                D.Handler.api_launch(handler, {
                    "repo": str(repo), "name": "pause-flag", "agent_idx": 0,
                    "validate_idx": 0, "pause_after_plan": False,
                })
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertFalse(captured["pause_after_plan"])
                # launch:表單未指定時落回團隊 defaults
                ws = L.Workspace("pause-flag")
                state = ws.fresh_state()
                ws.save_state(state)
                handler = self.ResponseCapture()
                D.Handler.api_launch(handler, {
                    "repo": str(repo), "name": "pause-flag", "agent_idx": 0,
                    "validate_idx": 0,
                    "workspace_generation": state["workspace_generation"],
                })
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertTrue(captured["pause_after_plan"])

                # run:從 state.config 帶回同一開關
                state = ws.fresh_state()
                state["config"] = {
                    "repo": str(repo), "agent_cmd": "true", "validate_cmd": "true",
                    "pause_after_plan": True,
                }
                ws.save_state(state)
                handler = self.ResponseCapture()
                D.Handler.api_run(handler, {
                    "name": "pause-flag", "workspace_generation": state["workspace_generation"]})
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertTrue(captured["pause_after_plan"])

                # edit-config:停止狀態可切換,下一次運行生效
                handler = self.ResponseCapture()
                D.Handler.api_edit_config(handler, {
                    "name": "pause-flag", "workspace_generation": state["workspace_generation"],
                    "pause_after_plan": False,
                })
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertIn("pause_after_plan=off", handler.response[1]["changed"])
                saved = json.loads(ws.state_path.read_text(encoding="utf-8"))
                self.assertFalse(saved["config"]["pause_after_plan"])
                handler = self.ResponseCapture()
                D.Handler.api_run(handler, {
                    "name": "pause-flag", "workspace_generation": state["workspace_generation"]})
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertFalse(captured["pause_after_plan"])
            finally:
                D.JOBS.pop("pause-flag", None)
                D.ROOT, L.WORKSPACE_ROOT, D.load_config, D.spawn_loop = old_values

    def test_launch_null_generation_rejects_fleet_and_legacy_before_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            goal = repo / "goal.md"
            goal.write_text("original\n", encoding="utf-8")
            git(repo, "add", "goal.md")
            git(repo, "commit", "-m", "goal")
            before_head = git(repo, "rev-parse", "HEAD").stdout.strip()
            config = {
                "agent_cmds": [{"label": "true", "cmd": "true"}],
                "validate_cmds": [{"label": "true", "cmd": "true"}],
                "extra_path_dirs": [], "notify_cmd": "", "defaults": {},
            }
            old_values = D.ROOT, L.WORKSPACE_ROOT, D.load_config
            D.ROOT, L.WORKSPACE_ROOT = workspace_root, workspace_root
            D.load_config = lambda: config
            try:
                fixtures = {
                    "occupied-fleet": L.Workspace.__new__(L.Workspace).fresh_state(
                        "fleet-parent", "a" * 32),
                    "occupied-legacy": {"phase": "done", "loop": {"pid": None}},
                }
                for name, state in fixtures.items():
                    with self.subTest(name=name):
                        entry = workspace_root / name
                        entry.mkdir()
                        state_bytes = json.dumps(state, sort_keys=True).encode()
                        (entry / "state.json").write_bytes(state_bytes)
                        handler = self.ResponseCapture()
                        with mock.patch.object(D, "spawn_loop") as spawn:
                            D.Handler.api_launch(handler, {
                                "repo": str(repo), "name": name, "agent_idx": 0,
                                "validate_idx": 0, "workspace_generation": None,
                                "new_branch": True, "goal_content": "replacement\n",
                                "plan_json": json.dumps([
                                    {"order": 1, "task": "do work", "track": "main"}]),
                            })
                        self.assertEqual(handler.response[0], 409, handler.response)
                        spawn.assert_not_called()
                        self.assertEqual((entry / "state.json").read_bytes(), state_bytes)
                        self.assertEqual(goal.read_text(encoding="utf-8"), "original\n")
                        self.assertEqual(git(repo, "rev-parse", "HEAD").stdout.strip(), before_head)
                        branches = git(repo, "branch", "--format=%(refname:short)").stdout
                        self.assertNotIn(f"loop/{name}", branches)
                        self.assertNotIn(name, D.JOBS)
            finally:
                D.ROOT, L.WORKSPACE_ROOT, D.load_config = old_values


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
                [*LOOP_CMD, "--repo", str(repo), "--name", "flaky-backoff",
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
                [*LOOP_CMD, "--repo", str(repo), "--name", "visible-backoff",
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

    def test_invalid_round_telemetry_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                for index, invalid_fields in enumerate((
                    {"last_round_seconds": -0.1},
                    {"last_round_seconds": True},
                    {"last_round_timed_out": 1},
                )):
                    ws = L.Workspace(f"schema-telemetry-{index}")
                    invalid = {"phase": "plan", **invalid_fields}
                    ws.state_path.write_text(json.dumps(invalid), encoding="utf-8")
                    ws.checkpoint_path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.subTest(fields=invalid_fields), self.assertRaises(L.StateLoadError):
                        ws.load_state()
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_invalid_round_lifecycle_timestamp_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                for index, field in enumerate(("round_started_at", "round_deadline_at", "round_interrupted_at")):
                    ws = L.Workspace(f"schema-round-time-{index}")
                    invalid = {"phase": "plan", field: 123}
                    ws.state_path.write_text(json.dumps(invalid), encoding="utf-8")
                    ws.checkpoint_path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.subTest(field=field), self.assertRaises(L.StateLoadError):
                        ws.load_state()
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_invalid_issue_acknowledgement_watermark_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                ws = L.Workspace("schema-issue-ack")
                invalid = {"phase": "plan", "issues_acknowledged_round": False}
                ws.state_path.write_text(json.dumps(invalid), encoding="utf-8")
                ws.checkpoint_path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(L.StateLoadError):
                    ws.load_state()
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_invalid_goal_history_hash_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                ws = L.Workspace("schema-goal-hash")
                invalid = {"phase": "plan", "goal_previous_hash": "not-a-sha256"}
                ws.state_path.write_text(json.dumps(invalid), encoding="utf-8")
                ws.checkpoint_path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(L.StateLoadError):
                    ws.load_state()
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_invalid_phase_events_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                invalid_events = (
                    {"bad": True},
                    [{"phase": "exec", "merge_stage": "unknown", "round": 1,
                      "at": "2026-07-13T00:00:00+08:00"}],
                    [{"phase": "exec", "merge_stage": None, "round": False,
                      "at": "2026-07-13T00:00:00+08:00"}],
                    [{"phase": "exec", "merge_stage": None, "round": 1, "at": ""}],
                    [{"phase": "exec", "merge_stage": None, "round": 1,
                      "at": "2026-07-13T00:00:00+08:00"}] * 501,
                )
                for index, events in enumerate(invalid_events):
                    ws = L.Workspace(f"schema-phase-events-{index}")
                    invalid = ws.fresh_state()
                    invalid["phase_events"] = events
                    data = json.dumps(invalid)
                    ws.state_path.write_text(data, encoding="utf-8")
                    ws.checkpoint_path.write_text(data, encoding="utf-8")
                    with self.subTest(index=index), self.assertRaises(L.StateLoadError):
                        ws.load_state()
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_invalid_plan_and_completed_entries_fail_closed(self):
        invalid_states = [
            {"phase": "plan", "plan": [{"order": 0, "task": "bad", "track": "main"}]},
            {"phase": "plan", "plan": [
                {"order": 1, "task": "one", "track": "main"},
                {"order": 1, "task": "duplicate", "track": "main"},
            ]},
            {"phase": "plan", "plan": [{"order": 1, "task": "bad ref", "ref": 3, "track": "main"}]},
            {"phase": "exec", "completed": [None]},
            {"phase": "exec", "completed": [{"order": 1, "round": 1}]},
            {"phase": "exec", "completed": [{
                "order": 1, "sha": "a" * 40, "round": "1",
            }]},
            {"phase": "exec", "completed": [
                {"order": 1, "sha": "a" * 40, "round": 1},
                {"order": 1, "sha": "b" * 40, "round": 2},
            ]},
        ]
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                for index, invalid in enumerate(invalid_states):
                    ws = L.Workspace(f"schema-plan-completed-{index}")
                    ws.state_path.write_text(json.dumps(invalid), encoding="utf-8")
                    ws.checkpoint_path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.subTest(state=invalid), self.assertRaises(L.StateLoadError):
                        ws.load_state()
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_invalid_nested_runtime_collections_fail_closed(self):
        invalid_states = [
            {"phase": "plan", "notes": [None]},
            {"phase": "plan", "issues": ["legacy string"]},
            {"phase": "plan", "issues": [{"round": True, "text": "bad"}]},
            {"phase": "plan", "issues": [{"round": 1, "text": ""}]},
            {"phase": "exec", "task_reset_counts": {"1": "2"}},
            {"phase": "exec", "task_reset_counts": {"not-an-order": 2}},
        ]
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d)
                for index, invalid in enumerate(invalid_states):
                    ws = L.Workspace(f"schema-runtime-collection-{index}")
                    ws.state_path.write_text(json.dumps(invalid), encoding="utf-8")
                    ws.checkpoint_path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.subTest(state=invalid), self.assertRaises(L.StateLoadError):
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
        return subprocess.run([*WORK_CMD, command, *args],
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
                state.update(round=4, plan=[{"order": 1, "task": "one", "track": "main"}], current_order=1,
                             last_round_seconds=1.25, last_round_timed_out=False)
                ws.save_state(state)
                ws.state_path.write_text("{broken", encoding="utf-8")
                before = ws.state_path.read_bytes()
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                result = subprocess.run(
                    [*STATUS_CMD, "--name", "cli-status", "--json"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["schema_version"], 1)
                self.assertEqual(payload["round"], 4)
                self.assertEqual(payload["plan_len"], 1)
                self.assertEqual(payload["current_task"], "one")
                self.assertEqual(payload["last_round_seconds"], 1.25)
                self.assertFalse(payload["last_round_timed_out"])
                self.assertTrue(payload["state_recovery_pending"])
                self.assertEqual(ws.state_path.read_bytes(), before, "status CLI 不得修復 primary state")
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_fleet_parent_uses_fleet_truth_and_summary_excludes_child_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d) / "workspace"
                run_id = "a" * 32
                session_id = "b" * 32
                parent = L.Workspace("parallel")
                parent_state = parent.fresh_state()
                parent_state.update(workspace_kind="fleet-parent", fleet_run_id=run_id,
                                    plan=[{"order": 1, "task": "placeholder", "track": "backend"}])
                parent.save_state(parent_state)
                fleet = {
                    "schema_version": 1, "workspace_kind": "fleet-parent", "run_id": run_id,
                    "phase": "merging", "loop": {"pid": None, "session_id": session_id},
                    "plan": [{"order": 1, "task": "backend", "track": "backend"},
                             {"order": 2, "task": "final", "track": "@final"}],
                    "tracks": [{"name": "backend", "status": "cleaned",
                                "child_workspace": "parallel--backend"},
                               {"name": "@final", "status": "pending"}],
                }
                (parent.dir / "fleet.json").write_text(json.dumps(fleet), encoding="utf-8")
                child = L.Workspace("parallel--backend")
                child_state = child.fresh_state()
                child_state.update(workspace_kind="fleet-child", fleet_run_id=run_id,
                                   fleet_parent="parallel", track="backend",
                                   fleet_parent_session_id=session_id,
                                   merge_target_ref="refs/heads/main",
                                   issues=[{"round": 1, "text": "child finding",
                                            "resolved": False}],
                                   plan=[{"order": 1, "task": "backend", "track": "backend"}])
                child.save_state(child_state)
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                result = subprocess.run([*STATUS_CMD, "--all", "--json"], capture_output=True,
                                        text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                projected_parent = next(item for item in payload["workspaces"] if item["name"] == "parallel")
                self.assertEqual(projected_parent["phase"], "merging")
                self.assertEqual(projected_parent["completed"], 1)
                self.assertEqual(projected_parent["plan_len"], 2)
                self.assertEqual(projected_parent["parallel_tracks"][0]["status"], "cleaned")
                self.assertEqual(payload["summary"]["workspace_count"], 2)
                self.assertEqual(payload["summary"]["tasks_total"], 2)
                self.assertEqual(payload["summary"]["tasks_completed"], 1)
                self.assertEqual(payload["summary"]["executing"], 1)
                self.assertEqual(payload["summary"]["error_count"], 0)
                self.assertEqual(payload["summary"]["issues"], 1)
                self.assertEqual(payload["summary"]["attention"], 1)
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_missing_workspace_returns_machine_readable_error(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(Path(d) / "workspace")}
            result = subprocess.run(
                [*STATUS_CMD, "--name", "missing", "--json"],
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
                    [*STATUS_CMD, "--name", "watch-status", "--json",
                     "--watch", "--interval", "0.01"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                captured = []
                try:
                    # 等 CLI 真正進入 watch loop 並輸出兩筆，再驗證 Ctrl-C；固定 sleep
                    # 可能在較慢機器的 Python import 階段就送 SIGINT，造成與產品無關的 -2。
                    deadline = time.monotonic() + 2
                    while len(captured) < 2 and time.monotonic() < deadline:
                        ready, _, _ = select.select([process.stdout], [], [], 0.1)
                        if ready:
                            line = process.stdout.readline()
                            if line:
                                captured.append(line)
                    process.send_signal(signal.SIGINT)
                    output, error = process.communicate(timeout=2)
                    output = "".join(captured) + output
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
                    [*STATUS_CMD, "--name", "watch-change", "--json",
                     "--watch", "--on-change", "--interval", "0.01"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                captured = []
                try:
                    deadline = time.monotonic() + 2
                    while len(captured) < 1 and time.monotonic() < deadline:
                        ready, _, _ = select.select([process.stdout], [], [], 0.1)
                        if ready:
                            line = process.stdout.readline()
                            if line:
                                captured.append(line)
                    state["round"] = 1
                    ws.save_state(state)
                    deadline = time.monotonic() + 2
                    while len(captured) < 2 and time.monotonic() < deadline:
                        ready, _, _ = select.select([process.stdout], [], [], 0.1)
                        if ready:
                            line = process.stdout.readline()
                            if line:
                                captured.append(line)
                    process.send_signal(signal.SIGINT)
                    output, error = process.communicate(timeout=2)
                    output = "".join(captured) + output
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
                [*STATUS_CMD, "--name", "missing", "--on-change"],
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
                state.update(agent_failure_streak=1, last_round_seconds=60.1,
                             last_round_timed_out=True, state_recovery_count=2, goal_changed=True,
                             loop={"pid": 99999999, "session_id": "stale", "started_at": "2026-07-10T20:00:00"})
                ws.save_state(state)
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                result = subprocess.run(
                    [*STATUS_CMD, "--all", "--json", "--check"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["summary"]["attention"], 1)
                self.assertEqual(payload["summary"]["error_count"], 0)
                self.assertEqual(payload["summary"]["agent_failures"], 1)
                self.assertEqual(payload["summary"]["round_timeouts"], 1)
                self.assertEqual(payload["summary"]["state_recoveries"], 2)
                self.assertEqual(payload["summary"]["goal_changes"], 1)
                projection = payload["workspaces"][0]
                self.assertEqual(projection["agent_failure_streak"], 1)
                self.assertEqual(projection["last_round_seconds"], 60.1)
                self.assertTrue(projection["last_round_timed_out"])
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
                [*STATUS_CMD, "--name", "missing", "--check", "--watch"],
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("不可搭配 --watch", result.stderr)

    def test_acknowledged_issues_stay_in_projection_without_failing_check(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d) / "workspace"
                ws = L.Workspace("ack-status")
                state = ws.fresh_state()
                state.update(round=3, issues=[{"round": 2, "text": "kept for audit"}],
                             issues_acknowledged_round=3)
                ws.save_state(state)
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                result = subprocess.run(
                    [*STATUS_CMD, "--all", "--json", "--check"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["summary"]["issues"], 1)
                self.assertEqual(payload["summary"]["unread_issues"], 0)
                self.assertEqual(payload["summary"]["attention"], 0)
                self.assertEqual(payload["workspaces"][0]["issues"], 1)
                self.assertEqual(payload["workspaces"][0]["unread_issues"], 0)

                state["round"] = 4
                state["issues"].append({"round": 4, "text": "new issue"})
                ws.save_state(state)
                result = subprocess.run(
                    [*STATUS_CMD, "--all", "--json", "--check"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["summary"]["unread_issues"], 1)
                self.assertEqual(payload["summary"]["attention"], 1)
            finally:
                L.WORKSPACE_ROOT = old_root

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
                    [*STATUS_CMD, "--all", "--json", "--sort", "attention"],
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
                             last_round_seconds=60.1, last_round_timed_out=True,
                             state_recovery_count=1, goal_changed=True)
                ws.save_state(state)
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                result = subprocess.run(
                    [*STATUS_CMD, "--name", "human-status"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Agent 異常 2", result.stdout)
                self.assertIn("最近一輪 60.1 秒（逾時）", result.stdout)
                self.assertIn("state 復原 1", result.stdout)
                self.assertIn("goal 已變更", result.stdout)
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_metrics_option_projects_recent_history_for_json_and_humans(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d) / "workspace"
                ws = L.Workspace("metrics-status")
                ws.save_state(ws.fresh_state())
                ws.history.write_text(
                    "2026-07-10T10:00:00 round=1 phase=exec task=- secs=1.000 timeout=False\n"
                    "2026-07-10T10:01:00 round=2 phase=exec task=- secs=3.000 timeout=True\n"
                    "2026-07-10T10:02:00 round=3 phase=exec task=- secs=2.000 timeout=False\n")
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}
                result = subprocess.run(
                    [*STATUS_CMD, "--name", "metrics-status", "--metrics", "2", "--json"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                metrics = json.loads(result.stdout)["round_metrics"]
                self.assertEqual(metrics["sample_count"], 2)
                self.assertEqual([sample["round"] for sample in metrics["samples"]], [2, 3])
                self.assertEqual(metrics["average_seconds"], 2.5)
                self.assertEqual(metrics["timeout_rate_pct"], 50)

                human = subprocess.run(
                    [*STATUS_CMD, "--name", "metrics-status", "--metrics", "2"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(human.returncode, 0, human.stdout + human.stderr)
                self.assertIn("效能 2 輪", human.stdout)
                self.assertIn("P95 3", human.stdout)
                self.assertIn("逾時 1（50%）", human.stdout)
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_metrics_option_rejects_out_of_range_values_before_reading_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(Path(d) / "workspace")}
            for value in ("-1", str(L.ROUND_METRICS_MAX_SAMPLES + 1), "bad"):
                result = subprocess.run(
                    [*STATUS_CMD, "--name", "missing", "--metrics", value],
                    capture_output=True, text=True, env=env)
                with self.subTest(value=value):
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("--metrics", result.stderr)

    def test_sort_requires_all_mode(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(Path(d) / "workspace")}
            result = subprocess.run(
                [*STATUS_CMD, "--name", "missing", "--sort", "round"],
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("只有搭配 --all", result.stderr)

    def test_filter_modes_and_cli_keep_check_scoped_to_full_fleet(self):
        sample = [
            {"name": "alert", "running": False, "phase": "exec", "stall_rounds": 1},
            {"name": "running", "running": True, "phase": "exec"},
            {"name": "done", "running": False, "phase": "done", "stall_rounds": 2,
             "red_streak": 1, "last_round_timed_out": True, "state_recovery_count": 1},
            {"name": "broken", "error": "bad state"},
        ]
        self.assertEqual({item["name"] for item in S.filter_status_results(sample, "attention")},
                         {"alert", "broken"})
        self.assertEqual([item["name"] for item in S.filter_status_results(sample, "running")], ["running"])
        self.assertEqual({item["name"] for item in S.filter_status_results(sample, "stopped")},
                         {"alert", "done"})
        self.assertEqual([item["name"] for item in S.filter_status_results(sample, "done")], ["done"])
        self.assertEqual([item["name"] for item in S.filter_status_results(sample, "error")], ["broken"])

        with tempfile.TemporaryDirectory() as d:
            old_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = Path(d) / "workspace"
                done = L.Workspace("done")
                done_state = done.fresh_state()
                done_state.update(phase="done", stall_rounds=2, red_streak=1,
                                  last_round_timed_out=True, state_recovery_count=1)
                done.save_state(done_state)
                alert = L.Workspace("alert")
                alert_state = alert.fresh_state()
                alert_state["stall_rounds"] = 1
                alert.save_state(alert_state)
                broken = L.Workspace("broken")
                broken.state_path.write_text("{broken", encoding="utf-8")
                env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(L.WORKSPACE_ROOT)}

                result = subprocess.run(
                    [*STATUS_CMD, "--all", "--json", "--filter", "done", "--check"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["filter"], "done")
                self.assertEqual(payload["matched_count"], 1)
                self.assertEqual([item["name"] for item in payload["workspaces"]], ["done"])
                self.assertEqual(payload["summary"]["workspace_count"], 3)
                self.assertEqual(payload["summary"]["attention"], 1)
                self.assertEqual(payload["summary"]["error_count"], 1)

                result = subprocess.run(
                    [*STATUS_CMD, "--all", "--json", "--filter", "attention"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["matched_count"], 2)
                self.assertEqual([item["name"] for item in payload["workspaces"]], ["alert", "broken"])
            finally:
                L.WORKSPACE_ROOT = old_root

    def test_filter_requires_all_mode(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(Path(d) / "workspace")}
            result = subprocess.run(
                [*STATUS_CMD, "--name", "missing", "--filter", "attention"],
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--filter 只有搭配 --all", result.stderr)

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
                    [*STATUS_CMD, "--all", "--json"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["schema_version"], 1)
                self.assertNotIn("filter", payload)
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
                    "unread_issues": 0,
                    "agent_failures": 0,
                    "round_timeouts": 0,
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
                outside.write_text(json.dumps([{"order": 1, "task": "outside", "track": "main"}]), encoding="utf-8")
                token = "a" * 32
                (ws.dir / f"pending_plan.{token}.json").symlink_to(outside)
                (ws.dir / f"signal_plan_ok.{token}").symlink_to(outside)
                self.assertIsNone(ws.take_pending_plan(token))
                self.assertFalse(ws.signal("signal_plan_ok", token))
                self.assertEqual(outside.read_text(encoding="utf-8"),
                                 json.dumps([{"order": 1, "task": "outside", "track": "main"}]))
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
                [*LOOP_CMD, "--repo", str(repo), "--name", "preflight-console",
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
            previous["plan"] = [{"order": 1, "task": "must survive failed reset", "track": "main"}]
            previous["plan_version"] = 9
            state_path = workspace / "state.json"
            state_path.write_text(json.dumps(previous))
            before = state_path.read_bytes()
            checkpoint_path = workspace / "state.last-good.json"
            checkpoint_path.write_bytes(before)
            checkpoint_before = checkpoint_path.read_bytes()
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}

            result = subprocess.run(
                [*LOOP_CMD, "--repo", str(repo), "--name", "reset-safe",
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
            previous["plan"] = [{"order": 1, "task": "must be cleared", "track": "main"}]
            previous["plan_version"] = 9
            (workspace / "state.json").write_text(json.dumps(previous))
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}

            result = subprocess.run(
                [*LOOP_CMD, "--repo", str(repo), "--name", "reset-success",
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
                [*LOOP_CMD, "--repo", str(repo), "--name", "validate-timeout",
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
            previous["plan"] = [{"order": 1, "task": "old plan survives", "track": "main"}]
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
                    "workspace_generation": previous["workspace_generation"],
                    "plan_json": '[{"order":1,"task":"new imported plan","track":"main"}]', "start_phase": "plan",
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

    def test_parent_ui_projection_aggregates_child_issues_with_navigation_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "a" * 32
            session_id = "1" * 32
            parent, child = root / "parent", root / "parent--backend"
            parent.mkdir(); child.mkdir()
            parent_state = L.Workspace.__new__(L.Workspace).fresh_state("fleet-parent", run_id)
            child_state = L.Workspace.__new__(L.Workspace).fresh_state("fleet-child", run_id)
            child_state.update(fleet_parent="parent", track="backend", merge_target_ref="refs/heads/main",
                               fleet_parent_session_id=session_id,
                               issues=[{"round": 3, "where": "engine/a.py", "text": "repair me"}])
            (parent / "state.json").write_text(json.dumps(parent_state), encoding="utf-8")
            (child / "state.json").write_text(json.dumps(child_state), encoding="utf-8")
            fleet = {"schema_version": 1, "workspace_kind": "fleet-parent", "run_id": run_id,
                     "phase": "exec", "loop": {"pid": None, "session_id": session_id},
                     "tracks": [{"name": "backend", "child_workspace": child.name,
                                                    "status": "repairing"}]}
            (parent / "fleet.json").write_text(json.dumps(fleet), encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            try:
                projected, error = D.project_state_for_ui("parent")
                self.assertIsNone(error)
                self.assertEqual(projected["parallel_run"]["phase"], "exec")
                self.assertEqual(projected["issues"][0]["track"], "backend")
                self.assertEqual(projected["issues"][0]["child_workspace"], child.name)
                summaries = D.list_workspaces()
                child_summary = next(item for item in summaries if item["name"] == child.name)
                self.assertEqual(child_summary["fleet_parent"], "parent")
                self.assertEqual(child_summary["track"], "backend")
            finally:
                D.ROOT = old_root

    def test_replacement_child_with_wrong_run_does_not_pollute_parent_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, child = root / "parent", root / "parent--backend"
            parent.mkdir(); child.mkdir()
            run_id = "a" * 32
            parent_state = L.Workspace.__new__(L.Workspace).fresh_state("fleet-parent", run_id)
            replacement = L.Workspace.__new__(L.Workspace).fresh_state("fleet-child", "b" * 32)
            replacement.update(fleet_parent="parent", track="backend",
                               issues=[{"round": 1, "text": "replacement issue"}])
            (parent / "state.json").write_text(json.dumps(parent_state), encoding="utf-8")
            (child / "state.json").write_text(json.dumps(replacement), encoding="utf-8")
            fleet = {"schema_version": 1, "workspace_kind": "fleet-parent", "run_id": run_id,
                     "phase": "exec", "loop": {"pid": None},
                     "tracks": [{"name": "backend", "child_workspace": child.name,
                                 "status": "repairing"}]}
            (parent / "fleet.json").write_text(json.dumps(fleet), encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            try:
                projected, error = D.project_state_for_ui("parent")
                self.assertIsNone(error)
                self.assertEqual(projected["issues"], [])
                parent_summary = next(item for item in D.list_workspaces()
                                      if item["name"] == "parent")
                self.assertEqual(parent_summary["issues"], 0)
            finally:
                D.ROOT = old_root

    def test_child_from_previous_supervisor_session_does_not_pollute_parent_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, child = root / "parent", root / "parent--backend"
            parent.mkdir(); child.mkdir()
            run_id = "a" * 32
            parent_state = L.Workspace.__new__(L.Workspace).fresh_state("fleet-parent", run_id)
            stale_child = L.Workspace.__new__(L.Workspace).fresh_state("fleet-child", run_id)
            stale_child.update(
                fleet_parent="parent", track="backend",
                fleet_parent_session_id="1" * 32,
                issues=[{"round": 1, "text": "previous-session issue"}],
            )
            (parent / "state.json").write_text(json.dumps(parent_state), encoding="utf-8")
            (child / "state.json").write_text(json.dumps(stale_child), encoding="utf-8")
            fleet = {
                "schema_version": 1, "workspace_kind": "fleet-parent", "run_id": run_id,
                "phase": "exec", "loop": {"pid": None, "session_id": "2" * 32},
                "tracks": [{"name": "backend", "child_workspace": child.name,
                            "status": "repairing"}],
            }
            (parent / "fleet.json").write_text(json.dumps(fleet), encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            try:
                projected, error = D.project_state_for_ui("parent")
                self.assertIsNone(error)
                self.assertEqual(projected["issues"], [])
            finally:
                D.ROOT = old_root


class TestFleetHealthProjection(unittest.TestCase):
    """fleet health 是唯讀、可供 API/SSE/UI 共用的聚合 projection。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_health_aggregates_attention_and_state_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            healthy = root / "healthy"
            healthy.mkdir()
            healthy_state = L.Workspace.__new__(L.Workspace).fresh_state()
            healthy_state.update(phase="done", stall_rounds=2, red_streak=1)
            (healthy / "state.json").write_text(json.dumps(healthy_state), encoding="utf-8")
            attention = root / "attention"
            attention.mkdir()
            attention_state = L.Workspace.__new__(L.Workspace).fresh_state()
            attention_state.update({
                "phase": "exec", "red_streak": 2,
                "issues": [{"round": 1, "text": "a"}, {"round": 2, "text": "b"}],
                "agent_failure_streak": 3, "state_recovery_count": 4,
                "last_round_seconds": 60.2, "last_round_timed_out": True,
                "state_recovery_pending": True, "goal_changed": True,
                "loop": {"pid": 99999999, "session_id": "stale", "started_at": "2026-07-10T20:00:00"},
            })
            (attention / "state.json").write_text(json.dumps(attention_state), encoding="utf-8")
            broken = root / "broken"
            broken.mkdir()
            (broken / "state.json").write_text("{broken", encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            try:
                projection = D.fleet_health_projection()
                self.assertEqual(projection["schema_version"], 1)
                self.assertEqual(projection["status"], "error")
                self.assertEqual(projection["workspace_count"], 3)
                self.assertEqual(projection["error_count"], 1)
                self.assertEqual(projection["attention"], 2)
                self.assertEqual(projection["issues"], 2)
                self.assertEqual(projection["unread_issues"], 2)
                self.assertEqual(projection["agent_failures"], 3)
                self.assertEqual(projection["round_timeouts"], 1)
                self.assertEqual(projection["state_recoveries"], 4)
                self.assertEqual(projection["goal_changes"], 1)
                self.assertEqual(projection["stale_loop_pids"], 1)

                handler = self.ResponseCapture()
                handler.path = "/api/health"
                D.Handler.do_GET(handler)
                self.assertEqual(handler.response[0], 200)
                self.assertEqual(handler.response[1]["schema_version"], 1)
                self.assertEqual(handler.response[1]["status"], "error")

                strict = self.ResponseCapture()
                strict.path = "/api/health?strict=1"
                D.Handler.do_GET(strict)
                self.assertEqual(strict.response[0], 503)
                self.assertEqual(strict.response[1]["status"], "error")

                invalid = self.ResponseCapture()
                invalid.path = "/api/health?strict=maybe"
                D.Handler.do_GET(invalid)
                self.assertEqual(invalid.response[0], 400)
                self.assertIn("0 或 1", invalid.response[1]["error"])
            finally:
                D.ROOT = old_root

    def test_empty_fleet_is_healthy(self):
        projection = D.fleet_health_projection([])
        self.assertEqual(projection["status"], "ok")
        self.assertEqual(projection["workspace_count"], 0)
        self.assertEqual(projection["attention"], 0)

    def test_parent_group_deduplicates_child_but_orphan_stays_visible(self):
        items = [
            {"name": "parent", "workspace_kind": "fleet-parent", "phase": "exec",
             "fleet_run_id": "a" * 32,
             "parallel_phase": "merging",
             "parallel_tracks": [{"name": "backend", "status": "repairing",
                                   "child_workspace": "parent--backend"}],
             "issues": 2, "unread_issues": 2},
            {"name": "parent--backend", "workspace_kind": "fleet-child", "fleet_parent": "parent",
             "fleet_run_id": "a" * 32, "track": "backend",
             "phase": "exec", "issues": 2, "unread_issues": 2},
            {"name": "standalone", "workspace_kind": "standalone", "phase": "done",
             "issues": 0, "unread_issues": 0},
            {"name": "orphan--ui", "workspace_kind": "fleet-child", "fleet_parent": "missing",
             "phase": "exec", "issues": 1, "unread_issues": 1},
        ]
        projection = D.fleet_health_projection(items)
        self.assertEqual(projection["workspace_count"], 3)
        self.assertEqual(projection["issues"], 3)
        self.assertEqual(projection["attention"], 2)

    def test_mismatched_run_child_remains_visible_in_health(self):
        items = [
            {"name": "parent", "workspace_kind": "fleet-parent",
             "fleet_run_id": "a" * 32, "phase": "exec", "issues": 0},
            {"name": "parent--old", "workspace_kind": "fleet-child",
             "fleet_parent": "parent", "fleet_run_id": "b" * 32,
             "phase": "exec", "running": True, "error": "replacement mismatch",
             "issues": 1, "unread_issues": 1},
        ]
        projection = D.fleet_health_projection(items)
        self.assertEqual(projection["workspace_count"], 2)
        self.assertEqual(projection["running"], 1)
        self.assertEqual(projection["error_count"], 1)
        self.assertEqual(projection["issues"], 1)


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

    def test_incremental_projection_reports_actual_tail_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.log"
            path.write_text("中文輪次\n", encoding="utf-8")
            self.assertFalse(D.read_incremental(path, -1)["truncated"],
                             "UTF-8 byte size 不等於 JS 字數時也不得誤報裁切")
            path.write_text("old-line-1234567890\nnew-line-1234567890\n", encoding="utf-8")
            old_tail = D.TAIL_INIT
            try:
                D.TAIL_INIT = 24
                projection = D.read_incremental(path, -1)
            finally:
                D.TAIL_INIT = old_tail
            self.assertTrue(projection["truncated"])
            self.assertIn("new-line", projection["data"])


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

    def test_ingested_issue_is_next_agent_context_without_becoming_a_human_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            ws = L.Workspace.__new__(L.Workspace)
            ws.dir = Path(directory)
            ws.history = ws.dir / "history.log"
            token = "next-agent-context"
            pending = ws.dir / f"pending_issues.{token}"
            pending.write_text("L4-DR2-ISSUE-SENTINEL\n", encoding="utf-8")
            ws.pending_issues = lambda _token: pending
            state = {"issues": [], "notes": []}
            L.ingest_pending_issues(ws, state, token, 3, "task-1", "exec")
            self.assertEqual(state["issues"][0]["text"], "L4-DR2-ISSUE-SENTINEL")
            self.assertIn("下一輪 context，不是預設人工 gate", state["notes"][0])


class TestIssueAcknowledgement(unittest.TestCase):
    """標記已讀只更新 round watermark；issue 稽核資料保留，新 round 仍會重新告警。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_ack_preserves_records_and_only_suppresses_old_issue_attention(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            (root / "demo").mkdir(parents=True)
            state = L.Workspace.__new__(L.Workspace).fresh_state()
            state.update(round=4, issues=[
                {"round": 2, "where": "task-1", "text": "old", "ts": "2026-07-10T10:00:00"},
                {"round": 4, "where": "task-2", "text": "current", "ts": "2026-07-10T11:00:00"},
            ])
            old_roots = D.ROOT, L.WORKSPACE_ROOT
            D.ROOT = L.WORKSPACE_ROOT = root
            try:
                D.write_state("demo", state)
                before = D.list_workspaces()[0]
                self.assertEqual(before["issues"], 2)
                self.assertEqual(before["unread_issues"], 2)
                self.assertEqual(D.fleet_health_projection()["status"], "degraded")

                handler = self.ResponseCapture()
                D.Handler.api_edit_state(handler, {
                    "name": "demo", "workspace_generation": state["workspace_generation"],
                    "ack_issues": True})
                self.assertEqual(handler.response[0], 200, handler.response)
                saved, error = D.read_state("demo", repair=False)
                self.assertIsNone(error)
                self.assertEqual(len(saved["issues"]), 2, "標記已讀不得刪除 issue")
                self.assertEqual(saved["issues_acknowledged_round"], 4)
                self.assertEqual(L.unread_issue_count(saved), 0)
                after = D.list_workspaces()[0]
                self.assertEqual(after["issues"], 2)
                self.assertEqual(after["unread_issues"], 0)
                health = D.fleet_health_projection()
                self.assertEqual(health["status"], "ok")
                self.assertEqual(health["issues"], 2)
                self.assertEqual(health["unread_issues"], 0)

                saved["round"] = 5
                saved["issues"].append({"round": 5, "where": "task-2", "text": "new"})
                D.write_state("demo", saved)
                latest = D.list_workspaces()[0]
                self.assertEqual(latest["unread_issues"], 1)
                self.assertEqual(D.fleet_health_projection()["status"], "degraded")

                clear = self.ResponseCapture()
                D.Handler.api_edit_state(clear, {
                    "name": "demo", "workspace_generation": state["workspace_generation"],
                    "clear_issues": True})
                self.assertEqual(clear.response[0], 200, clear.response)
                cleared, error = D.read_state("demo", repair=False)
                self.assertIsNone(error)
                self.assertEqual(cleared["issues"], [])
                self.assertEqual(cleared["issues_acknowledged_round"], -1)
            finally:
                D.ROOT, L.WORKSPACE_ROOT = old_roots


class TestPendingPlanEditing(unittest.TestCase):
    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        (self.root / "demo").mkdir(parents=True)
        self.old_roots = D.ROOT, L.WORKSPACE_ROOT
        D.ROOT = L.WORKSPACE_ROOT = self.root
        state = L.Workspace.__new__(L.Workspace).fresh_state()
        state.update(
            phase="exec", plan_version=7, current_order=2,
            plan=[{"order": order, "task": f"task {order}", "ref": None, "track": "main"}
                  for order in range(1, 5)],
            completed=[{"order": 1, "sha": "a" * 40, "round": 2}],
            task_reset_counts={"1": 1, "3": 4},
        )
        self.generation = state["workspace_generation"]
        D.write_state("demo", state)

    def tearDown(self):
        D.ROOT, L.WORKSPACE_ROOT = self.old_roots
        self.temp.cleanup()

    def call(self, tasks, version=7):
        handler = self.ResponseCapture()
        D.Handler.api_edit_state(handler, {
            "name": "demo", "workspace_generation": self.generation,
            "plan_edit": True, "plan_version": version, "tasks": tasks,
        })
        return handler.response

    def test_standalone_edit_rejects_external_writer_lock_without_state_change(self):
        before = (self.root / "demo" / "state.json").read_bytes()
        handler = self.ResponseCapture()
        with D.locked_workspace_entry("demo", (".run.lock",)):
            D.Handler.api_edit_state(handler, {
                "name": "demo", "workspace_generation": self.generation,
                "done_count": 9,
            })
        self.assertEqual(handler.response[0], 409, handler.response)
        self.assertEqual((self.root / "demo" / "state.json").read_bytes(), before)

    def test_reorders_deletes_and_inserts_only_after_current_task(self):
        response = self.call([
            {"order": 1, "task": "task 1", "ref": None, "track": "main"},
            {"order": 2, "task": "task 2", "ref": None, "track": "main"},
            {"order": 4, "task": "task 4 moved", "ref": None, "track": "main"},
            {"order": None, "task": "inserted task", "ref": "spec.md", "track": "main"},
        ])
        self.assertEqual(response[0], 200, response)
        saved, error = D.read_state("demo", repair=False)
        self.assertIsNone(error)
        self.assertEqual(saved["plan_version"], 8)
        self.assertEqual([task["order"] for task in saved["plan"]], [1, 2, 3, 4])
        self.assertEqual([task["task"] for task in saved["plan"]],
                         ["task 1", "task 2", "task 4 moved", "inserted task"])
        self.assertEqual(saved["plan"][3]["ref"], "spec.md")
        self.assertEqual(saved["completed"][0]["order"], 1)
        self.assertEqual(saved["current_order"], 2)
        self.assertEqual(saved["task_reset_counts"], {"1": 1})

    def test_rejects_locked_task_move_and_stale_plan_version(self):
        moved = self.call([
            {"order": 2, "task": "task 2", "ref": None, "track": "main"},
            {"order": 1, "task": "task 1", "ref": None, "track": "main"},
            {"order": 3, "task": "task 3", "ref": None, "track": "main"},
            {"order": 4, "task": "task 4", "ref": None, "track": "main"},
        ])
        self.assertEqual(moved[0], 400)
        self.assertIn("不可移動", moved[1]["error"])
        stale = self.call([
            {"order": order, "task": f"task {order}", "ref": None, "track": "main"}
            for order in range(1, 5)
        ], version=6)
        self.assertEqual(stale[0], 409)
        self.assertIn("請重新載入", stale[1]["error"])

        modified = self.call([
            {"order": 1, "task": "改寫已完成任務", "ref": None, "track": "main"},
            {"order": 2, "task": "task 2", "ref": None, "track": "main"},
            {"order": 3, "task": "task 3", "ref": None, "track": "main"},
            {"order": 4, "task": "task 4", "ref": None, "track": "main"},
        ])
        self.assertEqual(modified[0], 400)
        self.assertIn("不可修改內容", modified[1]["error"])

    def test_awaiting_approval_parent_edits_fleet_truth_and_rehashes(self):
        run_id = "b" * 32
        parent = self.root / "parallel"
        parent.mkdir()
        plan = [
            {"order": 1, "task": "backend old", "ref": None, "track": "backend"},
            {"order": 2, "task": "frontend old", "ref": None, "track": "frontend"},
        ]
        state = L.Workspace.__new__(L.Workspace).fresh_state("fleet-parent", run_id)
        state.update(phase="exec", plan=plan, plan_version=4, current_order=1)
        D.write_state("parallel", state)
        fleet = {"schema_version": 1, "workspace_kind": "fleet-parent", "run_id": run_id,
                 "phase": "awaiting-approval", "plan": plan, "plan_generation": 4,
                 "plan_sha256": "old", "tracks": [], "order_map": {}, "loop": {"pid": None}}
        (parent / "fleet.json").write_text(json.dumps(fleet), encoding="utf-8")
        handler = self.ResponseCapture()
        tasks = [
            {"order": 2, "task": "frontend first", "ref": None, "track": "frontend"},
            {"order": 1, "task": "backend second", "ref": None, "track": "backend",
             "scope": ["engine/**"]},
            {"order": None, "task": "final checks", "ref": None, "track": "@final"},
        ]
        D.Handler.api_edit_state(handler, {"name": "parallel", "run_id": run_id, "plan_edit": True,
                                           "plan_version": 4, "tasks": tasks})
        self.assertEqual(handler.response[0], 200, handler.response)
        updated = json.loads((parent / "fleet.json").read_text(encoding="utf-8"))
        checkpoint = json.loads((parent / "fleet.last-good.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint, updated)
        self.assertEqual(updated["plan_generation"], 5)
        self.assertEqual([task["order"] for task in updated["plan"]], [1, 2, 3])
        self.assertEqual(updated["plan"][0]["task"], "frontend first")
        raw = json.dumps(updated["plan"], ensure_ascii=False, separators=(",", ":")).encode()
        self.assertEqual(updated["plan_sha256"], __import__("hashlib").sha256(raw).hexdigest())
        projected, error = D.project_state_for_ui("parallel")
        self.assertIsNone(error)
        self.assertEqual(projected["plan_version"], 5)
        self.assertEqual(projected["plan"], updated["plan"])

        (parent / "fleet.json").write_text("{broken", encoding="utf-8")
        recovered = D.read_parallel_run("parallel")
        self.assertTrue(recovered["fleet_recovery_pending"])
        self.assertEqual(recovered["plan"], updated["plan"])

        stale = self.ResponseCapture()
        D.Handler.api_edit_state(stale, {"name": "parallel", "run_id": run_id, "plan_edit": True,
                                         "plan_version": 4, "tasks": tasks})
        self.assertEqual(stale.response[0], 409)
        self.assertIn("請重新載入", stale.response[1]["error"])

    def test_stopped_parent_config_updates_fleet_truth_with_run_guard(self):
        run_id = "d" * 32
        parent = self.root / "parallel-config"
        parent.mkdir()
        config = {"repo": "/repo", "agent_cmd": "agent", "validate_cmd": "true",
                  "max_parallel": 4, "max_child_restarts": 0, "round_timeout": 30}
        state = L.Workspace.__new__(L.Workspace).fresh_state("fleet-parent", run_id)
        state["config"] = config
        D.write_state("parallel-config", state)
        fleet = {"schema_version": 1, "workspace_kind": "fleet-parent", "run_id": run_id,
                 "phase": "stopped", "resume_phase": "exec", "plan": [], "tracks": [],
                 "config": config, "loop": {"pid": None}}
        (parent / "fleet.json").write_text(json.dumps(fleet), encoding="utf-8")
        old_load = D.load_config
        D.load_config = lambda: {"agent_cmds": [{"label": "agent", "cmd": "agent"}],
                                 "defaults": {}, "extra_path_dirs": []}
        try:
            stale = self.ResponseCapture()
            D.Handler.api_edit_config(stale, {"name": "parallel-config", "run_id": "e" * 32,
                                              "max_parallel": 6})
            self.assertEqual(stale.response[0], 409)
            handler = self.ResponseCapture()
            D.Handler.api_edit_config(handler, {"name": "parallel-config", "run_id": run_id,
                                                "max_parallel": 6, "max_child_restarts": 2,
                                                "round_timeout": 8})
            self.assertEqual(handler.response[0], 200, handler.response)
            saved = json.loads((parent / "fleet.json").read_text(encoding="utf-8"))["config"]
            checkpoint = json.loads(
                (parent / "fleet.last-good.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["config"], saved)
            self.assertEqual(saved["max_parallel"], 6)
            self.assertEqual(saved["max_child_restarts"], 2)
            self.assertEqual(saved["round_timeout"], 8)
            projected, error = D.project_state_for_ui("parallel-config")
            self.assertIsNone(error)
            self.assertEqual(projected["config"], saved)
            (parent / "fleet.json").write_text("{broken", encoding="utf-8")
            recovered = D.read_parallel_run("parallel-config")
            self.assertTrue(recovered["fleet_recovery_pending"])
            self.assertEqual(recovered["config"], saved)
        finally:
            D.load_config = old_load

    def test_fleet_dashboard_transaction_rolls_back_each_partial_write(self):
        run_id = "f" * 32
        parent = self.root / "fleet-transaction"
        parent.mkdir()
        old_state = L.Workspace.__new__(L.Workspace).fresh_state("fleet-parent", run_id)
        old_state.update(phase="exec", plan=[], plan_version=0,
                         config={"repo": "/repo", "agent_cmd": "agent",
                                 "validate_cmd": "true"})
        D.write_state("fleet-transaction", old_state)
        old_fleet = {
            "schema_version": 1, "workspace_kind": "fleet-parent", "run_id": run_id,
            "phase": "stopped", "resume_phase": "exec", "plan": [],
            "plan_generation": 0, "plan_sha256": None, "dashboard_revision": 0,
            "tracks": [], "merge_queue": [], "config": old_state["config"],
            "loop": {"pid": None},
        }
        old_fleet_data = json.dumps(old_fleet, ensure_ascii=False, indent=2).encode()
        for filename in ("fleet.json", "fleet.last-good.json"):
            (parent / filename).write_bytes(old_fleet_data)
        old_bytes = {path.name: path.read_bytes() for path in (
            parent / "fleet.json", parent / "fleet.last-good.json",
            parent / "state.json", parent / "state.last-good.json")}
        new_fleet = json.loads(json.dumps(old_fleet))
        new_fleet["dashboard_revision"] = 1
        new_fleet["config"]["validate_cmd"] = "false"
        new_state = json.loads(json.dumps(old_state))
        new_state["config"] = new_fleet["config"]
        new_state["fleet_truth_revision"] = 1

        for failed_stage in ("after-fleet.json", "after-fleet.last-good.json",
                             "after-state.json", "after-state.last-good.json"):
            with self.subTest(stage=failed_stage):
                for filename, data in old_bytes.items():
                    (parent / filename).write_bytes(data)

                def fail_at(stage, _name):
                    if stage == failed_stage:
                        raise RuntimeError(f"fault {stage}")

                with mock.patch.object(D, "_fleet_edit_fault_hook", side_effect=fail_at):
                    with self.assertRaisesRegex(RuntimeError, "fault"):
                        D.write_parallel_dashboard_transaction(
                            "fleet-transaction", new_fleet, new_state)
                for filename, data in old_bytes.items():
                    self.assertEqual((parent / filename).read_bytes(), data, filename)
                projected = D.read_parallel_run("fleet-transaction")
                self.assertNotIn("read_error", projected)
                self.assertEqual(projected["dashboard_revision"], 0)

    def test_fleet_dashboard_transaction_abrupt_exit_keeps_one_matching_pair(self):
        run_id = "c" * 32
        parent = self.root / "fleet-crash-transaction"
        parent.mkdir()
        old_state = L.Workspace.__new__(L.Workspace).fresh_state("fleet-parent", run_id)
        old_state.update(phase="exec", plan=[], plan_version=0,
                         config={"repo": "/repo", "agent_cmd": "agent",
                                 "validate_cmd": "true"})
        self.assertEqual(old_state["fleet_truth_revision"], 0)
        D.write_state("fleet-crash-transaction", old_state)
        old_fleet = {
            "schema_version": 1, "workspace_kind": "fleet-parent", "run_id": run_id,
            "phase": "stopped", "resume_phase": "exec", "plan": [],
            "plan_generation": 0, "plan_sha256": None, "dashboard_revision": 0,
            "tracks": [], "merge_queue": [], "config": old_state["config"],
            "loop": {"pid": None},
        }
        old_fleet_data = json.dumps(old_fleet, ensure_ascii=False, indent=2).encode()
        for filename in ("fleet.json", "fleet.last-good.json"):
            (parent / filename).write_bytes(old_fleet_data)
        baseline = {path.name: path.read_bytes() for path in (
            parent / "fleet.json", parent / "fleet.last-good.json",
            parent / "state.json", parent / "state.last-good.json")}
        script = "\n".join((
            "import json, os",
            "from pathlib import Path",
            "from engine import dashboard as D",
            "D.ROOT = Path(os.environ['TX_ROOT'])",
            "name = 'fleet-crash-transaction'",
            "parent = D.ROOT / name",
            "fleet = json.loads((parent / 'fleet.json').read_text())",
            "state = json.loads((parent / 'state.json').read_text())",
            "fleet['dashboard_revision'] = 1",
            "fleet['config']['validate_cmd'] = 'false'",
            "state['fleet_truth_revision'] = 1",
            "state['config'] = fleet['config']",
            "def crash(stage, _name):",
            "    if stage == os.environ['TX_STAGE']:",
            "        os._exit(97)",
            "D._fleet_edit_fault_hook = crash",
            "D.write_parallel_dashboard_transaction(name, fleet, state)",
        ))
        for stage, expected_revision in (
                ("after-fleet.json", 0), ("after-state.json", 1),
                ("after-fleet.last-good.json", 1),
                ("after-state.last-good.json", 1)):
            with self.subTest(stage=stage):
                for filename, data in baseline.items():
                    (parent / filename).write_bytes(data)
                env = {**os.environ, "TX_ROOT": str(self.root), "TX_STAGE": stage,
                       "PYTHONPATH": str(REPO_ROOT)}
                result = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT,
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 97, result.stdout + result.stderr)
                projected = D.read_parallel_run("fleet-crash-transaction")
                self.assertNotIn("read_error", projected)
                self.assertEqual(projected["dashboard_revision"], expected_revision)


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


class TestExternalStandaloneStopIdentity(unittest.TestCase):
    """External stop must bind state session to one exact OS process instance."""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    @staticmethod
    def state(pid=4242):
        value = L.Workspace.__new__(L.Workspace).fresh_state("standalone")
        value["config"] = {"repo": "/tmp/external-repo", "agent_cmd": "agent",
                           "validate_cmd": "true"}
        value["loop"] = {"pid": pid, "session_id": "a" * 32,
                         "started_at": "2026-07-13T12:00:00"}
        return value

    @staticmethod
    def process(*, started="Mon Jul 13 12:00:00 2026", name="external-demo",
                repo="/tmp/external-repo"):
        return {"ppid": 1, "started": started,
                "command": (f"python -m engine.loop --repo {repo} "
                            f"--name {name} --agent-cmd agent --validate-cmd true")}

    def call_stop(self, state, snapshots):
        handler = self.ResponseCapture()
        with mock.patch.object(D, "read_state", return_value=(state, None)), \
                mock.patch.object(D, "freeze_workspace_stop_identity", return_value={}), \
                mock.patch.object(D, "cleanup_frozen_runtime_group",
                                  return_value=(True, None, 200)), \
                mock.patch.object(D, "_process_snapshot", side_effect=snapshots), \
                mock.patch.object(D, "workspace_console_log"), \
                mock.patch.object(D.os, "kill") as send_signal:
            D.Handler.api_stop(handler, {
                "name": "external-demo",
                "workspace_generation": state["workspace_generation"],
                "expected_pid": state["loop"]["pid"],
            })
        return handler.response, send_signal.call_args_list

    def test_wrong_workspace_process_with_same_pid_is_not_signaled(self):
        state = self.state()
        response, signals = self.call_stop(
            state, [{4242: self.process(name="other-workspace")}])
        self.assertEqual(response[0], 409, response)
        self.assertIn("另一個程序或 workspace", response[1]["error"])
        self.assertEqual(signals, [])

    def test_pid_reuse_during_grace_never_receives_force_signal(self):
        state = self.state()
        original = {4242: self.process()}
        reused = {4242: self.process(started="Mon Jul 13 12:00:01 2026")}
        response, signals = self.call_stop(state, [original, original, reused])
        self.assertEqual(response[0], 409, response)
        self.assertIn("停止期間已被重用", response[1]["error"])
        self.assertEqual(signals, [mock.call(4242, signal.SIGINT)])

    def test_valid_external_process_gets_sigint_and_clean_success(self):
        state = self.state()
        original = {4242: self.process()}
        response, signals = self.call_stop(state, [original, original, {}])
        self.assertEqual(response[0], 200, response)
        self.assertTrue(response[1]["external"])
        self.assertEqual(signals, [mock.call(4242, signal.SIGINT)])


class TestStopRuntimeGroupCleanup(unittest.TestCase):
    @staticmethod
    def process(pid, pgid, started, command):
        return {"ppid": 1, "pgid": pgid, "sid": pgid,
                "started": started, "command": command}

    @staticmethod
    def markers(root="/tmp/root", repo="/tmp/repo", name="demo"):
        coordinator = {
            "schema_version": L.ACTIVE_RUNTIME_SCHEMA_VERSION,
            "workspace_name": name, "workspace_root": root, "repo": repo,
            "workspace_generation": "a" * 32, "session_id": "b" * 32,
            "pid": 100, "started": "owner-start", "command": (
                f"python -m engine.loop --repo {repo} --name {name}"),
        }
        active = {
            "schema_version": L.ACTIVE_RUNTIME_SCHEMA_VERSION, "kind": "agent",
            "workspace_name": name, "workspace_root": root, "repo": repo,
            "workspace_generation": "a" * 32, "session_id": "b" * 32,
            "owner_pid": 100, "owner_started": "owner-start",
            "owner_command": coordinator["command"],
            "pid": 200, "pgid": 200, "sid": 200,
            "started": "runtime-start", "command": "runtime-wrapper",
            "target_command": "agent --work",
        }
        return coordinator, active

    def test_external_stop_requires_current_root_scoped_coordinator_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            repo = Path(directory) / "repo"
            (root / "demo").mkdir(parents=True)
            repo.mkdir()
            state = L.Workspace.__new__(L.Workspace).fresh_state("standalone")
            state["config"] = {"repo": str(repo)}
            state["loop"] = {"pid": 100, "session_id": "b" * 32}
            old_root = D.ROOT
            D.ROOT = root
            try:
                with mock.patch.object(D, "_process_snapshot", return_value={}):
                    with self.assertRaisesRegex(D.RuntimeStopIdentityError, "缺少目前 workspace root"):
                        D.freeze_workspace_stop_identity(
                            "demo", repo, 100, state=state,
                            require_coordinator_marker=True)
                coordinator, _active = self.markers(
                    root=str(Path(directory) / "other-root"), repo=str(repo))
                L.atomic_write_bytes(root / "demo" / L.COORDINATOR_RUNTIME_FILE,
                                     json.dumps(coordinator).encode())
                with mock.patch.object(D, "_process_snapshot", return_value={}):
                    with self.assertRaisesRegex(D.RuntimeStopIdentityError, "root/repo/PID"):
                        D.freeze_workspace_stop_identity(
                            "demo", repo, 100, state=state,
                            require_coordinator_marker=True)
            finally:
                D.ROOT = old_root

    def test_hung_runtime_group_is_signaled_and_must_be_empty(self):
        coordinator, active = self.markers()
        frozen = {"entry": Path("/tmp/root/demo"), "coordinator": coordinator,
                  "active": active}
        original = {200: self.process(200, 200, "runtime-start", "runtime-wrapper")}
        with mock.patch.object(D, "_read_runtime_marker",
                               side_effect=[coordinator, active]), \
                mock.patch.object(D, "_process_snapshot",
                                  side_effect=[original, original, original, {}]), \
                mock.patch.object(D.os, "killpg") as killpg:
            result = D.cleanup_frozen_runtime_group(
                frozen, grace_seconds=0, force_seconds=0)
        self.assertEqual(result, (True, None, 200))
        self.assertEqual(killpg.call_args_list,
                         [mock.call(200, signal.SIGINT), mock.call(200, signal.SIGKILL)])

    def test_runtime_pgid_reuse_during_grace_is_not_force_killed(self):
        coordinator, active = self.markers()
        frozen = {"entry": Path("/tmp/root/demo"), "coordinator": coordinator,
                  "active": active}
        original = {200: self.process(200, 200, "runtime-start", "runtime-wrapper")}
        reused = {200: self.process(200, 200, "replacement-start", "runtime-wrapper")}
        with mock.patch.object(D, "_read_runtime_marker",
                               side_effect=[coordinator, active]), \
                mock.patch.object(D, "_process_snapshot",
                                  side_effect=[original, original, reused]), \
                mock.patch.object(D.os, "killpg") as killpg:
            ok, message, code = D.cleanup_frozen_runtime_group(
                frozen, grace_seconds=0, force_seconds=0)
        self.assertFalse(ok)
        self.assertEqual(code, 409)
        self.assertIn("force 前", message)
        self.assertEqual(killpg.call_args_list, [mock.call(200, signal.SIGINT)])

    def test_replacement_runtime_marker_is_preserved_without_signal(self):
        coordinator, active = self.markers()
        replacement = dict(active, session_id="c" * 32, pid=300, pgid=300, sid=300,
                           started="replacement-start")
        frozen = {"entry": Path("/tmp/root/demo"), "coordinator": coordinator,
                  "active": active}
        snapshot = {300: self.process(300, 300, "replacement-start", "runtime-wrapper")}
        with mock.patch.object(D, "_read_runtime_marker",
                               side_effect=[coordinator, replacement]), \
                mock.patch.object(D, "_process_snapshot", return_value=snapshot), \
                mock.patch.object(D.os, "killpg") as killpg:
            ok, _message, code = D.cleanup_frozen_runtime_group(frozen)
        self.assertFalse(ok)
        self.assertEqual(code, 409)
        killpg.assert_not_called()

    def test_job_stop_forces_hung_coordinator_then_verifies_runtime_cleanup(self):
        coordinator, active = self.markers()
        frozen = {"entry": Path("/tmp/root/demo"), "coordinator": coordinator,
                  "active": active}

        class HungProcess:
            pid = 100

            def __init__(self):
                self.running = True
                self.signals = []

            def poll(self):
                return None if self.running else -9

            def send_signal(self, value):
                self.signals.append(value)

            def wait(self, timeout=None):
                if timeout == 8:
                    raise subprocess.TimeoutExpired(["coordinator"], timeout)
                self.running = False
                return -9

        process = HungProcess()
        job = D.Job.__new__(D.Job)
        job.name, job.repo, job.popen, job.kind = "demo", "/tmp/repo", process, "loop"
        job.last_stop_error, job.last_stop_code = None, 500
        snapshot = {100: self.process(100, 100, "owner-start", coordinator["command"])}
        with mock.patch.object(D, "read_state", return_value=(None, "missing")), \
                mock.patch.object(D, "freeze_workspace_stop_identity", return_value=frozen), \
                mock.patch.object(D, "_process_snapshot", return_value=snapshot), \
                mock.patch.object(D.os, "getpgid", return_value=100), \
                mock.patch.object(D.os, "killpg") as killpg, \
                mock.patch.object(D, "cleanup_frozen_runtime_group",
                                  return_value=(True, None, 200)) as cleanup:
            self.assertTrue(job.stop(wait=True))
        self.assertEqual(process.signals, [signal.SIGINT])
        killpg.assert_called_once_with(100, signal.SIGKILL)
        cleanup.assert_called_once_with(frozen)

    def test_dashboard_shutdown_root_sweep_failure_propagates(self):
        server = mock.Mock()
        server.serve_forever.return_value = None
        old_jobs = D.JOBS
        D.JOBS = {}
        try:
            with mock.patch.object(D, "dashboard_instance_lease"), \
                    mock.patch.object(D, "load_config", return_value={}), \
                    mock.patch.object(D, "DashboardServer", return_value=server), \
                    mock.patch.object(D.signal, "signal"), \
                    mock.patch.object(
                        D, "stop_workspace_coordinators",
                        side_effect=[{"requested": 0, "forced": 0, "remaining": []},
                                     RuntimeError("runtime remains")]):
                with self.assertRaisesRegex(RuntimeError, "root-scoped runtime 清場失敗"):
                    D.run_dashboard(port=18765)
            server.server_close.assert_called_once()
        finally:
            D.JOBS = old_jobs

    def test_markerless_job_cleans_child_that_ignores_group_sigint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace_root = base / "workspace"
            workspace_root.mkdir()
            ready = base / "child-ready"
            child_pid_path = base / "child-pid"
            child_code = (
                "import os,signal,time\n"
                "from pathlib import Path\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()))\n"
                f"Path({str(ready)!r}).write_text('ready')\n"
                "time.sleep(60)\n"
            )
            leader_code = (
                "import signal,subprocess,sys,time\n"
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
                "signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n"
                "time.sleep(60)\n"
            )
            leader = subprocess.Popen(
                [sys.executable, "-c", leader_code], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, start_new_session=True)
            old_root = D.ROOT
            D.ROOT = workspace_root
            job = D.Job("markerless", str(base), leader)
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    if leader.poll() is not None:
                        self.fail(f"markerless leader 提前退出 rc={leader.returncode}")
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "markerless child 未進入忽略 SIGINT 狀態")
                child_pid = int(child_pid_path.read_text())
                self.assertTrue(job.stop(wait=True), job.last_stop_error)
                final = D._process_snapshot()
                self.assertIsNotNone(final)
                self.assertNotIn(child_pid, final, "Job.stop 回成功前必須清空 frozen child")
            finally:
                D.ROOT = old_root
                if leader.poll() is None:
                    try:
                        os.killpg(leader.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    leader.wait(timeout=2)

    def test_markerless_member_pid_reuse_is_preserved_without_force(self):
        leader = self.process(100, 100, "leader-start", "coordinator")
        child = self.process(101, 100, "child-start", "child")
        frozen = {"pid": 100, "pgid": 100, "sid": 100,
                  "leader": leader, "roster": {100: leader, 101: child}}
        replacement = {101: self.process(101, 100, "replacement-start", "child")}
        with mock.patch.object(D, "_process_snapshot", return_value=replacement), \
                mock.patch.object(D.os, "kill") as kill:
            ok, message, code = D.cleanup_markerless_job_group(
                frozen, force_seconds=0)
        self.assertFalse(ok)
        self.assertEqual(code, 409)
        self.assertIn("reused", message)
        kill.assert_not_called()


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
                [*LOOP_CMD, "--repo", str(repo), "--name", "graceful-stop",
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
                [*LOOP_CMD, "--repo", str(repo), "--name", "stale-stop",
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
                [*LOOP_CMD, "--repo", str(repo), "--name", "cancel-drain",
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
                D.Handler.api_drain(handler, {
                    "name": "cancel-drain", "workspace_generation": state["workspace_generation"],
                    "expected_pid": state["loop"]["pid"]})
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertTrue(handler.response[1]["requested"])
                request_path = workspace_root / "cancel-drain" / L.STOP_AFTER_ROUND_FILE
                self.assertTrue(request_path.exists())

                handler = self.ResponseCapture()
                D.Handler.api_cancel_drain(handler, {
                    "name": "cancel-drain", "workspace_generation": state["workspace_generation"],
                    "expected_pid": state["loop"]["pid"]})
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
            state = L.Workspace.__new__(L.Workspace).fresh_state()
            state.update(phase="plan", loop={"pid": 4242, "session_id": "current-session"})
            (workspace / "state.json").write_text(json.dumps(state))
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
                D.Handler.api_cancel_drain(handler, {
                    "name": "demo", "workspace_generation": state["workspace_generation"],
                    "expected_pid": 4242})
                self.assertEqual(handler.response[0], 409)
                self.assertIn("已被 loop 取走", handler.response[1]["error"])

                for method in (D.Handler.api_drain, D.Handler.api_cancel_drain):
                    stale = self.ResponseCapture()
                    method(stale, {"name": "demo",
                                   "workspace_generation": state["workspace_generation"],
                                   "expected_pid": 4241})
                    self.assertEqual(stale.response[0], 409)
                    self.assertIn("畫面過期", stale.response[1]["error"])
            finally:
                D.ROOT, D.loop_pid_alive = old_root, old_alive


class TestAgentPromptPolicy(unittest.TestCase):
    """Agent 只能做針對性取證，且一般未知不得被機械地升級成人工處理。"""

    def prompt(self, name):
        return (REPO_ROOT / "engine" / "prompts" / name).read_text(encoding="utf-8")

    def test_runtime_prompts_forbid_broad_search_and_destructive_bulk_cleanup(self):
        for name in ("plan.md", "exec.md", "merge-sync.md", "merge-confirm.md"):
            with self.subTest(name=name):
                prompt = self.prompt(name)
                self.assertIn("不開放廣域 repo 搜尋/巡檢", prompt)
        for name in ("plan.md", "exec.md"):
            with self.subTest(name=name):
                prompt = self.prompt(name)
                self.assertIn("不要用全域 `git reset --hard` 或 `git clean -fd`", prompt)

    def test_external_planner_prefers_agent_judgment_over_mechanical_gates(self):
        base = self.prompt("external-agent-base.md")
        goal = self.prompt("external-agent-goal.md")
        plan = self.prompt("external-agent-plan.md")
        self.assertIn("本版本不做全 repo 列檔、廣域巡檢", base)
        self.assertIn("一般實作細節由執行 agent", base)
        self.assertIn("不得為證明「無」擴大成全 repo 負面搜尋", goal)
        self.assertIn("不為格式完整強制建立 ID", goal)
        self.assertIn("不必為了形式硬造 grep／腳本", goal)
        self.assertIn("不因命令名稱未知就設 human gate", plan)
        self.assertIn("不要為每個不相關候選產生 N/A", plan)

    def test_runtime_planner_audits_fresh_worktree_execution_without_command_whitelist(self):
        prompt = self.prompt("plan.md")
        self.assertIn("fresh-worktree 可執行性稽核", prompt)
        self.assertIn("不得發明 repo 未安裝、未定義的", prompt)
        self.assertIn("本 task 規劃新建的交付 module/script 不需事前存在", prompt)
        self.assertIn("capability check 證實可用的 runtime/tool/env", prompt)
        self.assertIn("不得假設 integration checkout", prompt)
        self.assertIn("`node_modules` 或本機 venv", prompt)
        self.assertIn("只有 repo/環境已證實", prompt)
        self.assertIn("需要真正整合實作的 `@final`", prompt)
        self.assertIn("完成後 index/worktree 必須相對新 HEAD 乾淨", prompt)
        self.assertIn("任務內明定 validation-only", prompt)
        self.assertIn("純驗收 `@final`", prompt)
        self.assertIn("status baseline", prompt)
        self.assertIn("驗收後必須全部與該 baseline 一致", prompt)
        self.assertIn("不新增測試框架白名單、命令字串 regex 或人工 gate", prompt)

    def test_exec_issue_is_next_agent_context_not_default_human_gate(self):
        prompt = self.prompt("exec.md")
        self.assertIn("context，不等於預設等待人工", prompt)
        self.assertIn("把證據交給下一輪 agent 繼續收斂", prompt)
        self.assertNotIn("交由人類處理", prompt)

    def test_merge_confirm_delegates_pre_cas_integration_gate_but_requires_repair(self):
        prompt = self.prompt("merge-confirm.md")
        self.assertIn("每個 task 的每一條 DoD", prompt)
        self.assertIn("不得自行把一般 DoD 判成", prompt)
        self.assertIn("「不適用」而跳過", prompt)
        self.assertIn("由 parent 在 merge-ready/CAS 後負責", prompt)
        self.assertIn("首次 pre-CAS confirm 尚無 integration-only 結果是預期時序", prompt)
        self.assertIn("不得因此報 issue", prompt)
        self.assertIn("rollback 後會把該次權威錯誤放進修復情報", prompt)
        self.assertIn("上述可在 child 重現的 DoD 與", prompt)
        self.assertIn("必須直接依錯誤內容", prompt)
        self.assertIn("修復並 commit", prompt)
        self.assertNotIn("缺少 integration-only DoD 的權威結果", prompt)
        self.assertNotIn("所有適用 DoD", prompt)

    def test_all_runtime_prompts_render_without_placeholder_residue(self):
        mappings = {
            "plan.md": {
                "GOAL": "goal", "PLAN_DOC": "plan.md", "PLAN_JSON": "[]",
                "CREATE_CMD": "create", "PLANOK_CMD": "ok", "ISSUE_CMD": "issue",
                "PLAN_MODE_CONTEXT": "parallel", "NOTES": "none",
            },
            "exec.md": {
                "GOAL": "goal", "TASK_LIST": "tasks", "TASK_ID": "task-1",
                "TASK_TEXT": "task", "TRACK_CONTEXT": "track", "TASK_REF": "ref",
                "PLAN_DOC": "plan.md", "ISSUE_CMD": "issue", "VALIDATE_CMD": "validate",
                "TASK_TAG": "track/task-1", "DONE_CMD": "done", "NOTES": "none",
            },
            "merge-sync.md": {
                "TRACK_NAME": "track", "INTEGRATION_TIP": "a" * 40, "GOAL": "goal",
                "TRACK_TASKS_FULL": "[]", "VALIDATE_CMD": "validate",
                "MERGE_TARGET": "refs/heads/main", "ISSUE_CMD": "issue",
                "REPAIR_CONTEXT": "none",
            },
            "merge-confirm.md": {
                "TRACK_NAME": "track", "MERGE_TARGET": "refs/heads/main",
                "INTEGRATION_TIP": "a" * 40, "GOAL": "goal", "TRACK_TASKS_FULL": "[]",
                "ISSUE_CMD": "issue", "VALIDATE_CMD": "validate", "DONE_CMD": "done",
                "REPAIR_CONTEXT": "none",
            },
        }
        for name, mapping in mappings.items():
            with self.subTest(name=name):
                template = REPO_ROOT / "engine" / "prompts" / name
                markers = set(re.findall(r"<<([A-Z][A-Z0-9_]*)>>", template.read_text(encoding="utf-8")))
                self.assertEqual(markers, set(mapping), "placeholder 契約漂移時必須更新 renderer 與測試")
                rendered = L.build_prompt(template, mapping)
                self.assertNotRegex(rendered, r"<<[A-Z][A-Z0-9_]*>>")

    def test_prompt_renderer_does_not_reinterpret_placeholder_literal_in_user_text(self):
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "prompt.md"
            template.write_text("goal=<<GOAL>> task=<<TASK>>", encoding="utf-8")
            rendered = L.build_prompt(template, {"GOAL": "literal <<TASK>>", "TASK": "real task"})
            self.assertEqual(rendered, "goal=literal <<TASK>> task=real task")


class TestDashboardJobIdentity(unittest.TestCase):
    """Jobs 分頁停止 fleet 時必須帶當前 process 對應的 immutable run identity。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_fleet_job_projects_kind_and_run_id_only_for_matching_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            old_root = D.ROOT
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                start_new_session=True,
            )
            try:
                D.ROOT = Path(directory)
                run_id = "f" * 32
                workspace = D.ROOT / "parallel-job"
                workspace.mkdir()
                state = L.Workspace.__new__(L.Workspace).fresh_state("fleet-parent", run_id)
                D.write_state("parallel-job", state)
                fleet = {
                    "schema_version": 1, "workspace_kind": "fleet-parent", "run_id": run_id,
                    "phase": "exec",
                    "loop": {"pid": process.pid},
                }
                (workspace / "fleet.json").write_text(json.dumps(fleet), encoding="utf-8")
                job = D.Job("parallel-job", directory, process, kind="fleet")
                self.assertEqual(job.info()["kind"], "fleet")
                self.assertEqual(job.info()["run_id"], run_id)
                fleet["loop"]["pid"] = process.pid + 1
                (workspace / "fleet.json").write_text(json.dumps(fleet), encoding="utf-8")
                self.assertNotIn("run_id", job.info(), "stale job 不得取得同名新 run 的 identity")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                D.ROOT = old_root

    def test_starting_standalone_job_requires_exact_expected_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            old_root = D.ROOT
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                start_new_session=True,
            )
            try:
                D.ROOT = Path(directory)
                D.JOBS["starting-job"] = D.Job("starting-job", directory, process)
                info = D.JOBS["starting-job"].info()
                self.assertEqual(info["pid"], process.pid)
                self.assertNotIn("workspace_generation", info)
                for body in ({"name": "starting-job"},
                             {"name": "starting-job", "expected_pid": process.pid + 1}):
                    response = self.ResponseCapture()
                    D.Handler.api_stop(response, body)
                    self.assertEqual(response.response[0], 409)
                    self.assertIsNone(process.poll())
                response = self.ResponseCapture()
                D.Handler.api_stop(response, {
                    "name": "starting-job", "expected_pid": process.pid})
                self.assertEqual(response.response[0], 200, response.response)
            finally:
                D.JOBS.pop("starting-job", None)
                if process.poll() is None:
                    process.kill()
                    process.wait()
                D.ROOT = old_root


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

    def test_cli_smoke_environment_drops_round_and_fleet_identity(self):
        keys = ("LOOP_WS", "LOOP_ROUND_TOKEN", "LOOP_FLEET_RUN_ID",
                "LOOP_FLEET_TRACK", "LOOP_FLEET_CRASH_AT")
        previous = {key: os.environ.get(key) for key in keys}
        workspace_root = os.environ.get("LOOP_AGENT_WORKSPACE_ROOT")
        try:
            for key in keys:
                os.environ[key] = f"secret-{key}"
            os.environ["LOOP_AGENT_WORKSPACE_ROOT"] = "/tmp/keep-workspace-root"
            env = D.command_test_env({"extra_path_dirs": []})
            self.assertTrue(all(key not in env for key in keys))
            self.assertEqual(env["LOOP_AGENT_WORKSPACE_ROOT"], "/tmp/keep-workspace-root")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if workspace_root is None:
                os.environ.pop("LOOP_AGENT_WORKSPACE_ROOT", None)
            else:
                os.environ["LOOP_AGENT_WORKSPACE_ROOT"] = workspace_root

    def test_merge_confirm_prompt_treats_integration_only_failure_as_authoritative(self):
        prompt = (REPO_ROOT / "engine" / "prompts" / "merge-confirm.md").read_text(encoding="utf-8")
        self.assertIn("無法重現是預期行為", prompt)
        self.assertIn("不得因本地 validate PASS 就忽略", prompt)
        self.assertIn("<<ISSUE_CMD>>", prompt)

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


class TestManualTaskProgressValidation(unittest.TestCase):
    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_forward_jump_honors_validate_timeout_without_changing_progress(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            old_roots = L.WORKSPACE_ROOT, D.ROOT
            L.WORKSPACE_ROOT = workspace_root
            D.ROOT = workspace_root
            try:
                ws = L.Workspace("manual-progress")
                state = ws.fresh_state()
                state.update({
                    "phase": "exec",
                    "plan": [
                        {"order": 1, "task": "first", "ref": None, "track": "main"},
                        {"order": 2, "task": "second", "ref": None, "track": "main"},
                    ],
                    "current_order": 1,
                    "config": {
                        "repo": str(repo),
                        "validate_cmd": shlex.join([
                            sys.executable, "-c", "import time; time.sleep(1)",
                        ]),
                        "validate_timeout": 0.05,
                    },
                })
                ws.save_state(state)

                handler = self.ResponseCapture()
                started = time.monotonic()
                D.Handler.api_set_task(handler, {
                    "name": "manual-progress", "workspace_generation": state["workspace_generation"],
                    "order": 2})
                elapsed = time.monotonic() - started

                self.assertEqual(handler.response[0], 400)
                self.assertIn("validate 逾時", handler.response[1]["error"])
                self.assertLess(elapsed, 0.8, "任務跳轉不得被卡住的 validator 長時間阻塞")
                saved = json.loads(ws.state_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["current_order"], 1)
                self.assertEqual(saved["completed"], [])
            finally:
                L.WORKSPACE_ROOT, D.ROOT = old_roots


class TestDashboardStateLockCoverage(unittest.TestCase):
    """#3 run/launch 必須和 edit/phase 共用 workspace lock,不能在 stopped check 後競態。"""

    def test_all_workspace_mutations_are_decorated(self):
        for method in ("api_launch", "api_run", "api_drain", "api_cancel_drain", "api_edit_state", "api_edit_config", "api_validate", "api_preflight", "api_test_agent", "api_phase", "api_set_task", "api_delete_workspace"):
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
                common = [*LOOP_CMD, "--repo", str(repo), "--name", name,
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
subprocess.run([sys.executable, "-m", "engine.work", "create-plan"],
               input='[{{"order":1,"task":"stolen dirty task","track":"main"}}]', text=True, env=dict(os.environ))
'''
_AGENT_CLEAN = f'''import os, subprocess, sys
sys.stdin.read()
subprocess.run([sys.executable, "-m", "engine.work", "create-plan"],
               input='[{{"order":1,"task":"legit planned task","track":"main"}}]', text=True, env=dict(os.environ))
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
                [*LOOP_CMD, "--repo", str(repo), "--name", name,
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


class TestWorkspaceSafeDelete(unittest.TestCase):
    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    @staticmethod
    def legacy_state(*, pid=None):
        """Last schema-v1 standalone shape: intentionally lacks v2 identity fields."""
        return {"phase": "done", "round": 3, "flag": 0, "plan": [],
                "plan_version": 1, "current_order": 0, "done_count": 0,
                "completed": [], "loop": {"pid": pid, "session_id": None}}

    @staticmethod
    def delete_request(name):
        """Build the same generation-bound delete request emitted by the Dashboard UI."""
        summary = next(item for item in D.list_workspaces() if item["name"] == name)
        return {"name": name, "workspace_generation": summary["workspace_generation"]}

    def test_legacy_v1_is_projected_delete_only_and_can_be_safely_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            legacy = root / "legacy"
            legacy.mkdir(parents=True)
            (legacy / "state.json").write_text(
                json.dumps(self.legacy_state()), encoding="utf-8")
            (legacy / "artifact.txt").write_text("legacy\n", encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            try:
                summary = D.list_workspaces()[0]
                self.assertTrue(summary["legacy_delete_only"])
                self.assertFalse(summary["running"])

                refused_run = self.ResponseCapture()
                D.Handler.api_run(refused_run, {"name": "legacy"})
                self.assertEqual(refused_run.response[0], 400)
                self.assertTrue(legacy.exists(), "legacy workspace 不得 resume")

                deleted = self.ResponseCapture()
                D.Handler.api_delete_workspace(deleted, self.delete_request("legacy"))
                self.assertEqual(deleted.response, (
                    200, {"ok": True, "name": "legacy", "deleted": True,
                          "legacy_delete_only": True}))
                self.assertFalse(legacy.exists())
            finally:
                D.ROOT = old_root

    def test_legacy_delete_marker_survives_crash_before_journal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            legacy = root / "legacy"
            legacy.mkdir(parents=True)
            (legacy / "state.json").write_text(
                json.dumps(self.legacy_state()), encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            fired = False

            def fault(stage, name):
                nonlocal fired
                if not fired and stage == "after-delete-generation" and name == "legacy":
                    fired = True
                    raise OSError("injected marker-before-journal crash")

            try:
                first = self.ResponseCapture()
                with mock.patch.object(D, "_delete_fault_hook", side_effect=fault):
                    D.Handler.api_delete_workspace(first, self.delete_request("legacy"))
                self.assertEqual(first.response[0], 409)
                marker = legacy / D.DELETE_GENERATION_MARKER
                generation = marker.read_text(encoding="ascii").strip()
                self.assertRegex(generation, r"^[0-9a-f]{32}$")
                self.assertFalse(D._delete_journal_path("legacy").exists())

                retry = self.ResponseCapture()
                D.Handler.api_delete_workspace(retry, self.delete_request("legacy"))
                self.assertEqual(retry.response[0], 200)
                self.assertFalse(legacy.exists())
            finally:
                D.ROOT = old_root

    def test_legacy_stale_journal_preserves_same_inode_replacement_and_job(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            legacy = root / "legacy"
            legacy.mkdir(parents=True)
            (legacy / "state.json").write_text(
                json.dumps(self.legacy_state()), encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            fired = False

            def fail_clear(stage, name):
                nonlocal fired
                if not fired and stage == "before-journal-clear" and name == "legacy":
                    fired = True
                    raise OSError("injected journal clear failure")

            sentinel_job = mock.Mock()
            sentinel_job.alive.return_value = False
            try:
                first = self.ResponseCapture()
                with mock.patch.object(D, "_delete_fault_hook", side_effect=fail_clear):
                    D.Handler.api_delete_workspace(first, self.delete_request("legacy"))
                self.assertEqual(first.response[0], 409)
                self.assertFalse(legacy.exists())

                legacy.mkdir()
                replacement = L.Workspace.__new__(L.Workspace).fresh_state()
                replacement["phase"] = "done"
                (legacy / "state.json").write_text(json.dumps(replacement), encoding="utf-8")
                marker = legacy / "new-workspace.txt"
                marker.write_text("must survive\n", encoding="utf-8")
                journal_path = D._delete_journal_path("legacy")
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                self.assertEqual(journal["entries"][0]["generation_source"], "delete-marker")
                replacement_info = legacy.lstat()
                journal["entries"][0]["dev"] = replacement_info.st_dev
                journal["entries"][0]["ino"] = replacement_info.st_ino
                journal_path.write_text(json.dumps(journal), encoding="utf-8")
                with D.JOBS_LOCK:
                    D.JOBS["legacy"] = sentinel_job

                retry = self.ResponseCapture()
                D.Handler.api_delete_workspace(retry, {"name": "legacy"})
                self.assertEqual(retry.response[0], 409)
                self.assertTrue(retry.response[1]["replacement_preserved"])
                self.assertEqual(marker.read_text(encoding="utf-8"), "must survive\n")
                with D.JOBS_LOCK:
                    self.assertIs(D.JOBS.get("legacy"), sentinel_job)

                confirmed = self.ResponseCapture()
                D.Handler.api_delete_workspace(confirmed, self.delete_request("legacy"))
                self.assertEqual(confirmed.response[0], 200)
                self.assertFalse(legacy.exists())
            finally:
                with D.JOBS_LOCK:
                    D.JOBS.pop("legacy", None)
                D.ROOT = old_root

    def test_legacy_live_pid_and_corrupt_v2_are_not_delete_only(self):
        for case in ("live-legacy", "corrupt-v2"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "workspace"
                workspace = root / "demo"
                workspace.mkdir(parents=True)
                state = (self.legacy_state(pid=4242) if case == "live-legacy" else {
                    "state_schema_version": L.STATE_SCHEMA_VERSION,
                    "workspace_generation": "not-valid",
                    "workspace_kind": "standalone", "fleet_run_id": None,
                    "phase": "done", "loop": {"pid": None},
                })
                (workspace / "state.json").write_text(json.dumps(state), encoding="utf-8")
                old_root = D.ROOT
                D.ROOT = root
                try:
                    with mock.patch.object(
                            D, "loop_pid_alive", side_effect=lambda pid: pid == 4242):
                        summary = D.list_workspaces()[0]
                        response = self.ResponseCapture()
                        request = ({"name": "demo", "workspace_generation": summary["workspace_generation"]}
                                   if case == "live-legacy" else {"name": "demo"})
                        D.Handler.api_delete_workspace(response, request)
                    self.assertTrue(workspace.exists())
                    self.assertFalse((workspace / D.DELETE_GENERATION_MARKER).exists())
                    if case == "live-legacy":
                        self.assertTrue(summary["legacy_delete_only"])
                        self.assertTrue(summary["running"])
                        self.assertEqual(response.response[0], 409)
                    else:
                        self.assertNotIn("legacy_delete_only", summary)
                        self.assertEqual(response.response[0], 400)
                finally:
                    D.ROOT = old_root

    def test_corrupt_v2_primary_cannot_downgrade_via_legacy_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            workspace = root / "demo"
            workspace.mkdir(parents=True)
            (workspace / "state.json").write_bytes(
                b'{"state_schema_version":2,"workspace_generation":')
            (workspace / "state.last-good.json").write_text(
                json.dumps(self.legacy_state()), encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            try:
                summary = D.list_workspaces()[0]
                self.assertNotIn("legacy_delete_only", summary)
                response = self.ResponseCapture()
                D.Handler.api_delete_workspace(response, {"name": "demo"})
                self.assertEqual(response.response[0], 400)
                self.assertTrue(workspace.exists())
                self.assertFalse((workspace / D.DELETE_GENERATION_MARKER).exists())
            finally:
                D.ROOT = old_root

    def test_legacy_delete_only_classification_bounds_state_reads(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            workspace = root / "demo"
            workspace.mkdir(parents=True)
            (workspace / "state.json").write_text(
                json.dumps(self.legacy_state()), encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            try:
                with mock.patch.object(D, "LEGACY_STATE_MAX_BYTES", 32):
                    self.assertIsNone(D.legacy_workspace_state_for_delete("demo"))
                    summary = D.list_workspaces()[0]
                self.assertNotIn("legacy_delete_only", summary)
                self.assertTrue(workspace.exists())
            finally:
                D.ROOT = old_root

    def test_arbitrary_or_ambiguous_json_is_not_legacy_delete_only(self):
        ambiguous_v2 = self.legacy_state()
        ambiguous_v2["workspace_kind"] = "standalone"
        invalid_phase = self.legacy_state()
        invalid_phase["phase"] = ["done"]
        for candidate in ({}, ambiguous_v2, invalid_phase):
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "workspace"
                workspace = root / "demo"
                workspace.mkdir(parents=True)
                (workspace / "state.json").write_text(
                    json.dumps(candidate), encoding="utf-8")
                old_root = D.ROOT
                D.ROOT = root
                try:
                    self.assertIsNone(D.legacy_workspace_state_for_delete("demo"))
                    response = self.ResponseCapture()
                    D.Handler.api_delete_workspace(response, {"name": "demo"})
                    self.assertEqual(response.response[0], 400)
                    self.assertTrue(workspace.exists())
                    self.assertFalse((workspace / D.DELETE_GENERATION_MARKER).exists())
                finally:
                    D.ROOT = old_root

    def test_standalone_prelock_replacement_is_never_deleted(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "workspace"
                workspace = root / "demo"
                workspace.mkdir(parents=True)
                original_state = (self.legacy_state() if legacy else
                                  L.Workspace.__new__(L.Workspace).fresh_state())
                original_state["phase"] = "done"
                (workspace / "state.json").write_text(
                    json.dumps(original_state), encoding="utf-8")
                original_marker = workspace / "old-workspace.txt"
                original_marker.write_text("old\n", encoding="utf-8")
                displaced = root / "displaced"
                replacement_marker = root / "replacement-marker-placeholder"
                fired = False

                def replace_before_writer_lock(stage, name):
                    nonlocal fired, replacement_marker
                    if (not fired and stage == "standalone-before-writer-lock" and
                            name == "demo"):
                        fired = True
                        workspace.rename(displaced)
                        workspace.mkdir()
                        replacement = (self.legacy_state() if legacy else
                                       L.Workspace.__new__(L.Workspace).fresh_state())
                        replacement["phase"] = "done"
                        (workspace / "state.json").write_text(
                            json.dumps(replacement), encoding="utf-8")
                        replacement_marker = workspace / "new-workspace.txt"
                        replacement_marker.write_text("new\n", encoding="utf-8")

                old_root = D.ROOT
                D.ROOT = root
                sentinel_job = mock.Mock()
                sentinel_job.alive.return_value = False
                try:
                    with D.JOBS_LOCK:
                        D.JOBS["demo"] = sentinel_job
                    response = self.ResponseCapture()
                    with mock.patch.object(
                            D, "_delete_race_hook", side_effect=replace_before_writer_lock):
                        D.Handler.api_delete_workspace(response, self.delete_request("demo"))
                    self.assertEqual(response.response[0], 409)
                    self.assertTrue(fired)
                    self.assertEqual(replacement_marker.read_text(encoding="utf-8"), "new\n")
                    self.assertEqual((displaced / "old-workspace.txt").read_text(
                        encoding="utf-8"), "old\n")
                    self.assertFalse(D._delete_journal_path("demo").exists())
                    with D.JOBS_LOCK:
                        self.assertIs(D.JOBS.get("demo"), sentinel_job)
                finally:
                    with D.JOBS_LOCK:
                        D.JOBS.pop("demo", None)
                    D.ROOT = old_root

    def test_stale_browser_generation_cannot_mutate_or_delete_same_name_replacement(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "workspace"
                workspace = root / "demo"
                workspace.mkdir(parents=True)
                original = (self.legacy_state() if legacy else
                            L.Workspace.__new__(L.Workspace).fresh_state())
                (workspace / "state.json").write_text(json.dumps(original), encoding="utf-8")
                old_root = D.ROOT
                D.ROOT = root
                try:
                    stale_generation = D.list_workspaces()[0]["workspace_generation"]
                    displaced = root / "old-demo"
                    workspace.rename(displaced)
                    workspace.mkdir()
                    replacement = (self.legacy_state() if legacy else
                                   L.Workspace.__new__(L.Workspace).fresh_state())
                    (workspace / "state.json").write_text(
                        json.dumps(replacement), encoding="utf-8")
                    marker = workspace / "replacement.txt"
                    marker.write_text("survive\n", encoding="utf-8")

                    if not legacy:
                        edited = self.ResponseCapture()
                        D.Handler.api_edit_state(edited, {
                            "name": "demo", "workspace_generation": stale_generation,
                            "ack_issues": True,
                        })
                        self.assertEqual(edited.response[0], 409)
                    deleted = self.ResponseCapture()
                    D.Handler.api_delete_workspace(deleted, {
                        "name": "demo", "workspace_generation": stale_generation,
                    })
                    self.assertEqual(deleted.response[0], 409)
                    self.assertEqual(marker.read_text(encoding="utf-8"), "survive\n")
                    self.assertFalse(D._delete_journal_path("demo").exists())
                finally:
                    D.ROOT = old_root

    def test_deletes_stopped_standalone_and_rejects_fleet_child(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            old_root = D.ROOT
            D.ROOT = root
            try:
                standalone = root / "demo"
                standalone.mkdir(parents=True)
                state = L.Workspace.__new__(L.Workspace).fresh_state()
                state.update(phase="done")
                (standalone / "state.json").write_text(json.dumps(state))
                (standalone / "nested").mkdir()
                (standalone / "nested" / "artifact.txt").write_text("delete me")
                handler = self.ResponseCapture()
                D.Handler.api_delete_workspace(handler, self.delete_request("demo"))
                self.assertEqual(handler.response, (200, {"ok": True, "name": "demo", "deleted": True}))
                self.assertFalse(standalone.exists())

                child = root / "parent--alpha"
                child.mkdir()
                child_state = L.Workspace.__new__(L.Workspace).fresh_state(
                    "fleet-child", "a" * 32)
                child_state.update(fleet_parent="parent", track="alpha",
                                   fleet_parent_session_id="1" * 32,
                                   merge_target_ref="refs/heads/main")
                (child / "state.json").write_text(json.dumps(child_state))
                refused = self.ResponseCapture()
                D.Handler.api_delete_workspace(refused, {"name": "parent--alpha"})
                self.assertEqual(refused.response[0], 409)
                self.assertTrue(child.exists())
            finally:
                D.ROOT = old_root

    def test_parent_delete_removes_registered_worktree_and_child_group_but_keeps_branch(self):
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            root = temp / "workspace"
            repo = make_repo(td)
            run_id = "b" * 32
            session_id = "2" * 32
            parent = root / "parent"
            child = root / "parent--alpha"
            parent.mkdir(parents=True)
            child.mkdir()
            parent_state = L.Workspace.__new__(L.Workspace).fresh_state("fleet-parent", run_id)
            parent_state["config"] = {"repo": str(repo)}
            child_state = L.Workspace.__new__(L.Workspace).fresh_state("fleet-child", run_id)
            child_state.update(fleet_parent="parent", fleet_parent_session_id=session_id,
                               track="alpha", merge_target_ref="refs/heads/main",
                               config={"repo": str(parent / "worktrees" / "alpha"),
                                       "agent_cmd": "true", "validate_cmd": "true"})
            (parent / "state.json").write_text(json.dumps(parent_state))
            (child / "state.json").write_text(json.dumps(child_state))
            worktree = parent / "worktrees" / "alpha"
            branch = f"loop/{run_id}/alpha"
            subprocess.run(["git", "worktree", "add", "-b", branch, str(worktree)], cwd=repo,
                           check=True, capture_output=True)
            fleet = {"schema_version": 1, "workspace_kind": "fleet-parent", "run_id": run_id,
                     "phase": "stopped", "integration_worktree": str(repo),
                     "config": {"repo": str(repo), "agent_cmd": "true", "validate_cmd": "true"},
                     "loop": {"pid": None, "session_id": session_id},
                     "tracks": [{"name": "alpha", "safe_name": "alpha",
                     "worktree": str(worktree), "branch_ref": f"refs/heads/{branch}",
                     "child_workspace": "parent--alpha", "status": "stopped"}]}
            (parent / "fleet.json").write_text(json.dumps(fleet))
            old_root = D.ROOT
            D.ROOT = root
            try:
                stale = self.ResponseCapture()
                D.Handler.api_delete_workspace(stale, {"name": "parent", "run_id": "c" * 32})
                self.assertEqual(stale.response[0], 409)
                self.assertTrue(parent.exists())
                self.assertTrue(child.exists())
                handler = self.ResponseCapture()
                D.Handler.api_delete_workspace(handler, {"name": "parent", "run_id": run_id})
                self.assertEqual(handler.response, (200, {"ok": True, "name": "parent", "deleted": True}))
                self.assertFalse(parent.exists())
                self.assertFalse(child.exists())
                registered = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo,
                                            text=True, capture_output=True, check=True).stdout
                self.assertNotIn(str(worktree), registered)
                self.assertEqual(subprocess.run(["git", "show-ref", "--verify", f"refs/heads/{branch}"],
                                                cwd=repo, capture_output=True).returncode, 0)
            finally:
                D.ROOT = old_root

    def test_standalone_delete_faults_are_journaled_and_retryable(self):
        for stage in ("after-rename", "after-unlink", "before-rmdir"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "workspace"
                root.mkdir()
                workspace = root / "demo"
                workspace.mkdir()
                state = L.Workspace.__new__(L.Workspace).fresh_state()
                state["phase"] = "done"
                (workspace / "state.json").write_text(json.dumps(state), encoding="utf-8")
                (workspace / "artifact.txt").write_text("preserve until retry\n", encoding="utf-8")
                old_root = D.ROOT
                D.ROOT = root
                fired = False

                def fault(candidate_stage, candidate_name):
                    nonlocal fired
                    if not fired and candidate_stage == stage and candidate_name == "demo":
                        fired = True
                        raise OSError(f"injected {stage}")

                try:
                    first = self.ResponseCapture()
                    with mock.patch.object(D, "_delete_fault_hook", side_effect=fault):
                        D.Handler.api_delete_workspace(first, self.delete_request("demo"))
                    self.assertEqual(first.response[0], 409)
                    self.assertIn("可安全重試", first.response[1]["error"])
                    self.assertTrue(D._delete_journal_path("demo").is_file())

                    replacement_marker = None
                    if stage == "after-rename":
                        workspace.mkdir()
                        replacement = L.Workspace.__new__(L.Workspace).fresh_state()
                        (workspace / "state.json").write_text(
                            json.dumps(replacement), encoding="utf-8")
                        replacement_marker = workspace / "new-workspace.txt"
                        replacement_marker.write_text("must survive\n", encoding="utf-8")

                    retry = self.ResponseCapture()
                    D.Handler.api_delete_workspace(retry, {"name": "demo"})
                    self.assertEqual(retry.response[0], 409 if replacement_marker else 200)
                    if replacement_marker:
                        self.assertTrue(retry.response[1]["replacement_preserved"])
                    else:
                        self.assertTrue(retry.response[1]["resumed_delete"])
                    if replacement_marker is None:
                        self.assertFalse(workspace.exists())
                    else:
                        self.assertEqual(replacement_marker.read_text(encoding="utf-8"),
                                         "must survive\n")
                    self.assertFalse(D._delete_journal_path("demo").exists())
                    self.assertFalse(any(path.name.startswith(".delete-")
                                         for path in root.iterdir()))
                finally:
                    D.ROOT = old_root

    def test_completed_delete_stale_journal_never_touches_new_same_name_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            root.mkdir()
            workspace = root / "demo"
            workspace.mkdir()
            state = L.Workspace.__new__(L.Workspace).fresh_state()
            state["phase"] = "done"
            (workspace / "state.json").write_text(json.dumps(state), encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            fired = False

            def fail_clear(stage, name):
                nonlocal fired
                if not fired and stage == "before-journal-clear" and name == "demo":
                    fired = True
                    raise OSError("injected journal clear failure")

            try:
                first = self.ResponseCapture()
                with mock.patch.object(D, "_delete_fault_hook", side_effect=fail_clear):
                    D.Handler.api_delete_workspace(first, self.delete_request("demo"))
                self.assertEqual(first.response[0], 409)
                self.assertFalse(workspace.exists())
                self.assertTrue(D._delete_journal_path("demo").exists())

                workspace.mkdir()
                replacement = L.Workspace.__new__(L.Workspace).fresh_state()
                (workspace / "state.json").write_text(json.dumps(replacement), encoding="utf-8")
                marker = workspace / "new-workspace.txt"
                marker.write_text("must survive\n", encoding="utf-8")
                # 模擬檔案系統重用舊 inode；generation 仍必須辨識新 workspace。
                journal_path = D._delete_journal_path("demo")
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                replacement_info = workspace.lstat()
                journal["entries"][0]["dev"] = replacement_info.st_dev
                journal["entries"][0]["ino"] = replacement_info.st_ino
                self.assertNotEqual(journal["entries"][0]["generation"],
                                    replacement["workspace_generation"])
                journal_path.write_text(json.dumps(journal), encoding="utf-8")
                sentinel_job = mock.Mock()
                sentinel_job.alive.return_value = False
                with D.JOBS_LOCK:
                    D.JOBS["demo"] = sentinel_job
                retry = self.ResponseCapture()
                D.Handler.api_delete_workspace(retry, {"name": "demo"})
                self.assertEqual(retry.response[0], 409)
                self.assertTrue(retry.response[1]["replacement_preserved"])
                self.assertEqual(marker.read_text(encoding="utf-8"), "must survive\n")
                self.assertFalse(D._delete_journal_path("demo").exists())
                with D.JOBS_LOCK:
                    self.assertIs(D.JOBS.get("demo"), sentinel_job)
                confirmed = self.ResponseCapture()
                D.Handler.api_delete_workspace(confirmed, self.delete_request("demo"))
                self.assertEqual(confirmed.response[0], 200)
                self.assertFalse(workspace.exists())
            finally:
                with D.JOBS_LOCK:
                    D.JOBS.pop("demo", None)
                D.ROOT = old_root

    def test_group_git_retry_preserves_new_different_branch_at_old_worktree_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            parent = root / "parent"
            worktrees = parent / "worktrees"
            worktrees.mkdir(parents=True)
            repo = make_repo(td)
            old_run = "a" * 32
            new_run = "b" * 32
            path = worktrees / "alpha"
            old_branch = f"refs/heads/loop/{old_run}/alpha"
            subprocess.run(["git", "worktree", "add", "-b",
                            old_branch.removeprefix("refs/heads/"), str(path)],
                           cwd=repo, check=True, capture_output=True)
            old_tip = subprocess.run(["git", "rev-parse", old_branch], cwd=repo,
                                     text=True, capture_output=True, check=True).stdout.strip()
            subprocess.run(["git", "worktree", "remove", str(path)], cwd=repo,
                           check=True, capture_output=True)
            new_branch = f"refs/heads/loop/{new_run}/alpha"
            subprocess.run(["git", "worktree", "add", "-b",
                            new_branch.removeprefix("refs/heads/"), str(path)],
                           cwd=repo, check=True, capture_output=True)
            common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=repo,
                                    text=True, capture_output=True, check=True).stdout.strip()
            common_path = (Path(common) if Path(common).is_absolute() else repo / common).resolve()
            journal = {"kind": "fleet-group", "git": {
                "repo": str(repo.resolve()), "common_dir": str(common_path),
                "integration_ref": "refs/heads/main",
                "worktrees": [{"track": "alpha", "safe_name": "alpha",
                               "path": str(path.resolve()), "branch_ref": old_branch,
                               "branch_tip": old_tip}]}}
            old_root = D.ROOT
            D.ROOT = root
            try:
                D._resume_delete_journal_git(journal)
                self.assertTrue(path.is_dir())
                actual = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=path,
                                        text=True, capture_output=True, check=True).stdout.strip()
                self.assertEqual(actual, new_branch)
            finally:
                D.ROOT = old_root

    def test_tampered_delete_journal_cannot_redirect_to_unrelated_workspace(self):
        for mutation in ("entry", "tombstone"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "workspace"
                demo = root / "demo"
                victim = root / "victim"
                demo.mkdir(parents=True)
                victim.mkdir()
                state = L.Workspace.__new__(L.Workspace).fresh_state()
                state["phase"] = "done"
                (demo / "state.json").write_text(json.dumps(state), encoding="utf-8")
                marker = victim / "must-survive.txt"
                marker.write_text("safe\n", encoding="utf-8")
                info = demo.lstat()
                identity = "demo\0\0demo"
                tombstone = ".delete-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
                journal = {"schema_version": 1, "request_name": "demo",
                           "kind": "standalone", "run_id": None,
                           "entries": [{"name": "demo", "tombstone": tombstone,
                                        "dev": info.st_dev, "ino": info.st_ino,
                                        "generation": state["workspace_generation"],
                                        "lock_names": [".run.lock"]}]}
                if mutation == "entry":
                    victim_info = victim.lstat()
                    journal["entries"][0].update(
                        name="victim", dev=victim_info.st_dev, ino=victim_info.st_ino)
                else:
                    journal["entries"][0]["tombstone"] = ".delete-attacker-chosen"
                old_root = D.ROOT
                D.ROOT = root
                try:
                    D._write_delete_journal(journal)
                    handler = self.ResponseCapture()
                    D.Handler.api_delete_workspace(handler, {"name": "demo"})
                    self.assertEqual(handler.response[0], 409)
                    self.assertTrue(demo.exists())
                    self.assertEqual(marker.read_text(encoding="utf-8"), "safe\n")
                finally:
                    D.ROOT = old_root

    def test_delete_journal_rejects_unbounded_entry_list(self):
        journal = {"schema_version": 1, "request_name": "demo", "kind": "fleet-group",
                   "run_id": "a" * 32, "entries": [{} for _ in range(10)]}
        with self.assertRaisesRegex(D.SafeDeleteError, "bounded 上限"):
            D._validate_delete_journal(journal, "demo")

    def test_delete_journal_rejects_escaping_worktree_component(self):
        run_id = "a" * 32
        identity = f"demo\0{run_id}\0demo"
        journal = {
            "schema_version": 1, "request_name": "demo", "kind": "fleet-group",
            "run_id": run_id,
            "entries": [{"name": "demo",
                         "tombstone": ".delete-" + hashlib.sha256(
                             identity.encode()).hexdigest()[:32],
                         "dev": 1, "ino": 1,
                         "generation": "b" * 32,
                         "lock_names": [".fleet.run.lock", ".run.lock"]}],
            "git": {"repo": "/tmp/repo", "common_dir": "/tmp/repo/.git",
                    "integration_ref": "refs/heads/main",
                    "worktrees": [{"track": "../../victim", "safe_name": "../../victim",
                                   "path": "/tmp/victim",
                                   "branch_ref": f"refs/heads/loop/{run_id}/../../victim",
                                   "branch_tip": "0" * 40}],
                    "children": []},
        }
        with self.assertRaisesRegex(D.SafeDeleteError, "worktree journal identity"):
            D._validate_delete_journal(journal, "demo")

    def test_fleet_delete_journal_rejects_unrelated_parent_prefix_workspace(self):
        run_id = "a" * 32
        entries = []
        for entry_name, locks in (
                ("demo", [".fleet.run.lock", ".run.lock"]),
                ("demo--notes", [".run.lock"])):
            identity = f"demo\0{run_id}\0{entry_name}"
            entries.append({
                "name": entry_name,
                "tombstone": ".delete-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
                "dev": 1, "ino": len(entries) + 1, "generation": "b" * 32,
                "generation_source": "state", "lock_names": locks,
            })
        journal = {
            "schema_version": 1, "request_name": "demo", "kind": "fleet-group",
            "run_id": run_id, "entries": entries,
            "git": {
                "repo": "/tmp/repo", "common_dir": "/tmp/repo/.git",
                "integration_ref": "refs/heads/main",
                "worktrees": [{"track": "alpha", "safe_name": "alpha",
                               "path": str((Path("/tmp/workspace") /
                                            "demo/worktrees/alpha").resolve()),
                               "branch_ref": f"refs/heads/loop/{run_id}/alpha",
                               "branch_tip": "0" * 40}],
                # A journal editor cannot make an arbitrary prefixed workspace part of
                # this run: every child must bijectively match the persisted track list.
                "children": [{"track": "alpha", "safe_name": "alpha",
                              "name": "demo--notes", "present": True}],
            },
        }
        old_root = D.ROOT
        D.ROOT = Path("/tmp/workspace")
        try:
            with self.assertRaisesRegex(D.SafeDeleteError, "child journal identity"):
                D._validate_delete_journal(journal, "demo")
        finally:
            D.ROOT = old_root


    def test_old_fleet_journal_preserves_new_run_and_requires_fresh_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            root.mkdir()
            old_parent = root / "parallel"
            old_parent.mkdir()
            old_info = old_parent.lstat()
            old_parent.rmdir()
            replacement = root / "parallel"
            replacement.mkdir()
            marker = replacement / "new-run.txt"
            marker.write_text("new\n", encoding="utf-8")
            repo = make_repo(td)
            common_raw = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True,
                capture_output=True, check=True).stdout.strip()
            common = (Path(common_raw) if Path(common_raw).is_absolute()
                      else repo / common_raw).resolve()
            old_run, new_run = "a" * 32, "b" * 32
            identity = f"parallel\0{old_run}\0parallel"
            journal = {
                "schema_version": 1, "request_name": "parallel", "kind": "fleet-group",
                "run_id": old_run,
                "entries": [{"name": "parallel",
                             "tombstone": ".delete-" + hashlib.sha256(
                                 identity.encode()).hexdigest()[:32],
                             "dev": old_info.st_dev, "ino": old_info.st_ino,
                             "generation": "c" * 32,
                             "lock_names": [".fleet.run.lock", ".run.lock"]}],
                "git": {"repo": str(repo.resolve()), "common_dir": str(common),
                        "integration_ref": "refs/heads/main", "worktrees": [],
                        "children": []},
            }
            old_root = D.ROOT
            D.ROOT = root
            sentinel_job = mock.Mock()
            sentinel_job.alive.return_value = False
            try:
                D._write_delete_journal(journal)
                with D.JOBS_LOCK:
                    D.JOBS["parallel"] = sentinel_job
                response = self.ResponseCapture()
                D.Handler.api_delete_workspace(
                    response, {"name": "parallel", "run_id": new_run})
                self.assertEqual(response.response[0], 409)
                self.assertTrue(response.response[1]["replacement_preserved"])
                self.assertEqual(marker.read_text(encoding="utf-8"), "new\n")
                self.assertFalse(D._delete_journal_path("parallel").exists())
                with D.JOBS_LOCK:
                    self.assertIs(D.JOBS.get("parallel"), sentinel_job)
            finally:
                with D.JOBS_LOCK:
                    D.JOBS.pop("parallel", None)
                D.ROOT = old_root


class TestDurableRuntimeIdentity(unittest.TestCase):
    def tearDown(self):
        L.remove_runtime_identity_markers()
        L.clear_runtime_identity_context()

    def test_marker_write_failure_never_releases_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "demo"
            workspace.mkdir()
            target_started = root / "target-started"
            generation, session_id = "a" * 32, "b" * 32
            L.configure_runtime_identity(workspace, generation, session_id,
                                         workspace_name="demo", repo=root)
            real_atomic = L.atomic_write_bytes

            def fail_active_marker(path, data):
                if Path(path).name == L.ACTIVE_RUNTIME_FILE:
                    raise OSError("simulated fsync failure")
                return real_atomic(path, data)

            command = [sys.executable, "-c",
                       f"from pathlib import Path; Path({str(target_started)!r}).write_text('bad')"]
            with mock.patch.object(L, "atomic_write_bytes", side_effect=fail_active_marker):
                with self.assertRaisesRegex(OSError, "fsync failure"):
                    L._spawn_runtime_process(command, kind="agent", cwd=root,
                                             env=os.environ, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
            time.sleep(0.1)
            self.assertFalse(target_started.exists(), "marker durable 前不得執行真命令")

    def test_gate_wrapper_exits_on_eof_without_starting_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target_started = Path(directory) / "target-started"
            read_fd, write_fd = os.pipe()
            env = {**os.environ, "LOOP_AGENT_RUNTIME_ARGV": json.dumps([
                sys.executable, "-c",
                f"from pathlib import Path; Path({str(target_started)!r}).write_text('bad')",
            ])}
            process = subprocess.Popen(
                [sys.executable, "-c", L._RUNTIME_GATE_WRAPPER, str(read_fd)],
                pass_fds=(read_fd,), env=env, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
            os.close(read_fd)
            os.close(write_fd)
            self.assertEqual(process.wait(timeout=2), 125)
            self.assertFalse(target_started.exists())

    def test_hard_killed_coordinator_leaves_group_that_startup_sweep_cleans(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspace-root"
            root.mkdir()
            workspace = root / "demo"
            workspace.mkdir()
            (workspace / "state.json").write_text("{corrupt", encoding="utf-8")
            prompt = base / "prompt.md"
            prompt.write_text("test\n", encoding="utf-8")
            agent_pid_file = base / "agent.pid"
            agent = base / "agent.py"
            agent.write_text(
                "import os,time\n"
                "from pathlib import Path\n"
                f"p=Path({str(agent_pid_file)!r})\n"
                "p.write_text(str(os.getpid()))\n"
                "with p.open('r') as f: os.fsync(f.fileno())\n"
                "time.sleep(60)\n", encoding="utf-8")
            generation, session_id = "a" * 32, "b" * 32
            script = (
                "import os,sys\n"
                "from pathlib import Path\n"
                "from engine import loop as L\n"
                f"ws=Path({str(workspace)!r}); root=Path({str(base)!r})\n"
                f"L.configure_runtime_identity(ws,{generation!r},{session_id!r},"
                "workspace_name='demo',repo=root)\n"
                f"L.run_agent([sys.executable,{str(agent)!r}],Path({str(prompt)!r}),root,"
                f"os.environ,ws/'agent.log',60)\n"
            )
            env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
            coordinator = subprocess.Popen(
                [sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while (not agent_pid_file.exists() or
                       not (workspace / L.ACTIVE_RUNTIME_FILE).exists()):
                    if coordinator.poll() is not None:
                        self.fail(f"coordinator 提前退出 rc={coordinator.returncode}")
                    if time.monotonic() >= deadline:
                        self.fail("agent/runtime marker 未在期限內啟動")
                    time.sleep(0.02)
                os.kill(coordinator.pid, signal.SIGKILL)
                coordinator.wait(timeout=2)
                old_root = D.ROOT
                D.ROOT = root
                try:
                    result = D.stop_workspace_coordinators(grace_seconds=0.2,
                                                           force_seconds=1.0)
                    self.assertGreaterEqual(result["requested"], 1)
                    marker = json.loads((workspace / L.ACTIVE_RUNTIME_FILE).read_text())
                    final = D._process_snapshot()
                    self.assertIsNotNone(final)
                    self.assertFalse(any(
                        process.get("pgid") == marker["pgid"] and
                        process.get("sid") == marker["sid"]
                        for process in final.values()))
                finally:
                    D.ROOT = old_root
            finally:
                if coordinator.poll() is None:
                    coordinator.kill()
                    coordinator.wait()

    def test_fleet_pending_marker_covers_window_before_parent_mkdir(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspace-root"
            root.mkdir()
            ready = base / "ready"
            script = (
                "import os,time\n"
                "from pathlib import Path\n"
                "from engine import loop as L\n"
                f"root=Path({str(root)!r}); repo=Path({str(base)!r})\n"
                "L.configure_pending_runtime_identity(root,'parallel',repo,'a'*32,'b'*32)\n"
                f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script], cwd=REPO_ROOT,
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                pending_path = L.pending_runtime_marker_path(root, "parallel")
                while not ready.exists() or not pending_path.exists():
                    if process.poll() is not None:
                        self.fail(f"pending fleet 提前退出 rc={process.returncode}")
                    if time.monotonic() >= deadline:
                        self.fail("pending fleet marker 未在期限內落盤")
                    time.sleep(0.02)
                self.assertFalse((root / "parallel").exists())
                old_root = D.ROOT
                D.ROOT = root
                try:
                    result = D.stop_workspace_coordinators(grace_seconds=1, force_seconds=1)
                    self.assertEqual(result["requested"], 1)
                finally:
                    D.ROOT = old_root
                process.wait(timeout=2)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


class TestDashboardStartupCoordinatorSweep(unittest.TestCase):
    @staticmethod
    def _write_marker(entry: Path, filename: str, payload: dict):
        L.atomic_write_bytes(entry / filename,
                             json.dumps(payload, separators=(",", ":")).encode())

    @staticmethod
    def _coordinator_marker(root: Path, name: str, generation: str, pid: int,
                            started: str, command: str):
        return {
            "schema_version": L.ACTIVE_RUNTIME_SCHEMA_VERSION,
            "workspace_name": name, "workspace_root": str(root.resolve()),
            "repo": "/tmp/repo", "workspace_generation": generation,
            "session_id": "d" * 32, "pid": pid,
            "started": started, "command": command,
        }

    @staticmethod
    def _active_marker(root: Path, name: str, generation: str, pid: int,
                       started: str, command: str):
        return {
            "schema_version": L.ACTIVE_RUNTIME_SCHEMA_VERSION, "kind": "agent",
            "workspace_name": name, "workspace_root": str(root.resolve()),
            "repo": "/tmp/repo", "workspace_generation": generation,
            "session_id": "d" * 32, "owner_pid": 77,
            "owner_started": "owner-start", "owner_command": "owner-command",
            "pid": pid, "pgid": pid, "sid": pid, "started": started,
            "command": command, "target_command": "agent --work",
        }
    def test_collects_standalone_parent_planner_orphan_child_and_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("standalone", "parallel", "parallel--alpha", "legacy"):
                (root / name).mkdir()
            states = {
                "standalone": {"workspace_kind": "standalone", "loop": {"pid": 101}},
                "parallel": {"workspace_kind": "fleet-parent", "loop": {"pid": 999}},
                "parallel--alpha": {"workspace_kind": "fleet-child", "loop": {"pid": 303}},
            }
            snapshot = {
                pid: {"ppid": 1, "started": f"start-{pid}", "command": command}
                for pid, command in {
                    101: "python -m engine.loop --name standalone",
                    202: "python -m engine.fleet --name parallel",
                    303: "python -m engine.loop --name parallel--alpha",
                    404: "python -m engine.loop --name legacy",
                    999: "python -m engine.loop --name parallel",
                }.items()
            }
            old_root = D.ROOT
            D.ROOT = root
            try:
                def read(name, repair=False):
                    return ((None, "legacy") if name == "legacy" else (states[name], None))

                with mock.patch.object(D, "read_state", side_effect=read), \
                        mock.patch.object(D, "read_parallel_run",
                                          return_value={"loop": {"pid": 202}}), \
                        mock.patch.object(
                            D, "_pending_coordinator_pids",
                            return_value=({"parallel": {202}}, set())), \
                        mock.patch.object(
                            D, "_coordinator_marker_pids",
                            side_effect=lambda entry, _snapshot, _generation: ({
                                "standalone": {101}, "parallel": {999},
                                "parallel--alpha": {303}, "legacy": {404},
                            }.get(entry.name, set()), None)), \
                        mock.patch.object(
                            D, "legacy_workspace_identity_for_delete",
                            side_effect=lambda name: ({"state": {"loop": {"pid": 404}}}
                                                      if name == "legacy" else None)):
                    self.assertEqual(D._workspace_coordinator_pids(snapshot),
                                     {101, 202, 303, 404, 999})
            finally:
                D.ROOT = old_root

    def test_force_path_kills_captured_descendants_before_stuck_coordinator(self):
        initial = {
            100: {"ppid": 1, "started": "s100",
                  "command": "python -m engine.loop --name demo"},
            101: {"ppid": 100, "started": "s101", "command": "agent child --work"},
            102: {"ppid": 101, "started": "s102", "command": "validator child"},
        }
        sent = []
        with mock.patch.object(D, "_process_snapshot",
                               side_effect=[initial, initial, initial, {}]), \
                mock.patch.object(D, "_workspace_coordinator_pids", return_value={100}), \
                mock.patch.object(D.os, "kill", side_effect=lambda pid, sig: sent.append((pid, sig))):
            result = D.stop_workspace_coordinators(grace_seconds=0, force_seconds=0)
        self.assertEqual(sent[0], (100, signal.SIGINT))
        self.assertEqual(sent[1:], [(102, signal.SIGKILL), (101, signal.SIGKILL),
                                    (100, signal.SIGKILL)])
        self.assertEqual(result, {"requested": 1, "forced": 3, "remaining": []})

    def test_force_path_still_kills_captured_child_after_reparent(self):
        initial = {
            100: {"ppid": 1, "started": "s100",
                  "command": "python -m engine.loop --name demo"},
            101: {"ppid": 100, "started": "s101", "command": "agent child --work"},
        }
        reparented = {
            101: {"ppid": 1, "started": "s101", "command": "agent child --work"},
        }
        sent = []
        with mock.patch.object(D, "_process_snapshot",
                               side_effect=[initial, initial, reparented, {}]), \
                mock.patch.object(D, "_workspace_coordinator_pids", return_value={100}), \
                mock.patch.object(D.os, "kill",
                                  side_effect=lambda pid, sig: sent.append((pid, sig))):
            result = D.stop_workspace_coordinators(grace_seconds=0, force_seconds=0)
        self.assertEqual(sent, [(100, signal.SIGINT), (101, signal.SIGKILL)])
        self.assertEqual(result, {"requested": 1, "forced": 1, "remaining": []})

    def test_stale_pid_owned_by_other_workspace_coordinator_is_not_signaled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "expected").mkdir()
            state = {"workspace_kind": "standalone", "loop": {"pid": 101}}
            snapshot = {
                101: {"ppid": 1, "started": "start-101",
                      "command": "python -m engine.loop --name=other --repo /tmp/expected"},
            }
            old_root = D.ROOT
            D.ROOT = root
            try:
                with mock.patch.object(D, "read_state", return_value=(state, None)):
                    self.assertEqual(D._workspace_coordinator_pids(snapshot), set())
            finally:
                D.ROOT = old_root

    def test_live_legacy_coordinator_without_marker_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "expected").mkdir()
            state = {"workspace_kind": "standalone", "loop": {"pid": 101}}
            snapshot = {
                101: {"ppid": 1, "started": "start-101",
                      "command": "python -m engine.loop --repo /tmp/expected"},
            }
            old_root = D.ROOT
            D.ROOT = root
            try:
                with mock.patch.object(D, "read_state", return_value=(state, None)):
                    with self.assertRaisesRegex(RuntimeError, "缺 durable root marker"):
                        D._workspace_coordinator_pids(snapshot)
            finally:
                D.ROOT = old_root

    def test_force_does_not_kill_reused_pid_with_same_command(self):
        initial = {100: {"ppid": 1, "started": "old-start",
                         "command": "python -m engine.loop --name demo"}}
        reused = {100: {"ppid": 77, "started": "new-start",
                        "command": "python -m engine.loop --name demo"}}
        sent = []
        with mock.patch.object(D, "_process_snapshot",
                               side_effect=[initial, reused, reused, {}]), \
                mock.patch.object(D, "_workspace_coordinator_pids", return_value={100}), \
                mock.patch.object(D.os, "kill", side_effect=lambda pid, sig: sent.append((pid, sig))):
            result = D.stop_workspace_coordinators(grace_seconds=0, force_seconds=0)
        self.assertEqual(sent, [])
        self.assertEqual(result["forced"], 0)

    def test_dead_runtime_leader_with_unrostered_same_pgid_member_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "demo"
            entry.mkdir()
            generation = "a" * 32
            marker = self._active_marker(root, "demo", generation, 100,
                                         "leader-start", "gate-wrapper nonce")
            self._write_marker(entry, L.ACTIVE_RUNTIME_FILE, marker)
            snapshot = {
                201: {"ppid": 1, "pgid": 100, "sid": 100,
                      "started": "member-start", "command": "agent child"},
                202: {"ppid": 1, "pgid": 999, "sid": 999,
                      "started": "other-start", "command": "other process"},
            }
            old_root = D.ROOT
            D.ROOT = root
            try:
                state = {"workspace_kind": "standalone",
                         "workspace_generation": generation, "loop": {"pid": None}}
                with mock.patch.object(D, "read_state", return_value=(state, None)), \
                        mock.patch.object(D, "_process_snapshot", return_value=snapshot), \
                        mock.patch.object(D.os, "kill") as kill:
                    with self.assertRaisesRegex(RuntimeError, "未證明 members"):
                        D.stop_workspace_coordinators(grace_seconds=0, force_seconds=0)
                    kill.assert_not_called()
            finally:
                D.ROOT = old_root

    def test_runtime_marker_pid_reuse_is_never_collected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "demo"
            entry.mkdir()
            generation = "a" * 32
            marker = self._active_marker(root, "demo", generation, 100,
                                         "old-start", "gate-wrapper nonce")
            self._write_marker(entry, L.ACTIVE_RUNTIME_FILE, marker)
            snapshot = {
                100: {"ppid": 1, "pgid": 100, "sid": 100,
                      "started": "new-start", "command": "gate-wrapper nonce"},
            }
            old_root = D.ROOT
            D.ROOT = root
            try:
                state = {"workspace_kind": "standalone",
                         "workspace_generation": generation, "loop": {"pid": None}}
                with mock.patch.object(D, "read_state", return_value=(state, None)):
                    self.assertEqual(D._workspace_coordinator_pids(snapshot), set())
            finally:
                D.ROOT = old_root

    def test_valid_state_cannot_bypass_coordinator_marker_pid_reuse_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "demo"
            entry.mkdir()
            generation = "a" * 32
            command = "python -m engine.loop --repo /tmp/repo --name demo"
            marker = self._coordinator_marker(root, "demo", generation, 101,
                                              "old-start", command)
            self._write_marker(entry, L.COORDINATOR_RUNTIME_FILE, marker)
            snapshot = {101: {"ppid": 1, "pgid": 101, "sid": 101,
                              "started": "new-start", "command": command}}
            state = {"workspace_kind": "standalone",
                     "workspace_generation": generation, "loop": {"pid": 101}}
            old_root = D.ROOT
            D.ROOT = root
            try:
                with mock.patch.object(D, "read_state", return_value=(state, None)):
                    with self.assertRaisesRegex(RuntimeError, "marker identity mismatch"):
                        D._workspace_coordinator_pids(snapshot)
            finally:
                D.ROOT = old_root

    def test_reset_pending_generation_accepts_old_disk_but_rejects_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "demo"
            entry.mkdir()
            old_generation, pending_generation = "a" * 32, "b" * 32
            command = "python -m engine.loop --repo /tmp/repo --name demo --reset-state"
            coordinator = self._coordinator_marker(
                root, "demo", pending_generation, 101, "start-101", command)
            coordinator["previous_workspace_generation"] = old_generation
            active = self._active_marker(
                root, "demo", pending_generation, 201, "start-201", "gate-wrapper")
            active["previous_workspace_generation"] = old_generation
            self._write_marker(entry, L.COORDINATOR_RUNTIME_FILE, coordinator)
            self._write_marker(entry, L.ACTIVE_RUNTIME_FILE, active)
            snapshot = {
                101: {"ppid": 1, "pgid": 101, "sid": 101,
                      "started": "start-101", "command": command},
                201: {"ppid": 101, "pgid": 201, "sid": 201,
                      "started": "start-201", "command": "gate-wrapper"},
            }
            old_root = D.ROOT
            D.ROOT = root
            try:
                old_state = {"workspace_kind": "standalone",
                             "workspace_generation": old_generation,
                             "loop": {"pid": 101}}
                with mock.patch.object(D, "read_state", return_value=(old_state, None)):
                    self.assertEqual(D._workspace_coordinator_pids(snapshot), {101, 201})
                replacement = {**old_state, "workspace_generation": "c" * 32}
                with mock.patch.object(D, "read_state", return_value=(replacement, None)):
                    with self.assertRaisesRegex(RuntimeError, "marker identity mismatch"):
                        D._workspace_coordinator_pids(snapshot)
            finally:
                D.ROOT = old_root

    def test_corrupt_state_uses_exact_root_coordinator_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "demo"
            entry.mkdir()
            command = "python -m engine.loop --repo /tmp/repo --name demo"
            marker = self._coordinator_marker(root, "demo", "a" * 32, 101,
                                              "start-101", command)
            self._write_marker(entry, L.COORDINATOR_RUNTIME_FILE, marker)
            snapshot = {101: {"ppid": 1, "pgid": 101, "sid": 101,
                              "started": "start-101", "command": command}}
            old_root = D.ROOT
            D.ROOT = root
            try:
                with mock.patch.object(D, "read_state", return_value=(None, "corrupt")), \
                        mock.patch.object(D, "legacy_workspace_identity_for_delete",
                                          return_value=None):
                    self.assertEqual(D._workspace_coordinator_pids(snapshot), {101})
            finally:
                D.ROOT = old_root

    def test_corrupt_state_does_not_kill_same_name_from_another_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root-a"
            root.mkdir()
            (root / "demo").mkdir()
            command = "python -m engine.loop --repo /other/repo --name demo"
            snapshot = {101: {"ppid": 1, "pgid": 101, "sid": 101,
                              "started": "start-101", "command": command}}
            old_root = D.ROOT
            D.ROOT = root
            try:
                with mock.patch.object(D, "read_state", return_value=(None, "corrupt")), \
                        mock.patch.object(D, "legacy_workspace_identity_for_delete",
                                          return_value=None), \
                        mock.patch.object(D, "_process_snapshot", return_value=snapshot), \
                        mock.patch.object(D.os, "kill") as kill:
                    with self.assertRaisesRegex(RuntimeError, "未經 root marker 證明"):
                        D.stop_workspace_coordinators(grace_seconds=0, force_seconds=0)
                    kill.assert_not_called()
            finally:
                D.ROOT = old_root

    def test_workspace_root_scan_failure_is_fail_closed(self):
        class BrokenRoot:
            def __truediv__(self, _name):
                return Path("/definitely-missing-loop-agent-ops")

            def iterdir(self):
                raise PermissionError("denied")

            def __str__(self):
                return "broken-root"

        broken_root = BrokenRoot()
        old_root = D.ROOT
        D.ROOT = broken_root
        try:
            with self.assertRaisesRegex(RuntimeError, "無法掃描 workspace root"):
                D._workspace_coordinator_pids({})
        finally:
            D.ROOT = old_root

    def test_run_dashboard_does_not_bind_when_startup_sweep_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            old_root = D.ROOT
            D.ROOT = Path(directory) / "workspace-root"
            try:
                with mock.patch.object(D, "load_config", return_value={}), \
                        mock.patch.object(D, "stop_workspace_coordinators",
                                          side_effect=RuntimeError("still running")), \
                        mock.patch.object(D, "DashboardServer") as server:
                    with self.assertRaisesRegex(RuntimeError, "still running"):
                        D.run_dashboard(port=8765)
                server.assert_not_called()
            finally:
                D.ROOT = old_root

    @staticmethod
    def _start_dashboard_lease_holder(root: Path, ready: Path):
        script = (
            "import time\n"
            "from pathlib import Path\n"
            "from engine import dashboard as D\n"
            "with D.dashboard_instance_lease():\n"
            f"    Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
            "    time.sleep(60)\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT),
               "LOOP_AGENT_WORKSPACE_ROOT": str(root)}
        process = subprocess.Popen(
            [sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while not ready.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    f"Dashboard lease holder 提前退出 rc={process.returncode}: {stdout}{stderr}")
            if time.monotonic() >= deadline:
                process.kill()
                stdout, stderr = process.communicate()
                raise AssertionError(f"Dashboard lease holder 未就緒: {stdout}{stderr}")
            time.sleep(0.02)
        return process

    def test_second_instance_on_different_port_fails_before_config_sweep_or_bind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace-root"
            root.mkdir()
            holder = self._start_dashboard_lease_holder(root, Path(directory) / "ready")
            old_root = D.ROOT
            D.ROOT = root
            try:
                with mock.patch.object(D, "load_config") as load_config, \
                        mock.patch.object(D, "stop_workspace_coordinators") as sweep, \
                        mock.patch.object(D, "DashboardServer") as server:
                    with self.assertRaisesRegex(RuntimeError, "singleton lease"):
                        D.run_dashboard(port=54321)
                load_config.assert_not_called()
                sweep.assert_not_called()
                server.assert_not_called()
            finally:
                if holder.poll() is None:
                    holder.terminate()
                holder.communicate(timeout=3)
                D.ROOT = old_root

    def test_dashboard_singleton_lease_is_released_by_process_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace-root"
            root.mkdir()
            holder = self._start_dashboard_lease_holder(root, Path(directory) / "ready")
            old_root = D.ROOT
            D.ROOT = root
            try:
                os.kill(holder.pid, signal.SIGKILL)
                holder.communicate(timeout=3)
                with D.dashboard_instance_lease() as lease:
                    self.assertFalse(lease.closed)
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.communicate(timeout=3)
                D.ROOT = old_root

    def test_dashboard_singleton_lock_rejects_symlink_and_hardlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace-root"
            root.mkdir()
            victim = Path(directory) / "victim"
            victim.write_text("safe", encoding="utf-8")
            lock_path = root / D.DASHBOARD_INSTANCE_LOCK
            lock_path.symlink_to(victim)
            old_root = D.ROOT
            D.ROOT = root
            try:
                with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                    with D.dashboard_instance_lease():
                        self.fail("symlink lock 不得取得 lease")
                self.assertEqual(victim.read_text(encoding="utf-8"), "safe")
                lock_path.unlink()
                lock_path.write_text("", encoding="utf-8")
                os.link(lock_path, root / ".dashboard.instance.alias")
                with self.assertRaisesRegex(RuntimeError, "單一 regular file"):
                    with D.dashboard_instance_lease():
                        self.fail("hard-linked lock 不得取得 lease")
            finally:
                D.ROOT = old_root


class TestPreflightOnly(unittest.TestCase):
    """--preflight-only:只健檢不啟動——不建 state.json、不動 snapshots,依結果回 exit code。"""

    def _run(self, repo, workspace_root, validate_cmd, name):
        env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
        return subprocess.run(
            [*LOOP_CMD, "--repo", str(repo), "--name", name,
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
                    [*LOOP_CMD, "--repo", str(repo), "--name", name,
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
            plan.write_text(json.dumps([{"order": 1, "task": "不得被零門檻跳過", "track": "main"}]))
            marker = root / "agent-started"
            agent = root / "noop_agent.py"
            agent.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n")
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            result = subprocess.run(
                [*LOOP_CMD, "--repo", str(repo), "--name", "zero-done",
                 "--agent-cmd", shlex.join([sys.executable, str(agent)]), "--validate-cmd", "true",
                 "--import-plan", str(plan), "--start-phase", "exec", "--done-threshold", "0"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("--done-threshold", result.stderr)
            self.assertFalse((workspace_root / "zero-done" / "state.json").exists())
            self.assertFalse(marker.exists(), "零門檻不得有機會繞過 work.py done 共識")


class TestDashboardNumericGuards(unittest.TestCase):
    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_launch_rejects_zero_nonfinite_bool_and_fractional_integer_before_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            config = {
                "agent_cmds": [{"label": "true", "cmd": "true"}],
                "validate_cmds": [{"label": "true", "cmd": "true"}],
                "extra_path_dirs": [], "notify_cmd": "", "defaults": {},
            }
            old_values = D.ROOT, L.WORKSPACE_ROOT, D.load_config
            D.ROOT, L.WORKSPACE_ROOT = workspace_root, workspace_root
            D.load_config = lambda: config
            cases = [
                ("flag_threshold", 0), ("done_threshold", 0),
                ("red_limit", 0), ("stall_limit", 0),
                ("flag_threshold", 1.5), ("done_threshold", True),
                ("round_timeout", float("nan")),
                ("agent_backoff_max", float("inf")),
                ("validate_timeout", 0),
            ]
            try:
                for index, (field, value) in enumerate(cases):
                    name = f"dashboard-bad-number-{index}"
                    handler = self.ResponseCapture()
                    D.Handler.api_launch(handler, {
                        "repo": str(repo), "name": name, "agent_idx": 0, "validate_idx": 0,
                        field: value,
                    })
                    with self.subTest(field=field, value=value):
                        self.assertEqual(handler.response[0], 400)
                        self.assertNotIn(name, D.JOBS)
                        self.assertFalse((workspace_root / name).exists())
            finally:
                D.ROOT, L.WORKSPACE_ROOT, D.load_config = old_values

    def test_edit_and_run_reject_nonfinite_workspace_settings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            workspace_root = root / "workspace"
            config = {
                "agent_cmds": [{"label": "true", "cmd": "true"}],
                "validate_cmds": [{"label": "true", "cmd": "true"}],
                "extra_path_dirs": [], "notify_cmd": "", "defaults": {},
            }
            old_values = D.ROOT, L.WORKSPACE_ROOT, D.load_config
            D.ROOT, L.WORKSPACE_ROOT = workspace_root, workspace_root
            D.load_config = lambda: config
            try:
                ws = L.Workspace("bad-saved-number")
                state = ws.fresh_state()
                state["config"] = {
                    "repo": str(repo), "agent_cmd": "true", "validate_cmd": "true",
                    "round_timeout": 30,
                }
                ws.save_state(state)

                edit = self.ResponseCapture()
                D.Handler.api_edit_config(edit, {
                    "name": "bad-saved-number", "workspace_generation": state["workspace_generation"],
                    "round_timeout": float("nan"),
                })
                self.assertEqual(edit.response[0], 400)
                saved = json.loads(ws.state_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["config"]["round_timeout"], 30)

                saved["config"]["round_timeout"] = float("inf")
                ws.save_state(saved)
                run = self.ResponseCapture()
                D.Handler.api_run(run, {
                    "name": "bad-saved-number", "workspace_generation": state["workspace_generation"]})
                self.assertEqual(run.response[0], 400)
                self.assertIn("非法數值", run.response[1]["error"])
                self.assertNotIn("bad-saved-number", D.JOBS)
            finally:
                job = D.JOBS.pop("bad-saved-number", None)
                if job and job.alive():
                    job.stop(wait=True)
                D.ROOT, L.WORKSPACE_ROOT, D.load_config = old_values


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
                    [*LOOP_CMD, "--repo", str(repo), "--name", name,
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
                [*LOOP_CMD, "--repo", str(repo), "--name", "linked",
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
                    [*LOOP_CMD, "--repo", str(repo), "--name", f"protected-{index}",
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
                state = L.Workspace.__new__(L.Workspace).fresh_state()
                state.update(phase="done", config={"repo": str(repo), "goal": "../outside.md"})
                (dashboard_workspace / "state.json").write_text(json.dumps(state), encoding="utf-8")
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
                # 合法 v2 state 缺 config.repo → 明確 error
                state = L.Workspace.__new__(L.Workspace).fresh_state()
                (root / "demo" / "state.json").write_text(json.dumps(state), encoding="utf-8")
                self.assertIn("缺 repo 設定", D.read_goal("demo")["error"])
                # 壞 state 塞非字串 repo → 受控 error,不得拋 TypeError
                state["config"] = {"repo": 123}
                (root / "demo" / "state.json").write_text(json.dumps(state), encoding="utf-8")
                self.assertIn("缺 repo 設定", D.read_goal("demo")["error"])
                # 正常:回 goal 內容與路徑,goal_changed 透傳
                state.update(goal_changed=True, config={"repo": str(repo), "goal": "goal.md"})
                (root / "demo" / "state.json").write_text(json.dumps(state), encoding="utf-8")
                result = D.read_goal("demo")
                self.assertEqual(result["content"], "GOAL v1\n")
                self.assertTrue(result["goal_changed"])
                self.assertIn("舊版", result["diff_error"])
                # goal 被換成 symlink → 拒絕(路徑驗證 + O_NOFOLLOW 開檔雙防線),不得讀出連結目標
                secret = Path(td) / "secret.txt"
                secret.write_text("secret-content", encoding="utf-8")
                (repo / "goal.md").unlink()
                (repo / "goal.md").symlink_to(secret)
                result = D.read_goal("demo")
                self.assertIn("error", result)
                self.assertNotIn("secret-content", json.dumps(result, ensure_ascii=False))
                # goal 檔被移走 → 明確 error,不 crash
                (repo / "goal.md").unlink()
                self.assertIn("goal 檔不存在", D.read_goal("demo")["error"])
            finally:
                D.ROOT = old_root

    def test_loop_preserves_previous_hash_and_goal_projection_builds_git_diff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = make_repo(root)
            previous_content = (repo / "goal.md").read_bytes()
            previous_hash = L.sha256_bytes(previous_content)
            workspace_root = root / "workspace"
            old_loop_root = L.WORKSPACE_ROOT
            try:
                L.WORKSPACE_ROOT = workspace_root
                ws = L.Workspace("goal-diff")
                state = ws.fresh_state()
                state.update(plan=[{"order": 1, "task": "existing plan", "track": "main"}], plan_version=1,
                             goal_hash=previous_hash)
                ws.save_state(state)
            finally:
                L.WORKSPACE_ROOT = old_loop_root

            (repo / "goal.md").write_text("GOAL v2\n新增驗收條件\n", encoding="utf-8")
            git(repo, "add", "goal.md")
            git(repo, "commit", "-qm", "update goal")
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}
            result = subprocess.run(
                [*LOOP_CMD, "--repo", str(repo), "--name", "goal-diff",
                 "--agent-cmd", "true", "--validate-cmd", "true", "--max-rounds", "1"],
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            saved = json.loads((workspace_root / "goal-diff" / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["goal_changed"])
            self.assertEqual(saved["goal_previous_hash"], previous_hash)
            self.assertEqual(saved["goal_hash"], L.sha256_bytes((repo / "goal.md").read_bytes()))

            old_dashboard_root = D.ROOT
            D.ROOT = workspace_root
            try:
                projection = D.read_goal("goal-diff")
            finally:
                D.ROOT = old_dashboard_root
            self.assertNotIn("error", projection)
            self.assertEqual(projection["previous_hash"], previous_hash)
            self.assertEqual(projection["previous_content"], "GOAL v1\n")
            self.assertIn("--- goal.md（計畫基準）", projection["diff"])
            self.assertIn("-GOAL v1", projection["diff"])
            self.assertIn("+GOAL v2", projection["diff"])

            # 尚未重新收斂前再次修改 goal，差異基準仍必須是原計畫使用的 v1。
            (repo / "goal.md").write_text("GOAL v3\n再調整範圍\n", encoding="utf-8")
            git(repo, "add", "goal.md")
            git(repo, "commit", "-qm", "update goal again")
            result = subprocess.run(
                [*LOOP_CMD, "--repo", str(repo), "--name", "goal-diff",
                 "--agent-cmd", "true", "--validate-cmd", "true", "--max-rounds", "1"],
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            saved = json.loads((workspace_root / "goal-diff" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["goal_previous_hash"], previous_hash)
            D.ROOT = workspace_root
            try:
                projection = D.read_goal("goal-diff")
            finally:
                D.ROOT = old_dashboard_root
            self.assertIn("-GOAL v1", projection["diff"])
            self.assertIn("+GOAL v3", projection["diff"])


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

    def test_aggregate_uses_latest_500_samples_across_all_workspaces(self):
        samples = [{
            "workspace": "alpha" if index % 2 == 0 else "beta",
            "round": index,
            "seconds": float(index),
            "timed_out": index == 502,
            "missing_done": index in {10, 502},
            "timestamp": f"2026-07-10T{index:04d}",
        } for index in range(503)]
        metrics = D.aggregate_fleet_round_metrics(samples)
        self.assertEqual(metrics["limit"], 500)
        self.assertEqual(metrics["sample_count"], 500)
        self.assertEqual(metrics["workspace_count"], 2)
        self.assertEqual(metrics["average_seconds"], 252.5)
        self.assertEqual(metrics["p50_seconds"], 252)
        self.assertEqual(metrics["p95_seconds"], 477)
        self.assertEqual(metrics["max_seconds"], 502)
        self.assertEqual(metrics["slowest_workspace"], "alpha")
        self.assertEqual(metrics["timeout_count"], 1)
        self.assertEqual(metrics["timeout_rate_pct"], 0.2)
        self.assertEqual(metrics["missing_done_count"], 2)
        self.assertEqual(metrics["missing_done_rate_pct"], 0.4)

    def test_tail_and_current_task_projection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            (root / "alpha").mkdir(parents=True)
            (root / "beta").mkdir(parents=True)
            line = ("2026-07-10T10:00:00 round=1 phase=exec task=task-1 rc=0 secs=2.500 "
                    "timeout=False changed=False signal=- done_missing=True tamper=False agent_ok=True "
                    "validate=PASS flag=0 done=0")
            (root / "alpha" / "history.log").write_text(line + "\n", encoding="utf-8")
            state = L.Workspace.__new__(L.Workspace).fresh_state()
            state.update({
                "phase": "exec", "current_order": 2,
                "round_started_at": "2026-07-10T10:00:00",
                "round_deadline_at": "2026-07-10T10:30:00",
                "round_interrupted_at": None,
                "plan": [{"order": 1, "task": "第一項", "ref": None, "track": "main"},
                         {"order": 2, "task": "第二項很長" + "x" * 200, "ref": None, "track": "main"}],
            })
            (root / "alpha" / "state.json").write_text(json.dumps(state), encoding="utf-8")
            old_root = D.ROOT
            D.ROOT = root
            try:
                projection = D.read_fleet_observability()
                entries = projection["entries"]
                self.assertEqual([e["name"] for e in entries], ["alpha"], "沒 history 的 beta 應跳過")
                self.assertIn("task=task-1", entries[0]["data"])
                self.assertEqual(entries[0]["metrics"]["sample_count"], 1)
                self.assertEqual(entries[0]["metrics"]["average_seconds"], 2.5)
                self.assertEqual(entries[0]["metrics"]["p50_seconds"], 2.5)
                self.assertEqual(entries[0]["metrics"]["p95_seconds"], 2.5)
                self.assertEqual(entries[0]["metrics"]["missing_done_count"], 1)
                self.assertEqual(entries[0]["metrics"]["missing_done_rate_pct"], 100)
                self.assertEqual(projection["metrics"]["limit"], 500)
                self.assertEqual(projection["metrics"]["sample_count"], 1)
                self.assertEqual(projection["metrics"]["average_seconds"], 2.5)
                self.assertEqual(projection["metrics"]["missing_done_count"], 1)
                self.assertEqual(projection["metrics"]["missing_done_rate_pct"], 100)
                self.assertEqual([e["name"] for e in D.read_fleet_history()], ["alpha"])
                fleet = D.list_workspaces()
                alpha = next(w for w in fleet if w["name"] == "alpha")
                self.assertEqual(alpha["current_order"], 2)
                self.assertTrue(alpha["current_task"].startswith("第二項很長"))
                self.assertLessEqual(len(alpha["current_task"]), 121, "任務文字應截斷")
                self.assertEqual(alpha["round_started_at"], "2026-07-10T10:00:00")
                self.assertEqual(alpha["round_deadline_at"], "2026-07-10T10:30:00")
                self.assertIsNone(alpha["round_interrupted_at"])
            finally:
                D.ROOT = old_root


class TestAnomalyLogProjection(unittest.TestCase):
    class ResponseCapture:
        response = None
        _ws_dir = D.Handler._ws_dir

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    def test_workspace_and_global_lists_link_to_safe_preserved_log(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            workspace = root / "alpha"
            logs = workspace / "logs"
            logs.mkdir(parents=True)
            timestamp = "2026-07-10T10:00:00"
            (workspace / "history.log").write_text(
                f"{timestamp} round=7 phase=exec task=task-2 rc=0 secs=2.500 "
                "timeout=False changed=True signal=- done_missing=True tamper=False "
                "agent_ok=True validate=PASS flag=0 done=0\n",
                encoding="utf-8",
            )
            round_log = logs / "round-0007.log"
            round_log.write_text("preserved-agent-output\n", encoding="utf-8")
            metadata = L.preserve_anomaly_log(
                workspace, round_log, round_number=7, phase="exec",
                task="task-2", timestamp=timestamp,
            )
            old_root = D.ROOT
            D.ROOT = root
            try:
                workspace_projection = D.read_anomaly_records(workspace)
                self.assertEqual(workspace_projection["total_count"], 1)
                record = workspace_projection["records"][0]
                self.assertEqual(record["round"], 7)
                self.assertTrue(record["changed"])
                self.assertEqual(record["log_id"], metadata["id"])

                global_projection = D.read_anomaly_records()
                self.assertEqual(global_projection["total_count"], 1)
                self.assertEqual(global_projection["records"][0]["workspace"], "alpha")

                saved = D.read_preserved_anomaly_log(workspace, metadata["id"])
                self.assertIn("preserved-agent-output", saved["data"])

                handler = self.ResponseCapture()
                handler.path = "/api/anomalies?ws=alpha"
                D.Handler.do_GET(handler)
                self.assertEqual(handler.response[0], 200)
                self.assertEqual(handler.response[1]["records"][0]["log_id"], metadata["id"])

                handler = self.ResponseCapture()
                handler.path = f"/api/anomaly-log?ws=alpha&id={metadata['id']}"
                D.Handler.do_GET(handler)
                self.assertEqual(handler.response[0], 200)
                self.assertIn("preserved-agent-output", handler.response[1]["data"])

                unsafe = self.ResponseCapture()
                unsafe.path = "/api/anomaly-log?ws=alpha&id=.."
                D.Handler.do_GET(unsafe)
                self.assertEqual(unsafe.response[0], 400)
            finally:
                D.ROOT = old_root

    def test_workspace_list_returns_json_error_for_unsafe_anomaly_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            workspace = root / "alpha"
            (workspace / "logs").mkdir(parents=True)
            (workspace / "history.log").write_text(
                "2026-07-10T10:00:00 round=1 phase=exec task=task-1 rc=0 secs=1 "
                "timeout=False changed=False signal=- done_missing=True validate=PASS\n",
                encoding="utf-8",
            )
            (workspace / "logs" / "anomalies").symlink_to(root)
            old_root = D.ROOT
            D.ROOT = root
            try:
                handler = self.ResponseCapture()
                handler.path = "/api/anomalies?ws=alpha"
                D.Handler.do_GET(handler)
                self.assertEqual(handler.response[0], 400)
                self.assertIn("異常清單讀取失敗", handler.response[1]["error"])
                self.assertIn("symbolic link", handler.response[1]["error"])
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
            [*LOOP_CMD, "--repo", str(repo), "--name", name,
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
    """POST body 必須有界且格式錯誤不讀取額外資料。"""

    class FakeHandler:
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
