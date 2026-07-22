/** 匯入前的輕量 plan 格式檢查；規則與 work.py 契約一致，但不能取代伺服器端校驗。 */
export const PLAN_TEMPLATE = JSON.stringify([
  { order: 1, task: "任務描述：寫到一個無前後文的工程師能直接動工，含驗收標準（DoD）", ref: "docs/analysis.md#段落（選填）" },
  { order: 2, task: "第二個任務，依依賴順序排列；沒有真實參考文件時省略 ref" }
], null, 2);

export const PARALLEL_PLAN_TEMPLATE = JSON.stringify([
  {
    order: 1,
    task: "範例，請先替換：明列 working set、生成物、語意依賴與獨立驗證資源；DoD：填入可重跑命令與通過判準。",
    stack: 1
  },
  {
    order: 2,
    task: "範例，請先替換：與 order 1 不共用檔案、schema、生成物、port、DB、cache 或 lock；DoD：填入隔離後的驗證命令。",
    stack: 1
  },
  { order: 3, task: "範例，請先替換：不確定是否獨立或有前置依賴時不標 stack，任務會自成串行 batch；DoD：填入驗證方式。" }
], null, 2);

interface PlanValidationOptions {
  required?: boolean;
  allowStack?: boolean;
}

interface ValidatedPlanTask {
  order: number;
  stack?: number;
}

export function validatePlan(text: string, { required = false, allowStack = false }: PlanValidationOptions = {}) {
  // 普通 Loop 可留空；Parallel 必須匯入已凍結 plan，且兩者都維持後端的完整 schema invariant。
  if (!text.trim()) return required ? "Parallel Loop 必須匯入已凍結的非空 plan" : "";
  let plan: unknown;
  try { plan = JSON.parse(text); } catch (error) { return `JSON 解析失敗：${(error as Error).message}`; }
  if (!Array.isArray(plan) || !plan.length) return "必須是非空的物件陣列";
  const orders: number[] = [];
  const tasks: ValidatedPlanTask[] = [];
  for (let index = 0; index < plan.length; index += 1) {
    const task = plan[index] as Record<string, unknown>;
    if (typeof task !== "object" || task === null || Array.isArray(task)) return `第 ${index} 項不是物件`;
    const extra = Object.keys(task).filter((key) => !["order", "task", "ref", "stack"].includes(key));
    if (extra.length) return `第 ${index} 項有未知欄位 ${extra.join(", ")}（只允許 order/task/ref/stack）`;
    if (!Number.isInteger(task.order)) return `第 ${index} 項 order 必須是 int`;
    if (typeof task.task !== "string" || !task.task.trim()) return `第 ${index} 項 task 必須是非空字串`;
    if ("ref" in task && task.ref !== null && typeof task.ref !== "string") return `第 ${index} 項 ref 必須是字串或 null`;
    if ("stack" in task) {
      if (!allowStack) return `第 ${index} 項含 stack；請改用 Parallel Loop`;
      if (typeof task.stack !== "number" || !Number.isInteger(task.stack) || task.stack <= 0) {
        return `第 ${index} 項 stack 必須是正整數（boolean 不允許）`;
      }
    }
    orders.push(task.order as number);
    tasks.push({ order: task.order as number, ...(typeof task.stack === "number" ? { stack: task.stack } : {}) });
  }
  const duplicates = [...new Set(orders.filter((order, index) => orders.indexOf(order) !== index))];
  if (duplicates.length) return `order 重複：${duplicates.join(", ")}`;
  const sorted = [...orders].sort((a, b) => a - b);
  if (sorted.some((order, index) => order !== index + 1)) return `order 必須從 1 連續遞增至 ${orders.length}`;

  const closedStacks = new Set<number>();
  let previousStack: number | undefined;
  for (const task of [...tasks].sort((a, b) => a.order - b.order)) {
    if (task.stack === previousStack) continue;
    if (previousStack !== undefined) closedStacks.add(previousStack);
    if (task.stack !== undefined && closedStacks.has(task.stack)) {
      return `stack ${task.stack} 必須只出現在一個連續的 order 區段`;
    }
    previousStack = task.stack;
  }
  return "";
}

/** 僅在 validatePlan 通過後用於 launcher 的輕量 batch 預覽與實際並行判定。 */
export function getPlanBatchAnalysis(text: string) {
  try {
    const plan = JSON.parse(text) as ValidatedPlanTask[];
    const tasks = [...plan].sort((a, b) => a.order - b.order);
    const batches: ValidatedPlanTask[][] = [];
    for (const task of tasks) {
      const current = batches[batches.length - 1];
      if (task.stack !== undefined && current?.[0]?.stack === task.stack) current.push(task);
      else batches.push([task]);
    }
    const labels = batches.slice(0, 6).map((batch) => {
      const first = batch[0];
      const last = batch[batch.length - 1];
      const orders = first.order === last.order ? `#${first.order}` : `#${first.order}–#${last.order}`;
      return first.stack === undefined ? orders : `stack ${first.stack} (${orders})`;
    });
    if (batches.length > labels.length) labels.push(`其餘 ${batches.length - labels.length} 批`);
    return {
      preview: `${batches.length} 個 batch：${labels.join(" → ")}`,
      // 單一 task 即使帶 stack 也沒有並行；必須有同一 batch 至少兩個 task。
      hasParallelBatch: batches.some((batch) => batch.length > 1)
    };
  } catch {
    return null;
  }
}

export function getPlanBatchPreview(text: string) {
  return getPlanBatchAnalysis(text)?.preview ?? "";
}
