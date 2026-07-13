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

STATUS_SCHEMA_VERSION = 1
FLEET_PHASES = {"planning", "splitting", "awaiting-approval", "exec", "merging",
                "final", "stopping", "stopped", "cleaning", "done", "failed"}
FLEET_RESUME_PHASES = {"planning", "splitting", "exec", "final", "cleaning"}


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
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError, PermissionError):
        return False
    try:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, check=False).stdout
    except OSError:
        return True
    return any(token in command for token in ("loop.py", "engine.loop", "engine.fleet"))


def read_fleet_projection(directory: Path, state: dict):
    """Read primary/checkpoint fleet truth without repair; both invalid is a hard projection error."""
    failures = []
    for path, label in ((directory / "fleet.json", "fleet.json"),
                        (directory / "fleet.last-good.json", "fleet.last-good.json")):
        try:
            path = loop.workspace_file(path, label)
            fd = loop._open_regular(path, os.O_RDONLY)
            with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
                fleet = json.load(stream)
            if not isinstance(fleet, dict):
                raise ValueError("頂層必須是 JSON object")
            if (fleet.get("schema_version") != 1 or fleet.get("workspace_kind") != "fleet-parent" or
                    fleet.get("run_id") != state.get("fleet_run_id")):
                raise ValueError("與 parent state 身分不符")
            mirror_revision = state.get("fleet_truth_revision")
            if (mirror_revision is not None and
                    fleet.get("dashboard_revision", 0) != mirror_revision):
                raise ValueError("dashboard revision 與 parent mirror 不符")
            if fleet.get("phase") not in FLEET_PHASES:
                raise ValueError("phase 不合法")
            resume_phase = fleet.get("resume_phase")
            if resume_phase is not None and resume_phase not in FLEET_RESUME_PHASES:
                raise ValueError("resume_phase 不合法")
            if fleet.get("phase") == "failed" and resume_phase is None:
                raise ValueError("failed fleet 缺少 resume_phase")
            for field in ("plan", "tracks", "merge_queue"):
                if field in fleet and not isinstance(fleet[field], list):
                    raise ValueError(f"{field} 型別不合法")
            if "loop" in fleet and not isinstance(fleet["loop"], dict):
                raise ValueError("loop 型別不合法")
            for track in fleet.get("tracks") or []:
                if not isinstance(track, dict) or not isinstance(track.get("name"), str):
                    raise ValueError("track 結構不合法")
            if "error" in fleet and fleet["error"] is not None and not isinstance(fleet["error"], str):
                raise ValueError("error 型別不合法")
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            failures.append(f"{label}: {error}")
            continue
        if label != "fleet.json":
            fleet = dict(fleet)
            fleet["fleet_recovery_pending"] = True
        return fleet
    raise ValueError("fleet truth 無法讀取：" + "; ".join(failures))


def fleet_progress_projection(directory: Path, fleet: dict):
    """Project master completed/current/issues using validated child identity and global order mapping."""
    plan = fleet.get("plan") if isinstance(fleet.get("plan"), list) else []
    tracks = fleet.get("tracks") if isinstance(fleet.get("tracks"), list) else []
    by_name = {track.get("name"): track for track in tracks if isinstance(track, dict)}
    completed = []
    current_orders = []
    issues = []
    order_map = fleet.get("order_map") if isinstance(fleet.get("order_map"), dict) else {}
    for task in plan:
        if not isinstance(task, dict) or not isinstance(task.get("order"), int):
            continue
        if (by_name.get(task.get("track")) or {}).get("status") in {"merged", "cleaned"}:
            completed.append(task["order"])
    for track_name, track in by_name.items():
        diagnostics = track.get("diagnostics") if isinstance(track.get("diagnostics"), dict) else {}
        child_issues = diagnostics.get("issues") if isinstance(diagnostics.get("issues"), list) else []
        child = None
        child_name = str(track.get("child_workspace") or "")
        if loop.valid_workspace_name(child_name):
            try:
                child_dir = loop.workspace_path(loop.WORKSPACE_ROOT, child_name)
                child, _data, _recovered = loop.load_checkpointed_state(
                    child_dir / "state.json", repair=False)
            except (FileNotFoundError, OSError, ValueError, loop.StateLoadError):
                child = None
        valid_child = bool(
            child and child.get("workspace_kind") == "fleet-child" and
            child.get("fleet_run_id") == fleet.get("run_id") and
            child.get("fleet_parent") == directory.name and child.get("track") == track_name and
            child.get("fleet_parent_session_id") == (fleet.get("loop") or {}).get("session_id"))
        if not child_issues and valid_child:
            child_issues = child.get("issues") if isinstance(child.get("issues"), list) else []
        issues.extend({**issue, "track": track_name, "child_workspace": child_name}
                      for issue in child_issues if isinstance(issue, dict))
        integration_error = track.get("last_integration_error")
        if isinstance(integration_error, str) and integration_error:
            issues.append({"round": 0, "text": integration_error,
                           "where": "fleet-integration-rollback", "source": "fleet",
                           "synthetic": True, "read_only": True,
                           "resolved": track.get("status") in {"merged", "cleaned"},
                           "track": track_name, "child_workspace": child_name})
        if track.get("status") in {"merged", "cleaned", "failed"}:
            continue
        if valid_child:
            mapped = (order_map.get(track_name) or {}).get(str(child.get("current_order")))
            if isinstance(mapped, int):
                current_orders.append(mapped)
                continue
        track_orders = [task["order"] for task in plan if isinstance(task, dict) and
                        task.get("track") == track_name and isinstance(task.get("order"), int)]
        if track_orders and track.get("status") in {"pending", "running", "repairing"}:
            current_orders.append(min(track_orders))
    transaction = fleet.get("merge_tx")
    if isinstance(transaction, dict) and (
            transaction.get("stage") in {"rollback-prepared", "rolled-back"} or
            isinstance(transaction.get("validation_error"), str)):
        track = by_name.get(transaction.get("track")) or {}
        message = transaction.get("validation_error")
        if not isinstance(message, str) or not message:
            message = f"integration rollback pending ({transaction.get('stage')})"
        existing = next((issue for issue in issues
                         if issue.get("track") == transaction.get("track") and
                         issue.get("text") == message), None)
        if existing is not None:
            existing["resolved"] = track.get("status") in {"merged", "cleaned"}
        else:
            issues.append({"round": 0, "text": message,
                           "where": "fleet-integration-rollback", "source": "fleet",
                           "synthetic": True, "read_only": True,
                           "resolved": track.get("status") in {"merged", "cleaned"},
                           "track": transaction.get("track"),
                           "child_workspace": track.get("child_workspace")})
    return completed, sorted(set(current_orders)), issues


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
    workspace_kind = state.get("workspace_kind") or "standalone"
    fleet = read_fleet_projection(directory, state) if workspace_kind == "fleet-parent" else None
    loop_state = state.get("loop") if isinstance(state.get("loop"), dict) else {}
    if fleet is not None:
        loop_state = fleet.get("loop") if isinstance(fleet.get("loop"), dict) else {}
    pid = loop_state.get("pid")
    running = pid_is_loop_alive(pid)
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
    projected_phase = fleet.get("phase") if fleet is not None else state.get("phase")
    projected_plan = fleet.get("plan") if fleet is not None and isinstance(fleet.get("plan"), list) else plan
    projected_completed = len(completed)
    tracks = fleet.get("tracks") if fleet is not None and isinstance(fleet.get("tracks"), list) else []
    if fleet is not None:
        completed_orders, current_orders, issues = fleet_progress_projection(directory, fleet)
        projected_completed = len(completed_orders)
        current_order = current_orders[0] if current_orders else None
        current_task = next((task.get("task", "") for task in projected_plan
                             if isinstance(task, dict) and task.get("order") == current_order), "")
        if len(current_task) > 160:
            current_task = current_task[:160] + "…"
    projection = {
        "name": name,
        "workspace": str(directory),
        "workspace_kind": workspace_kind,
        "fleet_run_id": state.get("fleet_run_id"),
        "fleet_parent": state.get("fleet_parent"),
        "track": state.get("track"),
        "merge_stage": state.get("merge_stage"),
        "phase": projected_phase,
        "parallel_phase": fleet.get("phase") if fleet is not None else None,
        "parallel_error": fleet.get("error") if fleet is not None else None,
        "parallel_stop_reason": fleet.get("stop_reason") if fleet is not None else None,
        "parallel_resumable": fleet.get("phase") != "done" if fleet is not None else None,
        "parallel_track_events": [
            {"track": track.get("name"), "child_workspace": track.get("child_workspace"),
             "event_history": track.get("event_history") or []}
            for track in tracks if isinstance(track, dict)],
        "parallel_tracks": tracks,
        "round": state.get("round", 0),
        "flag": state.get("flag", 0),
        "done_count": state.get("done_count", 0),
        "plan_version": state.get("plan_version", 0),
        "plan_len": len(projected_plan),
        "completed": projected_completed,
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
        "round_active": bool(round_started_at and running),
        "round_interrupted": bool(round_started_at and not running),
        **round_timing,
        "state_recovery_count": state.get("state_recovery_count", 0),
        "last_state_recovery": state.get("last_state_recovery"),
        "goal_changed": bool(state.get("goal_changed")),
        "goal_previous_hash": state.get("goal_previous_hash"),
        "issues": len(issues),
        "unread_issues": (sum(1 for issue in issues if not issue.get("resolved"))
                          if fleet is not None else loop.unread_issue_count(state)),
        "last_green_sha": state.get("last_green_sha"),
        "loop_pid": pid,
        "loop_session_id": loop_state.get("session_id"),
        "loop_started_at": loop_state.get("started_at"),
        "running": running,
        "stale_loop_pid": pid is not None and not running,
        "state_recovery_pending": recovered or bool(fleet and fleet.get("fleet_recovery_pending")),
    }
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
            results.append(project_status(entry.name, metrics_limit))
        except (FileNotFoundError, OSError, ValueError, loop.StateLoadError) as e:
            results.append({"name": entry.name, "error": str(e)})
    return results


def _parent_registers_fleet_child(parent, child):
    """Return true only when parent projection registers this exact child/track pair."""
    if (parent.get("workspace_kind") != "fleet-parent" or
            child.get("workspace_kind") != "fleet-child" or
            not isinstance(parent.get("name"), str) or
            not isinstance(child.get("fleet_parent"), str) or
            child.get("fleet_parent") != parent.get("name") or
            not isinstance(child.get("fleet_run_id"), str) or
            child.get("fleet_run_id") != parent.get("fleet_run_id") or
            not isinstance(child.get("name"), str) or
            not isinstance(child.get("track"), str)):
        return False
    tracks = parent.get("parallel_tracks")
    if not isinstance(tracks, list):
        tracks = parent.get("tracks")
    if not isinstance(tracks, list):
        return False
    return any(
        isinstance(track, dict) and
        track.get("name") == child["track"] and
        track.get("child_workspace") == child["name"]
        for track in tracks
    )


def summarize_status(results):
    """將 fleet projection 聚合成 shell/CI 可直接使用的摘要。"""
    valid = [result for result in results if "error" not in result]
    parents = {result.get("name"): result for result in valid
               if result.get("workspace_kind") == "fleet-parent"}
    aggregate = [result for result in valid
                 if not (result.get("workspace_kind") == "fleet-child" and
                         _parent_registers_fleet_child(
                             parents.get(result.get("fleet_parent"), {}), result))]
    tasks_total = sum(result.get("plan_len", 0) for result in aggregate)
    tasks_completed = sum(result.get("completed", 0) for result in aggregate)
    return {
        "workspace_count": len(results),
        "valid_count": len(valid),
        "error_count": len(results) - len(valid),
        "running": sum(1 for result in aggregate if result.get("running")),
        "planning": sum(1 for result in aggregate if result.get("phase") in {"plan", "planning"}),
        "executing": sum(1 for result in aggregate if result.get("phase") in
                         {"splitting", "exec", "merging", "final", "cleaning", "stopping"}),
        "done": sum(1 for result in aggregate if result.get("phase") == "done"),
        "attention": sum(1 for result in aggregate if projection_needs_attention(result)),
        "issues": sum(result.get("issues", 0) for result in aggregate),
        "unread_issues": sum(result.get("unread_issues", result.get("issues", 0))
                             for result in aggregate),
        "agent_failures": sum(result.get("agent_failure_streak", 0) for result in aggregate),
        "round_timeouts": sum(1 for result in aggregate if result.get("last_round_timed_out")),
        "state_recoveries": sum(result.get("state_recovery_count", 0) for result in aggregate),
        "goal_changes": sum(1 for result in aggregate if result.get("goal_changed")),
        "stale_loops": sum(1 for result in aggregate if result.get("stale_loop_pid")),
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
    completed = result.get("phase") == "done"
    return bool(
        "error" in result or
        result.get("unread_issues", result.get("issues", 0)) > 0 or
        result.get("state_recovery_pending") or
        result.get("goal_changed") or
        result.get("stale_loop_pid") or
        result.get("parallel_error") or
        result.get("phase") == "failed" or
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
    phase = {"plan": "規劃期", "planning": "並行規劃中", "splitting": "拆分中",
             "exec": "並行執行中", "merging": "整合中", "final": "最終驗收中",
             "cleaning": "清理中", "stopping": "停止中", "stopped": "已停止",
             "failed": "失敗", "done": "完成"}.get(result["phase"], result["phase"] or "未知")
    running = "執行中" if result["running"] else "⚠ PID 殘留" if result.get("stale_loop_pid") else "已停止"
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
    if result.get("workspace_kind") == "fleet-parent":
        statuses = ", ".join(f"{track.get('name')}:{track.get('status')}"
                             for track in result.get("parallel_tracks") or []) or "尚未拆軌"
        print(f"🔀 parallel run {result.get('fleet_run_id')}｜{statuses}", flush=True)
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
    raise SystemExit(main())
