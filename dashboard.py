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
import fcntl
import functools
import json
import mimetypes
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import loop as loop_mod          # 共用 Workspace/fresh_state,匯入計畫時建 state 不自己發明 schema
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

JOBS = {}          # name -> Job(由本 dashboard 啟動的 loop)
JOBS_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()

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
    workspace_dir = ROOT / name
    (workspace_dir / "startup_ready.json").unlink(missing_ok=True)
    if not import_plan:
        (workspace_dir / "import-plan.pending.json").unlink(missing_ok=True)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, start_new_session=True, env=env)
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
    ready = ROOT / name / "startup_ready.json"
    try:
        marker = json.loads(ready.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "starting"}
    if marker.get("pid") == expected_pid:
        return {"status": "ready", "pid": expected_pid}
    return {"status": "starting"}


def read_state(name, *, repair=True):
    """讀 workspace state；主檔壞時可由 last-good checkpoint 復原。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        return None, f"workspace 名稱 {name or '(空)'} 不合法"
    state_path = ROOT / name / "state.json"
    try:
        state, _data, recovered = loop_mod.load_checkpointed_state(state_path, repair=repair)
    except FileNotFoundError:
        return None, f"workspace {name} 不存在(沒有 state.json/checkpoint)"
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
    data = json.dumps(st, ensure_ascii=False, indent=2).encode("utf-8")
    loop_mod.write_checkpointed_state(ROOT / name / "state.json", data)


def read_report(name):
    """REPORT.md 唯讀投影:只在全部任務收斂完成後由 loop 產生,不存在回明確 error。"""
    try:
        return {"content": (ROOT / name / "REPORT.md").read_text(encoding="utf-8")}
    except FileNotFoundError:
        return {"error": "REPORT.md 不存在——全部任務收斂完成後才會由 loop 產生"}
    except OSError as e:
        return {"error": f"REPORT.md 讀取失敗:{e}"}


def read_goal(name):
    """goal 唯讀投影:從 state.config 記錄的 repo+goal 相對路徑讀人類真相,不寫回。"""
    st, err = read_state(name, repair=False)
    if err:
        return {"error": err}
    c = st.get("config") or {}
    repo, goal_rel = c.get("repo"), c.get("goal") or "goal.md"
    if not repo:
        return {"error": "state 缺 repo 設定(舊版 state)——用啟動表單跑過一次後即可檢視 goal"}
    goal_path = Path(repo).expanduser() / goal_rel
    try:
        return {"content": goal_path.read_text(encoding="utf-8"), "path": str(goal_path),
                "goal_changed": bool(st.get("goal_changed"))}
    except FileNotFoundError:
        return {"error": f"goal 檔不存在:{goal_path}(repo 被移走或 goal 被刪?)"}
    except OSError as e:
        return {"error": f"goal 讀取失敗:{e}"}


def read_prompt(name):
    """最近一輪送出的 prompt 唯讀投影(loop 只保留最近幾份,取 round 編號最大者)。"""
    def round_num(path):
        m = re.search(r"round-(\d+)", path.name)
        return int(m.group(1)) if m else -1

    try:
        latest = max((ROOT / name / "prompts").glob("round-*.md"), key=round_num, default=None)
        if latest is None:
            return {"error": "尚無 prompt 紀錄——loop 送出第一輪後才會出現"}
        return {"content": latest.read_text(encoding="utf-8"),
                "round": round_num(latest), "file": latest.name}
    except OSError as e:
        return {"error": f"prompt 讀取失敗:{e}"}


def workspace_console_log(name, message):
    """將 Dashboard 操作與 loop/Agent 寫進同一條 workspace console 時序。"""
    line = f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜{message}"
    print(line, flush=True)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        return
    try:
        workspace_dir = ROOT / name
        workspace_dir.mkdir(parents=True, exist_ok=True)
        loop_mod.append_console(workspace_dir / "console.log", line)
    except OSError as e:
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
    if CONFIG_OVERRIDE:
        if not PERSONAL_CONFIG_PATH.exists():
            loop_mod.atomic_write_bytes(
                PERSONAL_CONFIG_PATH,
                json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        try:
            return json.loads(PERSONAL_CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return {"error": f"覆寫設定檔 {PERSONAL_CONFIG_PATH} 解析失敗:{e}"}

    project = dict(DEFAULT_CONFIG)
    if PROJECT_CONFIG_PATH.exists():
        try:
            project.update(json.loads(PROJECT_CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            return {"error": f"團隊設定檔 {PROJECT_CONFIG_PATH.name} 解析失敗:{e}"}

    if not PERSONAL_CONFIG_PATH.exists() and LEGACY_CONFIG_PATH.exists():
        try:
            legacy = json.loads(LEGACY_CONFIG_PATH.read_text(encoding="utf-8"))
            migrated = {key: legacy[key] for key in PERSONAL_CONFIG_KEYS if key in legacy}
            loop_mod.atomic_write_bytes(
                PERSONAL_CONFIG_PATH,
                json.dumps(migrated, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            print(f"已將舊個人設定遷移至:{PERSONAL_CONFIG_PATH}（舊檔保留）", flush=True)
        except json.JSONDecodeError as e:
            return {"error": f"舊個人設定檔 {LEGACY_CONFIG_PATH.name} 解析失敗:{e}"}

    personal = {}
    if PERSONAL_CONFIG_PATH.exists():
        try:
            personal = json.loads(PERSONAL_CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return {"error": f"個人設定檔 {PERSONAL_CONFIG_PATH.name} 解析失敗:{e}"}
    for key in PERSONAL_CONFIG_KEYS:
        if key in personal:
            project[key] = personal[key]
    return project


def config_projection(cfg):
    raw_paths, resolved_paths = configured_path_dirs(cfg)
    return {"agent_cmds": cfg.get("agent_cmds", []),
            "validate_cmds": cfg.get("validate_cmds", []),
            "defaults": cfg.get("defaults") or {},
            "extra_path_dirs": raw_paths,
            "resolved_extra_path_dirs": resolved_paths,
            "config_path": str(PERSONAL_CONFIG_PATH),
            "personal_config_path": str(PERSONAL_CONFIG_PATH),
            "project_config_path": str(PROJECT_CONFIG_PATH),
            "config_override": bool(CONFIG_OVERRIDE),
            "repo_roots": cfg.get("repo_roots", DEFAULT_CONFIG["repo_roots"]),
            "repos": scan_repos(cfg)}


def save_personal_config(updates):
    """只寫個人檔；完整覆寫模式則保留 env 指定檔案的既有欄位。"""
    current = {}
    if PERSONAL_CONFIG_PATH.exists():
        current = json.loads(PERSONAL_CONFIG_PATH.read_text(encoding="utf-8"))
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
        if not d.is_dir():
            continue
        if not ((d / "state.json").is_file() or (d / "state.last-good.json").is_file()):
            continue
        info = {"name": d.name, "phase": None, "running": False}
        st, err = read_state(d.name, repair=False)
        if not err:
            c = st.get("config") or {}
            loop_state = st.get("loop") or {}
            running = ws_running(d.name, st)
            info.update(phase=st.get("phase"), round=st.get("round", 0), flag=st.get("flag", 0),
                        completed=len(st.get("completed") or []), plan_len=len(st.get("plan") or []),
                        done_count=st.get("done_count", 0), repo=c.get("repo"),
                        running=running,
                        draining=running and loop_mod.stop_after_round_requested(
                            d, loop_state.get("pid"), loop_state.get("session_id")))
        out.append(info)
    return out


TAIL_INIT = 64 * 1024  # offset<0(首抓)時只回檔案尾段,超長 log 秒開


def read_incremental(path: Path, offset: int):
    if not path.exists():
        return {"size": 0, "data": ""}
    size = path.stat().st_size
    if offset < 0:  # 首抓:直接跳到尾段,從下一個完整行開始
        offset = max(0, size - TAIL_INIT)
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(MAX_CHUNK)
        if offset > 0:
            nl = data.find(b"\n")
            if nl != -1:
                offset += nl + 1
                data = data[nl + 1:]
        return {"size": offset + len(data), "data": data.decode("utf-8", errors="replace")}
    if offset > size:
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(MAX_CHUNK)
    return {"size": offset + len(data), "data": data.decode("utf-8", errors="replace")}


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
        valid = {d.name for d in ROOT.iterdir() if d.is_dir()} if ROOT.is_dir() else set()
        if name not in valid:
            self._err(f"未知 workspace:{name or '(空)'},可用:{sorted(valid)}")
            return None
        return ROOT / name

    def _serve_events(self, q):
        """SSE:主畫面單向推送 fleet/state/完整 console 增量；寫入操作仍走 REST。"""
        workspace = q.get("ws", [""])[0]
        if workspace and not re.fullmatch(r"[A-Za-z0-9._-]+", workspace):
            self._err("workspace 名稱不合法")
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

        fleet_sig = state_sig = None
        fleet_at = keepalive_at = 0.0
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
                        fleet_sig = sig
                    # dashboard 自己啟動的 job 可能在 preflight 立刻退出；快速同步避免
                    # UI 還顯示「停止」數秒，使用者再點後才發現程序早已結束。
                    fleet_at = now + 0.6

                if workspace:
                    # GET/SSE 永遠只讀；真正修復由 loop resume 或後續明確 mutation 完成，
                    # 避免 Dashboard 在活躍 loop 的 agent round 中途改 python-owned state。
                    state, err = read_state(workspace, repair=False)
                    projected = {"error": err} if err else state
                    sig = json.dumps(projected, ensure_ascii=False, sort_keys=True)
                    if sig != state_sig:
                        emit("state", projected)
                        state_sig = sig
                    console_path = ROOT / workspace / "console.log"
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
                    self._out(200, json.dumps([j.info() for j in JOBS.values()], ensure_ascii=False))
            elif u.path == "/api/job-startup":
                name = q.get("name", [""])[0]
                pid = q.get("pid", [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
                    self._err("workspace 名稱不合法")
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
                self._out(200, json.dumps(
                    read_incremental(d / "history.log", off), ensure_ascii=False))
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
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError):
            self._err("body 必須是 JSON")
            return
        try:
            if u.path == "/api/launch":
                self.api_launch(body)
            elif u.path == "/api/drain":
                self.api_drain(body)
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
            elif u.path == "/api/phase":
                self.api_phase(body)
            elif u.path == "/api/set-task":
                self.api_set_task(body)
            elif u.path == "/api/archive-workspace":
                self.api_archive_workspace(body)
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
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            self._err(f"workspace 名稱 {name} 不合法,只允許英數 . _ -,例:legacy-orders")
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
                (repo / "goal.md").write_text(goal_content, encoding="utf-8")
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
        """停止狀態下的人工編輯:plan 任務文字敘述 + done 計數。執行中全部鎖死。"""
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
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            self._err(f"workspace 名稱 {name} 不合法,只允許英數 . _ -")
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
            save_personal_config({"agent_cmds": agents, "extra_path_dirs": paths})
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
            save_personal_config({"repo_roots": roots})
            cfg = load_config()
        self._out(200, json.dumps(config_projection(cfg), ensure_ascii=False))

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
        """停止狀態下封存 workspace:整個目錄搬進 .archive/(軟刪除,可手動搬回還原)。
        不動 target repo;執行中或單 writer 鎖仍被持有時 fail-closed 拒絕。"""
        name = str(body.get("name") or "")
        st, err = read_state(name)
        if err:
            self._err(err)
            return
        if ws_running(name, st):
            self._err(f"{name} 執行中,不能封存——先停止")
            return
        wsd = ROOT / name
        # pid 偵測可能失準(SIGKILL 殘值/ps 誤判);flock 是 loop 單 writer 的機械真相,
        # 拿不到鎖就代表還有 loop 活著,寧可擋下也不搬走執行中的 state。
        try:
            lock_file = open(wsd / ".run.lock", "a+b")
        except OSError as e:
            self._err(f"無法檢查單 writer 鎖:{e}")
            return
        try:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._err(f"{name} 的單 writer 鎖仍被持有(loop 還活著),不能封存")
                return
            archive_root = ROOT / ".archive"
            archive_root.mkdir(exist_ok=True)
            target = archive_root / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                os.rename(wsd, target)
            except OSError as e:
                self._err(f"封存失敗:{e}")
                return
        finally:
            lock_file.close()
        with JOBS_LOCK:
            JOBS.pop(name, None)  # 已結束 job 的殘影一併移除,避免 stale tail/名稱衝突
        print(f"[{time.strftime('%H:%M:%S')}] 🖥️ Dashboard｜封存 workspace {name} → {target}", flush=True)
        self._out(200, json.dumps({"ok": True, "archived_to": str(target)}, ensure_ascii=False))

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
        if loop_mod.stop_after_round_requested(ROOT / name, pid, session_id):
            self._out(200, json.dumps({"ok": True, "name": name, "pid": pid,
                                       "requested": True, "already_requested": True},
                                      ensure_ascii=False))
            return
        payload = {"pid": int(pid), "session_id": session_id,
                   "requested_at": datetime.now().isoformat(timespec="seconds")}
        loop_mod.atomic_write_bytes(
            ROOT / name / loop_mod.STOP_AFTER_ROUND_FILE,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        workspace_console_log(name, f"已要求本輪完整結束後停止｜pid={pid}")
        self._out(200, json.dumps({"ok": True, "name": name, "pid": pid,
                                   "requested": True}, ensure_ascii=False))

    def api_stop(self, body):
        name = str(body.get("name") or "")
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
        names = {d.name for d in ROOT.iterdir() if d.is_dir()} if ROOT.is_dir() else set()
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
