/** 匯入前的輕量 plan 格式檢查；規則與 work.py 契約一致，但不能取代伺服器端校驗。 */
export const PLAN_TEMPLATE = JSON.stringify([
  { order: 1, task: "任務描述：寫到 fresh-context agent 能直接動工，含驗收標準（DoD）", ref: "docs/analysis.md#段落（選填）", track: "main", scope: ["src/**"] },
  { order: 2, task: "第二個任務；ref 與 scope 可省略", track: "main" }
], null, 2);

const TRACK_RE = /^[a-z0-9][a-z0-9_-]{0,23}$/;

export function validatePlan(text: string) {
  // 空字串代表不匯入；有內容時要求非空陣列、連續 order 與 task/ref 型別正確。
  if (!text.trim()) return "";
  let plan: unknown;
  try { plan = JSON.parse(text); } catch (error) { return `JSON 解析失敗：${(error as Error).message}`; }
  if (!Array.isArray(plan) || !plan.length) return "必須是非空的物件陣列";
  const orders: number[] = [];
  for (let index = 0; index < plan.length; index += 1) {
    const task = plan[index] as Record<string, unknown>;
    if (typeof task !== "object" || task === null || Array.isArray(task)) return `第 ${index} 項不是物件`;
    const extra = Object.keys(task).filter((key) => !["order", "task", "ref", "track", "scope"].includes(key));
    if (extra.length) return `第 ${index} 項有未知欄位 ${extra.join(", ")}（只允許 order/task/ref/track/scope）`;
    if (!Number.isInteger(task.order)) return `第 ${index} 項 order 必須是 int`;
    if (typeof task.task !== "string" || !task.task.trim()) return `第 ${index} 項 task 必須是非空字串`;
    if ("ref" in task && task.ref !== null && typeof task.ref !== "string") return `第 ${index} 項 ref 必須是字串或 null`;
    if (typeof task.track !== "string" || (task.track !== "@final" && !TRACK_RE.test(task.track))) return `第 ${index} 項 track 格式不合法`;
    if ("scope" in task && (!Array.isArray(task.scope) || !task.scope.length || task.scope.some((value) => typeof value !== "string" || !value.trim()))) return `第 ${index} 項 scope 必須是非空字串陣列`;
    orders.push(task.order as number);
  }
  const duplicates = [...new Set(orders.filter((order, index) => orders.indexOf(order) !== index))];
  if (duplicates.length) return `order 重複：${duplicates.join(", ")}`;
  const sorted = [...orders].sort((a, b) => a - b);
  if (sorted.some((order, index) => order !== index + 1)) return `order 必須從 1 連續遞增至 ${orders.length}`;
  if (new Set(plan.map((task) => (task as Record<string, unknown>).track)).size > 8) return "track 總數不可超過 8";
  return "";
}

/** 多軌或 @final plan 不能落入 standalone 順序執行；格式不合法時先交由既有錯誤提示處理。 */
export function planRequiresParallel(text: string): boolean {
  if (!text.trim() || validatePlan(text)) return false;
  const plan = JSON.parse(text) as Array<{ track: string }>;
  const tracks = new Set(plan.map((task) => task.track));
  return tracks.size > 1 || tracks.has("@final");
}
