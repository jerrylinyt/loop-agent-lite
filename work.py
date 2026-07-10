#!/usr/bin/env python3
"""work.py — agent 唯一合法的協調層寫入口(create-plan / plan-ok / done)。

所有命令當場校驗、當場回報:錯了印「錯在哪+合法格式+正確範例」並以 rc=1 結束,
agent 同一輪修正後重打即可。命令不直接改 state.json——loop 於輪末統一 ingest。
"""
import json
import os
import sys
from pathlib import Path

EXAMPLE = '[{"order": 1, "task": "描述", "ref": "PLAN.md#段落"}, {"order": 2, "task": "ref 可省略"}]'


def die(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def ws_dir():
    p = os.environ.get("LOOP_WS")
    if not p or not Path(p).is_dir():
        die("LOOP_WS 未設定或不存在:work.py 只在 loop.py 派發的 agent 環境內有效")
    return Path(p)


def read_phase(ws):
    f = ws / "phase"
    return f.read_text(encoding="utf-8").strip() if f.exists() else ""


def validate_plan(plan):
    """計畫校驗(create-plan 與 dashboard 匯入共用):回 (normalized, errs)。"""
    if not isinstance(plan, list) or not plan:
        return None, [f"計畫必須是非空陣列。範例:{EXAMPLE}"]
    errs = []
    for i, t in enumerate(plan):
        if not isinstance(t, dict):
            errs.append(f"第 {i} 項不是物件")
            continue
        extra = set(t) - {"order", "task", "ref"}
        if extra:
            errs.append(f"第 {i} 項有未知欄位 {sorted(extra)},只允許 order/task/ref")
        if not isinstance(t.get("order"), int) or isinstance(t.get("order"), bool):
            errs.append(f"第 {i} 項 order 必須是 int")
        if not isinstance(t.get("task"), str) or not t.get("task", "").strip():
            errs.append(f"第 {i} 項 task 必須是非空字串(字數不限,寫到能動工)")
        if "ref" in t and t["ref"] is not None and not isinstance(t["ref"], str):
            errs.append(f"第 {i} 項 ref 必須是字串或 null(可省略)")
    orders = [t.get("order") for t in plan
              if isinstance(t, dict) and isinstance(t.get("order"), int) and not isinstance(t.get("order"), bool)]
    dup = sorted({o for o in orders if orders.count(o) > 1})
    if dup:
        errs.append(f"order 重複:{dup}")
    elif not errs and sorted(orders) != list(range(1, len(plan) + 1)):
        errs.append(f"order 必須從 1 連續遞增至 {len(plan)},收到:{sorted(orders)}")
    if errs:
        return None, errs
    normalized = [{"order": t["order"], "task": t["task"].strip(), "ref": (t.get("ref") or None)}
                  for t in sorted(plan, key=lambda x: x["order"])]
    return normalized, []


def cmd_create_plan(ws, argv):
    # 先落 marker:create-plan 只要被 call(不論成敗)flag 就歸零(fail-closed)
    (ws / "called_create_plan").write_text("", encoding="utf-8")
    if read_phase(ws) != "plan":
        die("執行期計畫已凍結,create-plan 不可用。任務本身有問題請在輸出/commit 說明,交由人類處理")
    raw = Path(argv[0]).read_text(encoding="utf-8") if argv else sys.stdin.read()
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"JSON 解析失敗:{e}。合法格式為物件陣列,範例:{EXAMPLE}")
    normalized, errs = validate_plan(plan)
    if errs:
        die("計畫校驗未過,整包不生效:\n  - " + "\n  - ".join(errs) + f"\n合法範例:{EXAMPLE}")
    (ws / "pending_plan.json").write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 計畫校驗通過(共 {len(normalized)} 條),輪末生效。本輪 flag 歸零(計畫有變動=尚未收斂)。")


def cmd_plan_ok(ws):
    if read_phase(ws) != "plan":
        die("目前不在規劃期,plan-ok 不可用")
    (ws / "signal_plan_ok").write_text("", encoding="utf-8")
    print("✅ 已記錄「計畫完整」宣告;若本輪無任何計畫變動與 repo 異動,flag +1。")


def cmd_done(ws, argv):
    if not argv:
        die("用法:work.py done <task-id>,例:work.py done task-3")
    cur = ws / "current_task"
    cur_id = cur.read_text(encoding="utf-8").strip() if cur.exists() else ""
    if read_phase(ws) != "exec" or not cur_id:
        die("目前不在執行期或無派發任務,done 不可用")
    if argv[0] != cur_id:
        die(f"任務編號不符:目前派發的是 {cur_id},你給的是 {argv[0]}。"
            f"若你認為派工有誤,什麼都不要做直接結束,交由下一輪處理")
    (ws / "signal_done").write_text("", encoding="utf-8")
    print(f"✅ 已記錄 {cur_id} 完成宣告;若本輪無 commit/工作區異動且驗證為綠,done +1。")


def cmd_issue(ws, argv):
    """agent 回報結構化問題(任務做不了/描述錯誤等):落 state 給人類看,不影響任何計數。"""
    text = " ".join(argv).strip() or sys.stdin.read().strip()
    if not text:
        die("用法:work.py issue <一句話描述問題>(或由 stdin 餵入)")
    with open(ws / "pending_issues", "a", encoding="utf-8") as f:
        f.write(text.replace("\n", " ") + "\n")
    print("⚠ 已記錄 issue,輪末落入 state 供人類在 dashboard 檢視(不影響本輪計數)。")


def main():
    if len(sys.argv) < 2:
        die("用法:work.py <create-plan [json檔]|plan-ok|done <task-id>|issue <描述>>")
    ws = ws_dir()
    cmd, argv = sys.argv[1], sys.argv[2:]
    if cmd == "create-plan":
        cmd_create_plan(ws, argv)
    elif cmd == "plan-ok":
        cmd_plan_ok(ws)
    elif cmd == "done":
        cmd_done(ws, argv)
    elif cmd == "issue":
        cmd_issue(ws, argv)
    else:
        die(f"未知命令 {cmd}。可用:create-plan / plan-ok / done <task-id> / issue <描述>")


if __name__ == "__main__":
    main()
