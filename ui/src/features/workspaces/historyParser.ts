export interface HistoryRow {
  round: number;
  time: string;
  phase: string;
  phaseRaw: string;
  task: string;
  signal: string;
  validate: string;
  flag: number;
  done: number;
  tamper: boolean;
  agentOk: boolean;
  event: string;
}

// loop.py 每輪輪末寫入 history.log:`<ts> key=value ... [<< 事件]`。
// 逐 token 解析 key=value,未知欄位忽略——欄位新增/插入不會讓投影整批失效;
// 連 round/phase 都沒有的行(舊版格式)才跳過並提示筆數。
const SIGNAL_NAMES: Record<string, string> = { create: "create-plan", ok: "plan-ok", done: "done" };
const PHASE_NAMES: Record<string, string> = { plan: "規劃", exec: "執行" };

export function parseHistory(data: string): { rows: HistoryRow[]; unparsed: number } {
  const rows: HistoryRow[] = [];
  let unparsed = 0;
  for (const line of data.split("\n")) {
    if (!line.trim()) continue;
    const eventIndex = line.indexOf("  << ");
    const head = eventIndex >= 0 ? line.slice(0, eventIndex) : line;
    const event = eventIndex >= 0 ? line.slice(eventIndex + 5) : "";
    const tokens = head.trim().split(/\s+/);
    const fields: Record<string, string> = {};
    for (const token of tokens.slice(1)) {
      const eq = token.indexOf("=");
      if (eq > 0) fields[token.slice(0, eq)] = token.slice(eq + 1);
    }
    if (!fields.round || !fields.phase) {
      unparsed += 1;
      continue;
    }
    const ts = tokens[0];
    const signal = fields.signal ?? "-";
    rows.push({
      time: ts.includes("T") ? ts.slice(ts.indexOf("T") + 1) : ts,
      round: +fields.round,
      phase: PHASE_NAMES[fields.phase] ?? fields.phase,
      phaseRaw: fields.phase,
      task: !fields.task || fields.task === "-" ? "" : fields.task,
      signal: SIGNAL_NAMES[signal] ?? (signal === "-" ? "" : signal),
      tamper: fields.tamper === "True",
      agentOk: fields.agent_ok !== "False",
      validate: fields.validate ?? "-",
      flag: +(fields.flag ?? 0),
      done: +(fields.done ?? 0),
      event
    });
  }
  return { rows: rows.reverse(), unparsed };
}
