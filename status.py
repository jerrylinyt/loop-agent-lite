#!/usr/bin/env python3
"""唯讀 workspace status CLI；直接投影 coordinator state，不啟動 loop、不修檔。"""

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import loop


def pid_is_loop_alive(pid) -> bool:
    """確認 state 記錄的 pid 仍是 loop.py，避免 pid reuse 誤報執行中。"""
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError, PermissionError):
        return False
    try:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, check=False).stdout
    except OSError:
        return True
    return "loop.py" in command


def project_status(name: str):
    """讀取主 state 或 checkpoint；repair=False 保證 CLI 是純唯讀投影。"""
    loop.require_workspace_name(name)
    directory = loop.workspace_path(loop.WORKSPACE_ROOT, name)
    try:
        info = directory.lstat()
    except FileNotFoundError as e:
        raise FileNotFoundError(f"workspace {name} 不存在") from e
    if not stat_is_directory(info.st_mode):
        raise ValueError(f"workspace {name} 必須是實體目錄")
    state, _data, recovered = loop.load_checkpointed_state(directory / "state.json", repair=False)
    loop_state = state.get("loop") if isinstance(state.get("loop"), dict) else {}
    pid = loop_state.get("pid")
    plan = state.get("plan") if isinstance(state.get("plan"), list) else []
    completed = state.get("completed") if isinstance(state.get("completed"), list) else []
    issues = state.get("issues") if isinstance(state.get("issues"), list) else []
    current_order = state.get("current_order")
    current_task = next((task.get("task", "") for task in plan
                         if isinstance(task, dict) and task.get("order") == current_order), "")
    if len(current_task) > 160:
        current_task = current_task[:160] + "…"
    return {
        "name": name,
        "workspace": str(directory),
        "phase": state.get("phase"),
        "round": state.get("round", 0),
        "flag": state.get("flag", 0),
        "done_count": state.get("done_count", 0),
        "plan_version": state.get("plan_version", 0),
        "plan_len": len(plan),
        "completed": len(completed),
        "current_order": current_order,
        "current_task": current_task,
        "red_streak": state.get("red_streak", 0),
        "stall_rounds": state.get("stall_rounds", 0),
        "agent_failure_streak": state.get("agent_failure_streak", 0),
        "issues": len(issues),
        "last_green_sha": state.get("last_green_sha"),
        "loop_pid": pid,
        "loop_session_id": loop_state.get("session_id"),
        "running": pid_is_loop_alive(pid),
        "state_recovery_pending": recovered,
    }


def stat_is_directory(mode: int) -> bool:
    return stat.S_ISDIR(mode) and not stat.S_ISLNK(mode)


def render_human(result, *, timestamp=False) -> None:
    phase = {"plan": "規劃期", "exec": "執行期", "done": "完成"}.get(result["phase"], result["phase"] or "未知")
    running = "執行中" if result["running"] else "已停止"
    prefix = f"[{time.strftime('%H:%M:%S')}] " if timestamp else ""
    task = f"｜task-{result['current_order']}：{result['current_task']}" if result.get("current_task") else ""
    print(f"{prefix}{result['name']}｜{phase}｜round {result['round']}｜"
          f"任務 {result['completed']}/{result['plan_len']}{task}｜{running}｜"
          f"紅連跳 {result['red_streak']}｜停滯 {result['stall_rounds']}｜issues {result['issues']}", flush=True)
    if result["state_recovery_pending"]:
        print("🛟 primary state 不可讀，目前只投影 last-good checkpoint（未修改檔案）", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="唯讀查詢 loop-agent-lite workspace 狀態")
    parser.add_argument("--name", required=True, help="workspace 名稱")
    parser.add_argument("--workspace-root", default=None,
                        help="workspace 根目錄（預設使用 LOOP_AGENT_WORKSPACE_ROOT 或專案 workspace）")
    parser.add_argument("--json", action="store_true", dest="as_json", help="輸出單行 JSON，供 shell/CI 使用")
    parser.add_argument("--watch", action="store_true", help="持續輪詢狀態，Ctrl-C 結束")
    parser.add_argument("--interval", type=float, default=2.0, help="--watch 輪詢秒數（預設 2）")
    args = parser.parse_args(argv)
    if not (args.interval > 0 and args.interval < float("inf")):
        parser.error("--interval 必須是有限正數")
    if args.workspace_root:
        loop.WORKSPACE_ROOT = Path(args.workspace_root).expanduser().resolve()
    try:
        while True:
            result = project_status(args.name)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
            else:
                render_human(result, timestamp=args.watch)
            if not args.watch:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except (FileNotFoundError, OSError, ValueError, loop.StateLoadError) as e:
        if args.as_json:
            print(json.dumps({"error": str(e)}, ensure_ascii=False), flush=True)
        else:
            print(f"❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
