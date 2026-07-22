#!/usr/bin/env python3
"""唯讀 workspace status CLI；直接投影 coordinator state，不啟動 loop、不修檔。"""

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from engine import loop
from engine import platform_compat as compat

STATUS_SCHEMA_VERSION = 1


def _parse_timestamp(value):
    """容錯解析 ISO timestamp；缺少 timezone 時沿用本機時區，無效值回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def round_timing_projection(started_at, deadline_at=None, interrupted_at=None, *, now=None):
    """將 state 的靜態時間戳投影為 elapsed/remaining；壞格式不讓唯讀 status 崩潰。"""
    started = _parse_timestamp(started_at)
    if started is None:
        return {"round_elapsed_seconds": None, "round_remaining_seconds": None}
    interrupted = _parse_timestamp(interrupted_at)
    if now is None:
        now = datetime.now(started.tzinfo) if started.tzinfo else datetime.now()
    end = interrupted or now
    if end.tzinfo != started.tzinfo:
        end = now
    elapsed = max(0, round((end - started).total_seconds()))
    deadline = _parse_timestamp(deadline_at)
    remaining = None
    if deadline is not None and deadline.tzinfo == end.tzinfo:
        remaining = round((deadline - end).total_seconds())
    return {"round_elapsed_seconds": elapsed, "round_remaining_seconds": remaining}


def format_clock(seconds):
    """將秒數格式化成適合終端狀態列的短時間。"""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def pid_is_loop_alive(pid) -> bool:
    """確認 state 記錄的 pid 仍是 coordinator，兼容舊檔案入口與新 module 入口。"""
    try:
        pid = int(pid)
        if not compat.process_looks_like_python(pid):
            return False
    except (TypeError, ValueError, ProcessLookupError, PermissionError):
        return False
    if compat.IS_WINDOWS:
        return True
    try:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, check=False).stdout
    except OSError:
        return True
    return any(marker in command for marker in (
        "loop.py", "engine.loop", "engine.parallel", "engine.ralph"))


def project_status(name: str, metrics_limit=0):
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
    state_running = pid_is_loop_alive(pid)
    # run 在 preflight/Validate 完成後才把 session PID 交易式寫進 state；這段期間仍可
    # 由 kernel flock 與 owner record 誠實辨識為 starting/busy，避免錯顯示「已停止」。
    lock_owner = loop.active_run_lock_owner(directory / ".run.lock")
    owner_pid = lock_owner.get("pid") if isinstance(lock_owner, dict) else None
    starting = bool(not state_running and owner_pid and pid_is_loop_alive(owner_pid))
    running = state_running or starting
    effective_pid = pid if state_running else owner_pid if starting else pid
    plan = state.get("plan") if isinstance(state.get("plan"), list) else []
    completed = state.get("completed") if isinstance(state.get("completed"), list) else []
    issues = state.get("issues") if isinstance(state.get("issues"), list) else []
    current_order = state.get("current_order")
    current_task = next((task.get("task", "") for task in plan
                         if isinstance(task, dict) and task.get("order") == current_order), "")
    if len(current_task) > 160:
        current_task = current_task[:160] + "…"
    round_started_at = state.get("round_started_at")
    round_deadline_at = state.get("round_deadline_at")
    round_interrupted_at = state.get("round_interrupted_at")
    round_timing = round_timing_projection(round_started_at, round_deadline_at, round_interrupted_at)
    projection = {
        "name": name,
        "workspace": str(directory),
        "runner": state.get("runner") or "loop",
        "managed_readonly": bool(state.get("managed_readonly")),
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
        "agent_backoff_seconds": state.get("agent_backoff_seconds", 0),
        "last_round_seconds": state.get("last_round_seconds", 0),
        "last_round_timed_out": bool(state.get("last_round_timed_out")),
        "round_started_at": round_started_at,
        "round_deadline_at": round_deadline_at,
        "round_interrupted_at": round_interrupted_at,
        "round_active": bool(round_started_at and state_running),
        "round_interrupted": bool(round_started_at and not state_running),
        **round_timing,
        "state_recovery_count": state.get("state_recovery_count", 0),
        "last_state_recovery": state.get("last_state_recovery"),
        "goal_changed": bool(state.get("goal_changed")),
        "goal_previous_hash": state.get("goal_previous_hash"),
        "issues": len(issues),
        "unread_issues": loop.unread_issue_count(state),
        "last_green_sha": state.get("last_green_sha"),
        "loop_pid": effective_pid,
        "loop_session_id": loop_state.get("session_id") if state_running else None,
        "loop_started_at": (lock_owner.get("started_at") if starting
                            else loop_state.get("started_at")),
        "starting": starting,
        "running": running,
        "stale_loop_pid": pid is not None and not state_running and not starting,
        "state_recovery_pending": recovered,
    }
    if projection["runner"] == "parallel-supervisor":
        parallel = state.get("parallel") if isinstance(state.get("parallel"), dict) else {}
        projection["parallel"] = {
            "run_id": parallel.get("run_id"),
            "status": parallel.get("status"),
            "terminal_intent": parallel.get("terminal_intent"),
            "batch": parallel.get("batch"),
            "tasks": list(parallel.get("tasks") or []),
            "error": parallel.get("error"),
        }
    elif (projection["runner"] == "parallel-worker"
          or projection["managed_readonly"]):
        # The readonly marker is the fail-safe authority for legacy worker
        # projections whose runner string predates the parallel-worker enum.
        projection["runner"] = "parallel-worker"
        assignment = state.get("assignment") if isinstance(state.get("assignment"), dict) else {}
        assigned_order = state.get("assigned_order")
        assigned_task = next((task for task in plan
                              if isinstance(task, dict)
                              and task.get("order") == assigned_order), None)
        assigned_text = (assigned_task.get("task", "")
                         if isinstance(assigned_task, dict) else "")
        if len(assigned_text) > 160:
            assigned_text = assigned_text[:160] + "…"
        assigned_complete = any(
            (entry.get("order") if isinstance(entry, dict) else entry) == assigned_order
            for entry in completed)
        # A managed worker owns exactly one assignment.  Its readonly status
        # must not imply that it can see or execute the parent frozen plan.
        projection.update({
            "plan_len": 1 if assigned_task is not None else 0,
            "completed": int(assigned_complete),
            "current_order": assigned_order,
            "current_task": assigned_text,
        })
        projection.update({
            "parent_workspace": state.get("parent_workspace"),
            "run_id": state.get("run_id"),
            "assigned_order": assigned_order,
            "assignment": dict(assignment),
        })
    if metrics_limit:
        projection["round_metrics"] = loop.read_round_metrics(directory / "history.log", metrics_limit)
    return projection


def stat_is_directory(mode: int) -> bool:
    """直接判斷 lstat mode 是否為目錄，不跟隨 symlink。"""
    return stat.S_ISDIR(mode) and not stat.S_ISLNK(mode)


def project_all_status(metrics_limit=0):
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
            projected = project_status(entry.name, metrics_limit)
            if (projected.get("runner") == "parallel-worker"
                    or projected.get("managed_readonly")):
                continue
            results.append(projected)
        except (FileNotFoundError, OSError, ValueError, loop.StateLoadError) as e:
            results.append({"name": entry.name, "error": str(e)})
    return results


def summarize_status(results):
    """將 fleet projection 聚合成 shell/CI 可直接使用的摘要。"""
    fleet_results = [
        result for result in results
        if (result.get("runner") != "parallel-worker"
            and not result.get("managed_readonly"))
    ]
    valid = [
        result for result in fleet_results if "error" not in result
    ]
    tasks_total = sum(result.get("plan_len", 0) for result in valid)
    tasks_completed = sum(result.get("completed", 0) for result in valid)
    return {
        "workspace_count": len(fleet_results),
        "valid_count": len(valid),
        "error_count": len(fleet_results) - len(valid),
        "running": sum(1 for result in valid if result.get("running")),
        "planning": sum(1 for result in valid if result.get("phase") == "plan"),
        "executing": sum(1 for result in valid if result.get("phase") == "exec"),
        "done": sum(1 for result in valid if result.get("phase") == "done"),
        "attention": sum(1 for result in valid if projection_needs_attention(result)),
        "issues": sum(result.get("issues", 0) for result in valid),
        "unread_issues": sum(result.get("unread_issues", result.get("issues", 0)) for result in valid),
        "agent_failures": sum(result.get("agent_failure_streak", 0) for result in valid),
        "round_timeouts": sum(1 for result in valid if result.get("last_round_timed_out")),
        "state_recoveries": sum(result.get("state_recovery_count", 0) for result in valid),
        "goal_changes": sum(1 for result in valid if result.get("goal_changed")),
        "stale_loops": sum(1 for result in valid if result.get("stale_loop_pid")),
        "tasks_completed": tasks_completed,
        "tasks_total": tasks_total,
        "task_completion_pct": round(tasks_completed / tasks_total * 100) if tasks_total else 0,
    }


def projection_needs_attention(result) -> bool:
    """判斷單一 projection 是否有目前仍需處理的狀況。

    完成前的紅燈、停滯、Agent 異常、逾時與 state 復原次數保留作稽核，
    但不能讓已完成 workspace 永久停在需關注；真正未處理的 issue、
    checkpoint、goal 變更與 stale PID 仍然會告警。
    """
    if result.get("runner") == "parallel-worker" or result.get("managed_readonly"):
        return False
    if result.get("runner") == "parallel-supervisor":
        parallel = result.get("parallel") or {}
        return bool(
            "error" in result or parallel.get("error")
            or parallel.get("status") == "blocked"
            or result.get("stale_loop_pid")
        )
    completed = result.get("phase") == "done"
    return bool(
        "error" in result or
        result.get("unread_issues", result.get("issues", 0)) > 0 or
        result.get("state_recovery_pending") or
        result.get("goal_changed") or
        result.get("stale_loop_pid") or
        (not completed and (
            result.get("red_streak", 0) > 0 or
            result.get("stall_rounds", 0) > 0 or
            result.get("agent_failure_streak", 0) > 0 or
            result.get("last_round_timed_out") or
            result.get("state_recovery_count", 0) > 0
        ))
    )


def sort_status_results(results, mode: str):
    """排序 fleet projection；錯誤 workspace 永遠排在有效 projection 前。"""
    if mode == "name":
        return sorted(results, key=lambda result: result.get("name", ""))
    phase_order = {"plan": 0, "exec": 1, "done": 2}

    def key(result):
        """attention 排序把錯誤與需關注項目前置，再以名稱保持穩定。"""
        if "error" in result:
            return (0, 0, result.get("name", ""))
        if mode == "attention":
            value = 0 if projection_needs_attention(result) else 1
        elif mode == "running":
            value = 0 if result.get("running") else 1
        elif mode == "phase":
            value = phase_order.get(result.get("phase"), 3)
        else:  # round
            value = -result.get("round", 0)
        return (1, value, result.get("name", ""))

    return sorted(results, key=key)


def filter_status_results(results, mode: str):
    """篩選 fleet projection；只改輸出集合，不改完整 fleet 的 summary/check gate。"""
    if mode == "all":
        return list(results)
    if mode == "attention":
        return [result for result in results if projection_needs_attention(result)]
    if mode == "error":
        return [result for result in results if "error" in result]
    valid = [result for result in results if "error" not in result]
    if mode == "running":
        return [result for result in valid if result.get("running")]
    if mode == "stopped":
        return [result for result in valid if not result.get("running")]
    if mode == "done":
        return [result for result in valid if result.get("phase") == "done"]
    raise ValueError(f"未知 status filter:{mode}")


def render_human(result, *, timestamp=False) -> None:
    """將單 workspace projection 轉成終端可掃讀摘要，不改變任何 state。"""
    phase = {"plan": "規劃期", "exec": "執行期", "done": "完成"}.get(result["phase"], result["phase"] or "未知")
    running = ("啟動檢查中" if result.get("starting") else "執行中" if result["running"]
               else "⚠ PID 殘留" if result.get("stale_loop_pid") else "已停止")
    prefix = f"[{time.strftime('%H:%M:%S')}] " if timestamp else ""
    task = f"｜task-{result['current_order']}：{result['current_task']}" if result.get("current_task") else ""
    issue_note = (f"issues {result['issues']}（未讀 {result['unread_issues']}）"
                  if result.get("unread_issues", result["issues"]) != result["issues"]
                  else f"issues {result['issues']}")
    duration = result.get("last_round_seconds", 0)
    round_note = (f"｜最近一輪 {duration:g} 秒"
                  + ("（逾時）" if result.get("last_round_timed_out") else "")) if duration else ""
    print(f"{prefix}{result['name']}｜{phase}｜round {result['round']}｜"
          f"任務 {result['completed']}/{result['plan_len']}{task}｜{running}｜"
          f"紅連跳 {result['red_streak']}｜停滯 {result['stall_rounds']}｜{issue_note}{round_note}", flush=True)
    if result.get("runner") == "parallel-supervisor":
        parallel = result.get("parallel") or {}
        tasks = parallel.get("tasks") or []
        integrated = sum(1 for item in tasks if item.get("outcome") == "integrated")
        print(
            f"⇉ Parallel｜status {parallel.get('status') or 'unknown'}｜"
            f"batch {parallel.get('batch') if parallel.get('batch') is not None else '—'}｜"
            f"integrated {integrated}/{len(tasks)}｜run {(parallel.get('run_id') or '—')[:12]}",
            flush=True,
        )
        if parallel.get("error"):
            print(f"⚠ {parallel['error']}", flush=True)
    elif result.get("runner") == "parallel-worker":
        assignment = result.get("assignment") or {}
        print(
            f"↳ Managed worker（唯讀）｜parent {result.get('parent_workspace') or '—'}｜"
            f"task-{result.get('assigned_order') or '?'}｜"
            f"status {assignment.get('status') or 'unknown'}",
            flush=True,
        )
    if result["state_recovery_pending"]:
        print("🛟 primary state 不可讀，目前只投影 last-good checkpoint（未修改檔案）", flush=True)
    if result.get("agent_failure_streak", 0):
        print(f"⚠ Agent 異常 {result['agent_failure_streak']}｜退避 {result.get('agent_backoff_seconds', 0):g} 秒", flush=True)
    if result.get("state_recovery_count", 0):
        print(f"🛟 state 復原 {result['state_recovery_count']}", flush=True)
    if result.get("goal_changed"):
        print("⚠ goal 已變更，建議回規劃期重新收斂", flush=True)
    if result.get("round_started_at") and result.get("round_elapsed_seconds") is not None:
        elapsed = format_clock(result["round_elapsed_seconds"])
        remaining = result.get("round_remaining_seconds")
        if result.get("round_active"):
            deadline = ("｜無 timeout" if remaining is None else
                        f"｜timeout 已超過 {format_clock(-remaining)}" if remaining < 0 else
                        f"｜timeout 剩 {format_clock(remaining)}")
            print(f"⏱ 本輪進行 {elapsed}{deadline}", flush=True)
        else:
            qualifier = "至少 " if not result.get("round_interrupted_at") else ""
            print(f"⏸ round {result['round']} 中斷｜已進行 {qualifier}{elapsed}", flush=True)
    metrics = result.get("round_metrics")
    if metrics is not None:
        if metrics["sample_count"]:
            truncated = "｜history 尾端樣本" if metrics.get("history_truncated") else ""
            print(f"⏱ 效能 {metrics['sample_count']} 輪｜平均 {metrics['average_seconds']:g} 秒｜"
                  f"P50 {metrics['p50_seconds']:g} 秒｜P95 {metrics['p95_seconds']:g} 秒｜"
                  f"最慢 r{metrics['slowest_round']} {metrics['max_seconds']:g} 秒｜"
                  f"逾時 {metrics['timeout_count']}（{metrics['timeout_rate_pct']:g}%）{truncated}", flush=True)
        else:
            print("⏱ 尚無含耗時 telemetry 的輪次", flush=True)


def render_fleet_summary(summary) -> None:
    """輸出 --all 的一行摘要，方便人類快速掌握 fleet 健康度。"""
    print(f"fleet｜workspaces {summary['workspace_count']}｜執行中 {summary['running']}｜"
          f"規劃/執行/完成 {summary['planning']}/{summary['executing']}/{summary['done']}｜"
          f"需關注 {summary['attention']}｜issues {summary['issues']}（未讀 {summary['unread_issues']}）｜"
          f"Agent 異常 {summary['agent_failures']}｜round timeout {summary['round_timeouts']}｜"
          f"state 復原 {summary['state_recoveries']}｜"
          f"goal 變更 {summary['goal_changes']}｜stale PID {summary['stale_loops']}｜"
          f"任務 {summary['tasks_completed']}/{summary['tasks_total']} "
          f"({summary['task_completion_pct']}%)｜錯誤 {summary['error_count']}", flush=True)


def parse_status_args(argv=None):
    """解析 status CLI 並在開始讀 workspace 前驗證互斥選項與數值邊界。"""
    parser = argparse.ArgumentParser(description="唯讀查詢 loop-agent-lite workspace 狀態")
    parser.add_argument("--name", default=None, help="workspace 名稱（與 --all 擇一）")
    parser.add_argument("--all", action="store_true", help="列出 workspace root 下全部合法 workspace")
    parser.add_argument("--workspace-root", default=None,
                        help="workspace 根目錄（預設使用 LOOP_AGENT_WORKSPACE_ROOT 或專案 workspace）")
    parser.add_argument("--json", action="store_true", dest="as_json", help="輸出單行 JSON，供 shell/CI 使用")
    parser.add_argument("--watch", action="store_true", help="持續輪詢狀態，Ctrl-C 結束")
    parser.add_argument("--interval", type=float, default=2.0, help="--watch 輪詢秒數（預設 2）")
    parser.add_argument("--on-change", action="store_true",
                        help="搭配 --watch：只有 projection 改變時才輸出")
    parser.add_argument("--check", action="store_true",
                        help="只查詢一次；state 錯誤或需關注時以 exit code 1 結束")
    parser.add_argument("--sort", choices=("name", "attention", "running", "phase", "round"),
                        default="name", help="--all 的排序方式（預設 name）")
    parser.add_argument("--filter", choices=("all", "attention", "running", "stopped", "done", "error"),
                        default="all", help="--all 的輸出篩選（預設 all；不縮小 --check 範圍）")
    parser.add_argument("--metrics", type=int, default=0, metavar="N",
                        help=f"聚合最近 N 輪效能（1～{loop.ROUND_METRICS_MAX_SAMPLES}；預設不掃 history）")
    args = parser.parse_args(argv)
    if bool(args.name) == args.all:
        parser.error("--name 與 --all 必須且只能選一個")
    if not (args.interval > 0 and args.interval < float("inf")):
        parser.error("--interval 必須是有限正數")
    if args.on_change and not args.watch:
        parser.error("--on-change 必須搭配 --watch")
    if args.check and args.watch:
        parser.error("--check 不可搭配 --watch")
    if args.sort != "name" and not args.all:
        parser.error("--sort 只有搭配 --all 才可使用")
    if args.filter != "all" and not args.all:
        parser.error("--filter 只有搭配 --all 才可使用")
    if not 0 <= args.metrics <= loop.ROUND_METRICS_MAX_SAMPLES:
        parser.error(f"--metrics 必須是 0～{loop.ROUND_METRICS_MAX_SAMPLES} 的整數")
    if args.workspace_root:
        loop.WORKSPACE_ROOT = Path(args.workspace_root).expanduser().resolve()
    return args


def render_status_iteration(args, previous_signature):
    """建立並輸出一次 projection，回傳新 signature 與未經 filter 縮小的健康判斷。"""
    if args.all:
        all_results = project_all_status(args.metrics)
        summary = summarize_status(all_results)
        check_failed = summary["error_count"] > 0 or summary["attention"] > 0
        results = sort_status_results(filter_status_results(all_results, args.filter), args.sort)
        projection = {"schema_version": STATUS_SCHEMA_VERSION,
                      "summary": summary, "workspaces": results}
        if args.filter != "all":
            projection["filter"] = args.filter
            projection["matched_count"] = len(results)
    else:
        result = project_status(args.name, args.metrics)
        check_failed = projection_needs_attention(result)
        projection = {"schema_version": STATUS_SCHEMA_VERSION, **result}

    signature = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if signature == previous_signature and args.on_change:
        return signature, check_failed
    if args.as_json:
        print(json.dumps(projection, ensure_ascii=False, separators=(",", ":")), flush=True)
    elif args.all:
        render_fleet_summary(summary)
        if args.filter != "all":
            print(f"filter {args.filter}｜符合 {len(results)}/{summary['workspace_count']}", flush=True)
        if not results:
            print("（沒有符合篩選的 workspace）" if args.filter != "all" else "（沒有合法 workspace）",
                  flush=True)
        for result in results:
            if "error" in result:
                print(f"❌ {result['name']}｜{result['error']}", flush=True)
            else:
                render_human(result, timestamp=args.watch)
    else:
        render_human(result, timestamp=args.watch)
    return signature, check_failed


def main(argv=None) -> int:
    """執行單次或 watch status；projection 與呈現由 render_status_iteration 統一處理。"""
    args = parse_status_args(argv)
    previous_signature = None
    try:
        while True:
            previous_signature, check_failed = render_status_iteration(args, previous_signature)
            if not args.watch:
                return 1 if args.check and check_failed else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except (FileNotFoundError, OSError, ValueError, loop.StateLoadError) as e:
        if args.as_json:
            print(json.dumps({"schema_version": STATUS_SCHEMA_VERSION, "error": str(e)},
                             ensure_ascii=False), flush=True)
        else:
            print(f"❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    def _windows_break(*_):
        raise KeyboardInterrupt

    compat.register_windows_break_handler(_windows_break)
    raise SystemExit(main())
