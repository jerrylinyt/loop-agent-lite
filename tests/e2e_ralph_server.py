#!/usr/bin/env python3
"""啟動隔離的 production Dashboard，內含真跑到完成的 ralph workspace，供 Playwright 驗證 RalphView。

fixture 準備：
1. clone snarktank/ralph（有網路時，滿足「真的 clone ralph」）；失敗則退回 tests/fixtures/fake_ralph.sh，
   兩者都是「真的 ralph.sh 迴圈」，只是 agent 用 fake（無網路/無 LLM 也能決定性完成）。
2. 隔離 PATH 放一支 fake `claude`（轉呼 fake_agent.py），推進 prd.json 的 story。
3. 真跑一次 `python -m engine.ralph` 到完成 → workspace「ralph-live」（completed）。
4. 另寫一份帶 usage_limit(waiting) 的 state → workspace「ralph-limit」，驗證用量上限橫幅渲染。
5. 起 Dashboard；Playwright 走真 API/SSE 驗證 RalphView 與 Ralph 啟動表單。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RALPH_URL = "https://github.com/snarktank/ralph.git"


def _run(*args, cwd=None, env=None, check=True, timeout=180):
    return subprocess.run(args, cwd=cwd and str(cwd), env=env, check=check,
                          capture_output=True, text=True, timeout=timeout)


def _prepare_ralph_repo(root: Path):
    """clone 真 ralph；失敗退回 fake_ralph.sh。回傳 (repo, ralph_script_cmd, args_style)。"""
    repo = root / "target"
    try:
        _run("git", "clone", "--depth", "1", RALPH_URL, str(repo), timeout=120)
        if (repo / "ralph.sh").is_file():
            # 真 snarktank ralph.sh：--tool <tool> <iters>
            return repo, f"bash {repo / 'ralph.sh'}", "snarktank"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        pass
    # 退回本地 fake ralph（仍是真 ralph.sh 迴圈語意，位置參數）。
    shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir(parents=True)
    _run("git", "init", "-q", cwd=repo)
    _run("git", "config", "user.email", "e2e@example.test", cwd=repo)
    _run("git", "config", "user.name", "ralph e2e", cwd=repo)
    shutil.copy(FIXTURES / "fake_ralph.sh", repo / "ralph.sh")
    (repo / "ralph.sh").chmod(0o755)
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-qm", "seed fake ralph", cwd=repo)
    return repo, f"bash {repo / 'ralph.sh'}", "positional"


def prepare_fixture():
    """建立隔離 repo、fake claude、真跑一次 ralph 到完成，並準備 usage-limit 展示 workspace。"""
    fixture = Path(tempfile.mkdtemp(prefix="ralph-e2e-ui-"))
    workspace = fixture / "workspace"
    workspace.mkdir()
    repo, ralph_cmd, args_style = _prepare_ralph_repo(fixture)

    # PRD：2 個 story。寫進 ralph 目錄（repo root）。
    prd = {
        "project": "E2E Ralph UI", "branchName": "ralph/e2e-ui",
        "userStories": [
            {"id": "US-1", "title": "第一個 e2e story", "description": "驗證 RalphView 檢核表。",
             "acceptanceCriteria": ["標記 passes=true", "progress 追加"], "priority": 1, "passes": False},
            {"id": "US-2", "title": "第二個 e2e story", "priority": 2, "passes": False},
        ],
    }
    (repo / "prd.json").write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")

    bindir = fixture / "bin"
    bindir.mkdir()
    claude = bindir / "claude"
    claude.write_text(
        "#!/usr/bin/env bash\n"
        f'exec {shutil.which("python3") or sys.executable} "{FIXTURES / "fake_agent.py"}" "$@"\n',
        encoding="utf-8")
    claude.chmod(0o755)

    env = {
        **os.environ,
        "LOOP_AGENT_WORKSPACE_ROOT": str(workspace),
        "PYTHONPATH": str(PROJECT_ROOT),
        "PATH": os.pathsep.join([str(bindir), os.environ.get("PATH", "")]),
        "FAKE_RALPH_SLEEP": "0",
    }
    # 真跑一次 ralph 監督層到完成 → workspace「ralph-live」。
    _run(sys.executable, "-m", "engine.ralph",
         "--repo", str(repo), "--name", "ralph-live", "--ralph-cmd", ralph_cmd,
         "--ralph-dir", str(repo), "--iterations", "6", "--tool", "claude",
         "--args-style", args_style, cwd=PROJECT_ROOT, env=env, check=False, timeout=120)

    _write_usage_limit_workspace(workspace / "ralph-limit", repo)

    config = {
        "agent_cmds": [{"label": "unused", "cmd": "true"}],
        "validate_cmds": [{"label": "green", "cmd": "true"}],
        "repo_roots": [str(fixture)],
        "extra_path_dirs": [str(bindir)],
        "ralph": {
            "scripts": [{"label": "e2e ralph", "cmd": ralph_cmd}],
            "tools": ["claude", "amp", "opencode"],
            "default_iterations": 6,
            "default_args_style": args_style,
            "prd_filenames": ["prd.json", "prd.md"],
            "default_usage_limit_action": "restart",
            "default_fallback_models": [],
            "default_auto_restart_max": 6,
            "usage_limit_patterns": [],
        },
    }
    config_path = fixture / "dashboard.config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return fixture, workspace, config_path


def _write_usage_limit_workspace(ws_dir: Path, repo: Path):
    """寫一份帶 usage_limit(waiting) 的合法 state，供 Playwright 驗證用量上限橫幅與倒數。"""
    ws_dir.mkdir(parents=True)
    (ws_dir / "logs").mkdir()
    resume_at = (datetime.now() + timedelta(minutes=25)).isoformat(timespec="seconds")
    state = {
        "runner": "ralph", "phase": "exec",
        "loop": {"pid": None, "session_id": "e2e", "started_at": datetime.now().isoformat(timespec="seconds")},
        "repo_binding": str(repo),
        "config": {"runner": "ralph", "repo": str(repo), "ralph_cmd": f"bash {repo/'ralph.sh'}",
                   "ralph_dir": str(repo), "iterations": 5000, "tool": "opencode", "model": "opus",
                   "args_template": ["{iterations}", "{tool}", "{model}"], "prd_path": "prd.json",
                   "notify_cmd": "", "usage_limit_action": "restart", "fallback_models": [],
                   "auto_restart_max": 6},
        "ralph": {
            "prd_format": "json", "prd_path": "prd.json", "project": "E2E Ralph UI",
            "branch_name": "ralph/e2e-ui",
            "stories": [{"id": "US-1", "title": "第一個 e2e story", "passes": True, "priority": 1},
                        {"id": "US-2", "title": "第二個 e2e story", "passes": False, "priority": 2}],
            "stories_total": 2, "stories_done": 1, "iteration": 37, "max_iterations": 5000,
            "base_sha": None, "head_sha": None, "commit_count": 12, "last_commit": "feat: US-1",
            "progress_bytes": 2048, "sentinel_complete": False, "stalled": False,
            "exit_code": None, "exit_reason": None, "prd_error": None,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "active_model": "opus", "restart_attempt": 2,
            "usage_limit": {
                "detection": "heuristic", "state": "waiting", "action": "waiting",
                "detected_at": datetime.now().isoformat(timespec="seconds"),
                "matched": "Claude usage limit reached|1893456000",
                "matches": [{"tier": 1, "source": "builtin", "pattern": "usage limit reached",
                             "line": "Claude usage limit reached|1893456000", "iteration": 37,
                             "at": datetime.now().isoformat(timespec="seconds")}],
                "iteration": 37, "resume_at": resume_at, "wait_until": resume_at,
                "reset_source": "parsed", "parsed_reset_at": resume_at, "wait_seconds": 1500,
                "from_model": None, "to_model": None, "restart_attempt": 2, "restarts_max": 6,
                "total_wait_secs": 300,
            },
        },
    }
    data = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    (ws_dir / "state.json").write_bytes(data)
    (ws_dir / "state.last-good.json").write_bytes(data)
    (ws_dir / "console.log").write_text("[00:00:00] ⏳ 用量上限(heuristic)｜等待重啟\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()
    fixture, workspace, config_path = prepare_fixture()
    os.environ["LOOP_AGENT_WORKSPACE_ROOT"] = str(workspace)
    os.environ["LOOP_AGENT_DASHBOARD_CONFIG"] = str(config_path)
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from engine import dashboard
        dashboard.run_dashboard(port=args.port, read_only=args.read_only)
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


if __name__ == "__main__":
    main()
