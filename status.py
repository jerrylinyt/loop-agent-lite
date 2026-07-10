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


def project_all_status():
    """投影 workspace root 下所有合法 workspace；單一壞 workspace 不阻斷其他結果。"""
    root = Path(loop.WORKSPACE_ROOT)
    try:
        info = root.lstat()
    except FileNotFoundError:
        return []
    if not stat_is_directory(info.st_mode):
        raise ValueError("workspace root 必須是實體目錄")
    results = []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if not loop.valid_workspace_name(entry.name):
            continue
        try:
            entry_info = entry.lstat()
            if not stat_is_directory(entry_info.st_mode):
                continue
            # Dashboard 只把至少有 state/checkpoint 的目錄視為 workspace；空的
            # mock/預留目錄不是錯誤，也不應污染 --all 的 fleet projection。
            if not any(path.exists() or path.is_symlink()
                       for path in (entry / "state.json", entry / "state.last-good.json")):
                continue
            results.append(project_status(entry.name))
        except (FileNotFoundError, OSError, ValueError, loop.StateLoadError) as e:
            results.append({"name": entry.name, "error": str(e)})
    return results


def summarize_status(results):
    """將 fleet projection 聚合成 shell/CI 可直接使用的摘要。"""
    valid = [result for result in results if "error" not in result]
    tasks_total = sum(result.get("plan_len", 0) for result in valid)
    tasks_completed = sum(result.get("completed", 0) for result in valid)
    return {
        "workspace_count": len(results),
        "valid_count": len(valid),
        "error_count": len(results) - len(valid),
        "running": sum(1 for result in valid if result.get("running")),
        "planning": sum(1 for result in valid if result.get("phase") == "plan"),
        "executing": sum(1 for result in valid if result.get("phase") == "exec"),
        "done": sum(1 for result in valid if result.get("phase") == "done"),
        "attention": sum(1 for result in valid if (
            result.get("red_streak", 0) > 0 or
            result.get("stall_rounds", 0) > 0 or
            result.get("issues", 0) > 0)),
        "issues": sum(result.get("issues", 0) for result in valid),
        "tasks_completed": tasks_completed,
        "tasks_total": tasks_total,
        "task_completion_pct": round(tasks_completed / tasks_total * 100) if tasks_total else 0,
    }


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


def render_fleet_summary(summary) -> None:
    """輸出 --all 的一行摘要，方便人類快速掌握 fleet 健康度。"""
    print(f"fleet｜workspaces {summary['workspace_count']}｜執行中 {summary['running']}｜"
          f"規劃/執行/完成 {summary['planning']}/{summary['executing']}/{summary['done']}｜"
          f"需關注 {summary['attention']}｜issues {summary['issues']}｜"
          f"任務 {summary['tasks_completed']}/{summary['tasks_total']} "
          f"({summary['task_completion_pct']}%)｜錯誤 {summary['error_count']}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="唯讀查詢 loop-agent-lite workspace 狀態")
    parser.add_argument("--name", default=None, help="workspace 名稱（與 --all 擇一）")
    parser.add_argument("--all", action="store_true", help="列出 workspace root 下全部合法 workspace")
    parser.add_argument("--workspace-root", default=None,
                        help="workspace 根目錄（預設使用 LOOP_AGENT_WORKSPACE_ROOT 或專案 workspace）")
    parser.add_argument("--json", action="store_true", dest="as_json", help="輸出單行 JSON，供 shell/CI 使用")
    parser.add_argument("--watch", action="store_true", help="持續輪詢狀態，Ctrl-C 結束")
    parser.add_argument("--interval", type=float, default=2.0, help="--watch 輪詢秒數（預設 2）")
    args = parser.parse_args(argv)
    if bool(args.name) == args.all:
        parser.error("--name 與 --all 必須且只能選一個")
    if not (args.interval > 0 and args.interval < float("inf")):
        parser.error("--interval 必須是有限正數")
    if args.workspace_root:
        loop.WORKSPACE_ROOT = Path(args.workspace_root).expanduser().resolve()
    try:
        while True:
            if args.all:
                results = project_all_status()
                summary = summarize_status(results)
                if args.as_json:
                    print(json.dumps({"summary": summary, "workspaces": results},
                                     ensure_ascii=False, separators=(",", ":")), flush=True)
                else:
                    render_fleet_summary(summary)
                    if not results:
                        print("（沒有合法 workspace）", flush=True)
                    for result in results:
                        if "error" in result:
                            print(f"❌ {result['name']}｜{result['error']}", flush=True)
                        else:
                            render_human(result, timestamp=args.watch)
            else:
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
