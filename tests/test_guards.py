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
import time
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
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root)}

            result = subprocess.run(
                [sys.executable, LOOP_PY, "--repo", str(repo), "--name", "reset-safe",
                 "--agent-cmd", "true", "--validate-cmd", "false", "--reset-state", "--max-rounds", "1"],
                capture_output=True, text=True, env=env,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(state_path.read_bytes(), before, "validate 失敗時舊 state 必須原封不動")
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
            (valid / "state.json").write_text(json.dumps(L.Workspace.__new__(L.Workspace).fresh_state()))
            old_root = D.ROOT
            try:
                D.ROOT = root
                self.assertEqual([item["name"] for item in D.list_workspaces()], ["valid"])
            finally:
                D.ROOT = old_root


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
        for method in ("api_launch", "api_run", "api_edit_state", "api_edit_config", "api_validate", "api_test_agent", "api_phase", "api_set_task"):
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
