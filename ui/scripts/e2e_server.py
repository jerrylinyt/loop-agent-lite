#!/usr/bin/env python3
"""啟動隔離的 production dashboard，供 Playwright 走真 API/SSE/loop/fake-agent。"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def prepare_fixture():
    fixture = Path(tempfile.mkdtemp(prefix="loop-lite-e2e-"))
    repos = fixture / "repos"
    repo = repos / "demo-repo"
    workspace = fixture / "workspace"
    repos.mkdir()
    repo.mkdir()
    workspace.mkdir()

    run("git", "init", "-q", cwd=repo)
    run("git", "config", "user.email", "e2e@example.test", cwd=repo)
    run("git", "config", "user.name", "loop-lite e2e", cwd=repo)
    (repo / "goal.md").write_text("E2E goal v1\n", encoding="utf-8")
    (repo / "README.md").write_text("# E2E fixture\n", encoding="utf-8")
    run("git", "add", "-A", cwd=repo)
    run("git", "commit", "-qm", "e2e fixture", cwd=repo)

    fake_agent = fixture / "fake_agent.py"
    work_py = PROJECT_ROOT / "work.py"
    fake_agent.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "sys.stdin.read()\n"
        "if 'LOOP_WS' not in os.environ:\n"
        "    print('E2E Agent CLI test result', flush=True)\n"
        "    raise SystemExit(0)\n"
        "ws = Path(os.environ['LOOP_WS'])\n"
        "phase = (ws / 'phase').read_text().strip()\n"
        "task = (ws / 'current_task').read_text().strip()\n"
        "print(f'E2E fake agent started phase={phase} task={task}', flush=True)\n"
        "print('\\x1b[32mE2E-ANSI-GREEN\\x1b[0m plain-tail', flush=True)\n"
        f"subprocess.run([sys.executable, {str(work_py)!r}, 'issue', 'E2E structured issue'], env=os.environ, check=True)\n"
        "if phase == 'plan':\n"
        "    marker = ws / '.e2e-plan-updated'\n"
        "    if not marker.exists():\n"
        "        plan = [\n"
        "            {'order': 1, 'task': '已由 E2E 更新的第一項功能', 'ref': 'README.md'},\n"
        "            {'order': 2, 'task': '由 Agent 重新分析的第二項功能'},\n"
        "        ]\n"
        f"        subprocess.run([sys.executable, {str(work_py)!r}, 'create-plan'], input=json.dumps(plan, ensure_ascii=False), text=True, env=os.environ, check=True)\n"
        "        marker.write_text('done')\n"
        "    else:\n"
        f"        subprocess.run([sys.executable, {str(work_py)!r}, 'plan-ok'], env=os.environ, check=True)\n"
        "elif task:\n"
        f"    subprocess.run([sys.executable, {str(work_py)!r}, 'done', task], env=os.environ, check=True)\n"
        "counter = ws / '.e2e-agent-count'\n"
        "count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        "time.sleep(2 if count == 2 else 0.45)\n",
        encoding="utf-8"
    )

    config = {
        "agent_cmds": [{"label": "fake agent", "cmd": shlex.join([sys.executable, str(fake_agent)])}],
        "validate_cmds": [{"label": "always green", "cmd": "true"}],
        "repo_roots": [str(repos)],
        "notify_cmd": "",
        "prompt_templates": [{
            "id": "e2e-team-analysis",
            "label": "E2E 團隊流程分析",
            "category": "團隊",
            "description": "E2E 團隊自訂模板",
            "requirement_placeholder": "請貼上 E2E 團隊需求",
            "instructions": "- 追蹤 E2E 團隊狀態真相來源。\n- 列出自訂驗證邊界。",
        }],
        "defaults": {
            "flag_threshold": 10,
            "done_threshold": 999,
            "round_timeout": 1,
            "agent_backoff_max": 60,
            "validate_timeout": 10,
            "red_limit": 20,
            "stall_limit": 300,
            "stuck_stop": False,
            "stuck_stop_count": 100
        }
    }
    config_path = fixture / "dashboard.config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return fixture, workspace, config_path


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
        import dashboard
        sys.argv = ["dashboard.py", "--port", str(args.port)] + (["--read-only"] if args.read_only else [])
        dashboard.main()
    finally:
        shutil.rmtree(fixture, ignore_errors=True)


if __name__ == "__main__":
    main()
