#!/usr/bin/env python3
"""loop-agent-lite — markdown/JSON 規劃 + 無窮迴圈的極簡 agent 迴圈。

雙層真相:
- 協調層(python-owned truth):goal、初步規劃書、state.json(含 plan)。
  agent 只能透過 work.py 的命令寫入;直接改檔會被偵測、還原、該輪作廢。
- 程式碼層(agent-owned):agent 直接在 repo 寫 code、自己 commit;
  程式不 autocommit、不清工作區,爛尾留給下一輪 agent 判斷。

收斂機制(共識 AND gate):
- 規劃期:agent call plan-ok 且該輪無任何異動 → flag+1;call create-plan(不論成敗)
  或有任何異動 → flag 歸零;flag > 10 → 執行期。
- 執行期:per-task 內圈——agent call done(task id 正確)且 HEAD 沒動、工作樹乾淨、
  驗證綠 → done+1;有異動/驗證紅 → done 歸零;done ≥ threshold(預設 3)→ 派下一個任務。

防線(全部機械、可關可調):
- preflight:validate 必須綠、工作樹必須乾淨、goal/初步規劃書必須已 commit,否則第一行就擋。
- 紅燈連跳 N 輪(預設 20)→ git reset --hard 回最後綠點。
- HEAD 停滯 N 輪(預設 300)→ 同上。reset 後依「task 完成 sha」回退任務指標,不用一個一個退。
- 同一任務 reset 次數達上限停機:預設關,開啟時預設 100 次。
"""

import argparse
import atexit
import fcntl
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE_ROOT = Path(os.environ.get("LOOP_AGENT_WORKSPACE_ROOT", HERE / "workspace")).expanduser().resolve()

# ===== 預設值(全部可用命令列覆蓋) =====
AGENT_CMD = ["claude", "-p"]          # prompt 走 stdin;公司 CLI 用 --agent-cmd 覆蓋
VALIDATE_CMD = ["mvn", "-q", "compile"]
FLAG_THRESHOLD = 10                    # 規劃期:flag > 此值 → 收斂
DONE_THRESHOLD = 3                     # 執行期:done ≥ 此值 → 任務完成(建議 3–5)
RED_LIMIT = 20                         # 連續驗證紅 N 輪 → reset
STALL_LIMIT = 300                      # HEAD 連續 N 輪沒前進 → reset
STUCK_STOP_COUNT = 100                 # --stuck-stop 開啟時,同一任務 reset 達此次數停機
ROUND_TIMEOUT_MIN = 30                 # 單輪 agent 上限(分鐘);0=不限
VALIDATE_TIMEOUT_SEC = 120             # 啟動前/每輪驗證上限(秒);避免 validator 永久卡住
VALIDATE_TAIL = 50                     # 驗證失敗餵給下一輪的輸出尾行數
TASK_LIST_TRUNC = 80                   # prompt 任務總覽單行截斷長度
CONSOLE_MAX_BYTES = 5 * 1024 * 1024   # console.log 單檔 5 MiB
CONSOLE_BACKUPS = 3                    # 保留 console.log.1～.3

_CONSOLE_PATH = None
_CONSOLE_LOCK = threading.Lock()


def now_ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def configure_console(path: Path) -> None:
    """將 loop 與 agent 的所有輸出追加到 workspace 共用 console。"""
    global _CONSOLE_PATH
    _CONSOLE_PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    append_console(path, f"\n[{now_ts()}] ━━━ 新的 loop session ━━━")


def append_console(path: Path, line: str, *, max_bytes: int = CONSOLE_MAX_BYTES,
                   backups: int = CONSOLE_BACKUPS) -> None:
    """跨 process 鎖定後追加 console；超過大小時輪替 .1～.N。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (line + "\n").encode("utf-8")
    lock_path = path.with_name(f".{path.name}.lock")
    with _CONSOLE_LOCK, open(lock_path, "a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            current_size = path.stat().st_size if path.exists() else 0
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
            with open(path, "ab") as console:
                console.write(encoded)
                console.flush()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _console_line(line: str) -> None:
    print(line, flush=True)
    if _CONSOLE_PATH is None:
        return
    append_console(_CONSOLE_PATH, line)


def log(msg: str) -> None:
    lines = str(msg).splitlines() or [""]
    for line in lines:
        _console_line(f"[{now_ts()}] {line}")


def agent_log(msg: str) -> None:
    _console_line(f"[{now_ts()}] 🤖 Agent｜{msg}")


def fail(msg: str):
    log(f"⛔ 流程停止｜{msg}")
    # 原因已同步寫到 stdout 與 console.log；只回 exit code，避免 stderr 再印一次相同訊息。
    raise SystemExit(1)


def sh(args, cwd, check=True):
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"命令失敗 rc={r.returncode}: {args}\n{r.stdout}\n{r.stderr}")
    return r


def git(repo, *args, check=True):
    return sh(["git", *args], cwd=repo, check=check)


def head_sha(repo) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def is_dirty(repo) -> bool:
    return bool(git(repo, "status", "--porcelain").stdout.strip())


def is_ancestor(repo, sha, of_sha) -> bool:
    """sha 是否為 of_sha 的祖先(含相等)。"""
    return git(repo, "merge-base", "--is-ancestor", sha, of_sha, check=False).returncode == 0


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
        r = subprocess.run(["git", "cat-file", "blob", f"{green}:{rel}"],
                           cwd=str(repo), capture_output=True)
        if r.returncode != 0 or r.stdout != snap.read_bytes():
            return False
    return True


def tracked_in_head(repo, rel_path) -> bool:
    return git(repo, "cat-file", "-e", f"HEAD:{rel_path}", check=False).returncode == 0


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子寫:同目錄 tmp → fsync → os.replace。避免 SIGKILL/磁碟滿留下半截檔。
    唯一真相(state.json)不能寫到一半——這是跑整夜必備的 correctness 防線。"""
    # tmp 名帶 uuid:同 process 多執行緒(dashboard ThreadingHTTPServer)並發寫同一 state.json
    # 時不再共用 tmp,避免互相 truncate 或 replace 後對方拿到 FileNotFoundError(#3)。
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Workspace:
    """workspace/<name>/ 底下所有 python-owned 檔案的單一寫入點。"""

    def __init__(self, name: str):
        self.dir = WORKSPACE_ROOT / name
        (self.dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.dir / "prompts").mkdir(parents=True, exist_ok=True)
        (self.dir / "snapshots").mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.history = self.dir / "history.log"
        self._state_hash = None  # 本 session 內偵測 agent 直接改 state.json 用

    # ---- state.json ----
    def fresh_state(self):
        return {
            "phase": "plan", "round": 0, "flag": 0,
            "plan": [], "plan_version": 0,
            "current_order": 0, "done_count": 0,
            "completed": [],            # [{order, sha, round}]
            "last_green_sha": None,
            "red_streak": 0, "stall_rounds": 0,
            "task_reset_counts": {},    # {order(str): 次數}
            "notes": [],
            "issues": [],               # agent 用 work.py issue 回報,給人類看,不影響計數
        }

    def load_state(self):
        if self.state_path.exists():
            data = self.state_path.read_bytes()
            self._state_hash = sha256_bytes(data)  # 停機期間人工改檔視為合法,resume 直接信任
            return json.loads(data)
        return self.fresh_state()

    def save_state(self, state):
        data = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        atomic_write_bytes(self.state_path, data)
        self._state_hash = sha256_bytes(data)

    def state_tampered(self) -> bool:
        """回傳 True 表示 agent 在本輪繞過 work.py 直接改了 state.json。"""
        if self._state_hash is None or not self.state_path.exists():
            return not self.state_path.exists() and self._state_hash is not None
        return sha256_bytes(self.state_path.read_bytes()) != self._state_hash

    # ---- 輪間訊號(work.py 寫、loop 讀) ----
    def clear_signals(self):
        for f in ("called_create_plan", "pending_plan.json", "signal_plan_ok", "signal_done"):
            (self.dir / f).unlink(missing_ok=True)

    def signal(self, name) -> bool:
        return (self.dir / name).exists()

    def take_pending_plan(self):
        p = self.dir / "pending_plan.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    def write_dispatch(self, phase, task_id=""):
        (self.dir / "phase").write_text(phase, encoding="utf-8")
        (self.dir / "current_task").write_text(task_id, encoding="utf-8")

    # ---- 受保護檔案快照(goal / 初步規劃書) ----
    def snapshot_protected(self, repo, rel_paths):
        for rel in rel_paths:
            (self.dir / "snapshots" / rel.replace("/", "__")).write_bytes((repo / rel).read_bytes())

    def protected_changed(self, repo, rel_paths):
        """純偵測:回傳被刪或被改的受保護檔案清單(空 = 沒人亂動)。不寫回。"""
        hit = []
        for rel in rel_paths:
            snap = (self.dir / "snapshots" / rel.replace("/", "__")).read_bytes()
            target = repo / rel
            if (not target.exists()) or target.read_bytes() != snap:
                hit.append(rel)
        return hit

    def restore_protected(self, repo, rel_paths):
        """把受保護檔案寫回快照(供 reset 後補正,green sha 版本理應相同故多為 no-op)。
        寫回前先建父目錄:green 不含該子目錄時 write_bytes 會 FileNotFoundError(#1)。"""
        for rel in rel_paths:
            snap = (self.dir / "snapshots" / rel.replace("/", "__")).read_bytes()
            target = repo / rel
            if (not target.exists()) or target.read_bytes() != snap:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(snap)


def run_agent(cmd, prompt_path, repo, env, log_path, timeout_secs, on_started=None):
    """跑一輪 agent:prompt 從檔案餵 stdin(避免大 payload 塞管線),
    stdout/stderr 逐行同步印上 console 並落 log 檔。
    逾時 SIGKILL 整個 process group(start_new_session 保證殺得到子孫)。
    回傳 (rc, 秒數, 是否逾時)。"""
    t0 = time.monotonic()
    flag = {"timed_out": False}
    with open(log_path, "w", encoding="utf-8") as lf, open(prompt_path, "rb") as pin:
        p = subprocess.Popen(cmd, cwd=str(repo), env=env, stdin=pin,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True)
        if on_started:
            on_started(p.pid)

        def _kill():
            if p.poll() is None:
                flag["timed_out"] = True
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        timer = threading.Timer(timeout_secs, _kill) if timeout_secs else None
        if timer:
            timer.start()
        try:
            for raw in p.stdout:
                line = raw.decode("utf-8", errors="replace")
                agent_log(line.rstrip("\n"))
                lf.write(line)
                lf.flush()  # 逐行落盤,dashboard 才 tail 得到即時輸出
        except KeyboardInterrupt:
            # 人(或 dashboard)停掉 loop:把跑到一半的 agent 整個 process group 帶走,不留孤兒
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            raise
        finally:
            p.wait()
            if timer:
                timer.cancel()
    return p.returncode, time.monotonic() - t0, flag["timed_out"]


def notify(cmd, status, name):
    """終態通知(佔位符 {status} {name}):失敗只記 warning,永不擋主流程。"""
    if not cmd:
        return
    try:
        subprocess.run(shlex.split(cmd.replace("{status}", status).replace("{name}", name)),
                       capture_output=True, timeout=15)
        log(f"🔔 notify 已送出:{status}")
    except Exception as e:  # noqa: BLE001 — 通知永不擋主流程
        log(f"⚠ notify 失敗(不影響結果):{e}")


def run_validate(cmd, repo, timeout_secs=VALIDATE_TIMEOUT_SEC):
    """執行正式 validator；逾時或中斷時清掉整個 validator process group。"""
    try:
        p = subprocess.Popen(cmd, cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, start_new_session=True)
    except FileNotFoundError:
        return False, f"找不到 Validate 命令：{cmd[0]}", False
    timed_out = False
    try:
        out, _ = p.communicate(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        out, _ = p.communicate()
    except KeyboardInterrupt:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        p.wait()
        raise
    out = (out or "").strip()
    tail = "\n".join(out.splitlines()[-VALIDATE_TAIL:])
    if timed_out:
        tail = (f"Validate 執行超過 {timeout_secs:g} 秒，已終止" + (f"\n{tail}" if tail else ""))
    return p.returncode == 0 and not timed_out, tail, timed_out


def render_task_list(state):
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
    text = tpl_path.read_text(encoding="utf-8")
    for k, v in mapping.items():
        text = text.replace(f"<<{k}>>", v)
    return text


def main():
    ap = argparse.ArgumentParser(description="loop-agent-lite:規劃/執行雙段共識迴圈")
    ap.add_argument("--repo", required=True, help="target code repo(git、乾淨、validate 綠)")
    ap.add_argument("--name", default=None, help="workspace 名稱(預設=repo 目錄名)")
    ap.add_argument("--goal", default="goal.md", help="goal 檔(相對 repo,須已 commit)")
    ap.add_argument("--plan-doc", default="", help="選配:參考分析文件(相對 repo);提供的話須已 commit 且受保護")
    ap.add_argument("--agent-cmd", default=None, help="agent CLI 命令(整串;prompt 走 stdin)")
    ap.add_argument("--validate-cmd", default=None, help="驗證命令(預設 mvn -q compile)")
    ap.add_argument("--flag-threshold", type=int, default=FLAG_THRESHOLD)
    ap.add_argument("--done-threshold", type=int, default=DONE_THRESHOLD)
    ap.add_argument("--red-limit", type=int, default=RED_LIMIT)
    ap.add_argument("--stall-limit", type=int, default=STALL_LIMIT)
    ap.add_argument("--stuck-stop", action="store_true", help="同一任務 reset 達上限即停機(預設關)")
    ap.add_argument("--stuck-stop-count", type=int, default=STUCK_STOP_COUNT)
    ap.add_argument("--round-timeout", type=float, default=ROUND_TIMEOUT_MIN,
                    help="單輪 agent 上限(分鐘;0=不限,預設 30)")
    ap.add_argument("--validate-timeout", type=float, default=VALIDATE_TIMEOUT_SEC,
                    help="啟動前與每輪 Validate 上限(秒;必須 >0,預設 120)")
    ap.add_argument("--notify-cmd", default="", help="終態通知命令,佔位符 {status} {name}(空=不通知)")
    ap.add_argument("--import-plan", default="", help="匯入 plan.json(重置 state;等同 dashboard 貼上匯入)")
    ap.add_argument("--consume-import-plan", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--start-phase", choices=("plan", "exec"), default="plan",
                    help="搭配 --import-plan:從規劃期(讓 agent 補完)或直接執行期開跑")
    ap.add_argument("--max-rounds", type=int, default=0, help="總輪數上限;0=不限(測試用)")
    ap.add_argument("--reset-state", action="store_true", help="清掉 workspace state 從頭跑")
    args = ap.parse_args()
    if args.validate_timeout <= 0:
        ap.error("--validate-timeout 必須 > 0")

    repo = Path(args.repo).resolve()
    agent_cmd = shlex.split(args.agent_cmd) if args.agent_cmd else AGENT_CMD
    validate_cmd = shlex.split(args.validate_cmd) if args.validate_cmd else VALIDATE_CMD
    protected = [args.goal] + ([args.plan_doc] if args.plan_doc else [])
    plan_doc_display = str(repo / args.plan_doc) if args.plan_doc else "(未提供——以 goal、現有計畫與實際程式碼為準)"

    # preflight 失敗也必須出現在 dashboard 的完整 console。舊流程直到所有 git
    # 檢查通過後才設定 console，導致「pid 出現後立刻停止」卻完全看不到原因。
    ws = Workspace(args.name or repo.name)
    configure_console(ws.dir / "console.log")
    startup_ready = ws.dir / "startup_ready.json"
    startup_ready.unlink(missing_ok=True)

    # ===== preflight:第一行就擋,不合格不進迴圈 =====
    if git(repo, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        fail(f"preflight：{repo} 不是 git repo")
    if git(repo, "rev-parse", "HEAD", check=False).returncode != 0:
        fail(f"preflight：{repo} 沒有任何 commit")
    if is_dirty(repo):
        fail("preflight：工作樹不乾淨。之後的 reset --hard 會吃掉你的 WIP，先 commit 或 stash 再來")
    for rel in protected:
        if not tracked_in_head(repo, rel):
            fail(f"preflight：{rel} 不在 HEAD 裡。流程是：模板產初版 → 你審 → commit → 才 run loop")

    log(f"🚀 Loop 啟動｜workspace={ws.dir.name}｜repo={repo}")
    if args.reset_state:
        # Reset 必須是交易式的：先在記憶體建立全新 state，等所有 preflight（尤其 validate）
        # 通過後才由下方第一個 save_state 原子取代舊檔。若驗證失敗，舊 state 仍完整可讀，
        # 不會留下只有 workspace 目錄、沒有 state.json 的幽靈分頁。
        state = ws.fresh_state()
        log("🧹 準備重置既有 state｜啟動前檢查通過後才會正式清除舊進度")
    else:
        state = ws.load_state()

    # repo identity fail-closed:workspace 只按 name 載入,若既有 state 綁的是別的 repo,
    # 續跑會拿別人的 plan/completed/last_green_sha 去 reset --hard——寧可停也不帶病前進。
    bound_repo = (state.get("config") or {}).get("repo")
    if bound_repo and Path(bound_repo).resolve() != repo and not (args.reset_state or args.import_plan):
        fail(f"workspace '{ws.dir.name}' 綁定的是 {bound_repo},但這次 --repo 是 {repo}。"
             f"同名 workspace 指到不同 repo 會用錯 plan/SHA——換個 --name,或加 --reset-state 重來。")

    # 選配:CLI 匯入 plan.json(重置 state,選起跑階段)——dashboard 匯入的 CLI 等價
    if args.import_plan:
        from work import validate_plan
        try:
            plan_obj = json.loads(Path(args.import_plan).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            fail(f"--import-plan 讀取/解析失敗:{e}")
        normalized, errs = validate_plan(plan_obj)
        if errs:
            fail("plan.json 校驗未過:\n- " + "\n- ".join(errs))
        state = ws.fresh_state()
        state["plan"] = normalized
        state["plan_version"] = 1
        state["phase"] = args.start_phase
        if args.start_phase == "exec":
            state["current_order"] = normalized[0]["order"]
        log(f"📝 匯入計畫｜{len(normalized)} 條｜從 {'規劃期' if args.start_phase == 'plan' else '執行期'} 開始")

    fresh_start = bool(args.reset_state or args.import_plan)
    # 一般 resume 要先拍快照供舊綠點驗證；reset/import 延後到 Validate 綠後，
    # 失敗的 staged 啟動就不會改掉舊 state 對應的 protected snapshot。
    if not fresh_start:
        ws.snapshot_protected(repo, protected)

    # preflight:validate。綠點錨定 fail-closed(#1)——resume 不能只看 last_green_sha 非空,
    # 否則舊 green 若已不存在/非 HEAD 祖先/protected 已分歧,reset 回去會製造髒工作樹或錯版 goal。
    log(f"🔎 啟動前檢查｜執行驗證：{shlex.join(validate_cmd)}")
    ok, vtail, validate_timed_out = run_validate(validate_cmd, repo, args.validate_timeout)
    # validate 本身若修改 tracked/untracked(non-ignored)檔案,不論 rc 綠紅都不能放行。否則 rc=0
    # 會落出下列分支直接續跑;rc!=0 + 舊 green 合法也會帶髒工作樹進 loop,把 validator 的副作用
    # 誤判成 agent 異動。preflight 起點必須在 validate 前後都乾淨。
    if is_dirty(repo):
        fail(f"啟動前驗證 `{shlex.join(validate_cmd)}` 執行後弄髒工作樹——"
             "validate 必須只產生 ignored build artifacts,不能修改 tracked/untracked 原始碼。"
             f"輸出尾段:\n{vtail}")
    if ok:
        # 當前 HEAD 綠且乾淨:它就是最新、protected 必然與啟動快照一致的錨點,直接錨在這。
        # 同時修掉「停機期間人改 goal、舊 green 已分歧」——丟棄舊 green,不沿用過時錨點。
        state["last_green_sha"] = head_sha(repo)
        log(f"✅ 啟動前檢查完成｜驗證通過｜綠點 {state['last_green_sha'][:8]}")
    elif not ok:
        # 當前 HEAD 紅:必須有「通過驗證」的舊綠點才放行,否則沒有可信起點,fail-closed 停機。
        green = state["last_green_sha"]
        if green_anchor_valid(repo, green, ws.dir / "snapshots", protected):
            log(f"⚠️ 啟動驗證失敗｜沿用已確認綠點 {green[:8]} 繼續修復")
            if vtail:
                log(f"驗證錯誤尾段：\n{vtail}")
            state["notes"].append(f"❌ 啟動時 `{' '.join(validate_cmd)}` 就是紅的,"
                                  f"先把它修綠再繼續往下做。輸出尾段:\n```\n{vtail}\n```")
        else:
            why = ("沒有綠點可錨定" if not green else
                   f"綠點 {green[:8]} 未通過驗證(不存在/非 HEAD 祖先/protected 與現況分歧)")
            timeout_note = f"（逾時 {args.validate_timeout:g} 秒）" if validate_timed_out else ""
            fail(f"啟動驗證 `{shlex.join(validate_cmd)}` 失敗{timeout_note}，{why}——"
                 f"起點必須是可信綠點。先把工作樹修到 validate 綠再 resume。輸出尾段:\n{vtail}")

    if fresh_start:
        ws.snapshot_protected(repo, protected)

    # goal 變更偵測:停機期間人改 goal 是合法的,但既有計畫是舊 goal 收斂的——大聲提醒
    goal_hash = sha256_bytes((repo / args.goal).read_bytes())
    if state.get("goal_hash") and state["goal_hash"] != goal_hash and state.get("plan"):
        state["goal_changed"] = True
        log("⚠ goal 已變更,但計畫是舊 goal 收斂的——建議回規劃期重新收斂(dashboard ⏪);刻意如此可忽略")
        state["notes"].append("⚠ goal 內容已被人類更新,現有計畫可能過期。以新 goal 為準檢視你的任務;"
                              "若計畫明顯對不上,用 issue 回報。")
    state["goal_hash"] = goal_hash
    # dashboard 靠 config 做 workspace 掃描與一鍵 run(agent_cmd 會再對 config 白名單驗過才准跑)
    state["config"] = {"flag_threshold": args.flag_threshold, "done_threshold": args.done_threshold,
                       "red_limit": args.red_limit, "stall_limit": args.stall_limit,
                       "round_timeout": args.round_timeout,
                       "validate_timeout": args.validate_timeout,
                       "repo": str(repo), "agent_cmd": shlex.join(agent_cmd),
                       "validate_cmd": shlex.join(validate_cmd),
                       "goal": args.goal, "plan_doc": args.plan_doc}
    state["loop"] = {"pid": os.getpid(), "started_at": datetime.now().isoformat(timespec="seconds")}

    def _mark_stopped():
        state["loop"]["pid"] = None  # 正常/Ctrl-C 退出都清 pid;被 SIGKILL 留殘值,由 dashboard ps 檢查兜底
        ws.save_state(state)
    atexit.register(_mark_stopped)
    # preflight 已通過：此刻才原子提交 reset/import 的全新 state。Agent 尚未啟動時若失敗，
    # state 仍是完整、可再次 Run 的 stopped workspace，不會是半套 import state。
    ws.save_state(state)
    if args.reset_state or args.import_plan:
        (ws.dir / "pending_issues").unlink(missing_ok=True)
    if args.import_plan and getattr(args, "consume_import_plan", False):
        Path(args.import_plan).unlink(missing_ok=True)

    startup_marked = False

    def mark_startup_ready(_agent_pid):
        nonlocal startup_marked
        if startup_marked:
            return
        atomic_write_bytes(startup_ready, json.dumps({"pid": os.getpid()}).encode("utf-8"))
        startup_marked = True
        log("🟢 啟動完成｜preflight、Validate 與 Agent spawn 均成功")

    work_py = HERE / "work.py"
    # Prompt 中的 coordinator 命令會交給另一個 CLI agent 執行；使用絕對路徑，
    # 避免不同使用者、IDE 或非互動 shell 的 PATH 指到另一套 Python。
    py = shlex.quote(str(Path(sys.executable).expanduser().resolve()))
    create_cmd = f"{py} {shlex.quote(str(work_py))} create-plan"
    planok_cmd = f"{py} {shlex.quote(str(work_py))} plan-ok"
    issue_cmd = f"{py} {shlex.quote(str(work_py))} issue"
    env = {**os.environ, "LOOP_WS": str(ws.dir)}

    phase_name = "規劃期" if state["phase"] == "plan" else "執行期"
    log(f"📍 恢復進度｜階段：{phase_name}｜已完成 round {state['round']}")
    log(f"⚙️ 執行設定｜Agent：{shlex.join(agent_cmd)}｜驗證：{shlex.join(validate_cmd)}")
    log(f"⚙️ 收斂門檻｜flag>{args.flag_threshold}｜done≥{args.done_threshold}｜red-limit={args.red_limit}｜"
        f"stall-limit={args.stall_limit}  stuck-stop={'on(' + str(args.stuck_stop_count) + ')' if args.stuck_stop else 'off'}  "
        f"round-timeout={args.round_timeout:g}min  validate-timeout={args.validate_timeout:g}s")

    goal_text = (repo / args.goal).read_text(encoding="utf-8")

    while state["phase"] != "done":
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
        if not (repo / args.goal).exists():
            ws.save_state(state)
            notify(args.notify_cmd, "goal_missing", ws.dir.name)
            fail(f"{args.goal} 不存在（每輪啟動前檢查）——請補回並 commit 後再啟動")

        # 派工資訊落地(work.py 靠這兩個檔做當場驗:phase 凍結 + task id 核對)
        cur_task = next((t for t in state["plan"] if t["order"] == state["current_order"]), None)
        if phase == "exec" and cur_task is None:
            ws.save_state(state)
            fail(f"執行期找不到 current_order={state['current_order']} 的任務"
                 f"（plan {len(state['plan'])} 條）——state 不合法，停機交由人員確認")
        task_id = f"task-{state['current_order']}" if (phase == "exec" and cur_task) else ""
        ws.write_dispatch(phase, task_id)
        ws.clear_signals()
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
            done_cmd = f"{py} {shlex.quote(str(work_py))} done {task_id}"
            prompt = build_prompt(HERE / "prompts" / "exec.md", {
                "GOAL": goal_text.strip(),
                "PLAN_DOC": plan_doc_display,
                "TASK_ID": task_id,
                "TASK_TEXT": cur_task["task"],
                "TASK_REF": cur_task.get("ref") or "(無)",
                "TASK_LIST": render_task_list(state),
                "DONE_CMD": done_cmd,
                "ISSUE_CMD": issue_cmd,
                "VALIDATE_CMD": " ".join(validate_cmd),
                "NOTES": notes_text,
            })
        prompt_path = ws.dir / "prompts" / f"round-{rnd:04d}.md"
        prompt_path.write_text(prompt, encoding="utf-8")
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
        rc, secs, timed_out = run_agent(agent_cmd, prompt_path, repo, env,
                                        ws.dir / "logs" / f"round-{rnd:04d}.log",
                                        args.round_timeout * 60, on_started=mark_startup_ready)
        log(f"🤖 Agent 結束｜exit code={rc}｜耗時 {secs:.0f} 秒" + "｜超時，已強制終止" * timed_out)
        if timed_out:
            state["notes"].append(f"⚠️ 上一輪 agent 超過 {args.round_timeout:g} 分鐘被強制終止,"
                                  "工作可能做到一半——工作區殘留照「收拾現場」步驟判斷。")

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
            # reset --hard 只清 repo,pending_plan.json 在 workspace 目錄清不到,必須顯式丟棄——
            # 否則規劃期會把「同一輪偷改 goal + create-plan」提交的髒 plan 當成真相收進去。
            ws.clear_signals()
            log(f"⚠️ 本輪作廢｜偵測到不允許的變更：{', '.join(tampered)}｜相關 signal 已丟棄")

        head_after = head_sha(repo)
        dirty = is_dirty(repo)
        changed = dirty or (head_after != head_before)
        state["stall_rounds"] = 0 if head_after != head_before else state["stall_rounds"] + 1

        event = ""
        if phase == "plan":
            # create-plan 只要被 call(不論成敗)就歸零 —— fail-closed
            if ws.signal("called_create_plan"):
                log("📨 Agent 指令｜create-plan（提交新計畫）")
                state["flag"] = 0
                pending = ws.take_pending_plan()
                if pending is not None:
                    state["plan"] = pending
                    state["plan_version"] += 1
                    event = f"📝 計畫已更新｜v{state['plan_version']}｜共 {len(pending)} 條任務"
                    log(event)
                else:
                    event = "❌ create-plan 校驗未通過｜保留原計畫"
                    log(event)
            elif tampered or changed:  # 規劃期 repo 異動已在上面整輪 reset,這裡只確保 flag 歸零
                state["flag"] = 0
            elif ws.signal("signal_plan_ok"):
                log("📨 Agent 指令｜plan-ok（確認目前計畫）")
                if state["plan"]:
                    state["flag"] += 1
                    log(f"✅ 規劃共識累計｜flag={state['flag']}｜門檻 > {args.flag_threshold}")
                else:
                    state["notes"].append("plan 仍為空,plan-ok 不計數。請先 create-plan。")
                    log("⚠️ plan-ok 未計數｜目前計畫為空，請先 create-plan")
            elif not tampered:
                log("ℹ️ Agent 本輪未送出 create-plan 或 plan-ok｜規劃共識不增加")
            validate_note = "-"
            if state["flag"] > args.flag_threshold:
                state["phase"] = "exec"
                state["flag"] = 0
                state["current_order"] = 1
                state["done_count"] = 0
                # 規劃期 HEAD 幾乎不動,停滯/紅燈計數是髒的,不歸零會把殘值帶進執行期誤觸 reset
                state["stall_rounds"] = 0
                state["red_streak"] = 0
                state["goal_changed"] = False  # 計畫已在(可能更新過的)goal 下重新收斂
                event = f"✅ 規劃收斂(plan v{state['plan_version']},{len(state['plan'])} 條)→ 執行期"
                log(event)
        else:
            if ws.signal("signal_done"):
                log(f"📨 Agent 指令｜done {task_id}（回報任務完成）")
            else:
                log(f"ℹ️ Agent 本輪未送出 done {task_id}")
            if ws.signal("called_create_plan"):
                log("📨 Agent 指令｜create-plan｜執行期計畫已凍結，將忽略此指令")
            log(f"🧪 執行驗證｜命令：{shlex.join(validate_cmd)}")
            ok, vtail, validate_timed_out = run_validate(validate_cmd, repo, args.validate_timeout)
            validate_note = "PASS" if ok else "FAIL"
            if ok:
                log("✅ 驗證通過")
                state["red_streak"] = 0
                if not dirty:
                    state["last_green_sha"] = head_after
            else:
                timeout_note = f"｜逾時 {args.validate_timeout:g} 秒" if validate_timed_out else ""
                log(f"❌ 驗證失敗{timeout_note}｜紅燈連續 {state['red_streak'] + 1} 輪")
                if vtail:
                    log(f"驗證錯誤尾段：\n{vtail}")
                state["red_streak"] += 1
                state["done_count"] = 0
                state["notes"].append(
                    f"❌ 上一輪結束後 `{' '.join(validate_cmd)}` 失敗。先判斷是前一個 commit 沒做好、"
                    f"還是前一個 agent 沒做完,把它修好讓驗證過了再繼續。輸出尾段:\n```\n{vtail}\n```")
            if ws.signal("called_create_plan"):
                state["notes"].append("執行期計畫已凍結,create-plan 被忽略。任務本身有問題請在 log/commit 說明,交人處理。")
            if tampered or changed:
                state["done_count"] = 0
                reason = "本輪被判定作廢" if tampered else "偵測到程式碼或 commit 變更，等待下一輪確認"
                log(f"↩️ done 共識歸零｜{reason}")
            elif ws.signal("signal_done") and ok:
                state["done_count"] += 1
                log(f"✅ done 共識累計｜{state['done_count']} / {args.done_threshold}")

            # ---- 任務完成判定 ----
            if state["done_count"] >= args.done_threshold:
                state["completed"].append({"order": state["current_order"], "sha": head_after, "round": rnd})
                event = f"✅ {task_id} 完成(sha {head_after[:8]},{state['done_count']} 輪共識)"
                log(event)
                state["done_count"] = 0
                nxt = next((t["order"] for t in state["plan"]
                            if t["order"] > state["current_order"]), None)
                if nxt is None:
                    state["phase"] = "done"
                else:
                    state["current_order"] = nxt

            # ---- reset 防線 ----
            reset_reason = ""
            if state["phase"] == "exec":
                if state["red_streak"] >= args.red_limit:
                    reset_reason = f"驗證連紅 {state['red_streak']} 輪"
                elif state["stall_rounds"] >= args.stall_limit:
                    reset_reason = f"HEAD 停滯 {state['stall_rounds']} 輪"
            if reset_reason:
                green = state["last_green_sha"]
                git(repo, "reset", "--hard", green)
                git(repo, "clean", "-fd")
                ws.restore_protected(repo, protected)
                # reset 後 post-condition(#1):必須回到乾淨綠點,否則綠點錨定不可信,不靠寫回
                # 快照硬撐,fail-closed 停機交人。
                if head_sha(repo) != green or is_dirty(repo):
                    ws.save_state(state)
                    notify(args.notify_cmd, "reset_broken", ws.dir.name)
                    fail(f"reset 回綠點 {green[:8]} 後工作樹不符預期"
                         f"（HEAD={head_sha(repo)[:8]}、dirty={is_dirty(repo)}）——"
                         f"綠點錨定不可信，停機交由人員確認。詳見 {ws.history}")
                # 依完成 sha 回退任務指標,不用一個一個退
                state["completed"] = [e for e in state["completed"] if is_ancestor(repo, e["sha"], green)]
                state["current_order"] = (state["completed"][-1]["order"] + 1) if state["completed"] else \
                    (state["plan"][0]["order"] if state["plan"] else 1)
                key = str(state["current_order"])
                state["task_reset_counts"][key] = state["task_reset_counts"].get(key, 0) + 1
                state["done_count"] = 0
                state["red_streak"] = 0
                state["stall_rounds"] = 0
                event = (f"🔄 RESET({reset_reason})→ 回到綠點 {green[:8]},任務指標退回 "
                         f"task-{state['current_order']}(該任務第 {state['task_reset_counts'][key]} 次 reset)")
                log(event)
                state["notes"].append(f"🔄 迴圈已 reset --hard 回最後綠點 {green[:8]}({reset_reason})。"
                                      "之前未收斂的工作已捨棄,請照當前任務重做。")
                if args.stuck_stop and state["task_reset_counts"][key] >= args.stuck_stop_count:
                    ws.save_state(state)
                    notify(args.notify_cmd, "stuck_stop", ws.dir.name)
                    fail(f"stuck-stop：task-{state['current_order']} 已 reset {state['task_reset_counts'][key]} 次，"
                         f"停機交由人員確認。詳見 {ws.history}")

        # agent 回報的 issue(work.py issue):落 state 給人類看,不影響任何計數
        pend = ws.dir / "pending_issues"
        if pend.exists():
            issue_lines = []
            for iline in pend.read_text(encoding="utf-8").splitlines():
                if iline.strip():
                    issue_lines.append(iline.strip())
                    state.setdefault("issues", []).append(
                        {"round": rnd, "where": task_id or phase, "text": iline.strip(),
                         "ts": datetime.now().isoformat(timespec="seconds")})
            pend.unlink()
            for issue_text in issue_lines:
                log(f"⚠️ Agent 回報 issue｜{issue_text}")
            if issue_lines:
                log(f"📌 Issue 累計｜目前有 {len(state.get('issues', []))} 條未清")

        line = (f"{datetime.now().isoformat(timespec='seconds')} round={rnd} phase={phase} "
                f"task={task_id or '-'} rc={rc} changed={changed} "
                f"signal={'create' if ws.signal('called_create_plan') else 'ok' if ws.signal('signal_plan_ok') else 'done' if ws.signal('signal_done') else '-'} "
                f"tamper={bool(tampered)} validate={validate_note} "
                f"flag={state['flag']} done={state['done_count']}"
                + (f"  << {event}" if event else ""))
        with open(ws.history, "a", encoding="utf-8") as hf:
            hf.write(line + "\n")
        log(f"📊 第 {rnd} 輪結束｜變更={'有' if changed else '無'}｜驗證={validate_note}｜"
            f"flag={state['flag']}｜done={state['done_count']}" + (f"｜{event}" if event else ""))
        ws.save_state(state)

    if state["phase"] == "done":
        report = (f"# loop-agent-lite RUN REPORT\n\n"
                  f"- repo: {repo}\n- 結束時間: {datetime.now().isoformat(timespec='seconds')}\n"
                  f"- 總輪數: {state['round']}\n- plan 版本: v{state['plan_version']}\n"
                  f"- 完成任務:\n"
                  + "".join(f"  - task-{e['order']} @ {e['sha'][:8]}(round {e['round']})\n"
                            for e in state["completed"])
                  + f"- reset 統計: {state['task_reset_counts'] or '無'}\n"
                  f"- 逐輪紀錄: {ws.history}\n")
        (ws.dir / "REPORT.md").write_text(report, encoding="utf-8")
        log(f"🏁 全部任務收斂。報告:{ws.dir / 'REPORT.md'}")
        notify(args.notify_cmd, "completed", ws.dir.name)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("⏸ 手動中斷｜state 已落地，重跑同一條命令即可續跑")
        sys.exit(130)
