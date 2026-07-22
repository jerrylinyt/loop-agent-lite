#!/usr/bin/env python3
"""loop-agent-lite — markdown/JSON 規劃 + 無窮迴圈的極簡 agent 迴圈。

雙層真相:
- 協調層(python-owned truth):goal、初步規劃書、state.json(含 plan)。
  agent 只能透過 work.py 的命令寫入;直接改檔會被偵測、還原、該輪作廢。
- 程式碼層(agent-owned):agent 直接在 repo 寫 code、自己 commit;
  程式不 autocommit、不清工作區,爛尾留給下一輪 agent 判斷。

收斂機制(共識 AND gate):
- 規劃期:agent call plan-ok 且該輪無任何異動 → flag+1;call create-plan(不論成敗)
  或有任何異動 → flag 歸零;未回完成訊號但 repo 無異動時保留 flag;flag > 10 → 執行期。
- 執行期:per-task 內圈——agent call done(task id 正確)且 HEAD 沒動、工作樹乾淨、
  驗證綠、Agent 未逾時 → done+1;有異動/驗證紅 → done 歸零;未回 done 但 repo
  無異動時視為 Agent 異常並保留 done;
  done ≥ threshold(預設 3)→ 派下一個任務。

防線(全部機械、可關可調):
- preflight:validate 必須綠、工作樹必須乾淨、goal/初步規劃書必須已 commit,否則第一行就擋。
- 每輪 coordinator 訊號帶唯一 token;舊 CLI 延遲命令無法污染下一輪。CLI 結束即清同 process-group 子孫。
- 紅燈連跳 N 輪(預設 20)→ git reset --hard 回最後綠點。
- HEAD 停滯 N 輪(預設 300)→ 同上。reset 後依「task 完成 sha」回退任務指標,不用一個一個退。
- 同一任務 reset 次數達上限停機:預設關,開啟時預設 100 次。
"""

import argparse
import atexit
import errno
import hashlib
import json
import math
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from engine import parallel_contract
from engine import parallel_worker
from engine import platform_compat as compat
from engine import repo_owner
from engine.paths import default_workspace_root, expose_project_package

HERE = Path(__file__).resolve().parent
WORKSPACE_ROOT = default_workspace_root()
WORKSPACE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
WORKSPACE_NAME_RULE = "只允許英數、.、_、-，且不可 . / .. 或以 . 開頭"

# ===== 預設值(全部可用命令列覆蓋) =====
AGENT_CMD = ["claude", "-p"]          # prompt 走 stdin;公司 CLI 用 --agent-cmd 覆蓋
VALIDATE_CMD = ["mvn", "-q", "compile"]
FLAG_THRESHOLD = 10                    # 規劃期:flag > 此值 → 收斂
DONE_THRESHOLD = 3                     # 執行期:done ≥ 此值 → 任務完成(建議 3–5)
RED_LIMIT = 20                         # 連續驗證紅 N 輪 → reset
STALL_LIMIT = 300                      # HEAD 連續 N 輪沒前進 → reset
STUCK_STOP_COUNT = 100                 # --stuck-stop 開啟時,同一任務 reset 達此次數停機
ROUND_TIMEOUT_MIN = 30                 # 單輪 agent 上限(分鐘);0=不限
AGENT_BACKOFF_MAX_SEC = 60             # CLI 連續異常退出:1,2,4...秒退避上限;0=關閉
VALIDATE_TIMEOUT_SEC = 120             # 啟動前/每輪驗證上限(秒);避免 validator 永久卡住
VALIDATE_TAIL = 50                     # 驗證失敗餵給下一輪的輸出尾行數
GATE_DRAIN_TIMEOUT_SEC = 5             # gate timeout 後 pipe drain 上限；逃逸子孫不得卡死 worker
GATE_CLIENT_GRACE_SEC = 1              # 外層 watchdog 必須晚於 gate client 自己的 durable cancel deadline
TASK_LIST_TRUNC = 80                   # prompt 任務總覽單行截斷長度
CONSOLE_MAX_BYTES = 5 * 1024 * 1024   # console.log 單檔 5 MiB
CONSOLE_BACKUPS = 3                    # 保留 console.log.1～.3
HISTORY_MAX_BYTES = 10 * 1024 * 1024  # history.log 當前 run 上限；只保留最新完整尾段
ROUND_METRICS_SCAN_BYTES = 2 * 1024 * 1024  # 效能投影只掃尾端，避免讀完整長 history
ROUND_METRICS_MAX_SAMPLES = 500       # API/CLI 單次最多聚合的近期輪數
ANOMALY_LOG_MAX_COUNT = 100           # 每個 workspace 最多保留的異常輪 Agent log
ANOMALY_LOG_MAX_BYTES = 2 * 1024 * 1024  # 單份異常 log 保留尾端上限，最多約 200 MiB/workspace
ANOMALY_ID_RE = re.compile(r"\d{8}T\d{12}-r\d{6}-[0-9a-f]{8}")
ISSUE_MAX_CHARS = 2000                # 單一 issue 文字上限
ISSUES_MAX_PENDING = 100              # 單一 round 最多 ingest 的 issue 行數
ISSUES_MAX_COUNT = 200                # state 保留最新 issue 數量
STOP_AFTER_ROUND_FILE = "stop-after-round.json"
STOP_AFTER_ROUND_CLAIMED_FILE = "stop-after-round.claimed.json"
WORKSPACE_OPS_DIR = ".ops"

_CONSOLE_PATH = None
_CONSOLE_LOCK = threading.Lock()
_ATOMIC_REPLACE_LOCK = threading.Lock()
_RUN_LOCKS = []
_REPO_OWNER_FENCE = None
_REPO_OWNER_STOP_CHECKPOINT = None


def _claim_repo_owner(repo: Path, workspace, owner_kind) -> repo_owner.RepoOwnerFence:
    """Claim the common-Git-dir fence used by ordinary (non-managed) runners."""
    global _REPO_OWNER_FENCE
    if _REPO_OWNER_FENCE is not None:
        raise repo_owner.OwnerBusy("ordinary repository owner is already active")
    fence = repo_owner.RepoOwnerFence.claim(
        repo,
        owner_kind=owner_kind,
        workspace=workspace.dir,
        state_path=workspace.state_path,
    )
    _REPO_OWNER_FENCE = fence
    return fence


def _owner_spawn(child_kind, argv, **popen_kwargs):
    """Spawn below the durable owner when an ordinary runner holds it."""
    if _REPO_OWNER_FENCE is None:
        return None
    return _REPO_OWNER_FENCE.spawn_child(child_kind, argv, **popen_kwargs)


def _owner_record(child):
    """Durably record one result only after all descendants are gone."""
    if not isinstance(child, repo_owner.ControlledOwnerChild):
        return None
    return child.record_result(containment_timeout=5.0)


def _owner_checkpoint_reaped() -> None:
    """Clear the last reaped identity after its caller checkpoint is durable."""
    if _REPO_OWNER_FENCE is None:
        return
    marker = _REPO_OWNER_FENCE.marker
    if marker["child_state"] == "child_reaped":
        _REPO_OWNER_FENCE.checkpoint_child(marker["child_generation"])


def _owner_record_and_checkpoint(child) -> None:
    """Idempotently close a short child whose result is its checkpoint."""
    if not isinstance(child, repo_owner.ControlledOwnerChild):
        return
    child.kill_containment()
    marker = child.fence.marker
    if (marker["child_generation"] != child.child_generation
            or marker["child_state"] not in {
                "child_running", "child_reaped", "idle"}):
        raise repo_owner.OwnerBusy(
            "controlled child no longer matches its durable lifecycle")
    if marker["child_state"] == "child_running":
        marker = _owner_record(child)
    if marker["child_state"] == "child_reaped":
        child.fence.checkpoint_child(child.child_generation)


def _terminalize_repo_owner(reason: str, *, checkpoint=True) -> None:
    """Quiesce children, checkpoint state, and terminalize an owner.

    A failed checkpoint or an incompletely reaped child deliberately leaves a
    nonterminal durable marker.  Releasing the OS lock in that case does not
    grant a later writer authority; explicit fenced recovery remains required.
    """
    global _REPO_OWNER_FENCE, _REPO_OWNER_STOP_CHECKPOINT
    fence = _REPO_OWNER_FENCE
    stop_checkpoint = _REPO_OWNER_STOP_CHECKPOINT
    if fence is None:
        return
    checkpoint_ok = True
    try:
        fence.quiesce_active_child()
        if checkpoint and stop_checkpoint is not None:
            stop_checkpoint()
            try:
                atexit.unregister(stop_checkpoint)
            except Exception:  # pragma: no cover - defensive across runtimes
                pass
        fence.terminalize(reason)
    except BaseException:
        checkpoint_ok = False
        raise
    finally:
        if not checkpoint_ok:
            fence.close()
        _REPO_OWNER_FENCE = None
        _REPO_OWNER_STOP_CHECKPOINT = None


def valid_workspace_name(name) -> bool:
    """workspace 是 ROOT 下單一子目錄；拒絕 dot-leading 保留目錄與路徑逸出。"""
    return isinstance(name, str) and not name.startswith(".") and bool(WORKSPACE_NAME_RE.fullmatch(name))


def require_workspace_name(name: str) -> str:
    """回傳已驗證名稱，讓所有建立 coordinator 檔案的入口 fail-closed。"""
    if not valid_workspace_name(name):
        raise ValueError(f"workspace 名稱不合法：{WORKSPACE_NAME_RULE}")
    return name


def workspace_path(root: Path, name: str) -> Path:
    """取得 root 直屬且非 symlink 的 workspace 路徑，避免合法名稱被連結導出 root。"""
    name = require_workspace_name(name)
    path = Path(root) / name
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return path
    except OSError as e:
        raise ValueError(f"無法檢查 workspace 目錄：{e}") from e
    if stat.S_ISLNK(mode) or compat.is_reparse_point(path.lstat()):
        raise ValueError("workspace 目錄不可為 symbolic link（避免逸出 workspace root）")
    return path


def workspace_directory(path: Path, label: str = "workspace 目錄", *, create=False):
    """驗證實體目錄；create=True 時才建立，讀取投影不會藉機改 workspace。"""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return None
        try:
            path.mkdir(parents=True, exist_ok=True)
            info = path.lstat()
        except OSError as e:
            raise ValueError(f"{label}無法建立:{e}") from e
    except OSError as e:
        raise ValueError(f"{label}無法檢查:{e}") from e
    if stat.S_ISLNK(info.st_mode) or compat.is_reparse_point(info):
        raise ValueError(f"{label}不可為 symbolic link")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label}必須是目錄")
    return path


def ensure_real_directory(path: Path, label: str = "workspace 目錄") -> Path:
    """建立或驗證實體目錄；任何 symlink/非目錄都 fail-closed。"""
    return workspace_directory(path, label, create=True)


def workspace_file(path: Path, label: str = "workspace 檔案") -> Path:
    """驗證 workspace artifact 是 regular file；檔案不存在時保留建立語意。"""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    except OSError as e:
        raise ValueError(f"{label}無法檢查:{e}") from e
    if stat.S_ISLNK(info.st_mode) or compat.is_reparse_point(info):
        raise ValueError(f"{label}不可為 symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label}必須是 regular file")
    return path


def _open_regular(path: Path, flags: int, mode: int = 0o600):
    """以 O_NOFOLLOW 開啟 regular file，避免檔案在 workspace 內被換成連結/FIFO。"""
    path = Path(path)
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except FileNotFoundError as e:
        raise ValueError(f"workspace artifact 父目錄不存在:{parent}") from e
    except OSError as e:
        raise ValueError(f"workspace artifact 父目錄無法檢查:{e}") from e
    if (stat.S_ISLNK(parent_info.st_mode) or compat.is_reparse_point(parent_info)
            or not stat.S_ISDIR(parent_info.st_mode)):
        raise ValueError("workspace artifact 父目錄必須是實體目錄")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None and not compat.IS_WINDOWS:
        raise ValueError("此系統不支援安全的 O_NOFOLLOW 檔案操作")
    if compat.IS_WINDOWS:
        try:
            before = path.lstat()
        except FileNotFoundError:
            before = None
        except OSError as e:
            raise ValueError(f"無法安全檢查 {path.name}:{e}") from e
        if before is not None and (stat.S_ISLNK(before.st_mode)
                                   or compat.is_reparse_point(before)
                                   or not stat.S_ISREG(before.st_mode)):
            raise ValueError(f"{path.name}必須是單一 regular file")
        # O_TRUNC would damage a link target before the post-open identity check.
        # Delay truncation until both the directory entry and opened handle agree.
        delayed_truncate = bool(flags & os.O_TRUNC)
        open_flags = flags & ~os.O_TRUNC
        try:
            for attempt in range(6):
                try:
                    fd = os.open(path, open_flags, mode)
                    break
                except PermissionError:
                    # os.replace、防毒掃描或索引服務可能留下極短的 Windows
                    # sharing-violation 窗口；安全檢查不變，只重試同一路徑。
                    if attempt == 5:
                        raise
                    time.sleep(0.02)
        except OSError as e:
            if e.errno == getattr(errno, "ENOENT", 2):
                raise FileNotFoundError(path) from e
            raise ValueError(f"無法安全開啟 {path.name}:{e}") from e
        try:
            after = path.lstat()
            opened = os.fstat(fd)
            parent_after = parent.lstat()
            if (stat.S_ISLNK(after.st_mode) or compat.is_reparse_point(after)
                    or not stat.S_ISREG(after.st_mode)
                    or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                    or stat.S_ISLNK(parent_after.st_mode) or compat.is_reparse_point(parent_after)
                    or not stat.S_ISDIR(parent_after.st_mode)
                    or (parent_info.st_dev, parent_info.st_ino)
                    != (parent_after.st_dev, parent_after.st_ino)
                    or (before is not None and (before.st_dev, before.st_ino)
                        != (after.st_dev, after.st_ino))):
                raise ValueError(f"{path.name} 在開啟期間被替換或不是安全的 regular file")
            if delayed_truncate:
                os.ftruncate(fd, 0)
            return fd
        except BaseException:
            os.close(fd)
            raise
    try:
        fd = os.open(path, flags | nofollow, mode)
    except OSError as e:
        if e.errno == getattr(errno, "ENOENT", 2):
            raise FileNotFoundError(path) from e
        raise ValueError(f"無法安全開啟 {path.name}:{e}") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"{path.name}必須是單一 regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def append_regular_text(path: Path, text: str) -> None:
    """安全追加 UTF-8 文字；供 history/console 共用。"""
    ensure_real_directory(Path(path).parent, "workspace artifact 父目錄")
    fd = _open_regular(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        with os.fdopen(fd, "a", encoding="utf-8", closefd=True) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # fdopen 接手後例外會由 context manager 關閉；這裡只讓原例外上拋。
        raise


def append_history(path: Path, text: str, *, max_bytes: int = HISTORY_MAX_BYTES) -> None:
    """追加 history 並限制當前 run 大小；裁切時盡量從完整行邊界開始。

    history.log.1 是 reset/import 保留的上一個 run，這裡只裁切當前 history.log，
    不會覆蓋上一輪稽核資料。
    """
    append_regular_text(path, text)
    if max_bytes <= 0:
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    data = read_regular_bytes(path, "history.log")
    if len(data) <= max_bytes:
        return
    tail = data[-max_bytes:]
    first_newline = tail.find(b"\n")
    if first_newline >= 0 and first_newline + 1 < len(tail):
        tail = tail[first_newline + 1:]
    atomic_write_bytes(path, tail)


def read_regular_bytes(path: Path, label: str = "workspace 檔案") -> bytes:
    """以 O_NOFOLLOW 讀取 regular artifact；遺失/不安全時由呼叫端決定處理。"""
    path = workspace_file(path, label)
    fd = _open_regular(path, os.O_RDONLY)
    with os.fdopen(fd, "rb", closefd=True) as stream:
        return stream.read()


def read_regular_text(path: Path, label: str = "workspace 檔案") -> str:
    """安全讀取 UTF-8 regular file；解碼錯誤交由呼叫端決定是否復原或拒絕。"""
    return read_regular_bytes(path, label).decode("utf-8")


def _validate_round_metrics_limit(limit):
    """守門 round metrics 聚合筆數；bool/非整數/超界一律 fail-closed。"""
    if (not isinstance(limit, int) or isinstance(limit, bool) or
            not 1 <= limit <= ROUND_METRICS_MAX_SAMPLES):
        raise ValueError(f"round metrics limit 必須介於 1～{ROUND_METRICS_MAX_SAMPLES}")


def round_metrics_from_history(data: str, limit: int = 50, *, history_truncated=False):
    """從 history 投影近期 Agent round 效能與未回 phase DONE 統計。"""
    _validate_round_metrics_limit(limit)
    samples = []
    for line in reversed(data.splitlines()):
        head = line.split("  << ", 1)[0]
        tokens = head.split()
        if len(tokens) < 2:
            continue
        fields = {}
        for token in tokens[1:]:
            key, separator, value = token.partition("=")
            if separator and key:
                fields[key] = value
        try:
            round_number = int(fields["round"])
            seconds = float(fields["secs"])
        except (KeyError, TypeError, ValueError):
            continue
        if round_number < 0 or not math.isfinite(seconds) or seconds < 0:
            continue
        explicit_missing_done = fields.get("done_missing")
        if explicit_missing_done in {"True", "False"}:
            missing_done = explicit_missing_done == "True"
        else:
            # 舊 history 沒有 done_missing 時，只有同時具備 phase/signal 才回溯判定。
            # Plan 的 create-plan / plan-ok 是 DONE 等價回報；Exec 則必須是 done。
            phase = fields.get("phase")
            signal = fields.get("signal")
            missing_done = ("signal" in fields and
                            ((phase == "plan" and signal not in {"create", "ok"}) or
                             (phase == "exec" and signal != "done")))
        samples.append({
            "round": round_number,
            "seconds": round(seconds, 3),
            "timed_out": fields.get("timeout") == "True",
            "missing_done": missing_done,
            "phase": fields.get("phase") or "",
            "task": "" if fields.get("task") in {None, "-"} else fields["task"],
            "signal": "" if fields.get("signal") in {None, "-"} else fields["signal"],
            "changed": fields.get("changed") == "True",
            "rc": int(fields["rc"]) if fields.get("rc", "").lstrip("-").isdigit() else None,
            "validate": fields.get("validate") or "-",
            "timestamp": tokens[0],
        })
        if len(samples) >= limit:
            break
    samples.reverse()
    durations = sorted(sample["seconds"] for sample in samples)

    def percentile(ratio):
        """以 nearest-rank 計算小樣本 percentile；沒有樣本時回傳 None。"""
        if not durations:
            return None
        index = max(0, math.ceil(len(durations) * ratio) - 1)
        return durations[index]

    timeout_count = sum(1 for sample in samples if sample["timed_out"])
    missing_done_count = sum(1 for sample in samples if sample["missing_done"])
    slowest = max(samples, key=lambda sample: (sample["seconds"], sample["round"])) if samples else None
    return {
        "limit": limit,
        "sample_count": len(samples),
        "average_seconds": round(sum(durations) / len(durations), 3) if durations else None,
        "p50_seconds": percentile(0.50),
        "p95_seconds": percentile(0.95),
        "max_seconds": slowest["seconds"] if slowest else None,
        "slowest_round": slowest["round"] if slowest else None,
        "timeout_count": timeout_count,
        "timeout_rate_pct": round(timeout_count / len(samples) * 100, 1) if samples else 0,
        "missing_done_count": missing_done_count,
        "missing_done_rate_pct": round(missing_done_count / len(samples) * 100, 1) if samples else 0,
        "history_truncated": bool(history_truncated),
        "samples": samples,
    }


def read_round_metrics(path: Path, limit: int = 50):
    """以 O_NOFOLLOW bounded tail read 投影 history metrics；檔案不存在視為無樣本。"""
    _validate_round_metrics_limit(limit)
    path = workspace_file(path, "history.log")
    try:
        fd = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return round_metrics_from_history("", limit)
    with os.fdopen(fd, "rb", closefd=True) as stream:
        size = os.fstat(stream.fileno()).st_size
        start = max(0, size - ROUND_METRICS_SCAN_BYTES)
        stream.seek(start)
        data = stream.read(ROUND_METRICS_SCAN_BYTES)
    if start:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    return round_metrics_from_history(
        data.decode("utf-8", errors="replace"), limit, history_truncated=start > 0)


def preserve_anomaly_log(workspace_dir: Path, round_log: Path, *, round_number: int,
                         phase: str, task: str, timestamp: str):
    """保留異常輪 Agent log 尾段與索引；每 workspace 嚴格上限 100 份。"""
    anomaly_dir = ensure_real_directory(
        Path(workspace_dir) / "logs" / "anomalies", "異常 log 目錄")
    fd = _open_regular(round_log, os.O_RDONLY)
    with os.fdopen(fd, "rb", closefd=True) as stream:
        original_size = os.fstat(stream.fileno()).st_size
        start = max(0, original_size - ANOMALY_LOG_MAX_BYTES)
        stream.seek(start)
        data = stream.read(ANOMALY_LOG_MAX_BYTES)

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    anomaly_id = f"{stamp}-r{round_number:06d}-{uuid.uuid4().hex[:8]}"
    log_name = f"{anomaly_id}.log"
    atomic_write_bytes(anomaly_dir / log_name, data)
    metadata = {
        "schema_version": 1,
        "id": anomaly_id,
        "round": round_number,
        "phase": phase,
        "task": task,
        "timestamp": timestamp,
        "log_file": log_name,
        "original_size": original_size,
        "retained_size": len(data),
        "truncated": start > 0,
    }
    atomic_write_bytes(
        anomaly_dir / f"{anomaly_id}.json",
        json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )

    metadata_files = []
    for path in anomaly_dir.glob("*.json"):
        try:
            if ANOMALY_ID_RE.fullmatch(path.stem) and stat.S_ISREG(path.lstat().st_mode):
                metadata_files.append(path)
        except OSError:
            continue
    ordered_metadata = sorted(metadata_files)
    for old_metadata in ordered_metadata[:-ANOMALY_LOG_MAX_COUNT]:
        old_log = anomaly_dir / f"{old_metadata.stem}.log"
        old_metadata.unlink(missing_ok=True)
        try:
            if stat.S_ISREG(old_log.lstat().st_mode):
                old_log.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    kept_ids = {path.stem for path in ordered_metadata[-ANOMALY_LOG_MAX_COUNT:]}
    for orphan_log in anomaly_dir.glob("*.log"):
        if not ANOMALY_ID_RE.fullmatch(orphan_log.stem) or orphan_log.stem in kept_ids:
            continue
        try:
            if stat.S_ISREG(orphan_log.lstat().st_mode):
                orphan_log.unlink(missing_ok=True)
        except OSError:
            pass
    return metadata


def repo_relative_path(repo: Path, rel: str) -> Path:
    """解析受保護檔案；只接受 repo 內的相對 regular file，拒絕 traversal 與 symlink。"""
    if not isinstance(rel, str) or not rel:
        raise ValueError("受保護檔案路徑不可為空")
    candidate = Path(rel)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"受保護檔案路徑 {rel!r} 必須是 repo 內的相對路徑")
    root = Path(repo).resolve()
    current = root
    try:
        for part in candidate.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(info.st_mode) or compat.is_reparse_point(info):
                raise ValueError(f"受保護檔案路徑 {rel!r} 不可經由 symbolic link")
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as e:
            raise ValueError(f"受保護檔案路徑 {rel!r} 不得逸出 repo") from e
    except OSError as e:
        raise ValueError(f"受保護檔案路徑 {rel!r} 無法檢查:{e}") from e
    try:
        final_info = (root / candidate).lstat()
    except FileNotFoundError:
        return root / candidate
    except OSError as e:
        raise ValueError(f"受保護檔案路徑 {rel!r} 無法讀取:{e}") from e
    if not stat.S_ISREG(final_info.st_mode):
        raise ValueError(f"受保護檔案路徑 {rel!r} 必須是 regular file")
    return root / candidate


class WorkspaceOperationLockError(RuntimeError):
    """另一個 Dashboard 操作或 CLI 正在改同名 workspace 的 root entry。"""


@contextmanager
def workspace_operation_lock(root: Path, name: str, *, blocking=True):
    """跨 Dashboard/CLI 的 per-name root lock，保護建立／刪除 root entry 的競態。"""
    name = require_workspace_name(name)
    root = Path(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise WorkspaceOperationLockError(f"無法建立 workspace 根目錄:{e}") from e
    if compat.IS_WINDOWS:
        lock_file = None
        try:
            ensure_real_directory(root, "workspace 根目錄")
            ops = ensure_real_directory(root / WORKSPACE_OPS_DIR, "workspace operation lock 目錄")
            lock_fd = _open_regular(ops / f"{name}.lock", os.O_RDWR | os.O_CREAT)
            lock_file = os.fdopen(lock_fd, "a+b")
            try:
                compat.lock_file(lock_file, blocking=blocking)
            except (BlockingIOError, PermissionError) as e:
                raise WorkspaceOperationLockError(
                    f"workspace {name} 正在進行建立或刪除操作") from e
            yield
        finally:
            if lock_file is not None:
                try:
                    compat.unlock_file(lock_file)
                except OSError:
                    pass
                lock_file.close()
        return

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise WorkspaceOperationLockError("此系統不支援安全的 workspace operation lock")
    directory_flags = os.O_RDONLY | nofollow | getattr(os, "O_DIRECTORY", 0)
    root_fd = ops_fd = None
    lock_file = None
    try:
        try:
            root_fd = os.open(root, directory_flags)
            try:
                os.mkdir(WORKSPACE_OPS_DIR, dir_fd=root_fd)
            except FileExistsError:
                pass
            ops_fd = os.open(WORKSPACE_OPS_DIR, directory_flags, dir_fd=root_fd)
        except OSError as e:
            raise WorkspaceOperationLockError(f"無法開啟 workspace operation lock 目錄:{e}") from e
        if not stat.S_ISDIR(os.fstat(ops_fd).st_mode):
            raise WorkspaceOperationLockError("workspace operation lock 目錄不是實體目錄")
        try:
            lock_fd = os.open(f"{name}.lock", os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | nofollow,
                              0o600, dir_fd=ops_fd)
        except OSError as e:
            raise WorkspaceOperationLockError(f"無法取得 workspace operation lock:{e}") from e
        lock_file = os.fdopen(lock_fd, "a+b")
        info = os.fstat(lock_file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceOperationLockError("workspace operation lock 不是安全的 regular file")
        try:
            compat.lock_file(lock_file, blocking=blocking)
        except BlockingIOError as e:
            raise WorkspaceOperationLockError(f"workspace {name} 正在進行建立或刪除操作") from e
        yield
    finally:
        if lock_file is not None:
            try:
                compat.unlock_file(lock_file)
            except OSError:
                pass
            lock_file.close()
        if ops_fd is not None:
            os.close(ops_fd)
        if root_fd is not None:
            os.close(root_fd)


def now_ts() -> str:
    """產生 console 使用的本機時分秒；具日期的稽核時間另由 state/history 保存。"""
    return datetime.now().strftime("%H:%M:%S")


def configure_console(path: Path) -> None:
    """將 loop 與 agent 的所有輸出追加到 workspace 共用 console。"""
    global _CONSOLE_PATH
    _CONSOLE_PATH = path
    ensure_real_directory(path.parent, "console 父目錄")
    append_console(path, f"\n[{now_ts()}] ━━━ 新的 loop session ━━━")


def append_console(path: Path, line: str, *, max_bytes: int = CONSOLE_MAX_BYTES,
                   backups: int = CONSOLE_BACKUPS) -> None:
    """跨 process 鎖定後追加 console；超過大小時輪替 .1～.N。"""
    ensure_real_directory(path.parent, "console 父目錄")
    encoded = (line + "\n").encode("utf-8")
    lock_path = path.with_name(f".{path.name}.lock")
    lock_fd = _open_regular(lock_path, os.O_RDWR | os.O_CREAT)
    with _CONSOLE_LOCK, os.fdopen(lock_fd, "a+b", closefd=True) as lock_file:
        compat.lock_file(lock_file)
        try:
            try:
                current_size = workspace_file(path, "console.log").lstat().st_size
            except FileNotFoundError:
                current_size = 0
            if max_bytes > 0 and current_size > 0 and current_size + len(encoded) > max_bytes:
                if backups > 0:
                    oldest = path.with_name(f"{path.name}.{backups}")
                    oldest.unlink(missing_ok=True)
                    for index in range(backups - 1, 0, -1):
                        source = path.with_name(f"{path.name}.{index}")
                        if source.exists():
                            os.replace(source, path.with_name(f"{path.name}.{index + 1}"))
                    os.replace(path, path.with_name(f"{path.name}.1"))
                else:
                    path.unlink(missing_ok=True)
            console_fd = _open_regular(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            with os.fdopen(console_fd, "ab", closefd=True) as console:
                console.write(encoded)
                console.flush()
                os.fsync(console.fileno())
        finally:
            compat.unlock_file(lock_file)


def _console_line(line: str) -> None:
    """同步輸出 stdout 與 workspace console；尚未設定 console 時只寫 stdout。"""
    print(line, flush=True)
    if _CONSOLE_PATH is None:
        return
    append_console(_CONSOLE_PATH, line)


def log(msg: str) -> None:
    """將一般 coordinator 訊息逐行加上本機時間後輸出。"""
    lines = str(msg).splitlines() or [""]
    for line in lines:
        _console_line(f"[{now_ts()}] {line}")


def agent_log(msg: str) -> None:
    """標記 Agent 來源後寫入共用 console，供前端依來源過濾。"""
    _console_line(f"[{now_ts()}] 🤖 Agent｜{msg}")


def fail(msg: str):
    """記錄單一失敗原因後以 exit 1 結束，避免 stderr 重複列印。"""
    log(f"⛔ 流程停止｜{msg}")
    # 原因已同步寫到 stdout 與 console.log；只回 exit code，避免 stderr 再印一次相同訊息。
    raise SystemExit(1)


def safe_kill(pid, sig):
    """送 signal 給單一 pid 前的最後防線。kernel 對 kill 的特殊語意:pid=0 殺自己
    整個 process group、pid=-1 殺掉所有殺得動的程序——上游 pid 來源(state 檔、
    型別轉換)一旦被污染,走到這裡就是全機屠殺。攔到只記 log 不送,回傳是否已送出。"""
    target = pid
    pid = int(getattr(pid, "pid", pid))
    if pid <= 1:
        log(f"⛔ 攔截 os.kill({pid}, {sig!r}):pid 為 -1/0/1 會波及整組甚至全機程序,拒絕送出")
        return False
    if compat.IS_WINDOWS and ((hasattr(target, "poll") and target.poll() is not None)
                              or not compat.process_is_alive(pid)):
        raise ProcessLookupError(pid)
    if compat.IS_WINDOWS and sig == signal.SIGINT:
        compat.interrupt_process_group(target)
    elif compat.IS_WINDOWS and hasattr(target, "kill"):
        target.kill()
    else:
        os.kill(pid, sig)
    return True


def safe_killpg(pgid, sig):
    """送 signal 給整個 process group 前的最後防線。pgid<=1 等同 kill(0)/kill(-1);
    pgid 等於自己所在 group 代表 start_new_session 沒生效或 pgid 來源被污染,
    這一刀會把 coordinator 連同啟動它的 shell/同 session 程序一起帶走。
    攔到只記 log 不送,回傳是否已送出。"""
    target = pgid
    pgid = int(getattr(pgid, "pid", pgid))
    if pgid <= 1:
        log(f"⛔ 攔截 os.killpg({pgid}, {sig!r}):pgid<=1 等同殺自己整組/全機程序,拒絕送出")
        return False
    if ((compat.IS_WINDOWS and pgid == os.getpid())
            or (not compat.IS_WINDOWS and pgid == os.getpgid(0))):
        log(f"⛔ 攔截 os.killpg({pgid}, {sig!r}):目標是 coordinator 自己的 process group,拒絕送出")
        return False
    compat.signal_process_group(target, sig)
    return True


def release_run_locks() -> None:
    """正常退出時最後釋放；SIGKILL 時 kernel 也會自動釋放 flock。"""
    while _RUN_LOCKS:
        lock_file = _RUN_LOCKS.pop()
        try:
            compat.unlock_file(lock_file)
        finally:
            lock_file.close()


def acquire_run_lock(path: Path, label: str) -> None:
    """取得跨 Dashboard/terminal process 的單 writer 鎖；不等待、不猜 pid。"""
    try:
        ensure_real_directory(path.parent, f"{label} 父目錄")
        lock_fd = _open_regular(path, os.O_RDWR | os.O_CREAT)
    except (OSError, ValueError) as e:
        fail(f"preflight：{label} 鎖檔不安全或無法建立:{e}")
    lock_file = os.fdopen(lock_fd, "a+b", closefd=True)
    try:
        compat.lock_file(lock_file, blocking=False)
    except (BlockingIOError, PermissionError):
        lock_file.seek(0)
        owner = lock_file.read().decode("utf-8", errors="replace").strip()
        lock_file.close()
        owner_note = f"（owner {owner}）" if owner else ""
        fail(f"preflight：{label} 已有另一個 loop 持有單 writer 鎖{owner_note}。"
             "不要同時操作同一份 state/worktree；要並行請使用不同 Git worktree 與 workspace")
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(json.dumps({"pid": os.getpid(), "started_at": datetime.now().isoformat(timespec="seconds")},
                               ensure_ascii=False).encode("utf-8"))
    lock_file.flush()
    os.fsync(lock_file.fileno())
    _RUN_LOCKS.append(lock_file)  # 強引用持有到 atexit；只留下 lock file，不靠檔案存在與否判斷


def active_run_lock_owner(path: Path):
    """若 run lock 正由另一 process 持有，安全讀回 owner；未鎖定則回 None。"""
    path = workspace_file(Path(path), "run lock")
    try:
        fd = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb", closefd=True) as lock_file:
        try:
            compat.lock_file(lock_file, blocking=False)
        except (BlockingIOError, PermissionError):
            # The owner obtains the OS lock immediately before replacing this
            # diagnostic JSON.  A reader can therefore observe the tiny
            # truncate/write window while the lock is unquestionably held.
            # Never collapse that state to "no owner": retry briefly, then
            # fail closed so a second supervisor cannot publish authority.
            for attempt in range(20):
                lock_file.seek(0)
                raw = lock_file.read()
                try:
                    owner = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    owner = None
                pid = owner.get("pid") if isinstance(owner, dict) else None
                if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
                    return owner
                if attempt != 19:
                    time.sleep(0.005)
            raise ValueError(
                "run lock 正由其他 process 持有，但 owner metadata 尚未形成或已損壞")
        else:
            compat.unlock_file(lock_file)
            return None


# 此 handler 比 main 內稍後註冊的 state stopped handler 更早註冊；atexit 為 LIFO，
# 因此會先把 state.pid 清掉並存檔，最後才釋放單 writer 鎖。
atexit.register(release_run_locks)


def sh(args, cwd, check=True, *, owner_child_kind=None, env=None):
    """執行不經 shell 的子程序並擷取輸出；check=True 時把非零狀態轉成例外。"""
    controlled = (_owner_spawn(
        owner_child_kind, list(args), cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    ) if owner_child_kind is not None else None)
    if controlled is None:
        r = subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, env=env)
    else:
        try:
            stdout, stderr = controlled.communicate()
        finally:
            try:
                _owner_record_and_checkpoint(controlled)
            except KeyboardInterrupt:
                # The first interrupt may land inside the durable cleanup
                # itself.  Retrying is safe because the helper is idempotent.
                _owner_record_and_checkpoint(controlled)
                raise
        r = subprocess.CompletedProcess(
            list(args), controlled.returncode, stdout, stderr)
    if check and r.returncode != 0:
        raise RuntimeError(f"命令失敗 rc={r.returncode}: {args}\n{r.stdout}\n{r.stderr}")
    return r


def git(repo, *args, check=True):
    """在指定 repo 執行 Git 子命令，統一沿用 sh 的錯誤語意。"""
    command = args[0] if args else ""
    read_only = {
        "rev-parse", "status", "merge-base", "cat-file", "diff", "show",
        "log", "rev-list", "ls-files", "show-ref",
    }
    symbolic_read = (
        command == "symbolic-ref" and len(args) == 3 and args[1] == "-q")
    is_read = command in read_only or symbolic_read
    argv = ["git", *args]
    env = None
    if is_read:
        # These are internal repository observations, not user-facing Git
        # sessions.  Prevent an observation from refreshing the index through
        # optional locks/fsmonitor or escaping owner containment through a
        # configured pager/external diff helper.  Command-specific options
        # stay after the subcommand so normal Git argument parsing is intact.
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_PAGER"] = "cat"
        env["PAGER"] = "cat"
        env.pop("GIT_EXTERNAL_DIFF", None)
        argv = [
            "git", "--no-pager", "-c", "core.fsmonitor=false",
            command, *args[1:],
        ]
        if command in {"diff", "show", "log"}:
            argv.insert(5, "--no-ext-diff")
            argv.insert(6, "--no-textconv")
    return sh(
        argv, cwd=repo, check=check,
        owner_child_kind=repo_owner.ChildKind.GIT, env=env)


def head_sha(repo) -> str:
    """讀取目前 HEAD 完整 SHA。"""
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def managed_task_ref_error(repo, expected_ref: str) -> str | None:
    """Return why a worker is not actually checked out on its immutable task ref."""
    symbolic = git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    actual_ref = symbolic.stdout.strip() if symbolic.returncode == 0 else ""
    if actual_ref != expected_ref:
        shown = actual_ref or "detached/unborn HEAD"
        return f"worker 必須 checkout {expected_ref}，目前為 {shown}"
    tip = git(repo, "rev-parse", "--verify", expected_ref, check=False)
    if tip.returncode != 0 or tip.stdout.strip() != head_sha(repo):
        return f"worker task ref {expected_ref} tip 必須與 HEAD 完全一致"
    return None


@dataclass(frozen=True)
class RepositorySnapshot:
    """Validator 前後可比較的 exact Git snapshot。"""

    head: str
    head_ref: str | None
    status: str

    @property
    def dirty(self) -> bool:
        return bool(self.status.strip())


def repository_snapshot(repo) -> RepositorySnapshot:
    """讀取 HEAD/ref、index/worktree 與所有 untracked paths 的一致快照。"""
    # A controlled child has non-trivial guardian/Job startup cost.  Porcelain
    # v2 exposes the exact HEAD oid and symbolic branch together with the full
    # worktree status, so one fenced Git read can replace three independently
    # fenced observations without a consistency window between them.
    result = git(
        repo, "status", "--porcelain=v2", "--branch",
        "--no-ahead-behind", "--untracked-files=all")
    headers = {}
    status_lines = []
    for line in result.stdout.splitlines():
        if line.startswith("# "):
            key, separator, value = line[2:].partition(" ")
            if separator:
                headers[key] = value
        else:
            status_lines.append(line)
    head = headers.get("branch.oid", "")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head):
        raise RuntimeError("git status did not report an exact HEAD oid")
    branch = headers.get("branch.head", "")
    head_ref = (None if branch == "(detached)"
                else f"refs/heads/{branch}" if branch else None)
    return RepositorySnapshot(
        head=head,
        head_ref=head_ref,
        status=("\n".join(status_lines) + "\n" if status_lines else ""),
    )


def is_dirty(repo) -> bool:
    """只要 porcelain status 有任何輸出就視為髒工作樹。"""
    return bool(git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip())


def is_ancestor(repo, sha, of_sha) -> bool:
    """sha 是否為 of_sha 的祖先(含相等)。"""
    return git(repo, "merge-base", "--is-ancestor", sha, of_sha, check=False).returncode == 0


def ensure_current_task_base_sha(state, repo, current_head=None) -> str | None:
    """確保執行中 task 有不可變的 Git 起點。

    新 state 會在 task 啟動時直接記下 HEAD；舊 state 則優先沿用前一個完成 task 的 SHA。
    舊版第一個 task 無法可靠回推起點，因此不補寫。候選必須真的是目前 HEAD 的祖先，
    避免損壞或跨 branch 的 state 讓之後的 task diff 形成無意義範圍。
    """
    if state.get("phase") != "exec" or not state.get("current_order"):
        return None
    current_head = current_head or head_sha(repo)
    legacy_without_base_field = "current_task_base_sha" not in state
    candidates = [state.get("current_task_base_sha")]
    previous = sorted(
        (entry for entry in state.get("completed", [])
         if entry.get("order", 0) < state["current_order"]),
        key=lambda entry: entry["order"], reverse=True,
    )
    candidates.extend(entry.get("sha") for entry in previous)
    for candidate in candidates:
        if (isinstance(candidate, str) and
                re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", candidate) and
                is_ancestor(repo, candidate, current_head)):
            state["current_task_base_sha"] = candidate
            return candidate
    # 舊版第一個 task 若已進行一段時間，現在的 HEAD 不是可靠起點；保持缺欄位，讓完成
    # 紀錄依契約退回單一 commit。新版 fresh_state 一開始就帶 null，因此會走下方真正記錄。
    if legacy_without_base_field:
        return None
    state["current_task_base_sha"] = current_head
    return current_head


def green_anchor_valid(repo, green, snap_dir, rel_paths) -> bool:
    """resume 時綠點錨定的 fail-closed 驗證(#1):綠點必須同時滿足
    (1) 是 repo 裡真實存在的 commit;(2) 是目前 HEAD 的祖先;
    (3) 該 commit 的每個受保護檔 blob 與本次啟動快照逐位元組相同。
    任一不成立就不能沿用——reset 回這種綠點會製造髒工作樹或還原出錯版本的 goal/plan-doc。"""
    if not green:
        return False
    if git(repo, "rev-parse", "--verify", "--quiet", f"{green}^{{commit}}", check=False).returncode != 0:
        return False
    if not is_ancestor(repo, green, head_sha(repo)):
        return False
    for rel in rel_paths:
        snap = snap_dir / rel.replace("/", "__")
        if not snap.exists():
            return False
        committed = git(repo, "rev-parse", f"{green}:{rel}", check=False)
        hash_argv = ["git", "hash-object", "--stdin", "--path", rel]
        controlled = _owner_spawn(
            repo_owner.ChildKind.GIT, hash_argv, cwd=str(repo),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if controlled is None:
            filtered = subprocess.run(
                hash_argv, cwd=str(repo), input=snap.read_bytes(),
                capture_output=True)
        else:
            try:
                stdout, stderr = controlled.communicate(input=snap.read_bytes())
            except KeyboardInterrupt:
                controlled.kill_containment()
                _owner_record_and_checkpoint(controlled)
                raise
            _owner_record_and_checkpoint(controlled)
            filtered = subprocess.CompletedProcess(
                hash_argv, controlled.returncode, stdout, stderr)
        if (committed.returncode != 0 or filtered.returncode != 0
                or committed.stdout.strip() != filtered.stdout.decode("ascii", errors="replace").strip()):
            return False
    return True


def tracked_in_head(repo, rel_path) -> bool:
    """判斷相對路徑是否存在於目前 HEAD，而不是只看工作樹是否有檔案。"""
    return git(repo, "cat-file", "-e", f"HEAD:{rel_path}", check=False).returncode == 0


def sha256_bytes(data: bytes) -> str:
    """產生 state/checkpoint 與 goal 內容比對使用的穩定雜湊。"""
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子寫:同目錄 tmp → fsync → os.replace。避免 SIGKILL/磁碟滿留下半截檔。
    唯一真相(state.json)不能寫到一半——這是跑整夜必備的 correctness 防線。"""
    path = Path(path)
    ensure_real_directory(path.parent, "原子寫入父目錄")
    workspace_file(path, "原子寫入目標")
    # tmp 名帶 uuid:同 process 多執行緒(dashboard ThreadingHTTPServer)並發寫同一 state.json
    # 時不再共用 tmp,避免互相 truncate 或 replace 後對方拿到 FileNotFoundError(#3)。
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # ReplaceFile/MoveFileEx may transiently report sharing violations while
        # another Windows thread is opening the destination. Serialise local
        # writers and retry those short-lived violations; POSIX remains one call.
        with _ATOMIC_REPLACE_LOCK:
            attempts = 100 if compat.IS_WINDOWS else 1
            for attempt in range(attempts):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt + 1 == attempts:
                        raise
                    time.sleep(0.005)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _stop_after_round_marker_request(
    path: Path, pid, session_id,
) -> dict | None:
    """讀取同時屬於目前 pid/session 的停止 marker。"""
    try:
        path = workspace_file(path, "stop marker")
        fd = _open_regular(path, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
            request = json.load(stream)
        if (int(request.get("pid")) == int(pid) and bool(session_id)
                and request.get("session_id") == session_id):
            return request
    except (AttributeError, FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return None


def _stop_after_round_marker_matches(path: Path, pid, session_id) -> bool:
    """驗證停止 marker 同時屬於目前 pid 與 session，拒絕舊 session 殘留請求。"""
    return _stop_after_round_marker_request(path, pid, session_id) is not None


def stop_after_round_requested(workspace_dir: Path, pid, session_id, *, consume=False) -> bool:
    """檢查本 session 的「本輪後停止」控制檔；consume 時連壞檔/舊 session 一併清掉。"""
    path = Path(workspace_dir) / STOP_AFTER_ROUND_FILE
    read_path = path
    claimed = None
    if consume:
        # 先原子 claim 再讀：若 Dashboard 恰在讀取後寫入新請求，不會被這次 unlink 誤刪。
        claimed = path.with_name(f".{path.name}.consume.{os.getpid()}.{uuid.uuid4().hex}")
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            return False
        except OSError:
            return False
        read_path = claimed
    try:
        matches = _stop_after_round_marker_matches(read_path, pid, session_id)
    finally:
        if claimed is not None:
            try:
                claimed.unlink(missing_ok=True)
            except OSError:
                pass
    return matches


def stop_after_round_claimed(workspace_dir: Path, pid, session_id) -> bool:
    """loop 已原子接手本輪後停止請求的可觀測標記；接手後不再允許撤銷。"""
    return _stop_after_round_marker_matches(
        Path(workspace_dir) / STOP_AFTER_ROUND_CLAIMED_FILE, pid, session_id)


def stop_after_round_claimed_request(
    workspace_dir: Path, pid, session_id,
) -> dict | None:
    """Return the exact claimed stop marker for managed Pause projection."""
    return _stop_after_round_marker_request(
        Path(workspace_dir) / STOP_AFTER_ROUND_CLAIMED_FILE,
        pid, session_id)


def claim_stop_after_round(workspace_dir: Path, pid, session_id) -> bool:
    """把 pending 請求原子搬成 claimed marker；成功後 marker 保留到本 session 退出。"""
    workspace_dir = Path(workspace_dir)
    pending = workspace_dir / STOP_AFTER_ROUND_FILE
    claimed = workspace_dir / STOP_AFTER_ROUND_CLAIMED_FILE
    try:
        os.replace(pending, claimed)
    except (FileNotFoundError, OSError):
        return False
    if stop_after_round_claimed(workspace_dir, pid, session_id):
        return True
    try:
        claimed.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def clear_stop_after_round_claimed(workspace_dir: Path, pid, session_id) -> None:
    """只刪除目前 session 的 claimed marker，避免誤碰別人剛寫入的控制檔。"""
    path = Path(workspace_dir) / STOP_AFTER_ROUND_CLAIMED_FILE
    if stop_after_round_claimed(workspace_dir, pid, session_id):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class StateLoadError(RuntimeError):
    """主 state 與 recovery checkpoint 都存在但無法安全解碼。"""


def state_checkpoint_path(state_path: Path) -> Path:
    """由 primary state 路徑取得固定的 last-good checkpoint 路徑。"""
    return state_path.with_name("state.last-good.json")


def validate_state_shape(state, label: str):
    """檢查 state 的核心欄位型別；允許舊版省略欄位，但不接受錯型真相。"""
    if "phase" in state and state["phase"] not in ("plan", "exec", "done"):
        raise StateLoadError(f"{label} phase 不合法:{state['phase']!r}")
    integer_fields = ("round", "flag", "plan_version", "done_count", "red_streak",
                      "stall_rounds", "agent_failure_streak", "state_recovery_count")
    for field in integer_fields:
        value = state.get(field)
        if field in state and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise StateLoadError(f"{label} {field} 必須是非負整數")
    if "issues_acknowledged_round" in state:
        value = state["issues_acknowledged_round"]
        if (not isinstance(value, int) or isinstance(value, bool) or value < -1):
            raise StateLoadError(f"{label} issues_acknowledged_round 必須是 ≥ -1 的整數")
    if "current_order" in state and state["current_order"] is not None:
        value = state["current_order"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise StateLoadError(f"{label} current_order 必須是非負整數或 null")
    if "current_task_base_sha" in state and state["current_task_base_sha"] is not None:
        value = state["current_task_base_sha"]
        if (not isinstance(value, str) or
                re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None):
            raise StateLoadError(f"{label} current_task_base_sha 必須是完整 SHA 或 null")
    for field in ("plan", "completed", "notes", "issues"):
        if field in state and not isinstance(state[field], list):
            raise StateLoadError(f"{label} {field} 必須是陣列")
    if "plan" in state:
        plan_orders = []
        for index, task in enumerate(state["plan"]):
            if (not isinstance(task, dict) or
                    not isinstance(task.get("order"), int) or isinstance(task.get("order"), bool) or
                    task["order"] < 1 or
                    not isinstance(task.get("task"), str) or not task["task"].strip() or
                    ("ref" in task and task["ref"] is not None and
                     not isinstance(task["ref"], str))):
                raise StateLoadError(f"{label} plan[{index}] 必須含有合法 order/task")
            plan_orders.append(task["order"])
        if plan_orders and plan_orders != list(range(1, len(plan_orders) + 1)):
            raise StateLoadError(f"{label} plan.order 必須從 1 依序連續遞增")
        if state["plan"]:
            # Import/state/manifest loaders share the complete stack invariant.
            # The local import avoids an import cycle because engine.work uses
            # this module's guarded workspace artifact helpers.
            from engine.work import validate_plan
            _normalized_plan, plan_errors = validate_plan(state["plan"])
            if plan_errors:
                raise StateLoadError(
                    f"{label} plan 不合法:" + "；".join(plan_errors))
    if state.get("runner") == parallel_worker.WORKER_RUNNER:
        try:
            parallel_worker.validate_persisted_state(state)
        except parallel_contract.ParallelContractError as exc:
            raise StateLoadError(f"{label} managed worker state 不合法:{exc}") from exc
    if "completed" in state:
        completed_orders = []
        for index, entry in enumerate(state["completed"]):
            if (not isinstance(entry, dict) or
                    not isinstance(entry.get("order"), int) or isinstance(entry.get("order"), bool) or
                    entry["order"] < 1 or
                    not isinstance(entry.get("sha"), str) or
                    re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", entry["sha"]) is None or
                    ("base_sha" in entry and
                     (not isinstance(entry["base_sha"], str) or
                      re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", entry["base_sha"]) is None)) or
                    not isinstance(entry.get("round"), int) or isinstance(entry.get("round"), bool) or
                    entry["round"] < 0 or
                    ("human" in entry and not isinstance(entry["human"], bool))):
                raise StateLoadError(
                    f"{label} completed[{index}] 必須含有合法 order/sha/round")
            completed_orders.append(entry["order"])
        if len(completed_orders) != len(set(completed_orders)):
            raise StateLoadError(f"{label} completed.order 不可重複")
    if "notes" in state and any(not isinstance(note, str) for note in state["notes"]):
        raise StateLoadError(f"{label} notes 每一項都必須是字串")
    if "issues" in state:
        for index, issue in enumerate(state["issues"]):
            if (not isinstance(issue, dict) or
                    not isinstance(issue.get("round"), int) or
                    isinstance(issue.get("round"), bool) or issue["round"] < 0 or
                    not isinstance(issue.get("text"), str) or not issue["text"].strip() or
                    ("where" in issue and not isinstance(issue["where"], str)) or
                    ("ts" in issue and not isinstance(issue["ts"], str))):
                raise StateLoadError(
                    f"{label} issues[{index}] 必須含有合法 round/text/where/ts")
    for field in ("task_reset_counts", "config", "loop"):
        if field in state and not isinstance(state[field], dict):
            raise StateLoadError(f"{label} {field} 必須是 object")
    if "task_reset_counts" in state:
        for key, count in state["task_reset_counts"].items():
            if (not isinstance(key, str) or not key.isdigit() or int(key) < 1 or
                    not isinstance(count, int) or isinstance(count, bool) or count < 0):
                raise StateLoadError(
                    f"{label} task_reset_counts 必須是正整數字串到非負整數的對應")
    if "loop" in state:
        loop_state = state["loop"]
        if "pid" in loop_state:
            pid = loop_state["pid"]
            if (pid is not None and
                    (not isinstance(pid, int) or isinstance(pid, bool) or pid < 1)):
                raise StateLoadError(f"{label} loop.pid 必須是正整數或 null")
        for field in ("session_id", "started_at"):
            if field in loop_state and not isinstance(loop_state[field], str):
                raise StateLoadError(f"{label} loop.{field} 必須是字串")
    if "agent_backoff_seconds" in state:
        delay = state["agent_backoff_seconds"]
        if (isinstance(delay, bool) or not isinstance(delay, (int, float)) or
                not math.isfinite(delay) or delay < 0):
            raise StateLoadError(f"{label} agent_backoff_seconds 必須是有限非負數")
    if "last_round_seconds" in state:
        duration = state["last_round_seconds"]
        if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or
                not math.isfinite(duration) or duration < 0):
            raise StateLoadError(f"{label} last_round_seconds 必須是有限非負數")
    if "last_round_timed_out" in state and not isinstance(state["last_round_timed_out"], bool):
        raise StateLoadError(f"{label} last_round_timed_out 必須是 boolean")
    for field in ("agent_backoff_until", "last_state_recovery", "round_started_at",
                  "round_deadline_at", "round_interrupted_at"):
        if field in state and state[field] is not None and not isinstance(state[field], str):
            raise StateLoadError(f"{label} {field} 必須是字串或 null")
    if "goal_changed" in state and not isinstance(state["goal_changed"], bool):
        raise StateLoadError(f"{label} goal_changed 必須是 boolean")
    for field in ("goal_hash", "goal_previous_hash"):
        value = state.get(field)
        if field in state and value is not None and (
                not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None):
            raise StateLoadError(f"{label} {field} 必須是 64 字元小寫 SHA-256 或 null")
    if "repo_binding" in state and state["repo_binding"] is not None and (
            not isinstance(state["repo_binding"], str) or not state["repo_binding"].strip()):
        raise StateLoadError(f"{label} repo_binding 必須是非空字串或 null")
    return state


def unread_issue_count(state) -> int:
    """計算尚未被人員標記已讀的 issue；舊版 state 沒 watermark 時全部視為未讀。"""
    issues = state.get("issues") if isinstance(state, dict) and isinstance(state.get("issues"), list) else []
    acknowledged = state.get("issues_acknowledged_round", -1) if isinstance(state, dict) else -1
    if not isinstance(acknowledged, int) or isinstance(acknowledged, bool):
        acknowledged = -1
    unread = 0
    for issue in issues:
        if not isinstance(issue, dict):
            unread += 1
            continue
        round_number = issue.get("round")
        if (not isinstance(round_number, int) or isinstance(round_number, bool) or
                round_number > acknowledged):
            unread += 1
    return unread


def decode_state_bytes(data: bytes, label: str):
    """解碼並驗證 state JSON；任何損壞都轉成帶來源標籤的 StateLoadError。"""
    try:
        state = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise StateLoadError(f"{label} JSON 損壞:{e}") from e
    if not isinstance(state, dict):
        raise StateLoadError(f"{label} 頂層必須是 JSON object,實得 {type(state).__name__}")
    return validate_state_shape(state, label)


def write_checkpointed_state(state_path: Path, data: bytes) -> None:
    """state.json 是主真相；主檔提交成功後才更新 last-good recovery copy。"""
    atomic_write_bytes(state_path, data)
    atomic_write_bytes(state_checkpoint_path(state_path), data)


def load_checkpointed_state(state_path: Path, *, repair: bool = True):
    """回傳 (state, canonical bytes, recovered)。

    primary 合法時永遠以它為準並刷新 checkpoint；只有 primary 不可讀時才採 checkpoint。
    repair=False 供唯讀 Dashboard 使用：可顯示 checkpoint，但不修改任何檔案。
    """
    state_path = workspace_file(state_path, "state.json")
    checkpoint = workspace_file(state_checkpoint_path(state_path), "state.last-good.json")
    primary_error = None
    try:
        fd = _open_regular(state_path, os.O_RDONLY)
        with os.fdopen(fd, "rb", closefd=True) as stream:
            primary_data = stream.read()
        state = decode_state_bytes(primary_data, "state.json")
    except FileNotFoundError as e:
        primary_error = e
    except (OSError, StateLoadError) as e:
        primary_error = e
    else:
        if repair:
            try:
                checkpoint_fd = _open_regular(checkpoint, os.O_RDONLY)
                with os.fdopen(checkpoint_fd, "rb", closefd=True) as stream:
                    checkpoint_matches = stream.read() == primary_data
            except OSError:
                checkpoint_matches = False
            if not checkpoint_matches:
                atomic_write_bytes(checkpoint, primary_data)
        return state, primary_data, False

    try:
        checkpoint_fd = _open_regular(checkpoint, os.O_RDONLY)
        with os.fdopen(checkpoint_fd, "rb", closefd=True) as stream:
            checkpoint_data = stream.read()
        state = decode_state_bytes(checkpoint_data, "state.last-good.json")
    except FileNotFoundError:
        if isinstance(primary_error, FileNotFoundError):
            raise FileNotFoundError(state_path)
        raise StateLoadError(f"state.json 無法讀取，且沒有 recovery checkpoint:{primary_error}") from primary_error
    except (OSError, StateLoadError) as checkpoint_error:
        raise StateLoadError(f"state.json 與 recovery checkpoint 都無法讀取:"
                             f"primary={primary_error}; checkpoint={checkpoint_error}") from checkpoint_error

    if repair:
        write_checkpointed_state(state_path, checkpoint_data)
    return state, checkpoint_data, True


def mark_state_recovered(state):
    """把 recovery 事件寫回 state，供 console/UI 稽核。"""
    try:
        count = max(0, int(state.get("state_recovery_count", 0))) + 1
    except (TypeError, ValueError):
        count = 1
    state["state_recovery_count"] = count
    state["last_state_recovery"] = datetime.now().isoformat(timespec="seconds")
    return state


class Workspace:
    """workspace/<name>/ 底下所有 python-owned 檔案的單一寫入點。"""

    def __init__(self, name: str):
        """建立/驗證 workspace 目錄與受管子目錄，但不自動啟動 loop。"""
        self.name = require_workspace_name(name)
        with workspace_operation_lock(WORKSPACE_ROOT, self.name):
            self.dir = workspace_path(WORKSPACE_ROOT, self.name)
            ensure_real_directory(self.dir, "workspace 目錄")
            for child in ("logs", "prompts", "snapshots"):
                ensure_real_directory(self.dir / child, f"workspace/{child}")
        self.state_path = self.dir / "state.json"
        self.checkpoint_path = state_checkpoint_path(self.state_path)
        self.history = self.dir / "history.log"
        self.stop_after_round_path = self.dir / STOP_AFTER_ROUND_FILE
        self.stop_after_round_claimed_path = self.dir / STOP_AFTER_ROUND_CLAIMED_FILE
        for path, label in (
            (self.state_path, "state.json"),
            (self.checkpoint_path, "state.last-good.json"),
            (self.history, "history.log"),
            (self.stop_after_round_path, "stop marker"),
            (self.stop_after_round_claimed_path, "claimed stop marker"),
            (self.dir / "console.log", "console.log"),
            (self.dir / "REPORT.md", "REPORT.md"),
        ):
            workspace_file(path, label)
        self._state_hash = None  # 本 session 內偵測 agent 直接改 state.json 用
        self._checkpoint_hash = None
        self.state_recovered = False

    # ---- state.json ----
    def fresh_state(self):
        """建立向後相容的全新規劃期 state，所有收斂與異常計數歸零。"""
        return {
            "phase": "plan", "round": 0, "flag": 0,
            "plan": [], "plan_version": 0,
            "current_order": 0, "done_count": 0,
            "completed": [],            # [{order, base_sha, sha, round}]
            "current_task_base_sha": None,
            "last_green_sha": None,
            "red_streak": 0, "stall_rounds": 0,
            "agent_failure_streak": 0, "agent_backoff_seconds": 0,
            "agent_backoff_until": None,
            "last_round_seconds": 0, "last_round_timed_out": False,
            "round_started_at": None, "round_deadline_at": None,
            "round_interrupted_at": None,
            "state_recovery_count": 0, "last_state_recovery": None,
            "repo_binding": None,       # workspace identity；不可由 config 子命令變更
            "task_reset_counts": {},    # {order(str): 次數}
            "notes": [],
            "issues": [],               # agent 用 work.py issue 回報,給人類看,不影響計數
            "issues_acknowledged_round": -1,
        }

    def load_state(self):
        """載入 primary/checkpoint；必要時受控復原並記錄本 session 的防竄改雜湊。"""
        try:
            state, data, recovered = load_checkpointed_state(self.state_path)
        except FileNotFoundError:
            return self.fresh_state()
        if recovered:
            self.state_recovered = True
            mark_state_recovered(state)
            data = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
            write_checkpointed_state(self.state_path, data)
        # 停機期間人工改合法 primary 視為真相；load helper 已同步成 checkpoint。
        self._state_hash = sha256_bytes(data)
        self._checkpoint_hash = self._state_hash
        return state

    def save_state(self, state):
        """同步原子寫入 primary 與 checkpoint，並更新本 session 的防竄改基準。"""
        data = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        write_checkpointed_state(self.state_path, data)
        self._state_hash = sha256_bytes(data)
        self._checkpoint_hash = self._state_hash

    def state_tampered(self) -> bool:
        """回傳 True 表示 agent 在本輪繞過 work.py 直接改了主 state 或 recovery copy。"""
        if self._state_hash is None:
            return False
        try:
            workspace_file(self.state_path, "state.json")
            workspace_file(self.checkpoint_path, "state.last-good.json")
            primary_data = self.state_path.read_bytes()
            checkpoint_data = self.checkpoint_path.read_bytes()
        except (FileNotFoundError, OSError, ValueError):
            return True
        return (sha256_bytes(primary_data) != self._state_hash or
                sha256_bytes(checkpoint_data) != self._checkpoint_hash)

    # ---- 輪間訊號(work.py 寫、loop 讀) ----
    def clear_signals(self):
        """清掉已結束 round 的 coordinator 產物。

        訊號檔名帶 round token；就算舊 CLI 的背景子行程在 clear 後才醒來，
        它也只能重建舊 token 的檔案，下一輪不會誤收。
        """
        names = ("called_create_plan", "pending_plan", "signal_plan_ok", "signal_done",
                 "pending_issues", "pending_block")
        for name in names:
            (self.dir / name).unlink(missing_ok=True)  # 清理舊版固定檔名
            for path in self.dir.glob(f"{name}.*"):
                path.unlink(missing_ok=True)
        # work.py 在 proposal 原子 replace 前若被 SIGKILL，可能留下隱藏 tmp；永不讀取，
        # 但長跑也不該讓它們無限累積。
        for path in self.dir.glob(".pending_plan.*.tmp.*"):
            path.unlink(missing_ok=True)

    def signal(self, name, round_token) -> bool:
        """只接受目前 round token 命名的 regular signal file。"""
        try:
            read_regular_bytes(self.dir / f"{name}.{round_token}", f"signal {name}")
            return True
        except (FileNotFoundError, OSError, ValueError):
            return False

    def take_pending_plan(self, round_token):
        """讀取本輪 plan proposal；損壞或不安全時忽略整包而不中止長跑 loop。"""
        p = self.dir / f"pending_plan.{round_token}.json"
        try:
            return json.loads(read_regular_text(p, "pending plan"))
        except (FileNotFoundError, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            # work.py 理論上只會原子寫入校驗過的 JSON；磁碟/外力仍可能破壞檔案。
            # 壞 proposal 只應讓本輪不採用，不能讓跑整夜的 loop 整支 crash。
            return None

    def pending_issues(self, round_token):
        """取得本輪 issue 暫存檔路徑；內容會在輪末集中併入 state。"""
        return self.dir / f"pending_issues.{round_token}"

    def pending_block_reason(self, round_token, task_id):
        """讀取 managed block terminal signal；存在但損壞時也 fail closed。"""
        path = self.dir / f"pending_block.{round_token}.json"
        try:
            payload = json.loads(read_regular_text(path, "pending managed block"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"managed block signal 損壞或不安全:{exc}"
        if (not isinstance(payload, dict)
                or set(payload) != {"schema_version", "round_token", "task_id", "reason"}
                or payload.get("schema_version") != 1
                or payload.get("round_token") != round_token
                or payload.get("task_id") != task_id
                or not isinstance(payload.get("reason"), str)
                or not payload["reason"].strip()
                or len(payload["reason"]) > ISSUE_MAX_CHARS):
            return "managed block signal schema/token/task 不符"
        return payload["reason"].strip()

    def write_dispatch(self, phase, task_id, round_token, *, runner="loop",
                       allow_serial_stack=False):
        """dispatch 是 work.py 的原子真相；另保留舊唯讀檔供既有 wrapper 觀測。"""
        payload = json.dumps({
            "phase": phase,
            "task_id": task_id,
            "round_token": round_token,
            "runner": runner,
            "allow_serial_stack": allow_serial_stack is True,
        }, ensure_ascii=False).encode("utf-8")
        atomic_write_bytes(self.dir / "dispatch.json", payload)
        atomic_write_bytes(self.dir / "phase", phase.encode("utf-8"))
        atomic_write_bytes(self.dir / "current_task", task_id.encode("utf-8"))

    def take_stop_after_round(self, pid, session_id) -> bool:
        """原子接手控制檔；成功時留下 session-bound marker，供 Dashboard 誠實顯示不可撤銷狀態。"""
        return claim_stop_after_round(self.dir, pid, session_id)

    # ---- 受保護檔案快照(goal / 初步規劃書) ----
    def _protected_snapshot_path(self, rel):
        """受保護檔案在 workspace 內的快照路徑；扁平化 rel 並強制 regular file。"""
        return workspace_file(self.dir / "snapshots" / rel.replace("/", "__"), "protected snapshot")

    def snapshot_protected(self, repo, rel_paths):
        """複製 goal/plan doc 到 workspace snapshot，供輪末偵測 Agent 越權修改。"""
        for rel in rel_paths:
            target = repo_relative_path(repo, rel)
            snap = self._protected_snapshot_path(rel)
            snap.write_bytes(target.read_bytes())

    def protected_changed(self, repo, rel_paths):
        """純偵測:回傳被刪或被改的受保護檔案清單(空 = 沒人亂動)。不寫回。"""
        hit = []
        for rel in rel_paths:
            snap = self._protected_snapshot_path(rel).read_bytes()
            try:
                target = repo_relative_path(repo, rel)
            except ValueError:
                hit.append(rel)
                continue
            if (not target.exists()) or target.read_bytes() != snap:
                hit.append(rel)
        return hit

    def restore_protected(self, repo, rel_paths):
        """把受保護檔案寫回快照(供 reset 後補正,green sha 版本理應相同故多為 no-op)。
        寫回前先建父目錄:green 不含該子目錄時 write_bytes 會 FileNotFoundError(#1)。"""
        for rel in rel_paths:
            snap = self._protected_snapshot_path(rel).read_bytes()
            target = repo_relative_path(repo, rel)
            if (not target.exists()) or target.read_bytes() != snap:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(snap)


def workspace_has_managed_worker_identity(workspace) -> bool:
    """Probe both durable copies before any reset/preflight side effect.

    A managed worker is readonly to ordinary Loop entrypoints.  Looking at both
    copies prevents ``--reset-state`` from erasing the runner field before the
    guard and also fails closed while one copy is awaiting checkpoint recovery.
    Malformed unrelated state is left to the existing loader/reset behavior.
    """
    for path in (workspace.state_path, workspace.checkpoint_path):
        try:
            payload = json.loads(read_regular_text(path, path.name))
        except (FileNotFoundError, OSError, ValueError, UnicodeDecodeError,
                json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("runner") == parallel_worker.WORKER_RUNNER:
            return True
    return False


def workspace_has_parallel_supervisor_identity(workspace) -> bool:
    """Fail closed before reset can erase a durable parallel base identity."""
    for path in (workspace.state_path, workspace.checkpoint_path):
        try:
            payload = json.loads(read_regular_text(path, path.name))
        except (FileNotFoundError, OSError, ValueError, UnicodeDecodeError,
                json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("runner") == "parallel-supervisor":
            return True
    return False


def run_agent(cmd, prompt, repo, env, log_path, timeout_secs, on_started=None):
    """跑一輪 agent：prompt 由 stdin pipe 注入，不把 prompt 檔案交給子程序，
    stdout/stderr 逐行同步印上 console 並落 log 檔。
    逾時 SIGKILL 整個 process group(start_new_session 保證殺得到子孫)。
    回傳 (rc, 秒數, 是否逾時)。"""
    t0 = time.monotonic()
    timed_out = False
    env = dict(env)
    if compat.IS_WINDOWS:
        # The stdin contract is UTF-8 bytes.  Python-based CLIs otherwise use
        # the active ANSI code page and silently mojibake non-ASCII prompts.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
    log_fd = _open_regular(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    prompt_bytes = prompt.encode("utf-8")
    with os.fdopen(log_fd, "w", encoding="utf-8", closefd=True) as lf:
        p = _owner_spawn(
            repo_owner.ChildKind.AGENT, list(cmd), cwd=str(repo), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if p is None:
            p = subprocess.Popen(
                cmd, cwd=str(repo), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                **compat.popen_group_kwargs())
            compat.attach_process_group(p)
        process_group = p.pid  # start_new_session=True → pgid 固定等於 child pid
        reader_errors = []
        writer_errors = []
        escaped_pipe = False

        def _kill_group():
            """終止 Agent 的獨立 process group，避免背景子程序持續佔用 pipe。"""
            try:
                if isinstance(p, repo_owner.ControlledOwnerChild):
                    p.kill_containment()
                else:
                    safe_killpg(p, compat.FORCE_SIGNAL)
            except (ProcessLookupError, PermissionError):
                pass

        def _stream_output():
            """逐行鏡像 Agent stdout 到 console 與 round log；錯誤留給主執行緒轉拋。"""
            try:
                for raw in p.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    agent_log(line.rstrip("\n"))
                    lf.write(line)
                    lf.flush()  # 逐行落盤,dashboard 才 tail 得到即時輸出
            except Exception as e:  # noqa: BLE001 — 主執行緒清理 process group 後再轉拋
                reader_errors.append(e)

        def _write_prompt():
            """在獨立 thread 寫 stdin，避免 Agent 先輸出再讀取時和 stdout pipe 互鎖。"""
            try:
                p.stdin.write(prompt_bytes)
                p.stdin.flush()
            except BrokenPipeError:
                # CLI 可在讀完前主動退出；與 subprocess.communicate 的行為一致，不另判失敗。
                pass
            except Exception as e:  # noqa: BLE001 — 主執行緒完成 process-group 清理後再轉拋
                writer_errors.append(e)
            finally:
                try:
                    p.stdin.close()
                except (OSError, ValueError):
                    pass

        # stdout 由 reader thread 處理，主執行緒直接等 CLI 主程序。否則 CLI 已退出、
        # 背景孫行程仍握著 stdout pipe 時，`for raw in stdout` 會把 round 卡到孫行程結束。
        reader = threading.Thread(target=_stream_output, name="agent-output", daemon=True)
        writer = threading.Thread(target=_write_prompt, name="agent-prompt", daemon=True)
        reader.start()
        writer.start()
        try:
            if on_started:
                on_started(p.pid)
            try:
                compat.wait_process(p, timeout=timeout_secs if timeout_secs else None)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group()
                p.wait()
        except KeyboardInterrupt:
            # 人(或 dashboard)停掉 loop:把跑到一半的 agent 整個 process group 帶走,不留孤兒
            _kill_group()
            raise
        finally:
            # CLI 主程序不論正常、錯誤或超時退出，round 到此即封口；清掉仍存活的同組
            # 子孫，避免它們在 Validate 或下一輪期間繼續改 repo/寫 coordinator 訊號。
            _kill_group()
            if p.poll() is None:
                p.wait()
            writer.join(timeout=5)
            reader.join(timeout=5)
            if writer.is_alive() or reader.is_alive():
                # 有子行程刻意 setsid 逃離 process group 且仍握著 pipe；不能讓它反過來
                # 卡死 loop，也不能帶著未知 writer 繼續下一輪。不要在此直接 close BufferedReader：
                # reader thread 可能正持有其 lock，close 本身反而會永久阻塞。
                escaped_pipe = True
                log("⛔ Agent 有逃離 process group 的背景程序仍持有 stdin/stdout；為避免跨輪競寫，loop 停止")
            else:
                p.stdout.close()
            if isinstance(p, repo_owner.ControlledOwnerChild):
                _owner_record(p)
        if escaped_pipe:
            raise RuntimeError("Agent 背景程序逃離 process group，無法安全進入下一輪")
        if writer_errors:
            raise writer_errors[0]
        if reader_errors:
            raise reader_errors[0]
    return p.returncode, time.monotonic() - t0, timed_out


def notify(cmd, status, name, *, event_id=None):
    """送出通知並回報是否成功；一般 Loop 呼叫者可繼續忽略結果。"""
    if not cmd:
        return True
    try:
        rendered = cmd.replace("{status}", status).replace("{name}", name)
        if event_id is not None:
            rendered = rendered.replace("{event_id}", str(event_id))
        notify_argv = compat.split_command(rendered)
        controlled = _owner_spawn(
            repo_owner.ChildKind.TOOL, notify_argv,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if controlled is None:
            result = subprocess.run(notify_argv, capture_output=True, timeout=15)
        else:
            try:
                stdout, stderr = controlled.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                controlled.kill_containment()
                _owner_record_and_checkpoint(controlled)
                raise
            except KeyboardInterrupt:
                controlled.kill_containment()
                _owner_record_and_checkpoint(controlled)
                raise
            _owner_record_and_checkpoint(controlled)
            result = subprocess.CompletedProcess(
                notify_argv, controlled.returncode, stdout, stderr)
        if result.returncode:
            log(f"⚠ notify 失敗 rc={result.returncode}(不影響一般 Loop 結果)")
            return False
        log(f"🔔 notify 已送出:{status}")
        return True
    except Exception as e:  # noqa: BLE001 — 通知永不擋主流程
        log(f"⚠ notify 失敗(不影響結果):{e}")
        return False


def run_validate(cmd, repo, timeout_secs=VALIDATE_TIMEOUT_SEC):
    """執行正式 validator；逾時或中斷時清掉整個 validator process group。
    killpg 只保證殺得死直接子行程；若有孫行程刻意 setsid 逃離 process group 仍握著
    stdout，收尾讀取最多再等 5 秒，逾時就放棄剩餘輸出——否則啟動前的綠點驗證會被
    卡死，dashboard 永遠等不到 startup handshake，workspace 也就一直沒有 state.json。"""
    try:
        p = _owner_spawn(
            repo_owner.ChildKind.VALIDATOR, list(cmd), cwd=str(repo),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if p is None:
            p = subprocess.Popen(
                cmd, cwd=str(repo), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
                **compat.popen_group_kwargs())
            compat.attach_process_group(p)
    except FileNotFoundError:
        return False, f"找不到 Validate 命令：{cmd[0]}", False
    timed_out = False
    escaped = False
    try:
        out, _ = p.communicate(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            if isinstance(p, repo_owner.ControlledOwnerChild):
                p.kill_containment()
            else:
                safe_killpg(p, compat.FORCE_SIGNAL)
        except (ProcessLookupError, PermissionError):
            pass
        p.wait()  # SIGKILL 保證直接子行程終止；卡住的只會是下面等孫行程放開 stdout 的讀取
        drained = {}
        def _drain():
            try:
                drained["out"] = p.stdout.read()
            except Exception:  # noqa: BLE001 — 讀取失敗不影響「已逾時」判定，直接視為無收尾輸出
                pass
        drainer = threading.Thread(target=_drain, daemon=True)
        drainer.start()
        drainer.join(timeout=5)
        escaped = drainer.is_alive()
        out = drained.get("out") or ""
        if not escaped:
            p.stdout.close()
    except KeyboardInterrupt:
        try:
            if isinstance(p, repo_owner.ControlledOwnerChild):
                p.kill_containment()
            else:
                safe_killpg(p, compat.FORCE_SIGNAL)
        except (ProcessLookupError, PermissionError):
            pass
        p.wait()
        raise
    finally:
        # Job Object 也必須在正常成功時關閉；逾時路徑已關閉時此操作為 no-op。
        if isinstance(p, repo_owner.ControlledOwnerChild):
            if p.poll() is None:
                p.kill_containment()
            _owner_record(p)
        else:
            compat.close_process_group(p)
    out = (out or "").strip()
    tail = "\n".join(out.splitlines()[-VALIDATE_TAIL:])
    if escaped:
        tail = (tail + "\n" if tail else "") + "⚠️ 有孫行程逃離 process group 仍握著輸出管線，已放棄等待收尾。"
    if timed_out:
        tail = (f"Validate 執行超過 {timeout_secs:g} 秒，已終止" + (f"\n{tail}" if tail else ""))
    return p.returncode == 0 and not timed_out, tail, timed_out


def run_completion_gate(cmd, repo, env, timeout_secs):
    """Run the supervisor-owned gate client and capture its strict JSON line.

    The command itself is not allowed to mutate Git.  This helper only owns
    process lifetime; response/schema and repository invariants are checked by
    the caller.
    """
    child_env = dict(env)
    if compat.IS_WINDOWS:
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
    try:
        process = subprocess.Popen(
            cmd, cwd=str(repo), env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **compat.popen_group_kwargs(),
        )
        compat.attach_process_group(process)
    except (FileNotFoundError, OSError) as exc:
        return 127, "", f"gate client 無法啟動:{exc}", False
    timed_out = False
    escaped = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            safe_killpg(process, compat.FORCE_SIGNAL)
        except (ProcessLookupError, PermissionError):
            pass
        # 只等被直接 spawn 的 gate client；即使有逃逸子孫持有 pipe，也不可再用
        # communicate() 無限等待 EOF。讀取工作交給 daemon threads 並設硬上限。
        try:
            process.wait(timeout=GATE_DRAIN_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                process.wait(timeout=GATE_DRAIN_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                pass

        drained = {"stdout": b"", "stderr": b""}

        def _drain(name, stream):
            try:
                drained[name] = stream.read() or b""
            except Exception:  # noqa: BLE001 - timeout cleanup must remain best effort
                pass

        drainers = []
        for name in ("stdout", "stderr"):
            stream = getattr(process, name, None)
            if stream is None:
                continue
            thread = threading.Thread(target=_drain, args=(name, stream), daemon=True)
            thread.start()
            drainers.append(thread)
        deadline = time.monotonic() + GATE_DRAIN_TIMEOUT_SEC
        for thread in drainers:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        escaped = any(thread.is_alive() for thread in drainers)
        stdout, stderr = drained["stdout"], drained["stderr"]
    except KeyboardInterrupt:
        try:
            safe_killpg(process, compat.FORCE_SIGNAL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()
        raise
    finally:
        compat.close_process_group(process)
    try:
        stdout = (stdout if isinstance(stdout, str)
                  else (stdout or b"").decode("utf-8", errors="strict"))
        stderr = (stderr if isinstance(stderr, str)
                  else (stderr or b"").decode("utf-8", errors="strict"))
    except UnicodeError as exc:
        return process.returncode, "", f"gate client 輸出不是合法 UTF-8:{exc}", timed_out
    if escaped:
        escape_note = "gate client timeout 後仍有程序持有 stdout/stderr pipe；輸出只取可安全讀到的部分"
        stderr = (stderr.rstrip() + "\n" if stderr.strip() else "") + escape_note
    return process.returncode, stdout or "", stderr or "", timed_out


def apply_managed_completion_gate(state, repo, workspace, *, round_number, validated_sha,
                                  timeout_seconds):
    """Apply one exact-SHA gate result to a managed worker assignment."""
    assignment = state["assignment"]
    order = state["assigned_order"]
    try:
        gate_cmd = compat.split_command(state["complete_gate_cmd"])
    except ValueError as exc:
        gate_cmd = []
        parse_error = f"gate command 格式錯誤:{exc}"
    else:
        parse_error = "" if gate_cmd else "gate command 不可為空"
    if parse_error:
        assignment.update({
            "status": "blocked", "validated_sha": validated_sha,
            "validated_round": round_number, "exit_reason": parse_error,
            "gate_request": None,
        })
        state["done_count"] = 0
        return f"⛔ task-{order} gate fatal｜{parse_error}"

    before_gate = repository_snapshot(repo)
    if (before_gate.dirty or before_gate.head != validated_sha
            or before_gate.head_ref != state.get("task_ref")):
        reason = (
            "gate 前 worker snapshot 不再是剛驗證的乾淨 exact SHA"
            f"（validated={validated_sha[:8]}、HEAD={before_gate.head[:8]}、"
            f"ref={before_gate.head_ref or '(detached)'}、dirty={before_gate.dirty}）"
        )
        assignment.update({
            "status": "blocked", "validated_sha": validated_sha,
            "validated_round": round_number, "exit_reason": reason,
            "gate_request": None,
        })
        state["done_count"] = 0
        return f"⛔ task-{order} gate fatal｜{reason}"

    request_id = uuid.uuid4().hex
    gate_request = {
        "request_id": request_id,
        "validated_sha": validated_sha,
        "validated_round": round_number,
    }
    assignment.update({
        "status": "running", "validated_sha": validated_sha,
        "validated_round": round_number, "exit_reason": None,
        "gate_request": gate_request,
    })
    # 這是 request 的 durable commit point。若 worker 在 child claim 前後死亡，
    # supervisor 會看到 durable gate_request 並 reconcile；worker 不可把同一完成票當新 request 重送。
    workspace.save_state(state)
    gate_env = expose_project_package({
        **os.environ,
        "RUN_ID": state["run_id"],
        "TASK": str(order),
        "REQUEST_ID": request_id,
        "VALIDATED_SHA": validated_sha,
        "VALIDATED_ROUND": str(round_number),
        "RUN_CONFIG_HASH": state["run_config_hash"],
        "LAUNCH_SPEC_HASH": state["launch_spec_hash"],
        "MANIFEST_HASH": state["manifest_hash"],
    })

    returncode, stdout, stderr, timed_out = run_completion_gate(
        gate_cmd, repo, gate_env, timeout_seconds)
    after_gate = repository_snapshot(repo)
    gate_mutated_repo = after_gate != before_gate
    if timed_out:
        reason = f"gate client 超過 {timeout_seconds:g} 秒；claim 狀態未知，必須 reconcile"
        if gate_mutated_repo:
            reason += "；且 gate client 違反唯讀契約並改變 worker Git snapshot"
        assignment.update({
            "status": "recovery-required", "validated_sha": validated_sha,
            "validated_round": round_number, "exit_reason": reason,
        })
        state["done_count"] = 0
        return f"⛔ task-{order} gate recovery-required｜{reason}"
    if gate_mutated_repo:
        reason = ("gate client 違反唯讀契約並改變 worker Git snapshot；"
                  "claim 狀態不可再由 worker 推定，必須 reconcile")
        assignment.update({
            "status": "recovery-required", "validated_sha": validated_sha,
            "validated_round": round_number, "exit_reason": reason,
        })
        state["done_count"] = 0
        return f"⛔ task-{order} gate recovery-required｜{reason}"
    try:
        result = parallel_contract.parse_gate_response(
            returncode, stdout,
            run_id=state["run_id"], task=order, request_id=request_id,
            validated_sha=validated_sha,
        )
    except parallel_contract.ParallelContractError as exc:
        stderr_tail = "\n".join(stderr.strip().splitlines()[-10:])
        reason = (f"gate protocol fatal:{exc}；claim 狀態未知，必須 reconcile"
                  + (f"；stderr:{stderr_tail}" if stderr_tail else ""))
        assignment.update({
            "status": "recovery-required", "validated_sha": validated_sha,
            "validated_round": round_number, "exit_reason": reason,
        })
        state["done_count"] = 0
        return f"⛔ task-{order} gate recovery-required｜{reason}"

    reason = result.reason
    if result.status in {"merged", "already-merged"}:
        assignment.update({
            "status": "integrated", "validated_sha": validated_sha,
            "validated_round": round_number, "exit_reason": None,
            "gate_request": None,
        })
        state["done_count"] = 0
        return f"✅ task-{order} 已由 supervisor gate 整合 @ {validated_sha[:8]}"
    if result.status == "stale-integration":
        state["done_count"] = 0
        assignment.update({
            "status": "running", "validated_sha": None,
            "validated_round": None, "exit_reason": None,
            "gate_request": None,
        })
        state["notes"].append(
            "↪ integration 已前進；下一輪先同步 safe integration ref、完整 Validate，"
            "再重新累計 done。")
        return f"↪ task-{order} gate stale｜重新同步並收斂"
    if result.status in {"busy", "supervisor-lost-before-claim"}:
        assignment.update({
            "status": "running", "validated_sha": None,
            "validated_round": None, "exit_reason": None,
            "gate_request": None,
        })
        state["notes"].append(
            "⏳ gate 尚未 claim 且 client 已安全取消；保留 done 共識，下輪可重試。")
        return f"⏳ task-{order} gate busy｜保留 done={state['done_count']}"

    status_map = {
        "paused": "paused",
        "cancelled": "cancelled",
        "fatal-invariant": "blocked",
        "recovery-required-after-claim": "recovery-required",
    }
    assignment_status = status_map[result.status]
    exit_reason = reason or result.status
    terminal_update = {
        "status": assignment_status, "validated_sha": validated_sha,
        "validated_round": round_number, "exit_reason": exit_reason,
    }
    if assignment_status != "recovery-required":
        terminal_update["gate_request"] = None
    assignment.update(terminal_update)
    state["done_count"] = 0
    return f"⛔ task-{order} gate {assignment_status}｜{exit_reason}"


def mark_managed_worker_blocked(state, reason) -> bool:
    """Persist a controlled worker failure as supervisor-visible terminal state."""
    if state.get("runner") != parallel_worker.WORKER_RUNNER:
        return False
    assignment = state.get("assignment")
    if not isinstance(assignment, dict):
        return False
    assignment.update({
        "status": "blocked",
        "exit_reason": str(reason).strip() or "managed worker failure",
        "gate_request": None,
    })
    state["done_count"] = 0
    return True


def render_task_list(state):
    """將 plan 投影成 prompt 內的精簡任務清單，標示完成與目前任務並限制單行長度。"""
    done_orders = {e["order"] for e in state["completed"]}
    lines = []
    for t in state["plan"]:
        text = t["task"].replace("\n", " ")
        if len(text) > TASK_LIST_TRUNC:
            text = text[:TASK_LIST_TRUNC] + "…"
        if t["order"] in done_orders:
            mark = "[✔]"
        elif t["order"] == state["current_order"]:
            mark = "[→]"
        else:
            mark = "[ ]"
        lines.append(f"{mark} task-{t['order']}: {text}")
    return "\n".join(lines) if lines else "(尚無計畫)"


def build_prompt(tpl_path, mapping):
    """以固定 placeholder 做純文字替換；不執行模板內容。"""
    template = tpl_path.read_text(encoding="utf-8")
    placeholder_re = re.compile(r"<<([A-Z][A-Z0-9_]*)>>")
    required = {match.group(1) for match in placeholder_re.finditer(template)}
    missing = sorted(required - set(mapping))
    if missing:
        unresolved = [f"<<{name}>>" for name in missing]
        raise ValueError(f"prompt placeholder 未完整注入:{', '.join(unresolved)}")
    # 單次替換只解讀 template 本身的 token。GOAL/TASK/NOTES 等不可信文字即使含
    # ``<<TOKEN>>`` 也保持原樣，不會被後續 mapping 項目二次替換或誤判為漏注入。
    return placeholder_re.sub(lambda match: str(mapping[match.group(1)]), template)


def coordinator_command(action, *args, python_executable=None):
    """建立給 Agent 的 coordinator 指令，固定帶目前 Python executable 的完整路徑。"""
    executable = Path(python_executable or sys.executable).expanduser().resolve()
    return shlex.join([str(executable), "-m", "engine.work", action, *args])


def fenced_block(text):
    """以動態長度反引號圍欄包住不可信輸出；內容含 ``` 時圍欄不會被提前關閉。"""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def agent_failure_backoff(streak, maximum_seconds) -> float:
    """CLI 連續異常的機械退避：1,2,4...秒並封頂；0 表示關閉。"""
    if streak <= 0 or maximum_seconds <= 0:
        return 0.0
    # 防止被手改的巨大 streak 觸發超大整數運算；超過 2^30 對實際秒數上限已無意義。
    return min(float(maximum_seconds), float(2 ** min(streak - 1, 30)))


@dataclass(frozen=True)
class RuntimeOptions:
    """已通過 CLI 邊界驗證、可直接交給 coordinator 的啟動參數。"""

    args: argparse.Namespace
    repo: Path
    workspace_name: str
    agent_cmd: list
    validate_cmd: list
    protected: list
    plan_doc_display: str


def build_argument_parser() -> argparse.ArgumentParser:
    """宣告 CLI 契約；解析後的跨欄位與路徑驗證由 parse_runtime_options 負責。"""
    parser = argparse.ArgumentParser(description="loop-agent-lite:規劃/執行雙段共識迴圈")
    parser.add_argument("--repo", required=True, help="target code repo(git、乾淨、validate 綠)")
    parser.add_argument("--name", default=None,
                        help="workspace 名稱(預設=repo 目錄名;不可 . / .. 或以 . 開頭)")
    parser.add_argument("--goal", default="goal.md", help="goal 檔(相對 repo,須已 commit)")
    parser.add_argument("--plan-doc", default="", help="選配:參考分析文件(相對 repo);提供的話須已 commit 且受保護")
    parser.add_argument("--agent-cmd", default=None, help="agent CLI 命令(整串;prompt 走 stdin)")
    parser.add_argument("--validate-cmd", default=None, help="驗證命令(預設 mvn -q compile)")
    parser.add_argument("--flag-threshold", type=int, default=FLAG_THRESHOLD)
    parser.add_argument("--done-threshold", type=int, default=DONE_THRESHOLD)
    parser.add_argument("--red-limit", type=int, default=RED_LIMIT)
    parser.add_argument("--stall-limit", type=int, default=STALL_LIMIT)
    parser.add_argument("--stuck-stop", action="store_true", help="同一任務 reset 達上限即停機(預設關)")
    parser.add_argument("--stuck-stop-count", type=int, default=STUCK_STOP_COUNT)
    parser.add_argument("--round-timeout", type=float, default=ROUND_TIMEOUT_MIN,
                        help="單輪 agent 上限(分鐘;0=不限,預設 30)")
    parser.add_argument("--agent-backoff-max", type=float, default=AGENT_BACKOFF_MAX_SEC,
                        help="Agent CLI 連續異常退出的指數退避上限(秒;0=關閉,預設 60)")
    parser.add_argument("--validate-timeout", type=float, default=VALIDATE_TIMEOUT_SEC,
                        help="啟動前與每輪 Validate 上限(秒;必須 >0,預設 120)")
    parser.add_argument("--notify-cmd", default="", help="終態通知命令,佔位符 {status} {name}(空=不通知)")
    parser.add_argument("--import-plan", default="", help="匯入 plan.json(重置 state;等同 dashboard 貼上匯入)")
    parser.add_argument("--consume-import-plan", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--start-phase", choices=("plan", "exec"), default="plan",
                        help="搭配 --import-plan:從規劃期(讓 agent 補完)或直接執行期開跑")
    parser.add_argument("--pause-after-plan", action="store_true",
                        help="規劃收斂後暫停:不自動進入執行期,人工按「▶ 運行」才開始執行輪")
    parser.add_argument("--allow-serial-stack", action="store_true",
                        help="明確允許普通 Loop 忽略 plan.stack 並依 order 串行執行")
    parser.add_argument("--max-rounds", type=int, default=0, help="總輪數上限;0=不限(測試用)")
    parser.add_argument("--reset-state", action="store_true", help="清掉 workspace state 從頭跑")
    parser.add_argument("--preflight-only", action="store_true",
                        help="只跑啟動前健檢(git/鎖/乾淨樹/goal 已 commit/validate)就退出;"
                             "不建 state、不動 snapshots、不啟動 agent")
    parser.add_argument("--init-only", action="store_true",
                        help="完成 preflight 並建立 stopped workspace/state 後退出，不啟動 agent")
    parser.add_argument("--resume-interrupted", action="store_true",
                        help="僅限已開始且有既有綠點的執行期輪次：保留現場並略過啟動 Validate")
    parallel_worker.add_arguments(parser)
    return parser


def parse_runtime_options(argv=None) -> RuntimeOptions:
    """解析並驗證所有不需存取 Git/state 的啟動參數，回傳 coordinator 所需衍生值。"""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    # 這些值直接控制共識/timeout；0、負數或 NaN 不可被解讀成「立刻收斂」。
    for attr, option in (("flag_threshold", "--flag-threshold"),
                         ("done_threshold", "--done-threshold"),
                         ("red_limit", "--red-limit"),
                         ("stall_limit", "--stall-limit"),
                         ("stuck_stop_count", "--stuck-stop-count")):
        if getattr(args, attr) < 1:
            parser.error(f"{option} 必須 ≥ 1")
    if args.max_rounds < 0:
        parser.error("--max-rounds 必須 ≥ 0")
    for attr, option, positive in (("round_timeout", "--round-timeout", False),
                                   ("agent_backoff_max", "--agent-backoff-max", False),
                                   ("validate_timeout", "--validate-timeout", True)):
        value = getattr(args, attr)
        if not math.isfinite(value) or value < 0 or (positive and value == 0):
            parser.error(f"{option} 必須是{' > 0' if positive else ' ≥ 0'} 的有限數字")
    if args.preflight_only and args.init_only:
        parser.error("--preflight-only 不可搭配 --init-only")
    if args.resume_interrupted and (args.preflight_only or args.init_only or
                                    args.reset_state or args.import_plan):
        parser.error("--resume-interrupted 不可搭配 --preflight-only、--init-only、"
                     "--reset-state 或 --import-plan")
    worker_launch = parallel_worker.validate_launch_args(parser, args)
    if worker_launch is not None and args.allow_serial_stack:
        parser.error("managed worker 不可搭配 --allow-serial-stack；stack 由 supervisor 派工")
    if worker_launch is not None and args.pause_after_plan:
        parser.error("managed worker 固定從 exec 啟動，不可搭配 --pause-after-plan")
    if worker_launch is not None and args.notify_cmd:
        parser.error("managed worker 不可直接送全域 notify；終態通知由 supervisor 統一處理")
    args.managed_worker_launch = worker_launch

    repo = Path(args.repo).resolve()
    workspace_name = args.name or repo.name
    try:
        require_workspace_name(workspace_name)
    except ValueError as e:
        parser.error(f"--name {e}")
    protected = [args.goal] + ([args.plan_doc] if args.plan_doc else [])
    for option, relative_path in (("--goal", args.goal), ("--plan-doc", args.plan_doc)):
        if not relative_path:
            continue
        try:
            repo_relative_path(repo, relative_path)
        except ValueError as e:
            parser.error(f"{option} {e}")
    return RuntimeOptions(
        args=args,
        repo=repo,
        workspace_name=workspace_name,
        agent_cmd=compat.split_command(args.agent_cmd) if args.agent_cmd else AGENT_CMD,
        validate_cmd=compat.split_command(args.validate_cmd) if args.validate_cmd else VALIDATE_CMD,
        protected=protected,
        plan_doc_display=(str(repo_relative_path(repo, args.plan_doc)) if args.plan_doc
                          else "(未提供——以 goal、現有計畫與實際程式碼為準)"),
    )


def guard_repository_baseline(repo: Path, protected, *, allow_dirty=False) -> None:
    """取得 worktree 單 writer 鎖，並確認 Git、乾淨樹與受保護檔案可作為起點。"""
    if git(repo, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        fail(f"preflight：{repo} 不是 git repo")
    if git(repo, "rev-parse", "HEAD", check=False).returncode != 0:
        fail(f"preflight：{repo} 沒有任何 commit")
    git_dir = Path(git(repo, "rev-parse", "--git-dir").stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (repo / git_dir).resolve()
    if _REPO_OWNER_FENCE is None:
        acquire_run_lock(git_dir / "loop-agent-lite.run.lock", f"Git worktree {repo}")
    if is_dirty(repo) and not allow_dirty:
        fail("preflight：工作樹不乾淨。之後的 reset --hard 會吃掉你的 WIP，先 commit 或 stash 再來")
    for relative_path in protected:
        if not tracked_in_head(repo, relative_path):
            fail(f"preflight：{relative_path} 不在 HEAD 裡。流程是：模板產初版 → 你審 → commit → 才 run loop")


def run_preflight_check(repo: Path, validate_cmd, timeout_seconds: float) -> None:
    """執行不建立 state 的完整啟動健檢；失敗時以既有 fail-closed 路徑終止。"""
    log(f"🔎 Preflight 健檢（--preflight-only,不啟動 loop）｜驗證:{shlex.join(validate_cmd)}")
    before_validate = repository_snapshot(repo)
    ok, tail, timed_out = run_validate(validate_cmd, repo, timeout_seconds)
    # The validator tree is fully reaped.  Release its single durable child
    # slot before post-validation Git observations launch as controlled
    # children of their own.
    _owner_checkpoint_reaped()
    after_validate = repository_snapshot(repo)
    if after_validate != before_validate or after_validate.dirty:
        effect = ("執行後弄髒工作樹" if after_validate.dirty
                  else "執行後改變 HEAD/Git snapshot")
        fail(f"preflight-only:validate `{shlex.join(validate_cmd)}` {effect}——"
             "validate 不可 commit，也不可修改 tracked/untracked 原始碼。輸出尾段:\n" + tail)
    if not ok:
        timeout_note = f"（逾時 {timeout_seconds:g} 秒）" if timed_out else ""
        fail(f"preflight-only:驗證失敗{timeout_note}——全新啟動會被擋"
             f"(既有 workspace 若有合法綠點,resume 仍可能放行)。輸出尾段:\n{tail}")
    log("✅ preflight-only 全部通過｜repo 乾淨、goal/plan-doc 已 commit、validate 綠、無其他 loop 佔用")


def establish_startup_green_anchor(repo: Path, workspace, state, protected,
                                   validate_cmd, timeout_seconds: float) -> None:
    """驗證目前 HEAD，或在紅燈時確認既有 last-green 仍是合法、安全的錨點。"""
    log(f"🔎 啟動前檢查｜執行驗證：{shlex.join(validate_cmd)}")
    before_validate = repository_snapshot(repo)
    ok, tail, timed_out = run_validate(validate_cmd, repo, timeout_seconds)
    _owner_checkpoint_reaped()
    after_validate = repository_snapshot(repo)
    # validator 若修改原始碼，就算 exit 0 也不能讓副作用混成下一輪 agent 變更。
    if after_validate != before_validate or after_validate.dirty:
        effect = ("執行後弄髒工作樹" if after_validate.dirty
                  else "執行後改變 HEAD/Git snapshot")
        fail(f"啟動前驗證 `{shlex.join(validate_cmd)}` {effect}——"
             "validate 必須只產生 ignored build artifacts,不能 commit 或修改 tracked/untracked 原始碼。"
             f"輸出尾段:\n{tail}")
    if ok:
        state["last_green_sha"] = after_validate.head
        log(f"✅ 啟動前檢查完成｜驗證通過｜綠點 {state['last_green_sha'][:8]}")
        return

    green = state["last_green_sha"]
    if green_anchor_valid(repo, green, workspace.dir / "snapshots", protected):
        log(f"⚠️ 啟動驗證失敗｜沿用已確認綠點 {green[:8]} 繼續修復")
        if tail:
            log(f"驗證錯誤尾段：\n{tail}")
        state["notes"].append(f"❌ 啟動時 `{shlex.join(validate_cmd)}` 就是紅的,"
                              f"先把它修綠再繼續往下做。輸出尾段:\n{fenced_block(tail)}")
        return

    why = ("沒有綠點可錨定" if not green else
           f"綠點 {green[:8]} 未通過驗證(不存在/非 HEAD 祖先/protected 與現況分歧)")
    timeout_note = f"（逾時 {timeout_seconds:g} 秒）" if timed_out else ""
    fail(f"啟動驗證 `{shlex.join(validate_cmd)}` 失敗{timeout_note}，{why}——"
         f"起點必須是可信綠點。先把工作樹修到 validate 綠再 resume。輸出尾段:\n{tail}")


def interrupted_resume_block_reason(repo: Path, workspace_dir: Path, state, protected):
    """Resume 最小資格：開始時間早於現在，且 SHA 是 code repo 內存在的 commit。"""
    _started_at, _green, reason = normalize_interrupted_resume_metadata(repo, state)
    return reason


def normalize_interrupted_resume_metadata(repo: Path, state):
    """驗證並正規化可由人工補登的 Resume 時間與綠點 SHA。"""
    raw_started_at = state.get("round_started_at")
    if not isinstance(raw_started_at, str) or not raw_started_at.strip():
        return None, None, "請補上執行開始時間"
    try:
        parsed = datetime.fromisoformat(raw_started_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return None, None, "執行開始時間格式不正確"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    if parsed >= datetime.now(parsed.tzinfo):
        return None, None, "執行開始時間必須早於現在"

    raw_green = state.get("last_green_sha")
    if not isinstance(raw_green, str) or not re.fullmatch(r"[0-9a-fA-F]{4,64}", raw_green.strip()):
        return None, None, "請補上 code repo 內存在的 commit SHA"
    # 前面已限制為純 hex，不需依賴較新版 Git 才支援的 --end-of-options；內網舊 Linux Git 也可用。
    resolved = git(repo, "rev-parse", "--verify", "--quiet",
                   f"{raw_green.strip()}^{{commit}}", check=False)
    if resolved.returncode != 0:
        return None, None, "指定的 SHA 不存在於此 code repo"
    return parsed.isoformat(timespec="seconds"), resolved.stdout.strip(), None


def ingest_pending_issues(workspace, state, round_token: str, round_number: int,
                          task_id: str, phase: str) -> None:
    """安全讀取單輪 issue signal、套用數量/長度上限，並合併進 coordinator state。"""
    pending_path = workspace.pending_issues(round_token)
    try:
        pending_text = read_regular_text(pending_path, "pending issue")
    except FileNotFoundError:
        return
    except (OSError, ValueError, UnicodeDecodeError) as e:
        # 外力若把 signal 換成 symlink/FIFO，只忽略該 round，不可跟隨到 workspace 外。
        log(f"⚠️ 忽略不安全的 pending issue：{e}")
        return

    issue_lines = [line.strip()[:ISSUE_MAX_CHARS]
                   for line in pending_text.splitlines() if line.strip()]
    if len(issue_lines) > ISSUES_MAX_PENDING:
        issue_lines = issue_lines[-ISSUES_MAX_PENDING:]
        log(f"⚠️ pending issues 超過單輪上限 {ISSUES_MAX_PENDING}，只保留最新項目")
    timestamp = datetime.now().isoformat(timespec="seconds")
    issues = state.setdefault("issues", [])
    issues.extend({"round": round_number, "where": task_id or phase,
                   "text": line, "ts": timestamp} for line in issue_lines)
    if len(issues) > ISSUES_MAX_COUNT:
        state["issues"] = issues[-ISSUES_MAX_COUNT:]
        log(f"⚠️ issues 已達保留上限 {ISSUES_MAX_COUNT}，只保留最新項目")
    pending_path.unlink()
    for line in issue_lines:
        log(f"⚠️ Agent 回報 issue｜{line}")
    if issue_lines:
        log(f"📌 Issue 累計｜目前有 {len(state.get('issues', []))} 條，"
            f"未讀 {unread_issue_count(state)} 條")


def write_run_report(repo: Path, workspace, state, *, ended_at=None,
                     run_status="completed") -> Path:
    """由最終 state 產生人類可讀報告並原子寫入 workspace。"""
    ended_at = ended_at or datetime.now().isoformat(timespec="seconds")
    report = (f"# loop-agent-lite RUN REPORT\n\n"
              f"- repo: {repo}\n- 狀態: {run_status}\n- 結束時間: {ended_at}\n"
              f"- 總輪數: {state['round']}\n- plan 版本: v{state['plan_version']}\n"
              f"- 完成任務:\n"
              + "".join(f"  - task-{entry['order']} @ {entry['sha'][:8]}(round {entry['round']})\n"
                        for entry in state["completed"])
              + f"- reset 統計: {state['task_reset_counts'] or '無'}\n"
                f"- 逐輪紀錄: {workspace.history}\n")
    report_path = workspace_file(workspace.dir / "REPORT.md", "REPORT.md")
    atomic_write_bytes(report_path, report.encode("utf-8"))
    return report_path


def reset_run_artifacts(workspace) -> None:
    """開始全新 run 時清除逐輪產物；history 僅輪替保留上一代供稽核。"""
    (workspace.dir / "pending_issues").unlink(missing_ok=True)
    (workspace.dir / "REPORT.md").unlink(missing_ok=True)
    if workspace.history.exists():
        os.replace(workspace.history, workspace.history.with_name(f"{workspace.history.name}.1"))
    for old in (workspace.dir / "logs").glob("round-*.log"):
        old.unlink(missing_ok=True)
    for old in (workspace.dir / "prompts").glob("round-*.md"):
        old.unlink(missing_ok=True)


def process_plan_round(state, workspace, round_token: str, *, tampered, changed,
                       agent_failed: bool, completion_missing: bool,
                       flag_threshold: int, allow_serial_stack: bool = False) -> str:
    """套用規劃期訊號與共識規則，必要時切換到執行期，回傳本輪事件摘要。"""
    event = ""
    if workspace.signal("called_create_plan", round_token):
        log("📨 Agent 指令｜create-plan（提交新計畫）")
        state["flag"] = 0
        pending = workspace.take_pending_plan(round_token)
        if pending is not None:
            from engine.work import (plan_has_stack, validate_plan,
                                     validate_serial_stack_opt_in)
            normalized, pending_errors = validate_plan(pending)
            if not pending_errors and plan_has_stack(normalized):
                pending_errors.append(
                    "規劃期不接受 stack；請以 frozen plan 直接從 exec 啟動")
            if not pending_errors:
                pending_errors.extend(validate_serial_stack_opt_in(
                    normalized, allow_serial_stack=allow_serial_stack))
            if pending_errors:
                state["notes"].append(
                    f"⛔ create-plan ingest 已拒絕：{pending_errors[0]}")
                event = f"⛔ create-plan ingest 已拒絕｜{pending_errors[0]}"
                log(event)
            elif plan_has_stack(state["plan"]) and normalized != state["plan"]:
                # Planner prompt v1 不擁有 stack 語意。既有人工 stack 一旦載入就不可由
                # create-plan 靜默移除或改寫；要改請離線產生新的 frozen plan 再 import。
                state["notes"].append(
                    "⚠️ 既有計畫含人工 stack，已拒絕 create-plan 改寫；請離線審核後重新 import。")
                event = "⛔ 計畫含人工 stack｜拒絕 planner 改寫"
                log(event)
            else:
                state["plan"] = normalized
                state["plan_version"] += 1
                event = f"📝 計畫已更新｜v{state['plan_version']}｜共 {len(normalized)} 條任務"
                log(event)
        else:
            event = "❌ create-plan 校驗未通過｜保留原計畫"
            log(event)
    elif tampered or changed or (agent_failed and not completion_missing):
        state["flag"] = 0
    elif workspace.signal("signal_plan_ok", round_token):
        log("📨 Agent 指令｜plan-ok（確認目前計畫）")
        if state["plan"]:
            state["flag"] += 1
            log(f"✅ 規劃共識累計｜flag={state['flag']}｜門檻 > {flag_threshold}")
        else:
            state["notes"].append("plan 仍為空,plan-ok 不計數。請先 create-plan。")
            log("⚠️ plan-ok 未計數｜目前計畫為空，請先 create-plan")
    elif not tampered:
        log("ℹ️ Agent 本輪未送出 create-plan 或 plan-ok｜repo 無異動，保留既有規劃共識")

    if state["flag"] > flag_threshold:
        from engine.work import plan_has_stack, validate_serial_stack_opt_in
        stack_errors = (["規劃期 plan 不可帶 stack；請以 frozen plan 直接從 exec 啟動"]
                        if plan_has_stack(state["plan"]) else
                        validate_serial_stack_opt_in(
                            state["plan"], allow_serial_stack=allow_serial_stack))
        if stack_errors:
            state["flag"] = 0
            state["notes"].append(f"⛔ plan→exec 已拒絕：{stack_errors[0]}")
            event = f"⛔ plan→exec 已拒絕｜{stack_errors[0]}"
            log(event)
            return event
        state["phase"] = "exec"
        state["flag"] = 0
        state["current_order"] = 1
        state["done_count"] = 0
        # 規劃期的停滯/紅燈計數不具執行期語意，不可帶入 reset 判斷。
        state["stall_rounds"] = 0
        state["red_streak"] = 0
        state["goal_changed"] = False
        state.pop("goal_previous_hash", None)
        event = f"✅ 規劃收斂(plan v{state['plan_version']},{len(state['plan'])} 條)→ 執行期"
        log(event)
    return event


def process_exec_round(state, workspace, round_token: str, *, task_id: str,
                       round_number: int, repo: Path, protected, validate_cmd,
                       args, head_before: str, pre_validate_snapshot: RepositorySnapshot,
                       tampered, changed, managed_block_reason=None,
                       agent_failed: bool, completion_missing: bool):
    """套用執行期 done/Validate/reset，並回傳 Validate 後 exact snapshot。"""
    event = ""
    head_after = pre_validate_snapshot.head
    if managed_block_reason is not None:
        assignment = state.get("assignment")
        if not isinstance(assignment, dict):
            raise RuntimeError("managed worker 缺少 assignment state")
        assignment.update({
            "status": "blocked",
            "validated_sha": None,
            "validated_round": None,
            "exit_reason": managed_block_reason,
        })
        state["done_count"] = 0
        event = f"⛔ {task_id} blocked｜{managed_block_reason}"
        log(event)
        return event, "BLOCKED", pre_validate_snapshot, False
    if state.get("runner") == parallel_worker.WORKER_RUNNER:
        task_ref_error = managed_task_ref_error(repo, state["task_ref"])
        if task_ref_error:
            mark_managed_worker_blocked(state, task_ref_error)
            event = f"⛔ {task_id} blocked｜{task_ref_error}"
            log(event)
            return event, "BLOCKED", repository_snapshot(repo), True
    done_signaled = workspace.signal("signal_done", round_token)
    create_signaled = workspace.signal("called_create_plan", round_token)
    log(f"📨 Agent 指令｜done {task_id}（回報任務完成）" if done_signaled
        else f"ℹ️ Agent 本輪未送出 done {task_id}")
    if create_signaled:
        log("📨 Agent 指令｜create-plan｜執行期計畫已凍結，將忽略此指令")

    log(f"🧪 執行驗證｜命令：{shlex.join(validate_cmd)}")
    ok, tail, validate_timed_out = run_validate(validate_cmd, repo, args.validate_timeout)
    _owner_checkpoint_reaped()
    post_validate_snapshot = repository_snapshot(repo)
    validator_changed_snapshot = post_validate_snapshot != pre_validate_snapshot
    post_validate_rejected = validator_changed_snapshot or post_validate_snapshot.dirty
    head_after = post_validate_snapshot.head
    # stall 只反映 agent 在 validator 前是否推進 HEAD。Validator 自己 commit 是拒絕的
    # side effect，不得藉此把 stall 歸零而永遠逃過 reset。
    state["stall_rounds"] = (
        0 if pre_validate_snapshot.head != head_before else state["stall_rounds"] + 1)

    if state.get("runner") == parallel_worker.WORKER_RUNNER:
        task_ref_error = managed_task_ref_error(repo, state["task_ref"])
        if task_ref_error:
            mark_managed_worker_blocked(state, task_ref_error)
            state["notes"].append(f"⛔ Validator 後 task branch invariant 失敗：{task_ref_error}")
            event = f"⛔ {task_id} blocked｜{task_ref_error}"
            log(event)
            return event, "SIDE-EFFECT", post_validate_snapshot, True

    validate_note = ("FAIL" if not ok else
                     "SIDE-EFFECT" if validator_changed_snapshot else
                     "DIRTY" if post_validate_snapshot.dirty else "PASS")
    if ok:
        state["red_streak"] = 0
        if post_validate_rejected:
            log("⚠️ 驗證命令通過，但 Validate 後 Git snapshot 不可採納")
        else:
            log("✅ 驗證通過")
            state["last_green_sha"] = head_after
    else:
        timeout_note = f"｜逾時 {args.validate_timeout:g} 秒" if validate_timed_out else ""
        log(f"❌ 驗證失敗{timeout_note}｜紅燈連續 {state['red_streak'] + 1} 輪")
        if tail:
            log(f"驗證錯誤尾段：\n{tail}")
        state["red_streak"] += 1
        state["done_count"] = 0
        state["notes"].append(
            f"❌ 上一輪結束後 `{shlex.join(validate_cmd)}` 失敗。先判斷是前一個 commit 沒做好、"
            f"還是前一個 agent 沒做完,把它修好讓驗證過了再繼續。輸出尾段:\n{fenced_block(tail)}")
    if validator_changed_snapshot:
        effects = []
        if post_validate_snapshot.head != pre_validate_snapshot.head:
            effects.append(
                f"HEAD {pre_validate_snapshot.head[:8]}→{post_validate_snapshot.head[:8]}")
        if post_validate_snapshot.head_ref != pre_validate_snapshot.head_ref:
            effects.append(
                f"HEAD ref {pre_validate_snapshot.head_ref or '(detached)'}→"
                f"{post_validate_snapshot.head_ref or '(detached)'}")
        if post_validate_snapshot.status != pre_validate_snapshot.status:
            effects.append("index/worktree 狀態改變")
        if post_validate_snapshot.dirty:
            effects.append("Validate 後仍有 tracked/untracked dirty")
        effect_text = "、".join(effects) or "Validate 後 snapshot 不乾淨"
        state["done_count"] = 0
        state["notes"].append(
            f"⚠️ Validator side effect：{effect_text}。本輪 done 不採納；下一輪須對新 snapshot 重新驗證。")
    elif post_validate_snapshot.dirty:
        state["done_count"] = 0
        state["notes"].append(
            "⚠️ Validate 後 Git snapshot 仍為 dirty；本輪 done 不採納。"
            "此 dirty 在 validator 前已存在。")
    if create_signaled:
        state["notes"].append("執行期計畫已凍結,create-plan 被忽略。任務本身有問題請在 log/commit 說明,交人處理。")
    reset_consensus = (tampered or changed or post_validate_rejected or create_signaled or
                       (agent_failed and not completion_missing))
    if reset_consensus:
        state["done_count"] = 0
        reason = ("本輪被判定作廢" if tampered else
                   "執行期誤打 create-plan，不能同時算完成票" if create_signaled else
                   "Validator 改變 Validate 後 snapshot" if validator_changed_snapshot else
                   "偵測到程式碼或 commit 變更，等待下一輪確認" if changed else
                   "Validate 後 snapshot 仍為 dirty" if post_validate_snapshot.dirty else
                   "Agent round 未正常結束")
        log(f"↩️ done 共識歸零｜{reason}")
    elif done_signaled and ok and not agent_failed:
        state["done_count"] += 1
        log(f"✅ done 共識累計｜{state['done_count']} / {args.done_threshold}")
    elif completion_missing and ok:
        log(f"ℹ️ Agent 本輪未送出 done｜repo 無異動且驗證通過，保留 done 共識 {state['done_count']}")

    if state["done_count"] >= args.done_threshold:
        if state.get("runner") == parallel_worker.WORKER_RUNNER:
            event = apply_managed_completion_gate(
                state, repo, workspace, round_number=round_number, validated_sha=head_after,
                # parallel_gate uses validate_timeout as its durable wait/cancel
                # deadline.  Give it a strict outer margin so it can win
                # pending->cancelled and emit rc11 instead of being killed a
                # fraction early and misclassified as recovery-required.
                timeout_seconds=(args.validate_timeout
                                 + max(GATE_CLIENT_GRACE_SEC,
                                       min(10.0, args.validate_timeout * 0.1))),
            )
            log(event)
            gate_snapshot = repository_snapshot(repo)
            if gate_snapshot != post_validate_snapshot:
                post_validate_snapshot = gate_snapshot
                post_validate_rejected = True
            if state["assignment"]["status"] in parallel_contract.WORKER_QUIESCENT_STATUSES:
                return event, validate_note, post_validate_snapshot, post_validate_rejected
        else:
            task_base_sha = ensure_current_task_base_sha(state, repo, head_after)
            completed_entry = {"order": state["current_order"], "sha": head_after,
                               "round": round_number}
            if task_base_sha:
                completed_entry["base_sha"] = task_base_sha
            state["completed"].append(completed_entry)
            event = f"✅ {task_id} 完成(sha {head_after[:8]},{state['done_count']} 輪共識)"
            log(event)
            state["done_count"] = 0
            next_order = next((task["order"] for task in state["plan"]
                               if task["order"] > state["current_order"]), None)
            if next_order is None:
                state["phase"] = "done"
                state["current_task_base_sha"] = None
                state["red_streak"] = 0
                state["stall_rounds"] = 0
            else:
                state["current_order"] = next_order
                # 下一個 task 的起點就是上一個 task 收斂完成的 HEAD；後續多輪不得更新。
                state["current_task_base_sha"] = head_after

    reset_reason = ""
    if state["phase"] == "exec":
        if state["red_streak"] >= args.red_limit:
            reset_reason = f"驗證連紅 {state['red_streak']} 輪"
        elif state["stall_rounds"] >= args.stall_limit:
            reset_reason = f"HEAD 停滯 {state['stall_rounds']} 輪"
    if not reset_reason:
        return event, validate_note, post_validate_snapshot, post_validate_rejected

    green = state["last_green_sha"]
    git(repo, "reset", "--hard", green)
    git(repo, "clean", "-fd")
    workspace.restore_protected(repo, protected)
    if head_sha(repo) != green or is_dirty(repo):
        reason = (f"reset 回綠點 {green[:8]} 後工作樹不符預期"
                  f"（HEAD={head_sha(repo)[:8]}、dirty={is_dirty(repo)}）")
        mark_managed_worker_blocked(state, reason)
        workspace.save_state(state)
        notify(args.notify_cmd, "reset_broken", workspace.dir.name)
        fail(f"{reason}——"
             f"綠點錨定不可信，停機交由人員確認。詳見 {workspace.history}")
    previous_order = state["current_order"]
    previous_base = state.get("current_task_base_sha")
    state["completed"] = [entry for entry in state["completed"]
                          if is_ancestor(repo, entry["sha"], green)]
    if state.get("runner") == parallel_worker.WORKER_RUNNER:
        state["completed"] = []
        state["current_order"] = state["assigned_order"]
    else:
        state["current_order"] = (
            (state["completed"][-1]["order"] + 1) if state["completed"] else
            (state["plan"][0]["order"] if state["plan"] else 1))
    if (state["current_order"] != previous_order or not previous_base or
            not is_ancestor(repo, previous_base, green)):
        state["current_task_base_sha"] = (state["completed"][-1]["sha"]
                                           if state["completed"] else green)
    key = str(state["current_order"])
    state["task_reset_counts"][key] = state["task_reset_counts"].get(key, 0) + 1
    state["done_count"] = state["red_streak"] = state["stall_rounds"] = 0
    event = (f"🔄 RESET({reset_reason})→ 回到綠點 {green[:8]},任務指標退回 "
             f"task-{state['current_order']}(該任務第 {state['task_reset_counts'][key]} 次 reset)")
    log(event)
    state["notes"].append(f"🔄 迴圈已 reset --hard 回最後綠點 {green[:8]}({reset_reason})。"
                          "之前未收斂的工作已捨棄,請照當前任務重做。")
    if args.stuck_stop and state["task_reset_counts"][key] >= args.stuck_stop_count:
        reason = (f"stuck-stop：task-{state['current_order']} 已 reset "
                  f"{state['task_reset_counts'][key]} 次")
        mark_managed_worker_blocked(state, reason)
        workspace.save_state(state)
        notify(args.notify_cmd, "stuck_stop", workspace.dir.name)
        fail(f"{reason}，停機交由人員確認。詳見 {workspace.history}")
    return event, validate_note, repository_snapshot(repo), post_validate_rejected


def runtime_config_snapshot(args, repo, agent_cmd, validate_cmd) -> dict:
    """Canonical runtime values persisted by Loop and frozen for managed workers."""
    return {
        "flag_threshold": args.flag_threshold,
        "done_threshold": args.done_threshold,
        "red_limit": args.red_limit,
        "stall_limit": args.stall_limit,
        "stuck_stop": bool(args.stuck_stop),
        "stuck_stop_count": args.stuck_stop_count,
        "round_timeout": args.round_timeout,
        "agent_backoff_max": args.agent_backoff_max,
        "validate_timeout": args.validate_timeout,
        "max_rounds": args.max_rounds,
        "pause_after_plan": bool(args.pause_after_plan),
        "allow_serial_stack": bool(args.allow_serial_stack),
        "notify_cmd": args.notify_cmd,
        "repo": str(repo),
        # Windows 只在實際 spawn 時解析 python/.cmd 等 launcher；state 保留
        # Dashboard/parent 傳入的設定值，讓 immutable resume 能逐欄比較。
        "agent_cmd": (str(args.agent_cmd).strip() if compat.IS_WINDOWS
                      else compat.join_command(agent_cmd)),
        "validate_cmd": (str(args.validate_cmd).strip() if compat.IS_WINDOWS
                         else compat.join_command(validate_cmd)),
        "goal": args.goal,
        "plan_doc": args.plan_doc,
    }


def _main_impl(argv=None):
    """Run one Loop invocation after parsing its immutable runtime options."""
    global _REPO_OWNER_STOP_CHECKPOINT
    options = parse_runtime_options(argv)
    args = options.args
    repo = options.repo
    workspace_name = options.workspace_name
    agent_cmd = options.agent_cmd
    validate_cmd = options.validate_cmd
    protected = options.protected
    plan_doc_display = options.plan_doc_display
    worker_launch = args.managed_worker_launch
    requested_runtime_config = runtime_config_snapshot(args, repo, agent_cmd, validate_cmd)

    # A managed payload is released only after its guardian has durably claimed
    # and authorized the launch reservation.  Verify that exact handoff before
    # creating a Workspace, console, or .run.lock so a cancelled/forged launch
    # has zero worker-workspace side effects.
    if worker_launch is not None:
        try:
            parallel_worker.authorize_launch(
                WORKSPACE_ROOT, workspace_name, repo, worker_launch,
                import_plan=args.import_plan,
                runtime_config=requested_runtime_config,
            )
        except parallel_contract.ParallelContractError as exc:
            fail(f"managed worker launch authority 不符：{exc}")

    # preflight 失敗也必須出現在 dashboard 的完整 console。舊流程直到所有 git
    # 檢查通過後才設定 console，導致「pid 出現後立刻停止」卻完全看不到原因。
    try:
        ws = Workspace(workspace_name)
    except ValueError as e:
        build_argument_parser().error(f"--name {e}")
    configure_console(ws.dir / "console.log")
    acquire_run_lock(ws.dir / ".run.lock", f"workspace '{ws.dir.name}'")
    state_exists = ws.state_path.exists() or ws.checkpoint_path.exists()
    persisted_managed_worker = workspace_has_managed_worker_identity(ws)
    persisted_parallel_supervisor = workspace_has_parallel_supervisor_identity(ws)
    if persisted_managed_worker and (worker_launch is None or not worker_launch.resume):
        fail("managed parallel worker 是 parent supervisor 的 readonly workspace；"
             "普通 run/reset/import/preflight 不可接手或覆寫")
    if persisted_parallel_supervisor and worker_launch is None:
        fail("parallel-supervisor base workspace 只能由 parallel resume/pause/abort 操作；"
             "普通 run/reset/import/preflight 不可接手或覆寫")
    if worker_launch is not None and not worker_launch.resume and state_exists:
        fail("managed worker 首次啟動拒絕覆寫既有 state；crash 現場只能用 "
             "--managed-worker-resume 與同一份 immutable assignment 接手")
    if args.init_only and state_exists and not args.reset_state:
        fail(f"workspace '{ws.dir.name}' 已初始化；請改用高階 CLI 的 run/restart，"
             "或明確加 --reset-state 重新初始化")
    startup_ready = ws.dir / "startup_ready.json"
    if not args.preflight_only:  # 健檢模式不得動到既有啟動 handshake 檔
        startup_ready.unlink(missing_ok=True)

    # A managed worker is already guardian-fenced inside an isolated worktree.
    # Ordinary Loop owns the common primary repository for its whole run.
    if worker_launch is None:
        try:
            _claim_repo_owner(repo, ws, repo_owner.OwnerKind.LOOP)
        except repo_owner.RepoOwnerError as exc:
            fail(f"repository owner fence blocked ordinary Loop: {exc}")

    guard_repository_baseline(
        repo, protected,
        allow_dirty=(args.resume_interrupted
                     or (worker_launch is not None and worker_launch.resume)),
    )
    if args.preflight_only:
        run_preflight_check(repo, validate_cmd, args.validate_timeout)
        return

    log(f"🚀 Loop 啟動｜workspace={ws.dir.name}｜repo={repo}")
    if args.reset_state:
        # Reset 必須是交易式的：先在記憶體建立全新 state，等所有 preflight（尤其 validate）
        # 通過後才由下方第一個 save_state 原子取代舊檔。若驗證失敗，舊 state 仍完整可讀，
        # 不會留下只有 workspace 目錄、沒有 state.json 的幽靈分頁。
        state = ws.fresh_state()
        log("🧹 準備重置既有 state｜啟動前檢查通過後才會正式清除舊進度")
    else:
        try:
            state = ws.load_state()
        except StateLoadError as e:
            fail(f"workspace state 無法復原：{e}。請由人工檢查 {ws.state_path} 與 {ws.checkpoint_path}")
        if ws.state_recovered:
            log(f"🛟 state.json 已從 last-good checkpoint 復原｜第 {state['state_recovery_count']} 次")
            state.setdefault("notes", []).append(
                "🛟 協調 state 曾損壞或遺失，本次已從 last-good checkpoint 復原；"
                "先核對目前 task 與 repo 現場再繼續。")

    # repo identity fail-closed:workspace 只按 name 載入,若既有 state 綁的是別的 repo,
    # 續跑會拿別人的 plan/completed/last_green_sha 去 reset --hard——寧可停也不帶病前進。
    # repo_binding 位於可調 config 之外；config 子命令不能把既有 plan/SHA 偷渡到另一 repo。
    # 舊 state 首次啟動仍以歷史 config.repo 遷移，成功後立即固化 repo_binding。
    bound_repo = state.get("repo_binding") or (state.get("config") or {}).get("repo")
    if bound_repo and Path(bound_repo).resolve() != repo and not (args.reset_state or args.import_plan):
        fail(f"workspace '{ws.dir.name}' 綁定的是 {bound_repo},但這次 --repo 是 {repo}。"
             f"同名 workspace 指到不同 repo 會用錯 plan/SHA——換個 --name,或加 --reset-state 重來。")

    if state.get("runner") == parallel_worker.WORKER_RUNNER and worker_launch is None:
        fail("managed parallel worker 只能由 parent supervisor 以 immutable assignment 啟動；"
             "普通 run/resume/reset 不可接手")
    if worker_launch is not None and worker_launch.resume:
        try:
            state = parallel_worker.prepare_resume_state(state, worker_launch)
        except parallel_contract.ParallelContractError as exc:
            fail(f"managed worker resume state 不符:{exc}")
        if state.get("config") != requested_runtime_config:
            # argv 是 caller 輸入；未來 supervisor authority 驗證前不可讓偽造 caller
            # 藉一個漂移參數把合法 worker 永久 terminalize。只拒絕本次啟動，state 不變。
            fail("managed worker resume runtime argv 與首次 immutable config 不一致")
    if not args.import_plan and worker_launch is None:
        from engine.work import plan_has_stack, validate_serial_stack_opt_in
        if state.get("phase") == "plan" and plan_has_stack(state.get("plan", [])):
            fail("規劃期 plan 不可帶 stack；請匯入 frozen plan 並直接從 exec 啟動")
        stack_errors = validate_serial_stack_opt_in(
            state.get("plan", []), allow_serial_stack=args.allow_serial_stack)
        if stack_errors:
            fail(stack_errors[0])

    if args.resume_interrupted:
        blocked = interrupted_resume_block_reason(repo, ws.dir, state, protected)
        if blocked:
            fail(f"Resume 不符合條件：{blocked}")
        green = state["last_green_sha"]
        # Resume 代表人員已確認目前現場可接手；以當下 protected 內容重建本輪防竄改
        # 基準，讓舊版 workspace 的缺失快照或停機後的人工調整不會在 Agent 結束時才炸掉。
        try:
            ws.snapshot_protected(repo, protected)
        except (FileNotFoundError, OSError, ValueError) as e:
            fail(f"Resume 無法建立目前受保護檔案基準：{e}")
        log(f"Resume 中斷現場｜沿用綠點 {green[:8]}，保留工作樹並略過啟動 Validate")
        state.setdefault("notes", []).append(
            f"上一輪執行途中停止；已沿用綠點 {green[:8]} 保留現場直接 Resume。"
            "先檢查並完成既有變更，再執行本輪 Validate。")

    # 選配:CLI 匯入 plan.json(重置 state,選起跑階段)——dashboard 匯入的 CLI 等價
    if args.import_plan:
        from engine.work import plan_has_stack, validate_plan, validate_serial_stack_opt_in
        try:
            plan_obj = json.loads(Path(args.import_plan).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            fail(f"--import-plan 讀取/解析失敗:{e}")
        normalized, errs = validate_plan(plan_obj)
        if not errs and plan_has_stack(normalized) and args.start_phase != "exec":
            errs.append("含 stack 的 frozen plan 必須直接從 exec 啟動")
        if not errs and worker_launch is None:
            errs.extend(validate_serial_stack_opt_in(
                normalized, allow_serial_stack=args.allow_serial_stack))
        if errs:
            fail("plan.json 校驗未過:\n- " + "\n- ".join(errs))
        state = ws.fresh_state()
        state["plan"] = normalized
        state["plan_version"] = 1
        state["phase"] = args.start_phase
        if args.start_phase == "exec":
            state["current_order"] = normalized[0]["order"]
        if worker_launch is not None:
            try:
                state = parallel_worker.initialize_state(state, worker_launch)
            except parallel_contract.ParallelContractError as exc:
                fail(f"managed worker assignment 不符合 frozen plan:{exc}")
        log(f"📝 匯入計畫｜{len(normalized)} 條｜從 {'規劃期' if args.start_phase == 'plan' else '執行期'} 開始")

    if worker_launch is not None:
        task_ref_error = managed_task_ref_error(repo, worker_launch.task_ref)
        if task_ref_error:
            if worker_launch.resume and mark_managed_worker_blocked(state, task_ref_error):
                ws.save_state(state)
            fail(f"managed worker task branch 不符:{task_ref_error}")

    fresh_start = bool(args.reset_state or args.import_plan)
    skip_startup_validate = bool(
        args.resume_interrupted or (worker_launch is not None and worker_launch.resume))
    # 一般 run 要先拍快照供舊綠點驗證；Resume 已在人工確認資格後重建當前基準；
    # reset/import 延後到 Validate 綠後，
    # 失敗的 staged 啟動就不會改掉舊 state 對應的 protected snapshot。
    if not fresh_start and not skip_startup_validate:
        ws.snapshot_protected(repo, protected)

    if not skip_startup_validate:
        establish_startup_green_anchor(
            repo, ws, state, protected, validate_cmd, args.validate_timeout)
    elif worker_launch is not None and worker_launch.resume:
        log("Managed worker Resume｜保留中斷現場並略過啟動 Validate；第一輪仍須完整驗證")

    if fresh_start:
        ws.snapshot_protected(repo, protected)

    # goal 變更偵測:停機期間人改 goal 是合法的,但既有計畫是舊 goal 收斂的——大聲提醒
    goal_path = repo_relative_path(repo, args.goal)
    goal_hash = sha256_bytes(goal_path.read_bytes())
    previous_goal_hash = state.get("goal_hash")
    if previous_goal_hash and previous_goal_hash != goal_hash and state.get("plan"):
        # 保留「現有計畫所依據的 goal」hash；goal_changed 期間再次修改仍以最初基準為準。
        if not state.get("goal_previous_hash"):
            state["goal_previous_hash"] = previous_goal_hash
        state["goal_changed"] = True
        log("⚠ goal 已變更,但計畫是舊 goal 收斂的——建議回規劃期重新收斂(dashboard ⏪);刻意如此可忽略")
        state["notes"].append("⚠ goal 內容已被人類更新,現有計畫可能過期。以新 goal 為準檢視你的任務;"
                              "若計畫明顯對不上,用 issue 回報。")
    elif not state.get("goal_changed"):
        state.pop("goal_previous_hash", None)
    state["goal_hash"] = goal_hash
    try:
        state["agent_failure_streak"] = max(0, int(state.get("agent_failure_streak", 0)))
    except (TypeError, ValueError):
        state["agent_failure_streak"] = 0
    # resume 是人類主動重啟，第一輪立即嘗試；只有再次異常才依保留的 streak 退避。
    state["agent_backoff_seconds"] = 0
    state["agent_backoff_until"] = None
    # dashboard 靠 config 做 workspace 掃描與一鍵 run(agent_cmd 會再對 config 白名單驗過才准跑)
    state["config"] = requested_runtime_config
    state["repo_binding"] = str(repo)
    # 舊 session 的停止請求不可跨重啟生效；先清掉，再公開新 pid/session_id。
    ws.stop_after_round_path.unlink(missing_ok=True)
    ws.stop_after_round_claimed_path.unlink(missing_ok=True)
    for orphan in ws.dir.glob(f".{STOP_AFTER_ROUND_FILE}.consume.*"):
        orphan.unlink(missing_ok=True)
    if args.init_only:
        # init 是完整 preflight 後的交易提交點，但不公開暫時的 init process PID；
        # 下一次高階 CLI run 會從 config 重建同一組 runtime flags 並建立真正 session。
        state["loop"] = {"pid": None}
        ws.save_state(state)
        if fresh_start:
            reset_run_artifacts(ws)
        if args.import_plan and getattr(args, "consume_import_plan", False):
            Path(args.import_plan).unlink(missing_ok=True)
        log(f"✅ Workspace 初始化完成｜name={ws.dir.name}｜phase={state['phase']}｜"
            f"state={ws.state_path}｜尚未啟動 Agent")
        return
    session_id = uuid.uuid4().hex
    state["loop"] = {"pid": os.getpid(), "session_id": session_id,
                     "started_at": datetime.now().isoformat(timespec="seconds")}
    ensure_current_task_base_sha(state, repo)

    def _mark_stopped():
        """正常退出時清 session 控制檔、凍結未完成輪時間並清除 state pid。"""
        # 若「本輪後停止」後又立刻按立即停止，或程序在輪末競態退出，不把請求留給下次 session。
        stop_after_round_requested(ws.dir, os.getpid(), session_id, consume=True)
        clear_stop_after_round_claimed(ws.dir, os.getpid(), session_id)
        if state.get("round_started_at") and not state.get("round_interrupted_at"):
            state["round_interrupted_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        state["loop"]["pid"] = None  # 正常/Ctrl-C 退出都清 pid;被 SIGKILL 留殘值,由 dashboard ps 檢查兜底
        ws.save_state(state)
    atexit.register(_mark_stopped)
    if _REPO_OWNER_FENCE is not None:
        _REPO_OWNER_STOP_CHECKPOINT = _mark_stopped
    # preflight 已通過：此刻才原子提交 reset/import 的全新 state。Agent 尚未啟動時若失敗，
    # state 仍是完整、可再次 Run 的 stopped workspace，不會是半套 import state。
    ws.save_state(state)
    if fresh_start:
        # reset/import=「從頭跑」:round 重新從 1 起算,上一輪 run 的逐輪產物不得混進新 run——
        # 舊 history 會污染輪次紀錄/sparkline/事件流,舊 prompt(round 編號較大)會蓋過新 run 的
        # prompt 投影。history 具稽核價值,輪替保留一代 .1;其餘直接清除。
        # 清理與上面的 save_state 同屬 preflight 通過後的交易提交點:啟動檢查失敗時全數保留。
        reset_run_artifacts(ws)
    if args.import_plan and getattr(args, "consume_import_plan", False):
        Path(args.import_plan).unlink(missing_ok=True)

    startup_marked = False

    def mark_startup_ready(_agent_pid):
        """第一個 Agent 成功 spawn 後只寫一次 ready marker，完成 Dashboard handshake。"""
        nonlocal startup_marked
        if startup_marked:
            return
        atomic_write_bytes(startup_ready, json.dumps({"pid": os.getpid()}).encode("utf-8"))
        startup_marked = True
        if args.resume_interrupted:
            log("Resume 啟動完成｜已略過啟動 Validate，Agent 已接手中斷現場")
        else:
            log("🟢 啟動完成｜preflight、Validate 與 Agent spawn 均成功")

    # Prompt 中的 coordinator 命令會交給另一個 CLI agent 執行；一律使用目前
    # Python executable 的絕對路徑，避免 PATH 指到另一套 Python。
    create_cmd = coordinator_command("create-plan")
    planok_cmd = coordinator_command("plan-ok")
    managed_worker = state.get("runner") == parallel_worker.WORKER_RUNNER
    issue_cmd = (coordinator_command("block", "--reason") if managed_worker
                 else coordinator_command("issue"))
    sync_integration = (parallel_contract.managed_sync_instructions(
        state["integration_ref"], issue_cmd) if managed_worker else "")
    base_env = expose_project_package({**os.environ, "LOOP_WS": str(ws.dir)})

    phase_name = "規劃期" if state["phase"] == "plan" else "執行期"
    log(f"📍 恢復進度｜階段：{phase_name}｜已完成 round {state['round']}")
    log(f"⚙️ 執行設定｜Agent：{shlex.join(agent_cmd)}｜驗證：{shlex.join(validate_cmd)}")
    log(f"⚙️ 收斂門檻｜flag>{args.flag_threshold}｜done≥{args.done_threshold}｜red-limit={args.red_limit}｜"
        f"stall-limit={args.stall_limit}  stuck-stop={'on(' + str(args.stuck_stop_count) + ')' if args.stuck_stop else 'off'}  "
        f"round-timeout={args.round_timeout:g}min  agent-backoff≤{args.agent_backoff_max:g}s  "
        f"validate-timeout={args.validate_timeout:g}s  "
        f"pause-after-plan={'on' if args.pause_after_plan else 'off'}")

    goal_text = goal_path.read_text(encoding="utf-8")

    while state["phase"] != "done":
        # 接住「上一輪落盤後、下一輪 while 開始前」送達的請求，不再 spawn 新 Agent。
        if ws.take_stop_after_round(os.getpid(), session_id):
            state["agent_backoff_seconds"] = 0
            state["agent_backoff_until"] = None
            ws.save_state(state)
            log(f"⏸ 已依要求停止｜完整保留至 round {state['round']}，未啟動下一輪")
            break
        if args.max_rounds and state["round"] >= args.max_rounds:
            log(f"⏹ 達測試用輪數上限 {args.max_rounds},停止")
            break
        state["round"] += 1
        rnd = state["round"]
        phase = state["phase"]
        notes = state["notes"]
        state["notes"] = []

        # round log 只留當前輪(使用者不要歷史 log;prompt 與 history.log 照留)
        for old in (ws.dir / "logs").glob("round-*.log"):
            if old.name != f"round-{rnd:04d}.log":
                old.unlink(missing_ok=True)

        # 每輪 spawn 前檢查:goal 是人類真相,不存在就 fail-closed 停機(輪末 revert 防線的 backstop)
        try:
            goal_path = repo_relative_path(repo, args.goal)
        except ValueError as e:
            mark_managed_worker_blocked(state, f"goal 路徑不合法：{e}")
            ws.save_state(state)
            fail(f"goal 路徑不合法：{e}")
        if not goal_path.exists():
            mark_managed_worker_blocked(
                state, f"{args.goal} 不存在（每輪啟動前檢查）")
            ws.save_state(state)
            if not managed_worker:
                notify(args.notify_cmd, "goal_missing", ws.dir.name)
            fail(f"{args.goal} 不存在（每輪啟動前檢查）——請補回並 commit 後再啟動")
        if managed_worker:
            task_ref_error = managed_task_ref_error(repo, state["task_ref"])
            if task_ref_error:
                mark_managed_worker_blocked(state, task_ref_error)
                ws.save_state(state)
                fail(f"managed worker 啟動 Agent 前 task branch 不符:{task_ref_error}")

        # 派工資訊落地(work.py 靠原子 dispatch 做 phase/task/token 當場核對)
        cur_task = next((t for t in state["plan"] if t["order"] == state["current_order"]), None)
        if phase == "exec" and cur_task is None:
            mark_managed_worker_blocked(
                state, f"執行期找不到 current_order={state['current_order']} 的任務")
            ws.save_state(state)
            fail(f"執行期找不到 current_order={state['current_order']} 的任務"
                 f"（plan {len(state['plan'])} 條）——state 不合法，停機交由人員確認")
        task_id = f"task-{state['current_order']}" if (phase == "exec" and cur_task) else ""
        ws.clear_signals()
        round_token = uuid.uuid4().hex
        ws.write_dispatch(
            phase, task_id, round_token,
            runner=state.get("runner", "loop"),
            allow_serial_stack=args.allow_serial_stack,
        )
        round_started = datetime.now().astimezone()
        state["round_started_at"] = round_started.isoformat(timespec="seconds")
        state["round_deadline_at"] = ((round_started + timedelta(seconds=args.round_timeout * 60))
                                      .isoformat(timespec="seconds")) if args.round_timeout else None
        state["round_interrupted_at"] = None
        ws.save_state(state)  # spawn 前先落地:agent 讀得到最新 round/phase,tamper 基準同步更新

        notes_text = "\n\n".join(notes) if notes else "(無)"
        if phase == "plan":
            prompt = build_prompt(HERE / "prompts" / "plan.md", {
                "GOAL": goal_text.strip(),
                "PLAN_DOC": plan_doc_display,
                "PLAN_JSON": json.dumps(state["plan"], ensure_ascii=False, indent=2) if state["plan"] else "(尚未建立)",
                "CREATE_CMD": create_cmd,
                "PLANOK_CMD": planok_cmd,
                "ISSUE_CMD": issue_cmd,
                "NOTES": notes_text,
            })
        else:
            done_cmd = coordinator_command("done", task_id)
            prompt = build_prompt(HERE / "prompts" / "exec.md", {
                "GOAL": goal_text.strip(),
                "PLAN_DOC": plan_doc_display,
                "TASK_ID": task_id,
                "TASK_TEXT": cur_task["task"],
                "TASK_REF": cur_task.get("ref") or "(無)",
                "TASK_LIST": render_task_list(state),
                "DONE_CMD": done_cmd,
                "ISSUE_CMD": issue_cmd,
                "VALIDATE_CMD": shlex.join(validate_cmd),
                "SYNC_INTEGRATION": sync_integration,
                "NOTES": notes_text,
            })
        prompt_path = ws.dir / "prompts" / f"round-{rnd:04d}.md"
        workspace_file(prompt_path, "round prompt")
        atomic_write_bytes(prompt_path, prompt.encode("utf-8"))
        # reset 後 rnd 會回到 1，但目錄可能仍有 round-0034..0038。若只按檔名排序取
        # 最後五個，剛寫好的 round-0001 會立刻被當成「最舊」刪掉，接著 spawn Agent
        # 就 FileNotFoundError。當前 prompt 永遠保留，另外最多留四份舊稽核檔。
        previous_prompts = sorted(
            (path for path in (ws.dir / "prompts").glob("round-*.md") if path != prompt_path),
            reverse=True,
        )
        for old in previous_prompts[4:]:
            old.unlink(missing_ok=True)

        head_before = head_sha(repo)
        phase_name = "規劃期" if phase == "plan" else "執行期"
        task_summary = f"｜{task_id}：{cur_task['task']}" if cur_task else ""
        log(f"🔄 第 {rnd} 輪開始｜{phase_name}{task_summary}｜flag={state['flag']}｜done={state['done_count']}")
        log(f"🤖 啟動 Agent｜命令：{shlex.join(agent_cmd)}")
        round_env = {**base_env, "LOOP_ROUND_TOKEN": round_token}
        round_env.pop("LOOP_PROMPT_FILE", None)
        rc, secs, timed_out = run_agent(agent_cmd, prompt, repo, round_env,
                                        ws.dir / "logs" / f"round-{rnd:04d}.log",
                                        args.round_timeout * 60, on_started=mark_startup_ready)
        if _REPO_OWNER_FENCE is not None:
            # Preserve the exact post-agent round before clearing the durable
            # child identity; later Git/validator children may then start.
            state["last_round_seconds"] = round(secs, 3)
            state["last_round_timed_out"] = bool(timed_out)
            ws.save_state(state)
            _owner_checkpoint_reaped()
        # Agent process 已結束的當下先保存是否送出該 phase 的完成回報；Plan 的
        # create-plan / plan-ok 是 DONE 等價訊號，Exec 則是 done。後續竄改／逾時
        # 可能清除 coordinator signals，但不能因此失去這個觀測事實。
        managed_block_reason = (
            ws.pending_block_reason(round_token, task_id)
            if managed_worker and phase == "exec" else None)
        agent_reported_done = (
            (phase == "plan" and (ws.signal("called_create_plan", round_token) or
                                  ws.signal("signal_plan_ok", round_token))) or
            (phase == "exec" and ws.signal("signal_done", round_token))
        )
        missing_done = not (agent_reported_done or managed_block_reason is not None)
        state["last_round_seconds"] = round(secs, 3)
        state["last_round_timed_out"] = bool(timed_out)
        state["round_started_at"] = None
        state["round_deadline_at"] = None
        state["round_interrupted_at"] = None
        log(f"🤖 Agent 結束｜exit code={rc}｜耗時 {secs:.0f} 秒" + "｜超時，已強制終止" * timed_out)
        if timed_out:
            state["notes"].append(f"⚠️ 上一輪 agent 超過 {args.round_timeout:g} 分鐘被強制終止,"
                                  "工作可能做到一半——工作區殘留照「收拾現場」步驟判斷。")

        # managed worker 在任何 reset/clean 前再次驗 task ref。Agent 可能在本輪切到
        # 同 SHA 的 sibling branch；若先 reset --hard，會移動錯誤 branch ref。此時只留下
        # structured blocked 現場交 supervisor/human reconcile，絕不自動 checkout/reset。
        if managed_worker:
            post_agent_task_ref_error = managed_task_ref_error(repo, state["task_ref"])
            if post_agent_task_ref_error:
                if managed_block_reason:
                    post_agent_task_ref_error += f"；agent block 回報：{managed_block_reason}"
                mark_managed_worker_blocked(state, post_agent_task_ref_error)
                state["notes"].append(
                    f"⛔ Agent 後、repo cleanup 前 task branch invariant 失敗："
                    f"{post_agent_task_ref_error}")
                ws.save_state(state)
                fail(f"managed worker task branch 不符:{post_agent_task_ref_error}")

        # ---- 協調層竄改偵測:整輪作廢(reset --hard 回輪初 sha) ----
        tampered = []
        # 受保護檔案(goal/plan-doc)被刪或被改 = 最嚴重破壞:整輪 reset + clean,該輪所有變更
        # (含 agent 已 commit 的、其他 code 改動)一併作廢——壞掉的一輪不留任何產出(#3)。
        hit = ws.protected_changed(repo, protected)
        if hit:
            git(repo, "reset", "--hard", head_before)
            git(repo, "clean", "-fd")
            tampered += hit
            state["notes"].append(f"⚠️ 上一輪動了受保護檔案 {hit}(改或刪)——整輪已 reset --hard 回 "
                                  f"{head_before[:8]}、工作區清空,該輪所有變更已捨棄。"
                                  "goal 與計畫是人類真相,agent 永遠不准動。")
            log(f"⚠️ 受保護檔案被動 {hit},整輪 reset --hard 回 {head_before[:8]}")
        # 規劃期 agent 不該碰 repo:任何 code 異動(commit 或殘留)也整輪 reset,避免規劃期
        # 誤寫的 code 混進執行基線(git checkout/clean 清不掉已 commit 的東西,#7)。
        elif phase == "plan" and (is_dirty(repo) or head_sha(repo) != head_before):
            git(repo, "reset", "--hard", head_before)
            git(repo, "clean", "-fd")
            tampered.append("規劃期 repo 異動")
            state["notes"].append(f"⚠️ 規劃期出現 repo 異動(規劃 agent 不該動 code)——已整輪 reset --hard 回 "
                                  f"{head_before[:8]}、工作區清空。")
            log(f"⚠️ 規劃期 repo 異動,整輪 reset --hard 回 {head_before[:8]}")
        if ws.state_tampered():
            tampered.append("state.json")
            ws.save_state(state)  # 用 loop 記憶中的真相覆寫回去
            state["notes"].append("⚠️ 上一輪繞過 work.py 直接改了 state.json,已還原、該輪作廢。"
                                  "計畫與進度只能透過 work.py 的命令寫入。")
            log("⚠️ 偵測到 Agent 直接修改 state.json｜已用 loop 保存的狀態還原")
        if tampered:
            # 竄改輪整輪作廢:本輪任何偷渡的 signal / pending plan 一律不採信(#2)。
            # reset --hard 只清 repo，token-scoped pending plan 在 workspace 清不到，必須顯式丟棄——
            # 否則規劃期會把「同一輪偷改 goal + create-plan」提交的髒 plan 當成真相收進去。
            ws.clear_signals()
            managed_block_reason = None
            log(f"⚠️ 本輪作廢｜偵測到不允許的變更：{', '.join(tampered)}｜相關 signal 已丟棄")

        # 不同 Agent CLI 對 exit code 的定義不一致，因此 rc 只供 log 診斷，不參與
        # round 成敗。done/plan-ok 是完成票，逾時必須作廢；但 managed block 是 fail-closed
        # terminal 訊號，不是成功票。token/task 驗證通過後，即使 agent 回報後未及退出而 timeout，
        # 也必須保留 blocked，不能再開下一輪或跑 validator/gate。
        agent_failed = (
            not (agent_reported_done or managed_block_reason is not None)
            or (timed_out and managed_block_reason is None)
        )
        if agent_failed:
            state["agent_failure_streak"] = state.get("agent_failure_streak", 0) + 1
            ws.clear_signals()
            failure_reason = (f"逾時 {args.round_timeout:g} 分鐘"
                              if timed_out else "未送出本階段完成回報")
            state["notes"].append(
                f"⚠️ 上一輪 Agent {failure_reason}(exit code={rc} 僅供診斷)，"
                "該輪 coordinator 訊號已作廢；repo 殘留交下一輪檢查。")
            log(f"⚠️ Agent round 異常｜{failure_reason}｜本輪 coordinator 訊號已全部作廢")
        else:
            if state.get("agent_failure_streak", 0):
                log(f"✅ Agent 完成回報已恢復｜連續異常 {state['agent_failure_streak']} 輪後收到有效訊號")
            state["agent_failure_streak"] = 0

        pre_validate_snapshot = repository_snapshot(repo)
        head_after = pre_validate_snapshot.head
        dirty = pre_validate_snapshot.dirty
        changed = dirty or (head_after != head_before)

        if phase == "plan":
            state["stall_rounds"] = 0 if head_after != head_before else state["stall_rounds"] + 1
            event = process_plan_round(
                state, ws, round_token, tampered=tampered, changed=changed,
                agent_failed=agent_failed, completion_missing=missing_done,
                flag_threshold=args.flag_threshold,
                allow_serial_stack=args.allow_serial_stack)
            validate_note = "-"
        else:
            event, validate_note, post_validate_snapshot, post_validate_rejected = process_exec_round(
                state, ws, round_token, task_id=task_id, round_number=rnd, repo=repo,
                protected=protected, validate_cmd=validate_cmd, args=args,
                head_before=head_before, pre_validate_snapshot=pre_validate_snapshot,
                tampered=tampered, changed=changed,
                managed_block_reason=managed_block_reason,
                agent_failed=agent_failed, completion_missing=missing_done)
            head_after = post_validate_snapshot.head
            changed = changed or post_validate_rejected

        # 規劃期可能在本輪剛切入 exec；在 state 落盤前建立 task-1 的不可變起點。
        # 一般 exec 輪也用同一 helper 為舊版缺欄位 state 做向下相容補記。
        if phase == "plan" and state["phase"] == "exec":
            # 即使是舊版 plan state，這一刻也是明確的 task-1 啟動點，可以開始新 schema 紀錄。
            state["current_task_base_sha"] = None
        ensure_current_task_base_sha(state, repo, head_after)

        ingest_pending_issues(ws, state, round_token, rnd, task_id, phase)

        will_retry = (agent_failed and state["phase"] != "done" and
                      not (args.max_rounds and state["round"] >= args.max_rounds))
        retry_delay = agent_failure_backoff(state["agent_failure_streak"], args.agent_backoff_max) \
            if will_retry else 0.0
        state["agent_backoff_seconds"] = retry_delay
        state["agent_backoff_until"] = ((datetime.now() + timedelta(seconds=retry_delay))
                                          .isoformat(timespec="seconds")) if retry_delay else None

        round_finished_at = datetime.now().isoformat(timespec="seconds")
        if missing_done:
            try:
                preserve_anomaly_log(
                    ws.dir, ws.dir / "logs" / f"round-{rnd:04d}.log",
                    round_number=rnd, phase=phase, task=task_id,
                    timestamp=round_finished_at,
                )
                log(f"🧾 已保留異常輪 log｜round {rnd}｜最多 {ANOMALY_LOG_MAX_COUNT} 份")
            except (OSError, ValueError) as e:
                # 稽核保留失敗不可反過來破壞 coordinator truth 或讓收斂迴圈停機。
                log(f"⚠️ 異常輪 log 保留失敗｜round {rnd}｜{e}")

        line = (f"{round_finished_at} round={rnd} phase={phase} "
                f"task={task_id or '-'} rc={rc} secs={secs:.3f} timeout={timed_out} changed={changed} "
                f"signal={'create' if ws.signal('called_create_plan', round_token) else 'ok' if ws.signal('signal_plan_ok', round_token) else 'done' if ws.signal('signal_done', round_token) else '-'} "
                f"done_missing={missing_done} "
                f"tamper={bool(tampered)} agent_ok={not agent_failed} "
                f"agent_failures={state['agent_failure_streak']} backoff={retry_delay:g}s validate={validate_note} "
                f"flag={state['flag']} done={state['done_count']}"
                + (f"  << {event}" if event else ""))
        append_history(ws.history, line + "\n")
        log(f"📊 第 {rnd} 輪結束｜變更={'有' if changed else '無'}｜驗證={validate_note}｜"
            f"耗時={secs:.1f}s｜flag={state['flag']}｜done={state['done_count']}"
            + (f"｜{event}" if event else ""))
        if retry_delay:
            log(f"⏳ Agent 連續未完成 {state['agent_failure_streak']} 輪｜{retry_delay:g} 秒後重試"
                f"（上限 {args.agent_backoff_max:g} 秒）")
        ws.save_state(state)
        assignment_status = ((state.get("assignment") or {}).get("status")
                             if managed_worker else None)
        if assignment_status in parallel_contract.WORKER_QUIESCENT_STATUSES:
            log(f"⏹ Managed worker 已進入 {assignment_status}｜保留 phase=exec，交回 supervisor")
            break
        stop_after_round = ws.take_stop_after_round(os.getpid(), session_id)
        if retry_delay and not stop_after_round:
            try:
                # 退避已位於兩輪之間；此時收到請求應立即停，不必等完退避，更不能再開一輪。
                deadline = time.monotonic() + retry_delay
                while True:
                    if ws.take_stop_after_round(os.getpid(), session_id):
                        stop_after_round = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.2, remaining))
            finally:
                # Ctrl-C/正常醒來都清掉 UI 的等待狀態；failure streak 留到下一輪成功才歸零。
                state["agent_backoff_seconds"] = 0
                state["agent_backoff_until"] = None
                ws.save_state(state)
        if stop_after_round:
            state["agent_backoff_seconds"] = 0
            state["agent_backoff_until"] = None
            if managed_worker:
                marker = stop_after_round_claimed_request(
                    ws.dir, os.getpid(), session_id)
                try:
                    action = (marker or {}).get("action")
                    if action == "pause":
                        generation = marker.get("pause_generation")
                        state = parallel_worker.mark_supervisor_paused(
                            state,
                            pause_generation=generation,
                            reason=(
                                f"parent supervisor requested Pause generation "
                                f"{generation}"),
                        )
                    elif action == "abort":
                        state = parallel_worker.mark_supervisor_cancelled(state)
                    else:
                        raise parallel_contract.ParallelContractError(
                            "managed stop marker action 必須是 pause/abort")
                except parallel_contract.ParallelContractError as exc:
                    mark_managed_worker_blocked(
                        state,
                        f"managed Pause marker/state authority invalid: {exc}")
            ws.save_state(state)
            log(f"⏸ 已依要求停止｜round {rnd} 已完整處理並落盤，未啟動下一輪")
            break
        # 規劃剛收斂(本輪 phase=plan、state 已切到 exec)且啟用「規劃後暫停」:
        # 停在執行期起點,不 spawn 執行輪;人工核對計畫後按「▶ 運行」直接從 exec 續跑。
        if phase == "plan" and state["phase"] == "exec" and args.pause_after_plan:
            log("⏸ 規劃已收斂｜依「規劃後暫停」設定停止，未啟動執行輪；"
                "核對計畫後按「▶ 運行」開始執行期")
            notify(args.notify_cmd, "plan_paused", ws.dir.name)
            break

    if state["phase"] == "done":
        report_path = write_run_report(repo, ws, state)
        log(f"🏁 全部任務收斂。報告:{report_path}")
        notify(args.notify_cmd, "completed", ws.dir.name)


def main(argv=None):
    """Run Loop and durably close an ordinary owner on every catchable exit."""
    terminal_reason = "loop-returned"
    try:
        return _main_impl(argv)
    except KeyboardInterrupt:
        terminal_reason = "loop-interrupted"
        raise
    except BaseException:
        terminal_reason = "loop-failed"
        raise
    finally:
        if _REPO_OWNER_FENCE is not None:
            propagating = sys.exc_info()[0] is not None
            try:
                _terminalize_repo_owner(terminal_reason)
            except BaseException as exc:
                # Never replace the original failure, but also never forge a
                # terminal marker when state/child quiescence was not proven.
                if propagating:
                    log(f"repository owner terminalization deferred: {exc}")
                else:
                    raise


if __name__ == "__main__":
    def _windows_break(*_):
        """Turn a targeted CTRL+BREAK from Dashboard into the normal stop path."""
        raise KeyboardInterrupt

    compat.register_windows_break_handler(_windows_break)
    try:
        main()
    except KeyboardInterrupt:
        log("⏸ 手動中斷｜state 已落地，重跑同一條命令即可續跑")
        sys.exit(130)
