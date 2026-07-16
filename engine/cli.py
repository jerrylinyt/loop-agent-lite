#!/usr/bin/env python3
"""單一 workspace 的高階生命週期 CLI。

``engine.loop`` 保留為完整但低階的 coordinator 入口；本模組負責把 init 時
保存於 ``state.config`` 的設定安全地重建成後續 run/restart 命令。
"""

import argparse
import json
import math
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from engine import loop as loop_mod
from engine import status as status_mod
from engine.paths import default_workspace_root, expose_project_package


def _finite_number(value, key, *, minimum=0, strictly_positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"state.config.{key} 必須是數字")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < minimum or (strictly_positive and parsed == 0):
        comparator = "> 0" if strictly_positive else f"≥ {minimum:g}"
        raise ValueError(f"state.config.{key} 必須是有限數字且 {comparator}")
    return parsed


def _positive_integer(value, key) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"state.config.{key} 必須是 ≥ 1 的整數")
    return value


def normalize_runtime_config(state) -> dict:
    """驗證並補齊可重建 engine.loop argv 的 workspace config。"""
    raw = state.get("config") if isinstance(state, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("state 缺少 config；請重新 init，或先由 Dashboard 完整啟動一次")
    config = dict(raw)
    for key in ("repo", "agent_cmd", "validate_cmd"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"state.config.{key} 必須是非空字串")
    for key in ("agent_cmd", "validate_cmd"):
        try:
            command = shlex.split(config[key])
        except ValueError as e:
            raise ValueError(f"state.config.{key} 命令格式錯誤：{e}") from e
        if not command:
            raise ValueError(f"state.config.{key} 不可為空")

    for key, default in (
        ("flag_threshold", loop_mod.FLAG_THRESHOLD),
        ("done_threshold", loop_mod.DONE_THRESHOLD),
        ("red_limit", loop_mod.RED_LIMIT),
        ("stall_limit", loop_mod.STALL_LIMIT),
        ("stuck_stop_count", loop_mod.STUCK_STOP_COUNT),
    ):
        config[key] = _positive_integer(config.get(key, default), key)
    config["round_timeout"] = _finite_number(
        config.get("round_timeout", loop_mod.ROUND_TIMEOUT_MIN), "round_timeout")
    config["agent_backoff_max"] = _finite_number(
        config.get("agent_backoff_max", loop_mod.AGENT_BACKOFF_MAX_SEC), "agent_backoff_max")
    config["validate_timeout"] = _finite_number(
        config.get("validate_timeout", loop_mod.VALIDATE_TIMEOUT_SEC), "validate_timeout",
        strictly_positive=True)

    for key, default in (("pause_after_plan", False), ("stuck_stop", False)):
        value = config.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"state.config.{key} 必須是 boolean")
        config[key] = value
    for key, default in (("goal", "goal.md"), ("plan_doc", ""), ("notify_cmd", "")):
        value = config.get(key, default)
        if not isinstance(value, str) or (key == "goal" and not value):
            raise ValueError(f"state.config.{key} 必須是{'非空' if key == 'goal' else ''}字串")
        config[key] = value
    if config["notify_cmd"]:
        try:
            shlex.split(config["notify_cmd"])
        except ValueError as e:
            raise ValueError(f"state.config.notify_cmd 命令格式錯誤：{e}") from e
    config["repo"] = str(Path(config["repo"]).expanduser().resolve())
    binding = state.get("repo_binding") if isinstance(state, dict) else None
    if binding is not None:
        if not isinstance(binding, str) or not binding.strip():
            raise ValueError("state.repo_binding 必須是非空字串或 null")
        binding = str(Path(binding).expanduser().resolve())
        if binding != config["repo"]:
            raise ValueError(
                "state.config.repo 與不可變 repo_binding 不一致；不可把既有進度改綁到另一 repo，"
                "請改用新的 workspace，或以 init --force 明確重建")
        config["repo"] = binding
    return config


def config_to_loop_args(name: str, config: dict) -> list[str]:
    """將已驗證的 state.config 完整重建為低階 coordinator argv。"""
    args = [
        "--repo", config["repo"], "--name", name,
        "--goal", config["goal"],
        "--agent-cmd", config["agent_cmd"],
        "--validate-cmd", config["validate_cmd"],
        "--flag-threshold", str(config["flag_threshold"]),
        "--done-threshold", str(config["done_threshold"]),
        "--red-limit", str(config["red_limit"]),
        "--stall-limit", str(config["stall_limit"]),
        "--stuck-stop-count", str(config["stuck_stop_count"]),
        "--round-timeout", f"{config['round_timeout']:g}",
        "--agent-backoff-max", f"{config['agent_backoff_max']:g}",
        "--validate-timeout", f"{config['validate_timeout']:g}",
    ]
    if config["plan_doc"]:
        args += ["--plan-doc", config["plan_doc"]]
    if config["stuck_stop"]:
        args.append("--stuck-stop")
    if config["pause_after_plan"]:
        args.append("--pause-after-plan")
    if config["notify_cmd"]:
        args += ["--notify-cmd", config["notify_cmd"]]
    return args


def _workspace_state(name: str, *, repair=False):
    loop_mod.require_workspace_name(name)
    directory = loop_mod.workspace_path(loop_mod.WORKSPACE_ROOT, name)
    if loop_mod.workspace_directory(directory, create=False) is None:
        raise FileNotFoundError(f"workspace {name} 不存在；請先執行 init")
    state, _data, recovered = loop_mod.load_checkpointed_state(
        directory / "state.json", repair=repair)
    return directory, state, recovered


def _engine_command(args: list[str]) -> list[str]:
    return [sys.executable, "-m", "engine.loop", *args]


def _engine_env() -> dict:
    env = expose_project_package(dict(os.environ))
    env["LOOP_AGENT_WORKSPACE_ROOT"] = str(loop_mod.WORKSPACE_ROOT)
    return env


def _exec_engine(args: list[str]):
    command = _engine_command(args)
    os.execve(sys.executable, command, _engine_env())


def _append_common_runtime_args(args: list[str], values) -> None:
    args += [
        "--agent-cmd", values.agent_cmd,
        "--validate-cmd", values.validate_cmd,
        "--flag-threshold", str(values.flag_threshold),
        "--done-threshold", str(values.done_threshold),
        "--red-limit", str(values.red_limit),
        "--stall-limit", str(values.stall_limit),
        "--stuck-stop-count", str(values.stuck_stop_count),
        "--round-timeout", str(values.round_timeout),
        "--agent-backoff-max", str(values.agent_backoff_max),
        "--validate-timeout", str(values.validate_timeout),
    ]
    if values.stuck_stop:
        args.append("--stuck-stop")
    if values.pause_after_plan:
        args.append("--pause-after-plan")
    if values.notify_cmd:
        args += ["--notify-cmd", values.notify_cmd]


def command_init(args) -> int:
    engine_args = ["--repo", str(Path(args.repo).expanduser().resolve()), "--init-only"]
    if args.name:
        engine_args += ["--name", args.name]
    engine_args += ["--goal", args.goal]
    if args.plan_doc:
        engine_args += ["--plan-doc", args.plan_doc]
    _append_common_runtime_args(engine_args, args)
    if args.import_plan:
        engine_args += ["--import-plan", str(Path(args.import_plan).expanduser().resolve()),
                        "--start-phase", args.start_phase]
    elif args.start_phase != "plan":
        raise ValueError("--start-phase 只有搭配 --import-plan 才有意義")
    if args.force:
        engine_args.append("--reset-state")
    _exec_engine(engine_args)
    return 0


def command_run(args):
    _directory, state, _recovered = _workspace_state(args.name, repair=False)
    if state.get("phase") == "done" and not args.reset_state:
        raise ValueError(
            f"workspace {args.name} 已完成；要建立全新 run 請明確加 --reset-state")
    config = normalize_runtime_config(state)
    engine_args = config_to_loop_args(args.name, config)
    if args.resume_interrupted:
        engine_args.append("--resume-interrupted")
    if args.reset_state:
        engine_args.append("--reset-state")
    _exec_engine(engine_args)
    return 0


def command_check(args) -> int:
    _directory, state, _recovered = _workspace_state(args.name, repair=False)
    config = normalize_runtime_config(state)
    _exec_engine(config_to_loop_args(args.name, config) + ["--preflight-only"])
    return 0


def command_status(args) -> int:
    forwarded = ["--name", args.name]
    if args.as_json:
        forwarded.append("--json")
    if args.watch:
        forwarded.append("--watch")
    if args.on_change:
        forwarded.append("--on-change")
    if args.check:
        forwarded.append("--check")
    if args.interval != 2.0:
        forwarded += ["--interval", str(args.interval)]
    if args.metrics:
        forwarded += ["--metrics", str(args.metrics)]
    return status_mod.main(forwarded)


CONFIG_OPTION_TO_KEY = {
    "agent_cmd": "agent_cmd",
    "validate_cmd": "validate_cmd",
    "goal": "goal",
    "plan_doc": "plan_doc",
    "flag_threshold": "flag_threshold",
    "done_threshold": "done_threshold",
    "red_limit": "red_limit",
    "stall_limit": "stall_limit",
    "stuck_stop_count": "stuck_stop_count",
    "round_timeout": "round_timeout",
    "agent_backoff_max": "agent_backoff_max",
    "validate_timeout": "validate_timeout",
    "pause_after_plan": "pause_after_plan",
    "stuck_stop": "stuck_stop",
    "notify_cmd": "notify_cmd",
}


def _print_config(name: str, config: dict) -> None:
    print(json.dumps({"name": name, "config": config}, ensure_ascii=False, indent=2))


def command_config(args) -> int:
    directory, state, _recovered = _workspace_state(args.name, repair=False)
    updates = {key: getattr(args, option) for option, key in CONFIG_OPTION_TO_KEY.items()
               if getattr(args, option) is not None}
    config = normalize_runtime_config(state)
    if not updates:
        _print_config(args.name, config)
        return 0

    # config 是 coordinator 下一次啟動的輸入；目前 writer 未停時不可競寫。
    loop_mod.acquire_run_lock(directory / ".run.lock", f"workspace '{args.name}'")
    workspace = loop_mod.Workspace(args.name)
    state = workspace.load_state()
    config = normalize_runtime_config(state)
    config.update(updates)
    config = normalize_runtime_config({"config": config})
    repo = Path(config["repo"])
    loop_mod.repo_relative_path(repo, config["goal"])
    if config["plan_doc"]:
        loop_mod.repo_relative_path(repo, config["plan_doc"])
    state["config"] = config
    if not state.get("repo_binding"):
        state["repo_binding"] = config["repo"]
    workspace.save_state(state)
    print(f"✅ 已更新 workspace {args.name} 的 state.config（下次 run/restart 生效）")
    _print_config(args.name, config)
    return 0


def _pid_matches_workspace(pid: int, name: str) -> bool:
    """送 signal 前確認 PID 仍是指定 workspace 的 engine.loop，降低 PID reuse 風險。"""
    try:
        command = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="], capture_output=True,
            text=True, check=False).stdout.strip()
        tokens = shlex.split(command)
    except (OSError, TypeError, ValueError):
        return False
    if not ("engine.loop" in tokens or any(token.endswith("/loop.py") for token in tokens)):
        return False
    return any(tokens[index:index + 2] == ["--name", name]
               for index in range(max(0, len(tokens) - 1)))


def command_stop(args) -> int:
    directory, state, _recovered = _workspace_state(args.name, repair=False)
    loop_state = state.get("loop") if isinstance(state.get("loop"), dict) else {}
    state_pid, session_id = loop_state.get("pid"), loop_state.get("session_id")
    owner = loop_mod.active_run_lock_owner(directory / ".run.lock")
    if owner is None:
        print(f"workspace {args.name} 已停止")
        return 0
    pid = owner["pid"]
    published_session = (status_mod.pid_is_loop_alive(state_pid) and
                         int(state_pid) == int(pid) and bool(session_id))
    if status_mod.pid_is_loop_alive(state_pid) and int(state_pid) != int(pid):
        raise ValueError(
            f"state PID {state_pid} 與目前 .run.lock owner {pid} 不一致；拒絕停止")
    if not status_mod.pid_is_loop_alive(pid):
        raise ValueError(f".run.lock owner PID {pid} 不是可確認的 loop；拒絕停止")
    if not _pid_matches_workspace(int(pid), args.name):
        raise ValueError(f"PID {pid} 無法確認為 workspace {args.name} 的 loop；拒絕送出停止要求")

    if not args.now:
        if not published_session:
            raise ValueError(
                f"workspace {args.name} 正在 startup/preflight，session 尚未公開；"
                "請等待 run 開始後再平順停止，或使用 --now")
        if loop_mod.stop_after_round_claimed(directory, pid, session_id):
            print(f"workspace {args.name} 已在本輪收尾中（pid {pid}）")
            return 0
        if loop_mod.stop_after_round_requested(directory, pid, session_id):
            print(f"workspace {args.name} 已要求本輪後停止（pid {pid}）")
            return 0
        payload = {"pid": int(pid), "session_id": session_id,
                   "requested_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        loop_mod.atomic_write_bytes(
            directory / loop_mod.STOP_AFTER_ROUND_FILE,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        print(f"✅ 已要求 workspace {args.name} 在本輪完整落盤後停止（pid {pid}）")
        return 0

    loop_mod.safe_kill(int(pid), signal.SIGINT)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and status_mod.pid_is_loop_alive(pid):
        time.sleep(0.1)
    if status_mod.pid_is_loop_alive(pid):
        raise RuntimeError(
            f"workspace {args.name} 在 SIGINT 後 15 秒仍未停止；未自動 SIGKILL，"
            "避免留下 orphan Agent。請先查 process tree 與 console.log")
    print(f"✅ workspace {args.name} 已立即停止")
    return 0


def _add_tuning_options(parser, *, defaults=True) -> None:
    default = (lambda value: value) if defaults else (lambda _value: None)
    parser.add_argument("--flag-threshold", type=int, default=default(loop_mod.FLAG_THRESHOLD))
    parser.add_argument("--done-threshold", type=int, default=default(loop_mod.DONE_THRESHOLD))
    parser.add_argument("--red-limit", type=int, default=default(loop_mod.RED_LIMIT))
    parser.add_argument("--stall-limit", type=int, default=default(loop_mod.STALL_LIMIT))
    parser.add_argument("--stuck-stop-count", type=int, default=default(loop_mod.STUCK_STOP_COUNT))
    parser.add_argument("--round-timeout", type=float, default=default(loop_mod.ROUND_TIMEOUT_MIN),
                        help="單輪 Agent 上限（分鐘；0=不限）")
    parser.add_argument("--agent-backoff-max", type=float,
                        default=default(loop_mod.AGENT_BACKOFF_MAX_SEC), help="連續異常退避上限（秒）")
    parser.add_argument("--validate-timeout", type=float,
                        default=default(loop_mod.VALIDATE_TIMEOUT_SEC), help="Validate 上限（秒）")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="loop-agent-lite：以單一 workspace 初始化、執行與重新啟動 loop")
    parser.add_argument("--workspace-root", default=None,
                        help="workspace 根目錄（預設 LOOP_AGENT_WORKSPACE_ROOT 或專案 workspace/）")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="preflight 後建立 stopped workspace，不啟動 Agent")
    init.add_argument("--repo", required=True, help="target Git repo")
    init.add_argument("--name", default=None, help="workspace 名稱（預設 repo 目錄名）")
    init.add_argument("--goal", default="goal.md", help="repo-relative、已 commit 的 Goal")
    init.add_argument("--plan-doc", default="", help="選配 repo-relative、已 commit 的參考文件")
    init.add_argument("--agent-cmd", required=True, help="Agent CLI；prompt 由 stdin 傳入")
    init.add_argument("--validate-cmd", required=True, help="每輪與啟動前驗證命令")
    init.add_argument("--import-plan", "--plan", dest="import_plan", default="",
                      help="選配 plan JSON；init 時匯入")
    init.add_argument("--start-phase", choices=("plan", "exec"), default="plan",
                      help="匯入 plan 後從規劃期或執行期開始")
    init.add_argument("--force", action="store_true",
                      help="交易式覆寫既有 workspace 進度；未指定時 init 會拒絕既有 state")
    init.add_argument("--stuck-stop", action="store_true")
    init.add_argument("--pause-after-plan", action="store_true")
    init.add_argument("--notify-cmd", default="")
    _add_tuning_options(init)

    run = commands.add_parser("run", aliases=["restart"],
                              help="用 state.config 前景執行；restart 是同義命令")
    run.add_argument("name", help="workspace 名稱")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--resume-interrupted", action="store_true",
                      help="明確保留中斷髒現場並略過啟動 Validate")
    mode.add_argument("--reset-state", action="store_true",
                      help="交易式清除 coordinator 進度，從規劃期重新開始")

    check = commands.add_parser("check", help="用保存設定執行 preflight，不啟動 Agent")
    check.add_argument("name")

    status = commands.add_parser("status", help="唯讀查看單一 workspace")
    status.add_argument("name")
    status.add_argument("--json", action="store_true", dest="as_json")
    status.add_argument("--watch", action="store_true")
    status.add_argument("--on-change", action="store_true")
    status.add_argument("--check", action="store_true", help="需關注時 exit 1")
    status.add_argument("--interval", type=float, default=2.0)
    status.add_argument("--metrics", type=int, default=0)

    config = commands.add_parser("config", help="顯示或安全更新 stopped workspace 的 state.config")
    config.add_argument("name")
    config.add_argument("--agent-cmd", default=None)
    config.add_argument("--validate-cmd", default=None)
    config.add_argument("--goal", default=None)
    config.add_argument("--plan-doc", default=None)
    config.add_argument("--notify-cmd", default=None)
    _add_tuning_options(config, defaults=False)
    pause = config.add_mutually_exclusive_group()
    pause.add_argument("--pause-after-plan", action="store_true", dest="pause_after_plan")
    pause.add_argument("--no-pause-after-plan", action="store_false", dest="pause_after_plan")
    stuck = config.add_mutually_exclusive_group()
    stuck.add_argument("--stuck-stop", action="store_true", dest="stuck_stop")
    stuck.add_argument("--no-stuck-stop", action="store_false", dest="stuck_stop")
    config.set_defaults(pause_after_plan=None, stuck_stop=None)

    stop = commands.add_parser("stop", help="預設本輪後停止；--now 立即送 SIGINT")
    stop.add_argument("name")
    stop.add_argument("--now", action="store_true", help="立即中斷目前 round")
    return parser


def main(argv=None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    loop_mod.WORKSPACE_ROOT = (Path(args.workspace_root).expanduser().resolve()
                               if args.workspace_root else default_workspace_root())
    try:
        if args.command == "init":
            return command_init(args)
        if args.command in ("run", "restart"):
            return command_run(args)
        if args.command == "check":
            return command_check(args)
        if args.command == "status":
            return command_status(args)
        if args.command == "config":
            return command_config(args)
        if args.command == "stop":
            return command_stop(args)
        parser.error(f"未知命令：{args.command}")
    except (FileNotFoundError, OSError, RuntimeError, ValueError, loop_mod.StateLoadError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
