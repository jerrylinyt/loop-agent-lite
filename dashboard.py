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
import difflib
import fcntl
import functools
import json
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
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import loop as loop_mod          # 共用 Workspace/fresh_state,匯入計畫時建 state 不自己發明 schema
from prompt_templates import prompt_template_projection
from work import validate_plan  # 計畫校驗單一來源(create-plan / 匯入共用)

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("LOOP_AGENT_WORKSPACE_ROOT", HERE / "workspace")).expanduser().resolve()
CONFIG_OVERRIDE = os.environ.get("LOOP_AGENT_DASHBOARD_CONFIG")
PROJECT_CONFIG_PATH = Path(os.environ.get(
    "LOOP_AGENT_DASHBOARD_PROJECT_CONFIG", HERE / "dashboard.config.shared.json"
)).expanduser().resolve()
PERSONAL_CONFIG_PATH = Path(CONFIG_OVERRIDE or HERE / "dashboard.config.local.json").expanduser().resolve()
LEGACY_CONFIG_PATH = (HERE / "dashboard.config.json").resolve()
CONFIG_PATH = PERSONAL_CONFIG_PATH  # 舊程式/錯誤訊息相容名稱；UI 會分別顯示團隊版與個人版
MAX_CHUNK = 512 * 1024  # 單次 tail 最多回傳量
MAX_REQUEST_BYTES = 8 * 1024 * 1024  # POST JSON 上限，避免 goal/plan 或惡意 body 吃光 dashboard 記憶體
HEALTH_SCHEMA_VERSION = 1
ARCHIVE_DIR_NAME = ".archive"
ARCHIVE_OPS_LOCK_NAME = ".ops.lock"
ARCHIVE_ID_RE = re.compile(
    r"(?P<name>[A-Za-z0-9._-]+)--(?P<stamp>\d{8}T\d{6}Z)--(?P<nonce>[0-9a-f]{32})"
)
LEGACY_ARCHIVE_ID_RE = re.compile(r"(?P<name>[A-Za-z0-9._-]+)-(?P<stamp>\d{8}-\d{6})")

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
    },
}

PERSONAL_CONFIG_KEYS = {"agent_cmds", "extra_path_dirs", "repo_roots", "notify_cmd"}


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
    return env


def command_not_found(label, executable, cfg):
    raw, resolved = configured_path_dirs(cfg)
    shown = ", ".join(raw) or "（未設定）"
    resolved_shown = os.pathsep.join(resolved) or "（無）"
    return (f"找不到 {label}：{executable}。請先在終端執行 `command -v {Path(executable).name}`，"
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


class ArchiveOperationError(RuntimeError):
    """封存操作的可預期 fail-closed 錯誤；status 直接對應 REST 回應。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _lstat(path: Path, label: str):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ArchiveOperationError(f"無法檢查{label}:{e}") from e


def _require_real_directory(path: Path, label: str, *, missing_status=404) -> Path:
    """只接受真的 directory；exists/is_dir 會跟隨 symlink，不足以當邊界檢查。"""
    info = _lstat(path, label)
    if info is None:
        raise ArchiveOperationError(f"{label}不存在", missing_status)
    if stat.S_ISLNK(info.st_mode):
        raise ArchiveOperationError(f"{label}不可為 symbolic link", 409)
    if not stat.S_ISDIR(info.st_mode):
        raise ArchiveOperationError(f"{label}必須是目錄", 409)
    return path


def archive_root(*, create=False):
    """取得真正的 .archive 目錄；不跟隨 symlink，建立時也會重新 lstat 確認。"""
    path = ROOT / ARCHIVE_DIR_NAME
    info = _lstat(path, "封存根目錄")
    if info is None:
        if not create:
            return None
        try:
            path.mkdir()
        except FileExistsError:
            pass
        except OSError as e:
            raise ArchiveOperationError(f"無法建立封存根目錄:{e}") from e
    return _require_real_directory(path, "封存根目錄")


def archive_metadata(archive_id):
    """驗證 archive id 並從新、舊檔名格式取回原 workspace 名稱與時間。"""
    if not isinstance(archive_id, str) or Path(archive_id).name != archive_id:
        return None
    matched = ARCHIVE_ID_RE.fullmatch(archive_id)
    legacy = False
    if matched is None:
        matched = LEGACY_ARCHIVE_ID_RE.fullmatch(archive_id)
        legacy = True
    if matched is None:
        return None
    name, stamp = matched.group("name"), matched.group("stamp")
    if not loop_mod.valid_workspace_name(name):
        return None
    try:
        parsed = datetime.strptime(stamp, "%Y%m%d-%H%M%S" if legacy else "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    if legacy:
        archived_at = parsed.strftime("%Y-%m-%d %H:%M:%S（舊格式，時區未知）")
    else:
        archived_at = parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return {"id": archive_id, "name": name, "archived_at": archived_at, "legacy": legacy}


def new_archive_id(name: str) -> str:
    loop_mod.require_workspace_name(name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{name}--{stamp}--{uuid.uuid4().hex}"


def _lstat_at(directory_fd, name: str, label: str):
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ArchiveOperationError(f"無法檢查{label}:{e}", 409) from e


@contextmanager
def directory_fd(path, label: str, *, dir_fd=None):
    """以 O_DIRECTORY|O_NOFOLLOW 開實體目錄；後續 lock/rename 都相對此 descriptor 執行。"""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ArchiveOperationError("此系統不支援安全的 O_NOFOLLOW 目錄操作，已拒絕封存操作")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_DIRECTORY", 0)
    fd = None
    try:
        try:
            fd = os.open(path, flags, dir_fd=dir_fd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise ArchiveOperationError(f"{label}不可為 symbolic link", 409) from e
            if e.errno == errno.ENOENT:
                raise ArchiveOperationError(f"{label}不存在", 404) from e
            raise ArchiveOperationError(f"無法開啟{label}:{e}", 409) from e
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ArchiveOperationError(f"{label}必須是目錄", 409)
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def exclusive_file_lock(path, label: str, *, dir_fd=None):
    """以 descriptor-relative O_NOFOLLOW regular file + flock 取得跨 dashboard 鎖。"""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ArchiveOperationError("此系統不支援安全的 O_NOFOLLOW 鎖定，已拒絕封存操作")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | nofollow | os.O_NONBLOCK, 0o600, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ArchiveOperationError(f"{label}不可為 symbolic link", 409) from e
        raise ArchiveOperationError(f"無法取得{label}:{e}", 409) from e
    lock_file = os.fdopen(fd, "a+b")
    try:
        info = os.fstat(lock_file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ArchiveOperationError(f"{label}必須是單一 regular file", 409)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise ArchiveOperationError(f"{label}仍被持有，請稍後再試", 409) from e
        yield lock_file
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


_ARCHIVE_THREAD_LOCK = threading.Lock()


@contextmanager
def archive_operation_lock(*, create=True):
    """序列化 archive/restore 的檢查→rename，並回傳 ROOT/.archive 的安全 dir fd。"""
    if not _ARCHIVE_THREAD_LOCK.acquire(blocking=False):
        raise ArchiveOperationError("另一個封存操作正在進行，請稍後再試", 409)
    try:
        root = archive_root(create=create)
        if root is None:
            raise ArchiveOperationError("封存根目錄不存在", 404)
        with directory_fd(ROOT, "workspace 根目錄") as root_fd:
            with directory_fd(ARCHIVE_DIR_NAME, "封存根目錄", dir_fd=root_fd) as archive_fd:
                with exclusive_file_lock(ARCHIVE_OPS_LOCK_NAME, "封存操作鎖", dir_fd=archive_fd):
                    yield root, root_fd, archive_fd
    finally:
        _ARCHIVE_THREAD_LOCK.release()


def _require_same_directory_entry(parent_fd, name: str, opened_fd, label: str):
    """確認 parent/name 仍指向剛開啟且持鎖的同一個 directory inode。"""
    entry = _lstat_at(parent_fd, name, label)
    opened = os.fstat(opened_fd)
    if (entry is None or stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode)
            or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)):
        raise ArchiveOperationError(f"{label}在操作期間變更，已拒絕搬移", 409)


def _require_absent_entry(parent_fd, name: str, label: str):
    if _lstat_at(parent_fd, name, label) is not None:
        raise ArchiveOperationError(f"{label}已存在，已拒絕覆寫", 409)


def _remove_tree_at(parent_fd, name: str, label: str):
    """以 descriptor-relative、不跟隨 symlink 的方式移除目錄樹。"""
    with directory_fd(name, label, dir_fd=parent_fd) as child_fd:
        try:
            entries = list(os.scandir(child_fd))
        except OSError as e:
            raise ArchiveOperationError(f"無法讀取{label}:{e}", 409) from e
        for entry in entries:
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as e:
                raise ArchiveOperationError(f"無法檢查{label}內容:{e}", 409) from e
            if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                _remove_tree_at(child_fd, entry.name, f"{label}/{entry.name}")
            else:
                try:
                    os.unlink(entry.name, dir_fd=child_fd)
                except OSError as e:
                    raise ArchiveOperationError(f"無法移除{label}/{entry.name}:{e}", 409) from e
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError as e:
        raise ArchiveOperationError(f"無法移除{label}:{e}", 409) from e


def archived_state_projection(directory: Path):
    """archive 列表只讀可安全解析的 state；壞檔或 symlink 只省略 metadata。"""
    state_path = directory / "state.json"
    checkpoint_path = loop_mod.state_checkpoint_path(state_path)
    for path in (state_path, checkpoint_path):
        info = _lstat(path, "封存 state")
        if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)):
            return {}
    try:
        state, _data, _recovered = loop_mod.load_checkpointed_state(state_path, repair=False)
    except (FileNotFoundError, OSError, loop_mod.StateLoadError):
        return {}
    return {"phase": state.get("phase"), "round": state.get("round")}


def list_archives():
    """列出可嚴格辨識、非 symlink 的封存項目；不修 state、不暴露任意路徑。"""
    try:
        root = archive_root(create=False)
    except ArchiveOperationError as e:
        return {"archives": [], "error": str(e)}
    if root is None:
        return {"archives": []}
    archives = []
    try:
        entries = list(root.iterdir())
    except OSError as e:
        return {"archives": [], "error": f"無法讀取封存根目錄:{e}"}
    for entry in entries:
        metadata = archive_metadata(entry.name)
        if metadata is None:
            continue
        try:
            info = _lstat(entry, f"封存項目 {entry.name}")
        except ArchiveOperationError:
            continue
        if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            continue
        metadata.update(archived_state_projection(entry))
        archives.append(metadata)
    archives.sort(key=lambda item: item["id"], reverse=True)
    return {"archives": archives}

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
        @functools.wraps(func)
        def wrapper(self, body):
            name = str(body.get("name") or "").strip()
            if not name and repo_fallback:
                name = Path(str(body.get("repo") or "")).expanduser().name
            with _state_lock(name):
                return func(self, body)
        return wrapper
    return decorate(fn) if fn is not None else decorate


class DashboardServer(ThreadingHTTPServer):
    """SSE 連線是長存 thread；設為 daemon 才不會阻擋 dashboard 優雅關閉。"""
    daemon_threads = True
    allow_reuse_address = True


class Job:
    def __init__(self, name, repo, popen):
        self.name = name
        self.repo = repo
        self.popen = popen
        self.out = deque(maxlen=200)
        t = threading.Thread(target=self._reader, daemon=True)
        self.reader = t
        t.start()

    def _reader(self):
        try:
            for line in self.popen.stdout:
                self.out.append(line.rstrip("\n"))
        finally:
            self.popen.stdout.close()

    def alive(self):
        return self.popen.poll() is None

    def info(self):
        return {"name": self.name, "repo": self.repo, "pid": self.popen.pid,
                "alive": self.alive(), "rc": self.popen.returncode,
                "tail": "\n".join(list(self.out)[-8:])}

    def stop(self, wait=False):
        """SIGINT 優雅收尾；8 秒沒死 SIGKILL。wait=True 時等到狀態真的可重啟。"""
        if not self.alive():
            return True
        try:
            self.popen.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return True

        def _force():
            if self.alive():
                try:
                    os.killpg(os.getpgid(self.popen.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        t = threading.Timer(8, _force)
        t.daemon = True
        t.start()
        if not wait:
            return True
        try:
            self.popen.wait(timeout=9)
        except subprocess.TimeoutExpired:
            return not self.alive()
        return True


def loop_pid_alive(pid):
    """state.json 記的 pid 是否還是活著的 loop.py(pid 重用靠 ps 命令內容兜底)。"""
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError, PermissionError):
        return False
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return "loop.py" in r.stdout


def norm_cmd(s):
    """shlex 正規化,供命令白名單比對。"""
    try:
        return shlex.join(shlex.split(str(s)))
    except ValueError:
        return None


def spawn_loop(name, repo, agent_cmd, validate_cmd, ft, dt, rt, validate_timeout=120,
               reset=False, import_plan=None, start_phase="plan", notify_cmd="",
               red_limit=20, stall_limit=300, stuck_stop=False, stuck_count=100,
               agent_backoff_max=60, env=None):
    """spawn loop.py 並登記進 JOBS(呼叫方需持 JOBS_LOCK)。"""
    loop_mod.require_workspace_name(name)
    workspace_dir = safe_workspace_dir(name)
    cmd = [sys.executable, str(HERE / "loop.py"), "--repo", str(repo), "--name", name,
           "--agent-cmd", agent_cmd, "--validate-cmd", validate_cmd,
           "--flag-threshold", str(ft), "--done-threshold", str(dt), "--round-timeout", str(rt),
           "--agent-backoff-max", str(agent_backoff_max),
           "--validate-timeout", str(validate_timeout),
           "--red-limit", str(red_limit), "--stall-limit", str(stall_limit)]
    if stuck_stop:
        cmd += ["--stuck-stop", "--stuck-stop-count", str(stuck_count)]
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


def write_state(name, st):
    """原子寫 workspace 主 state 與 last-good checkpoint。"""
    loop_mod.require_workspace_name(name)
    data = json.dumps(st, ensure_ascii=False, indent=2).encode("utf-8")
    loop_mod.write_checkpointed_state(safe_workspace_dir(name) / "state.json", data)


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
ANOMALY_ID_RE = re.compile(r"\d{8}T\d{12}-r\d{6}-[0-9a-f]{8}")


def aggregate_fleet_round_metrics(samples, *, history_truncated=False):
    """依時間合併各 workspace，精確聚合全體最新 500 筆。"""
    samples = sorted(samples, key=lambda sample: (
        sample["timestamp"], sample["workspace"], sample["round"]))[-FLEET_AGGREGATE_LIMIT:]
    durations = sorted(sample["seconds"] for sample in samples)

    def percentile(percent):
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
        st, _ = read_state(name)
    return bool(st) and loop_pid_alive((st.get("loop") or {}).get("pid"))


def stop_all_jobs():
    with JOBS_LOCK:
        jobs = [j for j in JOBS.values() if j.alive()]
    if not jobs:
        return
    print(f"⏹ 關閉 dashboard:停止 {len(jobs)} 個 loop …", flush=True)
    for j in jobs:
        j.stop()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(j.alive() for j in jobs):
        time.sleep(0.2)


def load_config():
    """讀取團隊版 + 個人版；個人版只允許覆蓋 PERSONAL_CONFIG_KEYS。"""
    def read_json(path, label):
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
    raw_paths, resolved_paths = configured_path_dirs(cfg)
    prompt_templates, prompt_template_warnings = prompt_template_projection(cfg)
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


UI_DIST = HERE / "ui" / "dist"

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
        else:
            c = st.get("config") or {}
            loop_state = st.get("loop") or {}
            running = ws_running(d.name, st)
            loop_pid = loop_state.get("pid")
            drain_claimed = running and loop_mod.stop_after_round_claimed(
                d, loop_state.get("pid"), loop_state.get("session_id"))
            current_order = st.get("current_order")
            current_task = next((t.get("task") or "" for t in (st.get("plan") or [])
                                 if isinstance(t, dict) and t.get("order") == current_order), "")
            if len(current_task) > 120:
                current_task = current_task[:120] + "…"
            info.update(phase=st.get("phase"), round=st.get("round", 0), flag=st.get("flag", 0),
                        completed=len(st.get("completed") or []), plan_len=len(st.get("plan") or []),
                        done_count=st.get("done_count", 0), repo=c.get("repo"),
                        red_streak=st.get("red_streak", 0), stall_rounds=st.get("stall_rounds", 0),
                        issues=len(st.get("issues") or []),
                        unread_issues=loop_mod.unread_issue_count(st),
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
    completed = info.get("phase") == "done"
    return bool(
        unread_issues > 0 or
        info.get("state_recovery_pending") or
        info.get("goal_changed") or
        info.get("stale_loop_pid") or
        (not completed and (
            (info.get("red_streak") or 0) > 0 or
            (info.get("stall_rounds") or 0) > 0 or
            (info.get("agent_failure_streak") or 0) > 0 or
            info.get("last_round_timed_out") or
            (info.get("state_recovery_count") or 0) > 0
        ))
    )


def fleet_health_projection(workspaces=None):
    """回傳唯讀 fleet health projection，供探針、SSE 與 UI 共用。"""
    items = list_workspaces() if workspaces is None else list(workspaces)
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    preselect = ""
    readonly = False

    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _out(self, code, body, ctype="application/json; charset=utf-8"):
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
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        fleet_sig = state_sig = fleet_history_sig = None
        fleet_at = fleet_history_at = keepalive_at = 0.0
        console_offset = -1
        console_identity = None

        def file_identity(path):
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
                    state, err = read_state(workspace, repair=False)
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
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                self._serve_ui("index.html")
            elif u.path.startswith("/assets/"):
                self._serve_ui(u.path.lstrip("/"))
            elif u.path == "/api/bootstrap":
                self._out(200, json.dumps({"preselect": self.preselect,
                                           "readonly": self.readonly}, ensure_ascii=False))
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
            elif u.path == "/api/archives":
                self._out(200, json.dumps(list_archives(), ensure_ascii=False))
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
                if not (repo / ".git").exists():
                    self._out(200, json.dumps({"error": f"{repo} 不是 git repo"}, ensure_ascii=False))
                    return

                def fstat(rel):
                    in_head = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"HEAD:{rel}"],
                                             capture_output=True).returncode == 0
                    dirty = bool(subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", rel],
                                                capture_output=True, text=True).stdout.strip())
                    if in_head and not dirty:
                        return "committed"
                    if in_head:
                        return "modified"
                    return "untracked" if (repo / rel).exists() else "missing"

                clean = not subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                           capture_output=True, text=True).stdout.strip()
                if (repo / "pom.xml").is_file():
                    suggested_validate = "mvn -q compile"
                elif (repo / "package.json").is_file():
                    suggested_validate = "sh -c 'npm run build && npm test -- --run && npx playwright test'"
                elif (repo / "tests").is_dir():
                    suggested_validate = "python3 -m unittest discover -s tests -t . -q"
                else:
                    suggested_validate = None
                self._out(200, json.dumps({"goal": fstat("goal.md"),
                                           "tree_clean": clean,
                                           "suggested_validate_cmd": suggested_validate}, ensure_ascii=False))
            elif u.path == "/api/state":
                d = self._ws_dir(q)
                if d is None:
                    return
                st, err = read_state(d.name, repair=False)
                self._out(200, json.dumps({"error": err} if err else st, ensure_ascii=False))
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
                    projection = read_anomaly_records(directory, run=run)
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
        if self.readonly:
            self._err("唯讀模式:此實例不接受任何操作", 403)
            return
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0:
                raise ValueError("Content-Length 不可為負數")
            if length > MAX_REQUEST_BYTES:
                self.close_connection = True
                self._err(f"request body 太大（上限 {MAX_REQUEST_BYTES // (1024 * 1024)} MiB）", 413)
                return
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError):
            self._err("body 必須是 JSON")
            return
        try:
            if u.path == "/api/launch":
                self.api_launch(body)
            elif u.path == "/api/drain":
                self.api_drain(body)
            elif u.path == "/api/cancel-drain":
                self.api_cancel_drain(body)
            elif u.path == "/api/stop":
                self.api_stop(body)
            elif u.path == "/api/run":
                self.api_run(body)
            elif u.path == "/api/edit-state":
                self.api_edit_state(body)
            elif u.path == "/api/edit-config":
                self.api_edit_config(body)
            elif u.path == "/api/validate":
                self.api_validate(body)
            elif u.path == "/api/preflight":
                self.api_preflight(body)
            elif u.path == "/api/test-agent":
                self.api_test_agent(body)
            elif u.path == "/api/test-cli":
                self.api_test_cli(body)
            elif u.path == "/api/edit-cli-config":
                self.api_edit_cli_config(body)
            elif u.path == "/api/edit-repo-roots":
                self.api_edit_repo_roots(body)
            elif u.path == "/api/edit-notify":
                self.api_edit_notify(body)
            elif u.path == "/api/test-notify":
                self.api_test_notify(body)
            elif u.path == "/api/phase":
                self.api_phase(body)
            elif u.path == "/api/set-task":
                self.api_set_task(body)
            elif u.path == "/api/archive-workspace":
                self.api_archive_workspace(body)
            elif u.path == "/api/restore-workspace":
                self.api_restore_workspace(body)
            elif u.path == "/api/delete-archive":
                self.api_delete_archive(body)
            else:
                self._err("not found", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    @with_state_lock(repo_fallback=True)
    def api_launch(self, body):
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
            ft = int(body.get("flag_threshold") or d.get("flag_threshold", 10))
            dt = int(body.get("done_threshold") or d.get("done_threshold", 3))
            rt = float(body.get("round_timeout") if body.get("round_timeout") is not None
                       else d.get("round_timeout", 30))
            ab = float(body.get("agent_backoff_max") if body.get("agent_backoff_max") is not None
                       else d.get("agent_backoff_max", 60))
            vt = float(body.get("validate_timeout") if body.get("validate_timeout") is not None
                       else d.get("validate_timeout", 120))
            rl = int(body.get("red_limit") or d.get("red_limit", 20))
            sl = int(body.get("stall_limit") or d.get("stall_limit", 300))
            if ft < 1 or dt < 1 or rt < 0 or ab < 0 or vt <= 0 or rl < 1 or sl < 1:
                raise ValueError
        except (TypeError, ValueError):
            self._err("flag/done/red/stall 必須 ≥1，round_timeout/agent_backoff_max 必須 ≥0，"
                      "validate_timeout 必須 >0 秒")
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
        # 衝突檢查 + git mutation + spawn 全包進同一個 lock,且順序=先檢查再 mutate(#2):
        # 對正在跑的 repo 再按啟動時,必須在切 branch/改 goal 前就擋下,否則現有 loop 會被動到。
        with JOBS_LOCK:
            st, _ = read_state(name)
            if st and loop_pid_alive((st.get("loop") or {}).get("pid")):
                self._err(f"workspace {name} 已有 loop 在跑(外部啟動的也算),先停掉再啟動")
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
                    workspace_console_log(name, "已匯入並 commit goal.md")
            if normalized is not None:
                lws = loop_mod.Workspace(name)
                import_plan_path = lws.dir / "import-plan.pending.json"
                loop_mod.atomic_write_bytes(
                    import_plan_path,
                    json.dumps(normalized, ensure_ascii=False, indent=2).encode("utf-8"),
                )
                workspace_console_log(
                    name,
                    f"準備匯入 plan.json｜共 {len(normalized)} 條｜Validate 通過後才取代舊 state",
                )
            else:
                import_plan_path = None
            p = spawn_loop(name, repo, agent_cmd, validate_cmd, ft, dt, rt,
                           validate_timeout=vt,
                           reset=bool(body.get("reset_state")) and normalized is None,
                           import_plan=import_plan_path, start_phase=start_phase,
                           notify_cmd=str(cfg.get("notify_cmd") or ""),
                           red_limit=rl, stall_limit=sl,
                           agent_backoff_max=ab,
                           stuck_stop=bool(d.get("stuck_stop")),
                           stuck_count=int(d.get("stuck_stop_count", 100)),
                           env=command_env(cfg))
        workspace_console_log(name, f"啟動 loop｜pid={p.pid}｜repo={repo}")
        self._out(200, json.dumps({"ok": True, "starting": True, "name": name, "pid": p.pid,
                                   "startup_timeout": vt + 15}, ensure_ascii=False))

    @with_state_lock
    def api_run(self, body):
        """一鍵重跑既有 workspace:設定全部從 state.json 拿,agent 命令先過 config 白名單。"""
        name = str(body.get("name") or "")
        st, err = read_state(name)
        if err:
            self._err(err)
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
        with JOBS_LOCK:
            j = JOBS.get(name)
            if j is not None and j.alive():
                self._err(f"{name} 已在執行中(pid {j.popen.pid})")
                return
            for jj in JOBS.values():
                if jj.alive() and Path(jj.repo) == Path(repo):
                    self._err(f"repo {repo} 已有 loop 在跑({jj.name})")
                    return
            d = cfg.get("defaults") or {}
            p = spawn_loop(name, repo, agent_cmd, validate_cmd,
                           c.get("flag_threshold", 10), c.get("done_threshold", 3),
                           c.get("round_timeout", 30),
                           validate_timeout=c.get("validate_timeout", d.get("validate_timeout", 120)),
                           notify_cmd=str(cfg.get("notify_cmd") or ""),
                           red_limit=c.get("red_limit", d.get("red_limit", 20)),
                           stall_limit=c.get("stall_limit", d.get("stall_limit", 300)),
                           agent_backoff_max=c.get("agent_backoff_max", d.get("agent_backoff_max", 60)),
                           stuck_stop=bool(d.get("stuck_stop")),
                           stuck_count=int(d.get("stuck_stop_count", 100)),
                           env=command_env(cfg))
        workspace_console_log(name, f"繼續運行 loop｜pid={p.pid}")
        startup_timeout = float(c.get("validate_timeout", d.get("validate_timeout", 120))) + 15
        self._out(200, json.dumps({"ok": True, "starting": True, "name": name, "pid": p.pid,
                                   "startup_timeout": startup_timeout}, ensure_ascii=False))

    @with_state_lock
    def api_edit_state(self, body):
        """停止狀態下的人工編輯:plan、done 計數與 issue 已讀/清除；執行中全部鎖死。"""
        name = str(body.get("name") or "")
        st, err = read_state(name)
        if err:
            self._err(err)
            return
        if ws_running(name, st):
            self._err(f"{name} 執行中,全部鎖死——先停止才能編輯")
            return
        changed = []
        tasks = body.get("tasks")
        if tasks is not None:
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
        write_state(name, st)
        workspace_console_log(name, f"人工編輯計畫｜{', '.join(changed) or '無變更'}")
        self._out(200, json.dumps({"ok": True, "changed": changed}, ensure_ascii=False))

    @with_state_lock
    def api_edit_config(self, body):
        """停止狀態下編輯 workspace 設定(agent/validate/五顆旋鈕),存回 state.config,▶ 運行時生效。執行中鎖死。"""
        name = str(body.get("name") or "")
        st, err = read_state(name)
        if err:
            self._err(err)
            return
        if ws_running(name, st):
            self._err(f"{name} 執行中,全部鎖死——先停止才能改設定")
            return
        cfg = load_config()
        if "error" in cfg:
            self._err(cfg["error"])
            return
        c = dict(st.get("config") or {})
        changed = []
        # 數字旋鈕(round_timeout/agent_backoff_max≥0,其餘≥1)
        for k, lo in (("flag_threshold", 1), ("done_threshold", 1), ("round_timeout", 0),
                      ("agent_backoff_max", 0),
                      ("validate_timeout", 1),
                      ("red_limit", 1), ("stall_limit", 1)):
            if body.get(k) is None:
                continue
            try:
                v = float(body[k]) if k in ("round_timeout", "agent_backoff_max", "validate_timeout") else int(body[k])
                if v < lo:
                    raise ValueError
            except (TypeError, ValueError):
                self._err(f"{k} 不合法(round_timeout/agent_backoff_max 需 ≥0；"
                          "validate_timeout 需 ≥1 秒；其餘需 ≥1)")
                return
            if c.get(k) != v:
                c[k] = v
                changed.append(f"{k}={v}")
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
        st["config"] = c
        write_state(name, st)
        workspace_console_log(name, f"更新 Workspace 設定｜{', '.join(changed) or '無變更'}")
        self._out(200, json.dumps({"ok": True, "changed": changed}, ensure_ascii=False))

    @with_state_lock(repo_fallback=True)
    def api_validate(self, body):
        """試跑 Validate 欄位內容；可來自既有 workspace 或尚未 launch 的 repo。"""
        name = str(body.get("name") or "")
        st = None
        if name:
            st, err = read_state(name)
            if err:
                self._err(err)
                return
            if ws_running(name, st):
                self._err(f"{name} 執行中——先停止才能單獨確認 Validate 命令")
                return
            repo = Path(str((st.get("config") or {}).get("repo") or "")).expanduser()
        else:
            repo = Path(str(body.get("repo") or "")).expanduser()
        if not (repo / ".git").exists():
            self._err(f"{repo} 不是 git repo(repo 被移走了?)")
            return
        raw = str(body.get("validate_cmd") or "").strip()
        if not raw:
            self._err("Validate 命令不可為空")
            return
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
            rc, output, timed_out = run_command_check(cmd, repo, timeout=timeout, env=command_env(cfg))
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
        command = [sys.executable, str(HERE / "loop.py"), "--repo", str(repo), "--name", name,
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
        if name:
            st, err = read_state(name)
            if err:
                self._err(err)
                return
            if ws_running(name, st):
                self._err(f"{name} 執行中——先停止才能單獨確認 Agent CLI")
                return
            repo = Path(str((st.get("config") or {}).get("repo") or "")).expanduser()
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
            current = norm_cmd((st.get("config") or {}).get("agent_cmd", ""))
            match = next((agent.get("cmd") for agent in agents if norm_cmd(agent.get("cmd", "")) == current), None)
            if not match:
                self._err("目前的 Agent CLI 不在個人/團隊合併後的 CLI 清單內")
                return
            raw = match
        try:
            cmd = shlex.split(raw)
            command_problem = command_error(raw, "Agent CLI", cfg)
            if command_problem:
                self._err(command_problem)
                return
            rc, output, timed_out = run_command_check(
                cmd, repo, prompt="test\n", timeout=60, env=command_env(cfg)
            )
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
            st, err = read_state(name)
            if err:
                self._err(err)
                return
            if ws_running(name, st):
                self._err(f"{name} 執行中——先停止才能測試 Agent CLI")
                return
            repo = Path(str((st.get("config") or {}).get("repo") or "")).expanduser()
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
            rc, output, timed_out = run_command_check(
                cmd, repo, prompt="test\n", timeout=60, env=command_env(cfg)
            )
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
        st, err = read_state(name)
        if err:
            self._err(err)
            return
        if ws_running(name, st):
            self._err(f"{name} 執行中,全部鎖死——先停止才能切換 phase")
            return
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
        write_state(name, st)
        workspace_console_log(name, f"切換階段｜{'規劃期' if target == 'plan' else '執行期'}")
        self._out(200, json.dumps({"ok": True, "phase": target}, ensure_ascii=False))

    @with_state_lock
    def api_set_task(self, body):
        """停止狀態下的進度管理:退回重做,或往前跳(validate 綠才放行,被跳過的標人工完成)。"""
        name = str(body.get("name") or "")
        st, err = read_state(name)
        if err:
            self._err(err)
            return
        if ws_running(name, st):
            self._err(f"{name} 執行中,全部鎖死——先停止才能調整進度")
            return
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
        completed = st.get("completed") or []
        done_orders = {e["order"] for e in completed}
        skipped = [o for o in plan_orders if o < order and o not in done_orders]
        if skipped:  # 往前跳:同 preflight 原則,validate 綠才放行
            c = st.get("config") or {}
            repo, vcmd = c.get("repo"), c.get("validate_cmd")
            if not (repo and vcmd):
                self._err("state 缺 repo/validate 設定,無法驗證——先用啟動表單跑過一次")
                return
            r = subprocess.run(shlex.split(vcmd), cwd=repo, capture_output=True, text=True)
            if r.returncode != 0:
                tail = "\n".join(((r.stdout or "") + "\n" + (r.stderr or "")).strip().splitlines()[-15:])
                self._err(f"validate 未過,不能往後跳(同 preflight 原則):\n{tail}")
                return
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            for o in skipped:
                completed.append({"order": o, "sha": head, "round": 0, "human": True})
        # 目標(含)之後的完成紀錄清除 → 從 order 重新執行
        completed = sorted([e for e in completed if e["order"] < order], key=lambda e: e["order"])
        st["completed"] = completed
        st.update(phase="exec", current_order=order, done_count=0, red_streak=0, stall_rounds=0)
        write_state(name, st)
        workspace_console_log(
            name,
            f"調整任務進度｜前往 task-{order}"
            + (f"｜人工標記完成：{', '.join(f'task-{value}' for value in skipped)}" if skipped else ""),
        )
        self._out(200, json.dumps({"ok": True, "current_order": order,
                                   "human_marked": skipped}, ensure_ascii=False))

    @with_state_lock
    def api_archive_workspace(self, body):
        """停止狀態下以原子 rename 軟刪除 workspace；不動 target repo 或覆寫既有 archive。"""
        name = str(body.get("name") or "")
        st, err = read_state(name, repair=False)
        if err:
            self._err(err)
            return
        if ws_running(name, st):
            self._err(f"{name} 執行中,不能封存——先停止")
            return
        try:
            safe_workspace_dir(name)
        except ValueError as e:
            self._err(str(e))
            return
        try:
            with loop_mod.workspace_operation_lock(ROOT, name, blocking=False):
                with archive_operation_lock(create=True) as (root, root_fd, archive_fd):
                    # 從 ROOT descriptor 開 source，避免檢查後 workspace path 被換成 symlink。
                    with directory_fd(name, f"workspace {name}", dir_fd=root_fd) as workspace_fd:
                        current, current_error = read_state(name, repair=False)
                        if current_error or ws_running(name, current):
                            raise ArchiveOperationError(f"{name} 在封存時已開始執行，不能封存", 409)
                        # pid 偵測可能失準；.run.lock 是 loop 單 writer 的機械真相。鎖檔本身也必須非 symlink。
                        with exclusive_file_lock(".run.lock", f"{name} 的單 writer 鎖", dir_fd=workspace_fd):
                            _require_same_directory_entry(root_fd, name, workspace_fd, f"workspace {name}")
                            archive_id = new_archive_id(name)
                            _require_absent_entry(archive_fd, archive_id, "封存目標")
                            target = root / archive_id
                            try:
                                os.rename(name, archive_id, src_dir_fd=root_fd, dst_dir_fd=archive_fd)
                            except OSError as e:
                                raise ArchiveOperationError(f"封存失敗:{e}", 409) from e
        except ArchiveOperationError as e:
            self._err(str(e), e.status)
            return
        except loop_mod.WorkspaceOperationLockError as e:
            self._err(str(e), 409)
            return
        except ValueError as e:
            self._err(str(e))
            return
        with JOBS_LOCK:
            JOBS.pop(name, None)  # 已結束 job 的殘影一併移除,避免 stale tail/名稱衝突
        print(f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜封存 workspace {name} → {target}", flush=True)
        self._out(200, json.dumps({"ok": True, "name": name, "archive_id": archive_id,
                                   "archived_to": str(target)}, ensure_ascii=False))

    def api_restore_workspace(self, body):
        """從嚴格 archive id 還原原目錄；只 rename，絕不覆寫或自動啟動 loop。"""
        archive_id = body.get("archive_id")
        metadata = archive_metadata(archive_id)
        if metadata is None:
            self._err("archive_id 不合法")
            return
        name = metadata["name"]
        # restore 的 body 是 archive id，不會自然與 launch/run 共享 decorator key；手動拿原名稱鎖。
        with _state_lock(name):
            try:
                with JOBS_LOCK:
                    job = JOBS.get(name)
                    if job is not None and job.alive():
                        raise ArchiveOperationError(f"workspace {name} 正在執行，不能還原", 409)
                with loop_mod.workspace_operation_lock(ROOT, name, blocking=False):
                    # 無 archive 時 restore 只回 404，不建立空 .archive 目錄。
                    with archive_operation_lock(create=False) as (_root, root_fd, archive_fd):
                        _require_absent_entry(root_fd, name, f"workspace {name}")
                        # 從 archive descriptor 開 source，避免 archive entry / parent 被替換後跟隨 symlink。
                        with directory_fd(archive_id, f"封存項目 {archive_id}", dir_fd=archive_fd) as source_fd:
                            with exclusive_file_lock(".run.lock", f"{name} 的單 writer 鎖", dir_fd=source_fd):
                                _require_same_directory_entry(archive_fd, archive_id, source_fd,
                                                              f"封存項目 {archive_id}")
                                _require_absent_entry(root_fd, name, f"workspace {name}")
                                try:
                                    os.rename(archive_id, name, src_dir_fd=archive_fd, dst_dir_fd=root_fd)
                                except OSError as e:
                                    raise ArchiveOperationError(f"還原失敗:{e}", 409) from e
            except ArchiveOperationError as e:
                self._err(str(e), e.status)
                return
            except loop_mod.WorkspaceOperationLockError as e:
                self._err(str(e), 409)
                return
            except ValueError as e:
                self._err(str(e))
                return
        with JOBS_LOCK:
            JOBS.pop(name, None)
        print(f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜還原 workspace {name} ← {archive_id}", flush=True)
        self._out(200, json.dumps({"ok": True, "name": name, "archive_id": archive_id}, ensure_ascii=False))

    def api_delete_archive(self, body):
        """永久刪除停止中的封存；先原子搬到隱藏名稱，再以安全 fd 遞迴移除。"""
        archive_id = body.get("archive_id")
        metadata = archive_metadata(archive_id)
        if metadata is None:
            self._err("archive_id 不合法")
            return
        name = metadata["name"]
        with _state_lock(name):
            try:
                with JOBS_LOCK:
                    job = JOBS.get(name)
                    if job is not None and job.alive():
                        raise ArchiveOperationError(f"workspace {name} 正在執行，不能刪除封存", 409)
                with loop_mod.workspace_operation_lock(ROOT, name, blocking=False):
                    with archive_operation_lock(create=False) as (_root, _root_fd, archive_fd):
                        with directory_fd(archive_id, f"封存項目 {archive_id}", dir_fd=archive_fd) as source_fd:
                            with exclusive_file_lock(".run.lock", f"{name} 的單 writer 鎖", dir_fd=source_fd):
                                _require_same_directory_entry(archive_fd, archive_id, source_fd,
                                                              f"封存項目 {archive_id}")
                                tombstone = f".delete-{uuid.uuid4().hex}"
                                _require_absent_entry(archive_fd, tombstone, "封存刪除暫存項目")
                                try:
                                    os.rename(archive_id, tombstone, src_dir_fd=archive_fd, dst_dir_fd=archive_fd)
                                except OSError as e:
                                    raise ArchiveOperationError(f"準備刪除封存失敗:{e}", 409) from e
                        _remove_tree_at(archive_fd, tombstone, f"封存刪除暫存項目 {tombstone}")
            except ArchiveOperationError as e:
                self._err(str(e), e.status)
                return
            except loop_mod.WorkspaceOperationLockError as e:
                self._err(str(e), 409)
                return
            except ValueError as e:
                self._err(str(e))
                return
        print(f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜永久刪除封存 {archive_id}", flush=True)
        self._out(200, json.dumps({"ok": True, "name": name, "archive_id": archive_id,
                                   "deleted": True}, ensure_ascii=False))

    @with_state_lock
    def api_drain(self, body):
        """要求目前 session 在完整處理本輪後停止；只寫旁路控制檔，不競寫 loop state。"""
        name = str(body.get("name") or "")
        st, err = read_state(name)
        if err:
            self._err(err)
            return
        loop_state = st.get("loop") or {}
        pid = loop_state.get("pid")
        session_id = loop_state.get("session_id")
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
        st, err = read_state(name)
        if err:
            self._err(err)
            return
        loop_state = st.get("loop") or {}
        pid = loop_state.get("pid")
        session_id = loop_state.get("session_id")
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

    def api_stop(self, body):
        name = str(body.get("name") or "")
        if not loop_mod.valid_workspace_name(name):
            self._err(f"workspace 名稱 {name or '(空)'} 不合法：{loop_mod.WORKSPACE_NAME_RULE}")
            return
        with JOBS_LOCK:
            j = JOBS.get(name)
        if j is not None and j.alive():
            workspace_console_log(name, f"停止 loop｜pid={j.popen.pid}")
            if not j.stop(wait=True):
                self._err(f"{name} 停止逾時，程序仍在執行中", 500)
                return
            self._out(200, json.dumps({"ok": True, "name": name}, ensure_ascii=False))
            return
        # 不是本 dashboard 啟動的:用 state.json 記錄的 pid 停(SIGINT 優雅收尾,8 秒後 SIGKILL)
        st, _ = read_state(name)
        pid = (st.get("loop") or {}).get("pid") if st else None
        if loop_pid_alive(pid):
            workspace_console_log(name, f"停止外部 loop｜pid={pid}")
            os.kill(int(pid), signal.SIGINT)

            def _force():
                if loop_pid_alive(pid):
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            t = threading.Timer(8, _force)
            t.daemon = True
            t.start()
            deadline = time.monotonic() + 9
            while time.monotonic() < deadline and loop_pid_alive(pid):
                time.sleep(0.1)
            if loop_pid_alive(pid):
                self._err(f"{name} 停止逾時，程序仍在執行中", 500)
                return
            self._out(200, json.dumps({"ok": True, "name": name, "external": True}, ensure_ascii=False))
            return
        # UI 的 fleet 狀態每幾秒同步一次，程序可能恰好在點擊前自行結束。
        # stop 應為冪等操作，避免這個正常競態跳出錯誤並讓按鈕卡在舊狀態。
        self._out(200, json.dumps({"ok": True, "name": name, "already_stopped": True}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="loop-agent-lite dashboard(fleet + 直播 + launcher)")
    ap.add_argument("--name", default="", help="預選 workspace(可省;頁面內隨時可切)")
    ap.add_argument("--port", type=int, default=8765, help="被占用會自動往上找(最多 +20)")
    ap.add_argument("--read-only", action="store_true", help="唯讀實例:擋所有 POST,UI 隱藏操作鈕(分享看板用)")
    args = ap.parse_args()
    Handler.readonly = args.read_only

    load_config()  # 不存在就先建預設檔,讓人有得改
    if args.name:
        if not loop_mod.valid_workspace_name(args.name):
            sys.exit(f"❌ workspace 名稱不合法：{loop_mod.WORKSPACE_NAME_RULE}")
        names = ({d.name for d in ROOT.iterdir()
                  if loop_mod.valid_workspace_name(d.name) and not d.is_symlink() and d.is_dir()}
                 if ROOT.is_dir() else set())
        if args.name not in names:
            sys.exit(f"❌ workspace {args.name} 不存在,可用:{sorted(names) or '(無)'}")
    Handler.preselect = args.name

    def _sigterm(*_):
        raise KeyboardInterrupt  # 走與 Ctrl-C 相同的優雅關閉路徑(stop_all_jobs)
    signal.signal(signal.SIGTERM, _sigterm)

    port = args.port
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


if __name__ == "__main__":
    main()
