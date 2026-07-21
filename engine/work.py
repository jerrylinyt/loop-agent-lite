#!/usr/bin/env python3
"""work.py — agent 唯一合法的協調層寫入口。

所有命令當場校驗、當場回報:錯了印「錯在哪+合法格式+正確範例」並以 rc=1 結束,
agent 同一輪修正後重打即可。命令不直接改 state.json——loop 於輪末統一 ingest。
"""
import json
import os
import sys
from pathlib import Path

from engine import loop as loop_mod

EXAMPLE = '[{"order": 1, "task": "描述", "ref": "PLAN.md#段落"}, {"order": 2, "task": "ref 可省略"}]'


def die(msg):
    """印出 coordinator 契約錯誤並以 exit 1 結束，表示命令未被接受。"""
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def ws_dir():
    """由受控環境變數取得 workspace，並驗證名稱與 root 邊界。"""
    p = os.environ.get("LOOP_WS")
    if not p:
        die("LOOP_WS 未設定或不存在:work.py 只在 loop.py 派發的 agent 環境內有效")
    try:
        directory = loop_mod.workspace_directory(Path(p), "LOOP_WS workspace")
    except ValueError as e:
        die(f"LOOP_WS 不安全:{e}")
    if directory is None:
        die("LOOP_WS 未設定或不存在:work.py 只在 loop.py 派發的 agent 環境內有效")
    return directory


def atomic_write_text(path, text):
    """Coordinator proposal 原子落地；CLI 被 SIGKILL 不得留下半截 JSON。"""
    loop_mod.atomic_write_bytes(path, text.encode("utf-8"))


def current_dispatch(ws):
    """讀取本輪原子派工並拒絕上一輪殘留 process 的延遲命令。"""
    token = os.environ.get("LOOP_ROUND_TOKEN", "")
    try:
        dispatch_path = loop_mod.workspace_file(ws / "dispatch.json", "dispatch.json")
        fd = loop_mod._open_regular(dispatch_path, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
            dispatch = json.load(stream)
    except (OSError, ValueError, json.JSONDecodeError):
        die("派工資訊不存在或損壞；本輪命令不生效")
    if not isinstance(dispatch, dict) or not token or token != dispatch.get("round_token"):
        die("這個 coordinator 命令來自已結束的 round，已拒絕；不得影響目前進度")
    return dispatch, token


def signal_path(ws, name, token):
    """建立綁定目前 round token 的 signal 路徑，舊輪檔案不會誤觸新輪。"""
    return ws / f"{name}.{token}"


def write_marker(path):
    """原子寫入空 marker；任何路徑安全錯誤都拒絕本次 Agent 回報。"""
    try:
        loop_mod.atomic_write_bytes(path, b"")
    except (OSError, ValueError) as e:
        die(f"協調檔案不安全或無法寫入:{e}")


def plan_has_stack(plan):
    """計畫是否明確出現 stack 欄位。

    這是 presence check，刻意不把非法值當成「沒有 stack」；schema 錯誤仍由
    ``validate_plan`` 回報，普通 runner 則可以用此 helper fail closed。
    """
    return (isinstance(plan, list)
            and any(isinstance(task, dict) and "stack" in task for task in plan))


def validate_serial_stack_opt_in(plan, allow_serial_stack=False):
    """回傳普通串行 runner 對 stack 的 opt-in 錯誤。

    只接受明確的 ``True``，避免呼叫端誤傳 truthy 字串就靜默忽略並行語意。
    呼叫端應先以 ``validate_plan`` 完成 schema/invariant 校驗。
    """
    if not plan_has_stack(plan) or allow_serial_stack is True:
        return []
    return [
        "計畫含 stack；普通 Loop 預設拒絕靜默串行，請改用 Parallel Loop，"
        "或明確傳入 --allow-serial-stack 允許忽略 stack 串行執行"
    ]


def validate_plan(plan):
    """計畫校驗(create-plan 與 dashboard 匯入共用):回 (normalized, errs)。"""
    if not isinstance(plan, list) or not plan:
        return None, [f"計畫必須是非空陣列。範例:{EXAMPLE}"]
    errs = []
    orders_valid = True
    stacks_valid = True
    for i, t in enumerate(plan):
        if not isinstance(t, dict):
            errs.append(f"第 {i} 項不是物件")
            orders_valid = False
            stacks_valid = False
            continue
        extra = set(t) - {"order", "task", "ref", "stack"}
        if extra:
            errs.append(f"第 {i} 項有未知欄位 {sorted(extra)},只允許 order/task/ref/stack")
        if not isinstance(t.get("order"), int) or isinstance(t.get("order"), bool):
            errs.append(f"第 {i} 項 order 必須是 int")
            orders_valid = False
        if not isinstance(t.get("task"), str) or not t.get("task", "").strip():
            errs.append(f"第 {i} 項 task 必須是非空字串(字數不限,寫到能動工)")
        if "ref" in t and t["ref"] is not None and not isinstance(t["ref"], str):
            errs.append(f"第 {i} 項 ref 必須是字串或 null(可省略)")
        if "stack" in t and (not isinstance(t["stack"], int)
                             or isinstance(t["stack"], bool) or t["stack"] <= 0):
            errs.append(f"第 {i} 項 stack 必須是正整數(bool 不接受)")
            stacks_valid = False
    orders = [t.get("order") for t in plan
              if isinstance(t, dict) and isinstance(t.get("order"), int) and not isinstance(t.get("order"), bool)]
    dup = sorted({o for o in orders if orders.count(o) > 1})
    if dup:
        errs.append(f"order 重複:{dup}")
        orders_valid = False
    elif orders_valid and sorted(orders) != list(range(1, len(plan) + 1)):
        errs.append(f"order 必須從 1 連續遞增至 {len(plan)},收到:{sorted(orders)}")
        orders_valid = False
    if orders_valid and stacks_valid:
        closed_stacks = set()
        previous_stack = None
        for task in sorted(plan, key=lambda item: item["order"]):
            current_stack = task.get("stack")
            if current_stack == previous_stack:
                continue
            if previous_stack is not None:
                closed_stacks.add(previous_stack)
            if current_stack is not None and current_stack in closed_stacks:
                errs.append(f"stack {current_stack} 必須只出現在一段連續 task")
            previous_stack = current_stack
    if errs:
        return None, errs
    normalized = []
    for task in sorted(plan, key=lambda item: item["order"]):
        item = {"order": task["order"], "task": task["task"].strip(),
                "ref": (task.get("ref") or None)}
        if "stack" in task:
            item["stack"] = task["stack"]
        normalized.append(item)
    return normalized, []


def cmd_create_plan(ws, argv):
    """接收完整 plan proposal，整包校驗後原子落盤，輪末才由 loop 採用。"""
    dispatch, token = current_dispatch(ws)
    if len(argv) > 1:
        die("用法:work.py create-plan [json檔]；最多只能給一個檔案，或由 stdin 餵入 JSON")
    # 先落 marker:create-plan 只要被 call(不論成敗)flag 就歸零(fail-closed)
    write_marker(signal_path(ws, "called_create_plan", token))
    if dispatch.get("phase") != "plan":
        die("執行期計畫已凍結,create-plan 不可用。任務本身有問題請在輸出/commit 說明,交由人類處理")
    raw = Path(argv[0]).read_text(encoding="utf-8") if argv else sys.stdin.read()
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"JSON 解析失敗:{e}。合法格式為物件陣列,範例:{EXAMPLE}")
    normalized, errs = validate_plan(plan)
    if not errs and plan_has_stack(normalized):
        errs.append(
            "規劃期 create-plan 不接受 stack；v1 的 stack 只能由人工 frozen plan import，"
            "並直接從 exec 啟動")
    if not errs:
        errs.extend(validate_serial_stack_opt_in(
            normalized,
            allow_serial_stack=(dispatch.get("allow_serial_stack") is True
                                or dispatch.get("runner") == "parallel-worker"),
        ))
    if errs:
        die("計畫校驗未過,整包不生效:\n  - " + "\n  - ".join(errs) + f"\n合法範例:{EXAMPLE}")
    try:
        atomic_write_text(ws / f"pending_plan.{token}.json",
                          json.dumps(normalized, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as e:
        die(f"協調檔案不安全或無法寫入:{e}")
    print(f"✅ 計畫校驗通過(共 {len(normalized)} 條),輪末生效。本輪 flag 歸零(計畫有變動=尚未收斂)。")


def cmd_plan_ok(ws, argv):
    """在規劃期宣告本輪計畫已完整；實際 flag 是否增加仍由輪末條件決定。"""
    dispatch, token = current_dispatch(ws)
    if argv:
        die("用法:work.py plan-ok（不接受其他參數）")
    if dispatch.get("phase") != "plan":
        die("目前不在規劃期,plan-ok 不可用")
    write_marker(signal_path(ws, "signal_plan_ok", token))
    print("✅ 已記錄「計畫完整」宣告;若本輪無任何計畫變動與 repo 異動,flag +1。")


def cmd_done(ws, argv):
    """只接受目前 dispatch 的 task id，避免 Agent 誤完成其他任務。"""
    dispatch, token = current_dispatch(ws)
    if len(argv) != 1:
        die("用法:work.py done <task-id>,例:work.py done task-3")
    cur_id = dispatch.get("task_id") or ""
    if dispatch.get("phase") != "exec" or not cur_id:
        die("目前不在執行期或無派發任務,done 不可用")
    if argv[0] != cur_id:
        die(f"任務編號不符:目前派發的是 {cur_id},你給的是 {argv[0]}。"
            f"若你認為派工有誤,什麼都不要做直接結束,交由下一輪處理")
    write_marker(signal_path(ws, "signal_done", token))
    print(f"✅ 已記錄 {cur_id} 完成宣告;若本輪無 commit/工作區異動且驗證為綠,done +1。")


def cmd_block(ws, argv):
    """managed worker 回報不可繼續的結構化 terminal reason。"""
    dispatch, token = current_dispatch(ws)
    if len(argv) < 2 or argv[0] != "--reason":
        die("用法:work.py block --reason <描述>")
    if dispatch.get("runner") != "parallel-worker":
        die("block 只供 managed parallel worker 使用；普通 Loop 請使用 issue")
    cur_id = dispatch.get("task_id") or ""
    if dispatch.get("phase") != "exec" or not cur_id:
        die("目前不在受管執行期或無派發任務,block 不可用")
    reason = " ".join(argv[1:]).strip().replace("\r", " ").replace("\n", " ")
    if not reason:
        die("用法:work.py block --reason <非空描述>")
    if len(reason) > loop_mod.ISSUE_MAX_CHARS:
        die(f"block reason 不可超過 {loop_mod.ISSUE_MAX_CHARS} 字")
    payload = {
        "schema_version": 1,
        "round_token": token,
        "task_id": cur_id,
        "reason": reason,
    }
    try:
        atomic_write_text(
            ws / f"pending_block.{token}.json",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
    except (OSError, ValueError) as e:
        die(f"協調檔案不安全或無法寫入:{e}")
    print(f"⛔ 已記錄 {cur_id} 為 blocked terminal；本輪結束後由 supervisor 接手。")


def cmd_issue(ws, argv):
    """agent 回報結構化問題(任務做不了/描述錯誤等):落 state 給人類看,不影響任何計數。"""
    _dispatch, token = current_dispatch(ws)
    text = " ".join(argv).strip() or sys.stdin.read().strip()
    if not text:
        die("用法:work.py issue <一句話描述問題>(或由 stdin 餵入)")
    if len(text) > loop_mod.ISSUE_MAX_CHARS:
        die(f"issue 描述不可超過 {loop_mod.ISSUE_MAX_CHARS} 字")
    pending_path = ws / f"pending_issues.{token}"
    try:
        pending = loop_mod.read_regular_text(pending_path, "pending issues")
    except FileNotFoundError:
        pending = ""
    except (OSError, ValueError, UnicodeDecodeError) as e:
        die(f"協調檔案不安全或無法讀取:{e}")
    if len(pending.splitlines()) >= loop_mod.ISSUES_MAX_PENDING:
        die(f"本輪 issue 不可超過 {loop_mod.ISSUES_MAX_PENDING} 條")
    try:
        loop_mod.append_regular_text(pending_path, text.replace("\n", " ") + "\n")
    except (OSError, ValueError) as e:
        die(f"協調檔案不安全或無法寫入:{e}")
    print("⚠ 已記錄 issue,輪末落入 state 供人類在 dashboard 檢視(不影響本輪計數)。")


def main():
    """分派 Agent 可用的最小 coordinator CLI；未知命令或參數一律 fail closed。"""
    if len(sys.argv) < 2:
        die("用法:work.py <create-plan [json檔]|plan-ok|done <task-id>|block --reason <描述>|issue <描述>>")
    ws = ws_dir()
    cmd, argv = sys.argv[1], sys.argv[2:]
    if cmd == "create-plan":
        cmd_create_plan(ws, argv)
    elif cmd == "plan-ok":
        cmd_plan_ok(ws, argv)
    elif cmd == "done":
        cmd_done(ws, argv)
    elif cmd == "block":
        cmd_block(ws, argv)
    elif cmd == "issue":
        cmd_issue(ws, argv)
    else:
        die(f"未知命令 {cmd}。可用:create-plan / plan-ok / done <task-id> / "
            "block --reason <描述> / issue <描述>")


if __name__ == "__main__":
    main()
