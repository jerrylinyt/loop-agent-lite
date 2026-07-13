#!/usr/bin/env python3
"""loop-agent-lite dashboard:fleet 總覽 + 計畫表格 + console 直播 + loop launcher。

邊界:
- 讀的部分是 projection(不是真相):只讀 workspace/<name>/ 檔案,不寫任何 truth。
- 寫的部分只有「spawn / 停止 loop.py 進程」與 session-scoped 停止控制檔:agent 命令是團隊/個人設定合併後的固定選項
  (瀏覽器端只能選 index,塞不進任意命令);validate 可選預設或手寫;repo 從 config 的
  repo_roots 掃出來點選,也可手填。
- dashboard 關閉(SIGINT/SIGTERM)→ 對每個由它啟動的 loop 送 SIGINT 優雅收尾
  (loop 會存 state、殺掉自己的 agent),8 秒沒死再 SIGKILL 整個 process group。

stdlib only,綁 127.0.0.1。
"""
import argparse
import copy
import difflib
import fcntl
import functools
import hashlib
import json
import math
import mimetypes
import os
import errno
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from engine import loop as loop_mod  # 共用 Workspace/fresh_state,匯入計畫時建 state 不自己發明 schema
from engine.paths import (default_personal_config, default_workspace_root, expose_checkout_package,
                          legacy_config_path)
from engine.prompt_templates import prompt_template_bundle, prompt_template_projection
from engine.work import validate_plan  # 計畫校驗單一來源(create-plan / 匯入共用)

HERE = Path(__file__).resolve().parent
ROOT = default_workspace_root()
CONFIG_OVERRIDE = os.environ.get("LOOP_AGENT_DASHBOARD_CONFIG")
PROJECT_CONFIG_PATH = Path(os.environ.get(
    "LOOP_AGENT_DASHBOARD_PROJECT_CONFIG", HERE / "dashboard.config.shared.json"
)).expanduser().resolve()
PERSONAL_CONFIG_PATH = default_personal_config()
LEGACY_CONFIG_PATH = legacy_config_path()
CONFIG_PATH = PERSONAL_CONFIG_PATH  # 舊程式/錯誤訊息相容名稱；UI 會分別顯示團隊版與個人版
MAX_CHUNK = 512 * 1024  # 單次 tail 最多回傳量
MAX_REQUEST_BYTES = 8 * 1024 * 1024  # POST JSON 上限，避免 goal/plan 或惡意 body 吃光 dashboard 記憶體
LEGACY_STATE_MAX_BYTES = 8 * 1024 * 1024  # delete-only 分類不得為了壞 state 無界吃記憶體
HEALTH_SCHEMA_VERSION = 1
FLEET_PHASES = {"planning", "splitting", "awaiting-approval", "exec", "merging",
                "final", "stopping", "stopped", "cleaning", "done", "failed"}
FLEET_RESUME_PHASES = {"planning", "splitting", "exec", "final", "cleaning"}
DASHBOARD_INSTANCE_LOCK = ".dashboard.instance.lock"

DEFAULT_CONFIG = {
    "agent_cmds": [
        {"label": "claude", "cmd": "claude -p"},
    ],
    "validate_cmds": [
        {"label": "python unittest", "cmd": "python3 -m unittest discover -s tests -t . -q"},
        {"label": "mvn compile", "cmd": "mvn -q compile"},
        {"label": "mvn test", "cmd": "mvn -q test"},
        {"label": "react build+test+e2e", "cmd": "sh -c 'npm run build && npm test -- --run && npx playwright test'"},
    ],
    "repo_roots": ["~/IdeaProjects"],
    # GUI/IDE 啟動時通常不會載入 shell profile；用可攜式 home-relative 路徑補 CLI。
    # 支援 ~ 與 $HOME。不同電腦可在個人設定或 UI 的 CLI 管理器自行增刪。
    "extra_path_dirs": ["~/.local/bin", "~/bin"],
    "notify_cmd": "",  # 終態通知(completed/stuck_stop/goal_missing),佔位符 {status} {name};空=不通知
    "defaults": {      # launch/run 的預設值；表單可覆蓋常用參數，其他防線參數在團隊設定改
        "flag_threshold": 10, "done_threshold": 3, "round_timeout": 30, "agent_backoff_max": 60,
        "validate_timeout": 120,
        "red_limit": 20, "stall_limit": 300, "stuck_stop": False, "stuck_stop_count": 100,
        "pause_after_plan": False,
        "track_env": {}, "track_port_base": 0,
    },
}

PERSONAL_CONFIG_KEYS = {"agent_cmds", "extra_path_dirs", "repo_roots", "notify_cmd"}


def parse_numeric_setting(value, *, integer: bool, minimum: float):
    """解析 Dashboard 數值欄位；拒絕 bool、非有限值與會被 int() 靜默截斷的小數。"""
    if isinstance(value, bool):
        raise ValueError
    if integer and isinstance(value, float) and not value.is_integer():
        raise ValueError
    parsed = int(value) if integer else float(value)
    try:
        finite = math.isfinite(float(parsed))
    except OverflowError as e:
        raise ValueError from e
    if not finite or parsed < minimum:
        raise ValueError
    return parsed


def configured_path_dirs(cfg):
    """回傳使用者設定的原文與展開後路徑；明確設 [] 表示不額外補 PATH。"""
    raw_dirs = cfg.get("extra_path_dirs", DEFAULT_CONFIG["extra_path_dirs"])
    if not isinstance(raw_dirs, list):
        raw_dirs = DEFAULT_CONFIG["extra_path_dirs"]
    raw = [str(value).strip() for value in raw_dirs if str(value).strip()]
    resolved = [os.path.expanduser(os.path.expandvars(value)) for value in raw]
    return raw, resolved


def command_env(cfg):
    """建立正式 loop 與測試按鈕共用的命令環境，不依賴 IDE 是否載入 shell profile。"""
    env = dict(os.environ)
    _, extra = configured_path_dirs(cfg)
    existing = [value for value in env.get("PATH", "").split(os.pathsep) if value]
    env["PATH"] = os.pathsep.join(dict.fromkeys(extra + existing))
    return expose_checkout_package(env)


def command_test_env(cfg):
    """CLI smoke tests must not inherit the coordinator round/track identity."""
    env = command_env(cfg)
    env.pop("LOOP_WS", None)
    env.pop("LOOP_ROUND_TOKEN", None)
    for key in list(env):
        if key.startswith("LOOP_FLEET_") or key.startswith("LOOP_TRACK_"):
            env.pop(key, None)
    return env


def command_not_found(label, executable, cfg):
    """建立包含目前 PATH 設定與可操作修正方式的找不到命令錯誤。"""
    raw, resolved = configured_path_dirs(cfg)
    shown = ", ".join(raw) or "（未設定）"
    resolved_shown = os.pathsep.join(resolved) or "（無）"
    return (f"找不到 {label}：{executable}。請先在終端執行 `command -v {shlex.quote(Path(executable).name)}`，"
            f"再用 Agent CLI 管理器把所在目錄加入個人設定 {PERSONAL_CONFIG_PATH.name} 的 "
            f"`extra_path_dirs`（支援 ~ / $HOME）。"
            f"目前設定：{shown}；展開後：{resolved_shown}")


def command_error(raw, label, cfg):
    """啟動前先給可操作的 CLI 路徑錯誤，避免 child 只留下 FileNotFoundError。"""
    try:
        cmd = shlex.split(str(raw))
    except ValueError as e:
        return f"{label} 格式錯誤：{e}"
    if not cmd:
        return f"{label} 不可為空"
    if shutil.which(cmd[0], path=command_env(cfg).get("PATH")) is None:
        return command_not_found(label, cmd[0], cfg)
    return None


def safe_workspace_dir(name):
    """Dashboard 的 ROOT 也必須套用 loop 共用的名稱與 symlink 邊界。"""
    return loop_mod.workspace_path(ROOT, name)


class SafeDeleteError(RuntimeError):
    """Descriptor-safe workspace deletion error with an HTTP status projection."""

    def __init__(self, message, status=400):
        """保存可直接映射為 HTTP response 的訊息與狀態碼。"""
        super().__init__(message)
        self.status = status


def _lstat_at(directory_fd, name: str, label: str):
    """相對已開啟 directory fd 執行 lstat，避免路徑在檢查與使用之間被替換。"""
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SafeDeleteError(f"無法檢查{label}:{e}", 409) from e


@contextmanager
def directory_fd(path, label: str, *, dir_fd=None):
    """以 O_DIRECTORY|O_NOFOLLOW 開實體目錄；後續 lock/rename 都相對此 descriptor 執行。"""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise SafeDeleteError("此系統不支援安全的 O_NOFOLLOW 目錄操作，已拒絕刪除")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_DIRECTORY", 0)
    fd = None
    try:
        try:
            fd = os.open(path, flags, dir_fd=dir_fd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise SafeDeleteError(f"{label}不可為 symbolic link", 409) from e
            if e.errno == errno.ENOENT:
                raise SafeDeleteError(f"{label}不存在", 404) from e
            raise SafeDeleteError(f"無法開啟{label}:{e}", 409) from e
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise SafeDeleteError(f"{label}必須是目錄", 409)
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def exclusive_file_lock(path, label: str, *, dir_fd=None, create=True):
    """以 descriptor-relative O_NOFOLLOW regular file + flock 取得跨 dashboard 鎖。"""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise SafeDeleteError("此系統不支援安全的 O_NOFOLLOW 鎖定，已拒絕刪除")
    try:
        flags = os.O_RDWR | nofollow | os.O_NONBLOCK | (os.O_CREAT if create else 0)
        fd = os.open(path, flags, 0o600, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise SafeDeleteError(f"{label}不可為 symbolic link", 409) from e
        raise SafeDeleteError(f"無法取得{label}:{e}", 409) from e
    lock_file = os.fdopen(fd, "a+b")
    try:
        info = os.fstat(lock_file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SafeDeleteError(f"{label}必須是單一 regular file", 409)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise SafeDeleteError(f"{label}仍被持有，請稍後再試", 409) from e
        yield lock_file
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


@contextmanager
def dashboard_instance_lease():
    """Hold the root-scoped singleton lease for one writable Dashboard process."""
    stack = ExitStack()
    try:
        root = loop_mod.ensure_real_directory(ROOT, "workspace 根目錄")
        root_fd = stack.enter_context(directory_fd(root, "workspace 根目錄"))
        lock_file = stack.enter_context(exclusive_file_lock(
            DASHBOARD_INSTANCE_LOCK, "Dashboard instance 鎖", dir_fd=root_fd))
    except (SafeDeleteError, ValueError) as error:
        stack.close()
        raise RuntimeError(
            f"無法取得 workspace root {ROOT} 的 Dashboard singleton lease：{error}") from error
    try:
        yield lock_file
    finally:
        stack.close()


def _require_same_directory_entry(parent_fd, name: str, opened_fd, label: str):
    """確認 parent/name 仍指向剛開啟且持鎖的同一個 directory inode。"""
    entry = _lstat_at(parent_fd, name, label)
    opened = os.fstat(opened_fd)
    if (entry is None or stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode)
            or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)):
        raise SafeDeleteError(f"{label}在操作期間變更，已拒絕搬移", 409)


def _require_absent_entry(parent_fd, name: str, label: str):
    """確認目的名稱不存在；任何已存在類型都 fail closed，絕不覆蓋。"""
    if _lstat_at(parent_fd, name, label) is not None:
        raise SafeDeleteError(f"{label}已存在，已拒絕覆寫", 409)


def _remove_tree_at(parent_fd, name: str, label: str):
    """以 descriptor-relative、不跟隨 symlink 的方式移除目錄樹。"""
    with directory_fd(name, label, dir_fd=parent_fd) as child_fd:
        try:
            entries = list(os.scandir(child_fd))
        except OSError as e:
            raise SafeDeleteError(f"無法讀取{label}:{e}", 409) from e
        for entry in entries:
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as e:
                raise SafeDeleteError(f"無法檢查{label}內容:{e}", 409) from e
            if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                _remove_tree_at(child_fd, entry.name, f"{label}/{entry.name}")
            else:
                try:
                    os.unlink(entry.name, dir_fd=child_fd)
                except OSError as e:
                    raise SafeDeleteError(f"無法移除{label}/{entry.name}:{e}", 409) from e
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError as e:
        raise SafeDeleteError(f"無法移除{label}:{e}", 409) from e


@contextmanager
def locked_workspace_entry(name: str, lock_names=(".run.lock",), *,
                           expected_inode=None, create_locks=True):
    """Bind a workspace root entry and all relative writer locks to one directory fd."""
    with directory_fd(ROOT, "workspace 根目錄") as root_fd:
        with directory_fd(name, f"workspace {name}", dir_fd=root_fd) as workspace_fd:
            info = os.fstat(workspace_fd)
            inode = (info.st_dev, info.st_ino)
            if expected_inode is not None and inode != tuple(expected_inode):
                raise SafeDeleteError(f"workspace {name} inode 與已驗證身分不符", 409)
            with ExitStack() as locks:
                for lock_name in lock_names:
                    locks.enter_context(exclusive_file_lock(
                        lock_name, f"{name} {lock_name} writer 鎖", dir_fd=workspace_fd,
                        create=create_locks))
                _require_same_directory_entry(root_fd, name, workspace_fd, f"workspace {name}")
                yield {"root_fd": root_fd, "workspace_fd": workspace_fd, "name": name,
                       "inode": inode, "prelock_stat": info, "lock_names": tuple(lock_names)}


def _remove_open_tree(parent_fd: int, name: str, directory_fd_value: int, label: str,
                      *, logical_name: str):
    """Recursively remove the already-open directory inode without pathname reopen."""
    opened = os.fstat(directory_fd_value)
    current = _lstat_at(parent_fd, name, label)
    if (current is None or not stat.S_ISDIR(current.st_mode) or
            (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
        raise SafeDeleteError(f"{label}在刪除前已替換", 409)
    for entry in list(os.scandir(directory_fd_value)):
        info = os.stat(entry.name, dir_fd=directory_fd_value, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=directory_fd_value)
            try:
                _remove_open_tree(directory_fd_value, entry.name, child_fd,
                                  f"{label}/{entry.name}", logical_name=logical_name)
            finally:
                os.close(child_fd)
        else:
            os.unlink(entry.name, dir_fd=directory_fd_value)
            _delete_fault_hook("after-unlink", logical_name)
    _require_same_directory_entry(parent_fd, name, directory_fd_value, label)
    _delete_fault_hook("before-rmdir", logical_name)
    os.rmdir(name, dir_fd=parent_fd)


def _remove_locked_workspace(handle, tombstone: str):
    root_fd = handle["root_fd"]
    workspace_fd = handle["workspace_fd"]
    name = handle["name"]
    _require_same_directory_entry(root_fd, name, workspace_fd, f"workspace {name}")
    _require_absent_entry(root_fd, tombstone, "刪除暫存項目")
    os.rename(name, tombstone, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    _require_same_directory_entry(root_fd, tombstone, workspace_fd,
                                  f"刪除暫存項目 {tombstone}")
    _delete_fault_hook("after-rename", name)
    _remove_open_tree(root_fd, tombstone, workspace_fd, f"刪除暫存項目 {tombstone}",
                      logical_name=name)


def _delete_race_hook(_stage: str, _name: str):
    """Deterministic no-op hook patched by descriptor replacement-race tests."""


def _delete_fault_hook(_stage: str, _name: str):
    """Deterministic no-op hook patched by retry/fault-injection tests."""


def _delete_worktree_hook(_stage: str, _path: str):
    """Deterministic no-op hook patched by worktree retry tests."""


def _delete_journal_path(name: str) -> Path:
    digest = hashlib.sha256(name.encode()).hexdigest()[:24]
    return ROOT / getattr(loop_mod, "WORKSPACE_OPS_DIR", ".ops") / f"delete-{digest}.json"


DELETE_GENERATION_MARKER = ".delete-generation"


def _read_delete_generation_marker(directory_fd_value: int):
    """Read a delete-only identity without following or accepting linked files."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise SafeDeleteError("此系統不支援安全的 delete generation marker", 409)
    try:
        fd = os.open(DELETE_GENERATION_MARKER, os.O_RDONLY | nofollow,
                     dir_fd=directory_fd_value)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SafeDeleteError(f"delete generation marker 無法開啟:{error}", 409) from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64:
            raise SafeDeleteError("delete generation marker 不是 bounded 單一 regular file", 409)
        with os.fdopen(fd, "rb", closefd=True) as stream:
            fd = None
            raw = stream.read(65)
    finally:
        if fd is not None:
            os.close(fd)
    try:
        generation = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise SafeDeleteError("delete generation marker 不是 ASCII", 409) from error
    if re.fullmatch(r"[0-9a-f]{32}", generation) is None:
        raise SafeDeleteError("delete generation marker identity 不合法", 409)
    return generation


def _ensure_delete_generation_marker(directory_fd_value: int):
    """Create once and durably reuse the legacy delete transaction identity."""
    existing = _read_delete_generation_marker(directory_fd_value)
    if existing is not None:
        # The previous request may have failed exactly at the directory fsync.
        # Do not publish a journal until the marker directory entry is durable.
        os.fsync(directory_fd_value)
        return existing
    generation = uuid.uuid4().hex
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise SafeDeleteError("此系統不支援安全的 delete generation marker", 409)
    try:
        fd = os.open(DELETE_GENERATION_MARKER,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                     0o600, dir_fd=directory_fd_value)
    except FileExistsError:
        # A crash or concurrent creator may have committed the marker first.
        existing = _read_delete_generation_marker(directory_fd_value)
        os.fsync(directory_fd_value)
        return existing
    except OSError as error:
        raise SafeDeleteError(f"delete generation marker 無法建立:{error}", 409) from error
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = None
            stream.write((generation + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory_fd_value)
    finally:
        if fd is not None:
            os.close(fd)
    return generation


def _journal_generation_from_fd(directory_fd_value: int, entry):
    """Return the exact durable generation selected by this journal entry."""
    if entry.get("generation_source", "state") == "delete-marker":
        return _read_delete_generation_marker(directory_fd_value)
    state = _read_state_from_directory_fd(directory_fd_value)
    return state.get("workspace_generation")


def _delete_journal(name: str, kind: str, run_id, handles, *, git_identity=None):
    entries = []
    for handle in handles:
        entry_name = handle["name"]
        generation_source = handle.get("delete_generation_source", "state")
        if generation_source == "delete-marker":
            generation = _ensure_delete_generation_marker(handle["workspace_fd"])
        else:
            state = _read_state_from_directory_fd(handle["workspace_fd"])
            generation = state.get("workspace_generation")
        if re.fullmatch(r"[0-9a-f]{32}", str(generation or "")) is None:
            raise SafeDeleteError(f"workspace {entry_name} generation 不合法", 409)
        identity = f"{name}\0{run_id or ''}\0{entry_name}"
        entries.append({"name": entry_name,
                        "tombstone": ".delete-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
                        "dev": handle["inode"][0], "ino": handle["inode"][1],
                        "generation": generation,
                        "generation_source": generation_source,
                        "lock_names": list(handle.get("lock_names") or ())})
    journal = {"schema_version": 1, "request_name": name, "kind": kind,
               "run_id": run_id, "entries": entries}
    if git_identity is not None:
        git_identity = dict(git_identity)
        present_entries = {entry["name"] for entry in entries if entry["name"] != name}
        git_identity["children"] = [
            {"track": worktree["track"], "safe_name": worktree["safe_name"],
             "name": f"{name}--{worktree['safe_name']}",
             "present": f"{name}--{worktree['safe_name']}" in present_entries}
            for worktree in git_identity.get("worktrees") or []
        ]
        journal["git"] = git_identity
    return journal


def _validate_delete_journal(journal, name: str):
    if (not isinstance(journal, dict) or journal.get("schema_version") != 1 or
            journal.get("request_name") != name or
            journal.get("kind") not in {"standalone", "fleet-group"} or
            not isinstance(journal.get("entries"), list) or not journal["entries"]):
        raise SafeDeleteError("delete journal 結構或 request identity 不符", 409)
    kind = journal["kind"]
    run_id = journal.get("run_id")
    if len(journal["entries"]) > 9:
        raise SafeDeleteError("delete journal entries 超過 bounded 上限", 409)
    if kind == "standalone":
        if run_id is not None or len(journal["entries"]) != 1 or journal.get("git") is not None:
            raise SafeDeleteError("standalone delete journal identity 不合法", 409)
    elif re.fullmatch(r"[0-9a-f]{32}", str(run_id or "")) is None:
        raise SafeDeleteError("fleet-group delete journal run_id 不合法", 409)
    seen = set()
    parent_entries = 0
    for entry in journal["entries"]:
        generation_source = (entry.get("generation_source", "state")
                             if isinstance(entry, dict) else None)
        if (not isinstance(entry, dict) or not loop_mod.valid_workspace_name(entry.get("name")) or
                not isinstance(entry.get("tombstone"), str) or
                not entry["tombstone"].startswith(".delete-") or
                Path(entry["tombstone"]).name != entry["tombstone"] or
                not isinstance(entry.get("dev"), int) or not isinstance(entry.get("ino"), int) or
                entry["dev"] < 0 or entry["ino"] < 1 or
                re.fullmatch(r"[0-9a-f]{32}", str(entry.get("generation") or "")) is None or
                generation_source not in {"state", "delete-marker"} or
                not isinstance(entry.get("lock_names"), list) or
                any(lock not in {".run.lock", ".fleet.run.lock"}
                    for lock in entry["lock_names"]) or entry["name"] in seen):
            raise SafeDeleteError("delete journal entry 不合法", 409)
        entry["generation_source"] = generation_source
        seen.add(entry["name"])
        identity = f"{name}\0{run_id or ''}\0{entry['name']}"
        expected_tombstone = ".delete-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        if entry["tombstone"] != expected_tombstone:
            raise SafeDeleteError("delete journal tombstone identity 不符", 409)
        if kind == "standalone":
            if entry["name"] != name or entry["lock_names"] != [".run.lock"]:
                raise SafeDeleteError("standalone delete journal entry 不符", 409)
        elif entry["name"] == name:
            parent_entries += 1
            if (generation_source != "state" or
                    entry["lock_names"] != [".fleet.run.lock", ".run.lock"]):
                raise SafeDeleteError("fleet parent delete journal locks 不符", 409)
        elif (not entry["name"].startswith(name + "--") or
              generation_source != "state" or entry["lock_names"] != [".run.lock"]):
            raise SafeDeleteError("fleet child delete journal identity/locks 不符", 409)
    if kind == "fleet-group":
        if parent_entries != 1:
            raise SafeDeleteError("fleet-group delete journal parent entry 不唯一", 409)
        git_identity = journal.get("git")
        if (not isinstance(git_identity, dict) or
                not isinstance(git_identity.get("repo"), str) or
                not isinstance(git_identity.get("common_dir"), str) or
                not isinstance(git_identity.get("integration_ref"), str) or
                not git_identity["integration_ref"].startswith("refs/heads/") or
                not isinstance(git_identity.get("worktrees"), list) or
                not isinstance(git_identity.get("children"), list)):
            raise SafeDeleteError("fleet-group delete journal Git identity 不合法", 409)
        if len(git_identity["worktrees"]) > 8 or len(git_identity["children"]) > 8:
            raise SafeDeleteError("fleet-group delete journal tracks 超過 bounded 上限", 409)
        worktree_names = set()
        worktree_identities = set()
        parent = (ROOT / name).resolve()
        for worktree in git_identity["worktrees"]:
            if (not isinstance(worktree, dict) or
                    not isinstance(worktree.get("track"), str) or
                    not isinstance(worktree.get("safe_name"), str) or
                    (worktree["track"] != "@final" and
                     re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,23}", worktree["track"]) is None) or
                    loop_mod.fleet_track_safe_name(worktree["track"]) != worktree["safe_name"] or
                    Path(worktree["safe_name"]).name != worktree["safe_name"] or
                    worktree["safe_name"] in {".", ".."} or
                    worktree.get("branch_ref") !=
                    f"refs/heads/loop/{run_id}/{worktree['safe_name']}" or
                    re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?",
                                 str(worktree.get("branch_tip", ""))) is None):
                raise SafeDeleteError("fleet-group worktree journal identity 不合法", 409)
            expected_path = (parent / "worktrees" / worktree["safe_name"]).resolve()
            if worktree.get("path") != str(expected_path) or worktree["track"] in worktree_names:
                raise SafeDeleteError("fleet-group worktree path/track 重複或不符", 409)
            worktree_names.add(worktree["track"])
            worktree_identities.add((worktree["track"], worktree["safe_name"]))
        child_identities = set()
        present_children = set()
        for child in git_identity["children"]:
            if (not isinstance(child, dict) or not isinstance(child.get("track"), str) or
                    not isinstance(child.get("safe_name"), str) or
                    not isinstance(child.get("name"), str) or
                    not isinstance(child.get("present"), bool) or
                    child["name"] != f"{name}--{child['safe_name']}" or
                    (child["track"], child["safe_name"]) not in worktree_identities or
                    (child["track"], child["safe_name"]) in child_identities):
                raise SafeDeleteError("fleet-group child journal identity 不合法", 409)
            child_identities.add((child["track"], child["safe_name"]))
            if child["present"]:
                present_children.add(child["name"])
        if child_identities != worktree_identities:
            raise SafeDeleteError("fleet-group child/track journal 清單不一致", 409)
        entry_children = seen - {name}
        if entry_children != present_children:
            raise SafeDeleteError("fleet-group child entries 與 track journal 不一致", 409)
    return journal


def _write_delete_journal(journal):
    path = _delete_journal_path(journal["request_name"])
    loop_mod.ensure_real_directory(path.parent, "delete journal 目錄")
    loop_mod.atomic_write_bytes(path, json.dumps(journal, ensure_ascii=False, indent=2).encode())
    with directory_fd(path.parent, "delete journal 目錄") as parent_fd:
        os.fsync(parent_fd)


def _load_delete_journal(name: str):
    path = _delete_journal_path(name)
    try:
        parent_info = path.parent.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise SafeDeleteError("delete journal 目錄不是實體目錄", 409)
    try:
        data = loop_mod.read_regular_bytes(path, "delete journal")
    except FileNotFoundError:
        return None
    try:
        journal = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SafeDeleteError(f"delete journal JSON 不合法:{error}", 409) from error
    return _validate_delete_journal(journal, name)


def _clear_delete_journal(name: str):
    path = _delete_journal_path(name)
    try:
        path = loop_mod.workspace_file(path, "delete journal")
    except FileNotFoundError:
        return
    _delete_fault_hook("before-journal-clear", name)
    path.unlink()


def _resume_delete_journal_git(journal):
    if journal.get("kind") != "fleet-group":
        return
    identity = journal["git"]
    repo = Path(identity["repo"]).expanduser().resolve()
    if Path(_git_delete_check(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise SafeDeleteError("delete journal integration repo 身分不符", 409)
    common_dir = _git_directory(repo, "--git-common-dir")
    if str(common_dir) != identity["common_dir"]:
        raise SafeDeleteError("delete journal common-dir 身分不符", 409)
    integration_ref = identity["integration_ref"]
    lock_key = hashlib.sha256(integration_ref.encode()).hexdigest()[:16]
    with ExitStack() as git_locks:
        git_locks.enter_context(exclusive_file_lock(
            common_dir / f"loop-fleet-{lock_key}.lock",
            f"integration ref {integration_ref} 鎖"))
        inventory = _worktree_inventory(repo)
        for worktree in identity["worktrees"]:
            path = Path(worktree["path"])
            branch_ref = worktree["branch_ref"]
            branch_tip = _git_delete_check(repo, "rev-parse", branch_ref)
            if branch_tip != worktree["branch_tip"]:
                raise SafeDeleteError(f"track {worktree['track']} journal branch tip 已變更", 409)
            registered = inventory.get(path)
            try:
                path_info = path.lstat()
            except FileNotFoundError:
                path_info = None
            if path_info is not None:
                if registered is None or registered.get("branch") != branch_ref:
                    # The old registration is gone and this path now belongs to a
                    # different/new run (or is unrelated). Never touch it.
                    continue
                if (stat.S_ISLNK(path_info.st_mode) or not stat.S_ISDIR(path_info.st_mode) or
                        _git_delete_check(path, "symbolic-ref", "-q", "HEAD") != branch_ref or
                        _git_delete_check(path, "rev-parse", "HEAD") != branch_tip or
                        _git_directory(path, "--git-common-dir") != common_dir or
                        _git_delete_check(path, "status", "--porcelain")):
                    raise SafeDeleteError(
                        f"track {worktree['track']} journal worktree 身分/cleanliness 不符", 409)
                child_git_dir = _git_directory(path, "--git-dir")
                git_locks.enter_context(exclusive_file_lock(
                    child_git_dir / "loop-agent-lite.run.lock",
                    f"track {worktree['track']} writer 鎖"))
                removed = subprocess.run(
                    ["git", "-C", str(repo), "worktree", "remove", str(path)],
                    capture_output=True, text=True)
                if removed.returncode:
                    raise SafeDeleteError(
                        f"移除 worktree 失敗:{(removed.stderr or removed.stdout)[-400:]}", 409)
                _delete_worktree_hook("after-remove", str(path))
                inventory.pop(path, None)
            elif registered is not None:
                pruned = subprocess.run(["git", "-C", str(repo), "worktree", "prune"],
                                        capture_output=True, text=True)
                if pruned.returncode:
                    raise SafeDeleteError(
                        f"git worktree prune 失敗:{(pruned.stderr or pruned.stdout)[-400:]}", 409)
                inventory = _worktree_inventory(repo)
                if path in inventory:
                    raise SafeDeleteError(
                        f"track {worktree['track']} missing worktree registration 無法 prune", 409)
        pruned = subprocess.run(["git", "-C", str(repo), "worktree", "prune"],
                                capture_output=True, text=True)
        if pruned.returncode:
            raise SafeDeleteError(f"git worktree prune 失敗:{(pruned.stderr or pruned.stdout)[-400:]}", 409)


def _resume_delete_journal(journal):
    """Finish only source/tombstone inodes proven by the durable journal."""
    name = journal["request_name"]
    entries = journal["entries"]
    with ExitStack() as operations:
        for entry_name in sorted({entry["name"] for entry in entries}):
            operations.enter_context(
                loop_mod.workspace_operation_lock(ROOT, entry_name, blocking=False))
        entry_modes = {}
        source_handles = {}
        preserved_replacements = set()
        for entry in entries:
            source = ROOT / entry["name"]
            tombstone = ROOT / entry["tombstone"]
            try:
                source_info = source.lstat()
            except FileNotFoundError:
                source_info = None
            try:
                tombstone_info = tombstone.lstat()
            except FileNotFoundError:
                tombstone_info = None
            expected = (entry["dev"], entry["ino"])
            source_inode = ((source_info.st_dev, source_info.st_ino)
                            if source_info is not None else None)
            tombstone_inode = ((tombstone_info.st_dev, tombstone_info.st_ino)
                               if tombstone_info is not None else None)
            if tombstone_inode is not None and tombstone_inode != expected:
                raise SafeDeleteError(
                    f"delete journal {entry['name']} tombstone inode 不符", 409)
            if source_inode == expected and tombstone_inode is not None:
                raise SafeDeleteError(
                    f"delete journal {entry['name']} source/tombstone 同時指向舊身分", 409)
            if source_inode is not None:
                if source_inode != expected:
                    # A new same-name workspace may legitimately coexist with the old
                    # deterministic tombstone. Preserve it and finish only the old inode.
                    entry_modes[entry["name"]] = (
                        "tombstone" if tombstone_inode == expected else "completed-replaced")
                    preserved_replacements.add(entry["name"])
                    continue
                try:
                    with locked_workspace_entry(
                            entry["name"], (), expected_inode=expected,
                            create_locks=False) as identity_handle:
                        observed_generation = _journal_generation_from_fd(
                            identity_handle["workspace_fd"], entry)
                except SafeDeleteError:
                    # If the old identity cannot be proven, this name is treated as a
                    # replacement. A fresh confirmation may create a new transaction.
                    observed_generation = None
                if observed_generation != entry["generation"]:
                    entry_modes[entry["name"]] = "completed-replaced"
                    preserved_replacements.add(entry["name"])
                else:
                    source_handles[entry["name"]] = operations.enter_context(
                        locked_workspace_entry(
                            entry["name"], tuple(entry["lock_names"]), expected_inode=expected,
                            create_locks=False))
                    locked_generation = _journal_generation_from_fd(
                        source_handles[entry["name"]]["workspace_fd"], entry)
                    if locked_generation != entry["generation"]:
                        raise SafeDeleteError(
                            f"delete journal {entry['name']} generation 在 writer lock 前已更新", 409)
                    entry_modes[entry["name"]] = "source"
            elif tombstone_inode is not None:
                entry_modes[entry["name"]] = "tombstone"
            else:
                entry_modes[entry["name"]] = "completed"
            # neither exists means this entry completed before the previous failure.
        _resume_delete_journal_git(journal)
        for entry in entries:
            mode = entry_modes[entry["name"]]
            if mode == "source":
                _remove_locked_workspace(source_handles[entry["name"]], entry["tombstone"])
            elif mode == "tombstone":
                with locked_workspace_entry(
                        entry["tombstone"], (), expected_inode=(entry["dev"], entry["ino"]),
                        create_locks=False) as handle:
                    _remove_open_tree(handle["root_fd"], entry["tombstone"],
                                      handle["workspace_fd"],
                                      f"刪除暫存項目 {entry['tombstone']}",
                                      logical_name=entry["name"])
    _clear_delete_journal(name)
    return preserved_replacements


def _safe_remove_workspace_tree(name: str, *, writer_lock_held=False,
                                generation_source="state", expected_generation=None,
                                expected_inode=None):
    """Revalidate and delete one exact writer-locked workspace descriptor."""
    lock_names = () if writer_lock_held else (".run.lock",)
    with locked_workspace_entry(name, lock_names, expected_inode=expected_inode) as handle:
        if generation_source == "delete-marker":
            locked_state = _legacy_workspace_state_from_fd(handle["workspace_fd"])
            if locked_state is None:
                raise SafeDeleteError(
                    f"{name} 在 writer lock 內不是可刪除的 legacy workspace", 409)
            # `.run.lock` may have been created after the descriptor snapshot; compare the
            # browser identity against the stat captured before that internal lock mutation.
            locked_info = handle["prelock_stat"]
            if (_legacy_snapshot_generation(locked_info, locked_state) !=
                    expected_generation):
                raise SafeDeleteError(
                    f"{name} legacy generation 在確認後已更新", 409)
        else:
            locked_state = _read_state_from_directory_fd(handle["workspace_fd"])
            if (locked_state.get("workspace_kind") != "standalone" or
                    locked_state.get("workspace_generation") != expected_generation):
                raise SafeDeleteError(
                    f"{name} generation/kind 在刪除 preflight 後已更新", 409)
        if ws_running(name, locked_state):
            raise SafeDeleteError(f"{name} 在 writer lock 內仍在執行，不能刪除", 409)
        handle["delete_generation_source"] = generation_source
        journal = _delete_journal(name, "standalone", None, [handle])
        if generation_source == "delete-marker":
            _delete_fault_hook("after-delete-generation", name)
        _write_delete_journal(journal)
        _delete_race_hook("standalone-before-delete", name)
        _remove_locked_workspace(handle, journal["entries"][0]["tombstone"])
        _clear_delete_journal(name)


def _git_delete_check(repo: Path, *args: str) -> str:
    """Run one read-only Git identity check and normalize failures as safe-delete conflicts."""
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode:
        raise SafeDeleteError(
            f"Git 身分檢查失敗 ({shlex.join(args)}):{(result.stderr or result.stdout)[-400:]}", 409)
    return result.stdout.strip()


def _git_directory(repo: Path, argument: str) -> Path:
    """Resolve --git-dir/--git-common-dir output against the worktree that produced it."""
    raw = _git_delete_check(repo, "rev-parse", argument)
    path = Path(raw)
    return (path if path.is_absolute() else repo / path).resolve()


def _worktree_inventory(repo: Path):
    """Return canonical worktree paths with their registered full branch refs."""
    raw = _git_delete_check(repo, "worktree", "list", "--porcelain")
    inventory = {}
    current = None
    for line in raw.splitlines() + [""]:
        if line.startswith("worktree "):
            current = {"path": Path(line.removeprefix("worktree ")).resolve(), "branch": None}
        elif current is not None and line.startswith("branch "):
            current["branch"] = line.removeprefix("branch ")
        elif current is not None and not line:
            inventory[current["path"]] = current
            current = None
    return inventory


MAX_FINISHED_JOBS = 50  # 長跑 dashboard 只保留最近已結束 job；活躍 job 不受限制
JOBS = {}          # name -> Job(由本 dashboard 啟動的 loop)
JOBS_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()


def _prune_finished_jobs_locked(max_finished=MAX_FINISHED_JOBS):
    """呼叫端須持 JOBS_LOCK；保留活躍 job 與最近 N 個已結束 job。"""
    finished = [name for name, job in JOBS.items() if not job.alive()]
    excess = max(0, len(finished) - max(0, int(max_finished)))
    for name in finished[:excess]:
        JOBS.pop(name, None)


def prune_finished_jobs(max_finished=MAX_FINISHED_JOBS):
    """限制 dashboard 進程內 job tail 記憶體；不影響 workspace state 或可重跑性。"""
    with JOBS_LOCK:
        _prune_finished_jobs_locked(max_finished)

# per-workspace state lock:ThreadingHTTPServer 下,兩個並發 POST(雙擊/多分頁/操作重疊)對同一
# workspace 做 read-modify-write 會 lost update,且共用 state.json 的原子寫 tmp——用每個 name 一把鎖
# 把「讀 state → 改 → 寫回」序列化(#3)。粒度到 name,不同 workspace 的操作互不阻塞。
_STATE_LOCKS = {}
_STATE_LOCKS_GUARD = threading.Lock()


def _state_lock(name):
    """取得每 workspace 共用的 process-local mutation lock，不同 workspace 可並行。"""
    with _STATE_LOCKS_GUARD:
        lk = _STATE_LOCKS.get(name)
        if lk is None:
            lk = _STATE_LOCKS[name] = threading.Lock()
        return lk


def with_state_lock(fn=None, *, repo_fallback=False):
    """裝飾 api_*(self, body):以 workspace name 鎖序列化整段 read/check/mutate/spawn。

    launch 允許 name 留空,此時用 repo 目錄名作 lock key,確保它和同 workspace 的 run/edit/phase
    取得同一把鎖。支援 @with_state_lock 與 @with_state_lock(repo_fallback=True) 兩種寫法。
    """
    def decorate(func):
        """建立保留原函式 metadata 的鎖定 decorator。"""
        @functools.wraps(func)
        def wrapper(self, body):
            """從 request 推導鎖 key，涵蓋 launch 名稱留空時的 repo basename。"""
            name = str(body.get("name") or "").strip()
            if not name and repo_fallback:
                name = Path(str(body.get("repo") or "")).expanduser().name
            with _state_lock(name):
                return func(self, body)
        return wrapper
    return decorate(fn) if fn is not None else decorate


def with_workspace_operation_lock(func):
    """Hold the cross-process name lock across every launch check and side effect."""
    @functools.wraps(func)
    def wrapper(self, body):
        name = str(body.get("name") or "").strip()
        if not name:
            name = Path(str(body.get("repo") or "")).expanduser().name
        if not loop_mod.valid_workspace_name(name):
            return func(self, body)
        try:
            with loop_mod.workspace_operation_lock(ROOT, name, blocking=False):
                return func(self, body)
        except loop_mod.WorkspaceOperationLockError as error:
            self._err(str(error), 409)
            return None
    return wrapper


class DashboardServer(ThreadingHTTPServer):
    """SSE 連線是長存 thread；設為 daemon 才不會阻擋 dashboard 優雅關閉。"""
    daemon_threads = True
    allow_reuse_address = True


class Job:
    """Dashboard 啟動之 loop process 與 bounded 輸出尾段的生命週期封裝。"""

    def __init__(self, name, repo, popen, kind="loop", cleanup_paths=()):
        """保存 process 並啟動 daemon reader，避免 stdout pipe 塞滿阻塞 child。"""
        self.name = name
        self.repo = repo
        self.popen = popen
        self.kind = kind
        self.cleanup_paths = tuple(Path(path) for path in cleanup_paths if path)
        self.last_stop_error = None
        self.last_stop_code = 500
        self.out = deque(maxlen=200)
        t = threading.Thread(target=self._reader, daemon=True)
        self.reader = t
        t.start()

    def _reader(self):
        """持續讀取 stdout 到固定長度 deque，process 結束後關閉 pipe。"""
        try:
            for line in self.popen.stdout:
                self.out.append(line.rstrip("\n"))
        finally:
            self.popen.stdout.close()
            for path in self.cleanup_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def alive(self):
        """以 poll 判斷 child 是否仍在執行。"""
        return self.popen.poll() is None

    def info(self):
        """輸出前端 jobs 分頁使用的安全摘要與最近八行。"""
        result = {"name": self.name, "repo": self.repo, "pid": self.popen.pid,
                  "kind": self.kind, "alive": self.alive(), "rc": self.popen.returncode,
                  "tail": "\n".join(list(self.out)[-8:])}
        if self.kind == "fleet":
            fleet = read_parallel_run(self.name)
            # 只有同一個仍登記中的 process 才能把 run identity 給停止按鈕；同名重建或
            # stale job 不得取得新 run-id。
            if (not fleet.get("read_error") and
                    int((fleet.get("loop") or {}).get("pid") or 0) == self.popen.pid):
                result["run_id"] = fleet.get("run_id")
        else:
            state, error = read_state(self.name, repair=False)
            # Do not lend a replacement workspace's generation to a stale Job row.
            if (not error and state and state.get("workspace_kind") == "standalone" and
                    int((state.get("loop") or {}).get("pid") or 0) == self.popen.pid):
                result["workspace_generation"] = state.get("workspace_generation")
        return result

    def stop(self, wait=False):
        """Stop the coordinator and prove its separately-sessioned runtime group is empty."""
        self.last_stop_error = None
        self.last_stop_code = 500
        state, error = read_state(self.name, repair=False)
        if error or not state or int((state.get("loop") or {}).get("pid") or 0) != self.popen.pid:
            state = None
        try:
            frozen = freeze_workspace_stop_identity(
                self.name, self.repo, self.popen.pid,
                state=state if self.kind == "loop" and state and
                state.get("workspace_kind") == "standalone" else None,
                require_coordinator_marker=False)
        except RuntimeStopIdentityError as identity_error:
            self.last_stop_error = str(identity_error)
            self.last_stop_code = identity_error.code
            return False

        markerless_group = None
        if frozen is None and self.alive():
            try:
                markerless_group = freeze_job_process_group(self.popen)
            except RuntimeStopIdentityError as identity_error:
                self.last_stop_error = str(identity_error)
                self.last_stop_code = identity_error.code
                return False
        if self.alive():
            if markerless_group is not None:
                ok, group_error, group_code = signal_markerless_job_group(markerless_group)
                if not ok:
                    self.last_stop_error = group_error
                    self.last_stop_code = group_code
                    return False
            else:
                try:
                    self.popen.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass

        def _finish_stop():
            """Bound coordinator shutdown, then clean and verify the frozen runtime PGID."""
            markerless_cleaned = False
            if self.alive():
                try:
                    self.popen.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    if markerless_group is not None:
                        ok, group_error, group_code = cleanup_markerless_job_group(
                            markerless_group)
                        if not ok:
                            self.last_stop_error = group_error
                            self.last_stop_code = group_code
                            return False
                        markerless_cleaned = True
                    elif frozen is not None:
                        snapshot = _process_snapshot()
                        coordinator = frozen["coordinator"]
                        current = snapshot.get(self.popen.pid) if snapshot is not None else None
                        if snapshot is None or current is None or not _same_process_instance(
                                coordinator, current):
                            self.last_stop_error = "coordinator force 前 process 身分已更新"
                            self.last_stop_code = 409 if snapshot is not None else 500
                            return False
                        try:
                            os.killpg(os.getpgid(self.popen.pid), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        except (PermissionError, OSError) as signal_error:
                            self.last_stop_error = f"coordinator force 失敗：{signal_error}"
                            return False
                    try:
                        self.popen.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.last_stop_error = "coordinator force 後仍存活"
                        return False
            if markerless_group is not None and not markerless_cleaned:
                ok, group_error, group_code = cleanup_markerless_job_group(markerless_group)
                if not ok:
                    self.last_stop_error = group_error
                    self.last_stop_code = group_code
                    return False
            if frozen is None:
                try:
                    frozen_runtime = freeze_workspace_stop_identity(
                        self.name, self.repo, self.popen.pid,
                        require_coordinator_marker=False)
                except RuntimeStopIdentityError as identity_error:
                    self.last_stop_error = str(identity_error)
                    self.last_stop_code = identity_error.code
                    return False
            else:
                frozen_runtime = frozen
            ok, runtime_error, runtime_code = cleanup_frozen_runtime_group(frozen_runtime)
            if not ok:
                self.last_stop_error = runtime_error
                self.last_stop_code = runtime_code
                return False
            return True

        if not wait:
            # Callers may parallelize Job.stop themselves, but this method never reports
            # success before the separately-sessioned runtime group is proven empty.
            return _finish_stop()
        return _finish_stop()


def loop_pid_alive(pid):
    """state.json 記的 pid 是否仍是 coordinator；同時支援舊檔案入口與 package module。"""
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError, PermissionError):
        return False
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return any(token in r.stdout for token in ("loop.py", "engine.loop", "engine.fleet"))


def _process_snapshot():
    """Return one bounded local process-tree snapshot keyed by PID."""
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,lstart=,command="],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        return None
    snapshot = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 8)
        if len(parts) < 9:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        snapshot[pid] = {"ppid": ppid, "pgid": pgid, "sid": pgid,
                         "started": " ".join(parts[3:8]), "command": parts[8]}
    return snapshot


def _coordinator_workspace_name(command: str):
    """Return the workspace proven by one loop/fleet command line, else None."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    coordinator = any(
        (argv[index:index + 2] in (["-m", "engine.loop"], ["-m", "engine.fleet"]))
        for index in range(max(0, len(argv) - 1))
    ) or any(
        token.endswith(("/engine/loop.py", "/engine/fleet.py")) or
        token in ("engine/loop.py", "engine/fleet.py")
        for token in argv
    )
    if not coordinator:
        return None

    def option(name):
        prefix = name + "="
        for index, token in enumerate(argv):
            if token.startswith(prefix):
                return token[len(prefix):]
            if token == name and index + 1 < len(argv):
                return argv[index + 1]
        return None

    explicit = option("--name")
    if explicit:
        return explicit if loop_mod.valid_workspace_name(explicit) else None
    repo = option("--repo")
    if not repo:
        return None
    inferred = Path(repo).expanduser().name
    return inferred if loop_mod.valid_workspace_name(inferred) else None


def _read_runtime_marker(entry: Path, filename: str):
    """Read one small no-symlink runtime marker or fail closed on malformed truth."""
    path = entry / filename
    try:
        path = loop_mod.workspace_file(path, f"{filename} marker")
        info = path.lstat()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise RuntimeError(f"workspace {entry.name} {filename} 無法安全檢查:{error}") from error
    if info.st_size > loop_mod.ACTIVE_RUNTIME_MAX_BYTES:
        raise RuntimeError(f"workspace {entry.name} {filename} 超過大小上限")
    try:
        fd = loop_mod._open_regular(path, os.O_RDONLY)
        with os.fdopen(fd, "rb", closefd=True) as stream:
            raw = stream.read(loop_mod.ACTIVE_RUNTIME_MAX_BYTES + 1)
        marker = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"workspace {entry.name} {filename} 無法安全解析:{error}") from error
    if not isinstance(marker, dict):
        raise RuntimeError(f"workspace {entry.name} {filename} 頂層必須是 object")
    return marker


def _marker_generation_matches(expected_generation: str | None, marker: dict) -> bool:
    """Accept the on-disk generation or an explicit reset/import old→pending transition."""
    if expected_generation is None:
        return True
    return expected_generation in {
        marker.get("workspace_generation"), marker.get("previous_workspace_generation")}


def _pending_coordinator_pids(snapshot):
    """Read root-scoped fleet identities that exist before the parent directory does."""
    ops = ROOT / loop_mod.WORKSPACE_OPS_DIR
    try:
        info = ops.lstat()
    except FileNotFoundError:
        return {}, set()
    except OSError as error:
        raise RuntimeError(f"無法檢查 pending runtime marker 目錄:{error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("pending runtime marker 目錄不是安全的實體目錄")
    try:
        paths = list(ops.iterdir())
    except OSError as error:
        raise RuntimeError(f"無法掃描 pending runtime markers:{error}") from error
    proven, blocked = {}, set()
    for path in paths:
        if not path.name.endswith(loop_mod.PENDING_RUNTIME_SUFFIX):
            continue
        try:
            marker_path = loop_mod.workspace_file(path, "pending runtime marker")
            if marker_path.lstat().st_size > loop_mod.ACTIVE_RUNTIME_MAX_BYTES:
                raise ValueError("超過 bounded 上限")
            fd = loop_mod._open_regular(marker_path, os.O_RDONLY)
            with os.fdopen(fd, "rb", closefd=True) as stream:
                raw = stream.read(loop_mod.ACTIVE_RUNTIME_MAX_BYTES + 1)
            if len(raw) > loop_mod.ACTIVE_RUNTIME_MAX_BYTES:
                raise ValueError("超過 bounded 上限")
            marker = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"pending runtime marker {path.name} 無法安全解析:{error}") from error
        name = marker.get("workspace_name") if isinstance(marker, dict) else None
        expected_path = (loop_mod.pending_runtime_marker_path(ROOT, name)
                         if loop_mod.valid_workspace_name(name) else None)
        if (not isinstance(marker, dict) or
                marker.get("schema_version") != loop_mod.ACTIVE_RUNTIME_SCHEMA_VERSION or
                marker.get("kind") != "fleet-pending" or expected_path != path or
                marker.get("workspace_root") != str(ROOT.resolve()) or
                not isinstance(marker.get("repo"), str) or not marker["repo"] or
                re.fullmatch(r"[0-9a-f]{32}", str(marker.get("pending_generation") or "")) is None or
                re.fullmatch(r"[0-9a-f]{32}", str(marker.get("session_id") or "")) is None or
                not isinstance(marker.get("pid"), int) or isinstance(marker.get("pid"), bool) or
                marker["pid"] <= 0 or not isinstance(marker.get("started"), str) or
                not marker["started"] or not isinstance(marker.get("command"), str) or
                not marker["command"] or len(marker["command"]) > 8192):
            raise RuntimeError(f"pending runtime marker {path.name} schema 不合法")
        current = snapshot.get(marker["pid"])
        if (current and current.get("started") == marker["started"] and
                current.get("command") == marker["command"] and
                marker["pid"] != os.getpid()):
            proven.setdefault(name, set()).add(marker["pid"])
        else:
            blocked.add(marker["pid"])
    return proven, blocked


def _coordinator_marker_pids(entry: Path, snapshot, expected_generation: str | None):
    """Resolve this root's exact coordinator PID; reject PID reuse/replacement."""
    marker = _read_runtime_marker(entry, loop_mod.COORDINATOR_RUNTIME_FILE)
    if marker is None:
        return set(), None
    if (marker.get("schema_version") != loop_mod.ACTIVE_RUNTIME_SCHEMA_VERSION or
            marker.get("workspace_name") != entry.name or
            marker.get("workspace_root") != str(ROOT.resolve()) or
            (marker.get("repo") is not None and not isinstance(marker.get("repo"), str)) or
            re.fullmatch(r"[0-9a-f]{32}", str(marker.get("workspace_generation") or "")) is None or
            (marker.get("previous_workspace_generation") is not None and
             re.fullmatch(r"[0-9a-f]{32}",
                          str(marker.get("previous_workspace_generation"))) is None) or
            marker.get("previous_workspace_generation") == marker.get("workspace_generation") or
            re.fullmatch(r"[0-9a-f]{32}", str(marker.get("session_id") or "")) is None or
            not isinstance(marker.get("pid"), int) or isinstance(marker.get("pid"), bool) or
            marker["pid"] <= 0 or not isinstance(marker.get("started"), str) or
            not marker["started"] or not isinstance(marker.get("command"), str) or
            not marker["command"] or len(marker["command"]) > 8192):
        raise RuntimeError(f"workspace {entry.name} coordinator runtime marker schema 不合法")
    if not _marker_generation_matches(expected_generation, marker):
        return set(), marker["pid"]
    current = snapshot.get(marker["pid"])
    if (current and current.get("started") == marker["started"] and
            current.get("command") == marker["command"] and marker["pid"] != os.getpid()):
        return {marker["pid"]}, None
    # Marker exists but this numeric PID is a reused/replacement process.  Preserve
    # it and prevent the legacy state-only fallback from authorizing a signal.
    return set(), marker["pid"]


def _runtime_marker_pids(entry: Path, snapshot, expected_generation: str | None) -> set[int]:
    """Resolve an exact durable agent/validator group, including a dead leader's members."""
    marker = _read_runtime_marker(entry, loop_mod.ACTIVE_RUNTIME_FILE)
    if marker is None:
        return set()
    integer_fields = ("owner_pid", "pid", "pgid", "sid")
    if (marker.get("schema_version") != loop_mod.ACTIVE_RUNTIME_SCHEMA_VERSION or
            marker.get("kind") not in {"agent", "validator"} or
            marker.get("workspace_name") != entry.name or
            re.fullmatch(r"[0-9a-f]{32}", str(marker.get("workspace_generation") or "")) is None or
            (marker.get("previous_workspace_generation") is not None and
             re.fullmatch(r"[0-9a-f]{32}",
                          str(marker.get("previous_workspace_generation"))) is None) or
            marker.get("previous_workspace_generation") == marker.get("workspace_generation") or
            re.fullmatch(r"[0-9a-f]{32}", str(marker.get("session_id") or "")) is None or
            marker.get("workspace_root") != str(ROOT.resolve()) or
            (marker.get("repo") is not None and not isinstance(marker.get("repo"), str)) or
            not isinstance(marker.get("owner_started"), str) or
            not isinstance(marker.get("owner_command"), str) or
            any(not isinstance(marker.get(field), int) or isinstance(marker.get(field), bool) or
                marker[field] <= 0 for field in integer_fields) or
            marker["pid"] != marker["pgid"] or marker["pid"] != marker["sid"] or
            not isinstance(marker.get("started"), str) or not marker["started"] or
            not isinstance(marker.get("command"), str) or not marker["command"] or
            not isinstance(marker.get("target_command"), str) or
            len(marker["command"]) > 8192 or len(marker["target_command"]) > 8192):
        raise RuntimeError(f"workspace {entry.name} runtime marker schema 不合法")
    if not _marker_generation_matches(expected_generation, marker):
        # A copied/stale marker must never authorize killing a same-name replacement.
        return set()
    leader = snapshot.get(marker["pid"])
    if leader is not None and not (
            leader.get("pgid") == marker["pgid"] and leader.get("sid") == marker["sid"] and
            leader.get("started") == marker["started"] and
            leader.get("command") == marker["command"]):
        # PID reuse or a replacement process: preserve it.
        return set()
    members = {
        pid for pid, process in snapshot.items()
        if process.get("pgid") == marker["pgid"] and process.get("sid") == marker["sid"]
    }
    if leader is None and members:
        # Numeric process-group IDs may be reused after the original leader and all
        # durable members exit.  Without a persisted per-member start/command roster,
        # these processes are ambiguous and must never be guessed/signal-authorized.
        raise RuntimeError(
            f"workspace {entry.name} runtime leader 已不存在但 PGID/SID 有未證明 members；拒絕猜測 signal")
    return members


def _workspace_coordinator_pids(snapshot) -> set[int]:
    """Collect exact coordinators and durable orphan runtime groups for every workspace."""
    pending_by_name, pending_blocked = _pending_coordinator_pids(snapshot)
    candidates = set().union(*pending_by_name.values()) if pending_by_name else set()
    try:
        entries = list(ROOT.iterdir())
    except (FileNotFoundError, OSError) as error:
        raise RuntimeError(f"無法掃描 workspace root {ROOT}:{error}") from error
    for entry in entries:
        if not loop_mod.valid_workspace_name(entry.name):
            continue
        try:
            info = entry.lstat()
        except OSError as error:
            raise RuntimeError(f"無法檢查 workspace entry {entry.name}:{error}") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"workspace entry {entry.name} 不是安全的實體目錄")
        state, error = read_state(entry.name, repair=False)
        if error or not state:
            legacy = legacy_workspace_identity_for_delete(entry.name)
            state = legacy.get("state") if legacy else None
        expected_generation = (state.get("workspace_generation")
                               if isinstance(state, dict) else None)
        proven_coordinators, blocked_marker_pid = _coordinator_marker_pids(
            entry, snapshot, expected_generation)
        proven_coordinators.update(pending_by_name.get(entry.name, set()))
        candidates.update(proven_coordinators)
        candidates.update(_runtime_marker_pids(entry, snapshot, expected_generation))
        if not isinstance(state, dict):
            same_name = {
                pid for pid, process in snapshot.items()
                if pid != os.getpid() and
                _coordinator_workspace_name(process["command"]) == entry.name
            }
            if same_name - proven_coordinators:
                # A command's --name does not identify LOOP_AGENT_WORKSPACE_ROOT; it
                # may belong to another root.  Preserve it and refuse to serve until
                # this corrupt workspace can be proven, rather than guessing/killing.
                raise RuntimeError(
                    f"workspace {entry.name} state 無法讀取，且有未經 root marker 證明的同名 coordinator")
            continue
        pids = [(state.get("loop") or {}).get("pid")]
        if state.get("workspace_kind") == "fleet-parent":
            fleet = read_parallel_run(entry.name)
            if not fleet.get("read_error"):
                pids.append((fleet.get("loop") or {}).get("pid"))
        for pid in pids:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            if blocked_marker_pid == pid or pid in pending_blocked:
                process = snapshot.get(pid)
                if process is not None:
                    raise RuntimeError(
                        f"workspace {entry.name} coordinator marker identity mismatch，拒絕猜測 signal")
                continue
            process = snapshot.get(pid)
            if (pid != os.getpid() and process and
                    _coordinator_workspace_name(process["command"]) == entry.name):
                if pid not in proven_coordinators:
                    raise RuntimeError(
                        f"workspace {entry.name} live coordinator 缺 durable root marker，拒絕猜測 signal")
                candidates.add(pid)
    return candidates


def _snapshot_descendants(snapshot, roots: set[int]):
    descendants = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, process in snapshot.items():
            if pid not in descendants and process["ppid"] in descendants:
                descendants.add(pid)
                changed = True
    return descendants


def _same_process_instance(before, now) -> bool:
    """Match one captured PID without treating normal orphan reparenting as PID reuse."""
    return bool(before and now and
                before.get("started") == now.get("started") and
                before.get("command") == now.get("command"))


def _command_option(command: str, name: str):
    """Read one exact argv option from a process command without substring matching."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    prefix = name + "="
    for index, token in enumerate(argv):
        if token.startswith(prefix):
            return token[len(prefix):]
        if token == name and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _external_standalone_process_identity(name: str, state, snapshot):
    """Freeze one externally started standalone coordinator, or classify it as gone/stale."""
    loop_state = state.get("loop") if isinstance(state, dict) else None
    config = state.get("config") if isinstance(state, dict) else None
    if (not isinstance(loop_state, dict) or not isinstance(config, dict) or
            state.get("workspace_kind") != "standalone"):
        return None, "stale"
    try:
        pid = int(loop_state.get("pid"))
    except (TypeError, ValueError):
        return None, "gone"
    process = snapshot.get(pid)
    if process is None:
        return None, "gone"
    command = str(process.get("command") or "")
    if not str(process.get("started") or "") or not command:
        return None, "stale"
    repo = str(config.get("repo") or "")
    command_repo = _command_option(command, "--repo")
    session_id = loop_state.get("session_id")
    generation = state.get("workspace_generation")
    # A standalone state may only authorize its own engine.loop process.  A generic
    # `engine.loop`/`engine.fleet` substring or the same numeric PID is not identity.
    try:
        same_repo = bool(repo and command_repo and
                         Path(repo).expanduser().resolve() ==
                         Path(command_repo).expanduser().resolve())
    except (OSError, RuntimeError):
        same_repo = False
    try:
        argv = shlex.split(command)
    except ValueError:
        return None, "stale"
    standalone_coordinator = any(
        argv[index:index + 2] == ["-m", "engine.loop"]
        for index in range(max(0, len(argv) - 1))
    ) or any(token == "engine/loop.py" or token.endswith("/engine/loop.py") for token in argv)
    if (not same_repo or _coordinator_workspace_name(command) != name or
            not standalone_coordinator or
            not isinstance(session_id, str) or not session_id):
        return None, "stale"
    return {
        "name": name,
        "pid": pid,
        "session_id": session_id,
        "workspace_generation": generation,
        "repo": str(Path(repo).expanduser().resolve()),
        "process": process,
    }, "same"


def _revalidate_external_standalone_process(identity, *, allow_cleared=False):
    """Recheck frozen state and OS process truth immediately before another signal."""
    snapshot = _process_snapshot()
    if snapshot is None:
        return "snapshot-error"
    process = snapshot.get(identity["pid"])
    if process is None:
        return "gone"
    if not _same_process_instance(identity["process"], process):
        return "stale"
    state, error = read_state(identity["name"], repair=False)
    if error or not isinstance(state, dict):
        return "stale"
    loop_state = state.get("loop") if isinstance(state.get("loop"), dict) else {}
    config = state.get("config") if isinstance(state.get("config"), dict) else {}
    try:
        same_repo = (Path(str(config.get("repo") or "")).expanduser().resolve() ==
                     Path(identity["repo"]))
    except (OSError, RuntimeError):
        same_repo = False
    if (state.get("workspace_kind") != "standalone" or
            state.get("workspace_generation") != identity["workspace_generation"] or
            not same_repo or loop_state.get("session_id") != identity["session_id"]):
        return "stale"
    if loop_state.get("pid") == identity["pid"]:
        return "same"
    # Normal SIGINT cleanup clears pid immediately before process exit.  It is safe to
    # wait for that exact captured process, but no later force signal may be authorized.
    if allow_cleared and loop_state.get("pid") is None:
        return "exiting"
    return "stale"


def stop_workspace_coordinators(*, grace_seconds=8.0, force_seconds=2.0):
    """Stop every persisted coordinator before serving; force its captured descendants if needed."""
    initial = _process_snapshot()
    if initial is None:
        raise RuntimeError("無法取得 process snapshot，拒絕啟動 Dashboard")
    roots = _workspace_coordinator_pids(initial)
    if not roots:
        return {"requested": 0, "forced": 0, "remaining": []}
    captured = _snapshot_descendants(initial, roots)
    before_signal = _process_snapshot()
    if before_signal is None:
        raise RuntimeError("清場送出 SIGINT 前無法重新取得 process snapshot")
    signal_errors = []
    for pid in sorted(roots):
        if not _same_process_instance(initial.get(pid), before_signal.get(pid)):
            # It exited or the numeric PID was reused after the discovery snapshot.
            continue
        try:
            os.kill(pid, signal.SIGINT)
            print(f"Dashboard 啟動清場｜SIGINT coordinator pid={pid}", flush=True)
        except (ProcessLookupError, PermissionError, OSError) as error:
            print(f"Dashboard 啟動清場｜無法 SIGINT pid={pid}: {error}", flush=True)
            if not isinstance(error, ProcessLookupError):
                signal_errors.append(f"SIGINT {pid}: {error}")
    deadline = time.monotonic() + max(0.0, grace_seconds)
    current = _process_snapshot()
    if current is None:
        raise RuntimeError("清場期間無法重新取得 process snapshot")
    while any(pid in current for pid in roots) and time.monotonic() < deadline:
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        current = _process_snapshot()
        if current is None:
            raise RuntimeError("清場期間無法重新取得 process snapshot")
    force = []
    for pid in captured:
        before, now = initial.get(pid), current.get(pid)
        # PPID is only part of the initial bounded tree.  A surviving child is
        # reparented when its coordinator exits, but it is still the same captured
        # process and must be forced.  Start time + command continue to reject PID reuse.
        if _same_process_instance(before, now):
            force.append(pid)
    # Descendants first; a stuck coordinator must not leave a detached agent/validator.
    depth = {}
    for pid in force:
        value, seen = 0, set()
        parent = initial.get(pid, {}).get("ppid")
        while parent in captured and parent not in seen:
            seen.add(parent); value += 1; parent = initial.get(parent, {}).get("ppid")
        depth[pid] = value
    for pid in sorted(force, key=lambda value: (depth[value], value), reverse=True):
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"Dashboard 啟動清場｜SIGKILL pid={pid}", flush=True)
        except (ProcessLookupError, PermissionError, OSError) as error:
            print(f"Dashboard 啟動清場｜無法 SIGKILL pid={pid}: {error}", flush=True)
            if not isinstance(error, ProcessLookupError):
                signal_errors.append(f"SIGKILL {pid}: {error}")
    force_deadline = time.monotonic() + max(0.0, force_seconds)
    current = _process_snapshot()
    if current is None:
        raise RuntimeError("force 清場後無法取得 process snapshot")
    while any(pid in current for pid in force) and time.monotonic() < force_deadline:
        time.sleep(min(0.1, max(0.0, force_deadline - time.monotonic())))
        current = _process_snapshot()
        if current is None:
            raise RuntimeError("force 清場後無法取得 process snapshot")
    remaining = sorted(pid for pid in force if pid in current)
    if remaining:
        print(f"Dashboard 啟動清場｜仍存活:{remaining}", flush=True)
    if signal_errors or remaining:
        raise RuntimeError("Dashboard 啟動清場未完成：" +
                           "; ".join([*signal_errors,
                                      *( [f"仍存活 {remaining}"] if remaining else [])]))
    return {"requested": len(roots), "forced": len(force), "remaining": remaining}


def norm_cmd(s):
    """shlex 正規化,供命令白名單比對。"""
    try:
        return shlex.join(shlex.split(str(s)))
    except ValueError:
        return None


def repo_file_status(repo: Path, relative_path: str) -> str:
    """回傳 repo 檔案相對 HEAD 的狀態，供啟動器用一致語意顯示。"""
    in_head = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"HEAD:{relative_path}"],
        capture_output=True,
    ).returncode == 0
    dirty = bool(subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--", relative_path],
        capture_output=True,
        text=True,
    ).stdout.strip())
    if in_head and not dirty:
        return "committed"
    if in_head:
        return "modified"
    return "untracked" if (repo / relative_path).exists() else "missing"


def suggested_validate_command(repo: Path):
    """依專案入口檔提供保守的預設驗證命令；無法辨識時交由使用者選擇。"""
    if (repo / "pom.xml").is_file():
        return "mvn -q compile"
    if (repo / "package.json").is_file():
        return "sh -c 'npm run build && npm test -- --run && npx playwright test'"
    if (repo / "tests").is_dir():
        return "python3 -m unittest discover -s tests -t . -q"
    return None


def repo_status_projection(repo: Path):
    """集中組裝 repo 啟動前投影，避免 HTTP handler 同時負責 Git 細節與回應格式。"""
    if not (repo / ".git").exists():
        return {"error": f"{repo} 不是 git repo"}
    clean = not subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    branch_result = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True,
        text=True,
    )
    return {
        "goal": repo_file_status(repo, "goal.md"),
        "tree_clean": clean,
        "branch": branch_result.stdout.strip() if branch_result.returncode == 0 else "",
        "suggested_validate_cmd": suggested_validate_command(repo),
    }


def spawn_loop(name, repo, agent_cmd, validate_cmd, ft, dt, rt, validate_timeout=120,
               reset=False, import_plan=None, start_phase="plan", notify_cmd="",
               red_limit=20, stall_limit=300, stuck_stop=False, stuck_count=100,
               agent_backoff_max=60, pause_after_plan=False,
               expected_generation="", env=None):
    """spawn loop.py 並登記進 JOBS(呼叫方需持 JOBS_LOCK)。"""
    loop_mod.require_workspace_name(name)
    workspace_dir = safe_workspace_dir(name)
    cmd = [sys.executable, "-m", "engine.loop", "--repo", str(repo), "--name", name,
           "--agent-cmd", agent_cmd, "--validate-cmd", validate_cmd,
           "--flag-threshold", str(ft), "--done-threshold", str(dt), "--round-timeout", str(rt),
           "--agent-backoff-max", str(agent_backoff_max),
           "--validate-timeout", str(validate_timeout),
           "--red-limit", str(red_limit), "--stall-limit", str(stall_limit)]
    if expected_generation:
        cmd += ["--expected-workspace-generation", expected_generation]
    if stuck_stop:
        cmd += ["--stuck-stop", "--stuck-stop-count", str(stuck_count)]
    if pause_after_plan:
        cmd.append("--pause-after-plan")
    if reset:
        cmd.append("--reset-state")
    if import_plan:
        cmd += ["--import-plan", str(import_plan), "--start-phase", start_phase, "--consume-import-plan"]
    if notify_cmd:
        cmd += ["--notify-cmd", notify_cmd]
    (workspace_dir / "startup_ready.json").unlink(missing_ok=True)
    if not import_plan:
        (workspace_dir / "import-plan.pending.json").unlink(missing_ok=True)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, start_new_session=True, env=env)
    _prune_finished_jobs_locked()
    JOBS[name] = Job(name, str(repo), p)
    return p


def fleet_resume_command(name, fleet_state):
    """只由 persisted config 組 resume CLI，避免 Dashboard 當下 defaults 改寫既有 run。"""
    config = fleet_state.get("config") or {}
    command = [sys.executable, "-m", "engine.fleet", "--resume",
               "--expected-run-id", str(fleet_state.get("run_id") or ""),
               "--repo", str(config.get("repo") or fleet_state.get("integration_worktree")),
               "--name", name, "--goal", str(config.get("goal") or "goal.md"),
               "--agent-cmd", str(config.get("agent_cmd") or ""),
               "--validate-cmd", str(config.get("validate_cmd") or ""),
               "--max-parallel", str(config.get("max_parallel", 4)),
               "--merge-threshold", str(config.get("merge_threshold", 2)),
               "--done-threshold", str(config.get("done_threshold", 3)),
               "--flag-threshold", str(config.get("flag_threshold", 10)),
               "--red-limit", str(config.get("red_limit", 20)),
               "--stall-limit", str(config.get("stall_limit", 300)),
               "--round-timeout", str(config.get("round_timeout", 30)),
               "--validate-timeout", str(config.get("validate_timeout", 120)),
               "--agent-backoff-max", str(config.get("agent_backoff_max", 60)),
               "--max-child-restarts", str(config.get("max_child_restarts", 0)),
               "--track-env-json", json.dumps(config.get("track_env") or {}, ensure_ascii=False),
               "--track-port-base", str(config.get("track_port_base", 0))]
    if config.get("pause_after_plan"):
        command.append("--pause-after-plan")
    if config.get("notify_cmd"):
        command += ["--notify-cmd", str(config["notify_cmd"])]
    if config.get("plan_doc"):
        command += ["--plan-doc", str(config["plan_doc"])]
    return command


def spawn_fleet_resume(name, fleet_state, env=None):
    """Resume an existing fleet-parent from its frozen runtime configuration."""
    config = fleet_state.get("config") or {}
    command = fleet_resume_command(name, fleet_state)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1, start_new_session=True, env=env)
    _prune_finished_jobs_locked()
    JOBS[name] = Job(name, str(config.get("repo") or fleet_state.get("integration_worktree")), process,
                     kind="fleet")
    return process


def spawn_fleet(name, repo, agent_cmd, validate_cmd, *, goal="goal.md", plan_doc="", import_plan=None,
                max_parallel=4, merge_threshold=2, flag_threshold=10, done_threshold=3,
                red_limit=20, stall_limit=300,
                round_timeout=30, validate_timeout=120, agent_backoff_max=60,
                max_child_restarts=0, pause_after_plan=False, notify_cmd="", track_env=None,
                track_port_base=0, env=None):
    """Start a new parallel fleet and register its parent process as a Dashboard job."""
    command = [sys.executable, "-m", "engine.fleet", "--repo", str(repo), "--name", name,
               "--goal", goal, "--agent-cmd", agent_cmd, "--validate-cmd", validate_cmd,
               "--max-parallel", str(max_parallel), "--merge-threshold", str(merge_threshold),
               "--flag-threshold", str(flag_threshold), "--done-threshold", str(done_threshold),
               "--red-limit", str(red_limit), "--stall-limit", str(stall_limit),
               "--round-timeout", str(round_timeout), "--validate-timeout", str(validate_timeout),
               "--agent-backoff-max", str(agent_backoff_max),
               "--max-child-restarts", str(max_child_restarts),
               "--track-env-json", json.dumps(track_env or {}, ensure_ascii=False),
               "--track-port-base", str(track_port_base)]
    if pause_after_plan:
        command.append("--pause-after-plan")
    if plan_doc:
        command += ["--plan-doc", plan_doc]
    if import_plan:
        command += ["--import-plan", str(import_plan), "--consume-import-plan"]
    if notify_cmd:
        command += ["--notify-cmd", notify_cmd]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1, start_new_session=True, env=env)
    _prune_finished_jobs_locked()
    JOBS[name] = Job(name, str(repo), process, kind="fleet",
                     cleanup_paths=([import_plan] if import_plan else ()))
    return process


def write_parallel_launch_plan(name: str, plan) -> Path:
    """Stage a new-run import outside the not-yet-owned workspace and clean it with the Job."""
    loop_mod.require_workspace_name(name)
    launch_dir = loop_mod.ensure_real_directory(ROOT / ".launch-inputs", "parallel launch input 目錄")
    path = launch_dir / f"{name}-{uuid.uuid4().hex}.json"
    loop_mod.atomic_write_bytes(path, json.dumps(plan, ensure_ascii=False, indent=2).encode("utf-8"))
    return path


def run_command_check(cmd, cwd, prompt="", timeout=60, env=None):
    """執行 UI 的命令確認；逾時時連同 CLI 衍生的子程序群組一起清掉。"""
    p = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, start_new_session=True, env=env)
    try:
        output, _ = p.communicate(prompt, timeout=timeout)
        return p.returncode, output or "", False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        output, _ = p.communicate()
        return p.returncode, output or "", True


def job_startup_status(name, pid):
    """回報特定 spawn 是否真的通過 preflight/Validate 並成功啟動第一個 Agent。"""
    if not loop_mod.valid_workspace_name(name):
        return {"status": "failed", "error": f"workspace 名稱不合法：{loop_mod.WORKSPACE_NAME_RULE}"}
    try:
        expected_pid = int(pid)
    except (TypeError, ValueError):
        return {"status": "failed", "error": "啟動狀態 pid 不合法"}
    with JOBS_LOCK:
        job = JOBS.get(name)
    if job is None or job.popen.pid != expected_pid:
        return {"status": "failed", "error": "找不到這次啟動工作（可能已被另一個啟動取代）"}
    if job.kind == "fleet":
        fleet = read_parallel_run(name)
        if not fleet.get("read_error"):
            fleet_pid = int((fleet.get("loop") or {}).get("pid") or 0)
            if fleet_pid == expected_pid:
                return {"status": "ready", "pid": expected_pid, "run_id": fleet.get("run_id")}
            if not job.alive() and job.popen.returncode == 0 and fleet.get("phase") in {
                    "awaiting-approval", "stopped", "done"}:
                return {"status": "ready", "pid": expected_pid, "run_id": fleet.get("run_id")}
    if not job.alive():
        job.reader.join(timeout=0.5)
        info = job.info()
        return {"status": "failed", "rc": info["rc"],
                "error": f"loop 啟動失敗（rc={info['rc']}）",
                "tail": "\n".join(list(job.out)[-80:])}
    try:
        ready = safe_workspace_dir(name) / "startup_ready.json"
        loop_mod.workspace_file(ready, "startup marker")
    except ValueError as e:
        return {"status": "failed", "error": str(e)}
    try:
        fd = loop_mod._open_regular(ready, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
            marker = json.load(stream)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return {"status": "starting"}
    if marker.get("pid") == expected_pid:
        return {"status": "ready", "pid": expected_pid}
    return {"status": "starting"}


def read_state(name, *, repair=True):
    """讀 workspace state；主檔壞時可由 last-good checkpoint 復原。"""
    if not loop_mod.valid_workspace_name(name):
        return None, f"workspace 名稱 {name or '(空)'} 不合法：{loop_mod.WORKSPACE_NAME_RULE}"
    try:
        state_path = safe_workspace_dir(name) / "state.json"
    except ValueError as e:
        return None, str(e)
    try:
        state, _data, recovered = loop_mod.load_checkpointed_state(state_path, repair=repair)
    except FileNotFoundError:
        return None, f"workspace {name} 不存在(沒有 state.json/checkpoint)"
    except ValueError as e:
        return None, f"workspace artifact 不安全:{e}"
    except loop_mod.StateLoadError as e:
        return None, f"state.json 與 recovery checkpoint 都無法讀取:{e}"
    if recovered:
        state = dict(state)
        if repair:
            loop_mod.mark_state_recovered(state)
            write_state(name, state)
            workspace_console_log(name, f"🛟 state.json 已從 last-good checkpoint 復原｜"
                                  f"第 {state['state_recovery_count']} 次")
        else:
            state["state_recovery_pending"] = True
    return state, None


def _legacy_workspace_state_from_fd(directory_fd_value: int):
    """Classify v1 state using only one already-open workspace directory."""
    legacy_candidates = []
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return None
    for filename in ("state.json", "state.last-good.json"):
        try:
            fd = os.open(filename, os.O_RDONLY | nofollow, dir_fd=directory_fd_value)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        try:
            with os.fdopen(fd, "rb", closefd=True) as stream:
                fd = None
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                        info.st_size > LEGACY_STATE_MAX_BYTES):
                    return None
                raw = stream.read(LEGACY_STATE_MAX_BYTES + 1)
            if len(raw) > LEGACY_STATE_MAX_BYTES:
                return None
            candidate = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            # Every existing truth candidate must independently prove it is legacy.
            # A corrupt v2 primary must never be downgraded via a stale v1 checkpoint.
            return None
        finally:
            if fd is not None:
                os.close(fd)
        if not isinstance(candidate, dict):
            return None
        schema = candidate.get("state_schema_version")
        if schema == loop_mod.STATE_SCHEMA_VERSION:
            return None
        kind = candidate.get("workspace_kind")
        loop_state = candidate.get("loop")
        legacy_identity = (
            (schema is None and kind is None) or
            (isinstance(schema, int) and not isinstance(schema, bool) and schema == 1 and
             (kind is None or kind == "standalone"))
        )
        nonnegative_int = lambda value: (
            isinstance(value, int) and not isinstance(value, bool) and value >= 0)
        legacy_core = (
            candidate.get("phase") in ("plan", "exec", "done") and
            nonnegative_int(candidate.get("round")) and
            isinstance(candidate.get("plan"), list) and
            nonnegative_int(candidate.get("plan_version")) and
            isinstance(candidate.get("completed"), list)
        )
        if (legacy_identity and legacy_core and "workspace_generation" not in candidate and
                candidate.get("fleet_run_id") is None and
                (loop_state is None or isinstance(loop_state, dict))):
            legacy_candidates.append(candidate)
        else:
            return None
    return legacy_candidates[0] if legacy_candidates else None


def _legacy_snapshot_generation(info, state):
    """Hash one descriptor-bound legacy snapshot without writing migration metadata."""
    identity_bytes = json.dumps({
        "dev": info.st_dev,
        "ino": info.st_ino,
        "ctime_ns": info.st_ctime_ns,
        "mtime_ns": info.st_mtime_ns,
        "state": state,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(identity_bytes).hexdigest()[:32]


def legacy_workspace_identity_for_delete(name):
    """Return one descriptor-bound legacy state/inode snapshot for delete preflight."""
    if not loop_mod.valid_workspace_name(name):
        return None
    try:
        with directory_fd(ROOT, "workspace 根目錄") as root_fd:
            with directory_fd(name, f"workspace {name}", dir_fd=root_fd) as workspace_fd:
                info = os.fstat(workspace_fd)
                state = _legacy_workspace_state_from_fd(workspace_fd)
                if state is None:
                    return None
                _require_same_directory_entry(root_fd, name, workspace_fd, f"workspace {name}")
                # Legacy state intentionally has no durable v2 generation.  Expose a read-only
                # descriptor snapshot as the browser precondition without creating the delete
                # marker from GET/SSE.  The destructive transaction still creates a random,
                # fsync'd marker under the writer lock before journaling.
                return {"state": state, "inode": (info.st_dev, info.st_ino),
                        "generation": _legacy_snapshot_generation(info, state)}
    except (OSError, SafeDeleteError):
        return None


def legacy_workspace_state_for_delete(name):
    """Read-only summary projection for the v1 permanent-delete escape hatch."""
    identity = legacy_workspace_identity_for_delete(name)
    return identity["state"] if identity is not None else None


def write_state(name, st):
    """原子寫 workspace 主 state 與 last-good checkpoint。"""
    loop_mod.require_workspace_name(name)
    data = json.dumps(st, ensure_ascii=False, indent=2).encode("utf-8")
    loop_mod.write_checkpointed_state(safe_workspace_dir(name) / "state.json", data)


def _read_parallel_json(path: Path, label: str):
    """Read one fleet truth candidate without following links or repairing either copy."""
    path = loop_mod.workspace_file(path, label)
    fd = loop_mod._open_regular(path, os.O_RDONLY)
    with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{label} 頂層必須是 JSON object")
    return value


def _fleet_edit_fault_hook(_stage: str, _name: str):
    """Deterministic no-op hook for the four-file Dashboard fleet transaction."""


def _persisted_fleet_projection(fleet):
    value = dict(fleet)
    value.pop("resumable", None)
    value.pop("fleet_recovery_pending", None)
    return value


@contextmanager
def fleet_mutation_guard(name: str, expected_fleet, expected_parent_state):
    """Exclude both fleet coordinators and re-read the exact paired truth under their locks."""
    with loop_mod.workspace_operation_lock(ROOT, name, blocking=False):
        with locked_workspace_entry(name, (".fleet.run.lock", ".run.lock")) as handle:
            current_state = _read_state_from_directory_fd(handle["workspace_fd"])
            current_fleet = _read_fleet_from_directory_fd(handle["workspace_fd"])
            expected_fleet = _persisted_fleet_projection(expected_fleet)
            if (current_state != expected_parent_state or current_fleet != expected_fleet or
                    current_state.get("workspace_kind") != "fleet-parent" or
                    current_fleet.get("run_id") != expected_fleet.get("run_id")):
                raise SafeDeleteError(
                    f"parallel run {name} truth 在操作確認後已更新", 409)
            yield handle


def write_parallel_dashboard_transaction(name: str, fleet, parent_state, *,
                                         expected_fleet=None,
                                         expected_parent_state=None) -> None:
    """Commit four fleet truth files as one stopped, descriptor-revalidated transaction."""
    guard = (fleet_mutation_guard(name, expected_fleet, expected_parent_state)
             if expected_fleet is not None and expected_parent_state is not None
             else nullcontext())
    with guard:
        _write_parallel_dashboard_transaction_unlocked(name, fleet, parent_state)


def _write_parallel_dashboard_transaction_unlocked(name: str, fleet, parent_state) -> None:
    """Commit fleet truth/checkpoint and parent mirrors, rolling back caught failures."""
    workspace = safe_workspace_dir(name)
    fleet = _persisted_fleet_projection(fleet)
    paths = (
        (workspace / "fleet.json", "fleet.json",
         json.dumps(fleet, ensure_ascii=False, indent=2).encode()),
        (workspace / "state.json", "state.json",
         json.dumps(parent_state, ensure_ascii=False, indent=2).encode()),
        (workspace / "fleet.last-good.json", "fleet.last-good.json",
         json.dumps(fleet, ensure_ascii=False, indent=2).encode()),
        (workspace / "state.last-good.json", "state.last-good.json",
         json.dumps(parent_state, ensure_ascii=False, indent=2).encode()),
    )
    previous = {}
    for path, label, _data in paths:
        try:
            previous[path] = loop_mod.read_regular_bytes(path, label)
        except FileNotFoundError:
            previous[path] = None
    try:
        for path, label, data in paths:
            loop_mod.atomic_write_bytes(path, data)
            _fleet_edit_fault_hook(f"after-{label}", name)
    except (OSError, RuntimeError, ValueError):
        rollback_errors = []
        for path, _label, _data in paths:
            try:
                old = previous[path]
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    loop_mod.atomic_write_bytes(path, old)
            except (OSError, ValueError) as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(
                "fleet edit transaction rollback 未完成：" + "; ".join(rollback_errors))
        raise


def _read_state_from_directory_fd(directory_fd_value: int):
    """Read primary/checkpoint state relative to an already locked workspace directory."""
    failures = []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for filename in ("state.json", "state.last-good.json"):
        try:
            fd = os.open(filename, os.O_RDONLY | nofollow, dir_fd=directory_fd_value)
            with os.fdopen(fd, "rb", closefd=True) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("不是單一 regular file")
                return loop_mod.decode_state_bytes(stream.read(), filename)
        except (FileNotFoundError, OSError, ValueError, loop_mod.StateLoadError) as error:
            failures.append(f"{filename}: {error}")
    raise SafeDeleteError("locked child state 無法讀取：" + "; ".join(failures), 409)


def _read_fleet_from_directory_fd(directory_fd_value: int):
    failures = []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for filename in ("fleet.json", "fleet.last-good.json"):
        try:
            fd = os.open(filename, os.O_RDONLY | nofollow, dir_fd=directory_fd_value)
            with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
                value = json.load(stream)
            if not isinstance(value, dict):
                raise ValueError("頂層不是 object")
            return value
        except (FileNotFoundError, OSError, ValueError,
                json.JSONDecodeError, UnicodeDecodeError) as error:
            failures.append(f"{filename}: {error}")
    raise SafeDeleteError("locked fleet truth 無法讀取：" + "; ".join(failures), 409)


@contextmanager
def standalone_mutation_guard(name: str, expected_state):
    """Exclude external writers and re-read exact standalone truth before a stopped mutation."""
    with loop_mod.workspace_operation_lock(ROOT, name, blocking=False):
        with locked_workspace_entry(name, (".run.lock",)) as handle:
            current = _read_state_from_directory_fd(handle["workspace_fd"])
            if (current.get("workspace_kind") != "standalone" or
                    current.get("workspace_generation") !=
                    expected_state.get("workspace_generation") or current != expected_state):
                raise SafeDeleteError(
                    f"workspace {name} truth 在操作確認後已更新", 409)
            yield current


def commit_standalone_state(name: str, expected_state, new_state) -> None:
    with standalone_mutation_guard(name, expected_state):
        write_state(name, new_state)


def stopped_workspace_command_guard(name: str, state, fleet=None):
    """Keep a workspace stopped and unchanged for the duration of a manual command test."""
    if not name or state is None:
        return nullcontext()
    if state.get("workspace_kind") == "fleet-parent":
        return fleet_mutation_guard(name, fleet, state)
    return standalone_mutation_guard(name, state)


def _parallel_projection_error(data, state):
    """Validate every field consumed by Dashboard while preserving a legitimate fleet `error`."""
    if (data.get("schema_version") != 1 or data.get("workspace_kind") != "fleet-parent" or
            data.get("run_id") != state.get("fleet_run_id")):
        return "fleet truth 與 parent state 身分不符"
    mirror_revision = state.get("fleet_truth_revision")
    if (mirror_revision is not None and
            data.get("dashboard_revision", 0) != mirror_revision):
        return "fleet truth dashboard revision 與 parent mirror 不符"
    if data.get("phase") not in FLEET_PHASES:
        return "fleet truth phase 不合法"
    resume_phase = data.get("resume_phase")
    if resume_phase is not None and resume_phase not in FLEET_RESUME_PHASES:
        return "fleet truth resume_phase 不合法"
    if data.get("phase") == "failed" and resume_phase is None:
        return "failed fleet truth 缺少合法 resume_phase"
    for field in ("plan", "tracks", "merge_queue"):
        if field in data and not isinstance(data[field], list):
            return f"fleet truth {field} 型別不合法"
    if "loop" in data and not isinstance(data["loop"], dict):
        return "fleet truth loop 型別不合法"
    if "config" in data and not isinstance(data["config"], dict):
        return "fleet truth config 型別不合法"
    for track in data.get("tracks") or []:
        if not isinstance(track, dict) or not isinstance(track.get("name"), str):
            return "fleet truth track 結構不合法"
    if "error" in data and data["error"] is not None and not isinstance(data["error"], str):
        return "fleet truth error 型別不合法"
    return None


def read_parallel_run(name):
    """Return fleet truth or a distinct `read_error`; a failed fleet may legitimately contain `error`."""
    st, error = read_state(name, repair=False)
    if error:
        return {"read_error": error}
    if st.get("workspace_kind") != "fleet-parent":
        return {"read_error": f"{name} 不是 parallel run parent"}
    workspace = safe_workspace_dir(name)
    failures = []
    for path, label in ((workspace / "fleet.json", "fleet.json"),
                        (workspace / "fleet.last-good.json", "fleet.last-good.json")):
        try:
            data = _read_parallel_json(path, label)
            invalid = _parallel_projection_error(data, st)
            if invalid:
                raise ValueError(invalid)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            failures.append(f"{label}: {exc}")
            continue
        if label != "fleet.json":
            data = dict(data)
            data["fleet_recovery_pending"] = True
        else:
            data = dict(data)
        data["resumable"] = data.get("phase") != "done"
        return data
    return {"read_error": "fleet truth 無法讀取：" + "; ".join(failures)}


def parallel_progress_projection(name, parallel):
    """Aggregate completed tasks and exact child-local current orders into master-plan coordinates."""
    plan = parallel.get("plan") if isinstance(parallel.get("plan"), list) else []
    tracks = parallel.get("tracks") if isinstance(parallel.get("tracks"), list) else []
    by_name = {track.get("name"): track for track in tracks if isinstance(track, dict)}
    completed = []
    current_orders = []
    order_map = parallel.get("order_map") if isinstance(parallel.get("order_map"), dict) else {}
    for task in plan:
        if not isinstance(task, dict) or not isinstance(task.get("order"), int):
            continue
        track = by_name.get(task.get("track")) or {}
        if track.get("status") in {"merged", "cleaned"}:
            completed.append({"order": task["order"],
                              "sha": str(track.get("tip") or parallel.get("expected_integration_sha") or ""),
                              "round": 0, "fleet": True})
    for track_name, track in by_name.items():
        if track.get("status") in {"merged", "cleaned", "failed"}:
            continue
        child_name = str(track.get("child_workspace") or "")
        child = None
        if loop_mod.valid_workspace_name(child_name):
            child, _ = read_state(child_name, repair=False)
        if (child and child.get("workspace_kind") == "fleet-child" and
                child.get("fleet_run_id") == parallel.get("run_id") and
                child.get("fleet_parent") == name and child.get("track") == track_name and
                child.get("fleet_parent_session_id") == (parallel.get("loop") or {}).get("session_id")):
            local_order = child.get("current_order")
            mapped = (order_map.get(track_name) or {}).get(str(local_order))
            if isinstance(mapped, int):
                current_orders.append(mapped)
                continue
        track_orders = [task["order"] for task in plan
                        if isinstance(task, dict) and task.get("track") == track_name and
                        isinstance(task.get("order"), int)]
        if track_orders and track.get("status") in {"pending", "running", "repairing"}:
            current_orders.append(min(track_orders))
    return completed, sorted(set(current_orders))


def fleet_integration_issues(parallel):
    """Project immutable Fleet rollback audit into navigable synthetic issues."""
    tracks = [track for track in parallel.get("tracks") or [] if isinstance(track, dict)]
    by_name = {track.get("name"): track for track in tracks}
    issues = []
    for track in tracks:
        message = track.get("last_integration_error")
        if isinstance(message, str) and message:
            issues.append({"round": 0, "text": message,
                           "where": "fleet-integration-rollback", "source": "fleet",
                           "synthetic": True, "read_only": True,
                           "resolved": track.get("status") in {"merged", "cleaned"},
                           "track": track.get("name"),
                           "child_workspace": track.get("child_workspace")})
    transaction = parallel.get("merge_tx")
    if isinstance(transaction, dict) and (
            transaction.get("stage") in {"rollback-prepared", "rolled-back"} or
            isinstance(transaction.get("validation_error"), str)):
        track = by_name.get(transaction.get("track")) or {}
        message = transaction.get("validation_error")
        if not isinstance(message, str) or not message:
            message = f"integration rollback pending ({transaction.get('stage')})"
        existing = next((issue for issue in issues
                         if issue["track"] == transaction.get("track") and
                         issue["text"] == message), None)
        if existing is not None:
            existing["resolved"] = track.get("status") in {"merged", "cleaned"}
        else:
            issues.append({"round": 0, "text": message,
                           "where": "fleet-integration-rollback", "source": "fleet",
                           "synthetic": True, "read_only": True,
                           "resolved": track.get("status") in {"merged", "cleaned"},
                           "track": transaction.get("track"),
                           "child_workspace": track.get("child_workspace")})
    return issues


def track_child_state(parent_name: str, parallel, track):
    """Read a track child only when all persisted fleet identities match exactly."""
    child_name = str(track.get("child_workspace") or "")
    track_name = track.get("name")
    if not loop_mod.valid_workspace_name(child_name) or not isinstance(track_name, str):
        return None
    child, error = read_state(child_name, repair=False)
    if (error or not child or child.get("workspace_kind") != "fleet-child" or
            child.get("fleet_run_id") != parallel.get("run_id") or
            child.get("fleet_parent") != parent_name or child.get("track") != track_name or
            child.get("fleet_parent_session_id") !=
            (parallel.get("loop") or {}).get("session_id")):
        return None
    return child


def project_state_for_ui(name):
    """UI state projection：保留 coordinator truth，僅為 parent 聚合可讀診斷與 parallel metadata。"""
    st, error = read_state(name, repair=False)
    if error or not st or st.get("workspace_kind") != "fleet-parent":
        return st, error
    parallel = read_parallel_run(name)
    if parallel.get("read_error"):
        projected = dict(st)
        projected["parallel_run_error"] = parallel["read_error"]
        return projected, None
    projected = dict(st)
    projected["parallel_run"] = parallel
    projected["parallel_error"] = parallel.get("error")
    projected["parallel_stop_reason"] = parallel.get("stop_reason")
    projected["parallel_track_events"] = [
        {"track": track.get("name"), "child_workspace": track.get("child_workspace"),
         "event_history": track.get("event_history") or []}
        for track in parallel.get("tracks") or [] if isinstance(track, dict)]
    if isinstance(parallel.get("config"), dict):
        projected["config"] = parallel["config"]
    if isinstance(parallel.get("plan"), list):
        projected["plan"] = parallel["plan"]
        projected["plan_version"] = parallel.get("plan_generation", projected.get("plan_version", 0))
    completed, current_orders = parallel_progress_projection(name, parallel)
    projected["completed"] = completed
    projected["current_order"] = current_orders[0] if current_orders else None
    projected["parallel_current_orders"] = current_orders
    issues = []
    for track in parallel.get("tracks") or []:
        diagnostics = track.get("diagnostics") or {}
        child_issues = diagnostics.get("issues") or []
        child_name = str(track.get("child_workspace") or "")
        if not child_issues:
            child_state = track_child_state(name, parallel, track)
            child_issues = (child_state or {}).get("issues") or []
        issues.extend({**issue, "track": track.get("name"), "child_workspace": child_name}
                      for issue in child_issues if isinstance(issue, dict))
    issues.extend(fleet_integration_issues(parallel))
    projected["issues"] = issues
    # Parent 聚合 issue 是唯讀診斷，不可用 standalone watermark 假裝已讀。
    projected["issues_acknowledged_round"] = -1
    return projected, None


def require_fleet_run_id(handler, body, fleet):
    """所有 parent mutation 綁定 immutable run_id，舊頁面不能操作同名重建後的新 run。"""
    received = body.get("run_id")
    expected = fleet.get("run_id")
    if not isinstance(received, str) or received != expected:
        handler._err("parallel run 已更新或畫面過期，請重新載入後再操作", 409)
        return False
    return True


def require_workspace_generation(handler, body, state):
    """Bind a standalone browser mutation to the exact immutable workspace generation."""
    received = body.get("workspace_generation")
    expected = state.get("workspace_generation")
    if (not isinstance(received, str) or
            re.fullmatch(r"[0-9a-f]{32}", received) is None or received != expected):
        handler._err("workspace 已更新或同名重建，請重新載入後再操作", 409)
        return False
    return True


def require_fleet_plan_identity(handler, body, fleet):
    """Approval is a plan-specific mutation; run-id alone cannot protect a stale browser tab."""
    if fleet.get("phase") != "awaiting-approval":
        return True
    generation = body.get("plan_generation")
    digest = body.get("plan_sha256")
    if (not isinstance(generation, int) or isinstance(generation, bool) or
            generation != fleet.get("plan_generation") or not isinstance(digest, str) or
            digest != fleet.get("plan_sha256")):
        handler._err("parallel plan 已更新或核准畫面過期，請重新載入後再運行", 409)
        return False
    return True


def _load_state_or_err(handler, name, *, repair=False):
    """讀 workspace state；失敗時透過 handler 送出 _err JSON 並回傳 None（同 _ws_dir 慣例）。"""
    st, err = read_state(name, repair=repair)
    if err:
        handler._err(err)
        return None
    return st


def read_report(name):
    """REPORT.md 唯讀投影:只在全部任務收斂完成後由 loop 產生,不存在回明確 error。"""
    if not loop_mod.valid_workspace_name(name):
        return {"error": f"workspace 名稱不合法：{loop_mod.WORKSPACE_NAME_RULE}"}
    try:
        report = loop_mod.workspace_file(safe_workspace_dir(name) / "REPORT.md", "REPORT.md")
        fd = loop_mod._open_regular(report, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
            return {"content": stream.read()}
    except FileNotFoundError:
        return {"error": "REPORT.md 不存在——全部任務收斂完成後才會由 loop 產生"}
    except (OSError, ValueError) as e:
        return {"error": f"REPORT.md 讀取失敗:{e}"}


FLEET_HISTORY_TAIL = 16 * 1024  # 每個 workspace 事件流尾段上限
FLEET_METRICS_TAIL = 512 * 1024  # 足以 bounded 掃描單 workspace 近期 500 輪
FLEET_METRICS_LIMIT = 100
FLEET_AGGREGATE_LIMIT = 500      # 全 workspace 合併後只取時間最新 500 筆
FLEET_HISTORY_SSE_INTERVAL = 2.0  # 事件流只在歷史有變時推送，避免每圈重送整段尾端
ANOMALY_ID_RE = loop_mod.ANOMALY_ID_RE


def aggregate_fleet_round_metrics(samples, *, history_truncated=False):
    """依時間合併各 workspace，精確聚合全體最新 500 筆。"""
    samples = sorted(samples, key=lambda sample: (
        sample["timestamp"], sample["workspace"], sample["round"]))[-FLEET_AGGREGATE_LIMIT:]
    durations = sorted(sample["seconds"] for sample in samples)

    def percentile(percent):
        """以整數 nearest-rank 計算 fleet percentile，空樣本回 None。"""
        if not durations:
            return None
        index = max(0, (len(durations) * percent + 99) // 100 - 1)
        return durations[index]

    timeout_count = sum(1 for sample in samples if sample["timed_out"])
    missing_done_count = sum(1 for sample in samples if sample.get("missing_done", False))
    slowest = max(samples, key=lambda sample: (
        sample["seconds"], sample["workspace"], sample["round"])) if samples else None
    return {
        "limit": FLEET_AGGREGATE_LIMIT,
        "workspace_count": len({sample["workspace"] for sample in samples}),
        "sample_count": len(samples),
        "average_seconds": round(sum(durations) / len(durations), 3) if durations else None,
        "p50_seconds": percentile(50),
        "p95_seconds": percentile(95),
        "max_seconds": slowest["seconds"] if slowest else None,
        "slowest_round": slowest["round"] if slowest else None,
        "slowest_workspace": slowest["workspace"] if slowest else None,
        "timeout_count": timeout_count,
        "timeout_rate_pct": round(timeout_count / len(samples) * 100, 1) if samples else 0,
        "missing_done_count": missing_done_count,
        "missing_done_rate_pct": round(missing_done_count / len(samples) * 100, 1) if samples else 0,
        "history_truncated": bool(history_truncated),
    }


def read_fleet_observability():
    """一次 bounded read 投影事件尾段、各 workspace 與全體效能摘要。"""
    out = []
    all_samples = []
    any_truncated = False
    if not ROOT.is_dir():
        return {"entries": out, "metrics": aggregate_fleet_round_metrics([])}
    for d in sorted(ROOT.iterdir()):
        if not loop_mod.valid_workspace_name(d.name) or d.is_symlink() or not d.is_dir():
            continue
        try:
            history = loop_mod.workspace_file(d / "history.log", "history.log")
            fd = loop_mod._open_regular(history, os.O_RDONLY)
            with os.fdopen(fd, "rb", closefd=True) as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                metrics_start = max(0, size - FLEET_METRICS_TAIL)
                stream.seek(metrics_start)
                metrics_data = stream.read(FLEET_METRICS_TAIL)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            continue
        if metrics_start:
            newline = metrics_data.find(b"\n")
            metrics_data = metrics_data[newline + 1:] if newline >= 0 else b""
        metrics_text = metrics_data.decode("utf-8", errors="replace")
        event_data = metrics_data[-FLEET_HISTORY_TAIL:]
        tail = event_data.decode("utf-8", errors="replace")
        if size > FLEET_HISTORY_TAIL:
            newline = tail.find("\n")
            tail = tail[newline + 1:] if newline != -1 else tail
        aggregate_metrics = loop_mod.round_metrics_from_history(
            metrics_text, FLEET_AGGREGATE_LIMIT, history_truncated=metrics_start > 0)
        metrics = loop_mod.round_metrics_from_history(
            metrics_text, FLEET_METRICS_LIMIT, history_truncated=metrics_start > 0)
        samples = aggregate_metrics["samples"]
        metrics.pop("samples")
        all_samples.extend({**sample, "workspace": d.name} for sample in samples)
        any_truncated = any_truncated or metrics["history_truncated"]
        out.append({"name": d.name, "data": tail, "metrics": metrics})
    return {
        "entries": out,
        "metrics": aggregate_fleet_round_metrics(
            all_samples, history_truncated=any_truncated),
    }


def read_fleet_history():
    """相容的 workspace history list API；全體摘要由 sibling projection 提供。"""
    return read_fleet_observability()["entries"]


def read_preserved_anomaly_metadata(workspace_dir: Path):
    """安全讀取單 workspace 最多 100 份異常 log 索引。"""
    anomaly_dir = loop_mod.workspace_directory(
        workspace_dir / "logs" / "anomalies", "異常 log 目錄")
    if anomaly_dir is None:
        return []
    records = []
    metadata_paths = [path for path in anomaly_dir.glob("*.json")
                      if ANOMALY_ID_RE.fullmatch(path.stem)]
    for path in sorted(metadata_paths, reverse=True)[:loop_mod.ANOMALY_LOG_MAX_COUNT]:
        try:
            fd = loop_mod._open_regular(path, os.O_RDONLY)
            with os.fdopen(fd, "rb", closefd=True) as stream:
                size = os.fstat(stream.fileno()).st_size
                if size > 64 * 1024:
                    continue
                metadata = json.loads(stream.read().decode("utf-8"))
        except (FileNotFoundError, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (not isinstance(metadata, dict) or metadata.get("id") != path.stem or
                metadata.get("log_file") != f"{path.stem}.log" or
                not isinstance(metadata.get("round"), int) or
                not isinstance(metadata.get("timestamp"), str)):
            continue
        records.append(metadata)
    return records


def anomaly_records_for_workspace(workspace_dir: Path, *, run="current", round_limit=100):
    """從指定 run 的 bounded history 取出未回 DONE 異常並連結保存的 log metadata。"""
    history_name = "history.log" if run == "current" else "history.log.1"
    metrics = loop_mod.read_round_metrics(workspace_dir / history_name, round_limit)
    preserved = {
        (item["timestamp"], item["round"]): item
        for item in read_preserved_anomaly_metadata(workspace_dir)
    }
    records = []
    for sample in metrics["samples"]:
        if not sample["missing_done"]:
            continue
        saved = preserved.get((sample["timestamp"], sample["round"]))
        records.append({
            **sample,
            "workspace": workspace_dir.name,
            "log_id": saved.get("id") if saved else None,
            "log_truncated": bool(saved.get("truncated")) if saved else False,
        })
    return records


def read_anomaly_records(workspace_dir: Path = None, *, run="current"):
    """列出與 workspace 100 輪／Overview 全域 500 輪統計一致的異常，最多回 100 筆。"""
    if workspace_dir is not None:
        records = anomaly_records_for_workspace(workspace_dir, run=run, round_limit=100)
    else:
        samples = []
        if ROOT.is_dir():
            for directory in sorted(ROOT.iterdir()):
                if (not loop_mod.valid_workspace_name(directory.name) or directory.is_symlink() or
                        not directory.is_dir()):
                    continue
                try:
                    metrics = loop_mod.read_round_metrics(
                        directory / "history.log", FLEET_AGGREGATE_LIMIT)
                    preserved = {
                        (item["timestamp"], item["round"]): item
                        for item in read_preserved_anomaly_metadata(directory)
                    }
                except (OSError, ValueError):
                    continue
                for sample in metrics["samples"]:
                    saved = preserved.get((sample["timestamp"], sample["round"]))
                    samples.append({
                        **sample,
                        "workspace": directory.name,
                        "log_id": saved.get("id") if saved else None,
                        "log_truncated": bool(saved.get("truncated")) if saved else False,
                    })
        samples = sorted(samples, key=lambda item: (
            item["timestamp"], item["workspace"], item["round"]))[-FLEET_AGGREGATE_LIMIT:]
        records = [sample for sample in samples if sample["missing_done"]]
    records.sort(key=lambda item: (
        item["timestamp"], item["workspace"], item["round"]), reverse=True)
    return {
        "limit": loop_mod.ANOMALY_LOG_MAX_COUNT,
        "total_count": len(records),
        "records": records[:loop_mod.ANOMALY_LOG_MAX_COUNT],
    }


def read_preserved_anomaly_log(workspace_dir: Path, anomaly_id: str):
    """依嚴格 anomaly ID 讀取保存 log 尾段，拒絕任意路徑與 symlink。"""
    if not isinstance(anomaly_id, str) or not ANOMALY_ID_RE.fullmatch(anomaly_id):
        raise ValueError("異常 log id 不合法")
    metadata = next((item for item in read_preserved_anomaly_metadata(workspace_dir)
                     if item["id"] == anomaly_id), None)
    if metadata is None:
        raise FileNotFoundError("異常 log 不存在或已超過保留上限")
    log_path = workspace_dir / "logs" / "anomalies" / f"{anomaly_id}.log"
    fd = loop_mod._open_regular(log_path, os.O_RDONLY)
    with os.fdopen(fd, "rb", closefd=True) as stream:
        data = stream.read(loop_mod.ANOMALY_LOG_MAX_BYTES + 1)
    if len(data) > loop_mod.ANOMALY_LOG_MAX_BYTES:
        data = data[-loop_mod.ANOMALY_LOG_MAX_BYTES:]
        metadata = {**metadata, "truncated": True}
    return {
        "id": anomaly_id,
        "workspace": workspace_dir.name,
        "round": metadata["round"],
        "timestamp": metadata["timestamp"],
        "truncated": bool(metadata.get("truncated")),
        "data": data.decode("utf-8", errors="replace"),
    }


def read_goal(name):
    """goal 唯讀投影:從 state.config 記錄的 repo+goal 相對路徑讀人類真相,不寫回。"""
    st, err = read_state(name, repair=False)
    if err:
        return {"error": err}
    c = st.get("config") or {}
    repo, goal_rel = c.get("repo"), c.get("goal") or "goal.md"
    # repo 必須是非空字串:壞 state 塞非字串會讓 Path(repo) 拋 TypeError 變成未受控 500
    if not isinstance(repo, str) or not repo:
        return {"error": "state 缺 repo 設定(舊版 state)——用啟動表單跑過一次後即可檢視 goal"}
    try:
        goal_path = loop_mod.repo_relative_path(Path(repo).expanduser(), goal_rel)
    except ValueError as e:
        return {"error": f"goal 路徑不合法:{e}"}
    try:
        # repo_relative_path 只驗證不開檔;驗證與 read 之間 goal 可能被換成 symlink(TOCTOU)。
        # 與 read_report 相同用 O_NOFOLLOW 開檔,把驗證與讀取收斂到同一個 syscall。
        # UnicodeDecodeError 是 ValueError 子類,一併涵蓋。
        fd = loop_mod._open_regular(goal_path, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
            content = stream.read()
    except FileNotFoundError:
        return {"error": f"goal 檔不存在:{goal_path}(repo 被移走或 goal 被刪?)"}
    except (OSError, ValueError) as e:
        return {"error": f"goal 讀取失敗:{e}"}
    projection = {"content": content, "path": str(goal_path),
                  "goal_changed": bool(st.get("goal_changed"))}
    if not projection["goal_changed"]:
        return projection
    previous_hash = st.get("goal_previous_hash")
    projection["previous_hash"] = previous_hash
    if not isinstance(previous_hash, str):
        projection["diff_error"] = "此 workspace 由舊版建立，沒有保留舊 goal hash"
        return projection
    try:
        history = subprocess.run(
            ["git", "-C", str(Path(repo).expanduser()), "log", "--format=%H", "--max-count=200",
             "--", goal_rel], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        projection["diff_error"] = f"無法查詢 goal Git 歷史:{e}"
        return projection
    if history.returncode != 0:
        projection["diff_error"] = "無法查詢 goal Git 歷史"
        return projection
    previous_content = None
    for commit in history.stdout.splitlines():
        try:
            shown = subprocess.run(
                ["git", "-C", str(Path(repo).expanduser()), "show", f"{commit}:{goal_rel}"],
                capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if shown.returncode == 0 and loop_mod.sha256_bytes(shown.stdout) == previous_hash:
            try:
                previous_content = shown.stdout.decode("utf-8")
            except UnicodeDecodeError:
                projection["diff_error"] = "舊 goal 不是 UTF-8，無法顯示差異"
                return projection
            break
    if previous_content is None:
        projection["diff_error"] = "最近 200 筆 goal Git 歷史中找不到計畫基準版本"
        return projection
    projection["previous_content"] = previous_content
    projection["diff"] = "".join(difflib.unified_diff(
        previous_content.splitlines(keepends=True), content.splitlines(keepends=True),
        fromfile=f"{goal_rel}（計畫基準）", tofile=f"{goal_rel}（目前）"))
    return projection


def read_prompt(name):
    """最近一輪送出的 prompt 唯讀投影(loop 只保留最近幾份,取 round 編號最大者)。"""
    if not loop_mod.valid_workspace_name(name):
        return {"error": f"workspace 名稱不合法：{loop_mod.WORKSPACE_NAME_RULE}"}

    def round_num(path):
        """由 prompt 檔名擷取 round；不符格式時排到最後。"""
        m = re.search(r"round-(\d+)", path.name)
        return int(m.group(1)) if m else -1

    try:
        prompts_dir = loop_mod.workspace_directory(safe_workspace_dir(name) / "prompts", "prompts")
        if prompts_dir is None:
            return {"error": "尚無 prompt 紀錄——loop 送出第一輪後才會出現"}
        candidates = (path for path in prompts_dir.glob("round-*.md")
                      if loop_mod.workspace_file(path, "round prompt") is not None)
        latest = max(candidates,
                     key=round_num, default=None)
        if latest is None:
            return {"error": "尚無 prompt 紀錄——loop 送出第一輪後才會出現"}
        fd = loop_mod._open_regular(latest, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
            content = stream.read()
        return {"content": content,
                "round": round_num(latest), "file": latest.name}
    except (OSError, ValueError) as e:
        return {"error": f"prompt 讀取失敗:{e}"}


def workspace_console_log(name, message):
    """將 Dashboard 操作與 loop/Agent 寫進同一條 workspace console 時序。"""
    line = f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜{message}"
    print(line, flush=True)
    if not loop_mod.valid_workspace_name(name):
        return
    try:
        workspace_dir = safe_workspace_dir(name)
        loop_mod.ensure_real_directory(workspace_dir, "workspace 目錄")
        loop_mod.append_console(workspace_dir / "console.log", line)
    except (OSError, ValueError) as e:
        print(f"⚠ console.log 寫入失敗:{e}", flush=True)


def ws_running(name, st=None):
    """workspace 是否執行中:本 dashboard 的 job 或 state.json 記錄的外部 pid。"""
    with JOBS_LOCK:
        j = JOBS.get(name)
    if j is not None and j.alive():
        return True
    if st is None:
        st, _ = read_state(name, repair=False)
    return bool(st) and loop_pid_alive((st.get("loop") or {}).get("pid"))


class RuntimeStopIdentityError(RuntimeError):
    """A stop request cannot prove that marker/process truth still names its target."""

    def __init__(self, message, code=409):
        super().__init__(message)
        self.code = code


def _resolved_stop_repo(value) -> str:
    try:
        return str(Path(str(value or "")).expanduser().resolve()) if value else ""
    except (OSError, RuntimeError):
        return ""


def _validate_stop_active_marker(marker, coordinator, snapshot):
    """Validate one root/session-bound independent runtime group for stop cleanup."""
    if marker is None:
        return None
    integer_fields = ("owner_pid", "pid", "pgid", "sid")
    if (marker.get("schema_version") != loop_mod.ACTIVE_RUNTIME_SCHEMA_VERSION or
            marker.get("kind") not in {"agent", "validator"} or
            marker.get("workspace_name") != coordinator["workspace_name"] or
            marker.get("workspace_root") != coordinator["workspace_root"] or
            _resolved_stop_repo(marker.get("repo")) !=
            _resolved_stop_repo(coordinator.get("repo")) or
            marker.get("workspace_generation") != coordinator["workspace_generation"] or
            marker.get("session_id") != coordinator["session_id"] or
            marker.get("owner_pid") != coordinator["pid"] or
            marker.get("owner_started") != coordinator["started"] or
            marker.get("owner_command") != coordinator["command"] or
            any(not isinstance(marker.get(field), int) or isinstance(marker.get(field), bool) or
                marker[field] <= 0 for field in integer_fields) or
            marker["pid"] != marker["pgid"] or marker["pid"] != marker["sid"] or
            not isinstance(marker.get("started"), str) or not marker["started"] or
            not isinstance(marker.get("command"), str) or not marker["command"]):
        raise RuntimeStopIdentityError("active runtime marker 與目前 coordinator 身分不符")
    leader = snapshot.get(marker["pid"])
    if leader is not None and not (
            leader.get("pgid") == marker["pgid"] and leader.get("sid") == marker["sid"] and
            leader.get("started") == marker["started"] and
            leader.get("command") == marker["command"]):
        raise RuntimeStopIdentityError("active runtime PID/PGID 已被 replacement 重用")
    return dict(marker)


def freeze_workspace_stop_identity(name: str, repo, pid: int, *, state=None,
                                   require_coordinator_marker=True):
    """Freeze root/coordinator/runtime truth before a Dashboard stop sends any signal."""
    try:
        entry = safe_workspace_dir(name)
        loop_mod.ensure_real_directory(entry, "stop workspace")
    except (OSError, ValueError) as error:
        if require_coordinator_marker:
            raise RuntimeStopIdentityError(f"workspace root 身分無法確認：{error}") from error
        return None
    snapshot = _process_snapshot()
    if snapshot is None:
        raise RuntimeStopIdentityError("無法取得 stop process snapshot", 500)
    try:
        coordinator = _read_runtime_marker(entry, loop_mod.COORDINATOR_RUNTIME_FILE)
    except RuntimeError as error:
        raise RuntimeStopIdentityError(str(error)) from error
    if coordinator is None:
        if require_coordinator_marker:
            raise RuntimeStopIdentityError("缺少目前 workspace root 的 coordinator runtime marker")
        try:
            active = _read_runtime_marker(entry, loop_mod.ACTIVE_RUNTIME_FILE)
        except RuntimeError as error:
            raise RuntimeStopIdentityError(str(error)) from error
        if active is not None:
            raise RuntimeStopIdentityError("active runtime 存在但 coordinator marker 缺失")
        return None
    expected_repo = _resolved_stop_repo(repo)
    if (coordinator.get("schema_version") != loop_mod.ACTIVE_RUNTIME_SCHEMA_VERSION or
            coordinator.get("workspace_name") != name or
            coordinator.get("workspace_root") != str(ROOT.resolve()) or
            _resolved_stop_repo(coordinator.get("repo")) != expected_repo or
            coordinator.get("pid") != int(pid) or
            not isinstance(coordinator.get("started"), str) or not coordinator["started"] or
            not isinstance(coordinator.get("command"), str) or not coordinator["command"] or
            _coordinator_workspace_name(coordinator["command"]) != name or
            _resolved_stop_repo(_command_option(coordinator["command"], "--repo")) != expected_repo or
            re.fullmatch(r"[0-9a-f]{32}", str(coordinator.get("workspace_generation") or "")) is None or
            re.fullmatch(r"[0-9a-f]{32}", str(coordinator.get("session_id") or "")) is None):
        raise RuntimeStopIdentityError("coordinator runtime marker 與 workspace/root/repo/PID 不符")
    if state is not None:
        loop_state = state.get("loop") if isinstance(state.get("loop"), dict) else {}
        try:
            coordinator_argv = shlex.split(coordinator["command"])
        except ValueError:
            coordinator_argv = []
        standalone_command = any(
            coordinator_argv[index:index + 2] == ["-m", "engine.loop"]
            for index in range(max(0, len(coordinator_argv) - 1))
        ) or any(token == "engine/loop.py" or token.endswith("/engine/loop.py")
                 for token in coordinator_argv)
        if (state.get("workspace_kind") != "standalone" or
                not standalone_command or
                state.get("workspace_generation") != coordinator["workspace_generation"] or
                loop_state.get("session_id") != coordinator["session_id"] or
                loop_state.get("pid") != coordinator["pid"] or
                _resolved_stop_repo((state.get("config") or {}).get("repo")) != expected_repo):
            raise RuntimeStopIdentityError("coordinator marker 與目前 standalone state session 不符")
    process = snapshot.get(coordinator["pid"])
    if process is not None and not _same_process_instance(coordinator, process):
        raise RuntimeStopIdentityError("coordinator PID/start/command 已被 replacement 重用")
    frozen = {"entry": entry, "coordinator": dict(coordinator),
              "coordinator_alive": process is not None}
    try:
        active = _read_runtime_marker(entry, loop_mod.ACTIVE_RUNTIME_FILE)
    except RuntimeError as error:
        raise RuntimeStopIdentityError(str(error)) from error
    frozen["active"] = _validate_stop_active_marker(active, coordinator, snapshot)
    return frozen


def _runtime_group_status(marker):
    """Return alive/gone/stale for one frozen PGID without trusting numeric reuse."""
    if marker is None:
        return "gone", {}
    snapshot = _process_snapshot()
    if snapshot is None:
        return "snapshot-error", {}
    leader = snapshot.get(marker["pid"])
    if leader is not None and not (
            leader.get("pgid") == marker["pgid"] and leader.get("sid") == marker["sid"] and
            leader.get("started") == marker["started"] and
            leader.get("command") == marker["command"]):
        return "stale", {}
    members = {process_pid: process for process_pid, process in snapshot.items()
               if process.get("pgid") == marker["pgid"] and
               process.get("sid") == marker["sid"]}
    return ("alive" if members else "gone"), members


def freeze_job_process_group(process):
    """Freeze a Dashboard-owned coordinator group before durable markers exist."""
    snapshot = _process_snapshot()
    if snapshot is None:
        raise RuntimeStopIdentityError("無法取得 markerless Job process snapshot", 500)
    leader = snapshot.get(process.pid)
    if leader is None:
        raise RuntimeStopIdentityError(
            "markerless Job leader 未出現在 process snapshot，拒絕用 numeric PID 發 signal")
    if (leader.get("pgid") != process.pid or leader.get("sid") != process.pid or
            not leader.get("started") or not leader.get("command")):
        raise RuntimeStopIdentityError("markerless Job 未持有可證明的獨立 process group")
    roster = {
        pid: dict(item) for pid, item in snapshot.items()
        if item.get("pgid") == process.pid and item.get("sid") == process.pid
    }
    if process.pid not in roster:
        raise RuntimeStopIdentityError("markerless Job group 缺少 coordinator leader")
    return {"pid": process.pid, "pgid": process.pid, "sid": process.pid,
            "leader": dict(leader), "roster": roster}


def _markerless_job_group_status(frozen):
    if frozen is None:
        return "gone", {}
    snapshot = _process_snapshot()
    if snapshot is None:
        return "snapshot-error", {}
    leader = snapshot.get(frozen["pid"])
    if leader is not None and not _same_process_instance(frozen["leader"], leader):
        return "stale", {}
    current = {
        pid: item for pid, item in snapshot.items()
        if item.get("pgid") == frozen["pgid"] and item.get("sid") == frozen["sid"]
    }
    for pid, item in current.items():
        expected = frozen["roster"].get(pid)
        if expected is None or not _same_process_instance(expected, item):
            return "stale", {}
    return ("alive" if current else "gone"), current


def signal_markerless_job_group(frozen):
    """Send initial graceful stop only while the whole captured group is still exact."""
    status, _members = _markerless_job_group_status(frozen)
    if status == "gone":
        return True, None, 200
    if status != "alive":
        return False, "markerless Job group 身分已更新", 500 if status == "snapshot-error" else 409
    try:
        os.killpg(frozen["pgid"], signal.SIGINT)
    except ProcessLookupError:
        status, _members = _markerless_job_group_status(frozen)
        return ((True, None, 200) if status == "gone" else
                (False, "markerless Job group 無法證明已停止", 409 if status == "stale" else 500))
    except (PermissionError, OSError) as error:
        return False, f"markerless Job group SIGINT 失敗：{error}", 500
    return True, None, 200


def cleanup_markerless_job_group(frozen, *, force_seconds=1.0):
    """Kill only frozen member instances; never guess from a reusable numeric PGID."""
    status, members = _markerless_job_group_status(frozen)
    if status == "gone":
        return True, None, 200
    if status != "alive":
        return False, "markerless Job group 含未知或 reused member", 500 if status == "snapshot-error" else 409
    for pid in sorted(members, reverse=True):
        # Recheck this exact member immediately before every force signal.
        snapshot = _process_snapshot()
        if snapshot is None:
            return False, "markerless member force 前 snapshot 失敗", 500
        current = snapshot.get(pid)
        expected = frozen["roster"].get(pid)
        if current is None:
            continue
        if (expected is None or not _same_process_instance(expected, current) or
                current.get("pgid") != frozen["pgid"] or
                current.get("sid") != frozen["sid"]):
            return False, "markerless member PID 已被 replacement 重用", 409
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except (PermissionError, OSError) as error:
            return False, f"markerless member force 失敗：{error}", 500
    deadline = time.monotonic() + max(0.0, force_seconds)
    while time.monotonic() < deadline:
        status, _members = _markerless_job_group_status(frozen)
        if status == "gone":
            return True, None, 200
        if status != "alive":
            return False, "markerless Job group force 後出現 replacement", 409
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    status, _members = _markerless_job_group_status(frozen)
    return ((True, None, 200) if status == "gone" else
            (False, "markerless Job group force 後仍存活",
             500 if status == "alive" else 409))


def cleanup_frozen_runtime_group(frozen, *, grace_seconds=0.5, force_seconds=1.0):
    """Stop and prove empty the independent agent/validator group frozen before coordinator stop."""
    if frozen is None:
        return True, None, 200
    marker = frozen.get("active")
    try:
        current_coordinator = _read_runtime_marker(
            frozen["entry"], loop_mod.COORDINATOR_RUNTIME_FILE)
        if (current_coordinator is not None and
                current_coordinator != frozen["coordinator"]):
            return False, "coordinator runtime marker 已被 replacement 更新", 409
        current = _read_runtime_marker(frozen["entry"], loop_mod.ACTIVE_RUNTIME_FILE)
        if current is not None:
            snapshot = _process_snapshot()
            if snapshot is None:
                return False, "runtime cleanup 無法取得 process snapshot", 500
            current = _validate_stop_active_marker(
                current, frozen["coordinator"], snapshot)
            if marker is not None and current != marker:
                return False, "active runtime marker 已被 replacement 更新", 409
            marker = marker or current
    except RuntimeStopIdentityError as error:
        return False, str(error), error.code
    except RuntimeError as error:
        return False, str(error), 409
    if marker is None:
        return True, None, 200
    status, _members = _runtime_group_status(marker)
    if status == "gone":
        return True, None, 200
    if status != "alive":
        return False, "active runtime group 身分已更新或無法確認", 500 if status == "snapshot-error" else 409
    # Every signal is preceded by an exact leader/group identity check.
    try:
        os.killpg(marker["pgid"], signal.SIGINT)
    except ProcessLookupError:
        status, _members = _runtime_group_status(marker)
        return ((True, None, 200) if status == "gone" else
                (False, "active runtime group 無法證明已停止", 409 if status == "stale" else 500))
    except (PermissionError, OSError) as error:
        return False, f"無法停止 active runtime group：{error}", 500
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        status, _members = _runtime_group_status(marker)
        if status == "gone":
            return True, None, 200
        if status != "alive":
            return False, "active runtime group 在 grace 期間被 replacement 重用", 409
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    status, _members = _runtime_group_status(marker)
    if status == "gone":
        return True, None, 200
    if status != "alive":
        return False, "active runtime group 在 force 前被 replacement 重用", 409
    try:
        os.killpg(marker["pgid"], signal.SIGKILL)
    except ProcessLookupError:
        status, _members = _runtime_group_status(marker)
        return ((True, None, 200) if status == "gone" else
                (False, "active runtime group force 後無法證明已停止",
                 409 if status == "stale" else 500))
    except (PermissionError, OSError) as error:
        return False, f"無法強制停止 active runtime group：{error}", 500
    deadline = time.monotonic() + max(0.0, force_seconds)
    while time.monotonic() < deadline:
        status, _members = _runtime_group_status(marker)
        if status == "gone":
            return True, None, 200
        if status != "alive":
            return False, "active runtime group 在 force 後被 replacement 重用", 409
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    status, _members = _runtime_group_status(marker)
    return ((True, None, 200) if status == "gone" else
            (False, "active runtime group 仍存活", 500 if status == "alive" else 409))


def stop_all_jobs():
    """Dashboard shutdown proves every owned coordinator/runtime group stopped."""
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    unfinished = []
    if jobs:
        print(f"⏹ 關閉 dashboard:停止並驗證 {len(jobs)} 個 loop …", flush=True)
        results = {}

        def stop_one(job):
            results[job.name] = job.stop(wait=True)

        threads = [threading.Thread(target=stop_one, args=(job,), daemon=True)
                   for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=12)
        unfinished = [job.name for job, thread in zip(jobs, threads) if thread.is_alive()]
        failures = [job.name for job, thread in zip(jobs, threads)
                    if thread.is_alive() or not results.get(job.name)]
        if failures:
            print(f"⚠ Dashboard jobs 未能個別證明清場：{', '.join(failures)}", flush=True)
    # Fleet children and hard-crash orphans are not necessarily descendants of the parent
    # Job process group.  Reuse the root-scoped marker sweep as the final shutdown proof.
    try:
        stop_workspace_coordinators(grace_seconds=1.0, force_seconds=1.0)
    except RuntimeError as error:
        raise RuntimeError(f"Dashboard 關閉後 root-scoped runtime 清場失敗：{error}") from error
    if unfinished:
        raise RuntimeError(
            "Dashboard stop thread 逾時且仍可能在背景發送 signal：" + ", ".join(unfinished))


def load_config():
    """讀取團隊版 + 個人版；個人版只允許覆蓋 PERSONAL_CONFIG_KEYS。"""
    try:
        PERSONAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": f"個人設定目錄無法建立:{e}"}

    def read_json(path, label):
        """安全讀取單一設定 JSON object；缺檔與格式錯誤分開回報。"""
        try:
            raw = loop_mod.read_regular_text(path, label)
        except FileNotFoundError:
            return None, None
        except (OSError, ValueError, UnicodeDecodeError) as e:
            return None, f"{label}讀取失敗:{e}"
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as e:
            return None, f"{label}解析失敗:{e}"
        if not isinstance(value, dict):
            return None, f"{label}頂層必須是 JSON object"
        return value, None

    if CONFIG_OVERRIDE:
        personal, error = read_json(PERSONAL_CONFIG_PATH, f"覆寫設定檔 {PERSONAL_CONFIG_PATH.name}")
        if error:
            return {"error": error}
        if personal is None:
            try:
                loop_mod.atomic_write_bytes(
                    PERSONAL_CONFIG_PATH,
                    json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2).encode("utf-8"),
                )
            except (OSError, ValueError) as e:
                return {"error": f"覆寫設定檔無法建立:{e}"}
            personal, error = read_json(PERSONAL_CONFIG_PATH, f"覆寫設定檔 {PERSONAL_CONFIG_PATH.name}")
            if error:
                return {"error": error}
        return personal or {}

    project = dict(DEFAULT_CONFIG)
    project_data, error = read_json(PROJECT_CONFIG_PATH, f"團隊設定檔 {PROJECT_CONFIG_PATH.name}")
    if error:
        return {"error": error}
    if project_data is not None:
        project.update(project_data)

    personal, error = read_json(PERSONAL_CONFIG_PATH, f"個人設定檔 {PERSONAL_CONFIG_PATH.name}")
    if error:
        return {"error": error}
    legacy, legacy_error = read_json(LEGACY_CONFIG_PATH, f"舊個人設定檔 {LEGACY_CONFIG_PATH.name}")
    if personal is None and legacy is not None:
        try:
            migrated = {key: legacy[key] for key in PERSONAL_CONFIG_KEYS if key in legacy}
            loop_mod.atomic_write_bytes(
                PERSONAL_CONFIG_PATH,
                json.dumps(migrated, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            personal = migrated
            print(f"已將舊個人設定遷移至:{PERSONAL_CONFIG_PATH}（舊檔保留）", flush=True)
        except (OSError, ValueError) as e:
            return {"error": f"個人設定檔無法遷移:{e}"}
    elif personal is None and legacy_error:
        return {"error": legacy_error}
    if personal is None:
        personal = {}
    for key in PERSONAL_CONFIG_KEYS:
        if key in personal:
            project[key] = personal[key]
    return project


def config_projection(cfg):
    """移除後端內部設定，只投影 Launcher/設定頁需要的安全欄位。"""
    raw_paths, resolved_paths = configured_path_dirs(cfg)
    prompt_templates, prompt_template_warnings = prompt_template_projection(cfg)
    prompt_bundle, prompt_bundle_error = prompt_template_bundle()
    return {"agent_cmds": cfg.get("agent_cmds", []),
            "validate_cmds": cfg.get("validate_cmds", []),
            "defaults": cfg.get("defaults") or {},
            "extra_path_dirs": raw_paths,
            "resolved_extra_path_dirs": resolved_paths,
            "config_path": str(PERSONAL_CONFIG_PATH),
            "personal_config_path": str(PERSONAL_CONFIG_PATH),
            "project_config_path": str(PROJECT_CONFIG_PATH),
            "config_override": bool(CONFIG_OVERRIDE),
            "notify_cmd": str(cfg.get("notify_cmd") or ""),
            "repo_roots": cfg.get("repo_roots", DEFAULT_CONFIG["repo_roots"]),
            "repos": scan_repos(cfg),
            "prompt_templates": prompt_templates,
            "prompt_template_bundle": prompt_bundle,
            "prompt_template_bundle_error": prompt_bundle_error,
            "prompt_template_warnings": prompt_template_warnings}


def save_personal_config(updates):
    """只寫個人檔；完整覆寫模式則保留 env 指定檔案的既有欄位。"""
    try:
        current_text = loop_mod.read_regular_text(PERSONAL_CONFIG_PATH, f"個人設定檔 {PERSONAL_CONFIG_PATH.name}")
        current = json.loads(current_text)
    except FileNotFoundError:
        current = {}
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"個人設定檔無法讀取:{e}") from e
    if not isinstance(current, dict):
        raise ValueError("個人設定檔頂層必須是 JSON object")
    if CONFIG_OVERRIDE:
        current.update(updates)
    else:
        current.update({key: value for key, value in updates.items() if key in PERSONAL_CONFIG_KEYS})
        current = {key: value for key, value in current.items() if key in PERSONAL_CONFIG_KEYS}
    loop_mod.atomic_write_bytes(
        PERSONAL_CONFIG_PATH,
        json.dumps(current, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def scan_repos(cfg):
    """從 config 的 repo_roots 掃 git repo(根目錄本身或往下一層)。"""
    found = []
    for raw in cfg.get("repo_roots", []):
        root = Path(os.path.expandvars(str(raw))).expanduser()
        if not root.is_dir():
            continue
        if (root / ".git").exists():
            found.append(str(root))
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / ".git").exists():
                found.append(str(child))
            if len(found) >= 200:
                break
    return found


UI_DIST = HERE / "ui"

MIME_OVERRIDES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
}



def list_workspaces():
    """fleet 總覽:主 state 或 checkpoint 至少一份存在才算 workspace；掃描本身不修檔。"""
    if not ROOT.is_dir():
        return []
    out = []
    for d in sorted(ROOT.iterdir()):
        if not loop_mod.valid_workspace_name(d.name) or d.is_symlink() or not d.is_dir():
            continue
        state_files = []
        for artifact in (d / "state.json", d / "state.last-good.json"):
            try:
                info = artifact.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                state_files.append(artifact)
        if not state_files:
            continue
        info = {
            "name": d.name,
            "phase": None,
            "running": False,
            "agent_failure_streak": 0,
            "agent_backoff_seconds": 0,
            "last_round_seconds": 0,
            "last_round_timed_out": False,
            "round_started_at": None,
            "round_deadline_at": None,
            "round_interrupted_at": None,
            "state_recovery_count": 0,
            "state_recovery_pending": False,
            "unread_issues": 0,
            "goal_changed": False,
            "loop_pid": None,
            "loop_started_at": None,
            "stale_loop_pid": False,
        }
        st, err = read_state(d.name, repair=False)
        if err:
            info["error"] = err
            legacy_identity = legacy_workspace_identity_for_delete(d.name)
            if legacy_identity is not None:
                legacy_state = legacy_identity["state"]
                info["legacy_delete_only"] = True
                info["legacy_reason"] = (
                    "舊版 workspace 不支援執行或重跑；可安全永久刪除後重新開始")
                info["workspace_generation"] = legacy_identity["generation"]
                info["running"] = ws_running(d.name, legacy_state)
        else:
            c = st.get("config") or {}
            loop_state = st.get("loop") or {}
            running = ws_running(d.name, st)
            parallel_run = None
            if st.get("workspace_kind") == "fleet-parent":
                projection = read_parallel_run(d.name)
                if not projection.get("read_error"):
                    parallel_run = projection
                    fleet_loop = projection.get("loop") or {}
                    running = loop_pid_alive(fleet_loop.get("pid"))
                    loop_state = fleet_loop
                else:
                    info["error"] = projection["read_error"]
            loop_pid = loop_state.get("pid")
            drain_claimed = running and loop_mod.stop_after_round_claimed(
                d, loop_state.get("pid"), loop_state.get("session_id"))
            current_order = st.get("current_order")
            current_task = next((t.get("task") or "" for t in (st.get("plan") or [])
                                 if isinstance(t, dict) and t.get("order") == current_order), "")
            if len(current_task) > 120:
                current_task = current_task[:120] + "…"
            latest_issue = ((st.get("issues") or [])[-1].get("text") or "") if st.get("issues") else ""
            projected_plan_len = len(st.get("plan") or [])
            projected_completed = len(st.get("completed") or [])
            projected_issues = list(st.get("issues") or [])
            projected_unread = loop_mod.unread_issue_count(st)
            if parallel_run is not None:
                parent_plan = parallel_run.get("plan") or []
                completed_entries, current_orders = parallel_progress_projection(d.name, parallel_run)
                projected_plan_len = len(parent_plan)
                projected_completed = len(completed_entries)
                current_order = current_orders[0] if current_orders else None
                current_task = next((task.get("task") or "" for task in parent_plan
                                     if isinstance(task, dict) and task.get("order") == current_order), "")
                c = parallel_run.get("config") or c
                projected_issues = []
                for track in parallel_run.get("tracks") or []:
                    diagnostics = track.get("diagnostics") or {}
                    child_issues = diagnostics.get("issues") or []
                    if not child_issues:
                        child_state = track_child_state(d.name, parallel_run, track)
                        child_issues = (child_state or {}).get("issues") or []
                    projected_issues.extend({**issue, "track": track.get("name")}
                                            for issue in child_issues if isinstance(issue, dict))
                projected_issues.extend(fleet_integration_issues(parallel_run))
                projected_unread = sum(1 for issue in projected_issues
                                       if not issue.get("resolved"))
                latest_issue = ((projected_issues[-1].get("text") or "")
                                if projected_issues else "")
            if len(current_task) > 120:
                current_task = current_task[:120] + "…"
            if len(latest_issue) > 240:
                latest_issue = latest_issue[:240] + "…"
            info.update(workspace_generation=st.get("workspace_generation"),
                        workspace_kind=st.get("workspace_kind"), fleet_run_id=st.get("fleet_run_id"),
                        fleet_parent=st.get("fleet_parent"), track=st.get("track"),
                        merge_stage=st.get("merge_stage"),
                        phase=st.get("phase"), round=st.get("round", 0), flag=st.get("flag", 0),
                        completed=projected_completed, plan_len=projected_plan_len,
                        done_count=st.get("done_count", 0), repo=c.get("repo"),
                        red_streak=st.get("red_streak", 0), stall_rounds=st.get("stall_rounds", 0),
                        issues=len(projected_issues),
                        latest_issue=latest_issue,
                        unread_issues=projected_unread,
                        agent_failure_streak=st.get("agent_failure_streak", 0),
                        agent_backoff_seconds=st.get("agent_backoff_seconds", 0),
                        last_round_seconds=st.get("last_round_seconds", 0),
                        last_round_timed_out=bool(st.get("last_round_timed_out")),
                        round_started_at=st.get("round_started_at"),
                        round_deadline_at=st.get("round_deadline_at"),
                        round_interrupted_at=st.get("round_interrupted_at"),
                        state_recovery_count=st.get("state_recovery_count", 0),
                        state_recovery_pending=bool(st.get("state_recovery_pending")),
                        goal_changed=bool(st.get("goal_changed")),
                        loop_pid=loop_pid,
                        loop_started_at=loop_state.get("started_at"),
                        stale_loop_pid=loop_pid is not None and not running,
                        current_order=current_order, current_task=current_task,
                        parallel_phase=(parallel_run or {}).get("phase"),
                        parallel_tracks=(parallel_run or {}).get("tracks") or [],
                        parallel_merge_queue=(parallel_run or {}).get("merge_queue") or [],
                        parallel_merge_tx=(parallel_run or {}).get("merge_tx"),
                        parallel_phase_history=(parallel_run or {}).get("phase_history") or [],
                        parallel_merge_history=(parallel_run or {}).get("merge_history") or [],
                        parallel_error=(parallel_run or {}).get("error"),
                        parallel_stop_reason=(parallel_run or {}).get("stop_reason"),
                        parallel_track_events=[
                            {"track": track.get("name"),
                             "child_workspace": track.get("child_workspace"),
                             "event_history": track.get("event_history") or []}
                            for track in (parallel_run or {}).get("tracks") or []
                            if isinstance(track, dict)],
                        running=running,
                        draining=drain_claimed or (running and loop_mod.stop_after_round_requested(
                            d, loop_state.get("pid"), loop_state.get("session_id"))),
                        drain_claimed=drain_claimed)
        out.append(info)
    return out


def _workspace_needs_attention(info):
    """以 Dashboard projection 判斷目前仍需處理的項目；不讀寫 workspace。"""
    if info.get("error"):
        return True
    unread_issues = info.get("unread_issues", info.get("issues", 0)) or 0
    completed = info.get("phase") == "done" or info.get("parallel_phase") == "done"
    parallel_attention = (bool(info.get("parallel_error")) or
        info.get("parallel_phase") == "failed" or any(
        track.get("status") in {"repairing", "failed"}
        for track in info.get("parallel_tracks") or [] if isinstance(track, dict)))
    return bool(
        unread_issues > 0 or
        info.get("state_recovery_pending") or
        info.get("goal_changed") or
        info.get("stale_loop_pid") or
        parallel_attention or
        (not completed and (
            (info.get("red_streak") or 0) > 0 or
            (info.get("stall_rounds") or 0) > 0 or
            (info.get("agent_failure_streak") or 0) > 0 or
            info.get("last_round_timed_out") or
            (info.get("state_recovery_count") or 0) > 0
        ))
    )


def _parent_registers_fleet_child(parent, child):
    """Return true only for the exact child identity persisted by parent Fleet truth."""
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


def fleet_health_projection(workspaces=None):
    """回傳唯讀 fleet health projection，供探針、SSE 與 UI 共用。"""
    raw_items = list_workspaces() if workspaces is None else list(workspaces)
    parents = {item.get("name"): item for item in raw_items
               if item.get("workspace_kind") == "fleet-parent"}
    # 正常 child 的 issues/error 已投影到 parent；全域 health 只計 group，避免 parent+N 重複。
    # 若 parent 遺失則保留 orphan child，不能因資料損壞把警訊一起隱藏。
    items = []
    for item in raw_items:
        if item.get("workspace_kind") == "fleet-child":
            parent = parents.get(item.get("fleet_parent"))
            if parent is not None and _parent_registers_fleet_child(parent, item):
                continue
        items.append(item)
    error_count = sum(1 for item in items if item.get("error"))
    attention = sum(1 for item in items if _workspace_needs_attention(item))
    status = "error" if error_count else "degraded" if attention else "ok"
    return {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "status": status,
        "workspace_count": len(items),
        "running": sum(1 for item in items if item.get("running")),
        "attention": attention,
        "error_count": error_count,
        "issues": sum(item.get("issues") or 0 for item in items),
        "unread_issues": sum(item.get("unread_issues", item.get("issues", 0)) or 0 for item in items),
        "agent_failures": sum(item.get("agent_failure_streak") or 0 for item in items),
        "round_timeouts": sum(1 for item in items if item.get("last_round_timed_out")),
        "state_recoveries": sum(item.get("state_recovery_count") or 0 for item in items),
        "goal_changes": sum(1 for item in items if item.get("goal_changed")),
        "stale_loop_pids": sum(1 for item in items if item.get("stale_loop_pid")),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


TAIL_INIT = 64 * 1024  # offset<0(首抓)時只回檔案尾段,超長 log 秒開


def read_incremental(path: Path, offset: int):
    """依 byte offset bounded 讀取 log；首抓只取完整尾段，輪替/縮檔時從頭重讀。"""
    try:
        path = loop_mod.workspace_file(path, "workspace log")
        fd = loop_mod._open_regular(path, os.O_RDONLY)
        with os.fdopen(fd, "rb", closefd=True) as f:
            size = os.fstat(f.fileno()).st_size
            if offset < 0:  # 首抓:直接跳到尾段,從下一個完整行開始
                offset = max(0, size - TAIL_INIT)
                truncated = offset > 0
                f.seek(offset)
                data = f.read(MAX_CHUNK)
                if offset > 0:
                    nl = data.find(b"\n")
                    if nl != -1:
                        offset += nl + 1
                        data = data[nl + 1:]
                return {"size": offset + len(data), "data": data.decode("utf-8", errors="replace"),
                        "truncated": truncated}
            if offset > size:
                offset = 0
            f.seek(offset)
            data = f.read(MAX_CHUNK)
        return {"size": offset + len(data), "data": data.decode("utf-8", errors="replace")}
    except FileNotFoundError:
        return {"size": 0, "data": ""}
    except (OSError, ValueError) as e:
        return {"size": 0, "data": "", "error": f"workspace log 不安全:{e}"}


class PlanEditError(ValueError):
    """結構化 plan 編輯不符合 coordinator 邊界；code 保留衝突與輸入錯誤的差異。"""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def locked_plan_task_count(state, plan) -> int:
    """計算不可移動的連續前綴：已完成任務，加上執行期目前任務。"""
    locked_orders = {entry["order"] for entry in state.get("completed") or []}
    if state.get("phase") == "exec" and state.get("current_order"):
        locked_orders.add(state["current_order"])
    locked_indexes = [index for index, task in enumerate(plan)
                      if task["order"] in locked_orders]
    return max(locked_indexes, default=-1) + 1


def normalize_plan_edit(state, tasks, expected_version):
    """驗證完整 plan 快照並回傳正規化結果、變更摘要與鎖定前綴長度。

    此函式只計算、不寫 state，讓 HTTP adapter 能在所有驗證成功後一次提交，
    也讓鎖定任務、pending 身分與重新編號規則能脫離 transport 單獨測試。
    """
    if not isinstance(tasks, list) or not tasks:
        raise PlanEditError("plan 必須保留至少一項任務")
    if (not isinstance(expected_version, int) or isinstance(expected_version, bool) or
            expected_version != state.get("plan_version")):
        raise PlanEditError(
            f"plan 已更新（目前 v{state.get('plan_version', 0)}），請重新載入後再編輯", 409)

    original = state.get("plan") or []
    locked_count = locked_plan_task_count(state, original)
    if len(tasks) < locked_count:
        raise PlanEditError("已完成或目前任務不可刪除")
    if state.get("phase") == "done" and len(tasks) != len(original):
        raise PlanEditError("已完成 workspace 沒有可調整的 pending task；請先回規劃期")

    editable_orders = {task["order"] for task in original[locked_count:]}
    seen_existing = set()
    normalized = []
    for index, entry in enumerate(tasks):
        if not isinstance(entry, dict):
            raise PlanEditError(f"tasks[{index}] 必須是 object")
        text = entry.get("task")
        ref = entry.get("ref")
        track = entry.get("track")
        scope = entry.get("scope")
        if not isinstance(text, str) or not text.strip():
            raise PlanEditError(f"tasks[{index}].task 必須是非空字串")
        if ref is not None and not isinstance(ref, str):
            raise PlanEditError(f"tasks[{index}].ref 必須是字串或 null")
        if not isinstance(track, str):
            raise PlanEditError(f"tasks[{index}].track 必須是字串")
        if scope is not None and not isinstance(scope, list):
            raise PlanEditError(f"tasks[{index}].scope 必須是字串陣列或省略")

        source_order = entry.get("order")
        normalized_ref = ref.strip() if isinstance(ref, str) and ref.strip() else None
        normalized_scope = ([value.strip() for value in scope]
                            if isinstance(scope, list) else None)
        if index < locked_count:
            expected_order = original[index]["order"]
            if source_order != expected_order:
                raise PlanEditError(f"task-{expected_order} 已完成或正在執行，不可移動或刪除")
            if (text.strip() != original[index]["task"] or
                    normalized_ref != (original[index].get("ref") or None) or
                    track != original[index].get("track") or
                    normalized_scope != original[index].get("scope")):
                raise PlanEditError(f"task-{expected_order} 已完成或正在執行，不可修改內容")
        elif source_order is not None:
            if (not isinstance(source_order, int) or isinstance(source_order, bool) or
                    source_order not in editable_orders):
                raise PlanEditError(f"tasks[{index}].order 不是可編輯的 pending task")
            if source_order in seen_existing:
                raise PlanEditError(f"task-{source_order} 重複")
            seen_existing.add(source_order)
        item = {"order": index + 1, "task": text.strip(), "ref": normalized_ref,
                "track": track}
        if normalized_scope is not None:
            item["scope"] = normalized_scope
        normalized.append(item)

    validated, errors = validate_plan(normalized)
    if errors:
        raise PlanEditError("plan v2 校驗未過:\n- " + "\n- ".join(errors))
    normalized = validated

    # state schema 保證原 order 連續；此判斷明確守住未來 schema 改動時的安全邊界。
    if any(normalized[index]["order"] != original[index]["order"]
           for index in range(locked_count)):
        raise PlanEditError("已完成或目前任務的 order 不可變更")

    old_pending = [task["order"] for task in original[locked_count:]]
    submitted = [entry.get("order") for entry in tasks[locked_count:]
                 if entry.get("order") is not None]
    deleted_count = sum(order not in seen_existing for order in old_pending)
    inserted_count = sum(entry.get("order") is None for entry in tasks[locked_count:])
    reordered = submitted != [order for order in old_pending if order in seen_existing]
    summary = []
    if inserted_count:
        summary.append(f"新增 {inserted_count} 項")
    if deleted_count:
        summary.append(f"刪除 {deleted_count} 項")
    if reordered:
        summary.append("調整順序")
    return normalized, "、".join(summary) or "更新文字", locked_count


def normalize_fleet_master_plan(fleet, tasks, expected_version):
    """awaiting-approval 專用：尚未 split 前 master plan 全部可編，且 generation 必須相符。"""
    version = fleet.get("plan_generation", 0)
    if (not isinstance(expected_version, int) or isinstance(expected_version, bool) or
            expected_version != version):
        raise PlanEditError(f"parallel plan 已更新（目前 v{version}），請重新載入後再編輯", 409)
    if fleet.get("phase") != "awaiting-approval" or fleet.get("tracks"):
        raise PlanEditError("parallel plan 只可在 awaiting-approval 且尚未建立 tracks 時編輯", 409)
    if not isinstance(tasks, list) or not tasks:
        raise PlanEditError("parallel plan 必須保留至少一項任務")
    candidate = []
    for index, entry in enumerate(tasks):
        if not isinstance(entry, dict):
            raise PlanEditError(f"tasks[{index}] 必須是 object")
        item = {"order": index + 1}
        for key in ("task", "ref", "track", "scope"):
            if key in entry:
                item[key] = entry[key]
        candidate.append(item)
    normalized, errors = validate_plan(candidate)
    if errors:
        raise PlanEditError("parallel plan 校驗未過：" + "; ".join(errors))
    ordinary = {task["track"] for task in normalized if task["track"] != "@final"}
    if len(ordinary) < 2:
        raise PlanEditError("parallel plan 至少需要兩個一般 track；跨軌驗收可另加 @final")
    return normalized


class Handler(BaseHTTPRequestHandler):
    """本機 Dashboard 的靜態檔、REST 與 SSE handler。"""

    protocol_version = "HTTP/1.1"
    preselect = ""
    # 路由表讓 endpoint 清單可一次掃讀；值用 method 名稱以避免 class 建立前引用尚未宣告的方法。
    POST_ROUTES = {
        "/api/launch": "api_launch",
        "/api/drain": "api_drain",
        "/api/cancel-drain": "api_cancel_drain",
        "/api/stop": "api_stop",
        "/api/run": "api_run",
        "/api/edit-state": "api_edit_state",
        "/api/edit-config": "api_edit_config",
        "/api/validate": "api_validate",
        "/api/preflight": "api_preflight",
        "/api/test-agent": "api_test_agent",
        "/api/test-cli": "api_test_cli",
        "/api/edit-cli-config": "api_edit_cli_config",
        "/api/edit-repo-roots": "api_edit_repo_roots",
        "/api/edit-notify": "api_edit_notify",
        "/api/test-notify": "api_test_notify",
        "/api/phase": "api_phase",
        "/api/set-task": "api_set_task",
        "/api/delete-workspace": "api_delete_workspace",
    }

    def log_message(self, *a):
        """停用 BaseHTTPRequestHandler 預設 access log，避免污染 operator console。"""
        pass

    def handle(self):
        """把瀏覽器離線造成的正常斷線視為連線結束，不印 traceback。"""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _out(self, code, body, ctype="application/json; charset=utf-8"):
        """輸出帶 no-store、nosniff 與本機 CSP 的固定長度 response。"""
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, msg, code=400):
        """以一致 JSON shape 回傳使用者可讀錯誤。"""
        self._out(code, json.dumps({"error": msg}, ensure_ascii=False))

    def _serve_ui(self, relative_path):
        """提供 Vite build 產物；production runtime 不需要 Node 或外部 CDN。"""
        root = UI_DIST.resolve()
        target = (UI_DIST / relative_path).resolve()
        if target != root and root not in target.parents:
            self._err("not found", 404)
            return
        if not target.is_file():
            if relative_path == "index.html":
                body = ("<!doctype html><meta charset=utf-8><title>UI 尚未 build</title>"
                        "<style>body{font:16px system-ui;padding:40px}</style>"
                        "<h1>Dashboard UI 尚未 build</h1>"
                        "<p>請執行 <code>cd ui &amp;&amp; npm install &amp;&amp; npm run build</code>。</p>")
                self._out(503, body, "text/html; charset=utf-8")
                return
            self._err("not found", 404)
            return
        ctype = MIME_OVERRIDES.get(target.suffix) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._out(200, target.read_bytes(), ctype)

    def _ws_dir(self, q):
        """從 query 驗證 workspace 名稱與真實目錄，拒絕隱藏/未知/symlink 目標。"""
        name = q.get("ws", [""])[0]
        if not loop_mod.valid_workspace_name(name):
            self._err(f"workspace 名稱 {name or '(空)'} 不合法：{loop_mod.WORKSPACE_NAME_RULE}")
            return None
        valid = ({d.name for d in ROOT.iterdir()
                  if loop_mod.valid_workspace_name(d.name) and not d.is_symlink() and d.is_dir()}
                 if ROOT.is_dir() else set())
        if name not in valid:
            self._err(f"未知 workspace:{name or '(空)'},可用:{sorted(valid)}")
            return None
        try:
            return safe_workspace_dir(name)
        except ValueError as e:
            self._err(str(e))
            return None

    def _serve_events(self, q):
        """SSE:主畫面單向推送 fleet/state/歷史事件/console 增量；寫入操作仍走 REST。"""
        workspace = q.get("ws", [""])[0]
        include_fleet_history = q.get("fleet", ["0"])[0] == "1"
        if workspace and not loop_mod.valid_workspace_name(workspace):
            self._err(f"workspace 名稱不合法：{loop_mod.WORKSPACE_NAME_RULE}")
            return
        try:
            workspace_dir = safe_workspace_dir(workspace) if workspace else None
        except ValueError as e:
            self._err(str(e))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        def emit(event, payload):
            """送出一筆具事件名稱的 SSE JSON，立即 flush 以降低操作延遲。"""
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        fleet_sig = state_sig = fleet_history_sig = None
        fleet_at = fleet_history_at = keepalive_at = 0.0
        console_offset = -1
        console_identity = None

        def file_identity(path):
            """以 device/inode 偵測 console 輪替，避免沿用舊 byte offset。"""
            try:
                stat = path.stat()
                return stat.st_dev, stat.st_ino
            except FileNotFoundError:
                return None

        try:
            while True:
                now = time.monotonic()
                if now >= fleet_at:
                    fleet = list_workspaces()
                    sig = json.dumps(fleet, ensure_ascii=False, sort_keys=True)
                    if sig != fleet_sig:
                        emit("workspaces", fleet)
                        emit("health", fleet_health_projection(fleet))
                        fleet_sig = sig
                    # dashboard 自己啟動的 job 可能在 preflight 立刻退出；快速同步避免
                    # UI 還顯示「停止」數秒，使用者再點後才發現程序早已結束。
                    fleet_at = now + 0.6

                if include_fleet_history and now >= fleet_history_at:
                    fleet_observability = read_fleet_observability()
                    history_sig = json.dumps(fleet_observability, ensure_ascii=False, sort_keys=True)
                    if history_sig != fleet_history_sig:
                        emit("fleet-history", fleet_observability["entries"])
                        emit("fleet-round-metrics", fleet_observability["metrics"])
                        fleet_history_sig = history_sig
                    fleet_history_at = now + FLEET_HISTORY_SSE_INTERVAL

                if workspace:
                    # GET/SSE 永遠只讀；真正修復由 loop resume 或後續明確 mutation 完成，
                    # 避免 Dashboard 在活躍 loop 的 agent round 中途改 python-owned state。
                    state, err = project_state_for_ui(workspace)
                    projected = {"error": err} if err else state
                    sig = json.dumps(projected, ensure_ascii=False, sort_keys=True)
                    if sig != state_sig:
                        emit("state", projected)
                        state_sig = sig
                    console_path = workspace_dir / "console.log"
                    identity_before = file_identity(console_path)
                    if console_identity is not None and identity_before != console_identity:
                        console_offset = 0  # rotation 後從新 console.log 開頭接續
                    console = read_incremental(console_path, console_offset)
                    identity_after = file_identity(console_path)
                    if identity_before != identity_after:
                        # 剛好撞上 rename/create；下一圈重新讀穩定的新檔，不冒險漏掉開頭。
                        console_offset = 0
                    else:
                        console_offset = console["size"]
                        if console["data"]:
                            emit("console", {"data": console["data"]})
                    console_identity = identity_after

                if now >= keepalive_at:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    keepalive_at = now + 15
                time.sleep(0.6)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    # ---------- GET ----------
    def do_GET(self):
        """路由所有唯讀投影；每個 artifact reader 自己執行 bounded 與 symlink 檢查。"""
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                self._serve_ui("index.html")
            elif u.path.startswith("/assets/"):
                self._serve_ui(u.path.lstrip("/"))
            elif u.path == "/api/bootstrap":
                self._out(200, json.dumps({"preselect": self.preselect}, ensure_ascii=False))
            elif u.path == "/api/events":
                self._serve_events(q)
            elif u.path == "/api/health":
                strict = q.get("strict", ["0"])[0]
                if strict not in ("0", "1"):
                    self._err("health strict 必須是 0 或 1")
                    return
                health = fleet_health_projection()
                code = 503 if strict == "1" and health["status"] != "ok" else 200
                self._out(code, json.dumps(health, ensure_ascii=False))
            elif u.path == "/api/workspaces":
                self._out(200, json.dumps(list_workspaces(), ensure_ascii=False))
            elif u.path == "/api/config":
                cfg = load_config()
                if "error" in cfg:
                    self._out(200, json.dumps(cfg, ensure_ascii=False))
                    return
                self._out(200, json.dumps(config_projection(cfg), ensure_ascii=False))
            elif u.path == "/api/jobs":
                with JOBS_LOCK:
                    _prune_finished_jobs_locked()
                    self._out(200, json.dumps([j.info() for j in JOBS.values()], ensure_ascii=False))
            elif u.path == "/api/job-startup":
                name = q.get("name", [""])[0]
                pid = q.get("pid", [""])[0]
                if not loop_mod.valid_workspace_name(name):
                    self._err(f"workspace 名稱不合法：{loop_mod.WORKSPACE_NAME_RULE}")
                    return
                self._out(200, json.dumps(job_startup_status(name, pid), ensure_ascii=False))
            elif u.path == "/api/repo-status":
                repo = Path(q.get("repo", [""])[0]).expanduser()
                self._out(200, json.dumps(repo_status_projection(repo), ensure_ascii=False))
            elif u.path == "/api/state":
                d = self._ws_dir(q)
                if d is None:
                    return
                st, err = project_state_for_ui(d.name)
                self._out(200, json.dumps({"error": err} if err else st, ensure_ascii=False))
            elif u.path == "/api/parallel-run":
                d = self._ws_dir(q)
                if d is None:
                    return
                self._out(200, json.dumps(read_parallel_run(d.name), ensure_ascii=False))
            elif u.path == "/api/tail":
                d = self._ws_dir(q)
                if d is None:
                    return
                rnd = int(q.get("round", ["0"])[0])
                off = int(q.get("offset", ["0"])[0])
                self._out(200, json.dumps(
                    read_incremental(d / "logs" / f"round-{rnd:04d}.log", off), ensure_ascii=False))
            elif u.path == "/api/history":
                d = self._ws_dir(q)
                if d is None:
                    return
                off = int(q.get("offset", ["0"])[0])
                run = q.get("run", ["current"])[0]
                if run not in ("current", "previous"):
                    self._err("history run 必須是 current 或 previous")
                    return
                history_name = "history.log" if run == "current" else "history.log.1"
                projection = read_incremental(d / history_name, off)
                projection["run"] = run
                self._out(200, json.dumps(projection, ensure_ascii=False))
            elif u.path == "/api/round-metrics":
                d = self._ws_dir(q)
                if d is None:
                    return
                run = q.get("run", ["current"])[0]
                if run not in ("current", "previous"):
                    self._err("round metrics run 必須是 current 或 previous")
                    return
                try:
                    limit = int(q.get("limit", ["50"])[0])
                except (TypeError, ValueError):
                    self._err(f"round metrics limit 必須介於 1～{loop_mod.ROUND_METRICS_MAX_SAMPLES}")
                    return
                if not 1 <= limit <= loop_mod.ROUND_METRICS_MAX_SAMPLES:
                    self._err(f"round metrics limit 必須介於 1～{loop_mod.ROUND_METRICS_MAX_SAMPLES}")
                    return
                history_name = "history.log" if run == "current" else "history.log.1"
                try:
                    projection = loop_mod.read_round_metrics(d / history_name, limit)
                except ValueError as e:
                    self._err(str(e))
                    return
                projection["run"] = run
                self._out(200, json.dumps(projection, ensure_ascii=False))
            elif u.path == "/api/report":
                d = self._ws_dir(q)
                if d is None:
                    return
                self._out(200, json.dumps(read_report(d.name), ensure_ascii=False))
            elif u.path == "/api/goal":
                d = self._ws_dir(q)
                if d is None:
                    return
                self._out(200, json.dumps(read_goal(d.name), ensure_ascii=False))
            elif u.path == "/api/prompt":
                d = self._ws_dir(q)
                if d is None:
                    return
                self._out(200, json.dumps(read_prompt(d.name), ensure_ascii=False))
            elif u.path == "/api/fleet-history":
                self._out(200, json.dumps(read_fleet_history(), ensure_ascii=False))
            elif u.path == "/api/fleet-round-metrics":
                projection = read_fleet_observability()
                self._out(200, json.dumps(projection["metrics"], ensure_ascii=False))
            elif u.path == "/api/anomalies":
                run = q.get("run", ["current"])[0]
                if run not in ("current", "previous"):
                    self._err("anomaly run 必須是 current 或 previous")
                    return
                if q.get("ws"):
                    directory = self._ws_dir(q)
                    if directory is None:
                        return
                    try:
                        projection = read_anomaly_records(directory, run=run)
                    except (OSError, ValueError) as e:
                        self._err(f"異常清單讀取失敗:{e}")
                        return
                else:
                    if run != "current":
                        self._err("全域異常清單只支援 current run")
                        return
                    projection = read_anomaly_records()
                self._out(200, json.dumps(projection, ensure_ascii=False))
            elif u.path == "/api/anomaly-log":
                directory = self._ws_dir(q)
                if directory is None:
                    return
                anomaly_id = q.get("id", [""])[0]
                try:
                    projection = read_preserved_anomaly_log(directory, anomaly_id)
                except FileNotFoundError as e:
                    self._err(str(e), 404)
                    return
                except ValueError as e:
                    self._err(str(e))
                    return
                self._out(200, json.dumps(projection, ensure_ascii=False))
            else:
                self._err("not found", 404)
        except (ValueError, BrokenPipeError, ConnectionResetError):
            pass

    # ---------- POST ----------
    def do_POST(self):
        """Bounded 讀取 request body，避免大型或不完整 payload 污染連線。"""
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0:
                raise ValueError("Content-Length 不可為負數")
            if length > MAX_REQUEST_BYTES:
                self.close_connection = True
                self._err(f"request body 太大（上限 {MAX_REQUEST_BYTES // (1024 * 1024)} MiB）", 413)
                return
            raw_body = self.rfile.read(length) if length else b""
        except ValueError:
            self._err("body 必須是 JSON")
            return
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._err("body 必須是 JSON")
            return
        try:
            handler_name = Handler.POST_ROUTES.get(u.path)
            if handler_name is None:
                self._err("not found", 404)
                return
            # 以 Handler 取未綁定 method，測試可繼續用輕量 fake handler 驗證 request 邊界。
            getattr(Handler, handler_name)(self, body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    @with_state_lock(repo_fallback=True)
    @with_workspace_operation_lock
    def api_launch(self, body):
        """交易式啟動/重置 loop：完成所有可失敗 preflight 後才提交 state 與 spawn。"""
        cfg = load_config()
        if "error" in cfg:
            self._err(cfg["error"])
            return
        # agent 命令只能選 config 固定選項(index),前端塞不進任意命令
        agents = cfg.get("agent_cmds") or []
        try:
            agent_cmd = agents[int(body.get("agent_idx"))]["cmd"]
        except (TypeError, ValueError, IndexError, KeyError):
            self._err(f"agent_idx 不合法,合法值 0..{len(agents) - 1}(請用 Agent CLI 管理器設定)")
            return
        vals = cfg.get("validate_cmds") or []
        custom = (body.get("validate_custom") or "").strip()
        if custom:
            validate_cmd = custom
        else:
            try:
                validate_cmd = vals[int(body.get("validate_idx"))]["cmd"]
            except (TypeError, ValueError, IndexError, KeyError):
                self._err(f"validate_idx 不合法,合法值 0..{len(vals) - 1},或改用 validate_custom 手寫")
                return
        for command, label in ((agent_cmd, "Agent CLI"), (validate_cmd, "Validate 命令")):
            command_problem = command_error(command, label, cfg)
            if command_problem:
                self._err(command_problem)
                return
        repo = Path(str(body.get("repo") or "")).expanduser()
        if not (repo / ".git").exists():
            self._err(f"{repo} 不是 git repo(preflight 之後還會再驗一次)")
            return
        name = (body.get("name") or "").strip() or repo.name
        if not loop_mod.valid_workspace_name(name):
            self._err(f"workspace 名稱 {name} 不合法：{loop_mod.WORKSPACE_NAME_RULE}，例:legacy-orders")
            return
        try:
            safe_workspace_dir(name)
        except ValueError as e:
            self._err(str(e))
            return
        d = cfg.get("defaults") or {}
        try:
            requested = lambda key, default: body[key] if body.get(key) is not None else d.get(key, default)
            ft = parse_numeric_setting(requested("flag_threshold", 10), integer=True, minimum=1)
            dt = parse_numeric_setting(requested("done_threshold", 3), integer=True, minimum=1)
            rt = parse_numeric_setting(requested("round_timeout", 30), integer=False, minimum=0)
            ab = parse_numeric_setting(requested("agent_backoff_max", 60), integer=False, minimum=0)
            vt = parse_numeric_setting(requested("validate_timeout", 120), integer=False, minimum=1e-300)
            rl = parse_numeric_setting(requested("red_limit", 20), integer=True, minimum=1)
            sl = parse_numeric_setting(requested("stall_limit", 300), integer=True, minimum=1)
            track_port_base = parse_numeric_setting(d.get("track_port_base", 0), integer=True, minimum=0)
            track_env = d.get("track_env", {})
            if (track_port_base > 65527 or not isinstance(track_env, dict) or
                    any(not isinstance(key, str) or not isinstance(value, str)
                        for key, value in track_env.items())):
                raise ValueError
        except (TypeError, ValueError):
            self._err("flag/done/red/stall 必須 ≥1，round_timeout/agent_backoff_max 必須 ≥0，"
                      "validate_timeout 必須 >0 秒；track_port_base/track_env 必須合法")
            return
        # 規劃後暫停:表單值優先,未指定時落回團隊 defaults;非布林輸入以 truthiness 收斂。
        pause_requested = body.get("pause_after_plan")
        pause_after_plan = (bool(d.get("pause_after_plan")) if pause_requested is None
                            else bool(pause_requested))
        parallel_mode = bool(body.get("parallel"))
        try:
            max_parallel = parse_numeric_setting(body.get("max_parallel", 4), integer=True, minimum=1)
            merge_threshold = parse_numeric_setting(body.get("merge_threshold", 2), integer=True, minimum=1)
            max_child_restarts = parse_numeric_setting(body.get("max_child_restarts", 0), integer=True, minimum=0)
            if max_parallel > 8:
                raise ValueError
        except (TypeError, ValueError):
            self._err("max_parallel 必須是 1..8；merge_threshold 必須 ≥1；max_child_restarts 必須 ≥0")
            return
        # 貼了 plan.json → 建全新 state(等同重置),由使用者決定從 plan 或 exec 開跑
        plan_raw = (body.get("plan_json") or "").strip()
        normalized = None
        start_phase = str(body.get("start_phase") or "plan")
        if plan_raw:
            if start_phase not in ("plan", "exec"):
                self._err("start_phase 只能是 plan 或 exec")
                return
            try:
                plan_obj = json.loads(plan_raw)
            except json.JSONDecodeError as e:
                self._err(f"plan.json 解析失敗:{e}")
                return
            normalized, errs = validate_plan(plan_obj)
            if errs:
                self._err("plan.json 校驗未過:\n- " + "\n- ".join(errs))
                return
        if parallel_mode and (body.get("reset_state") or body.get("new_branch")):
            self._err("parallel mode 不接受重置 state 或另切 loop branch；要重跑請刪除舊 run")
            return
        if parallel_mode and normalized is not None:
            ordinary_tracks = {task["track"] for task in normalized if task["track"] != "@final"}
            if len(ordinary_tracks) < 2:
                self._err("parallel plan 匯入至少需要兩個一般 track；跨軌驗收可另加 @final")
                return
            if start_phase != "exec":
                self._err("parallel plan 匯入必須從 exec 開始")
                return
        goal_content = str(body.get("goal_content") or "")
        goal_path = None
        if goal_content.strip():
            # 所有可失敗的 goal 檢查先於 new_branch checkout；失敗的 launch 不得留下
            # 使用者未要求的 branch mutation。atomic_write 仍會在真正寫入時重驗一次。
            try:
                goal_path = loop_mod.repo_relative_path(repo, "goal.md")
            except (OSError, ValueError) as e:
                self._err(f"goal.md 不安全或無法寫入:{e}")
                return
        if parallel_mode:
            workspace_entry = safe_workspace_dir(name)
            try:
                workspace_entry.lstat()
            except FileNotFoundError:
                pass
            except OSError as error:
                self._err(f"無法檢查既有 workspace {name}:{error}", 409)
                return
            else:
                self._err(f"workspace {name} 已存在；parallel run 請使用 Run 續跑，或先安全刪除再新建", 409)
                return
            status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                    capture_output=True, text=True)
            if status.returncode or status.stdout.strip():
                self._err("parallel integration worktree 不乾淨；尚未寫入 goal/plan，也不會啟動", 409)
                return
            repo_identity = repo.expanduser().resolve()
            active = next((item for item in list_workspaces()
                           if item.get("running") and item.get("repo") and
                           Path(str(item["repo"])).expanduser().resolve() == repo_identity), None)
            if active:
                self._err(f"repo {repo} 已有外部 workspace {active.get('name')} 在執行；未做任何變更", 409)
                return
        # 衝突檢查 + git mutation + spawn 全包進同一個 lock,且順序=先檢查再 mutate(#2):
        # 對正在跑的 repo 再按啟動時,必須在切 branch/改 goal 前就擋下,否則現有 loop 會被動到。
        with JOBS_LOCK:
            workspace_entry = safe_workspace_dir(name)
            try:
                entry_info = workspace_entry.lstat()
                entry_exists = True
            except FileNotFoundError:
                entry_info = None
                entry_exists = False
            except OSError as error:
                self._err(f"無法確認 workspace {name} 身分：{error}", 409)
                return
            expected_generation = body.get("workspace_generation")
            st, state_error = read_state(name, repair=False) if entry_exists else (None, None)
            if expected_generation is None:
                if entry_exists:
                    self._err(
                        f"workspace {name} 已存在（類型可能是 standalone、parallel、legacy 或損壞）；"
                        "請重新載入後再操作", 409)
                    return
            elif (not entry_exists or stat.S_ISLNK(entry_info.st_mode) or state_error or not st or
                  st.get("workspace_kind") != "standalone" or
                  st.get("workspace_generation") != expected_generation):
                self._err("workspace 已刪除、損壞、類型不同或同名重建，請重新載入後再操作", 409)
                return
            if st and loop_pid_alive((st.get("loop") or {}).get("pid")):
                self._err(f"workspace {name} 已有 loop 在跑(外部啟動的也算),先停掉再啟動")
                return
            if st and st.get("workspace_kind") == "fleet-parent":
                fleet_state = read_parallel_run(name)
                if not fleet_state.get("read_error") and loop_pid_alive((fleet_state.get("loop") or {}).get("pid")):
                    self._err(f"parallel run {name} 已在執行中")
                    return
            for j in JOBS.values():
                if j.alive() and j.name == name:
                    self._err(f"workspace {name} 已有 loop 在跑(pid {j.popen.pid}),先停掉再啟動")
                    return
                if j.alive() and Path(j.repo) == repo:
                    self._err(f"repo {repo} 已有 loop 在跑({j.name}),同一 repo 不能同時跑兩個")
                    return
            # ---- 通過衝突檢查後才做任何 git mutation ----
            # 選配:在新 branch 跑(loop/<name>),不弄髒主線;已存在就 checkout 續用
            if body.get("new_branch"):
                br = f"loop/{name}"
                exists = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", br],
                                        capture_output=True, text=True).returncode == 0
                r = subprocess.run(["git", "-C", str(repo), "checkout", "-q"] + ([br] if exists else ["-b", br]),
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    self._err(f"切換 branch {br} 失敗:" + (r.stdout + r.stderr).strip()[-300:])
                    return
                workspace_console_log(name, f"已切換 Git branch｜{br}")
            # goal.md 隨啟動自動 commit(gate#1:人選了檔=人審過)。檔名固定、指定 pathspec,
            # 內容與 HEAD 相同就不產生新 commit。
            if goal_content.strip():
                try:
                    loop_mod.atomic_write_bytes(goal_path, goal_content.encode("utf-8"))
                except (OSError, ValueError) as e:
                    self._err(f"goal.md 不安全或無法寫入:{e}")
                    return
                r = subprocess.run(["git", "-C", str(repo), "add", "--", "goal.md"],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    self._err("git add goal.md 失敗:" + (r.stdout + r.stderr).strip()[-300:])
                    return
                r = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", "goal.md"],
                                   capture_output=True, text=True)
                if r.returncode != 0:  # 有變更才 commit
                    r = subprocess.run(["git", "-C", str(repo), "commit",
                                        "-m", "loop-lite: 匯入需求 goal.md", "--", "goal.md"],
                                       capture_output=True, text=True)
                    if r.returncode != 0:
                        self._err("git commit goal.md 失敗:" + (r.stdout + r.stderr).strip()[-300:])
                        return
                    if parallel_mode:
                        print(f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜{name} 已匯入並 commit goal.md",
                              flush=True)
                    else:
                        workspace_console_log(name, "已匯入並 commit goal.md")
            if normalized is not None:
                if parallel_mode:
                    import_plan_path = write_parallel_launch_plan(name, normalized)
                else:
                    workspace_dir = safe_workspace_dir(name)
                    workspace_dir.mkdir(parents=True, exist_ok=True)
                    import_plan_path = workspace_dir / "import-plan.pending.json"
                    loop_mod.atomic_write_bytes(
                        import_plan_path,
                        json.dumps(normalized, ensure_ascii=False, indent=2).encode("utf-8"),
                    )
                plan_message = f"準備匯入 plan.json｜共 {len(normalized)} 條｜Validate 通過後才取代舊 state"
                if parallel_mode:
                    print(f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜{name} {plan_message}", flush=True)
                else:
                    workspace_console_log(name, plan_message)
            else:
                import_plan_path = None
            if parallel_mode:
                p = spawn_fleet(name, repo, agent_cmd, validate_cmd,
                                import_plan=import_plan_path,
                                max_parallel=max_parallel, merge_threshold=merge_threshold,
                                flag_threshold=ft, done_threshold=dt, round_timeout=rt,
                                red_limit=rl, stall_limit=sl,
                                validate_timeout=vt, agent_backoff_max=ab,
                                max_child_restarts=max_child_restarts,
                                pause_after_plan=pause_after_plan,
                                track_env=track_env, track_port_base=track_port_base,
                                notify_cmd=str(cfg.get("notify_cmd") or ""), env=command_env(cfg))
            else:
                p = spawn_loop(name, repo, agent_cmd, validate_cmd, ft, dt, rt,
                               validate_timeout=vt,
                               reset=bool(body.get("reset_state")) and normalized is None,
                               import_plan=import_plan_path, start_phase=start_phase,
                               notify_cmd=str(cfg.get("notify_cmd") or ""),
                               red_limit=rl, stall_limit=sl,
                               agent_backoff_max=ab,
                               stuck_stop=bool(d.get("stuck_stop")),
                               stuck_count=int(d.get("stuck_stop_count", 100)),
                               pause_after_plan=pause_after_plan,
                               expected_generation=(
                                   st.get("workspace_generation")
                                   if st and st.get("workspace_kind") == "standalone"
                                   else "new"),
                               env=command_env(cfg))
        launch_message = f"啟動 {'fleet' if parallel_mode else 'loop'}｜pid={p.pid}｜repo={repo}"
        if parallel_mode:
            # Fleet owns creation of the parent directory; a Dashboard log must not win that race.
            print(f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜{name} {launch_message}", flush=True)
        else:
            workspace_console_log(name, launch_message)
        response = {"ok": True, "name": name, "pid": p.pid,
                    "starting": True, "startup_timeout": vt + 15}
        self._out(200, json.dumps(response, ensure_ascii=False))

    @with_state_lock
    def api_run(self, body):
        """一鍵重跑既有 workspace:設定全部從 state.json 拿,agent 命令先過 config 白名單。"""
        name = str(body.get("name") or "")
        st = _load_state_or_err(self, name)
        if st is None:
            return
        if st.get("workspace_kind") == "fleet-child":
            self._err(f"{name} 由 parallel run parent 管理，不能單獨啟動", 409)
            return
        if st.get("workspace_kind") == "fleet-parent":
            fleet_state = read_parallel_run(name)
            if fleet_state.get("read_error"):
                self._err(fleet_state["read_error"])
                return
            if not require_fleet_run_id(self, body, fleet_state):
                return
            if not require_fleet_plan_identity(self, body, fleet_state):
                return
            fleet_pid = (fleet_state.get("loop") or {}).get("pid")
            if loop_pid_alive(fleet_pid):
                self._err(f"{name} 已在執行中")
                return
            if fleet_state.get("phase") == "done":
                self._err(f"{name} 已完成；要重跑請刪除後建立新的 parallel run")
                return
            cfg = load_config()
            config = fleet_state.get("config") or {}
            allowed = {norm_cmd(item.get("cmd", "")) for item in cfg.get("agent_cmds") or []}
            if norm_cmd(config.get("agent_cmd", "")) not in allowed:
                self._err("parallel run 的 agent 命令不在目前 Agent CLI 清單內")
                return
            try:
                startup_timeout = parse_numeric_setting(
                    config.get("validate_timeout", 120), integer=False, minimum=1e-300) + 15
            except (TypeError, ValueError):
                self._err("parallel run 的 validate_timeout 不合法，請先修正設定", 409)
                return
            with JOBS_LOCK:
                current_job = JOBS.get(name)
                if current_job is not None and current_job.alive():
                    self._err(f"{name} 已在啟動或執行中(pid {current_job.popen.pid})")
                    return
                process = spawn_fleet_resume(name, fleet_state, env=command_env(cfg))
            workspace_console_log(name, f"繼續運行 fleet｜pid={process.pid}")
            self._out(200, json.dumps({"ok": True, "starting": True, "name": name,
                                       "pid": process.pid,
                                       "startup_timeout": startup_timeout},
                                      ensure_ascii=False))
            return
        if not require_workspace_generation(self, body, st):
            return
        c = st.get("config") or {}
        repo, agent_cmd, validate_cmd = c.get("repo"), c.get("agent_cmd"), c.get("validate_cmd")
        if not (repo and agent_cmd and validate_cmd):
            self._err("state 缺 repo/agent/validate 設定(舊版 state)——用「啟動新 loop」表單跑一次後就能一鍵開關")
            return
        if st.get("phase") == "done":
            self._err(f"{name} 已 done;要重跑請在啟動表單勾「重置 state」")
            return
        cfg = load_config()
        if "error" in cfg:
            self._err(cfg["error"])
            return
        allowed = {norm_cmd(a.get("cmd", "")) for a in cfg.get("agent_cmds") or []}
        if norm_cmd(agent_cmd) not in allowed:
            self._err("state 裡的 agent 命令不在目前 Agent CLI 清單內"
                      "(可能被改過或個人設定已更新),請用齒輪加入或重新選擇")
            return
        if not (Path(repo) / ".git").exists():
            self._err(f"{repo} 不是 git repo(repo 被移走了?)")
            return
        for command, label in ((agent_cmd, "Agent CLI"), (validate_cmd, "Validate 命令")):
            command_problem = command_error(command, label, cfg)
            if command_problem:
                self._err(command_problem)
                return
        if loop_pid_alive((st.get("loop") or {}).get("pid")):
            self._err(f"{name} 已在執行中")
            return
        d = cfg.get("defaults") or {}
        try:
            ft = parse_numeric_setting(c.get("flag_threshold", 10), integer=True, minimum=1)
            dt = parse_numeric_setting(c.get("done_threshold", 3), integer=True, minimum=1)
            rt = parse_numeric_setting(c.get("round_timeout", 30), integer=False, minimum=0)
            vt = parse_numeric_setting(
                c.get("validate_timeout", d.get("validate_timeout", 120)), integer=False,
                minimum=1e-300)
            rl = parse_numeric_setting(
                c.get("red_limit", d.get("red_limit", 20)), integer=True, minimum=1)
            sl = parse_numeric_setting(
                c.get("stall_limit", d.get("stall_limit", 300)), integer=True, minimum=1)
            ab = parse_numeric_setting(
                c.get("agent_backoff_max", d.get("agent_backoff_max", 60)), integer=False,
                minimum=0)
        except (TypeError, ValueError):
            self._err("workspace 執行設定含非法數值，請先用設定視窗修正")
            return
        with JOBS_LOCK:
            j = JOBS.get(name)
            if j is not None and j.alive():
                self._err(f"{name} 已在執行中(pid {j.popen.pid})")
                return
            for jj in JOBS.values():
                if jj.alive() and Path(jj.repo) == Path(repo):
                    self._err(f"repo {repo} 已有 loop 在跑({jj.name})")
                    return
            p = spawn_loop(name, repo, agent_cmd, validate_cmd,
                           ft, dt, rt,
                           validate_timeout=vt,
                           notify_cmd=str(cfg.get("notify_cmd") or ""),
                           red_limit=rl,
                           stall_limit=sl,
                           agent_backoff_max=ab,
                           stuck_stop=bool(d.get("stuck_stop")),
                           stuck_count=int(d.get("stuck_stop_count", 100)),
                           pause_after_plan=bool(c.get("pause_after_plan")),
                           expected_generation=st.get("workspace_generation", ""),
                           env=command_env(cfg))
        workspace_console_log(name, f"繼續運行 loop｜pid={p.pid}")
        startup_timeout = vt + 15
        self._out(200, json.dumps({"ok": True, "starting": True, "name": name, "pid": p.pid,
                                   "startup_timeout": startup_timeout}, ensure_ascii=False))

    @with_state_lock
    def api_edit_state(self, body):
        """停止狀態下的人工編輯:plan、done 計數與 issue 已讀/清除；執行中全部鎖死。"""
        name = str(body.get("name") or "")
        st = _load_state_or_err(self, name)
        if st is None:
            return
        if st.get("workspace_kind") == "fleet-child":
            self._err(f"{name} 的 plan/state 由 parallel run 管理，不能在 workspace API 人工修改", 409)
            return
        if st.get("workspace_kind") == "fleet-parent":
            if ws_running(name, st):
                self._err(f"{name} 執行中,全部鎖死——先停止才能編輯")
                return
            fleet = read_parallel_run(name)
            if fleet.get("read_error"):
                self._err(fleet["read_error"])
                return
            if not require_fleet_run_id(self, body, fleet):
                return
            tasks = body.get("tasks")
            if not body.get("plan_edit") or tasks is None or any(
                    body.get(key) is not None for key in ("clear_issues", "ack_issues", "done_count")):
                self._err("parallel parent 在 awaiting-approval 只允許編輯完整 master plan", 409)
                return
            try:
                normalized = normalize_fleet_master_plan(fleet, tasks, body.get("plan_version"))
            except PlanEditError as error:
                self._err(str(error), error.code)
                return
            changed = []
            expected_fleet = copy.deepcopy(fleet)
            expected_parent_state = copy.deepcopy(st)
            if normalized != (fleet.get("plan") or []):
                raw = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
                fleet["plan"] = normalized
                fleet["plan_sha256"] = hashlib.sha256(raw).hexdigest()
                fleet["plan_generation"] = fleet.get("plan_generation", 0) + 1
                fleet["dashboard_revision"] = fleet.get("dashboard_revision", 0) + 1
                fleet["order_map"] = {}
                st["plan"] = normalized
                st["plan_version"] = fleet["plan_generation"]
                st["fleet_truth_revision"] = fleet["dashboard_revision"]
                try:
                    write_parallel_dashboard_transaction(
                        name, fleet, st, expected_fleet=expected_fleet,
                        expected_parent_state=expected_parent_state)
                except (OSError, RuntimeError, ValueError, SafeDeleteError,
                        loop_mod.WorkspaceOperationLockError) as error:
                    self._err(f"parallel plan 寫入失敗；舊 truth 已保留：{error}", 409)
                    return
                changed.append(f"parallel plan v{fleet['plan_generation']}（{len(normalized)} 項）")
            workspace_console_log(name, f"人工編輯 master plan｜{', '.join(changed) or '無變更'}")
            self._out(200, json.dumps({"ok": True, "changed": changed}, ensure_ascii=False))
            return
        if st.get("workspace_kind") != "standalone":
            self._err(f"{name} workspace_kind 不支援人工修改", 409)
            return
        if not require_workspace_generation(self, body, st):
            return
        if ws_running(name, st):
            self._err(f"{name} 執行中,全部鎖死——先停止才能編輯")
            return
        expected_state = copy.deepcopy(st)
        changed = []
        tasks = body.get("tasks")
        if tasks is not None and body.get("plan_edit"):
            # normalize_plan_edit 只計算；在同一把 workspace lock 內通過全部驗證後才一次寫回，
            # 避免拖曳期間另一個 Dashboard 更新 plan 卻被舊畫面部分覆蓋。
            try:
                normalized, summary, locked_count = normalize_plan_edit(
                    st, tasks, body.get("plan_version"))
            except PlanEditError as e:
                self._err(str(e), e.code)
                return
            if normalized != (st.get("plan") or []):
                st["plan"] = normalized
                st["plan_version"] = st.get("plan_version", 0) + 1
                if st.get("phase") == "plan":
                    st["flag"] = 0
                st["task_reset_counts"] = {
                    key: value for key, value in (st.get("task_reset_counts") or {}).items()
                    if int(key) <= locked_count
                }
                changed.append(f"plan v{st['plan_version']}（{summary}）")
        elif tasks is not None:
            by_order = {t["order"]: t for t in st.get("plan") or []}
            for e in tasks:
                try:
                    o = int(e.get("order"))
                except (TypeError, ValueError):
                    self._err("tasks[].order 必須是 int")
                    return
                if o not in by_order:
                    self._err(f"order {o} 不存在於 plan——編輯只能改文字敘述,不能增刪任務")
                    return
                txt = str(e.get("task") or "").strip()
                if not txt:
                    self._err(f"order {o} 的 task 文字不可為空")
                    return
                if by_order[o]["task"] != txt:
                    by_order[o]["task"] = txt
                    changed.append(f"task-{o}")
        if body.get("clear_issues"):
            if st.get("issues"):
                changed.append(f"清除 {len(st['issues'])} 條 issues")
                st["issues"] = []
                st["issues_acknowledged_round"] = -1
        if body.get("ack_issues") and st.get("issues"):
            current_round = st.get("round", 0)
            if st.get("issues_acknowledged_round", -1) != current_round:
                st["issues_acknowledged_round"] = current_round
                changed.append(f"標記 {len(st['issues'])} 條 issues 已讀")
        if body.get("done_count") is not None:
            try:
                dc = int(body["done_count"])
                if dc < 0:
                    raise ValueError
            except (TypeError, ValueError):
                self._err("done_count 必須 ≥0 的整數")
                return
            if st.get("done_count") != dc:
                st["done_count"] = dc
                changed.append(f"done_count={dc}")
        try:
            commit_standalone_state(name, expected_state, st)
        except (SafeDeleteError, loop_mod.WorkspaceOperationLockError) as error:
            self._err(str(error), 409)
            return
        workspace_console_log(name, f"人工編輯計畫｜{', '.join(changed) or '無變更'}")
        self._out(200, json.dumps({"ok": True, "changed": changed}, ensure_ascii=False))

    @with_state_lock
    def api_edit_config(self, body):
        """停止狀態下編輯 workspace 設定(agent/validate/五顆旋鈕),存回 state.config,▶ 運行時生效。執行中鎖死。"""
        name = str(body.get("name") or "")
        st = _load_state_or_err(self, name)
        if st is None:
            return
        kind = st.get("workspace_kind")
        if kind == "fleet-child":
            self._err(f"{name} 的設定由 parallel run parent 管理", 409)
            return
        fleet = None
        if kind == "fleet-parent":
            fleet = read_parallel_run(name)
            if fleet.get("read_error"):
                self._err(fleet["read_error"])
                return
            if not require_fleet_run_id(self, body, fleet):
                return
            if fleet.get("phase") == "done":
                self._err(f"{name} 已完成；要調整設定請刪除後建立新的 parallel run", 409)
                return
            if loop_pid_alive((fleet.get("loop") or {}).get("pid")):
                self._err(f"{name} 執行中,交易參數鎖定——先停止才能改設定")
                return
        elif kind != "standalone":
            self._err(f"{name} workspace_kind 不支援設定編輯", 409)
            return
        elif not require_workspace_generation(self, body, st):
            return
        if fleet is None and ws_running(name, st):
            self._err(f"{name} 執行中,全部鎖死——先停止才能改設定")
            return
        expected_state = copy.deepcopy(st)
        expected_fleet = copy.deepcopy(fleet) if fleet is not None else None
        cfg = load_config()
        if "error" in cfg:
            self._err(cfg["error"])
            return
        c = dict((fleet or {}).get("config") or st.get("config") or {})
        changed = []
        # 數字旋鈕(round_timeout/agent_backoff_max≥0,其餘≥1)
        for k, lo in (("flag_threshold", 1), ("done_threshold", 1), ("round_timeout", 0),
                      ("agent_backoff_max", 0),
                      ("validate_timeout", 1),
                      ("red_limit", 1), ("stall_limit", 1)):
            if body.get(k) is None:
                continue
            try:
                v = parse_numeric_setting(
                    body[k], integer=k not in ("round_timeout", "agent_backoff_max", "validate_timeout"),
                    minimum=lo)
            except (TypeError, ValueError):
                self._err(f"{k} 不合法(round_timeout/agent_backoff_max 需 ≥0；"
                          "validate_timeout 需 ≥1 秒；其餘需 ≥1)")
                return
            if c.get(k) != v:
                c[k] = v
                changed.append(f"{k}={v}")
        if fleet is not None:
            for key, low, high in (("max_parallel", 1, 8), ("max_child_restarts", 0, None)):
                if body.get(key) is None:
                    continue
                try:
                    value = parse_numeric_setting(body[key], integer=True, minimum=low)
                    if high is not None and value > high:
                        raise ValueError
                except (TypeError, ValueError):
                    self._err("max_parallel 必須是 1..8；max_child_restarts 必須是 ≥0 整數")
                    return
                if c.get(key) != value:
                    c[key] = value
                    changed.append(f"{key}={value}")
        # agent 命令:只能選 config 白名單(前端傳 index;不傳=保持不變)
        if body.get("agent_idx") is not None:
            agents = cfg.get("agent_cmds") or []
            try:
                ac = agents[int(body["agent_idx"])]["cmd"]
            except (TypeError, ValueError, IndexError, KeyError):
                self._err(f"agent_idx 不合法,合法值 0..{len(agents) - 1}(請用 Agent CLI 管理器設定)")
                return
            if norm_cmd(c.get("agent_cmd")) != norm_cmd(ac):
                c["agent_cmd"] = ac
                changed.append("agent_cmd")
        # validate 命令:手寫(run 時不再過白名單,與 launch 的 validate_custom 一致)
        if body.get("validate_cmd") is not None:
            vc = str(body["validate_cmd"]).strip()
            if not vc:
                self._err("validate_cmd 不可為空")
                return
            if c.get("validate_cmd") != vc:
                c["validate_cmd"] = vc
                changed.append("validate_cmd")
        # 規劃後暫停:布林開關,下一次 ▶ 運行生效
        if body.get("pause_after_plan") is not None:
            pause = bool(body["pause_after_plan"])
            if bool(c.get("pause_after_plan")) != pause:
                c["pause_after_plan"] = pause
                changed.append(f"pause_after_plan={'on' if pause else 'off'}")
        st["config"] = c
        if fleet is not None:
            fleet["config"] = c
            if changed:
                fleet["dashboard_revision"] = fleet.get("dashboard_revision", 0) + 1
                st["fleet_truth_revision"] = fleet["dashboard_revision"]
                try:
                    write_parallel_dashboard_transaction(
                        name, fleet, st, expected_fleet=expected_fleet,
                        expected_parent_state=expected_state)
                except (OSError, RuntimeError, ValueError, SafeDeleteError,
                        loop_mod.WorkspaceOperationLockError) as error:
                    self._err(f"parallel config 寫入失敗；舊 truth 已保留：{error}", 409)
                    return
        else:
            try:
                commit_standalone_state(name, expected_state, st)
            except (SafeDeleteError, loop_mod.WorkspaceOperationLockError) as error:
                self._err(str(error), 409)
                return
        workspace_console_log(name, f"更新 Workspace 設定｜{', '.join(changed) or '無變更'}")
        self._out(200, json.dumps({"ok": True, "changed": changed}, ensure_ascii=False))

    @with_state_lock(repo_fallback=True)
    def api_validate(self, body):
        """試跑 Validate 欄位內容；可來自既有 workspace 或尚未 launch 的 repo。"""
        name = str(body.get("name") or "")
        st = None
        if name:
            st = _load_state_or_err(self, name)
            if st is None:
                return
            if st.get("workspace_kind") == "fleet-child":
                self._err(f"{name} 由 parallel run parent 管理，不能單獨執行 Validate", 409)
                return
            if st.get("workspace_kind") == "fleet-parent":
                fleet = read_parallel_run(name)
                if fleet.get("read_error"):
                    self._err(fleet["read_error"])
                    return
                if not require_fleet_run_id(self, body, fleet):
                    return
                if loop_pid_alive((fleet.get("loop") or {}).get("pid")):
                    self._err(f"{name} 執行中——先停止才能單獨確認 Validate 命令")
                    return
            elif not require_workspace_generation(self, body, st):
                return
            if ws_running(name, st):
                self._err(f"{name} 執行中——先停止才能單獨確認 Validate 命令")
                return
            runtime_config = ((fleet.get("config") or {}) if st.get("workspace_kind") == "fleet-parent"
                              else (st.get("config") or {}))
            repo = Path(str(runtime_config.get("repo") or "")).expanduser()
        else:
            repo = Path(str(body.get("repo") or "")).expanduser()
        if not (repo / ".git").exists():
            self._err(f"{repo} 不是 git repo(repo 被移走了?)")
            return
        raw = str(body.get("validate_cmd") or "").strip()
        if not raw:
            self._err("Validate 命令不可為空")
            return
        cmd = []
        try:
            cmd = shlex.split(raw)
        except ValueError as e:
            self._err(f"Validate 命令格式錯誤：{e}")
            return
        cfg = load_config()
        if "error" in cfg:
            self._err(cfg["error"])
            return
        command_problem = command_error(raw, "Validate 命令", cfg)
        if command_problem:
            self._err(command_problem)
            return
        try:
            requested_timeout = body.get("validate_timeout")
            if requested_timeout is None and st:
                requested_timeout = (st.get("config") or {}).get("validate_timeout")
            if requested_timeout is None:
                requested_timeout = (cfg.get("defaults") or {}).get("validate_timeout", 120)
            timeout = float(requested_timeout)
            if timeout <= 0:
                raise ValueError
            with stopped_workspace_command_guard(
                    name, st, fleet if name and st.get("workspace_kind") == "fleet-parent" else None):
                rc, output, timed_out = run_command_check(
                    cmd, repo, timeout=timeout, env=command_env(cfg))
            output = output.strip()
            tail = "\n".join(output.splitlines()[-50:])
            ok = rc == 0 and not timed_out
            result_text = f"執行 Validate 確認｜{'逾時 ' + f'{timeout:g}' + ' 秒' if timed_out else '通過' if ok else '失敗 rc=' + str(rc)}｜{raw}"
            if name:
                workspace_console_log(name, result_text)
            else:
                print(f"🖥️ Dashboard｜{result_text}｜repo={repo}", flush=True)
            self._out(200, json.dumps({"ok": ok, "rc": rc, "timeout": timed_out,
                                       "timeout_seconds": timeout, "tail": tail}, ensure_ascii=False))
        except ValueError:
            self._err("validate_timeout 必須 > 0 秒")
        except FileNotFoundError:
            self._err(f"找不到 Validate 命令：{cmd[0]}")
        except (SafeDeleteError, loop_mod.WorkspaceOperationLockError) as error:
            self._err(str(error), 409)

    @with_state_lock(repo_fallback=True)
    def api_preflight(self, body):
        """以 loop.py 的唯一 preflight 實作檢查目前已 commit 的 repo，不建立 state 或 Agent job。"""
        cfg = load_config()
        if "error" in cfg:
            self._err(cfg["error"])
            return
        repo = Path(str(body.get("repo") or "")).expanduser()
        if not (repo / ".git").exists():
            self._err(f"{repo} 不是 git repo")
            return
        name = str(body.get("name") or "").strip() or repo.name
        if not loop_mod.valid_workspace_name(name):
            self._err(f"workspace 名稱 {name} 不合法：{loop_mod.WORKSPACE_NAME_RULE}")
            return
        try:
            safe_workspace_dir(name)
        except ValueError as e:
            self._err(str(e))
            return
        values = cfg.get("validate_cmds") or []
        try:
            validate_cmd = values[int(body.get("validate_idx"))]["cmd"]
        except (TypeError, ValueError, IndexError, KeyError):
            self._err(f"validate_idx 不合法,合法值 0..{len(values) - 1}（完整健檢只使用已儲存的 Validate 命令）")
            return
        command_problem = command_error(validate_cmd, "Validate 命令", cfg)
        if command_problem:
            self._err(command_problem)
            return
        try:
            timeout = float(body.get("validate_timeout", (cfg.get("defaults") or {}).get("validate_timeout", 120)))
            if not (0 < timeout < float("inf")):
                raise ValueError
        except (TypeError, ValueError):
            self._err("validate_timeout 必須 > 0 秒")
            return
        command = [sys.executable, "-m", "engine.loop", "--repo", str(repo), "--name", name,
                   "--validate-cmd", validate_cmd, "--validate-timeout", str(timeout), "--preflight-only"]
        # loop.py 會自行以 validate_timeout 終止 validator；外層多留緩衝，只在意外卡住時清整群組。
        rc, output, timed_out = run_command_check(command, repo, timeout=timeout + 15, env=command_env(cfg))
        output = output.strip()
        tail = "\n".join(output.splitlines()[-100:])[-30000:]
        ok = rc == 0 and not timed_out
        status = "通過" if ok else f"逾時 {timeout + 15:g} 秒" if timed_out else f"失敗 rc={rc}"
        print(f"🖥️ Dashboard｜完整啟動前健檢｜{status}｜repo={repo}", flush=True)
        self._out(200, json.dumps({"ok": ok, "rc": rc, "timeout": timed_out,
                                   "timeout_seconds": timeout + 15, "tail": tail}, ensure_ascii=False))

    @with_state_lock(repo_fallback=True)
    def api_test_agent(self, body):
        """以固定 prompt=test 試跑白名單 Agent CLI；支援 workspace 與 launch 表單。"""
        name = str(body.get("name") or "")
        st = None
        runtime_config = None
        if name:
            st = _load_state_or_err(self, name)
            if st is None:
                return
            kind = st.get("workspace_kind")
            if kind == "fleet-child":
                self._err(f"{name} 由 parallel run parent 管理，不能單獨測試 Agent CLI", 409)
                return
            if kind == "fleet-parent":
                fleet = read_parallel_run(name)
                if fleet.get("read_error"):
                    self._err(fleet["read_error"])
                    return
                if not require_fleet_run_id(self, body, fleet):
                    return
                runtime_config = fleet.get("config") or {}
                fleet_running = loop_pid_alive((fleet.get("loop") or {}).get("pid"))
            else:
                if not require_workspace_generation(self, body, st):
                    return
                runtime_config = st.get("config") or {}
                fleet_running = False
            if fleet_running or ws_running(name, st):
                self._err(f"{name} 執行中——先停止才能單獨確認 Agent CLI")
                return
            repo = Path(str(runtime_config.get("repo") or "")).expanduser()
        else:
            repo = Path(str(body.get("repo") or "")).expanduser()
        if not (repo / ".git").exists():
            self._err(f"{repo} 不是 git repo(repo 被移走了?)")
            return
        cfg = load_config()
        if "error" in cfg:
            self._err(cfg["error"])
            return
        agents = cfg.get("agent_cmds") or []
        if body.get("agent_idx") is not None:
            try:
                raw = agents[int(body["agent_idx"])]["cmd"]
            except (TypeError, ValueError, IndexError, KeyError):
                self._err(f"agent_idx 不合法,合法值 0..{len(agents) - 1}")
                return
        else:
            if st is None:
                self._err("啟動表單必須選擇 Agent CLI")
                return
            current = norm_cmd((runtime_config or {}).get("agent_cmd", ""))
            match = next((agent.get("cmd") for agent in agents if norm_cmd(agent.get("cmd", "")) == current), None)
            if not match:
                self._err("目前的 Agent CLI 不在個人/團隊合併後的 CLI 清單內")
                return
            raw = match
        cmd = []
        try:
            cmd = shlex.split(raw)
            command_problem = command_error(raw, "Agent CLI", cfg)
            if command_problem:
                self._err(command_problem)
                return
            with stopped_workspace_command_guard(
                    name, st, fleet if name and kind == "fleet-parent" else None):
                rc, output, timed_out = run_command_check(
                    cmd, repo, prompt="test\n", timeout=60, env=command_test_env(cfg)
                )
        except (SafeDeleteError, loop_mod.WorkspaceOperationLockError) as error:
            self._err(str(error), 409)
            return
        except (ValueError, FileNotFoundError) as e:
            self._err(command_not_found("Agent CLI", cmd[0] if cmd else raw, cfg))
            return
        tail = "\n".join(output.strip().splitlines()[-100:])
        if len(tail) > 30000:
            tail = tail[-30000:]
        result_text = f"執行 Agent CLI 確認｜{'逾時' if timed_out else 'exit ' + str(rc)}｜prompt=test｜{raw}"
        if name:
            workspace_console_log(name, result_text)
        else:
            print(f"🖥️ Dashboard｜{result_text}｜repo={repo}", flush=True)
        self._out(200, json.dumps({"ok": rc == 0 and not timed_out, "rc": rc,
                                   "timeout": timed_out, "output": tail}, ensure_ascii=False))

    @with_state_lock(repo_fallback=True)
    def api_test_cli(self, body):
        """CLI 管理器測試尚未儲存的 command/PATH 草稿；固定 prompt=test。"""
        name = str(body.get("name") or "")
        if name:
            st = _load_state_or_err(self, name)
            if st is None:
                return
            kind = st.get("workspace_kind")
            if kind == "fleet-child":
                self._err(f"{name} 由 parallel run parent 管理，不能單獨測試 Agent CLI", 409)
                return
            if kind == "fleet-parent":
                fleet = read_parallel_run(name)
                if fleet.get("read_error"):
                    self._err(fleet["read_error"])
                    return
                if not require_fleet_run_id(self, body, fleet):
                    return
                runtime_config = fleet.get("config") or {}
                fleet_running = loop_pid_alive((fleet.get("loop") or {}).get("pid"))
            else:
                if not require_workspace_generation(self, body, st):
                    return
                runtime_config = st.get("config") or {}
                fleet_running = False
            if fleet_running or ws_running(name, st):
                self._err(f"{name} 執行中——先停止才能測試 Agent CLI")
                return
            repo = Path(str(runtime_config.get("repo") or "")).expanduser()
        else:
            repo = Path(str(body.get("repo") or "")).expanduser()
        if not (repo / ".git").exists():
            self._err(f"{repo} 不是 git repo(repo 被移走了?)")
            return
        raw = str(body.get("agent_cmd") or "").strip()
        cfg = load_config()
        if "error" in cfg:
            self._err(cfg["error"])
            return
        draft_paths = body.get("extra_path_dirs")
        if draft_paths is not None:
            if not isinstance(draft_paths, list):
                self._err("extra_path_dirs 必須是字串陣列")
                return
            cfg = dict(cfg)
            cfg["extra_path_dirs"] = [str(value).strip() for value in draft_paths if str(value).strip()]
        command_problem = command_error(raw, "Agent CLI", cfg)
        if command_problem:
            self._err(command_problem)
            return
        cmd = shlex.split(raw)
        try:
            with stopped_workspace_command_guard(
                    name, st if name else None,
                    fleet if name and kind == "fleet-parent" else None):
                rc, output, timed_out = run_command_check(
                    cmd, repo, prompt="test\n", timeout=60, env=command_test_env(cfg)
                )
        except (SafeDeleteError, loop_mod.WorkspaceOperationLockError) as error:
            self._err(str(error), 409)
            return
        except FileNotFoundError:
            self._err(command_not_found("Agent CLI", cmd[0], cfg))
            return
        tail = "\n".join(output.strip().splitlines()[-100:])[-30000:]
        self._out(200, json.dumps({"ok": rc == 0 and not timed_out, "rc": rc,
                                   "timeout": timed_out, "output": tail}, ensure_ascii=False))

    def api_edit_cli_config(self, body):
        """CLI 管理器一次儲存 Agent 清單與額外 PATH；保留 config 其他欄位。"""
        raw_agents = body.get("agent_cmds")
        raw_paths = body.get("extra_path_dirs")
        if not isinstance(raw_agents, list) or not (1 <= len(raw_agents) <= 50):
            self._err("Agent CLI 必須保留 1～50 個項目")
            return
        agents = []
        for index, item in enumerate(raw_agents, 1):
            if not isinstance(item, dict):
                self._err(f"Agent CLI #{index} 格式錯誤")
                return
            label = str(item.get("label") or "").strip()
            cmd = str(item.get("cmd") or "").strip()
            if not label or not cmd:
                self._err(f"Agent CLI #{index} 的名稱與 command 不可為空")
                return
            if len(label) > 80 or len(cmd) > 2000:
                self._err(f"Agent CLI #{index} 內容過長")
                return
            try:
                shlex.split(cmd)
            except ValueError as e:
                self._err(f"Agent CLI #{index} command 格式錯誤：{e}")
                return
            agents.append({"label": label, "cmd": cmd})
        if not isinstance(raw_paths, list) or len(raw_paths) > 50:
            self._err("PATH 目錄必須是最多 50 個字串的陣列")
            return
        paths = []
        for value in raw_paths:
            path = str(value).strip()
            if not path:
                continue
            if len(path) > 1000:
                self._err("PATH 目錄內容過長")
                return
            if path not in paths:
                paths.append(path)
        with CONFIG_LOCK:
            cfg = load_config()
            if "error" in cfg:
                self._err(cfg["error"])
                return
            try:
                save_personal_config({"agent_cmds": agents, "extra_path_dirs": paths})
            except ValueError as e:
                self._err(str(e))
                return
            cfg = load_config()
        self._out(200, json.dumps(config_projection(cfg), ensure_ascii=False))

    def api_edit_repo_roots(self, body):
        """Repo root 管理器儲存掃描根目錄；支援 ~ 與 $HOME。"""
        raw_roots = body.get("repo_roots")
        if not isinstance(raw_roots, list) or not (1 <= len(raw_roots) <= 50):
            self._err("Repo roots 必須保留 1～50 個目錄")
            return
        roots = []
        for value in raw_roots:
            root = str(value).strip()
            if not root:
                continue
            if len(root) > 1000:
                self._err("Repo root 內容過長")
                return
            if root not in roots:
                roots.append(root)
        if not roots:
            self._err("至少需要一個非空白 Repo root")
            return
        with CONFIG_LOCK:
            cfg = load_config()
            if "error" in cfg:
                self._err(cfg["error"])
                return
            try:
                save_personal_config({"repo_roots": roots})
            except ValueError as e:
                self._err(str(e))
                return
            cfg = load_config()
        self._out(200, json.dumps(config_projection(cfg), ensure_ascii=False))

    def api_edit_notify(self, body):
        """儲存個人設定的終態通知命令(佔位符 {status}/{name});空字串=停用。"""
        raw = body.get("notify_cmd")
        if not isinstance(raw, str) or len(raw) > 2000:
            self._err("notify_cmd 必須是 ≤2000 字元的字串(空=停用通知)")
            return
        raw = raw.strip()
        if raw:
            try:
                shlex.split(raw.replace("{status}", "test").replace("{name}", "test"))
            except ValueError as e:
                self._err(f"通知命令格式錯誤:{e}")
                return
        with CONFIG_LOCK:
            cfg = load_config()
            if "error" in cfg:
                self._err(cfg["error"])
                return
            try:
                save_personal_config({"notify_cmd": raw})
            except ValueError as e:
                self._err(str(e))
                return
            cfg = load_config()
        self._out(200, json.dumps(config_projection(cfg), ensure_ascii=False))

    def api_test_notify(self, body):
        """以 {status}=test/{name}=dashboard-test 實跑通知命令(替換規則同 loop 終態通知),15 秒上限。"""
        raw = str(body.get("notify_cmd") or "").strip()
        if not raw:
            self._err("通知命令為空,沒有可測試的內容")
            return
        cfg = load_config()
        if "error" in cfg:
            self._err(cfg["error"])
            return
        rendered = raw.replace("{status}", "test").replace("{name}", "dashboard-test")
        command_problem = command_error(rendered, "通知命令", cfg)
        if command_problem:
            self._err(command_problem)
            return
        rc, output, timed_out = run_command_check(shlex.split(rendered), HERE,
                                                  timeout=15, env=command_env(cfg))
        tail = "\n".join(output.strip().splitlines()[-20:])[-4000:]
        self._out(200, json.dumps({"ok": rc == 0 and not timed_out, "rc": rc,
                                   "timeout": timed_out, "output": tail}, ensure_ascii=False))

    @with_state_lock
    def api_phase(self, body):
        """停止狀態下切換 phase:exec/done → plan(執行進度歸零,計畫保留);plan → exec(直接開做)。"""
        name = str(body.get("name") or "")
        st = _load_state_or_err(self, name)
        if st is None:
            return
        if st.get("workspace_kind") != "standalone":
            self._err(f"{name} 由 parallel run 管理，不能人工切換 phase", 409)
            return
        if not require_workspace_generation(self, body, st):
            return
        if ws_running(name, st):
            self._err(f"{name} 執行中,全部鎖死——先停止才能切換 phase")
            return
        expected_state = copy.deepcopy(st)
        target = str(body.get("phase") or "")
        if target == "plan":
            if st.get("phase") not in ("exec", "done"):
                self._err(f"目前是 {st.get('phase')},只有 exec/done 能回規劃期")
                return
            st.update(phase="plan", flag=0, current_order=0, done_count=0, completed=[],
                      red_streak=0, stall_rounds=0, task_reset_counts={}, goal_changed=False)
            st.pop("goal_previous_hash", None)
        elif target == "exec":
            if st.get("phase") != "plan":
                self._err(f"目前是 {st.get('phase')},只有 plan 能直接切執行期(exec/done 請用進度管理)")
                return
            if not st.get("plan"):
                self._err("plan 為空,不能進執行期——先 create-plan 或匯入 plan.json")
                return
            st.update(phase="exec", flag=0, done_count=0, current_order=st["plan"][0]["order"],
                      stall_rounds=0, red_streak=0)  # 規劃期殘值不帶進執行期
        else:
            self._err("phase 只能是 plan 或 exec")
            return
        try:
            commit_standalone_state(name, expected_state, st)
        except (SafeDeleteError, loop_mod.WorkspaceOperationLockError) as error:
            self._err(str(error), 409)
            return
        workspace_console_log(name, f"切換階段｜{'規劃期' if target == 'plan' else '執行期'}")
        self._out(200, json.dumps({"ok": True, "phase": target}, ensure_ascii=False))

    @with_state_lock
    def api_set_task(self, body):
        """停止狀態下的進度管理:退回重做,或往前跳(validate 綠才放行,被跳過的標人工完成)。"""
        name = str(body.get("name") or "")
        st = _load_state_or_err(self, name)
        if st is None:
            return
        if st.get("workspace_kind") != "standalone":
            self._err(f"{name} 由 parallel run 管理，不能人工跳 task", 409)
            return
        if not require_workspace_generation(self, body, st):
            return
        if ws_running(name, st):
            self._err(f"{name} 執行中,全部鎖死——先停止才能調整進度")
            return
        expected_state = copy.deepcopy(st)
        if st.get("phase") not in ("exec", "done"):
            self._err(f"目前是 {st.get('phase')},進度管理只在 exec/done 可用")
            return
        try:
            order = int(body.get("order"))
        except (TypeError, ValueError):
            self._err("order 必須是 int")
            return
        plan_orders = [t["order"] for t in st.get("plan") or []]
        if order not in plan_orders:
            self._err(f"order {order} 不在 plan 裡,合法值:{plan_orders}")
            return
        try:
            with standalone_mutation_guard(name, expected_state):
                completed = st.get("completed") or []
                done_orders = {e["order"] for e in completed}
                skipped = [o for o in plan_orders if o < order and o not in done_orders]
                if skipped:  # 往前跳:validate 與 commit 共用同一個 writer exclusion context
                    c = st.get("config") or {}
                    repo, vcmd = c.get("repo"), c.get("validate_cmd")
                    if not (repo and vcmd):
                        self._err("state 缺 repo/validate 設定,無法驗證——先用啟動表單跑過一次")
                        return
                    cfg = load_config()
                    if "error" in cfg:
                        self._err(cfg["error"])
                        return
                    try:
                        timeout = float(c.get("validate_timeout", 120))
                        if not (0 < timeout < float("inf")):
                            raise ValueError
                        command = shlex.split(vcmd)
                        if not command:
                            raise ValueError
                        rc, output, timed_out = run_command_check(
                            command, repo, timeout=timeout, env=command_env(cfg))
                    except (OSError, ValueError) as e:
                        self._err(f"validate 設定無法執行,不能往後跳:{e}")
                        return
                    tail = "\n".join(output.strip().splitlines()[-15:])
                    if timed_out:
                        self._err(f"validate 逾時 {timeout:g} 秒,不能往後跳(同 preflight 原則):\n{tail}")
                        return
                    if rc != 0:
                        self._err(f"validate 未過,不能往後跳(同 preflight 原則):\n{tail}")
                        return
                    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                          capture_output=True, text=True).stdout.strip()
                    for o in skipped:
                        completed.append({"order": o, "sha": head, "round": 0, "human": True})
                # 目標(含)之後的完成紀錄清除 → 從 order 重新執行
                completed = sorted([e for e in completed if e["order"] < order],
                                   key=lambda e: e["order"])
                st["completed"] = completed
                st.update(phase="exec", current_order=order, done_count=0,
                          red_streak=0, stall_rounds=0)
                write_state(name, st)
        except (SafeDeleteError, loop_mod.WorkspaceOperationLockError) as error:
            self._err(str(error), 409)
            return
        workspace_console_log(
            name,
            f"調整任務進度｜前往 task-{order}"
            + (f"｜人工標記完成：{', '.join(f'task-{value}' for value in skipped)}" if skipped else ""),
        )
        self._out(200, json.dumps({"ok": True, "current_order": order,
                                   "human_marked": skipped}, ensure_ascii=False))

    @with_state_lock
    def api_delete_workspace(self, body):
        """安全永久刪除已停止的 standalone workspace；不觸碰 target repo。"""
        name = str(body.get("name") or "")
        try:
            pending_delete = _load_delete_journal(name)
            if pending_delete is not None:
                preserved = _resume_delete_journal(pending_delete)
                if preserved:
                    if name not in preserved:
                        with JOBS_LOCK:
                            JOBS.pop(name, None)
                    self._out(409, json.dumps({
                        "error": "先前刪除已完成；同名新 workspace 已保留，請重新載入並再次確認目前 run",
                        "name": name, "deleted": False, "resumed_delete": True,
                        "replacement_preserved": True,
                        "preserved_workspaces": sorted(preserved),
                    }, ensure_ascii=False))
                    return
                with JOBS_LOCK:
                    JOBS.pop(name, None)
                self._out(200, json.dumps({"ok": True, "name": name, "deleted": True,
                                           "resumed_delete": True}, ensure_ascii=False))
                return
        except (SafeDeleteError, loop_mod.WorkspaceOperationLockError, OSError, ValueError) as error:
            self._err(f"pending delete 尚未完成，可安全重試:{error}", 409)
            return
        st, state_error = read_state(name, repair=False)
        if state_error:
            legacy_identity = legacy_workspace_identity_for_delete(name)
            if legacy_identity is None:
                self._err(state_error)
                return
            if body.get("workspace_generation") != legacy_identity["generation"]:
                self._err("legacy workspace 已更新或同名重建，請重新載入後再操作", 409)
                return
            legacy_state = legacy_identity["state"]
            if ws_running(name, legacy_state):
                self._err(f"{name} 舊版 workspace 仍在執行，不能刪除——先停止", 409)
                return
            try:
                with loop_mod.workspace_operation_lock(ROOT, name, blocking=False):
                    _delete_race_hook("standalone-before-writer-lock", name)
                    _safe_remove_workspace_tree(
                        name, generation_source="delete-marker",
                        expected_generation=legacy_identity["generation"],
                        expected_inode=legacy_identity["inode"])
            except (SafeDeleteError, loop_mod.WorkspaceOperationLockError) as error:
                self._err(str(error), getattr(error, "status", 409))
                return
            except OSError as error:
                self._err(f"legacy workspace delete 尚未完成，可安全重試:{error}", 409)
                return
            with JOBS_LOCK:
                JOBS.pop(name, None)
            print(f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜永久刪除 legacy workspace {name}",
                  flush=True)
            self._out(200, json.dumps({"ok": True, "name": name, "deleted": True,
                                       "legacy_delete_only": True}, ensure_ascii=False))
            return
        if st is None:
            self._err(f"workspace {name} 不存在", 404)
            return
        kind = st.get("workspace_kind")
        if kind == "fleet-child":
            self._err(f"{name} 由 parallel run parent 管理，不能單獨刪除", 409)
            return
        if kind == "standalone" and not require_workspace_generation(self, body, st):
            return
        if ws_running(name, st):
            self._err(f"{name} 執行中，不能刪除——先停止")
            return
        fleet_state = read_parallel_run(name) if kind == "fleet-parent" else None
        if fleet_state is not None:
            if fleet_state.get("read_error"):
                self._err(fleet_state["read_error"])
                return
            if not require_fleet_run_id(self, body, fleet_state):
                return
            try:
                parent = safe_workspace_dir(name)
                with ExitStack() as locks:
                    locks.enter_context(loop_mod.workspace_operation_lock(ROOT, name, blocking=False))
                    parent_handle = locks.enter_context(locked_workspace_entry(
                        name, (".fleet.run.lock", ".run.lock")))
                    current, current_error = read_state(name, repair=False)
                    current_fleet = read_parallel_run(name)
                    _require_same_directory_entry(
                        parent_handle["root_fd"], name, parent_handle["workspace_fd"],
                        f"workspace {name}")
                    if (current_error or current is None or current.get("fleet_run_id") != fleet_state.get("run_id") or
                            current_fleet.get("read_error") or
                            current_fleet.get("run_id") != fleet_state.get("run_id")):
                        raise SafeDeleteError("parallel run 在刪除 preflight 期間已更新，未做任何刪除", 409)
                    fleet_state = current_fleet
                    if loop_pid_alive((fleet_state.get("loop") or {}).get("pid")):
                        raise SafeDeleteError(f"{name} parallel run 執行中，不能刪除——先停止", 409)
                    current_session = str((fleet_state.get("loop") or {}).get("session_id") or "")
                    session_history = fleet_state.get("supervisor_session_history")
                    if session_history is None:
                        # Read-only compatibility for a stopped pre-history run.
                        session_history = [current_session]
                    if (not isinstance(session_history, list) or not session_history or
                            len(session_history) > 100 or len(session_history) != len(set(session_history)) or
                            current_session not in session_history or
                            any(re.fullmatch(r"[0-9a-f]{32}", str(item)) is None
                                for item in session_history)):
                        raise SafeDeleteError("fleet supervisor session history 不合法", 409)

                    integration_repo = Path(str(
                        fleet_state.get("integration_worktree") or
                        (fleet_state.get("config") or {}).get("repo") or "")).expanduser().resolve()
                    configured_repo = str((fleet_state.get("config") or {}).get("repo") or "")
                    if configured_repo and Path(configured_repo).expanduser().resolve() != integration_repo:
                        raise SafeDeleteError("fleet config.repo 與 integration worktree 不符", 409)
                    top = Path(_git_delete_check(integration_repo, "rev-parse", "--show-toplevel")).resolve()
                    if top != integration_repo:
                        raise SafeDeleteError("integration worktree 身分不符", 409)
                    integration_ref = str(fleet_state.get("integration_ref") or
                                          _git_delete_check(integration_repo, "symbolic-ref", "-q", "HEAD"))
                    common_dir = _git_directory(integration_repo, "--git-common-dir")
                    lock_key = hashlib.sha256(integration_ref.encode()).hexdigest()[:16]
                    locks.enter_context(exclusive_file_lock(
                        common_dir / f"loop-fleet-{lock_key}.lock", f"integration ref {integration_ref} 鎖"))
                    inventory = _worktree_inventory(integration_repo)
                    worktrees_root = parent / "worktrees"
                    try:
                        worktrees_info = worktrees_root.lstat()
                    except FileNotFoundError:
                        worktrees_info = None
                    if worktrees_info is not None and (
                            stat.S_ISLNK(worktrees_info.st_mode) or not stat.S_ISDIR(worktrees_info.st_mode)):
                        raise SafeDeleteError("parallel worktrees 根目錄不是實體目錄", 409)
                    removals = []
                    journal_worktrees = []
                    child_handles = []
                    seen_worktrees = set()
                    seen_children = set()
                    for track in fleet_state.get("tracks") or []:
                        track_name = str(track.get("name") or "")
                        expected_safe = loop_mod.fleet_track_safe_name(track_name)
                        safe = str(track.get("safe_name") or "")
                        if safe != expected_safe or Path(safe).name != safe or safe in {".", ".."}:
                            raise SafeDeleteError(f"track {track_name} safe name 不合法", 409)
                        expected_tombstone = loop_mod.workspace_path(
                            ROOT, f"delete-{fleet_state['run_id']}-{safe}")
                        persisted_tombstone = track.get("cleanup_child_tombstone")
                        if (persisted_tombstone is not None and
                                str(persisted_tombstone) != str(expected_tombstone)):
                            raise SafeDeleteError(f"track {track_name} cleanup tombstone 身分不符", 409)
                        if (track.get("cleanup_stage") == "child-removing" or
                                loop_mod.workspace_directory(
                                    expected_tombstone, f"track {track_name} cleanup tombstone") is not None):
                            raise SafeDeleteError(
                                f"track {track_name} cleanup 正在 child-removing；請先 resume Fleet", 409)
                        expected_worktree = (worktrees_root / safe).resolve()
                        try:
                            expected_worktree.relative_to(parent.resolve())
                        except ValueError as error:
                            raise SafeDeleteError(f"track {track_name} worktree 逸出 parent", 409) from error
                        worktree = Path(str(track.get("worktree") or expected_worktree)).expanduser().resolve()
                        expected_branch = f"refs/heads/loop/{fleet_state['run_id']}/{safe}"
                        if (worktree != expected_worktree or worktree in seen_worktrees or
                                track.get("branch_ref") != expected_branch):
                            raise SafeDeleteError(f"track {track_name} worktree/branch persisted 身分不符", 409)
                        branch_tip = _git_delete_check(integration_repo, "rev-parse", expected_branch)
                        journal_worktrees.append({"track": track_name, "safe_name": safe,
                                                  "path": str(worktree),
                                                  "branch_ref": expected_branch,
                                                  "branch_tip": branch_tip})
                        persisted_tip = track.get("tip")
                        if persisted_tip is not None and (
                                re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", str(persisted_tip)) is None or
                                persisted_tip != branch_tip):
                            raise SafeDeleteError(f"track {track_name} preserved branch tip 不符", 409)
                        seen_worktrees.add(worktree)
                        registered = inventory.get(worktree)
                        try:
                            worktree_info = worktree.lstat()
                        except FileNotFoundError:
                            worktree_info = None
                        if worktree_info is None:
                            if (registered is not None or track.get("status") not in {"merged", "cleaned"} or
                                    persisted_tip is None):
                                raise SafeDeleteError(f"track {track_name} worktree 現場與 registration 不符", 409)
                        else:
                            if (stat.S_ISLNK(worktree_info.st_mode) or not stat.S_ISDIR(worktree_info.st_mode) or
                                    registered is None or registered.get("branch") != expected_branch or
                                    _git_delete_check(worktree, "symbolic-ref", "-q", "HEAD") != expected_branch or
                                    _git_directory(worktree, "--git-common-dir") != common_dir or
                                    _git_delete_check(worktree, "rev-parse", "HEAD") != branch_tip):
                                raise SafeDeleteError(f"track {track_name} branch/common-dir 身分不符", 409)
                            if _git_delete_check(worktree, "status", "--porcelain"):
                                raise SafeDeleteError(f"track {track_name} worktree 不乾淨，未做任何刪除", 409)
                            child_git_dir = _git_directory(worktree, "--git-dir")
                            locks.enter_context(exclusive_file_lock(
                                child_git_dir / "loop-agent-lite.run.lock", f"track {track_name} writer 鎖"))
                            removals.append(worktree)

                        child_name = str(track.get("child_workspace") or "")
                        expected_child = f"{name}--{safe}"
                        if child_name != expected_child or not loop_mod.valid_workspace_name(child_name) or child_name in seen_children:
                            raise SafeDeleteError(f"track {track_name} child workspace 身分不符", 409)
                        seen_children.add(child_name)
                        child_path = safe_workspace_dir(child_name)
                        try:
                            child_info = child_path.lstat()
                        except FileNotFoundError:
                            child_info = None
                        if child_info is None:
                            if track.get("status") not in {"merged", "cleaned"}:
                                raise SafeDeleteError(f"child {child_name} 不存在但 track 尚未清理", 409)
                            continue
                        if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(child_info.st_mode):
                            raise SafeDeleteError(f"child {child_name} 不是實體 workspace", 409)
                        child_state, child_error = read_state(child_name, repair=False)
                        child_config = ((child_state or {}).get("config")
                                        if isinstance((child_state or {}).get("config"), dict) else {})
                        fleet_config = fleet_state.get("config") or {}
                        expected_tasks = []
                        for local_order, task in enumerate(
                                (item for item in fleet_state.get("plan") or []
                                 if isinstance(item, dict) and item.get("track") == track_name), 1):
                            local_task = {key: value for key, value in task.items() if key != "order"}
                            local_task["order"] = local_order
                            expected_tasks.append(local_task)
                        try:
                            same_agent = (shlex.split(str(child_config.get("agent_cmd") or "")) ==
                                          shlex.split(str(fleet_config.get("agent_cmd") or "")))
                            same_validate = (shlex.split(str(child_config.get("validate_cmd") or "")) ==
                                             shlex.split(str(fleet_config.get("validate_cmd") or "")))
                        except ValueError:
                            same_agent = same_validate = False
                        if (child_error or child_state is None or child_state.get("workspace_kind") != "fleet-child" or
                                child_state.get("fleet_run_id") != fleet_state.get("run_id") or
                                child_state.get("fleet_parent") != name or child_state.get("track") != track_name or
                                child_state.get("fleet_parent_session_id") not in session_history or
                                child_state.get("merge_target_ref") != integration_ref or
                                Path(str(child_config.get("repo") or "")).expanduser().resolve() != worktree or
                                child_state.get("plan") != expected_tasks or
                                not same_agent or not same_validate):
                            raise SafeDeleteError(f"child {child_name} state 身分不符", 409)
                        if ws_running(child_name, child_state):
                            raise SafeDeleteError(f"child {child_name} 仍在執行，未做任何刪除", 409)
                        locks.enter_context(loop_mod.workspace_operation_lock(ROOT, child_name, blocking=False))
                        _delete_race_hook("child-before-lock", child_name)
                        child_handle = locks.enter_context(locked_workspace_entry(
                            child_name, expected_inode=(child_info.st_dev, child_info.st_ino)))
                        locked_child_state = _read_state_from_directory_fd(
                            child_handle["workspace_fd"])
                        locked_config = (locked_child_state.get("config")
                                         if isinstance(locked_child_state.get("config"), dict) else {})
                        try:
                            locked_same_agent = (
                                shlex.split(str(locked_config.get("agent_cmd") or "")) ==
                                shlex.split(str(fleet_config.get("agent_cmd") or "")))
                            locked_same_validate = (
                                shlex.split(str(locked_config.get("validate_cmd") or "")) ==
                                shlex.split(str(fleet_config.get("validate_cmd") or "")))
                        except ValueError:
                            locked_same_agent = locked_same_validate = False
                        if (locked_child_state.get("workspace_kind") != "fleet-child" or
                                locked_child_state.get("fleet_run_id") != fleet_state.get("run_id") or
                                locked_child_state.get("fleet_parent") != name or
                                locked_child_state.get("track") != track_name or
                                locked_child_state.get("fleet_parent_session_id") not in session_history or
                                locked_child_state.get("merge_target_ref") != integration_ref or
                                Path(str(locked_config.get("repo") or "")).expanduser().resolve() != worktree or
                                locked_child_state.get("plan") != expected_tasks or
                                not locked_same_agent or not locked_same_validate):
                            raise SafeDeleteError(
                                f"child {child_name} locked state 身分不符", 409)
                        locked_loop = (locked_child_state.get("loop")
                                       if isinstance(locked_child_state.get("loop"), dict) else {})
                        if loop_pid_alive(locked_loop.get("pid")):
                            raise SafeDeleteError(
                                f"child {child_name} locked state pid 仍存活", 409)
                        child_handles.append(child_handle)

                    # All identities, cleanliness, child liveness, and locks are proven before the first mutation.
                    delete_handles = [*child_handles, parent_handle]
                    delete_journal = _delete_journal(
                        name, "fleet-group", fleet_state.get("run_id"), delete_handles,
                        git_identity={"repo": str(integration_repo),
                                      "common_dir": str(common_dir),
                                      "integration_ref": integration_ref,
                                      "worktrees": journal_worktrees})
                    _write_delete_journal(delete_journal)
                    for worktree in removals:
                        removed = subprocess.run(["git", "-C", str(integration_repo), "worktree", "remove", str(worktree)],
                                                 capture_output=True, text=True)
                        if removed.returncode:
                            raise SafeDeleteError(
                                f"移除 worktree 失敗:{(removed.stderr or removed.stdout)[-400:]}", 409)
                        _delete_worktree_hook("after-remove", str(worktree))
                    pruned = subprocess.run(["git", "-C", str(integration_repo), "worktree", "prune"],
                                            capture_output=True, text=True)
                    if pruned.returncode:
                        raise SafeDeleteError(f"git worktree prune 失敗:{(pruned.stderr or pruned.stdout)[-400:]}", 409)
                    journal_entries = {entry["name"]: entry
                                       for entry in delete_journal["entries"]}
                    for child_handle in child_handles:
                        _delete_race_hook("child-before-delete", child_handle["name"])
                        _remove_locked_workspace(
                            child_handle, journal_entries[child_handle["name"]]["tombstone"])
                    _delete_race_hook("parent-before-delete", name)
                    _remove_locked_workspace(parent_handle, journal_entries[name]["tombstone"])
                    _clear_delete_journal(name)
            except (SafeDeleteError, loop_mod.WorkspaceOperationLockError) as error:
                self._err(str(error), getattr(error, "status", 409))
                return
            except (OSError, ValueError) as error:
                self._err(str(error), 409)
                return
            with JOBS_LOCK:
                JOBS.pop(name, None)
            print(f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜永久刪除 parallel workspace {name}", flush=True)
            self._out(200, json.dumps({"ok": True, "name": name, "deleted": True}, ensure_ascii=False))
            return
        try:
            safe_workspace_dir(name)
            with loop_mod.workspace_operation_lock(ROOT, name, blocking=False):
                _delete_race_hook("standalone-before-writer-lock", name)
                _safe_remove_workspace_tree(
                    name, expected_generation=st.get("workspace_generation"))
        except SafeDeleteError as e:
            self._err(str(e), e.status)
            return
        except loop_mod.WorkspaceOperationLockError as e:
            self._err(str(e), 409)
            return
        except OSError as e:
            self._err(f"workspace delete 尚未完成，可安全重試:{e}", 409)
            return
        except ValueError as e:
            self._err(str(e))
            return
        with JOBS_LOCK:
            JOBS.pop(name, None)
        print(f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜永久刪除 workspace {name}", flush=True)
        self._out(200, json.dumps({"ok": True, "name": name, "deleted": True}, ensure_ascii=False))

    @with_state_lock
    def api_drain(self, body):
        """要求目前 session 在完整處理本輪後停止；只寫旁路控制檔，不競寫 loop state。"""
        name = str(body.get("name") or "")
        st = _load_state_or_err(self, name)
        if st is None:
            return
        if st.get("workspace_kind") != "standalone":
            self._err(f"{name} 請由 parallel run parent 停止；child 不接受一般 drain", 409)
            return
        if not require_workspace_generation(self, body, st):
            return
        loop_state = st.get("loop") or {}
        pid = loop_state.get("pid")
        session_id = loop_state.get("session_id")
        try:
            expected_pid = int(body.get("expected_pid"))
            if expected_pid <= 0 or expected_pid != int(pid or 0):
                raise ValueError
        except (TypeError, ValueError):
            self._err(f"{name} 執行程序已更新或畫面過期，請重新載入後再操作", 409)
            return
        if not loop_pid_alive(pid):
            self._out(200, json.dumps({"ok": True, "name": name, "already_stopped": True},
                                      ensure_ascii=False))
            return
        if not session_id:
            self._err(f"{name} 是由舊版 loop 啟動，請先立即停止並用目前版本重新運行")
            return
        with JOBS_LOCK:
            job = JOBS.get(name)
        if job is not None and job.alive() and int(pid) != job.popen.pid:
            self._err(f"{name} 尚在啟動中，請等待執行狀態就緒後再要求本輪後停止")
            return
        try:
            workspace_dir = safe_workspace_dir(name)
        except ValueError as e:
            self._err(str(e))
            return
        if loop_mod.stop_after_round_claimed(workspace_dir, pid, session_id):
            self._out(200, json.dumps({"ok": True, "name": name, "pid": pid,
                                       "requested": True, "claimed": True}, ensure_ascii=False))
            return
        if loop_mod.stop_after_round_requested(workspace_dir, pid, session_id):
            self._out(200, json.dumps({"ok": True, "name": name, "pid": pid,
                                       "requested": True, "already_requested": True},
                                      ensure_ascii=False))
            return
        payload = {"pid": int(pid), "session_id": session_id,
                   "requested_at": datetime.now().isoformat(timespec="seconds")}
        loop_mod.atomic_write_bytes(
            workspace_dir / loop_mod.STOP_AFTER_ROUND_FILE,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        workspace_console_log(name, f"已要求本輪完整結束後停止｜pid={pid}")
        self._out(200, json.dumps({"ok": True, "name": name, "pid": pid,
                                   "requested": True}, ensure_ascii=False))

    @with_state_lock
    def api_cancel_drain(self, body):
        """撤銷尚未被 loop 取走的本輪後停止請求；claim 競態輸家必須明確回報太晚。"""
        name = str(body.get("name") or "")
        st = _load_state_or_err(self, name)
        if st is None:
            return
        if st.get("workspace_kind") != "standalone":
            self._err(f"{name} 由 parallel run parent 管理，不能撤銷 child/parent stop control", 409)
            return
        if not require_workspace_generation(self, body, st):
            return
        loop_state = st.get("loop") or {}
        pid = loop_state.get("pid")
        session_id = loop_state.get("session_id")
        try:
            expected_pid = int(body.get("expected_pid"))
            if expected_pid <= 0 or expected_pid != int(pid or 0):
                raise ValueError
        except (TypeError, ValueError):
            self._err(f"{name} 執行程序已更新或畫面過期，請重新載入後再操作", 409)
            return
        if not loop_pid_alive(pid):
            self._out(200, json.dumps({"ok": True, "name": name, "already_stopped": True},
                                      ensure_ascii=False))
            return
        if not session_id:
            self._err(f"{name} 是由舊版 loop 啟動，請先立即停止並用目前版本重新運行")
            return
        try:
            workspace_dir = safe_workspace_dir(name)
        except ValueError as e:
            self._err(str(e))
            return
        if loop_mod.stop_after_round_claimed(workspace_dir, pid, session_id):
            self._err(f"{name} 的停止請求已被 loop 取走，這一輪會在完成後停止，無法再撤銷", 409)
            return
        # 先無副作用確認，才以 consume=True 原子 claim。loop 若先 claim，絕不假裝已撤銷。
        if not loop_mod.stop_after_round_requested(workspace_dir, pid, session_id):
            if loop_mod.stop_after_round_claimed(workspace_dir, pid, session_id):
                self._err(f"{name} 的停止請求已被 loop 取走，這一輪會在完成後停止，無法再撤銷", 409)
                return
            self._out(200, json.dumps({"ok": True, "name": name, "not_requested": True},
                                      ensure_ascii=False))
            return
        if not loop_mod.stop_after_round_requested(workspace_dir, pid, session_id, consume=True):
            if not loop_mod.stop_after_round_claimed(workspace_dir, pid, session_id):
                self._err(f"{name} 的停止請求狀態剛變更，請重新整理後確認是否仍在收尾", 409)
                return
            self._err(f"{name} 的停止請求已被 loop 取走，這一輪會在完成後停止，無法再撤銷", 409)
            return
        workspace_console_log(name, f"已撤銷本輪後停止｜pid={pid}")
        self._out(200, json.dumps({"ok": True, "name": name, "pid": pid,
                                   "cancelled": True}, ensure_ascii=False))

    @with_state_lock
    def api_stop(self, body):
        """Stop standalone immediately; fleet-parent uses run-bound graceful sideband control."""
        name = str(body.get("name") or "")
        if not loop_mod.valid_workspace_name(name):
            self._err(f"workspace 名稱 {name or '(空)'} 不合法：{loop_mod.WORKSPACE_NAME_RULE}")
            return
        expected_pid = body.get("expected_pid")
        if expected_pid is not None:
            try:
                expected_pid = int(expected_pid)
                if expected_pid <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                self._err("expected_pid 必須是正整數", 409)
                return
        st, _ = read_state(name, repair=False)
        if st and st.get("workspace_kind") == "fleet-child":
            self._err(f"{name} 由 parallel run parent 管理，不能單獨停止", 409)
            return
        if st and st.get("workspace_kind") == "fleet-parent":
            fleet_state = read_parallel_run(name)
            if fleet_state.get("read_error"):
                self._err(fleet_state["read_error"])
                return
            if not require_fleet_run_id(self, body, fleet_state):
                return
            pid = (fleet_state.get("loop") or {}).get("pid")
            if expected_pid is not None and int(pid or 0) != expected_pid:
                self._err("job 已更新或同名重建，請重新載入後再停止", 409)
                return
            if not loop_pid_alive(pid):
                self._out(200, json.dumps({"ok": True, "name": name,
                                           "already_stopped": True}, ensure_ascii=False))
                return
            control = {"schema_version": 1, "run_id": fleet_state["run_id"], "action": "stop",
                       "request_id": uuid.uuid4().hex,
                       "requested_at": datetime.now().astimezone().isoformat(timespec="seconds")}
            loop_mod.atomic_write_bytes(safe_workspace_dir(name) / "fleet-control.json",
                                        json.dumps(control, ensure_ascii=False).encode())
            workspace_console_log(name, f"已要求 parallel run 完成 active child 本輪後停止｜pid={pid}")
            self._out(200, json.dumps({"ok": True, "name": name, "pid": pid,
                                       "requested": True, "graceful": True}, ensure_ascii=False))
            return
        if st and st.get("workspace_kind") == "standalone":
            if not require_workspace_generation(self, body, st):
                return
        with JOBS_LOCK:
            j = JOBS.get(name)
        if st is None and j is not None and j.alive() and expected_pid is None:
            self._err("尚在啟動中的 job 必須帶 expected_pid，請重新載入 jobs 後再停止", 409)
            return
        actual_pid = (j.popen.pid if j is not None and j.alive()
                      else (st.get("loop") or {}).get("pid") if st else None)
        if expected_pid is not None and int(actual_pid or 0) != expected_pid:
            self._err("job 已更新或同名重建，請重新載入後再停止", 409)
            return
        if j is not None and j.alive():
            workspace_console_log(name, f"停止 loop｜pid={j.popen.pid}")
            if not j.stop(wait=True):
                self._err(f"{name} 停止未完成：{j.last_stop_error or '程序或 runtime group 仍在執行中'}",
                          j.last_stop_code)
                return
            self._out(200, json.dumps({"ok": True, "name": name}, ensure_ascii=False))
            return
        # 不是本 dashboard 啟動的：先凍結 state session 與 OS process instance；numeric PID
        # 本身不可授權 signal，因為它可能已被另一個 workspace/restarted process 重用。
        pid = (st.get("loop") or {}).get("pid") if st else None
        if pid:
            try:
                frozen_runtime = freeze_workspace_stop_identity(
                    name, (st.get("config") or {}).get("repo"), int(pid), state=st,
                    require_coordinator_marker=True)
            except RuntimeStopIdentityError as identity_error:
                self._err(str(identity_error), identity_error.code)
                return

            def external_stopped(payload):
                ok, runtime_error, runtime_code = cleanup_frozen_runtime_group(frozen_runtime)
                if not ok:
                    self._err(f"{name} coordinator 已停止但 runtime 清場未完成：{runtime_error}",
                              runtime_code)
                    return
                self._out(200, json.dumps(payload, ensure_ascii=False))

            snapshot = _process_snapshot()
            if snapshot is None:
                self._err(f"{name} 無法取得 process snapshot，拒絕停止外部程序", 500)
                return
            identity, identity_status = _external_standalone_process_identity(name, st, snapshot)
            if identity_status == "stale":
                self._err(f"{name} 的 PID 已屬於另一個程序或 workspace，請重新載入後再操作", 409)
                return
            if identity_status == "gone":
                external_stopped({"ok": True, "name": name, "already_stopped": True})
                return
            if identity_status == "same":
                status = _revalidate_external_standalone_process(identity)
                if status == "snapshot-error":
                    self._err(f"{name} 無法重新確認 process 身分，拒絕停止", 500)
                    return
                if status == "stale":
                    self._err(f"{name} 的程序身分已更新，請重新載入後再操作", 409)
                    return
                if status == "gone":
                    external_stopped({"ok": True, "name": name, "already_stopped": True})
                    return
                workspace_console_log(name, f"停止外部 loop｜pid={identity['pid']}")
                try:
                    os.kill(identity["pid"], signal.SIGINT)
                except ProcessLookupError:
                    external_stopped({"ok": True, "name": name, "external": True})
                    return
                except (PermissionError, OSError) as error:
                    self._err(f"{name} 無法停止外部程序：{error}", 500)
                    return

                grace_deadline = time.monotonic() + 8
                while True:
                    status = _revalidate_external_standalone_process(identity, allow_cleared=True)
                    if status == "gone":
                        external_stopped({"ok": True, "name": name, "external": True})
                        return
                    if status == "stale":
                        self._err(f"{name} 的 PID 在停止期間已被重用，未對新程序發送 signal", 409)
                        return
                    if status == "snapshot-error":
                        self._err(f"{name} 停止期間無法重新確認 process 身分，未強制終止", 500)
                        return
                    if time.monotonic() >= grace_deadline:
                        break
                    time.sleep(min(0.1, max(0.0, grace_deadline - time.monotonic())))

                # Force is authorized only while both persisted session truth and the exact
                # start-time/command snapshot still identify the original coordinator.
                status = _revalidate_external_standalone_process(identity)
                if status == "gone":
                    external_stopped({"ok": True, "name": name, "external": True})
                    return
                if status != "same":
                    code = 500 if status == "snapshot-error" else 409
                    self._err(f"{name} force 前程序身分已更新，未發送 SIGKILL", code)
                    return
                try:
                    os.kill(identity["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except (PermissionError, OSError) as error:
                    self._err(f"{name} 無法強制停止外部程序：{error}", 500)
                    return
                force_deadline = time.monotonic() + 1
                while time.monotonic() < force_deadline:
                    status = _revalidate_external_standalone_process(identity, allow_cleared=True)
                    if status == "gone":
                        external_stopped({"ok": True, "name": name, "external": True})
                        return
                    if status in {"stale", "exiting"}:
                        self._err(f"{name} 的 PID 在 force 後已被重用，未再發送 signal", 409)
                        return
                    if status == "snapshot-error":
                        self._err(f"{name} force 後無法確認程序已停止", 500)
                        return
                    time.sleep(min(0.1, max(0.0, force_deadline - time.monotonic())))
                self._err(f"{name} 停止逾時，原程序仍在執行中", 500)
                return
        # UI 的 fleet 狀態每幾秒同步一次，程序可能恰好在點擊前自行結束。
        # stop 應為冪等操作，避免這個正常競態跳出錯誤並讓按鈕卡在舊狀態。
        self._out(200, json.dumps({"ok": True, "name": name, "already_stopped": True}, ensure_ascii=False))


def run_dashboard(*, name="", port=8765) -> int:
    """啟動 localhost Dashboard；供安裝後的 `loop dashboard` 與測試共用。"""
    # Acquire before config writes, coordinator discovery, signals, or bind.  A second
    # writable instance must be inert even when it selects a different HTTP port.
    with dashboard_instance_lease():
        load_config()  # 不存在就先建預設檔,讓人有得改
        if name:
            if not loop_mod.valid_workspace_name(name):
                sys.exit(f"❌ workspace 名稱不合法：{loop_mod.WORKSPACE_NAME_RULE}")
            names = ({d.name for d in ROOT.iterdir()
                      if loop_mod.valid_workspace_name(d.name) and not d.is_symlink() and d.is_dir()}
                     if ROOT.is_dir() else set())
            if name not in names:
                sys.exit(f"❌ workspace {name} 不存在,可用:{sorted(names) or '(無)'}")
        Handler.preselect = name
        # Dashboard sessions never adopt or auto-resume existing coordinators.  Start from
        # one deterministic stopped view and let the user explicitly choose what to run.
        stop_workspace_coordinators()

        def _sigterm(*_):
            """將服務管理器 SIGTERM 轉成既有 KeyboardInterrupt 關閉流程。"""
            raise KeyboardInterrupt  # 走與 Ctrl-C 相同的優雅關閉路徑(stop_all_jobs)
        signal.signal(signal.SIGTERM, _sigterm)

        srv = None
        for _ in range(20):
            try:
                srv = DashboardServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                port += 1
        if srv is None:
            sys.exit("❌ 找不到可用 port")
        mode = f"完整覆寫:{PERSONAL_CONFIG_PATH.name}" if CONFIG_OVERRIDE else (
            f"團隊:{PROJECT_CONFIG_PATH.name} + 個人:{PERSONAL_CONFIG_PATH.name}"
        )
        print(f"dashboard → http://127.0.0.1:{port}/  設定:{mode}", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
            stop_all_jobs()
            print("dashboard 已關閉。", flush=True)
        return 0


def main(argv=None) -> int:
    """保留 `python -m engine.dashboard` 開發入口；正式使用建議 `loop dashboard`。"""
    parser = argparse.ArgumentParser(description="loop-agent-lite dashboard(fleet + 直播 + launcher)")
    parser.add_argument("--name", default="", help="預選 workspace(可省;頁面內隨時可切)")
    parser.add_argument("--port", type=int, default=8765, help="被占用會自動往上找(最多 +20)")
    args = parser.parse_args(argv)
    return run_dashboard(name=args.name, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
