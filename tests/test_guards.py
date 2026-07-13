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
                [sys.executable, "-c", parent], prompt.read_text(), root, os.environ.copy(),
                root / "agent.log", 0,
            )

            self.assertEqual(rc, 0)
            self.assertFalse(timed_out)
            self.assertLess(time.monotonic() - started, 2,
                            "背景 child 繼承 stdout 也不得把 round 卡到 child 自行結束")

    def test_prompt_is_injected_through_stdin_pipe(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            log_path = root / "agent.log"
            expected = "pipe prompt 測試\nsecond line\n"
            agent = (
                "import json, os, stat, sys; "
                "print(json.dumps({'pipe': stat.S_ISFIFO(os.fstat(0).st_mode), "
                "'prompt': sys.stdin.read(), 'prompt_file': os.environ.get('LOOP_PROMPT_FILE')}))"
            )
            env = os.environ.copy()
            env.pop("LOOP_PROMPT_FILE", None)

            rc, _secs, timed_out = L.run_agent(
                [sys.executable, "-c", agent], expected, root, env, log_path, 5,
            )

            payload = json.loads(log_path.read_text())
            self.assertEqual(rc, 0)
            self.assertFalse(timed_out)
            self.assertTrue(payload["pipe"], "Agent stdin 必須是 pipe，不能是開啟的 prompt 檔案")
            self.assertEqual(payload["prompt"], expected)
            self.assertIsNone(payload["prompt_file"])


class TestAgentCoordinatorCommands(unittest.TestCase):
    """送進 prompt 的 coordinator 指令必須鎖定啟動 loop 的同一套 Python。"""

    def test_all_commands_include_absolute_python_executable(self):
        expected = str(Path(sys.executable).expanduser().resolve())
        commands = (
            L.coordinator_command("create-plan"),
            L.coordinator_command("plan-ok"),
            L.coordinator_command("issue"),
            L.coordinator_command("done", "task-7"),
        )
        for command in commands:
            with self.subTest(command=command):
                parts = shlex.split(command)
                self.assertEqual(parts[0], expected)
                self.assertTrue(Path(parts[0]).is_absolute())
                self.assertEqual(parts[1:3], ["-m", "engine.work"])

    def test_python_path_with_spaces_is_shell_quoted(self):
        executable = Path("/tmp/Python Runtime/bin/python3").resolve()
        command = L.coordinator_command("plan-ok", python_executable=str(executable))
        self.assertEqual(
            shlex.split(command),
            [str(executable), "-m", "engine.work", "plan-ok"],
        )


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
            plan.write_text('[{"order": 1, "task": "implement feature"}]')
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
            plan.write_text('[{"order": 1, "task": "only task"}]')
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

    def test_loop_pauses_at_exec_start_and_resumes_into_exec(self):
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
                handler = self.ResponseCapture()
                D.Handler.api_launch(handler, {
                    "repo": str(repo), "name": "pause-flag", "agent_idx": 0,
                    "validate_idx": 0,
                })
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertTrue(captured["pause_after_plan"])

                # run:從 state.config 帶回同一開關
                ws = L.Workspace("pause-flag")
                state = ws.fresh_state()
                state["config"] = {
                    "repo": str(repo), "agent_cmd": "true", "validate_cmd": "true",
                    "pause_after_plan": True,
                }
                ws.save_state(state)
                handler = self.ResponseCapture()
                D.Handler.api_run(handler, {"name": "pause-flag"})
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertTrue(captured["pause_after_plan"])

                # edit-config:停止狀態可切換,下一次運行生效
                handler = self.ResponseCapture()
                D.Handler.api_edit_config(handler, {
                    "name": "pause-flag", "pause_after_plan": False,
                })
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertIn("pause_after_plan=off", handler.response[1]["changed"])
                saved = json.loads(ws.state_path.read_text(encoding="utf-8"))
                self.assertFalse(saved["config"]["pause_after_plan"])
                handler = self.ResponseCapture()
                D.Handler.api_run(handler, {"name": "pause-flag"})
                self.assertEqual(handler.response[0], 200, handler.response)
                self.assertFalse(captured["pause_after_plan"])
            finally:
                D.JOBS.pop("pause-flag", None)
                D.ROOT, L.WORKSPACE_ROOT, D.load_config, D.spawn_loop = old_values


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

    def test_invalid_plan_and_completed_entries_fail_closed(self):
        invalid_states = [
            {"phase": "plan", "plan": [{"order": 0, "task": "bad"}]},
            {"phase": "plan", "plan": [
                {"order": 1, "task": "one"}, {"order": 1, "task": "duplicate"},
            ]},
            {"phase": "plan", "plan": [{"order": 1, "task": "bad ref", "ref": 3}]},
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
                state.update(round=4, plan=[{"order": 1, "task": "one"}], current_order=1,
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
            previous["plan"] = [{"order": 1, "task": "must be cleared"}]
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


class TestStoppedWorkspacePlanImport(unittest.TestCase):
    """設定內匯入只接受純 plan，成功時完整 reset，失敗時不得改 state。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self._out(code, json.dumps({"error": msg}, ensure_ascii=False))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        self.old_roots = D.ROOT, L.WORKSPACE_ROOT
        D.ROOT = L.WORKSPACE_ROOT = self.root
        self.workspace = L.Workspace("demo")
        state = self.workspace.fresh_state()
        state.update(
            phase="done", round=19, flag=3, plan_version=4, current_order=2,
            done_count=2, plan=[{"order": 1, "task": "old task", "ref": None}],
            completed=[{"order": 1, "sha": "a" * 40, "round": 8}],
            issues=[{"round": 9, "where": "task-1", "text": "old issue"}],
            config={"repo": "/target/repo", "agent_cmd": "agent", "validate_cmd": "true"},
        )
        self.workspace.save_state(state)
        self.workspace.history.write_text("old history\n", encoding="utf-8")
        (self.workspace.dir / "REPORT.md").write_text("old report\n", encoding="utf-8")
        (self.workspace.dir / "pending_issues").write_text("old pending\n", encoding="utf-8")
        (self.workspace.dir / "logs" / "round-0019.log").write_text("old log\n", encoding="utf-8")
        (self.workspace.dir / "prompts" / "round-0019.md").write_text("old prompt\n", encoding="utf-8")

    def tearDown(self):
        D.ROOT, L.WORKSPACE_ROOT = self.old_roots
        self.temp.cleanup()

    def call(self, plan_json):
        handler = self.ResponseCapture()
        D.Handler.api_import_plan(handler, {"name": "demo", "plan_json": plan_json})
        return handler.response

    def test_imports_pure_plan_and_resets_all_progress(self):
        response = self.call(json.dumps([
            {"order": 1, "task": "split one", "ref": "spec.md#one"},
            {"order": 2, "task": "split two"},
        ]))
        self.assertEqual(response[0], 200, response)
        self.assertEqual(response[1]["plan_count"], 2)
        saved, error = D.read_state("demo", repair=False)
        self.assertIsNone(error)
        self.assertEqual(saved["phase"], "plan")
        self.assertEqual(saved["round"], 0)
        self.assertEqual(saved["plan_version"], 1)
        self.assertEqual(saved["current_order"], 0)
        self.assertEqual(saved["completed"], [])
        self.assertEqual(saved["issues"], [])
        self.assertEqual(saved["plan"][0]["ref"], "spec.md#one")
        self.assertEqual(saved["config"]["repo"], "/target/repo")
        self.assertEqual((self.workspace.dir / "history.log.1").read_text(), "old history\n")
        for stale in (
            self.workspace.history,
            self.workspace.dir / "REPORT.md",
            self.workspace.dir / "pending_issues",
            self.workspace.dir / "logs" / "round-0019.log",
            self.workspace.dir / "prompts" / "round-0019.md",
        ):
            self.assertFalse(stale.exists(), f"完整 reset 應清除 {stale.name}")

    def test_rejects_state_object_without_mutating_workspace(self):
        before_state = self.workspace.state_path.read_bytes()
        before_history = self.workspace.history.read_bytes()
        response = self.call(json.dumps({
            "plan": [{"order": 1, "task": "stolen"}],
            "completed": [{"order": 1, "sha": "b" * 40, "round": 1}],
        }))
        self.assertEqual(response[0], 400)
        self.assertIn("非空陣列", response[1]["error"])
        self.assertEqual(self.workspace.state_path.read_bytes(), before_state)
        self.assertEqual(self.workspace.history.read_bytes(), before_history)
        self.assertFalse((self.workspace.dir / "history.log.1").exists())

    def test_rejects_import_while_workspace_is_running(self):
        original = D.ws_running
        try:
            D.ws_running = lambda _name, _state=None: True
            response = self.call('[{"order":1,"task":"new"}]')
        finally:
            D.ws_running = original
        self.assertEqual(response[0], 400)
        self.assertIn("先停止", response[1]["error"])


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
            (healthy / "state.json").write_text(json.dumps({
                "phase": "done", "stall_rounds": 2, "red_streak": 1,
            }), encoding="utf-8")
            attention = root / "attention"
            attention.mkdir()
            (attention / "state.json").write_text(json.dumps({
                "phase": "exec", "red_streak": 2,
                "issues": [{"round": 1, "text": "a"}, {"round": 2, "text": "b"}],
                "agent_failure_streak": 3, "state_recovery_count": 4,
                "last_round_seconds": 60.2, "last_round_timed_out": True,
                "state_recovery_pending": True, "goal_changed": True,
                "loop": {"pid": 99999999, "session_id": "stale", "started_at": "2026-07-10T20:00:00"},
            }), encoding="utf-8")
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

    def test_sse_incremental_backlog_keeps_only_latest_complete_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "console.log"
            path.write_text("old-" + "x" * 40 + "\nlatest-1\nlatest-2\n", encoding="utf-8")

            projection = D.read_incremental(
                path, 0, max_bytes=24, tail_if_oversized=True,
            )

            self.assertTrue(projection["truncated"])
            self.assertEqual(projection["size"], path.stat().st_size)
            self.assertNotIn("old-", projection["data"])
            self.assertEqual(projection["data"], "latest-1\nlatest-2\n")

    def test_sse_incremental_drops_one_oversized_partial_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "console.log"
            path.write_text("[time] 🤖 Agent｜" + "x" * 100, encoding="utf-8")

            projection = D.read_incremental(
                path, 0, max_bytes=24, tail_if_oversized=True,
            )

            self.assertTrue(projection["truncated"])
            self.assertEqual(projection["size"], path.stat().st_size)
            self.assertEqual(projection["data"], "")

            initial = D.read_incremental(path, -1, max_bytes=24, tail_if_oversized=True)
            self.assertTrue(initial["truncated"])
            self.assertEqual(initial["size"], path.stat().st_size)
            self.assertEqual(initial["data"], "")

    def test_sse_push_interval_is_three_seconds(self):
        self.assertEqual(D.SSE_PUSH_INTERVAL, 3.0)
        self.assertEqual(D.FLEET_HISTORY_SSE_INTERVAL, 3.0)


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
                D.Handler.api_edit_state(handler, {"name": "demo", "ack_issues": True})
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
                D.Handler.api_edit_state(clear, {"name": "demo", "clear_issues": True})
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
            plan=[{"order": order, "task": f"task {order}", "ref": None}
                  for order in range(1, 5)],
            completed=[{"order": 1, "sha": "a" * 40, "round": 2}],
            task_reset_counts={"1": 1, "3": 4},
        )
        D.write_state("demo", state)

    def tearDown(self):
        D.ROOT, L.WORKSPACE_ROOT = self.old_roots
        self.temp.cleanup()

    def call(self, tasks, version=7):
        handler = self.ResponseCapture()
        D.Handler.api_edit_state(handler, {
            "name": "demo", "plan_edit": True, "plan_version": version, "tasks": tasks,
        })
        return handler.response

    def test_reorders_deletes_and_inserts_only_after_current_task(self):
        response = self.call([
            {"order": 1, "task": "task 1", "ref": None},
            {"order": 2, "task": "task 2", "ref": None},
            {"order": 4, "task": "task 4 moved", "ref": None},
            {"order": None, "task": "inserted task", "ref": "spec.md"},
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
            {"order": 2, "task": "task 2", "ref": None},
            {"order": 1, "task": "task 1", "ref": None},
            {"order": 3, "task": "task 3", "ref": None},
            {"order": 4, "task": "task 4", "ref": None},
        ])
        self.assertEqual(moved[0], 400)
        self.assertIn("不可移動", moved[1]["error"])
        stale = self.call([
            {"order": order, "task": f"task {order}", "ref": None}
            for order in range(1, 5)
        ], version=6)
        self.assertEqual(stale[0], 409)
        self.assertIn("請重新載入", stale[1]["error"])

        modified = self.call([
            {"order": 1, "task": "改寫已完成任務", "ref": None},
            {"order": 2, "task": "task 2", "ref": None},
            {"order": 3, "task": "task 3", "ref": None},
            {"order": 4, "task": "task 4", "ref": None},
        ])
        self.assertEqual(modified[0], 400)
        self.assertIn("不可修改內容", modified[1]["error"])


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
                        {"order": 1, "task": "first", "ref": None},
                        {"order": 2, "task": "second", "ref": None},
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
                D.Handler.api_set_task(handler, {"name": "manual-progress", "order": 2})
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
               input='[{{"order":1,"task":"stolen dirty task"}}]', text=True, env=dict(os.environ))
'''
_AGENT_CLEAN = f'''import os, subprocess, sys
sys.stdin.read()
subprocess.run([sys.executable, "-m", "engine.work", "create-plan"],
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


class TestWorkspaceDelete(unittest.TestCase):
    """停止 workspace 可永久刪除；執行、鎖定或不安全路徑一律 fail-closed。"""

    class ResponseCapture:
        response = None

        def _out(self, code, body, _ctype="application/json; charset=utf-8"):
            self.response = code, json.loads(body)

        def _err(self, msg, code=400):
            self.response = code, {"error": msg}

    @staticmethod
    def _seed_workspace(root, name="demo"):
        workspace = root / name
        workspace.mkdir(parents=True)
        (workspace / "state.json").write_text(
            json.dumps({"phase": "done", "round": 7, "loop": {"pid": None}}), encoding="utf-8")
        (workspace / "nested").mkdir()
        (workspace / "nested" / "content.txt").write_text("workspace data", encoding="utf-8")
        return workspace

    def test_refuses_running_and_held_lock_then_deletes_full_tree(self):
        import fcntl
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            workspace = self._seed_workspace(root)
            outside = Path(td) / "outside"
            outside.mkdir()
            (outside / "must-survive.txt").write_text("outside")
            old_root = D.ROOT
            D.ROOT = root
            try:
                old_running = D.ws_running
                D.ws_running = lambda *args, **kwargs: True
                try:
                    running = self.ResponseCapture()
                    D.Handler.api_delete_workspace(running, {"name": "demo"})
                finally:
                    D.ws_running = old_running
                self.assertEqual(running.response[0], 400)
                self.assertTrue(workspace.exists())

                holder = open(workspace / ".run.lock", "a+b")
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    locked = self.ResponseCapture()
                    D.Handler.api_delete_workspace(locked, {"name": "demo"})
                finally:
                    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                    holder.close()
                self.assertEqual(locked.response[0], 409, locked.response)
                self.assertIn("單 writer 鎖", locked.response[1]["error"])
                self.assertTrue(workspace.exists())

                (workspace / "escape").symlink_to(outside, target_is_directory=True)
                deleted = self.ResponseCapture()
                D.Handler.api_delete_workspace(deleted, {"name": "demo"})
                self.assertEqual(deleted.response[0], 200, deleted.response)
                self.assertEqual(deleted.response[1], {"ok": True, "name": "demo", "deleted": True})
                self.assertFalse(workspace.exists())
                self.assertTrue((outside / "must-survive.txt").exists())
                self.assertEqual(D.list_workspaces(), [])
                self.assertEqual([path for path in root.iterdir() if path.name.startswith(".delete-")], [])
            finally:
                D.ROOT = old_root

    def test_rejects_invalid_name_symlink_workspace_and_root_operation_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "workspace"
            workspace = self._seed_workspace(root)
            old_root = D.ROOT
            D.ROOT = root
            try:
                bad = self.ResponseCapture()
                D.Handler.api_delete_workspace(bad, {"name": "../escape"})
                self.assertEqual(bad.response[0], 400)
                self.assertTrue(workspace.exists())

                with L.workspace_operation_lock(root, "demo"):
                    locked = self.ResponseCapture()
                    D.Handler.api_delete_workspace(locked, {"name": "demo"})
                self.assertEqual(locked.response[0], 409, locked.response)
                self.assertTrue(workspace.exists())

                shutil.rmtree(workspace)
                outside = Path(td) / "outside-workspace"
                self._seed_workspace(outside, name="data")
                workspace.symlink_to(outside / "data", target_is_directory=True)
                symlink = self.ResponseCapture()
                D.Handler.api_delete_workspace(symlink, {"name": "demo"})
                self.assertIn(symlink.response[0], (400, 409))
                self.assertTrue(workspace.is_symlink())
                self.assertTrue((outside / "data" / "state.json").exists())
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
            plan.write_text(json.dumps([{"order": 1, "task": "不得被零門檻跳過"}]))
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
                    "name": "bad-saved-number", "round_timeout": float("nan"),
                })
                self.assertEqual(edit.response[0], 400)
                saved = json.loads(ws.state_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["config"]["round_timeout"], 30)

                saved["config"]["round_timeout"] = float("inf")
                ws.save_state(saved)
                run = self.ResponseCapture()
                D.Handler.api_run(run, {"name": "bad-saved-number"})
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
                # 壞 state 塞非字串 repo → 受控 error,不得拋 TypeError
                (root / "demo" / "state.json").write_text(json.dumps(
                    {"phase": "plan", "config": {"repo": 123}}), encoding="utf-8")
                self.assertIn("缺 repo 設定", D.read_goal("demo")["error"])
                # 正常:回 goal 內容與路徑,goal_changed 透傳
                (root / "demo" / "state.json").write_text(json.dumps(
                    {"phase": "plan", "goal_changed": True,
                     "config": {"repo": str(repo), "goal": "goal.md"}}), encoding="utf-8")
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
                state.update(plan=[{"order": 1, "task": "existing plan"}], plan_version=1,
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
            (root / "alpha" / "state.json").write_text(json.dumps({
                "phase": "exec", "current_order": 2,
                "round_started_at": "2026-07-10T10:00:00",
                "round_deadline_at": "2026-07-10T10:30:00",
                "round_interrupted_at": None,
                "plan": [{"order": 1, "task": "第一項", "ref": None},
                         {"order": 2, "task": "第二項很長" + "x" * 200, "ref": None}],
            }), encoding="utf-8")
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


class TestSafeKillGuards(unittest.TestCase):
    """safe_kill/safe_killpg:pid/pgid 被污染成 -1/0/1 或指向自己的 group 時,
    絕不能把 signal 送出去(kernel 語意是殺自己整組甚至全機),只能記 log 放行流程。"""

    def test_safe_kill_blocks_wildcard_pids(self):
        for pid in (-1, 0, 1):
            with self.subTest(pid=pid):
                self.assertFalse(L.safe_kill(pid, signal.SIGKILL))

    def test_safe_killpg_blocks_wildcard_and_own_group(self):
        for pgid in (-1, 0, 1):
            with self.subTest(pgid=pgid):
                self.assertFalse(L.safe_killpg(pgid, signal.SIGKILL))
        # 自己所在的 group = start_new_session 沒生效的災難場景,必須攔下
        self.assertFalse(L.safe_killpg(os.getpgid(0), signal.SIGKILL))

    def test_safe_kill_delivers_to_real_child(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.assertTrue(L.safe_kill(p.pid, signal.SIGKILL))
            self.assertEqual(p.wait(timeout=5), -signal.SIGKILL)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()

    def test_safe_killpg_delivers_to_detached_group(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
        try:
            self.assertTrue(L.safe_killpg(p.pid, signal.SIGKILL))
            self.assertEqual(p.wait(timeout=5), -signal.SIGKILL)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()

    def test_safe_kill_raises_lookup_error_like_os_kill(self):
        """已死目標仍要丟 ProcessLookupError,呼叫端既有的 except 分支才接得住。"""
        p = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        p.wait(timeout=10)
        with self.assertRaises(ProcessLookupError):
            L.safe_kill(p.pid, signal.SIGKILL)
        with self.assertRaises(ProcessLookupError):
            L.safe_killpg(p.pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
