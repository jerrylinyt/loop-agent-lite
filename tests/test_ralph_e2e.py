#!/usr/bin/env python3
"""Ralph runner 端到端測試:真的 clone snarktank/ralph,用真 Dashboard HTTP + 真 ralph.sh 驅動。

流程:git clone snarktank/ralph → 起隔離的 production Dashboard → POST /api/launch(runner=ralph,
ralph_custom 指向 clone 的 ralph.sh,匯入 2-story prd.json)→ 輪詢 /api/state 直到 phase=done →
驗證 story 完成、exit_reason=completed,並確認 /api/ralph/prd 與 /api/ralph/progress 投影正常。

真正的 ralph.sh 會呼叫 `claude`;這裡用 tests/fixtures/fake_agent.py 包成一支同名 `claude`
放進隔離 PATH,推進 prd.json 的 story、commit、全部完成印 <promise>COMPLETE</promise>。
需要網路才能 clone;clone 失敗會整個 class skip(不讓離線環境誤判為失敗)。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RALPH_REPO_URL = "https://github.com/snarktank/ralph.git"
_HAS_TOOLS = all(shutil.which(tool) for tool in ("git", "bash", "jq"))


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _clone_ralph(dest: Path) -> bool:
    """淺 clone snarktank/ralph;成功回 True,網路/權限失敗回 False(class 會 skip)。"""
    result = subprocess.run(
        ["git", "clone", "--depth", "1", RALPH_REPO_URL, str(dest)],
        capture_output=True, text=True, timeout=120)
    return result.returncode == 0 and (dest / "ralph.sh").is_file()


def _post(port, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


@unittest.skipUnless(_HAS_TOOLS, "需要 git/bash/jq 才能跑 ralph 端到端 clone 測試")
class TestRalphDashboardE2E(unittest.TestCase):
    """真 clone + 真 Dashboard + 真 ralph.sh 的完整 runner 流程。"""

    @classmethod
    def setUpClass(cls):
        cls.fixture = Path(tempfile.mkdtemp(prefix="ralph-e2e-"))
        cls.clone = cls.fixture / "ralph"
        try:
            ok = _clone_ralph(cls.clone)
        except (subprocess.TimeoutExpired, OSError):
            ok = False
        if not ok:
            shutil.rmtree(cls.fixture, ignore_errors=True)
            raise unittest.SkipTest("無法 clone snarktank/ralph(可能無網路),跳過端到端測試")

        # 隔離 PATH 內的 fake `claude`:真正 ralph.sh 會呼叫它;此 wrapper 轉呼 fake_agent.py。
        cls.bindir = cls.fixture / "bin"
        cls.bindir.mkdir()
        claude = cls.bindir / "claude"
        claude.write_text(
            "#!/usr/bin/env bash\n"
            f'exec {shutil.which("python3") or sys.executable} "{FIXTURES / "fake_agent.py"}" "$@"\n',
            encoding="utf-8")
        claude.chmod(0o755)

        cls.workspace = cls.fixture / "workspace"
        cls.workspace.mkdir()
        config = {
            "agent_cmds": [{"label": "unused", "cmd": "true"}],
            "validate_cmds": [{"label": "green", "cmd": "true"}],
            "repo_roots": [str(cls.fixture)],
            "extra_path_dirs": [str(cls.bindir)],
            "ralph": {
                "scripts": [{"label": "cloned ralph", "cmd": f"bash {cls.clone / 'ralph.sh'}"}],
                "tools": ["claude", "amp"],
                "default_iterations": 6,
                "default_args_style": "snarktank",
                "prd_filenames": ["prd.json", "prd.md"],
                "usage_limit_patterns": [],
            },
        }
        cls.config_path = cls.fixture / "dashboard.config.json"
        cls.config_path.write_text(json.dumps(config), encoding="utf-8")

        cls.port = _free_port()
        env = {
            **os.environ,
            "LOOP_AGENT_WORKSPACE_ROOT": str(cls.workspace),
            "LOOP_AGENT_DASHBOARD_CONFIG": str(cls.config_path),
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": os.pathsep.join([str(cls.bindir), os.environ.get("PATH", "")]),
        }
        cls.proc = subprocess.Popen(
            [sys.executable, "-c",
             "from engine import dashboard; dashboard.run_dashboard(port=%d)" % cls.port],
            cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls._wait_until_up()

    @classmethod
    def _wait_until_up(cls):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if cls.proc.poll() is not None:
                raise RuntimeError("dashboard 提前結束:\n" + (cls.proc.stdout.read() if cls.proc.stdout else ""))
            try:
                _get(cls.port, "/api/bootstrap")
                return
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.3)
        raise RuntimeError("dashboard 未在時限內就緒")

    @classmethod
    def tearDownClass(cls):
        proc = getattr(cls, "proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(getattr(cls, "fixture", Path("/nonexistent")), ignore_errors=True)

    def test_launch_monitor_and_complete_real_ralph(self):
        prd = {
            "project": "E2E Ralph",
            "branchName": "ralph/e2e",
            "userStories": [
                {"id": "US-1", "title": "first e2e story", "priority": 1, "passes": False},
                {"id": "US-2", "title": "second e2e story", "priority": 2, "passes": False},
            ],
        }
        resp = _post(self.port, "/api/launch", {
            "runner": "ralph",
            "repo": str(self.clone),
            "name": "e2e",
            "ralph_custom": f"bash {self.clone / 'ralph.sh'}",
            "ralph_dir": str(self.clone),
            "iterations": 6,
            "tool": "claude",
            "args_style": "snarktank",
            "prd_content": json.dumps(prd),
            "prd_format": "json",
            "prd_path": "prd.json",
        })
        self.assertTrue(resp.get("ok"), resp)
        self.assertTrue(resp.get("starting"), resp)

        # 輪詢 state 直到 ralph 收斂(fake claude 每輪推進一個 story)。
        deadline = time.monotonic() + 120
        state = {}
        while time.monotonic() < deadline:
            state = _get(self.port, "/api/state?ws=e2e")
            if state.get("phase") == "done":
                break
            time.sleep(1)

        self.assertEqual(state.get("runner"), "ralph", state)
        self.assertEqual(state.get("phase"), "done",
                         f"ralph 未在時限內完成:{json.dumps(state.get('ralph', {}), ensure_ascii=False)}")
        block = state["ralph"]
        self.assertEqual(block["stories_total"], 2)
        self.assertEqual(block["stories_done"], 2)
        self.assertEqual(block["exit_reason"], "completed")
        self.assertTrue(block["sentinel_complete"])
        self.assertGreaterEqual(block["commit_count"], 2)
        self.assertIsNone(block["usage_limit"])

        # PRD 投影:兩個 story 都應 passes=true。
        prd_proj = _get(self.port, "/api/ralph/prd?ws=e2e")
        self.assertEqual(prd_proj["stories_total"], 2)
        self.assertEqual(prd_proj["stories_done"], 2)
        self.assertTrue(all(s["passes"] for s in prd_proj["stories"]))
        self.assertIn("US-1", prd_proj["raw"])

        # progress.txt 投影:真正 ralph.sh 會初始化並 append。
        progress = _get(self.port, "/api/ralph/progress?ws=e2e&offset=0")
        self.assertGreater(progress["size"], 0)
        self.assertIn("Ralph Progress Log", progress["data"])

        # 在 fleet 摘要中,workspace 應標記為 ralph runner。
        workspaces = _get(self.port, "/api/workspaces")
        entry = next((w for w in workspaces if w["name"] == "e2e"), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["runner"], "ralph")
        self.assertEqual(entry["ralph"]["stories_done"], 2)

    def test_coordinator_only_endpoint_rejected_for_ralph(self):
        # ralph workspace 已於前一個測試建立;coordinator 專屬操作應被擋。
        # (unittest 預設 method 名稱字母序:complete 測試會先跑並留下 e2e workspace。)
        try:
            _post(self.port, "/api/phase", {"name": "e2e", "phase": "exec"})
            rejected = False
        except urllib.error.HTTPError as e:
            rejected = e.code == 400
        self.assertTrue(rejected, "coordinator 專屬 /api/phase 應對 ralph workspace 回 400")


if __name__ == "__main__":
    unittest.main()
