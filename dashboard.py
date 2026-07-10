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


PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>loop-lite</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box;margin:0}
html,body{height:100%;overflow:hidden} /* 頁面本身永不捲動:左右兩欄各自內部 scroll */
body{display:grid;grid-template-rows:auto 1fr;
     font:14px/1.5 -apple-system,"Noto Sans TC",sans-serif;background:#0d1117;color:#c9d1d9}
#tabs{display:flex;gap:6px;flex-wrap:wrap;padding:8px 12px;border-bottom:1px solid #30363d;background:#161b22;align-items:center}
.tab{padding:2px 10px;border-radius:6px;background:#21262d;border:1px solid #30363d;
     color:#c9d1d9;font-size:12px;cursor:pointer;white-space:nowrap}
.tab.active{border-color:#58a6ff;background:#1c2a3a}
.tab .dot{margin-right:5px}
#launchbtn{margin-left:auto;background:#238636;border-color:#2ea043}
#main{display:grid;grid-template-columns:minmax(400px,44%) 1fr;min-height:0}
#left{display:flex;flex-direction:column;border-right:1px solid #30363d;min-width:0;min-height:0;position:relative}
#head{padding:10px 14px;border-bottom:1px solid #30363d;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
h1{font-size:15px;margin-right:6px}
.chip{padding:1px 10px;border-radius:10px;background:#21262d;font-size:12px;white-space:nowrap}
.phase-plan{background:#1f3a5f}.phase-exec{background:#5a4a1f}.phase-done{background:#1f5f2f}
#tablewrap{flex:1;overflow:auto;min-height:0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:5px 10px;border-bottom:1px solid #21262d;text-align:left;vertical-align:top}
th{position:sticky;top:0;background:#161b22;font-weight:600}
td.st{white-space:nowrap}
tr.ok td{color:#7ee787}
tr.cur td{color:#f0c674;background:#1c1f26}
td.task{white-space:pre-wrap;word-break:break-word}
.ref{color:#8b949e;font-size:12px}
#events{max-height:170px;overflow-y:auto;border-top:1px solid #30363d;padding:6px 10px;
        font:11px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:#8b949e;
        white-space:pre-wrap;word-break:break-all}
#right{display:flex;flex-direction:column;min-width:0;min-height:0;position:relative}
#rhead{padding:10px 14px;border-bottom:1px solid #30363d;font-size:13px;color:#8b949e}
#console{flex:1;overflow:auto;min-height:0;padding:10px 14px;white-space:pre-wrap;word-break:break-word;
         font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
#jump{position:absolute;right:18px;bottom:14px;background:#1f6feb;border:none;border-radius:14px;
      color:#fff;padding:4px 14px;font-size:12px;cursor:pointer;box-shadow:0 2px 8px #0008}
td.task .tt{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer}
tr.expand td.task .tt,tr.cur td.task .tt{display:block;-webkit-line-clamp:unset}
tr.sum td{color:#7ee787;cursor:pointer;background:#12251a}
.chip.alert{background:#5a1f1f;cursor:pointer}
.chip.warn{background:#5a4a1f}
#e_clr{background:#8b2626;border:none;border-radius:5px;color:#fff;padding:3px 10px;cursor:pointer}
@keyframes flashbg{0%{background:#1f3a5f}100%{background:transparent}}
tr.flash td{animation:flashbg 1.6s ease-out}
.chip.flash{animation:flashbg 1.6s ease-out}
.divider{color:#58a6ff}
#overlay,#issoverlay,#cfgoverlay{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:flex-start;justify-content:center;padding-top:6vh;z-index:10}
[hidden]{display:none !important}
#panel,#ispanel,#cfgpanel{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px;width:min(680px,92vw);max-height:86vh;overflow:auto}
#cfgpanel label{display:block;font-size:12px;color:#8b949e;margin:8px 0 2px}
#cfgpanel select,#cfgpanel input{width:100%;padding:5px 8px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:13px}
#cfg_close{float:right;width:auto;background:#21262d;border:1px solid #30363d;border-radius:5px;color:#c9d1d9;padding:1px 10px;cursor:pointer}
#cfg_save{margin-top:12px;padding:6px 18px;background:#1f6feb;border:none;border-radius:6px;color:#fff;cursor:pointer}
#ispanel{width:min(780px,94vw)}
#iswrap{max-height:62vh;overflow:auto;border:1px solid #21262d;border-radius:8px;margin-top:8px}
#is_close{float:right;width:auto;background:#21262d;border:1px solid #30363d;border-radius:5px;color:#c9d1d9;padding:1px 10px;cursor:pointer}
#is_clear{background:#8b2626;border:none;border-radius:5px;color:#fff;padding:3px 12px;cursor:pointer}
#jumpcur{position:absolute;right:16px;background:#1f6feb;border:none;border-radius:14px;
         color:#fff;padding:4px 14px;font-size:12px;cursor:pointer;box-shadow:0 2px 8px #0008;z-index:5}
#panel h2{font-size:14px;margin:10px 0 4px}
#panel label{display:block;font-size:12px;color:#8b949e;margin:8px 0 2px}
#panel select,#panel input,#panel textarea{width:100%;padding:5px 8px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:13px}
#panel textarea{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;resize:vertical}
#f_plan.bad{border-color:#f85149;outline:1px solid #f85149}
#planerr{color:#f85149;font-size:12px;margin-top:4px;white-space:pre-wrap}
#f_tpl{float:right;width:auto;background:#21262d;border:1px solid #30363d;border-radius:5px;
       color:#c9d1d9;padding:1px 10px;font-size:11px;cursor:pointer}
button.goto{background:#21262d;border:1px solid #30363d;border-radius:4px;color:#58a6ff;cursor:pointer;padding:0 6px;margin-left:6px;font-size:11px}
.nums{display:flex;gap:10px}.nums label{flex:1}
#f_go{padding:6px 18px;background:#238636;border:none;border-radius:6px;color:#fff;cursor:pointer;margin-top:12px}
#f_import{padding:4px 14px;background:#1f6feb;border:none;border-radius:6px;color:#fff;cursor:pointer;margin-top:8px}
#f_msg{font-size:12px;margin-left:8px}
.job{border:1px solid #30363d;border-radius:8px;padding:8px 10px;margin:6px 0;font-size:12px}
.job pre{margin-top:6px;color:#8b949e;font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;max-height:120px;overflow:auto}
.job button{background:#8b2626;border:none;border-radius:5px;color:#fff;padding:2px 10px;cursor:pointer;float:right}
.hint{color:#8b949e;font-size:11px;margin-top:12px}
textarea.edittask{width:100%;background:#0d1117;border:1px solid #58a6ff55;color:#c9d1d9;
                  font:inherit;border-radius:6px;padding:4px 6px;resize:vertical}
#editbar{display:flex;gap:8px;align-items:center;border-top:1px solid #30363d;padding:8px 10px;font-size:12px}
#editbar[hidden]{display:none}
#editbar input{width:70px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;padding:3px 6px}
#editbar button{background:#1f6feb;border:none;border-radius:5px;color:#fff;padding:3px 12px;cursor:pointer}
#wsctl .run{background:#238636}#wsctl .stp{background:#8b2626}
</style></head><body>
<div id=tabs><button class=tab id=launchbtn>＋ 啟動 / 管理</button></div>
<div id=main>
<div id=left>
  <div id=head>
    <h1>loop-lite · <span id=wsname>…</span></h1>
    <span class=chip id=phase>…</span><span class=chip id=round></span>
    <span class=chip id=prog></span><span class=chip id=flag></span><span class=chip id=done></span>
    <span class=chip id=extra></span><span class="chip alert" id=issues style="display:none"></span>
    <span class="chip warn" id=goalwarn style="display:none"></span>
    <span id=wsctl style="margin-left:auto;display:flex;gap:6px"></span>
  </div>
  <div id=tablewrap><table>
    <thead><tr><th style="width:40px">#</th><th>任務</th><th style="width:140px">狀態</th></tr></thead>
    <tbody id=rows><tr><td colspan=3 style="color:#8b949e">(等待資料…)</td></tr></tbody>
  </table></div>
  <div id=editbar hidden>
    done 計數 <input id=e_done type=number min=0>
    <button id=e_save>💾 儲存</button><span id=e_msg class=hint></span>
  </div>
  <div id=events></div>
  <button id=jumpcur hidden>→ 回到執行中</button>
</div>
<div id=right>
  <div id=rhead>agent console 直播(workspace logs/round-*.log)</div>
  <div id=console></div>
  <button id=jump hidden>⤓ 跟到最新</button>
</div>
</div>
<div id=issoverlay hidden><div id=ispanel>
  <h2>⚠ Issues(agent 回報的結構化問題,不影響計數)<button id=is_close>✕</button></h2>
  <div id=iswrap><table>
    <thead><tr><th style="width:56px">round</th><th style="width:86px">位置</th><th>內容</th><th style="width:150px">時間</th></tr></thead>
    <tbody id=isrows></tbody>
  </table></div>
  <div style="margin-top:10px"><button id=is_clear>清空全部(停止時才可)</button><span id=is_msg class=hint></span></div>
</div></div>
<div id=cfgoverlay hidden><div id=cfgpanel>
  <h2>⚙ 編輯設定(停止時才可;▶ 運行時生效)<button id=cfg_close>✕</button></h2>
  <label>Agent 命令<select id=cfg_agent></select></label>
  <label>Validate 命令<input id=cfg_validate placeholder="mvn -q test"></label>
  <div class=nums>
    <label>flag 收斂(>)<input id=cfg_flag type=number min=1></label>
    <label>done 收斂(≥)<input id=cfg_done type=number min=1></label>
    <label>單輪上限(分)<input id=cfg_timeout type=number min=0></label>
  </div>
  <div class=nums>
    <label>紅燈連跳 reset<input id=cfg_red type=number min=1></label>
    <label>HEAD 停滯 reset<input id=cfg_stall type=number min=1></label>
  </div>
  <div><button id=cfg_save>💾 儲存設定</button><span id=cfg_msg class=hint></span></div>
</div></div>
<div id=overlay hidden><div id=panel>
  <h2>啟動新 loop</h2>
  <label>Repo(config 的 repo_roots 掃出來的)<select id=f_repo></select></label>
  <input id=f_repo_custom placeholder="/path/to/repo" hidden style="margin-top:4px">
  <div id=repostatus class=hint style="margin-top:6px"></div>
  <label>goal.md(選檔;gate#1=你審過的版本,啟動時自動 commit;留空=沿用 repo 已 commit 的)
    <input type=file id=f_goalfile accept=".md,.markdown,.txt"></label>
  <label>匯入 plan.json(選填,貼上即匯入)
    <button id=f_tpl type=button>📋 複製範本</button>
    <textarea id=f_plan rows=4 placeholder='留空=沿用既有計畫或從零規劃'></textarea></label>
  <div id=planerr hidden></div>
  <div id=planopts hidden class=hint>⚠️ 貼了 plan.json = 建全新 state(舊進度清除)。從哪個階段開跑:
    <label style="display:inline"><input type=radio name=sp value=plan checked style="width:auto"> 規劃期(讓 agent 補完)</label>
    <label style="display:inline"><input type=radio name=sp value=exec style="width:auto"> 直接執行期</label>
  </div>
  <label>Workspace 名稱(留空=repo 目錄名)<input id=f_name></label>
  <label>Agent 命令(dashboard.config.json 固定選項)<select id=f_agent></select></label>
  <label>Validate 命令<select id=f_validate></select></label>
  <input id=f_validate_custom placeholder="mvn -q test" hidden style="margin-top:4px">
  <div class=nums>
    <label>flag 收斂(>)<input id=f_flag type=number value=10 min=1></label>
    <label>done 收斂(≥)<input id=f_done type=number value=3 min=1></label>
    <label>單輪上限(分)<input id=f_timeout type=number value=30 min=0></label>
  </div>
  <label style="display:flex;align-items:center;gap:6px;margin-top:8px;color:#c9d1d9">
    <input type=checkbox id=f_reset style="width:auto">重置 workspace state(清掉舊進度從頭規劃;改過 goal/PLAN 後建議勾)
  </label>
  <label style="display:flex;align-items:center;gap:6px;margin-top:4px;color:#c9d1d9">
    <input type=checkbox id=f_branch style="width:auto">在新 branch 跑(loop/&lt;workspace名&gt;;已存在就 checkout 續用,不弄髒主線)
  </label>
  <div><button id=f_go>▶ 啟動</button><span id=f_msg></span></div>
  <h2>由本 dashboard 啟動的 loop</h2>
  <div id=jobs>(無)</div>
  <div class=hint>設定檔:dashboard.config.json(agent/validate 選項、repo 掃描根目錄)。
  ⚠️ 關閉 dashboard 會停掉上面列的全部 loop(SIGINT 優雅收尾,state 已落地可續跑)。</div>
</div></div>
<script>
const $=id=>document.getElementById(id);
const PRESELECT='%%PRESELECT%%';
const RO='%%RO%%'==='1';
let WS='',wsList=[],curRound=0,sr=0,off=0,hOff=0,evLines=[],panelOpen=false,editing=false,lastState=null;
let lastRowsKey='',pendingCur=false,expanded=new Set(),conLen=0,planFlashUntil=0;
let showDone=localStorage.getItem('showdone')==='1';
function chip(id,txt,show=true){const e=$(id);e.textContent=txt;e.style.display=show?'':'none';}
function esc(x){return String(x).replace(/[&<>"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));}
async function jget(u){try{const r=await fetch(u);return await r.json();}catch(e){return null;}}
async function jpost(u,obj){try{const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});return await r.json();}catch(e){return {error:'連線失敗'};}}
const DOT={plan:'#58a6ff',exec:'#f0c674',done:'#7ee787'};
function renderTabs(){
  const bar=$('tabs');
  bar.querySelectorAll('.wstab').forEach(e=>e.remove());
  const btn=$('launchbtn');
  wsList.forEach(w=>{
    const b=document.createElement('button');
    b.className='tab wstab'+(w.name===WS?' active':'');
    const dot=document.createElement('span');
    dot.className='dot';dot.textContent='●';dot.style.color=DOT[w.phase]||'#8b949e';
    b.appendChild(dot);
    let info=w.name;
    if(w.phase==='plan')info+=' · plan f'+w.flag+' r'+w.round;
    else if(w.phase==='exec')info+=' · '+w.completed+'/'+w.plan_len+' r'+w.round;
    else if(w.phase==='done')info+=' · 🏁';
    b.appendChild(document.createTextNode((w.running?'▶ ':'')+info));
    b.onclick=()=>switchWs(w.name);
    bar.insertBefore(b,btn);
  });
  renderCtl();
}
function wsEntry(){return wsList.find(w=>w.name===WS);}
function renderCtl(){
  const w=wsEntry();const box=$('wsctl');box.innerHTML='';
  if(!w||RO)return; // 唯讀模式:不給任何操作鈕
  if(w.running&&editing){editing=false;$('editbar').hidden=true;}
  const b=document.createElement('button');
  b.className='tab '+(w.running?'stp':'run');
  b.textContent=w.running?'⏹ 停止':'▶ 運行';
  b.onclick=async()=>{
    b.disabled=true;
    const r=await jpost(w.running?'/api/stop':'/api/run',{name:WS});
    if(r.error)alert(r.error);
    setTimeout(pollWorkspaces,900);
  };
  box.appendChild(b);
  if(!w.running&&w.phase){
    if(w.phase==='plan'&&w.plan_len>0){
      const x=document.createElement('button');x.className='tab';x.textContent='⏩ 進執行期';
      x.onclick=async()=>{
        if(!confirm('直接進入執行期,從第一個任務開始。繼續?'))return;
        const r=await jpost('/api/phase',{name:WS,phase:'exec'});
        if(r.error)alert(r.error);
        setTimeout(()=>{pollWorkspaces();pollState();},300);
      };
      box.appendChild(x);
    }
    if(w.phase==='exec'||w.phase==='done'){
      const x=document.createElement('button');x.className='tab';x.textContent='⏪ 回規劃期';
      x.onclick=async()=>{
        if(!confirm('回到規劃期:執行進度(完成紀錄/計數)全部歸零,計畫保留。繼續?'))return;
        const r=await jpost('/api/phase',{name:WS,phase:'plan'});
        if(r.error)alert(r.error);
        setTimeout(()=>{pollWorkspaces();pollState();},300);
      };
      box.appendChild(x);
    }
    const e=document.createElement('button');e.className='tab';
    e.textContent=editing?'✕ 取消':'✎ 編輯計畫';
    e.onclick=()=>{
      editing=!editing;
      lastRowsKey=''; // 進出編輯模式都強制重繪表格
      if(editing)enterEdit();else{$('editbar').hidden=true;$('e_msg').textContent='';pollState();}
      renderCtl();
    };
    box.appendChild(e);
    const g=document.createElement('button');g.className='tab';g.textContent='⚙ 設定';
    g.onclick=openCfgEdit;
    box.appendChild(g);
  }
}
async function openCfgEdit(){
  const c=(lastState&&lastState.config)||{};
  const cf=await jget('/api/config');
  const as=$('cfg_agent');as.innerHTML='';
  as.appendChild(new Option('(保持不變:'+(c.agent_cmd||'?')+')',''));
  (cf&&cf.agent_cmds||[]).forEach((a,i)=>as.appendChild(new Option(a.label+' — '+a.cmd,i)));
  $('cfg_validate').value=c.validate_cmd||'';
  $('cfg_flag').value=c.flag_threshold??10;
  $('cfg_done').value=c.done_threshold??3;
  $('cfg_timeout').value=c.round_timeout??30;
  $('cfg_red').value=c.red_limit??20;
  $('cfg_stall').value=c.stall_limit??300;
  $('cfg_msg').textContent='';$('cfgoverlay').hidden=false;
}
$('cfg_close').onclick=()=>{$('cfgoverlay').hidden=true;};
$('cfgoverlay').onclick=e=>{if(e.target.id==='cfgoverlay')$('cfgoverlay').hidden=true;};
$('cfg_save').onclick=async()=>{
  const body={name:WS,validate_cmd:$('cfg_validate').value.trim(),
    flag_threshold:+$('cfg_flag').value,done_threshold:+$('cfg_done').value,
    round_timeout:+$('cfg_timeout').value,red_limit:+$('cfg_red').value,stall_limit:+$('cfg_stall').value};
  if($('cfg_agent').value!=='')body.agent_idx=+$('cfg_agent').value; // 空=保持不變
  $('cfg_msg').textContent='儲存中…';
  const r=await jpost('/api/edit-config',body);
  if(r.error){$('cfg_msg').textContent='❌ '+r.error;return;}
  $('cfg_msg').textContent='✅ 已儲存 '+((r.changed||[]).join(', ')||'(無變更)');
  setTimeout(()=>{$('cfgoverlay').hidden=true;pollState();},700);
};
async function gotoTask(order){
  const s=lastState;if(!s)return;
  const doneOrders=new Set((s.completed||[]).map(e=>e.order));
  const skipped=(s.plan||[]).map(t=>t.order).filter(o=>o<order&&!doneOrders.has(o));
  const msg=skipped.length
    ?('跳到 task-'+order+':task '+skipped.join(', ')+' 會標記為「人工確認完成」,'
      +'並先跑 validate(綠燈才放行,可能要等一下)。繼續?')
    :('退回 task-'+order+' 重新執行:task-'+order+'(含)之後的完成紀錄會清除,'
      +'code 不會動(由之後的輪次驗收/重做)。繼續?');
  if(!confirm(msg))return;
  const r=await jpost('/api/set-task',{name:WS,order});
  if(r.error){alert(r.error);return;}
  pollWorkspaces();pollState();
}
function enterEdit(){
  const s=lastState;
  if(!s||!(s.plan||[]).length){alert('這個 workspace 還沒有 plan 可編輯');editing=false;renderCtl();return;}
  lastRowsKey='';
  $('rows').innerHTML=(s.plan||[]).map(t=>
    '<tr><td>'+t.order+'</td><td colspan=2><textarea class=edittask data-order="'+t.order+'" rows=2>'
    +esc(t.task)+'</textarea></td></tr>').join('');
  $('e_done').value=s.done_count||0;
  $('editbar').hidden=false;$('e_msg').textContent='';
}
$('e_save').onclick=async()=>{
  const tasks=[...document.querySelectorAll('textarea.edittask')].map(t=>({order:+t.dataset.order,task:t.value}));
  $('e_msg').textContent='儲存中…';
  const r=await jpost('/api/edit-state',{name:WS,tasks,done_count:+$('e_done').value});
  if(r.error){$('e_msg').textContent='❌ '+r.error;return;}
  $('e_msg').textContent='✅ 已儲存 '+((r.changed||[]).join(', ')||'(無變更)');
  editing=false;lastRowsKey='';$('editbar').hidden=true;renderCtl();pollState();
};
let issOpen=false;
function renderIssues(){ // modal 開著就跟著輪詢即時更新
  if(!issOpen||!lastState)return;
  const list=(lastState.issues||[]).slice().reverse(); // 最新在上
  $('isrows').innerHTML=list.length?list.map(i=>
    '<tr><td>'+i.round+'</td><td>'+esc(i.where||'')+'</td><td class=task>'+esc(i.text)
    +'</td><td class=ref>'+esc((i.ts||'').replace('T',' '))+'</td></tr>').join('')
    :'<tr><td colspan=4 style="color:#8b949e">(無)</td></tr>';
}
$('issues').onclick=()=>{issOpen=true;$('issoverlay').hidden=false;$('is_msg').textContent='';renderIssues();};
$('is_close').onclick=()=>{issOpen=false;$('issoverlay').hidden=true;};
$('issoverlay').onclick=e=>{if(e.target.id==='issoverlay'){issOpen=false;$('issoverlay').hidden=true;}};
$('is_clear').onclick=async()=>{
  if(!confirm('清空全部 issues?'))return;
  const r=await jpost('/api/edit-state',{name:WS,clear_issues:true});
  $('is_msg').textContent=r.error?('❌ '+r.error):'✅ 已清空';
  await pollState();renderIssues();
};
if(RO)$('is_clear').style.display='none';
function updateJumpCur(){ // 當前任務捲出視野才出現「回到執行中」
  const b=$('jumpcur');
  const cr=document.querySelector('#rows tr.cur');
  if(!cr||editing){b.hidden=true;return;}
  const tw=$('tablewrap');
  const r=cr.getBoundingClientRect(),wr=tw.getBoundingClientRect();
  b.style.bottom=($('events').offsetHeight+10)+'px';
  b.hidden=!(r.bottom<wr.top||r.top>wr.bottom);
}
$('tablewrap').onscroll=updateJumpCur;
$('jumpcur').onclick=()=>{
  const cr=document.querySelector('#rows tr.cur');
  if(cr)cr.scrollIntoView({block:'center'});
  $('jumpcur').hidden=true;
};
$('console').onscroll=()=>{
  const c=$('console');
  $('jump').hidden=c.scrollTop+c.clientHeight>=c.scrollHeight-60;
};
$('jump').onclick=()=>{const c=$('console');c.scrollTop=c.scrollHeight;$('jump').hidden=true;};
if(RO){$('launchbtn').style.display='none';}
function switchWs(n){
  if(!n||n===WS)return;
  WS=n;localStorage.setItem('ws',n);location.hash=n;document.title='loop-lite · '+n;
  $('wsname').textContent=n;
  curRound=0;sr=0;off=0;hOff=0;evLines=[];lastState=null;conLen=0;
  lastRowsKey='';pendingCur=true;expanded=new Set();
  editing=false;$('editbar').hidden=true;
  issOpen=false;$('issoverlay').hidden=true;$('cfgoverlay').hidden=true;$('jumpcur').hidden=true;
  $('console').textContent='';$('events').textContent='';
  $('rows').innerHTML='<tr><td colspan=3 style="color:#8b949e">(載入中…)</td></tr>';
  renderTabs();pollState();
}
async function pollWorkspaces(){
  const l=await jget('/api/workspaces');
  if(!l||!Array.isArray(l))return;
  wsList=l;renderTabs();
  if(!WS&&l.length){
    const h=decodeURIComponent((location.hash||'').replace('#',''));
    const cand=[h,PRESELECT,localStorage.getItem('ws')].find(n=>n&&l.some(w=>w.name===n));
    switchWs(cand||l[0].name);
  }
}
function render(s){
  if(!s){chip('phase','連線失敗');return;}
  if(s.error){chip('phase',s.error==='busy'?'…':s.error);return;}
  const c=s.config||{};
  curRound=s.round;
  const prev=lastState;lastState=s;
  if(editing)return; // 編輯模式:表格凍結,不讓輪詢蓋掉輸入中的內容(console 直播照常)
  // plan 動態更新提示(v4 動態樹的極簡版):版本跳了 → 變動的列亮一閃 + plan chip 閃
  let flashSet=null;
  if(prev&&!prev.error&&(s.plan_version||0)>(prev.plan_version||0)){
    planFlashUntil=Date.now()+1600;
    flashSet=new Set();
    const old=new Map((prev.plan||[]).map(t=>[t.order,t.task+'|'+(t.ref||'')]));
    (s.plan||[]).forEach(t=>{if(old.get(t.order)!==t.task+'|'+(t.ref||''))flashSet.add(t.order);});
  }
  const names={plan:'規劃期',exec:'執行期',done:'🏁 完成'};
  const ph=$('phase');ph.textContent=names[s.phase]||s.phase;ph.className='chip phase-'+s.phase;
  chip('round','round '+s.round);
  chip('flag','flag '+s.flag+' / >'+(c.flag_threshold??10),s.phase==='plan');
  chip('done','done '+s.done_count+' / ≥'+(c.done_threshold??3),s.phase==='exec');
  const ex=$('extra');
  ex.textContent='紅連跳 '+s.red_streak+' · 停滯 '+s.stall_rounds+' · plan v'+s.plan_version
    +((s.phase==='plan'&&s.plan_version>=10)?' ⚠ 可能震盪':'');
  ex.className='chip'+((s.phase==='plan'&&s.plan_version>=10)?' warn':'')
    +(Date.now()<planFlashUntil?' flash':'');
  const iss=(s.issues||[]).length;
  const ic=$('issues');ic.style.display=iss?'':'none';
  if(iss)ic.textContent='⚠ issues '+iss;
  renderIssues();
  chip('goalwarn','⚠ goal 已變更,建議 ⏪ 回規劃期重新收斂',!!s.goal_changed);
  const done=new Map((s.completed||[]).map(e=>[e.order,e]));
  const we=wsEntry();
  const canGoto=!RO&&we&&!we.running&&(s.phase==='exec'||s.phase==='done');
  const total=(s.plan||[]).length,doneCnt=done.size;
  chip('prog','任務 '+doneCnt+'/'+total,s.phase!=='plan'&&total>0);
  const rows=[];
  if(doneCnt)rows.push('<tr class=sum><td colspan=3>'+(showDone
    ?'▾ 已完成 '+doneCnt+' 條顯示中(點擊收合)'
    :'✔ 已完成 '+doneCnt+' 條(點擊展開)')+'</td></tr>');
  (s.plan||[]).forEach(t=>{
    const e=done.get(t.order);
    if(e&&!showDone)return; // 完成的預設收合,首屏留給進行中與待辦
    let st='·',cls='';
    if(e){st='✔ '+(e.human?'人工':e.sha.slice(0,8));cls='ok';}
    else if(s.phase!=='plan'&&t.order===s.current_order){st='→ 進行中';cls='cur';}
    const rs=(s.task_reset_counts||{})[String(t.order)];
    if(rs)st+=' ⟲'+rs;
    if(canGoto&&t.order!==s.current_order)
      st+='<button class=goto data-order="'+t.order+'" title="把進度設到這裡(往前跳會先跑 validate)">⏵</button>';
    if(expanded.has(t.order))cls+=' expand';
    rows.push('<tr class="'+cls+'" data-order="'+t.order+'"><td>'+t.order+'</td><td class=task><div class=tt>'
      +esc(t.task)+'</div>'+(t.ref?'<div class=ref>ref: '+esc(t.ref)+'</div>':'')+'</td><td class=st>'+st+'</td></tr>');
  });
  const html=rows.length?rows.join('')
    :'<tr><td colspan=3 style="color:#8b949e">(規劃期:計畫尚未建立)</td></tr>';
  if(html!==lastRowsKey){ // 內容沒變就不重繪:保住使用者的捲動位置
    lastRowsKey=html;
    const tw=$('tablewrap');const sp=tw.scrollTop;
    $('rows').innerHTML=html;
    tw.scrollTop=sp;
    document.querySelectorAll('#rows .goto').forEach(b=>{
      b.onclick=ev=>{ev.stopPropagation();gotoTask(+b.dataset.order);};});
    document.querySelectorAll('#rows tr.sum').forEach(r=>{
      r.onclick=()=>{showDone=!showDone;localStorage.setItem('showdone',showDone?'1':'0');
        lastRowsKey='';render(lastState);};});
    document.querySelectorAll('#rows td.task .tt').forEach(d=>{ // 點文字展開/收合 3 行 clamp
      d.onclick=()=>{const o=+d.closest('tr').dataset.order;
        if(expanded.has(o))expanded.delete(o);else expanded.add(o);
        lastRowsKey='';render(lastState);};});
    if(flashSet)flashSet.forEach(o=>{ // 本次更新有變動的列亮一閃
      const fr=document.querySelector('#rows tr[data-order="'+o+'"]');
      if(fr)fr.classList.add('flash');
    });
  }
  if(pendingCur){ // 切進 workspace 時自動捲到進行中任務
    const cr=document.querySelector('#rows tr.cur');
    if(cr)cr.scrollIntoView({block:'center'});
    if(cr||total)pendingCur=false;
  }
  updateJumpCur();
}
function addConsole(text,cls){
  const con=$('console');
  const atBottom=con.scrollTop+con.clientHeight>=con.scrollHeight-60;
  const sp=document.createElement('span');
  if(cls)sp.className=cls;
  sp.textContent=text;
  con.appendChild(sp);
  conLen+=text.length;
  while(conLen>300000&&con.childNodes.length>1){ // 凍結 console 長度:超過就丟最舊的
    conLen-=con.firstChild.textContent.length;
    con.removeChild(con.firstChild);
  }
  if(atBottom)con.scrollTop=con.scrollHeight; // 在底部就跟著 tail,永遠看得到最新 print
  $('jump').hidden=con.scrollTop+con.clientHeight>=con.scrollHeight-60;
}
async function pollState(){
  if(!WS)return;
  const my=WS;
  const s=await jget('/api/state?ws='+encodeURIComponent(my));
  if(my!==WS)return;
  render(s);
  const h=await jget('/api/history?ws='+encodeURIComponent(my)+'&offset='+hOff);
  if(my!==WS)return;
  if(h&&h.data){
    hOff=h.size;
    evLines=evLines.concat(h.data.split('\n').filter(Boolean)).slice(-10);
    $('events').textContent=evLines.join('\n');
    $('events').scrollTop=$('events').scrollHeight;
  }
}
async function pollTail(){
  if(WS&&curRound>0){
    const my=WS;
    if(sr===0){sr=curRound;off=-1;addConsole('── round '+sr+' ──\n','divider');} // -1=首抓直接 tail 尾段
    const j=await jget('/api/tail?ws='+encodeURIComponent(my)+'&round='+sr+'&offset='+off);
    if(my===WS){
      if(j){if(j.data)addConsole(j.data);off=j.size;}
      if(curRound>sr){sr=curRound;off=0;addConsole('\n── round '+sr+' ──\n','divider');}
    }
  }
  setTimeout(pollTail,600);
}
$('launchbtn').onclick=()=>{panelOpen=$('overlay').hidden;$('overlay').hidden=!panelOpen;if(panelOpen){loadConfig();pollJobs();}};
$('overlay').onclick=e=>{if(e.target.id==='overlay'){panelOpen=false;$('overlay').hidden=true;}};
async function loadConfig(){
  const c=await jget('/api/config');
  if(!c){$('f_msg').textContent='❌ 設定載入失敗,關掉面板再開一次';return;}
  if(c.error){$('f_msg').textContent='❌ '+c.error;return;}
  $('f_msg').textContent='';
  const rs=$('f_repo');rs.innerHTML='';
  (c.repos||[]).forEach(r=>rs.appendChild(new Option(r,r)));
  rs.appendChild(new Option('手動輸入…','__custom__'));
  rs.onchange=()=>{$('f_repo_custom').hidden=rs.value!=='__custom__';refreshRepoStatus();};
  $('f_repo_custom').onchange=refreshRepoStatus;
  rs.onchange();
  const as=$('f_agent');as.innerHTML='';
  (c.agent_cmds||[]).forEach((a,i)=>as.appendChild(new Option(a.label+' — '+a.cmd,i)));
  const vs=$('f_validate');vs.innerHTML='';
  (c.validate_cmds||[]).forEach((v,i)=>vs.appendChild(new Option(v.label+' — '+v.cmd,i)));
  vs.appendChild(new Option('手寫…','__custom__'));
  vs.onchange=()=>{$('f_validate_custom').hidden=vs.value!=='__custom__';};
  vs.onchange();
  const df=c.defaults||{}; // 預設值統一住 dashboard.config.json,表單只是覆蓋
  $('f_flag').value=df.flag_threshold??10;
  $('f_done').value=df.done_threshold??3;
  $('f_timeout').value=df.round_timeout??30;
}
function curRepo(){return $('f_repo').value==='__custom__'?$('f_repo_custom').value.trim():$('f_repo').value;}
async function refreshRepoStatus(){
  const repo=curRepo();
  if(!repo){$('repostatus').textContent='';return;}
  const s=await jget('/api/repo-status?repo='+encodeURIComponent(repo));
  if(!s||s.error){$('repostatus').textContent=s?('❌ '+s.error):'';return;}
  const mark=v=>v==='committed'?'✅ 已commit':v==='modified'?'⚠️ 改了沒commit':v==='untracked'?'⚠️ 沒commit':'❌ 缺';
  let line='goal.md '+mark(s.goal)+' · 工作樹 '+(s.tree_clean?'✅ 乾淨':'❌ 髒(preflight 會擋)');
  const w=wsList.find(x=>x.repo===repo);
  if(w)line+=' · ⚠️ workspace「'+w.name+'」已存在('+(w.phase||'?')+' r'+(w.round||0)+'),沿用會續跑舊進度';
  $('repostatus').textContent=line;
}
async function readFile(inp){
  if(!inp.files||!inp.files.length)return null;
  return await inp.files[0].text();
}
const PLAN_TPL=JSON.stringify([
  {order:1,task:'任務描述:寫到一個無前後文的工程師能直接動工,含驗收標準(DoD)',ref:'docs/analysis.md#段落(選填)'},
  {order:2,task:'第二個任務,依依賴順序排列',ref:null},
  {order:3,task:'ref 可整個省略'}
],null,2);
$('f_tpl').onclick=async()=>{
  try{await navigator.clipboard.writeText(PLAN_TPL);$('f_tpl').textContent='✅ 已複製';}
  catch(e){ // clipboard 被擋就直接填進去
    if(!$('f_plan').value.trim()){$('f_plan').value=PLAN_TPL;$('f_plan').dispatchEvent(new Event('input'));}
    $('f_tpl').textContent='已填入範本';
  }
  setTimeout(()=>{$('f_tpl').textContent='📋 複製範本';},1500);
};
function planLintErr(text){ // 與後端 validate_plan 一比一鏡射,貼上當下即時警告
  if(!text.trim())return '';
  let p;
  try{p=JSON.parse(text);}catch(e){return 'JSON 解析失敗:'+e.message;}
  if(!Array.isArray(p)||!p.length)return '必須是非空的物件陣列';
  const orders=[];
  for(let i=0;i<p.length;i++){
    const t=p[i];
    if(typeof t!=='object'||t===null||Array.isArray(t))return '第 '+i+' 項不是物件';
    const extra=Object.keys(t).filter(k=>!['order','task','ref'].includes(k));
    if(extra.length)return '第 '+i+' 項有未知欄位 '+extra.join(', ')+'(只允許 order/task/ref)';
    if(!Number.isInteger(t.order))return '第 '+i+' 項 order 必須是 int';
    if(typeof t.task!=='string'||!t.task.trim())return '第 '+i+' 項 task 必須是非空字串';
    if('ref' in t&&t.ref!==null&&typeof t.ref!=='string')return '第 '+i+' 項 ref 必須是字串或 null';
    orders.push(t.order);
  }
  const dup=[...new Set(orders.filter((o,i)=>orders.indexOf(o)!==i))];
  if(dup.length)return 'order 重複:'+dup.join(', ');
  const sorted=[...orders].sort((a,b)=>a-b);
  for(let i=0;i<sorted.length;i++)if(sorted[i]!==i+1)return 'order 必須從 1 連續遞增至 '+orders.length;
  return '';
}
$('f_plan').oninput=()=>{
  const v=$('f_plan').value;
  $('planopts').hidden=!v.trim();
  const err=planLintErr(v);
  $('f_plan').classList.toggle('bad',!!err);
  $('planerr').hidden=!err;
  $('planerr').textContent=err;
};
$('f_go').onclick=async()=>{
  const lintErr=planLintErr($('f_plan').value);
  if(lintErr){$('f_msg').textContent='❌ plan.json 格式不對:'+lintErr;return;}
  const repo=curRepo();
  const body={repo,name:$('f_name').value.trim(),agent_idx:+$('f_agent').value,
    flag_threshold:+$('f_flag').value,done_threshold:+$('f_done').value,round_timeout:+$('f_timeout').value,
    reset_state:$('f_reset').checked,new_branch:$('f_branch').checked,plan_json:$('f_plan').value,
    start_phase:(document.querySelector('input[name=sp]:checked')||{}).value||'plan'};
  const g=await readFile($('f_goalfile'));
  if(g!==null)body.goal_content=g;  // 啟動時自動 commit,不用另外按匯入
  if($('f_validate').value==='__custom__')body.validate_custom=$('f_validate_custom').value.trim();
  else body.validate_idx=+$('f_validate').value;
  $('f_msg').textContent='啟動中…';
  const r=await jpost('/api/launch',body);
  $('f_msg').textContent=r.error?('❌ '+r.error):('✅ 已啟動 '+r.name+'(pid '+r.pid+')');
  if(!r.error){
    $('f_goalfile').value='';$('f_plan').value='';$('planopts').hidden=true;refreshRepoStatus();
    panelOpen=false;$('overlay').hidden=true;      // 啟動成功 → 關閉彈窗
    switchWs(r.name);setTimeout(()=>switchWs(r.name),50);  // 跳到剛啟動的 workspace 看直播
  }
  setTimeout(()=>{pollJobs();pollWorkspaces();},600);
};
async function pollJobs(){
  if(!panelOpen)return;
  const l=await jget('/api/jobs');if(!l)return;
  const box=$('jobs');box.innerHTML='';
  if(!l.length){box.textContent='(無)';return;}
  l.forEach(j=>{
    const d=document.createElement('div');d.className='job';
    d.innerHTML=(j.alive?'<button>⏹ 停止</button>':'')
      +'<b>'+esc(j.name)+'</b> · pid '+j.pid+' · '+(j.alive?'🟢 執行中':'⚪ 已結束 rc='+j.rc)
      +'<div style="color:#8b949e">'+esc(j.repo)+'</div><pre></pre>';
    d.querySelector('pre').textContent=j.tail||'';
    const b=d.querySelector('button');
    if(b)b.onclick=async()=>{b.disabled=true;await jpost('/api/stop',{name:j.name});setTimeout(pollJobs,800);};
    box.appendChild(d);
  });
}
setInterval(pollState,1000);
setInterval(pollWorkspaces,3000);
setInterval(pollJobs,2000);
pollWorkspaces();pollTail();loadConfig();
</script></body></html>
"""


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
    preselect = ""
    readonly = False

    def log_message(self, *a):
        pass

    def _out(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, msg, code=400):
        self._out(code, json.dumps({"error": msg}, ensure_ascii=False))

    def _ws_dir(self, q):
        name = q.get("ws", [""])[0]
        valid = {d.name for d in ROOT.iterdir() if d.is_dir()} if ROOT.is_dir() else set()
        if name not in valid:
            self._err(f"未知 workspace:{name or '(空)'},可用:{sorted(valid)}")
            return None
        return ROOT / name

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                self._out(200, PAGE.replace("%%PRESELECT%%", self.preselect)
                          .replace("%%RO%%", "1" if self.readonly else ""), "text/html; charset=utf-8")
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
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
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
        stop_all_jobs()
        print("dashboard 已關閉。", flush=True)


if __name__ == "__main__":
    main()
