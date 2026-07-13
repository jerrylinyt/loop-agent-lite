#!/usr/bin/env python3
"""子程序終止方法實驗室（kill method lab）。

用途：找出「逾時砍掉 agent CLI」真正安全的方法——既要把 agent 的子孫殺乾淨，
又絕對不能誤殺機器上其他無關程序。

每種方法的測試流程：
  1. 先放 3 隻「金絲雀」程序（sleep 600，分別放在不同 session/group），
     再對整台機器做一次「本使用者所有 PID」快照。
  2. 用該方法對應的方式啟動 agent CLI（預設：opencode run 幫我整理這個專案到底在做什麼的）。
  3. 等 N 秒（預設 5 秒）。
  4. 記錄 agent 的完整程序樹（走 /proc 的 ppid 鏈 + 同 pgid/sid 掃描）。
  5. 用該方法砍掉。
  6. 驗證：
     - 主程序是否死亡
     - 程序樹是否殺乾淨（比對 pid+starttime，避免 PID 重用造成誤判）
     - 金絲雀是否全部存活（死了 = 這個方法會誤殺無辜 → 危險！）
     - 快照 diff：有沒有「不屬於 agent 樹」的既有程序消失（= 附帶傷害）

安全護欄（重要）：
  本腳本所有 kill/killpg 都經過 guard：
    - 拒絕 kill(pid<=1)   → pid 0 = 殺自己整個 group；pid -1 = 殺掉你有權限殺的所有程序（全機屠殺的元兇）
    - 拒絕 killpg(pgid<=1)
    - 拒絕 killpg(自己所在的 group) → start_new_session 沒生效時的典型災難
  如果某個方法觸發了 guard，報告會標出來——那就是你機器上「全部 process 被砍」的 root cause。

用法：
  python3 scripts/kill_method_lab.py                # 依序測所有方法（真的跑 opencode）
  python3 scripts/kill_method_lab.py current graceful
  python3 scripts/kill_method_lab.py --fake all     # 不跑 opencode，用內建的「會生小孩+會逃逸」假 agent 安全驗證
  python3 scripts/kill_method_lab.py --cmd "opencode run 其他prompt" --timeout 5 all

方法一覽：
  current   本專案 loop.py 的做法：start_new_session + 啟動時記下 pgid(=child pid)，逾時 killpg SIGKILL
  requery   本專案 run_validate 的變體：killpg(os.getpgid(p.pid))——砍的當下才查 pgid
  naive     天真做法：只 p.kill() 主程序（subprocess.run(timeout=..) 的等價行為）——預期會留孤兒
  graceful  先 SIGTERM 整個 group，給 3 秒優雅收尾，再 SIGKILL 整個 group（推薦的一般解）
  tree      不用 killpg：走 /proc 列出所有子孫，逐一 SIGKILL（對 group 出問題免疫，但有競態窗口）
  pidfd     pidfd_send_signal 精準殺主程序（PID 重用免疫）+ guard 過的 killpg 掃子孫（Python 3.9+）
  cgroup    systemd-run --user --scope 包起來跑，用 systemctl kill 整個 cgroup（連 setsid 逃逸的都殺得到，最徹底）
"""
import argparse
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_CMD = ["opencode", "run", "幫我整理這個專案到底在做什麼的"]
DEFAULT_TIMEOUT = 5.0
GRACE_SECS = 3.0        # graceful 方法的 SIGTERM → SIGKILL 寬限
SETTLE_SECS = 1.5       # 砍完後等 kernel/收屍安定的時間


# ─────────────────────────── /proc 工具 ───────────────────────────

def read_proc(pid):
    """讀 /proc/<pid>/stat，回傳 dict 或 None（程序不存在/已收屍）。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        uid = os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None
    # 格式：pid (comm) state ppid pgrp session ... starttime 在 comm 後第 20 欄
    rp = data.rfind(b")")
    comm = data[data.find(b"(") + 1:rp].decode(errors="replace")
    fields = data[rp + 2:].split()
    return {
        "pid": int(pid), "comm": comm, "state": fields[0].decode(),
        "ppid": int(fields[1]), "pgrp": int(fields[2]), "sess": int(fields[3]),
        "starttime": int(fields[19]), "uid": uid,
    }


def snapshot_user_procs():
    """本使用者所有存活程序：{pid: info}。用 pid+starttime 當身分，避免 PID 重用誤判。"""
    me = os.getuid()
    procs = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        info = read_proc(entry)
        if info and info["uid"] == me and info["state"] != "Z":
            procs[info["pid"]] = info
    return procs


def agent_cohort(root_pid, procs):
    """agent 的完整程序集合：ppid 鏈的所有子孫 ∪ 同 pgid ∪ 同 session。
    daemonize（reparent 到 1）靠 sess/pgid 抓；setsid 逃逸的靠砍之前的 ppid 鏈抓。"""
    children = {}
    for info in procs.values():
        children.setdefault(info["ppid"], []).append(info["pid"])
    cohort, queue = set(), [root_pid]
    while queue:
        pid = queue.pop()
        if pid in cohort:
            continue
        cohort.add(pid)
        queue.extend(children.get(pid, []))
    for info in procs.values():
        if info["pgrp"] == root_pid or info["sess"] == root_pid:
            cohort.add(info["pid"])
    return cohort


def still_alive(identity):
    """identity=(pid, starttime)。同 pid 但 starttime 不同 = PID 被重用，視為已死。"""
    pid, starttime = identity
    info = read_proc(pid)
    return info is not None and info["starttime"] == starttime and info["state"] != "Z"


# ─────────────────────────── 安全護欄 ───────────────────────────

class GuardTripped(RuntimeError):
    """kill/killpg 的參數落入「會屠殺無辜」的範圍，被攔下。"""


def guarded_kill(pid, sig):
    if pid <= 1:
        raise GuardTripped(
            f"攔截 os.kill({pid}, {sig!r})：pid=0 會殺掉自己整個 process group、"
            f"pid=-1 會殺掉你有權限殺的【所有】程序（root 下等於全機）。"
            f"這就是『全部 process 被砍』的典型元兇。")
    os.kill(pid, sig)


def guarded_killpg(pgid, sig):
    if pgid <= 1:
        raise GuardTripped(
            f"攔截 os.killpg({pgid}, {sig!r})：pgid<=0 語意等同 kill(0)/kill(-1)，會波及自己整組甚至全機。")
    if pgid == os.getpgid(0):
        raise GuardTripped(
            f"攔截 os.killpg({pgid}, {sig!r})：這是【本腳本自己所在】的 process group！"
            f"代表 start_new_session 沒有生效（child 沒有自成一組）——"
            f"在正式環境這一刀會把啟動 loop 的 shell 和同 session 的所有東西一起帶走。")
    os.killpg(pgid, sig)


# ─────────────────────────── 各種殺法 ───────────────────────────
# launch(cmd, log_file) -> (Popen, ctx)；kill(p, ctx) -> list[str] 過程紀錄

def launch_new_session(cmd, log):
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
    return p, {"pgid": p.pid}


def launch_plain(cmd, log):
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT)
    return p, {}


def kill_current(p, ctx):
    """本專案 run_agent 的做法：用啟動當下記住的 pgid 直接 SIGKILL 整組。"""
    guarded_killpg(ctx["pgid"], signal.SIGKILL)
    return [f"killpg({ctx['pgid']}, SIGKILL)（pgid 為啟動時記錄，不重查）"]


def kill_requery(p, ctx):
    """本專案 run_validate 的變體：砍的當下才 getpgid。
    風險：若主程序已死且 PID 被重用，getpgid 拿到的是別人的 group。"""
    pgid = os.getpgid(p.pid)  # 主程序已收屍會丟 ProcessLookupError
    guarded_killpg(pgid, signal.SIGKILL)
    return [f"getpgid({p.pid}) -> {pgid}，killpg({pgid}, SIGKILL)"]


def kill_naive(p, ctx):
    """只殺主程序（等同 subprocess.run(timeout=...) 逾時行為）。預期：孤兒殘留。"""
    p.kill()
    return [f"p.kill()（SIGKILL 只送給主程序 {p.pid}）"]


def kill_graceful(p, ctx):
    """先 SIGTERM 整組讓 CLI 優雅收尾（存 session、關檔案），寬限後才 SIGKILL 整組。"""
    notes = []
    guarded_killpg(ctx["pgid"], signal.SIGTERM)
    notes.append(f"killpg({ctx['pgid']}, SIGTERM)，寬限 {GRACE_SECS:g}s")
    try:
        p.wait(timeout=GRACE_SECS)
        notes.append("主程序在寬限期內自行退出")
    except subprocess.TimeoutExpired:
        notes.append("寬限期滿，補 SIGKILL")
    try:
        guarded_killpg(ctx["pgid"], signal.SIGKILL)
        notes.append(f"killpg({ctx['pgid']}, SIGKILL)")
    except ProcessLookupError:
        notes.append("整組已無存活程序，SIGKILL 免了")
    return notes


def kill_tree(p, ctx):
    """完全不用 killpg：走 /proc 列出子孫，先殺主程序（阻止再生），再逐一殺子孫。
    優點：group 出任何問題都不會誤殺；缺點：列舉到動手之間有競態窗口。"""
    notes = []
    procs = snapshot_user_procs()
    cohort = agent_cohort(p.pid, procs)
    guarded_kill(p.pid, signal.SIGKILL)
    notes.append(f"kill({p.pid}, SIGKILL) 主程序")
    for pid in sorted(cohort - {p.pid}):
        try:
            guarded_kill(pid, signal.SIGKILL)
            notes.append(f"kill({pid}, SIGKILL) 子孫 {procs[pid]['comm']}")
        except ProcessLookupError:
            pass
    # 再掃一輪，抓列舉窗口期間新生的
    for pid in sorted(agent_cohort(p.pid, snapshot_user_procs()) - {p.pid}):
        try:
            guarded_kill(pid, signal.SIGKILL)
            notes.append(f"kill({pid}, SIGKILL) 補刀（第二輪掃描）")
        except ProcessLookupError:
            pass
    return notes


def launch_pidfd(cmd, log):
    p, ctx = launch_new_session(cmd, log)
    ctx["pidfd"] = os.pidfd_open(p.pid)  # 從此刻起這個 fd 永遠指向「這一個」程序，PID 重用免疫
    return p, ctx


def kill_pidfd(p, ctx):
    """pidfd 精準殺主程序（不可能誤中 PID 重用後的別人），group 掃尾照樣走 guard。"""
    notes = []
    try:
        signal.pidfd_send_signal(ctx["pidfd"], signal.SIGKILL)
        notes.append("pidfd_send_signal(SIGKILL)：精準命中主程序本尊")
    except ProcessLookupError:
        notes.append("主程序已死（pidfd 保證不會誤傷重用該 PID 的新程序）")
    finally:
        os.close(ctx["pidfd"])
    try:
        guarded_killpg(ctx["pgid"], signal.SIGKILL)
        notes.append(f"killpg({ctx['pgid']}, SIGKILL) 清子孫")
    except ProcessLookupError:
        notes.append("group 已無存活程序")
    return notes


def cgroup_available():
    if not shutil.which("systemd-run"):
        return False, "找不到 systemd-run"
    r = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "true"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"systemd --user 不可用：{(r.stderr or '').strip()[:120]}"
    return True, ""


def launch_cgroup(cmd, log):
    unit = f"kill-lab-{os.getpid()}-{time.monotonic_ns()}.scope"
    wrapped = ["systemd-run", "--user", "--scope", "--collect",
               "--quiet", "--unit", unit] + cmd
    p = subprocess.Popen(wrapped, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
    return p, {"pgid": p.pid, "unit": unit}


def kill_cgroup(p, ctx):
    """殺整個 cgroup：kernel 記帳，連 setsid/雙重 fork 逃出 process group 的都躲不掉。"""
    subprocess.run(["systemctl", "--user", "kill", "--kill-whom=all",
                    "--signal=SIGKILL", ctx["unit"]], capture_output=True)
    subprocess.run(["systemctl", "--user", "stop", ctx["unit"]], capture_output=True)
    return [f"systemctl --user kill --kill-whom=all -s SIGKILL {ctx['unit']}"]


METHODS = {
    "current": ("本專案 loop.py 做法：start_new_session + killpg(啟動時記的 pgid, SIGKILL)",
                launch_new_session, kill_current, None),
    "requery": ("本專案 run_validate 變體：killpg(砍時才 getpgid(p.pid), SIGKILL)",
                launch_new_session, kill_requery, None),
    "naive": ("天真做法：只 p.kill() 主程序（subprocess.run timeout 等價）",
              launch_plain, kill_naive, None),
    "graceful": ("推薦一般解：SIGTERM 整組 → 3 秒寬限 → SIGKILL 整組",
                 launch_new_session, kill_graceful, None),
    "tree": ("不用 killpg：走 /proc 逐一殺子孫（免疫 group 問題，有競態窗口）",
             launch_new_session, kill_tree, None),
    "pidfd": ("pidfd 精準殺主程序（PID 重用免疫）+ guard 過的 killpg 掃子孫",
              launch_pidfd, kill_pidfd,
              lambda: (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"),
                       "需要 Python 3.9+ / Linux 5.3+")),
    "cgroup": ("systemd cgroup scope：連 setsid 逃逸的子孫都殺得到（最徹底）",
               launch_cgroup, kill_cgroup, lambda: cgroup_available()),
}
ORDER = ["current", "requery", "naive", "graceful", "tree", "pidfd", "cgroup"]


# ─────────────────────────── 假 agent（--fake） ───────────────────────────

FAKE_AGENT = """#!/bin/bash
# 模擬一個「會生小孩、有的還會逃逸」的 agent CLI，用來安全驗證各殺法。
sleep 300 &                          # 普通子程序（同 group）
nohup sleep 300 >/dev/null 2>&1 &    # nohup 子程序（仍在同 group）
setsid sleep 300 >/dev/null 2>&1 &   # 逃離 process group 的子程序（只有 cgroup 殺得到）
( sleep 300 ) &                      # 孫程序
echo "fake agent 已啟動 4 個子孫，主程序開始待命"
sleep 300
"""


# ─────────────────────────── 單一方法測試 ───────────────────────────

def run_one(name, agent_cmd, wait_secs, log_dir):
    desc, launch, kill, avail = METHODS[name]
    print(f"\n{'=' * 72}\n▶ 方法 [{name}]  {desc}")
    if avail:
        ok, why = avail()
        if not ok:
            print(f"  ⏭ 跳過：{why}")
            return {"name": name, "skipped": why}

    result = {"name": name, "notes": []}

    # 1. 金絲雀：分別放在「同 group」「自成 session」「自成 group」三種位置
    canaries = [
        ("同group金絲雀", subprocess.Popen(["sleep", "600"], stdin=subprocess.DEVNULL)),
        ("獨立session金絲雀", subprocess.Popen(["sleep", "600"], stdin=subprocess.DEVNULL,
                                               start_new_session=True)),
        ("獨立group金絲雀", subprocess.Popen(["sleep", "600"], stdin=subprocess.DEVNULL,
                                             preexec_fn=os.setpgrp)),
    ]
    time.sleep(0.2)
    canary_ids = {}
    for label, c in canaries:
        info = read_proc(c.pid)
        canary_ids[label] = (c.pid, info["starttime"] if info else -1)

    before = snapshot_user_procs()
    shell_pid = os.getppid()

    log_path = log_dir / f"{name}.log"
    survivors = []
    try:
        with open(log_path, "wb") as log:
            p, ctx = launch(agent_cmd, log)
            print(f"  已啟動 pid={p.pid}，等 {wait_secs:g} 秒後動手…（輸出 → {log_path}）")
            try:
                p.wait(timeout=wait_secs)
                print(f"  ⚠ agent 在 {wait_secs:g} 秒內自己結束了（rc={p.returncode}），仍檢查子孫殘留")
            except subprocess.TimeoutExpired:
                pass

            # 2. 砍之前先把整棵樹的身分記下來（pid+starttime）
            procs_now = snapshot_user_procs()
            cohort_pids = agent_cohort(p.pid, procs_now) if p.poll() is None else \
                agent_cohort(p.pid, procs_now) - {p.pid}
            cohort_ids = {pid: (pid, procs_now[pid]["starttime"])
                          for pid in cohort_pids if pid in procs_now}
            print(f"  砍之前的程序樹（{len(cohort_ids)} 個）："
                  + ", ".join(f"{pid}:{procs_now[pid]['comm']}" for pid in sorted(cohort_ids)))

            # 3. 動手
            t0 = time.monotonic()
            try:
                for note in kill(p, ctx):
                    print(f"  ↳ {note}")
            except GuardTripped as e:
                print(f"  🚨 GUARD 攔截！{e}")
                result["guard"] = str(e)
            except ProcessLookupError:
                print("  ↳ 目標已不存在（ProcessLookupError）")
            if p.poll() is None:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            result["kill_ms"] = round((time.monotonic() - t0) * 1000)

        time.sleep(SETTLE_SECS)

        # 4. 驗證
        result["main_dead"] = p.poll() is not None
        survivors = [(pid, procs_now[pid]["comm"]) for pid, ident in cohort_ids.items()
                     if pid != p.pid and still_alive(ident)]
        result["survivors"] = survivors

        dead_canaries = [label for label, ident in canary_ids.items() if not still_alive(ident)]
        result["dead_canaries"] = dead_canaries
        result["shell_alive"] = read_proc(shell_pid) is not None

        after = snapshot_user_procs()
        collateral = []
        for pid, info in before.items():
            if pid in cohort_ids or pid == p.pid:
                continue
            if any(pid == c.pid for _, c in canaries):
                continue
            gone = pid not in after or after[pid]["starttime"] != info["starttime"]
            if gone and info["comm"] not in ("ps",):  # 短命工具程序不算
                collateral.append((pid, info["comm"]))
        result["collateral"] = collateral

        # 5. 印結果
        print(f"  主程序死亡：{'✅' if result['main_dead'] else '❌ 還活著'}"
              f"（kill 耗時 {result.get('kill_ms', '?')}ms）")
        if survivors:
            print(f"  ❌ 子孫殘留 {len(survivors)} 個：" + ", ".join(f"{pid}:{c}" for pid, c in survivors))
        else:
            print("  ✅ 程序樹殺乾淨，無孤兒")
        if dead_canaries:
            print(f"  🚨 誤殺金絲雀：{dead_canaries} ← 這個方法【危險】，會殺到無關程序！")
        else:
            print("  ✅ 三隻金絲雀全數存活（沒有誤殺無辜）")
        if not result["shell_alive"]:
            print("  🚨 連本腳本的父 shell 都被殺了！（= 你機器上發生的事）")
        if collateral:
            print(f"  🚨 附帶傷害：{len(collateral)} 個既有無關程序消失："
                  + ", ".join(f"{pid}:{c}" for pid, c in collateral[:10]))
        else:
            print("  ✅ 全機 PID 快照 diff：沒有無關程序消失")
    finally:
        # 6. 清場：金絲雀 + 任何殘留，讓下一個方法從乾淨狀態開始
        for _, c in canaries:
            try:
                c.kill()
                c.wait(timeout=3)
            except Exception:
                pass
        for pid, _ in survivors:
            try:
                guarded_kill(pid, signal.SIGKILL)
            except (ProcessLookupError, GuardTripped):
                pass

    return result


def verdict(r):
    if "skipped" in r:
        return f"⏭ 跳過（{r['skipped']}）"
    if r.get("dead_canaries") or r.get("collateral") or not r.get("shell_alive", True):
        return "🚨 危險：會誤殺無關程序"
    if "guard" in r:
        return "🛑 guard 攔截（沒有 guard 的話就是災難）"
    if not r.get("main_dead"):
        return "❌ 無效：主程序沒死"
    if r.get("survivors"):
        return f"⚠ 不完整：留下 {len(r['survivors'])} 個孤兒"
    return "✅ 安全且乾淨"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("methods", nargs="*", default=[],
                    help=f"要測的方法（{'/'.join(ORDER)}/all），預設 all")
    ap.add_argument("--cmd", default=None,
                    help=f"覆寫 agent 命令，預設：{shlex.join(DEFAULT_CMD)}")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help=f"啟動後幾秒動手砍（預設 {DEFAULT_TIMEOUT:g}）")
    ap.add_argument("--fake", action="store_true",
                    help="不跑真的 CLI，用內建假 agent（會生 4 個子孫、其中 1 個 setsid 逃逸）安全驗證")
    args = ap.parse_args()

    if os.geteuid() == 0:
        print("🚨 拒絕以 root 執行：root 下 kill(-1) 一類的失誤 = 全機屠殺。請用一般使用者測試。")
        return 2

    names = args.methods or ["all"]
    if "all" in names:
        names = ORDER
    for n in names:
        if n not in METHODS:
            ap.error(f"未知方法 {n}，可用：{', '.join(ORDER)}, all")

    if args.fake:
        fake = Path(tempfile.mkdtemp(prefix="kill-lab-")) / "fake_agent.sh"
        fake.write_text(FAKE_AGENT, encoding="utf-8")
        fake.chmod(0o755)
        agent_cmd = ["/bin/bash", str(fake)]
        print(f"使用假 agent：{fake}（含 setsid 逃逸子孫，觀察哪些方法抓得到）")
    else:
        agent_cmd = shlex.split(args.cmd) if args.cmd else DEFAULT_CMD
        if not shutil.which(agent_cmd[0]):
            print(f"找不到命令 {agent_cmd[0]!r}。先用 --fake 驗證，或用 --cmd 指定路徑。")
            return 2

    log_dir = Path.cwd() / "kill_lab_logs"
    log_dir.mkdir(exist_ok=True)
    print(f"agent 命令：{shlex.join(agent_cmd)}｜{args.timeout:g} 秒後砍｜"
          f"本腳本 pid={os.getpid()} pgid={os.getpgid(0)} sid={os.getsid(0)}")

    results = [run_one(n, agent_cmd, args.timeout, log_dir) for n in names]

    print(f"\n{'=' * 72}\n總結（判斷標準：金絲雀存活 + 無附帶傷害 = 安全；無孤兒 = 乾淨）\n")
    width = max(len(n) for n in names)
    for r in results:
        print(f"  {r['name']:<{width}}  {verdict(r)}")
    print("""
判讀指南：
  ✅ 安全且乾淨      → 可以用在正式環境
  ⚠ 不完整          → 不會誤殺，但孤兒會繼續佔資源/改 repo（naive 的預期結果）
  🚨 危險            → 就是它把你內網機器的程序全砍了
  🛑 guard 攔截      → 參數落入 kill(-1)/kill(0)/killpg(自己group) 的屠殺區，guard 救了你；
                       去追這個 pgid/pid 是怎麼算出來的，那就是 root cause

備註：--fake 模式下，setsid 逃逸的那隻 sleep 只有 cgroup 方法殺得到；
其他方法會把它列為孤兒——這是預期行為，不是 bug，代表該方法對「刻意逃逸」無解。""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
