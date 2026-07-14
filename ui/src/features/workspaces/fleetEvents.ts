/** 從各 workspace 的 bounded history 尾段推導 Fleet 事件；只做顯示，不建立新的 coordinator truth。 */
import { parseHistory } from "./historyParser";
import type { FleetHistoryEntry } from "../../shared/api/types";
import { withoutEmojiIcons } from "../console/consoleText";

export interface FleetEvent {
  ws: string;
  ts: string;
  time: string;
  text: string;
}

/** 從各 workspace 的 history.log 尾段推導事件流:
 * 任務切換時顯示「開始 task-N」；驗證由綠轉紅時顯示警示；輪末事件會移除 emoji 後顯示。 */
export function deriveFleetEvents(entries: FleetHistoryEntry[], limit = 60): FleetEvent[] {
  const events: FleetEvent[] = [];
  for (const entry of entries) {
    const ordered = [...parseHistory(entry.data).rows].reverse(); // 舊 → 新
    let prevTask = "";
    let prevValidate = "";
    for (const row of ordered) {
      if (row.task && row.task !== prevTask) {
        events.push({ ws: entry.name, ts: row.ts, time: row.time, text: `開始 ${row.task}` });
      }
      if (row.validate === "FAIL" && prevValidate !== "FAIL") {
        events.push({ ws: entry.name, ts: row.ts, time: row.time, text: `錯誤：驗證轉紅（r${row.round}）` });
      }
      if (row.timedOut) {
        events.push({ ws: entry.name, ts: row.ts, time: row.time, text: `Agent 逾時（r${row.round}）` });
      }
      if (row.event) {
        events.push({ ws: entry.name, ts: row.ts, time: row.time, text: withoutEmojiIcons(row.event) });
      }
      // 回規劃期代表任務指標已清空;之後重新進執行期,即使是同一個 task 也要重新發「開始」。
      prevTask = row.phaseRaw === "plan" ? "" : (row.task || prevTask);
      prevValidate = row.validate;
    }
  }
  events.sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0));
  return events.slice(0, limit);
}
