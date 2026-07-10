#!/usr/bin/env python3
"""loop-agent-lite dashboard:fleet 總覽 + 計畫表格 + console 直播 + loop launcher。

邊界:
- 讀的部分是 projection(不是真相):只讀 workspace/<name>/ 檔案,不寫任何 truth。
- 寫的部分只有「spawn / 停止 loop.py 進程」:agent 命令是 dashboard.config.json 的固定選項
  (瀏覽器端只能選 index,塞不進任意命令);validate 可選預設或手寫;repo 從 config 的
  repo_roots 掃出來點選,也可手填。
- dashboard 關閉(SIGINT/SIGTERM)→ 對每個由它啟動的 loop 送 SIGINT 優雅收尾
  (loop 會存 state、殺掉自己的 agent),8 秒沒死再 SIGKILL 整個 process group。

stdlib only,綁 127.0.0.1。
"""
import argparse
import json
import mimetypes
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import loop as loop_mod          # 共用 Workspace/fresh_state,匯入計畫時建 state 不自己發明 schema
from work import validate_plan  # 計畫校驗單一來源(create-plan / 匯入共用)

HERE = Path(__file__).resolve().parent
ROOT = HERE / "workspace"
CONFIG_PATH = HERE / "dashboard.config.json"
MAX_CHUNK = 512 * 1024  # 單次 tail 最多回傳量

DEFAULT_CONFIG = {
    "agent_cmds": [
        {"label": "claude", "cmd": "claude -p"},
    ],
    "validate_cmds": [
        {"label": "mvn compile", "cmd": "mvn -q compile"},
        {"label": "mvn test", "cmd": "mvn -q test"},
        {"label": "react build+test+e2e", "cmd": "sh -c 'npm run build && npm test -- --run && npx playwright test'"},
    ],
    "repo_roots": ["~/IdeaProjects"],
    "notify_cmd": "",  # 終態通知(completed/stuck_stop/goal_missing),佔位符 {status} {name};空=不通知
    "defaults": {      # launch/run 的預設值,表單可覆蓋前三顆;防線參數只在這裡改
        "flag_threshold": 10, "done_threshold": 3, "round_timeout": 30,
        "red_limit": 20, "stall_limit": 300, "stuck_stop": False, "stuck_stop_count": 100,
    },
}

JOBS = {}          # name -> Job(由本 dashboard 啟動的 loop)
JOBS_LOCK = threading.Lock()


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
        t.start()

    def _reader(self):
        for line in self.popen.stdout:
            self.out.append(line.rstrip("\n"))

    def alive(self):
        return self.popen.poll() is None

    def info(self):
        return {"name": self.name, "repo": self.repo, "pid": self.popen.pid,
                "alive": self.alive(), "rc": self.popen.returncode,
                "tail": "\n".join(list(self.out)[-8:])}

    def stop(self):
        """SIGINT 優雅收尾(loop 存 state、殺自己的 agent);8 秒沒死 SIGKILL 整個 group。"""
        if not self.alive():
            return
        try:
            self.popen.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return

        def _force():
            if self.alive():
                try:
                    os.killpg(os.getpgid(self.popen.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        t = threading.Timer(8, _force)
        t.daemon = True
        t.start()


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


def spawn_loop(name, repo, agent_cmd, validate_cmd, ft, dt, rt, reset=False, notify_cmd="",
               red_limit=20, stall_limit=300, stuck_stop=False, stuck_count=100):
    """spawn loop.py 並登記進 JOBS(呼叫方需持 JOBS_LOCK)。"""
    cmd = [sys.executable, str(HERE / "loop.py"), "--repo", str(repo), "--name", name,
           "--agent-cmd", agent_cmd, "--validate-cmd", validate_cmd,
           "--flag-threshold", str(ft), "--done-threshold", str(dt), "--round-timeout", str(rt),
           "--red-limit", str(red_limit), "--stall-limit", str(stall_limit)]
    if stuck_stop:
        cmd += ["--stuck-stop", "--stuck-stop-count", str(stuck_count)]
    if reset:
        cmd.append("--reset-state")
    if notify_cmd:
        cmd += ["--notify-cmd", notify_cmd]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, start_new_session=True)
    JOBS[name] = Job(name, str(repo), p)
    return p


def read_state(name):
    """讀 workspace state.json;回 (state, err)。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        return None, f"workspace 名稱 {name or '(空)'} 不合法"
    p = ROOT / name / "state.json"
    if not p.exists():
        return None, f"workspace {name} 不存在(沒有 state.json)"
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except json.JSONDecodeError:
        return None, "state.json 讀取失敗(可能撞上寫入瞬間),再試一次"


def write_state(name, st):
    """原子寫 workspace state.json(共用 loop 的 tmp→fsync→os.replace,#6)。"""
    data = json.dumps(st, ensure_ascii=False, indent=2).encode("utf-8")
    loop_mod.atomic_write_bytes(ROOT / name / "state.json", data)


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
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已建立預設設定檔:{CONFIG_PATH}(agent/validate 選項、repo 掃描根目錄都在這改)", flush=True)
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"error": f"dashboard.config.json 解析失敗:{e}——修好前無法啟動新 loop"}


def scan_repos(cfg):
    """從 config 的 repo_roots 掃 git repo(根目錄本身或往下一層)。"""
    found = []
    for raw in cfg.get("repo_roots", []):
        root = Path(raw).expanduser()
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
    """fleet 總覽:逐 workspace 讀 state.json 摘要;讀不到(未啟動/寫入瞬間)只給名字。"""
    if not ROOT.is_dir():
        return []
    out = []
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir():
            continue
        info = {"name": d.name, "phase": None, "running": False}
        try:
            st = json.loads((d / "state.json").read_text(encoding="utf-8"))
            c = st.get("config") or {}
            info.update(phase=st.get("phase"), round=st.get("round", 0), flag=st.get("flag", 0),
                        completed=len(st.get("completed") or []), plan_len=len(st.get("plan") or []),
                        done_count=st.get("done_count", 0), repo=c.get("repo"),
                        running=ws_running(d.name, st))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
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
        """SSE:主畫面單向推送 fleet/state/history/console 增量；寫入操作仍走 REST。"""
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
        history_offset = -1
        tail_offset = -1
        current_round = 0
        try:
            while True:
                now = time.monotonic()
                if now >= fleet_at:
                    fleet = list_workspaces()
                    sig = json.dumps(fleet, ensure_ascii=False, sort_keys=True)
                    if sig != fleet_sig:
                        emit("workspaces", fleet)
                        fleet_sig = sig
                    fleet_at = now + 3

                if workspace:
                    state, err = read_state(workspace)
                    projected = {"error": err} if err else state
                    sig = json.dumps(projected, ensure_ascii=False, sort_keys=True)
                    if sig != state_sig:
                        emit("state", projected)
                        state_sig = sig
                    if state:
                        rnd = int(state.get("round") or 0)
                        if rnd > 0 and rnd != current_round:
                            current_round = rnd
                            tail_offset = -1
                            emit("round", {"round": rnd})
                        history = read_incremental(ROOT / workspace / "history.log", history_offset)
                        history_offset = history["size"]
                        if history["data"]:
                            emit("history", {"data": history["data"]})
                        if current_round > 0:
                            tail = read_incremental(ROOT / workspace / "logs" / f"round-{current_round:04d}.log",
                                                    tail_offset)
                            tail_offset = tail["size"]
                            if tail["data"]:
                                emit("tail", {"data": tail["data"]})

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
                self._out(200, json.dumps({"agent_cmds": cfg.get("agent_cmds", []),
                                           "validate_cmds": cfg.get("validate_cmds", []),
                                           "defaults": cfg.get("defaults") or {},
                                           "repos": scan_repos(cfg)}, ensure_ascii=False))
            elif u.path == "/api/jobs":
                with JOBS_LOCK:
                    self._out(200, json.dumps([j.info() for j in JOBS.values()], ensure_ascii=False))
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
                self._out(200, json.dumps({"goal": fstat("goal.md"),
                                           "tree_clean": clean}, ensure_ascii=False))
            elif u.path == "/api/state":
                d = self._ws_dir(q)
                if d is None:
                    return
                p = d / "state.json"
                if not p.exists():
                    self._out(200, json.dumps({"error": "state.json 不存在,loop 尚未啟動"}, ensure_ascii=False))
                    return
                try:
                    st = json.loads(p.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    self._out(200, json.dumps({"error": "busy"}))
                    return
                self._out(200, json.dumps(st, ensure_ascii=False))
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
            elif u.path == "/api/stop":
                self.api_stop(body)
            elif u.path == "/api/run":
                self.api_run(body)
            elif u.path == "/api/edit-state":
                self.api_edit_state(body)
            elif u.path == "/api/edit-config":
                self.api_edit_config(body)
            elif u.path == "/api/phase":
                self.api_phase(body)
            elif u.path == "/api/set-task":
                self.api_set_task(body)
            else:
                self._err("not found", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
            self._err(f"agent_idx 不合法,合法值 0..{len(agents) - 1}(選項在 dashboard.config.json)")
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
            rl = int(body.get("red_limit") or d.get("red_limit", 20))
            sl = int(body.get("stall_limit") or d.get("stall_limit", 300))
            if ft < 1 or dt < 1 or rt < 0 or rl < 1 or sl < 1:
                raise ValueError
        except (TypeError, ValueError):
            self._err("flag/done/red/stall 必須 ≥1 的整數,round_timeout 必須 ≥0(分鐘)")
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
                print(f"⎇ {repo} → {br}", flush=True)
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
                    print(f"⤴ goal.md 已隨啟動 commit:{repo}", flush=True)
            if normalized is not None:
                lws = loop_mod.Workspace(name)
                st_new = lws.fresh_state()
                st_new["plan"] = normalized
                st_new["plan_version"] = 1
                st_new["phase"] = start_phase
                if start_phase == "exec":
                    st_new["current_order"] = normalized[0]["order"]
                lws.save_state(st_new)
                print(f"⤴ 匯入 plan.json:{name} 共 {len(normalized)} 條,從 {start_phase} 開跑", flush=True)
            p = spawn_loop(name, repo, agent_cmd, validate_cmd, ft, dt, rt,
                           reset=bool(body.get("reset_state")) and normalized is None,
                           notify_cmd=str(cfg.get("notify_cmd") or ""),
                           red_limit=rl, stall_limit=sl,
                           stuck_stop=bool(d.get("stuck_stop")),
                           stuck_count=int(d.get("stuck_stop_count", 100)))
        print(f"▶ 啟動 loop:{name}(pid {p.pid})repo={repo}", flush=True)
        self._out(200, json.dumps({"ok": True, "name": name, "pid": p.pid}, ensure_ascii=False))

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
            self._err("state 裡的 agent 命令不在 dashboard.config.json 固定選項內"
                      "(可能被改過或設定已更新),請用啟動表單重新啟動")
            return
        if not (Path(repo) / ".git").exists():
            self._err(f"{repo} 不是 git repo(repo 被移走了?)")
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
                           c.get("round_timeout", 30), notify_cmd=str(cfg.get("notify_cmd") or ""),
                           red_limit=c.get("red_limit", d.get("red_limit", 20)),
                           stall_limit=c.get("stall_limit", d.get("stall_limit", 300)),
                           stuck_stop=bool(d.get("stuck_stop")),
                           stuck_count=int(d.get("stuck_stop_count", 100)))
        print(f"▶ run workspace:{name}(pid {p.pid})", flush=True)
        self._out(200, json.dumps({"ok": True, "name": name, "pid": p.pid}, ensure_ascii=False))

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
        print(f"✎ 人工編輯 {name}:{', '.join(changed) or '無變更'}", flush=True)
        self._out(200, json.dumps({"ok": True, "changed": changed}, ensure_ascii=False))

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
        # 五顆數字旋鈕(round_timeout≥0,其餘≥1)
        for k, lo in (("flag_threshold", 1), ("done_threshold", 1), ("round_timeout", 0),
                      ("red_limit", 1), ("stall_limit", 1)):
            if body.get(k) is None:
                continue
            try:
                v = float(body[k]) if k == "round_timeout" else int(body[k])
                if v < lo:
                    raise ValueError
            except (TypeError, ValueError):
                self._err(f"{k} 不合法(round_timeout 需 ≥0,其餘需 ≥1 的整數)")
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
                self._err(f"agent_idx 不合法,合法值 0..{len(agents) - 1}(選項在 dashboard.config.json)")
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
        print(f"⚙ 編輯設定 {name}:{', '.join(changed) or '無變更'}", flush=True)
        self._out(200, json.dumps({"ok": True, "changed": changed}, ensure_ascii=False))

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
        print(f"⇄ 切換 phase:{name} → {target}", flush=True)
        self._out(200, json.dumps({"ok": True, "phase": target}, ensure_ascii=False))

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
        print(f"⏵ 進度調整:{name} → task-{order}"
              + (f"(人工標記完成:{skipped})" if skipped else ""), flush=True)
        self._out(200, json.dumps({"ok": True, "current_order": order,
                                   "human_marked": skipped}, ensure_ascii=False))

    def api_stop(self, body):
        name = str(body.get("name") or "")
        with JOBS_LOCK:
            j = JOBS.get(name)
        if j is not None and j.alive():
            j.stop()
            print(f"⏹ 停止 loop:{name}(pid {j.popen.pid})", flush=True)
            self._out(200, json.dumps({"ok": True, "name": name}, ensure_ascii=False))
            return
        # 不是本 dashboard 啟動的:用 state.json 記錄的 pid 停(SIGINT 優雅收尾,8 秒後 SIGKILL)
        st, _ = read_state(name)
        pid = (st.get("loop") or {}).get("pid") if st else None
        if loop_pid_alive(pid):
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
            print(f"⏹ 停止外部 loop:{name}(pid {pid})", flush=True)
            self._out(200, json.dumps({"ok": True, "name": name, "external": True}, ensure_ascii=False))
            return
        self._err(f"{name} 沒有在執行中")


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
    print(f"dashboard → http://127.0.0.1:{port}/  設定檔:{CONFIG_PATH.name}", flush=True)
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
