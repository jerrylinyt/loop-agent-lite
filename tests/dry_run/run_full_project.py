#!/usr/bin/env python3
"""L4 full-project runner: isolated clone, configured Codex, production Dashboard, and real UI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_THRESHOLDS = {"flag": 10, "done": 3, "merge": 2, "max_parallel": 4}
PLAYWRIGHT_TOTAL_SECONDS = 4 * 60 * 60
PLAYWRIGHT_DELETE_MAX_SECONDS = 10 * 60
L4_PLANNING_TIMEOUT_SECONDS = 2 * 60 * 60
# The L4 validator runs the complete Python suite, a clean UI install/build, and
# the immutable integration validator.  Keep this separate from the lightweight
# Dashboard default: the complete suite already takes longer than 120 seconds on
# the supported local runner, especially while DR-1 and DR-2 run concurrently.
L4_VALIDATE_TIMEOUT_SECONDS = 15 * 60
SENSITIVE_OPTION_NAMES = {
    "--api-key", "--apikey", "--token", "--access-token", "--password", "--secret",
    "--credential", "--credentials",
}
SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:api_?key|access_?token|token|password|passwd|secret|credential|credentials)$",
    re.IGNORECASE,
)
HOST_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:api_?key|access_?key|private_?key|token|password|passwd|secret|"
    r"credential|credentials|authorization|cookie)(?:_|$)",
    re.IGNORECASE,
)
TEXT_ARTIFACT_SUFFIXES = {
    "", ".css", ".csv", ".html", ".js", ".json", ".jsonl", ".log", ".map", ".md",
    ".network", ".py", ".stderr", ".stdout", ".text", ".trace", ".ts", ".txt",
    ".xml", ".yaml", ".yml",
}
FLEET_EVENT_HISTORY_LIMIT = 500
DR1_BACKEND_CONTRACT_PATHS = {
    "engine/l4_delivery_probe.py",
    "tests/test_l4_delivery_probe.py",
}
DR1_FRONTEND_CONTRACT_PATHS = {
    "ui/src/features/workspaces/l4DeliveryProbe.ts",
    "ui/src/features/workspaces/ParallelRunGroup.tsx",
    "ui/e2e/dashboard-flow.spec.ts",
}
TRUTH_SNAPSHOT_NAMES = (
    "fleet.json", "fleet.last-good.json", "state.json", "state.last-good.json",
)


def safe_track_name(name):
    return "_final" if name == "@final" else name


class CommandFailure(RuntimeError):
    """保留失敗命令的結構化紀錄，讓 manifest 不因 raise 遺失最重要的證據。"""

    def __init__(self, message, record):
        super().__init__(message)
        self.record = record


class PortReservation:
    """以 O_EXCL lock 把 ephemeral port claim 延伸到整個跨程序 L4 run。"""

    def __init__(self, port: int, lock_path: Path, token: str):
        self.port = port
        self.lock_path = lock_path
        self.token = token
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        try:
            owner = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if owner.get("token") != self.token or owner.get("pid") != os.getpid():
            return
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass


def _process_group_exists(process_group: int) -> bool:
    result = subprocess.run(
        ["ps", "-axo", "pgid=,state="], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode:
        return True
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            pgid = int(fields[0])
        except ValueError:
            continue
        if pgid == process_group and not fields[1].startswith("Z"):
            return True
    return False


def _wait_for_process_group_exit(process_group: int, timeout: float,
                                 process: subprocess.Popen | None = None) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while _process_group_exists(process_group):
        if process is not None:
            process.poll()
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    return True


def _signal_process_group(process_group: int, sig: int) -> bool:
    try:
        os.killpg(process_group, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def _terminate_process_group(process: subprocess.Popen, process_group: int, *,
                             interrupt_seconds=1.0, force_seconds=2.0):
    """Stop the isolated command tree and prove that its process group is empty."""
    sent_interrupt = _signal_process_group(process_group, signal.SIGINT)
    if sent_interrupt:
        _wait_for_process_group_exit(process_group, interrupt_seconds, process)
    sent_kill = False
    if _process_group_exists(process_group):
        sent_kill = _signal_process_group(process_group, signal.SIGKILL)
    group_empty = _wait_for_process_group_exit(process_group, force_seconds, process)
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=force_seconds)
        except subprocess.TimeoutExpired:
            group_empty = False
    return {"sent_interrupt": sent_interrupt, "sent_kill": sent_kill,
            "group_empty": group_empty}


def _timeout_output(error: subprocess.TimeoutExpired) -> str:
    output = error.stdout or ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return output


def run(command, *, cwd=None, env=None, log=None, check=True, input_text=None, timeout=None):
    started = time.time()
    process = subprocess.Popen(
        command, cwd=cwd, env=env, text=True, stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    )
    process_group = process.pid
    try:
        output, _ = process.communicate(input_text, timeout=timeout)
    except KeyboardInterrupt:
        cleanup = _terminate_process_group(process, process_group)
        try:
            output, _ = process.communicate(timeout=2)
        except subprocess.TimeoutExpired as final_error:
            output = _timeout_output(final_error)
        output = output or ""
        output += "\nL4 harness cancelled by KeyboardInterrupt\n"
        if not cleanup["group_empty"]:
            output += f"L4 harness could not empty process group {process_group}\n"
        if log:
            Path(log).write_text(output, encoding="utf-8")
        if not cleanup["group_empty"]:
            raise RuntimeError(
                f"L4 harness cancellation could not empty process group {process_group}"
            )
        raise
    except subprocess.TimeoutExpired as error:
        output = _timeout_output(error)
        cleanup = _terminate_process_group(process, process_group)
        try:
            final_output, _ = process.communicate(timeout=2)
        except subprocess.TimeoutExpired as final_error:
            final_output = _timeout_output(final_error)
        if final_output:
            output = final_output
        output += f"\nL4 harness timeout after {timeout:g} seconds\n"
        if not cleanup["group_empty"]:
            output += f"L4 harness could not empty process group {process_group}\n"
        if log:
            Path(log).write_text(output, encoding="utf-8")
        record = {
            "command": command, "cwd": str(cwd or Path.cwd()), "exit_code": 124,
            "started_at": started, "ended_at": time.time(), "log": str(log) if log else None,
            "timed_out": True, "timeout_seconds": timeout,
            "process_group": process_group, "process_group_cleanup": cleanup,
        }
        raise CommandFailure(f"command timed out after {timeout:g}s: {command}\n{output[-4000:]}", record) from error
    output = output or ""
    leaked_group = _process_group_exists(process_group)
    cleanup = None
    if leaked_group:
        cleanup = _terminate_process_group(process, process_group)
    if log:
        Path(log).write_text(output, encoding="utf-8")
    record = {"command": command, "cwd": str(cwd or Path.cwd()), "exit_code": process.returncode,
              "started_at": started, "ended_at": time.time(), "log": str(log) if log else None,
              "timed_out": False, "timeout_seconds": timeout, "process_group": process_group}
    if cleanup is not None:
        record["process_group_cleanup"] = cleanup
        record["leaked_process_group"] = True
        raise CommandFailure(
            f"command left a live process group after exit ({process.returncode}): {command}\n{output[-4000:]}",
            record,
        )
    if check and process.returncode:
        raise CommandFailure(
            f"command failed ({process.returncode}): {command}\n{output[-4000:]}",
            record,
        )
    return subprocess.CompletedProcess(command, process.returncode, output, None), record


def sha256_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sensitive_environment_values(environment=None) -> tuple[bytes, ...]:
    """取得宿主敏感 env 的值供 exact-value 掃描；呼叫端永不保存名稱或內容。"""
    source = os.environ if environment is None else environment
    return tuple(sorted({str(value).encode("utf-8") for name, value in source.items()
                         if value and HOST_SENSITIVE_ENV_NAME.search(str(name))}, key=len, reverse=True))


def sanitized_child_environment(environment=None) -> dict[str, str]:
    """保留 PATH/HOME/Codex config 等一般使用者環境，但不把 credentials 或 coordinator 身分下放。"""
    source = os.environ if environment is None else environment
    return {str(name): str(value) for name, value in source.items()
            if not str(name).startswith("LOOP_") and
            not HOST_SENSITIVE_ENV_NAME.search(str(name))}


def _stream_contains_value(stream, sensitive_values: tuple[bytes, ...]) -> bool:
    if not sensitive_values:
        return False
    overlap = max(len(value) for value in sensitive_values) - 1
    tail = b""
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return False
        data = tail + chunk
        if any(value in data for value in sensitive_values):
            return True
        tail = data[-overlap:] if overlap > 0 else b""


def assert_artifacts_contain_no_sensitive_values(artifacts: Path,
                                                  sensitive_values: tuple[bytes, ...]):
    """Fail closed on exact host-secret values in text artifacts or decompressed zip entries."""
    if not sensitive_values:
        return
    findings = []
    contaminated = set()
    for path in sorted(artifacts.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = str(path.relative_to(artifacts))
        if path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    for item in archive.infolist():
                        if item.is_dir():
                            continue
                        with archive.open(item) as stream:
                            if _stream_contains_value(stream, sensitive_values):
                                findings.append(redact_sensitive_text(
                                    f"{relative}::{item.filename}", sensitive_values))
                                contaminated.add(path)
            except (OSError, zipfile.BadZipFile, RuntimeError) as error:
                safe_relative = redact_sensitive_text(relative, sensitive_values)
                raise RuntimeError(
                    f"無法安全掃描 zip artifact：{safe_relative} ({type(error).__name__})") from error
        elif path.suffix.lower() in TEXT_ARTIFACT_SUFFIXES:
            with path.open("rb") as stream:
                if _stream_contains_value(stream, sensitive_values):
                    findings.append(redact_sensitive_text(relative, sensitive_values))
                    contaminated.add(path)
    if findings:
        for path in contaminated:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise RuntimeError("L4 artifacts 偵測到宿主敏感環境值，拒絕保存 release gate：" +
                           ", ".join(findings))


def redact_sensitive_text(value: str, sensitive_values: tuple[bytes, ...]) -> str:
    redacted = str(value)
    for sensitive in sensitive_values:
        redacted = redacted.replace(sensitive.decode("utf-8", errors="ignore"), "<redacted>")
    return redacted


def redact_sensitive_object(value, sensitive_values: tuple[bytes, ...]):
    if isinstance(value, str):
        return redact_sensitive_text(value, sensitive_values)
    if isinstance(value, list):
        return [redact_sensitive_object(item, sensitive_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_object(item, sensitive_values) for item in value)
    if isinstance(value, dict):
        return {redact_sensitive_text(str(key), sensitive_values):
                redact_sensitive_object(item, sensitive_values) for key, item in value.items()}
    return value


def assert_manifest_contains_no_sensitive_values(manifest: dict, sensitive_values: tuple[bytes, ...]):
    if redact_sensitive_object(manifest, sensitive_values) != manifest:
        raise RuntimeError("L4 manifest 偵測到宿主敏感環境值，拒絕 release gate")


def snapshot_parallel_evidence(parent: Path, artifacts: Path, manifest: dict):
    """成功或失敗都保存 bounded truth；刪除 phase 後則沿用先前已保存的 snapshot。"""
    artifacts.mkdir(parents=True, exist_ok=True)
    for name in (*TRUTH_SNAPSHOT_NAMES, "REPORT.md", "history.log"):
        source_file = parent / name
        if source_file.is_file() and not source_file.is_symlink():
            shutil.copy2(source_file, artifacts / name)
    console = parent / "console.log"
    if console.is_file() and not console.is_symlink():
        (artifacts / "console-tail.log").write_text(
            console.read_text(encoding="utf-8", errors="replace")[-500_000:], encoding="utf-8")

    fleet_path = artifacts / "fleet.json"
    if fleet_path.is_file() and not fleet_path.is_symlink():
        try:
            fleet_state = json.loads(fleet_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            manifest["parallel_run_snapshot_error"] = str(error)
        else:
            manifest["parallel_run"] = {
                "run_id": fleet_state.get("run_id"),
                "phase": fleet_state.get("phase"),
                "resume_phase": fleet_state.get("resume_phase"),
                "integration_ref": fleet_state.get("integration_ref"),
                "initial_sha": fleet_state.get("initial_integration_sha"),
                "final_sha": fleet_state.get("expected_integration_sha"),
                "plan_sha256": fleet_state.get("plan_sha256"),
                "phase_history": fleet_state.get("phase_history") or [],
                "merge_history": fleet_state.get("merge_history") or [],
                "merge_transaction": fleet_state.get("merge_tx"),
                "tracks": [{key: track.get(key) for key in (
                    "name", "child_workspace", "branch_ref", "tip", "status", "restart_count",
                    "integration_validate_failures", "last_integration_error", "started_at", "ended_at",
                    "status_history", "event_history", "evidence_path", "evidence_sha256", "diagnostics")}
                           for track in fleet_state.get("tracks") or []],
            }

    index_truth_snapshots(artifacts, manifest)


def index_truth_snapshots(artifacts: Path, manifest: dict):
    truth_snapshots = []
    for name in TRUTH_SNAPSHOT_NAMES:
        path = artifacts / name
        if path.is_file() and not path.is_symlink():
            truth_snapshots.append({
                "name": name,
                "path": name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    if truth_snapshots:
        manifest["truth_snapshots"] = truth_snapshots


def require_parallel_truth_snapshots(artifacts: Path, manifest: dict):
    """Release gate 必須同時保存、解析且比對 primary 與 last-good truth。"""
    index_truth_snapshots(artifacts, manifest)
    indexed = {item["name"]: item for item in manifest.get("truth_snapshots") or []}
    missing = [name for name in TRUTH_SNAPSHOT_NAMES if name not in indexed]
    if missing:
        raise RuntimeError("parallel truth/checkpoint snapshot 不完整：" + ", ".join(missing))
    for primary_name, checkpoint_name in (
            ("fleet.json", "fleet.last-good.json"),
            ("state.json", "state.last-good.json")):
        primary = artifacts / primary_name
        checkpoint = artifacts / checkpoint_name
        try:
            primary_value = json.loads(primary.read_text(encoding="utf-8"))
            checkpoint_value = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"parallel truth/checkpoint 無法解析：{error}") from error
        if (primary_value != checkpoint_value or
                indexed[primary_name]["bytes"] != indexed[checkpoint_name]["bytes"] or
                indexed[primary_name]["sha256"] != indexed[checkpoint_name]["sha256"]):
            raise RuntimeError(f"parallel truth/checkpoint 不一致：{primary_name} / {checkpoint_name}")


def snapshot_git_evidence(clone: Path, artifacts: Path, manifest: dict):
    """只讀 Git 取證不因前一個 UI assertion 失敗而被跳過。"""
    if not (clone / ".git").exists():
        return
    artifacts.mkdir(parents=True, exist_ok=True)
    commands = (
        (["git", "fsck", "--full"], "git-fsck.log"),
        (["git", "log", "--graph", "--decorate", "--oneline", "--all", "-100"], "git-graph.log"),
        (["git", "worktree", "list", "--porcelain"], "git-worktrees.log"),
        (["git", "status", "--porcelain"], "git-status.log"),
    )
    recorded = {tuple(item.get("command") or []) for item in manifest.get("commands") or []}
    for command, name in commands:
        if tuple(command) in recorded:
            continue
        try:
            _, record = run(command, cwd=clone, log=artifacts / name, check=False)
            manifest.setdefault("commands", []).append(record)
        except OSError as error:
            manifest.setdefault("evidence_errors", []).append(f"{name}: {error}")


def index_playwright_artifacts(artifacts: Path, manifest: dict):
    """manifest 直接列出可稽核的 trace/video/screenshot，不只留下未知目錄。"""
    suffixes = {".zip": "trace", ".webm": "video", ".png": "screenshot"}
    indexed = []
    for path in sorted(artifacts.rglob("*")):
        kind = suffixes.get(path.suffix.lower())
        if kind and path.is_file() and not path.is_symlink():
            indexed.append({"kind": kind, "path": str(path.relative_to(artifacts)),
                            "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    manifest["playwright_artifacts"] = indexed


def require_playwright_artifacts(artifacts: Path, manifest: dict):
    """兩段真 UI 驗收都必須各自留下 trace/video/screenshot，否則不能簽 release gate。"""
    index_playwright_artifacts(artifacts, manifest)
    indexed = manifest["playwright_artifacts"]
    missing = []
    for phase in ("playwright-run/", "playwright-delete/"):
        for kind in ("trace", "video", "screenshot"):
            if not any(item["kind"] == kind and item["path"].startswith(phase) and item["bytes"] > 0
                       for item in indexed):
                missing.append(f"{phase}{kind}")
    if missing:
        raise RuntimeError("Playwright 稽核產物不完整：" + ", ".join(missing))


def reserve_loopback_port(max_attempts=100) -> PortReservation:
    """Reserve a loopback port across concurrent L4 harness processes.

    The kernel selects the port while the socket is still bound; O_EXCL then claims a
    process-shared lock before the socket is closed.  Server identity is verified after
    bind, so an unrelated non-harness process racing the same port cannot be accepted.
    """
    lock_root = Path(tempfile.gettempdir()) / f"loop-agent-l4-ports-{os.getuid()}"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for _ in range(max_attempts):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
            lock_path = lock_root / f"{port}.lock"
            token = uuid.uuid4().hex
            try:
                descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            try:
                payload = json.dumps({"pid": os.getpid(), "port": port, "token": token}).encode()
                os.write(descriptor, payload)
                os.fsync(descriptor)
            except Exception:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                raise
            finally:
                os.close(descriptor)
            return PortReservation(port, lock_path, token)
    raise RuntimeError("無法在跨程序 lock 下保留 L4 Dashboard port")


def dashboard_command(loop_cli: Path, port: int) -> list[str]:
    return [str(loop_cli), "dashboard", "--port", str(port)]


def _dashboard_json(base_url: str, path: str):
    with urlopen(f"{base_url}{path}", timeout=1) as response:
        if response.status != 200:
            raise OSError(f"Dashboard {path} HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def wait_for_dashboard_fixture(process: subprocess.Popen, base_url: str,
                               expected_config: Path, timeout=30):
    """Wait for this fixture, never merely any HTTP server that inherited the port."""
    deadline = time.monotonic() + timeout
    expected = expected_config.resolve()
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"production Dashboard 啟動前退出 rc={process.returncode}")
        try:
            health = _dashboard_json(base_url, "/api/health")
            config = _dashboard_json(base_url, "/api/config")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        actual_path = config.get("personal_config_path")
        if not config.get("config_override") or not actual_path or Path(actual_path).resolve() != expected:
            raise RuntimeError("Dashboard port 已被非本次 L4 fixture 的 server 佔用")
        if health.get("status") != "ok":
            raise RuntimeError(f"本次 L4 Dashboard health 不可用：{health.get('status')}")
        return {"health": health, "config": {
            "config_override": True,
            "personal_config_path": str(expected),
        }}
    raise RuntimeError("production Dashboard 未就緒或 fixture 身分無法驗證")


def codex_command_metadata(raw_command: str, source_kind: str) -> dict:
    """保留可執行的正規化命令，但 manifest 只取得去敏版本與明確 model。"""
    try:
        tokens = shlex.split(str(raw_command))
    except ValueError as error:
        raise RuntimeError(f"Codex command 無法用 shlex 解析：{error}") from error
    if not tokens:
        raise RuntimeError("Codex command 不可為空")
    normalized = shlex.join(tokens)
    redacted = []
    sensitive = False
    model = None
    redact_next = False
    for index, token in enumerate(tokens):
        lower = token.lower()
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            sensitive = True
            continue
        if lower in SENSITIVE_OPTION_NAMES:
            redacted.append(token)
            redact_next = True
            sensitive = True
            continue
        if any(lower.startswith(f"{name}=") for name in SENSITIVE_OPTION_NAMES):
            redacted.append(token.split("=", 1)[0] + "=<redacted>")
            sensitive = True
            continue
        if "=" in token and SENSITIVE_ENV_NAME.search(token.split("=", 1)[0]):
            redacted.append(token.split("=", 1)[0] + "=<redacted>")
            sensitive = True
            continue
        redacted.append(token)
        if lower in {"-m", "--model"} and index + 1 < len(tokens):
            model = tokens[index + 1]
        elif lower.startswith("--model="):
            model = token.split("=", 1)[1]
    if redact_next:
        raise RuntimeError("Codex command 的敏感選項缺少值")
    return {
        "command": normalized,
        "manifest_command": shlex.join(redacted),
        "model": model,
        "source": source_kind,
        "contains_sensitive_value": sensitive,
        "executable": tokens[0],
    }


def configured_codex(source: Path):
    override = os.environ.get("LOOP_L4_CODEX_CMD")
    if override:
        return codex_command_metadata(override, "environment_command_override")
    configured_path = os.environ.get("LOOP_AGENT_DASHBOARD_CONFIG")
    config = Path(configured_path or source / "dashboard.config.local.json")
    data = json.loads(config.read_text(encoding="utf-8"))
    matches = [item.get("cmd") for item in data.get("agent_cmds", []) if item.get("label") == "codex"]
    if len(matches) != 1:
        raise RuntimeError("個人設定必須恰有一個 label=codex 的 Agent CLI")
    source_kind = "dashboard_config_override" if configured_path else "personal_label_codex"
    return codex_command_metadata(matches[0], source_kind)


def goal_text(scenario: str):
    if scenario == "dr1":
        return """# L4 DR-1 goal

這是只存在於本次隔離 clone、啟動前明確不存在的 deterministic delivery contract。請由 Agent 自動拆成互不依賴的 backend/frontend tracks：

1. Backend 必須新增 `engine/l4_delivery_probe.py`，提供 `summarize_l4_parallel_phases(phases)`，把連續重複 phase 合併後以 ` → ` 串接；例如 `planning, planning, exec, done` 得到 `planning → exec → done`。新增 `tests/test_l4_delivery_probe.py` 精準測空輸入、重複與順序。
2. Frontend 必須新增 `ui/src/features/workspaces/l4DeliveryProbe.ts`，提供 `l4TrackProgressLabel(done, total)` 並回傳 `Parallel tracks ${done}/${total} merged`；`ParallelRunGroup.tsx` 的 `.parallel-run-head` 必須用此 helper 設定同值 `aria-label`，並在 `ui/e2e/dashboard-flow.spec.ts` 驗證。必須 build 並 commit `engine/ui` production assets。
3. Backend contract files 與 frontend contract files/production assets 必須由不同一般 tracks 交付；不得把工作集中在同一軌或只改文字敘述。

最後以 @final 執行完整 Python tests 與 `ui/npm run check:all`。不得人工修改 state、code 或 Git，也不得把本次 fixture contract 回寫原 source checkout。
"""
    return """# L4 DR-2 goal

依匯入 plan 在隔離 tracks 製造並由 agent 解決真 Git conflict；integration-only validator 失敗後必須自動 rollback、回送錯誤並由原 track 修復，最後 @final 全套驗收。
"""


def l4_process_environments(base_env: dict[str, str], *, workspace: Path, home: Path,
                            config: Path, base_url: str, repo: Path, artifacts: Path,
                            scenario: str, validate_cmd: str):
    """隔離 Dashboard/Agent runtime 與只供 Playwright 使用的 L4 fixture metadata。"""
    if scenario not in {"dr1", "dr2"}:
        raise ValueError(f"unsupported L4 scenario: {scenario}")
    # Dashboard 會再啟動 Fleet 與真 Codex；不得讓 harness scenario/plan 透過
    # inherited env 洩漏給 Agent，否則 DR-1 的自主規劃證據不成立。
    clean_base = sanitized_child_environment(base_env)
    dashboard_env = {
        **clean_base,
        "LOOP_AGENT_WORKSPACE_ROOT": str(workspace),
        "LOOP_AGENT_HOME": str(home),
        "LOOP_AGENT_DASHBOARD_CONFIG": str(config),
    }
    playwright_env = {
        **clean_base,
        "LOOP_L4_BASE_URL": base_url,
        "LOOP_L4_REPO": str(repo),
        "LOOP_L4_SCENARIO": scenario,
        "LOOP_L4_VALIDATE": validate_cmd,
        "LOOP_L4_VALIDATE_TIMEOUT": str(L4_VALIDATE_TIMEOUT_SECONDS),
        "LOOP_L4_ARTIFACTS": str(artifacts),
    }
    if scenario == "dr1":
        playwright_env["LOOP_L4_PLANNING_TIMEOUT"] = str(L4_PLANNING_TIMEOUT_SECONDS)
    else:
        playwright_env["LOOP_L4_PLAN"] = json.dumps(dr2_plan(), ensure_ascii=False)
    delete_env = {
        **clean_base,
        "LOOP_L4_BASE_URL": base_url,
        "LOOP_L4_SCENARIO": scenario,
        "LOOP_L4_ARTIFACTS": str(artifacts),
        "LOOP_L4_DELETE_PHASE": "1",
    }
    return dashboard_env, playwright_env, delete_env


def prepare_integration_validator(scenario: str, clone: Path, harness: Path):
    """DR-2 才建立 repo 外 immutable fault validator；DR-1 不得看見或執行它。"""
    if scenario == "dr1":
        return None, None
    if scenario != "dr2":
        raise ValueError(f"unsupported L4 scenario: {scenario}")
    validator = harness / "integration_validator.py"
    validator.write_text(
        (clone / "tests" / "dry_run" / "integration_validator.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    return validator, hashlib.sha256(validator.read_bytes()).hexdigest()


def l4_validate_command(python: Path, validator: Path | None):
    python_arg = shlex.quote(str(python))
    command = ("export LOOP_AGENT_WORKSPACE_ROOT=\"$TMPDIR/validation-workspace\"; "
               "export LOOP_AGENT_HOME=\"$TMPDIR/validation-home\"; "
               "mkdir -p \"$LOOP_AGENT_WORKSPACE_ROOT\" \"$LOOP_AGENT_HOME\" && "
               f"{python_arg} -m unittest discover -s tests -t . -q && "
               "cd ui && npm ci --prefer-offline --no-audit --no-fund && npm run check")
    if validator is not None:
        command += f" && cd .. && {python_arg} {shlex.quote(str(validator))}"
    return shlex.join(["sh", "-c", command])


def assert_dr1_contract_absent(repo: Path):
    existing = sorted(path for path in DR1_BACKEND_CONTRACT_PATHS |
                      {"ui/src/features/workspaces/l4DeliveryProbe.ts"}
                      if (repo / path).exists() or (repo / path).is_symlink())
    if existing:
        raise RuntimeError("DR-1 clone-only contract 已存在，fixture 不再 deterministic：" +
                           ", ".join(existing))


def validate_dr1_contract_behavior(repo: Path, python: Path):
    """以獨立 process 驗 deterministic backend contract，不依賴 agent 自述。"""
    script = (
        "from engine.l4_delivery_probe import summarize_l4_parallel_phases as f\n"
        "assert f([]) == ''\n"
        "assert f(['planning','planning','exec','done']) == 'planning → exec → done'\n"
    )
    result = subprocess.run([str(python), "-c", script], cwd=repo, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError("DR-1 deterministic backend contract 行為不符：" + result.stdout[-2000:])


def dr2_plan():
    validate = "在 repo 根目錄執行完整 validate 命令且 exit 0"
    return [
        {"order": 1, "track": "conflict-a", "scope": ["docs/**"],
         "task": (f"建立 docs/dr2-a.txt，並把 docs/dr2-shared.txt 唯一一行改成 track A 的相容文案後 commit。"
                  f"DoD：{validate}")},
        {"order": 2, "track": "conflict-b", "scope": ["docs/**"],
         "task": f"建立 docs/dr2-b.txt，並把 docs/dr2-shared.txt 唯一一行改成 track B 的相容文案後 commit；merge 時自行解衝突。integration validator 若要求 docs/dr2-compat.txt，依錯誤內容修復並 commit。DoD：{validate}"},
        {"order": 3, "track": "@final", "scope": ["docs/**", "tests/**", "ui/**"],
         "task": "確認兩個 marker、衝突解法與 docs/dr2-compat.txt 均存在，執行 Python tests 及 ui/npm run check:all。DoD：兩個命令均 exit 0"},
    ]


def require_subsequence(values, required, label):
    """要求 required 依序出現；中間允許 stop/resume 或其他合法狀態。"""
    position = 0
    for value in values:
        if position < len(required) and value == required[position]:
            position += 1
    if position != len(required):
        raise RuntimeError(f"{label} 缺少依序狀態 {required}；實際 {values}")


def _git_output(repo: Path, *args, check=True):
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)} 失敗：{result.stdout}{result.stderr}")
    return result


def _git_is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return _git_output(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def git_changed_paths(repo: Path, older: str, newer: str) -> set[str]:
    output = _git_output(repo, "diff", "--name-only", "--diff-filter=ACMRT", older, newer).stdout
    return {line.strip() for line in output.splitlines() if line.strip()}


def assert_source_checkout_unchanged(source: Path, expected_sha: str):
    actual_sha = _git_output(source, "rev-parse", "HEAD").stdout.strip()
    dirty = _git_output(source, "status", "--porcelain").stdout
    if actual_sha != expected_sha or dirty:
        raise RuntimeError("L4 執行期間原 source checkout 的 HEAD 或工作樹已改變，證據不可重現")


def assert_continuous_event_history(evidence_events: list[dict], final_events: list[dict], track: str):
    """Evidence 在 cleanup 前擷取；final history 只能接續 bounded cleanup journal。"""
    cleanup_events = [
        "cleanup-evidence-captured", "cleanup-worktree-removed", "cleanup-child-removed", "cleaned",
    ]
    if not all(isinstance(event, dict) and isinstance(event.get("event"), str)
               for event in final_events):
        raise RuntimeError(f"track {track} fleet event history 結構不合法")
    for overlap in range(min(len(evidence_events), len(final_events)), 0, -1):
        if evidence_events[-overlap:] != final_events[:overlap]:
            continue
        continuation = final_events[overlap:]
        expected_overlap = min(len(evidence_events), FLEET_EVENT_HISTORY_LIMIT - len(continuation))
        if overlap != expected_overlap or [event.get("event") for event in continuation] != cleanup_events:
            break
        return
    raise RuntimeError(f"track {track} fleet/event evidence history 不是連續 cleanup 延伸")


def prove_dr2_divergent_conflict(repo: Path, initial_sha: str, final_sha: str) -> dict:
    """以兩個同基底、同一行但內容相異的 commits 證明 adversarial conflict 確實成立。"""
    commits = _git_output(repo, "log", "--all", "--format=%H", "--", "docs/dr2-shared.txt").stdout.splitlines()
    candidates = {"a": [], "b": []}
    for commit in commits:
        shown = _git_output(repo, "show", f"{commit}:docs/dr2-shared.txt", check=False)
        if shown.returncode:
            continue
        content = shown.stdout.lower()
        if "track a" in content and "track b" not in content:
            candidates["a"].append(commit)
        if "track b" in content and "track a" not in content:
            candidates["b"].append(commit)
    for commit_a in candidates["a"]:
        for commit_b in candidates["b"]:
            if _git_is_ancestor(repo, commit_a, commit_b) or _git_is_ancestor(repo, commit_b, commit_a):
                continue
            merge_base = _git_output(repo, "merge-base", commit_a, commit_b).stdout.strip()
            if merge_base != initial_sha:
                continue
            if not _git_is_ancestor(repo, commit_a, final_sha) or not _git_is_ancestor(repo, commit_b, final_sha):
                continue
            return {"track_a_commit": commit_a, "track_b_commit": commit_b, "merge_base": merge_base}
    raise RuntimeError(
        "DR-2 無法證明真 conflict：缺少從 initial SHA 分岔、分別寫入 track A/B 且均進 final 的 commits")


def validate_report(report_path: Path, fleet_state: dict, scenario: str):
    if not report_path.is_file() or report_path.is_symlink():
        raise RuntimeError("parent REPORT.md 不存在或不是實體檔案")
    report = report_path.read_text(encoding="utf-8", errors="replace")
    required = ("# Parallel Run Report", "## Phase history", "## Merge transaction history", "## Tracks")
    missing = [value for value in required if value not in report]
    for track in fleet_state.get("tracks") or []:
        marker = f"### `{track.get('name')}`"
        if marker not in report:
            missing.append(marker)
        branch = str(track.get("branch_ref") or "")
        if branch and branch not in report:
            missing.append(branch)
    final_sha = str(fleet_state.get("expected_integration_sha") or "")
    if final_sha and final_sha not in report:
        missing.append("final SHA")
    if scenario == "dr2" and not re.search(r"validate rollbacks:\s*[1-9]", report):
        missing.append("DR-2 rollback count")
    if missing:
        raise RuntimeError("REPORT.md 證據不完整：" + ", ".join(missing))


def validate_parent_track_evidence(parent: Path, artifacts: Path, fleet_state: dict,
                                   expected_agent_command: str, expected_validate_command: str,
                                   scenario: str) -> list[dict]:
    """Fail closed until Fleet persists bounded child truth before deleting child workspaces.

    Each track stores ``evidence_path/evidence_sha256``. The parent-relative JSON contains
    schema_version, track, child_workspace, captured_at, state, no_progress_count,
    agent/validate command SHA-256, prompt_artifacts, console_tail, history_tail and event_history.
    """
    expected_command_hash = hashlib.sha256(expected_agent_command.encode()).hexdigest()
    expected_validate_hash = hashlib.sha256(expected_validate_command.encode()).hexdigest()
    copied = []
    all_events = []
    sha_re = re.compile(r"^[0-9a-f]{64}$")
    for track in fleet_state.get("tracks") or []:
        name = str(track.get("name") or "")
        recorded_path = Path(str(track.get("evidence_path") or ""))
        expected_hash = str(track.get("evidence_sha256") or "")
        if not recorded_path.parts or ".." in recorded_path.parts or not sha_re.fullmatch(expected_hash):
            raise RuntimeError(f"track {name} 缺少安全的 evidence_path/evidence_sha256")
        evidence_path = recorded_path if recorded_path.is_absolute() else parent / recorded_path
        try:
            evidence_path.resolve().relative_to(parent.resolve())
        except ValueError as error:
            raise RuntimeError(f"track {name} evidence_path 不在 fleet parent 內") from error
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise RuntimeError(f"track {name} bounded evidence JSON 不存在或不是實體檔案")
        if evidence_path.stat().st_size > 2_000_000:
            raise RuntimeError(f"track {name} bounded evidence JSON 超過 2 MB")
        actual_hash = sha256_file(evidence_path)
        if actual_hash != expected_hash:
            raise RuntimeError(f"track {name} bounded evidence SHA-256 不一致")
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"track {name} bounded evidence JSON 無法解析：{error}") from error
        if not isinstance(evidence, dict) or evidence.get("schema_version") != 1:
            raise RuntimeError(f"track {name} 缺少 bounded evidence schema v1")
        if evidence.get("track") != name or evidence.get("child_workspace") != track.get("child_workspace"):
            raise RuntimeError(f"track {name} bounded evidence 身分不一致")
        if not isinstance(evidence.get("captured_at"), str) or not evidence["captured_at"].strip():
            raise RuntimeError(f"track {name} bounded evidence 缺少 captured_at")
        if evidence.get("agent_command_sha256") != expected_command_hash:
            raise RuntimeError(f"track {name} agent command hash 與設定的 Codex 不一致")
        if evidence.get("validate_command_sha256") != expected_validate_hash:
            raise RuntimeError(f"track {name} validate command hash 與 production UI 輸入不一致")
        state = evidence.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("round"), int) or state["round"] < 1:
            raise RuntimeError(f"track {name} bounded state/round 不合法")
        no_progress = evidence.get("no_progress_count")
        if not isinstance(no_progress, int) or no_progress < 0:
            raise RuntimeError(f"track {name} evidence.no_progress_count 不合法")
        prompt_artifacts = evidence.get("prompt_artifacts")
        if not isinstance(prompt_artifacts, list) or not prompt_artifacts:
            raise RuntimeError(f"track {name} 缺少 prompt artifact index")
        if len(prompt_artifacts) > 500:
            raise RuntimeError(f"track {name} prompt artifact index 超過 bounded 上限")
        prompt_total = 0
        for item in prompt_artifacts:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or \
                    Path(item["name"]).name != item["name"] or \
                    not sha_re.fullmatch(str(item.get("sha256") or "")) or \
                    not isinstance(item.get("size"), int) or item["size"] <= 0:
                raise RuntimeError(f"track {name} prompt artifact index 不合法")
            prompt_total += item["size"]
            if prompt_total > 8_000_000:
                raise RuntimeError(f"track {name} prompt artifact index 超過 bounded 上限")
            prompt_path = evidence_path.parent / "prompts" / item["name"]
            if not prompt_path.is_file() or prompt_path.is_symlink() or \
                    prompt_path.stat().st_size != item["size"] or sha256_file(prompt_path) != item["sha256"]:
                raise RuntimeError(f"track {name} prompt artifact 檔案/hash/size 不一致：{item['name']}")
        console_tail = evidence.get("console_tail")
        if not isinstance(console_tail, str) or not console_tail.strip() or len(console_tail.encode()) > 500_000:
            raise RuntimeError(f"track {name} console_tail 缺少或超過 bounded 上限")
        history_tail = evidence.get("history_tail")
        if not isinstance(history_tail, (str, list)) or not history_tail or len(
                json.dumps(history_tail, ensure_ascii=False).encode()) > 500_000:
            raise RuntimeError(f"track {name} history_tail 缺少或超過 bounded 上限")
        events = evidence.get("event_history")
        if not isinstance(events, list) or not events or len(events) > FLEET_EVENT_HISTORY_LIMIT:
            raise RuntimeError(f"track {name} event_history 缺少或超過 bounded 上限")
        if not all(isinstance(event, dict) and isinstance(event.get("event"), str) for event in events):
            raise RuntimeError(f"track {name} event_history 結構不合法")
        track_events = track.get("event_history")
        if not isinstance(track_events, list) or not track_events:
            raise RuntimeError(f"track {name} fleet/event evidence history 不存在")
        assert_continuous_event_history(events, track_events, name)
        all_events.extend((name, event) for event in events)
        safe_name = safe_track_name(name)
        destination = artifacts / "track-evidence" / safe_name / "evidence.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(evidence_path, destination)
        copied.append({"track": name, "kind": "evidence",
                       "path": str(destination.relative_to(artifacts)),
                       "bytes": evidence_path.stat().st_size, "sha256": actual_hash,
                       "round_count": state["round"], "no_progress_count": no_progress,
                       "prompt_count": len(prompt_artifacts)})
        for item in prompt_artifacts:
            source_prompt = evidence_path.parent / "prompts" / item["name"]
            prompt_destination = destination.parent / "prompts" / item["name"]
            prompt_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_prompt, prompt_destination)
            copied.append({"track": name, "kind": "prompt",
                           "path": str(prompt_destination.relative_to(artifacts)),
                           "bytes": item["size"], "sha256": item["sha256"]})
    stages = [(event.get("phase"), event.get("merge_stage")) for _, event in all_events]
    if not any(phase == "exec" for phase, _ in stages):
        raise RuntimeError("parent track evidence 缺少 exec event")
    if not any(stage == "sync" for _, stage in stages) or not any(stage == "confirm" for _, stage in stages):
        raise RuntimeError("parent track evidence 缺少 merge sync/confirm event")
    if scenario == "dr2" and not any(
            track.get("integration_validate_failures", 0) > 0 and
            any(event.get("status") == "repairing" or event.get("event") == "repairing"
                for event in track.get("event_history") or [])
            for track in fleet_state.get("tracks") or []):
        raise RuntimeError("DR-2 parent track evidence 缺少 repairing event")
    return copied


def validate_parallel_run_evidence(fleet_state: dict, *, scenario: str, repo: Path,
                                   workspace_root: Path, parent_state: dict,
                                   expected_agent_command: str, expected_validate_command: str,
                                   fixture_sha: str) -> dict:
    tracks = fleet_state.get("tracks") or []
    if fleet_state.get("phase") != "done" or not tracks:
        raise RuntimeError("parallel run 未以非空 tracks/done 結束")
    if any(track.get("status") != "cleaned" for track in tracks):
        raise RuntimeError("parallel run 不是全部 tracks cleaned")
    names = [track.get("name") for track in tracks]
    if len(names) != len(set(names)) or names.count("@final") != 1:
        raise RuntimeError("parallel run track 身分重複或缺少唯一 @final")
    final_sha = str(fleet_state.get("expected_integration_sha") or "")
    initial_sha = str(fleet_state.get("initial_integration_sha") or "")
    integration_ref = str(fleet_state.get("integration_ref") or "")
    if not final_sha or not initial_sha or not integration_ref:
        raise RuntimeError("parallel run 缺少 initial/final/integration ref 身分")
    if initial_sha != fixture_sha:
        raise RuntimeError("parallel run initial SHA 不是本次固定 fixture SHA")
    actual_ref = _git_output(repo, "rev-parse", integration_ref).stdout.strip()
    actual_head = _git_output(repo, "rev-parse", "HEAD").stdout.strip()
    if actual_ref != final_sha or actual_head != final_sha or not _git_is_ancestor(repo, initial_sha, final_sha):
        raise RuntimeError("parallel run integration ref/HEAD/final SHA 或 ancestry 不一致")
    config = fleet_state.get("config") or {}
    actual_thresholds = {
        "flag": config.get("flag_threshold"), "done": config.get("done_threshold"),
        "merge": config.get("merge_threshold"), "max_parallel": config.get("max_parallel"),
    }
    if actual_thresholds != EXPECTED_THRESHOLDS:
        raise RuntimeError(f"L4 必須使用 shipped thresholds {EXPECTED_THRESHOLDS}；實際 {actual_thresholds}")
    try:
        actual_agent_command = shlex.join(shlex.split(str(config.get("agent_cmd") or "")))
    except ValueError as error:
        raise RuntimeError(f"fleet persisted agent command 不合法：{error}") from error
    if actual_agent_command != expected_agent_command:
        raise RuntimeError("fleet persisted agent command 不是本次設定的 Codex command")
    try:
        actual_validate_command = shlex.join(shlex.split(str(config.get("validate_cmd") or "")))
        normalized_expected_validate = shlex.join(shlex.split(expected_validate_command))
    except ValueError as error:
        raise RuntimeError(f"fleet persisted validate command 不合法：{error}") from error
    if actual_validate_command != normalized_expected_validate:
        raise RuntimeError("fleet persisted validate command 不是 production UI 輸入的完整驗證命令")
    actual_validate_timeout = config.get("validate_timeout")
    if actual_validate_timeout != L4_VALIDATE_TIMEOUT_SECONDS:
        raise RuntimeError(
            f"fleet persisted validate_timeout 必須是 L4 的 {L4_VALIDATE_TIMEOUT_SECONDS} 秒；"
            f"實際 {actual_validate_timeout!r}")
    child_workspaces = []
    track_ports = {}
    runtime_paths = set()
    for track in tracks:
        child = str(track.get("child_workspace") or "")
        if not child or Path(child).name != child or child in {".", ".."}:
            raise RuntimeError(f"track {track.get('name')} child workspace 身分不合法")
        child_workspaces.append(child)
        index, port, track_env = track.get("index"), track.get("port"), track.get("env")
        if (not isinstance(index, int) or isinstance(index, bool) or index < 1 or
                not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535 or
                not isinstance(track_env, dict) or track_env.get("LOOP_TRACK_PORT") != str(port) or
                track_env.get("LOOP_TRACK_INDEX") != str(index) or
                track_env.get("LOOP_TRACK_NAME") != track.get("name")):
            raise RuntimeError(f"track {track.get('name')} 缺少 frozen per-track env/port identity")
        tmp_dir = str(track_env.get("TMPDIR") or "")
        cache_dir = str(track_env.get("XDG_CACHE_HOME") or "")
        npm_cache = str(track_env.get("npm_config_cache") or "")
        track_paths = {tmp_dir, cache_dir, npm_cache}
        if (not tmp_dir or not cache_dir or not npm_cache or len(track_paths) != 3 or
                any(path in runtime_paths for path in track_paths)):
            raise RuntimeError(f"track {track.get('name')} runtime/cache 未隔離")
        runtime_paths.update(track_paths)
        if port in track_ports.values():
            raise RuntimeError("不同 track 共用 LOOP_TRACK_PORT")
        track_ports[str(track.get("name"))] = port
        if (workspace_root / child).exists() or (workspace_root / child).is_symlink():
            raise RuntimeError(f"成功後 child workspace 尚未清理：{child}")
        branch = str(track.get("branch_ref") or "")
        tip = str(track.get("tip") or "")
        if not branch or not tip or _git_output(repo, "rev-parse", branch).stdout.strip() != tip or \
                not _git_is_ancestor(repo, tip, final_sha):
            raise RuntimeError(f"track {track.get('name')} branch/tip/final ancestry 不一致")
    plan = fleet_state.get("plan") or []
    general_names = {task.get("track") for task in plan if task.get("track") != "@final"}
    if len(general_names) < 2 or not any(task.get("track") == "@final" for task in plan):
        raise RuntimeError(f"{scenario.upper()} 必須至少兩條一般 track 與 @final")
    if general_names != set(names) - {"@final"}:
        raise RuntimeError("fleet plan 與實際一般 track 身分不一致")
    phases = [entry.get("phase") for entry in fleet_state.get("phase_history") or []]
    if scenario == "dr1":
        require_subsequence(phases, ["planning", "splitting", "exec", "stopping", "stopped", "exec",
                                     "final", "cleaning", "done"], "DR-1 phase history")
        if not isinstance(parent_state.get("round"), int) or parent_state.get("round", 0) < 1:
            raise RuntimeError("DR-1 缺少真 Codex planning round 證據")
        resumed_tracks = []
        for track in tracks:
            if track.get("name") == "@final":
                continue
            statuses = [entry.get("status") for entry in track.get("status_history") or []]
            try:
                require_subsequence(statuses, ["running", "stopped", "running"],
                                    f"DR-1 child {track.get('name')} stop/resume history")
            except RuntimeError:
                continue
            resumed_tracks.append(str(track.get("name")))
        if not resumed_tracks:
            raise RuntimeError("DR-1 缺少任一 child ordered running → stopped → running 證據")
    elif scenario == "dr2":
        if set(names) != {"conflict-a", "conflict-b", "@final"}:
            raise RuntimeError(f"DR-2 track 結構漂移：{names}")
        require_subsequence(phases, ["planning", "splitting", "exec", "final", "cleaning", "done"],
                            "DR-2 phase history")
    else:
        raise RuntimeError(f"未知 L4 scenario：{scenario}")

    merge_history = fleet_state.get("merge_history") or []
    grouped = {}
    for entry in merge_history:
        if not isinstance(entry, dict) or not entry.get("track") or not entry.get("candidate_sha"):
            raise RuntimeError("merge history entry 結構不完整")
        grouped.setdefault((entry["track"], entry["candidate_sha"]), []).append(entry)
    for (track_name, candidate), entries in grouped.items():
        stages = [entry.get("stage") for entry in entries]
        if "prepared" not in stages:
            continue
        if "rolled-back" in stages:
            require_subsequence(stages, ["prepared", "ref-updated", "validating",
                                         "rollback-prepared", "rolled-back"],
                                f"{track_name}/{candidate[:8]} rollback history")
        else:
            require_subsequence(stages, ["prepared", "ref-updated", "validating", "validated"],
                                f"{track_name}/{candidate[:8]} CAS history")
    for track in tracks:
        key = (track.get("name"), track.get("tip"))
        stages = [entry.get("stage") for entry in grouped.get(key, [])]
        if "validated" not in stages:
            raise RuntimeError(f"track {track.get('name')} final candidate 缺少 CAS validated history")

    dr1_track_paths = {}
    if scenario == "dr1":
        for track in tracks:
            name, candidate = track.get("name"), track.get("tip")
            if name == "@final":
                continue
            validated = next((entry for entry in grouped.get((name, candidate), [])
                              if entry.get("stage") == "validated"), None)
            expected = str((validated or {}).get("expected_sha") or "")
            if not expected or not _git_is_ancestor(repo, expected, str(candidate)):
                raise RuntimeError(f"DR-1 track {name} 缺少可歸屬的 CAS expected/candidate")
            dr1_track_paths[str(name)] = git_changed_paths(repo, expected, str(candidate))

        def has_backend_delivery(paths):
            return DR1_BACKEND_CONTRACT_PATHS.issubset(paths)

        def has_frontend_delivery(paths):
            return (DR1_FRONTEND_CONTRACT_PATHS.issubset(paths) and
                    any(path.startswith("engine/ui/") for path in paths))

        backend_tracks = {name for name, paths in dr1_track_paths.items() if has_backend_delivery(paths)}
        frontend_tracks = {name for name, paths in dr1_track_paths.items() if has_frontend_delivery(paths)}
        if not any(backend != frontend for backend in backend_tracks for frontend in frontend_tracks):
            raise RuntimeError("DR-1 CAS diff 未證明不同 tracks 分別交付 engine+tests 與 ui+production assets")
        final_paths = git_changed_paths(repo, fixture_sha, final_sha)
        if not has_backend_delivery(final_paths) or not has_frontend_delivery(final_paths):
            raise RuntimeError("DR-1 fixture..final diff 缺少 deterministic backend/frontend contract 或 production assets")

    scenario_evidence = {"actual_thresholds": actual_thresholds,
                         "actual_validate_timeout": actual_validate_timeout,
                         "child_workspaces": child_workspaces, "track_names": names,
                         "track_ports": track_ports,
                         **({"resumed_child_tracks": resumed_tracks}
                            if scenario == "dr1" else {}),
                         **({"track_changed_paths": {name: sorted(paths)
                                                     for name, paths in dr1_track_paths.items()}}
                            if scenario == "dr1" else {})}
    if scenario == "dr2":
        failed_groups = [((track, candidate), entries) for (track, candidate), entries in grouped.items()
                         if "rolled-back" in [entry.get("stage") for entry in entries]]
        if not failed_groups:
            raise RuntimeError("DR-2 缺少 rolled-back candidate")
        repaired = None
        for (failed_track, failed_candidate), failed_entries in failed_groups:
            if not any(entry.get("validation_error") for entry in failed_entries):
                continue
            for (track_name, candidate), entries in grouped.items():
                if track_name != failed_track or candidate == failed_candidate:
                    continue
                if "validated" in [entry.get("stage") for entry in entries] and \
                        _git_is_ancestor(repo, failed_candidate, candidate):
                    repaired = {"track": track_name, "failed_candidate": failed_candidate,
                                "repaired_candidate": candidate}
                    break
            if repaired:
                break
        if repaired is None:
            raise RuntimeError("DR-2 缺少同 track 的新修復 commit/CAS PASS 證據")
        repaired_track = next(track for track in tracks if track.get("name") == repaired["track"])
        statuses = [entry.get("status") for entry in repaired_track.get("status_history") or []]
        if repaired_track.get("integration_validate_failures", 0) < 1 or "repairing" not in statuses or \
                not repaired_track.get("last_integration_error"):
            raise RuntimeError("DR-2 缺少 rollback error、repairing status或 failure counter")
        conflict = prove_dr2_divergent_conflict(
            repo, str(fleet_state.get("initial_integration_sha") or ""),
            str(fleet_state.get("expected_integration_sha") or ""))
        scenario_evidence.update(repair=repaired, conflict=conflict)
    return scenario_evidence


def materialize(source: Path, target: Path, allow_dirty: bool):
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=source, text=True,
                           capture_output=True, check=True).stdout
    if dirty and not allow_dirty:
        raise RuntimeError("L4 release gate 要求原 checkout 乾淨；開發 smoke 可明確加 --allow-dirty-snapshot")
    subprocess.run(["git", "clone", "--no-hardlinks", "-q", str(source), str(target)], check=True)
    subprocess.run(["git", "config", "user.email", "l4@loop-agent.local"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "loop-agent L4"], cwd=target, check=True)
    current_branch = subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=target,
                                    text=True, capture_output=True)
    if current_branch.returncode:
        subprocess.run(["git", "switch", "-q", "-c", "l4-candidate"], cwd=target, check=True)
    if dirty:
        patch = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=source, text=True,
                               capture_output=True, check=True).stdout
        if patch:
            subprocess.run(["git", "apply", "--binary", "-"], cwd=target, text=True,
                           input=patch, check=True)
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=source,
                                   text=True, capture_output=True, check=True).stdout.splitlines()
        for relative in untracked:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, destination)
        subprocess.run(["git", "add", "-A"], cwd=target, check=True)
        subprocess.run(["git", "commit", "-qm", "L4 development snapshot"], cwd=target, check=True)
    return not bool(dirty)


def stop_dashboards(dashboards: list[subprocess.Popen]):
    for dashboard in dashboards:
        if dashboard.poll() is not None:
            continue
        try:
            os.killpg(dashboard.pid, __import__("signal").SIGINT)
        except ProcessLookupError:
            continue
        try:
            dashboard.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(dashboard.pid, __import__("signal").SIGKILL)
            except ProcessLookupError:
                pass
    dashboards.clear()


def _coordinator_option(argv: list[str], name: str, start: int):
    prefix = name + "="
    for index in range(start, len(argv)):
        token = argv[index]
        if token.startswith(prefix):
            return token[len(prefix):]
        if token == name and index + 1 < len(argv):
            return argv[index + 1]
    return None


def fixture_coordinator_pids(snapshot: dict, workspace_name: str, clone: Path) -> set[int]:
    """Find this L4 coordinator without trusting workspace state or a PID file."""
    expected_repo = clone.resolve()
    matches = set()
    for pid, process in snapshot.items():
        try:
            argv = shlex.split(str(process.get("command") or ""))
        except ValueError:
            continue
        # Dashboard launches these coordinators directly as ``python -m ...``.
        # Requiring the interpreter-position pair avoids matching an agent command
        # or arbitrary text later in another process's argv.
        if len(argv) < 3 or argv[1] != "-m" or argv[2] not in ("engine.loop", "engine.fleet"):
            continue
        option_start = 3
        if _coordinator_option(argv, "--name", option_start) != workspace_name:
            continue
        repo = _coordinator_option(argv, "--repo", option_start)
        if not repo:
            continue
        try:
            if Path(repo).expanduser().resolve() != expected_repo:
                continue
        except OSError:
            continue
        matches.add(int(pid))
    return matches


def stop_bounded_coordinator_roots(dashboard, initial: dict, roots: set[int], *,
                                   grace_seconds=8.0, force_seconds=2.0):
    """Stop one identity-frozen process tree without signalling a reused PID."""
    captured = dashboard._snapshot_descendants(initial, roots)
    requested = []
    signal_errors = []
    before_signal = dashboard._process_snapshot()
    if before_signal is None:
        raise RuntimeError("cleanup 無法在 signal 前重取 process snapshot")
    for pid in sorted(roots):
        if not dashboard._same_process_instance(initial.get(pid), before_signal.get(pid)):
            continue
        try:
            os.kill(pid, signal.SIGINT)
            requested.append(pid)
        except ProcessLookupError:
            continue
        except (PermissionError, OSError) as error:
            signal_errors.append(f"SIGINT {pid}: {error}")

    deadline = time.monotonic() + max(0.0, grace_seconds)
    current = dashboard._process_snapshot()
    if current is None:
        raise RuntimeError("cleanup grace period 無法取得 process snapshot")
    while (any(dashboard._same_process_instance(initial.get(pid), current.get(pid))
               for pid in roots) and time.monotonic() < deadline):
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        current = dashboard._process_snapshot()
        if current is None:
            raise RuntimeError("cleanup grace period 無法取得 process snapshot")

    force = [pid for pid in captured
             if dashboard._same_process_instance(initial.get(pid), current.get(pid))]
    depth = {}
    for pid in force:
        value, seen = 0, set()
        parent = initial.get(pid, {}).get("ppid")
        while parent in captured and parent not in seen:
            seen.add(parent)
            value += 1
            parent = initial.get(parent, {}).get("ppid")
        depth[pid] = value
    forced = []
    for pid in sorted(force, key=lambda value: (depth[value], value), reverse=True):
        latest = dashboard._process_snapshot()
        if latest is None:
            raise RuntimeError("cleanup force 前無法取得 process snapshot")
        if not dashboard._same_process_instance(initial.get(pid), latest.get(pid)):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            forced.append(pid)
        except ProcessLookupError:
            continue
        except (PermissionError, OSError) as error:
            signal_errors.append(f"SIGKILL {pid}: {error}")

    deadline = time.monotonic() + max(0.0, force_seconds)
    final = dashboard._process_snapshot()
    if final is None:
        raise RuntimeError("cleanup force period 無法取得 process snapshot")
    remaining = [pid for pid in captured
                 if dashboard._same_process_instance(initial.get(pid), final.get(pid))]
    while remaining and time.monotonic() < deadline:
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        final = dashboard._process_snapshot()
        if final is None:
            raise RuntimeError("cleanup verify 無法取得 process snapshot")
        remaining = [pid for pid in captured
                     if dashboard._same_process_instance(initial.get(pid), final.get(pid))]
    if signal_errors or remaining:
        raise RuntimeError(
            "L4 scoped cleanup 無法清空 identity-frozen process tree：" +
            "; ".join([*signal_errors, *(f"remaining {pid}" for pid in remaining)]))
    return {"roots": sorted(roots), "captured": sorted(captured),
            "requested": requested, "forced": forced, "remaining": []}, final


def run_scoped_coordinator_cleanup(python: Path, clone: Path, env: dict[str, str],
                                   log: Path, workspace_name: str):
    """在 Dashboard process 外，以 persisted truth + exact fixture identity 清場。"""
    workspace_root = Path(str(env.get("LOOP_AGENT_WORKSPACE_ROOT") or "")).resolve()
    if not str(env.get("LOOP_AGENT_WORKSPACE_ROOT") or "") or not workspace_root.is_dir():
        raise RuntimeError("L4 scoped cleanup 缺少有效 workspace root")
    if not workspace_name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", workspace_name):
        raise RuntimeError("L4 scoped cleanup 缺少合法 workspace name")
    script = (
        "import json\n"
        "from engine import dashboard\n"
        "from tests.dry_run.run_full_project import fixture_coordinator_pids, stop_bounded_coordinator_roots\n"
        "from pathlib import Path\n"
        "import sys\n"
        "snapshot = dashboard._process_snapshot()\n"
        "if snapshot is None:\n"
        "    raise RuntimeError('cleanup initial process snapshot 失敗')\n"
        "persisted = dashboard._workspace_coordinator_pids(snapshot)\n"
        "fixture = fixture_coordinator_pids(snapshot, sys.argv[1], Path(sys.argv[2]))\n"
        "result, final = stop_bounded_coordinator_roots(dashboard, snapshot, persisted | fixture)\n"
        "remaining_persisted = dashboard._workspace_coordinator_pids(final)\n"
        "remaining_fixture = fixture_coordinator_pids(final, sys.argv[1], Path(sys.argv[2]))\n"
        "remaining = sorted(remaining_persisted | remaining_fixture)\n"
        "result['persisted_roots'] = sorted(persisted)\n"
        "result['fixture_roots'] = sorted(fixture)\n"
        "if remaining:\n"
        "    raise RuntimeError('cleanup verify 仍有 coordinator: ' + repr(remaining))\n"
        "print(json.dumps({'cleanup': result, 'remaining': remaining}))\n"
    )
    result, record = run(
        [str(python), "-c", script, workspace_name, str(clone.resolve())],
        cwd=clone, env=env, log=log, timeout=30)
    payload = None
    for line in reversed((result.stdout or "").splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "cleanup" in candidate:
            payload = candidate
            break
    if payload is None or payload.get("remaining") != []:
        raise RuntimeError("L4 scoped cleanup 未留下可驗證的 empty remaining manifest")
    return payload, record


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("dr1", "dr2"), required=True)
    parser.add_argument("--source", default=str(ROOT))
    parser.add_argument("--output", default="")
    parser.add_argument("--allow-dirty-snapshot", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    source = Path(args.source).resolve()
    temp_root = Path(args.output).resolve() if args.output else Path(tempfile.mkdtemp(prefix=f"loop-l4-{args.scenario}-"))
    clone, workspace, home, harness, artifacts = (temp_root / name for name in
                                                   ("source", "workspace", "home", "harness", "artifacts"))
    for directory in (workspace, home, harness, artifacts):
        directory.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "scenario": args.scenario, "started_at": time.time(),
                "root": str(temp_root), "commands": [], "passed": False}
    dashboards = []
    dashboard_logs = []
    port_reservation = None
    python = None
    dashboard_env = None
    playwright_env = None
    host_sensitive_values = sensitive_environment_values()
    base_env = sanitized_child_environment()
    try:
        source_head_at_start = _git_output(source, "rev-parse", "HEAD").stdout.strip()
        manifest["release_gate_eligible"] = materialize(source, clone, args.allow_dirty_snapshot)
        manifest["source_sha"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, text=True,
                                                capture_output=True, check=True).stdout.strip()
        if manifest["release_gate_eligible"] and manifest["source_sha"] != source_head_at_start:
            raise RuntimeError("固定 clone SHA 與 L4 啟動時的 source HEAD 不一致")
        if args.scenario == "dr1":
            assert_dr1_contract_absent(clone)
        (clone / "goal.md").write_text(goal_text(args.scenario), encoding="utf-8")
        if args.scenario == "dr2":
            (clone / "docs" / "dr2-shared.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "goal.md", "docs/dr2-shared.txt"], cwd=clone,
                       check=True, stderr=subprocess.DEVNULL) if args.scenario == "dr2" else subprocess.run(
                           ["git", "add", "goal.md"], cwd=clone, check=True)
        subprocess.run(["git", "commit", "-qm", f"L4 {args.scenario} fixture"], cwd=clone, check=True)
        codex = configured_codex(source)
        codex_cmd = codex["command"]
        manifest.update(
            codex_config_source=codex["source"],
            codex_command=codex["manifest_command"],
            codex_model=codex["model"],
        )
        if codex["source"] != "personal_label_codex":
            manifest["release_gate_eligible"] = False
        if codex["contains_sensitive_value"]:
            raise RuntimeError(
                "Codex command 含 inline credential/token；L4 artifacts 會保留隔離 config，請改用環境認證")
        manifest["codex_command_sha256"] = hashlib.sha256(codex_cmd.encode()).hexdigest()
        if not codex["model"]:
            raise RuntimeError("Codex command 必須明確帶 -m/--model，manifest 才能證明實際 model")
        codex_path = shutil.which(codex["executable"])
        if not codex_path:
            raise RuntimeError("configured Codex executable 不存在")
        if Path(codex_path).name != "codex":
            raise RuntimeError(f"L4 真 Codex executable 必須名為 codex；實際 {Path(codex_path).name}")
        codex_version = subprocess.run([codex_path, "--version"], text=True, capture_output=True,
                                       check=True, env=base_env).stdout.strip()
        if "codex" not in codex_version.lower():
            raise RuntimeError("configured executable 的 --version 無法證明是 Codex CLI")
        manifest.update(fixture_sha=subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, text=True,
                                                   capture_output=True, check=True).stdout.strip(),
                        codex_path=codex_path, codex_version=codex_version)
        manifest["versions"] = {}
        for label, command in (("python", [sys.executable, "--version"]), ("node", ["node", "--version"]),
                               ("npm", ["npm", "--version"]), ("git", ["git", "--version"]),
                               ("os", ["uname", "-a"])):
            manifest["versions"][label] = subprocess.run(command, text=True, capture_output=True,
                                                          check=True, env=base_env).stdout.strip()
        venv = temp_root / "venv"
        _, record = run([sys.executable, "-m", "venv", str(venv)], env=base_env,
                        log=artifacts / "venv.log")
        manifest["commands"].append(record)
        python = venv / "bin" / "python"
        for command, cwd, name in (([str(python), "-m", "pip", "install", "-e", str(clone)], clone, "pip.log"),
                                   (["npm", "ci"], clone / "ui", "npm-ci.log")):
            _, record = run(command, cwd=cwd, env=base_env, log=artifacts / name)
            manifest["commands"].append(record)
        loop_cli = venv / "bin" / "loop"
        if not loop_cli.is_file() or not os.access(loop_cli, os.X_OK):
            raise RuntimeError("editable install 未提供可執行的 venv/bin/loop")
        validator, validator_hash = prepare_integration_validator(args.scenario, clone, harness)
        validate_cmd = l4_validate_command(python, validator)
        config = home / "dashboard.config.local.json"
        config.write_text(json.dumps({"repo_roots": [str(temp_root)],
                                      "agent_cmds": [{"label": "codex", "cmd": codex_cmd}],
                                      "extra_path_dirs": [str(Path(codex_path).parent)],
                                      "defaults": {
                                          "validate_timeout": L4_VALIDATE_TIMEOUT_SECONDS,
                                      }}, indent=2), encoding="utf-8")
        port_reservation = reserve_loopback_port()
        port = port_reservation.port
        manifest.update(port=port, workspace=str(workspace),
                        thresholds=dict(EXPECTED_THRESHOLDS),
                        planning_timeout_seconds=L4_PLANNING_TIMEOUT_SECONDS,
                        port_reservation_lock=str(port_reservation.lock_path))
        if validator_hash is not None:
            manifest["validator_sha256"] = validator_hash
        base_url = f"http://127.0.0.1:{port}"
        dashboard_env, playwright_env, delete_env = l4_process_environments(
            base_env, workspace=workspace, home=home, config=config, base_url=base_url,
            repo=clone, artifacts=artifacts, scenario=args.scenario, validate_cmd=validate_cmd)
        if args.prepare_only:
            manifest["prepared"] = True
            return 0
        dashboard_log = open(artifacts / "dashboard.log", "w", encoding="utf-8")
        dashboard_logs.append(dashboard_log)
        launch_command = dashboard_command(loop_cli, port)
        manifest.update(dashboard_entrypoint=str(loop_cli), dashboard_command=launch_command)
        dashboard = subprocess.Popen(launch_command, cwd=clone, env=dashboard_env, stdout=dashboard_log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        dashboards.append(dashboard)
        manifest["dashboard_fixture"] = wait_for_dashboard_fixture(
            dashboard, base_url, config)
        ui_deadline = time.monotonic() + PLAYWRIGHT_TOTAL_SECONDS
        _, record = run(["npm", "run", "test:dry-run"], cwd=clone / "ui", env=playwright_env,
                        log=artifacts / "playwright-real.log",
                        timeout=PLAYWRIGHT_TOTAL_SECONDS - 15 * 60)
        manifest["commands"].append(record)
        for command, cwd, name in (([str(python), "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"], clone, "python-final.log"),
                                   (["npm", "run", "check:all"], clone / "ui", "ui-final.log"),
                                   (["git", "fsck", "--full"], clone, "git-fsck.log"),
                                   (["git", "log", "--graph", "--decorate", "--oneline", "--all", "-100"], clone, "git-graph.log"),
                                   (["git", "worktree", "list", "--porcelain"], clone, "git-worktrees.log"),
                                   (["git", "status", "--porcelain"], clone, "git-status.log")):
            remaining = ui_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("單一 production Dashboard 的 UI 兩階段完整驗收已超過整體 4 小時")
            _, record = run(command, cwd=cwd, env=dashboard_env, log=artifacts / name,
                            timeout=remaining)
            manifest["commands"].append(record)
        parent = workspace / f"l4-{args.scenario}"
        snapshot_parallel_evidence(parent, artifacts, manifest)
        require_parallel_truth_snapshots(artifacts, manifest)
        if (parent / "fleet.json").is_file():
            fleet_state = json.loads((parent / "fleet.json").read_text(encoding="utf-8"))
        else:
            raise RuntimeError("parallel run fleet.json 不存在")
        try:
            parent_state = json.loads((parent / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"parent state.json 無法作 L4 evidence：{error}") from error
        scenario_evidence = validate_parallel_run_evidence(
            fleet_state, scenario=args.scenario, repo=clone, workspace_root=workspace,
            parent_state=parent_state, expected_agent_command=codex_cmd,
            expected_validate_command=validate_cmd,
            fixture_sha=manifest["fixture_sha"])
        if args.scenario == "dr1":
            validate_dr1_contract_behavior(clone, python)
        validate_report(parent / "REPORT.md", fleet_state, args.scenario)
        track_evidence = validate_parent_track_evidence(
            parent, artifacts, fleet_state, codex_cmd, validate_cmd, args.scenario)
        manifest["scenario_evidence"] = scenario_evidence
        manifest["track_evidence"] = track_evidence
        if (artifacts / "git-status.log").read_text(encoding="utf-8").strip():
            raise RuntimeError("最終 integration worktree 不乾淨")
        if (artifacts / "git-worktrees.log").read_text(encoding="utf-8").count("worktree ") != 1:
            raise RuntimeError("成功後仍有 child worktree registration")
        remaining = ui_deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("單一 production Dashboard 的 UI 兩階段完整驗收已超過整體 4 小時")
        _, record = run(["npm", "run", "test:dry-run"], cwd=clone / "ui", env=delete_env,
                        log=artifacts / "playwright-real-delete.log",
                        timeout=min(PLAYWRIGHT_DELETE_MAX_SECONDS, remaining))
        manifest["commands"].append(record)
        if parent.exists():
            raise RuntimeError("UI group delete 後 parent workspace 仍存在")
        for child in scenario_evidence["child_workspaces"]:
            if (workspace / child).exists() or (workspace / child).is_symlink():
                raise RuntimeError(f"UI group delete 後 child workspace 仍存在：{child}")
        remaining = ui_deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("單一 production Dashboard 的 UI 兩階段完整驗收已超過整體 4 小時")
        _, worktree_record = run(
            ["git", "worktree", "list", "--porcelain"], cwd=clone,
            log=artifacts / "git-worktrees-after-delete.log", timeout=remaining)
        manifest["commands"].append(worktree_record)
        worktrees_after = (artifacts / "git-worktrees-after-delete.log").read_text(encoding="utf-8")
        if worktrees_after.count("worktree ") != 1:
            raise RuntimeError("UI group delete 後仍有 child worktree registration")
        _, status_record = run(
            ["git", "status", "--porcelain"], cwd=clone,
            log=artifacts / "git-status-after-delete.log",
            timeout=max(1, ui_deadline - time.monotonic()))
        manifest["commands"].append(status_record)
        if (artifacts / "git-status-after-delete.log").read_text(encoding="utf-8").strip():
            raise RuntimeError("UI group delete 後 integration worktree 不乾淨")
        retained_branches = []
        for track in fleet_state.get("tracks") or []:
            branch = str(track.get("branch_ref") or "")
            if not branch or _git_output(clone, "show-ref", "--verify", "--quiet", branch,
                                         check=False).returncode:
                raise RuntimeError(f"UI group delete 不應刪除稽核 branch：{branch or track.get('name')}")
            retained_branches.append(branch)
        manifest["post_delete"] = {
            "remaining_workspace_entries": sorted(path.name for path in workspace.iterdir()),
            "retained_branches": retained_branches,
            "worktree_count": 1,
        }
        if (validator is not None and
                hashlib.sha256(validator.read_bytes()).hexdigest() != validator_hash):
            raise RuntimeError("immutable fault validator 被修改")
        if manifest["release_gate_eligible"]:
            assert_source_checkout_unchanged(source, source_head_at_start)
        require_playwright_artifacts(artifacts, manifest)
        stop_dashboards(dashboards)
        for handle in dashboard_logs:
            handle.flush()
            handle.close()
        dashboard_logs.clear()
        assert_artifacts_contain_no_sensitive_values(artifacts, host_sensitive_values)
        assert_manifest_contains_no_sensitive_values(manifest, host_sensitive_values)
        if time.monotonic() > ui_deadline:
            raise RuntimeError("單一 production Dashboard 的 UI 兩階段完整驗收已超過整體 4 小時")
        manifest["passed"] = bool(manifest["release_gate_eligible"])
        return 0 if manifest["passed"] else 2
    except CommandFailure as error:
        manifest["commands"].append(error.record)
        manifest["error"] = redact_sensitive_text(str(error), host_sensitive_values)
        return 1
    except Exception as error:
        manifest["error"] = redact_sensitive_text(str(error), host_sensitive_values)
        return 1
    finally:
        cleanup_failure = None
        try:
            stop_dashboards(dashboards)
            for handle in dashboard_logs:
                try:
                    handle.flush()
                    handle.close()
                except OSError:
                    pass
            dashboard_logs.clear()
            if python is not None and dashboard_env is not None and clone.is_dir():
                try:
                    cleanup, cleanup_record = run_scoped_coordinator_cleanup(
                        python, clone, dashboard_env, artifacts / "scoped-cleanup.log",
                        f"l4-{args.scenario}")
                    manifest["commands"].append(cleanup_record)
                    manifest["scoped_cleanup"] = cleanup
                except Exception as error:
                    cleanup_failure = error
                    if isinstance(error, CommandFailure):
                        manifest["commands"].append(error.record)
                    manifest["passed"] = False
                    manifest["cleanup_error"] = redact_sensitive_text(
                        str(error), host_sensitive_values)
            parent = workspace / f"l4-{args.scenario}"
            snapshot_parallel_evidence(parent, artifacts, manifest)
            snapshot_git_evidence(clone, artifacts, manifest)
            index_playwright_artifacts(artifacts, manifest)
            try:
                assert_artifacts_contain_no_sensitive_values(artifacts, host_sensitive_values)
            except RuntimeError as security_error:
                manifest["passed"] = False
                manifest["error"] = str(security_error)
            manifest["ended_at"] = time.time()
            artifacts.mkdir(parents=True, exist_ok=True)
            safe_manifest = redact_sensitive_object(manifest, host_sensitive_values)
            (artifacts / "manifest.json").write_text(
                json.dumps(safe_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            print(temp_root)
        finally:
            if port_reservation is not None:
                port_reservation.release()
        if cleanup_failure is not None:
            raise RuntimeError("L4 scoped coordinator cleanup 失敗；詳見 manifest") from cleanup_failure


if __name__ == "__main__":
    raise SystemExit(main())
