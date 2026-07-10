export const PLAN_TEMPLATE = JSON.stringify([
  { order: 1, task: "任務描述：寫到一個無前後文的工程師能直接動工，含驗收標準（DoD）", ref: "docs/analysis.md#段落（選填）" },
  { order: 2, task: "第二個任務，依依賴順序排列", ref: null },
  { order: 3, task: "ref 可整個省略" }
], null, 2);

export function validatePlan(text: string) {
  if (!text.trim()) return "";
  let plan: unknown;
  try { plan = JSON.parse(text); } catch (error) { return `JSON 解析失敗：${(error as Error).message}`; }
  if (!Array.isArray(plan) || !plan.length) return "必須是非空的物件陣列";
  const orders: number[] = [];
  for (let index = 0; index < plan.length; index += 1) {
    const task = plan[index] as Record<string, unknown>;
    if (typeof task !== "object" || task === null || Array.isArray(task)) return `第 ${index} 項不是物件`;
    const extra = Object.keys(task).filter((key) => !["order", "task", "ref"].includes(key));
    if (extra.length) return `第 ${index} 項有未知欄位 ${extra.join(", ")}（只允許 order/task/ref）`;
    if (!Number.isInteger(task.order)) return `第 ${index} 項 order 必須是 int`;
    if (typeof task.task !== "string" || !task.task.trim()) return `第 ${index} 項 task 必須是非空字串`;
    if ("ref" in task && task.ref !== null && typeof task.ref !== "string") return `第 ${index} 項 ref 必須是字串或 null`;
    orders.push(task.order as number);
  }
  const duplicates = [...new Set(orders.filter((order, index) => orders.indexOf(order) !== index))];
  if (duplicates.length) return `order 重複：${duplicates.join(", ")}`;
  const sorted = [...orders].sort((a, b) => a - b);
  if (sorted.some((order, index) => order !== index + 1)) return `order 必須從 1 連續遞增至 ${orders.length}`;
  return "";
}
