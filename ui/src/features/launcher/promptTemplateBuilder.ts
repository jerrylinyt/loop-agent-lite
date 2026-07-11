/** 將固定契約、任務模板與使用者需求組合成可交給外部 Agent 的純文字 prompt。 */
import type { PromptTemplate } from "../../shared/api/types";

export type PromptTemplateMode = "goal" | "plan";

const DEFAULT_REQUIREMENT = "請在這裡貼上完整需求、目前問題與已知限制。";
const DEFAULT_CONTEXT = "未提供。若可存取專案，請自行唯讀盤點；無法確認的資訊必須標為待確認。";

const GOAL_OUTPUT_CONTRACT = `## 最終輸出契約：goal.md

完成內部分析後，直接輸出一份可存成 \`goal.md\` 的 Markdown；不要加前言、分析過程、code fence 或 plan JSON。

必須使用下列結構：

# Goal
## 目標
## 背景與現況
## 範圍
## 非目標
## 限制與相容性
## 完成定義（DoD）
## 人工驗收
## 待確認事項

輸出規則：

- Goal 要保留需求意圖與邊界，具體到能判斷是否完成，但不要提前寫成逐步任務清單。
- 「完成定義」分開列出可由命令／exit code 判斷的機器 DoD；不可自動驗證的項目放「人工驗收」。
- 不得把推測寫成既定事實。沒有待確認事項時寫「無」，不要停下來反問使用者。
- 除非原需求指定其他語言，使用繁體中文。`;

const PLAN_OUTPUT_CONTRACT = `## 最終輸出契約：plan.json

完成內部分析後，只輸出一個合法 JSON array；第一個字元必須是 \`[\`，最後一個字元必須是 \`]\`。不要加前言、分析過程、Markdown code fence、註解或結尾說明。

嚴格 schema：

- 每個元素只能有 \`order\`、\`task\`、選填的 \`ref\`，不得出現其他欄位。
- \`order\` 必須是 integer，從 1 開始且依陣列順序連續遞增，不得重複或跳號。
- \`task\` 必須是非空字串，寫到 fresh-context Agent 不需依賴聊天紀錄就能動工；包含範圍、必要證據、相依條件與可驗證 DoD。
- \`ref\` 只在已有真實分析文件路徑或段落時填字串；無可用來源時整個省略，不得發明檔案。
- 使用合法 JSON 雙引號、正確跳脫，不得有 trailing comma。

拆分規則：

- 依相依順序排列；測試骨架、契約或風險驗證應先於依賴它的實作。
- 每項應是一個 Agent 一輪可合理完成並獨立驗證的垂直成果，不要只按檔案或技術層機械拆分。
- 每個 inventory 項目與需求行為都必須能追到至少一個任務；不能對應者代表計畫仍不完整。
- 人工決策不可偽裝成可執行任務；在相關 task 中明確標示 human gate 與阻擋條件。

合法形狀示意（內容必須改成實際分析結果）：\`[{"order":1,"task":"完成具體工作；DoD：執行指定驗證命令通過","ref":"docs/analysis.md#section"},{"order":2,"task":"完成下一個具體工作；DoD：相關測試通過"}]\``;

export function buildExternalAgentPrompt({
  template,
  mode,
  requirement,
  projectContext
}: {
  template: PromptTemplate;
  mode: PromptTemplateMode;
  requirement: string;
  projectContext: string;
}) {
  // 固定分析核心與輸出契約永遠存在；團隊模板只能補充任務類型指引。
  const requirementText = requirement.trim() || template.requirement_placeholder || DEFAULT_REQUIREMENT;
  const contextText = projectContext.trim() || DEFAULT_CONTEXT;
  const outputContract = mode === "goal" ? GOAL_OUTPUT_CONTRACT : PLAN_OUTPUT_CONTRACT;
  const outputName = mode === "goal" ? "goal.md" : "plan.json";

  return `# 外部 Agent 任務：依需求產生 ${outputName}

你是資深軟體分析與規劃 Agent。請先完整分析需求與可取得的專案證據，再依本文最後的輸出契約產生唯一結果。你的工作是唯讀分析與產出文字，不要修改 repo、建立 commit、執行破壞性命令或自行擴張需求。

## 原始需求

${requirementText}

## 專案／補充上下文

${contextText}

## 共用分析規則

1. 若可存取 repo，先讀實際目錄、設定、入口、呼叫端、測試與文件；重要結論附 \`檔案:行號\`。若無法存取或證據不足，明確標為待確認，不得臆測。
2. 把「全部」「完整」「等價」等字眼展開成可枚舉 inventory；沒有列入 inventory 的項目不得默認為已涵蓋。
3. 對每個行為整理輸入 → 輸出／副作用、驗證、錯誤處理、邊界條件與相容性要求。
4. 明列範圍、非目標、限制、相依項與 human gates；不要擅自加入重寫、最佳化或架構更換。
5. 優先沿用 codebase 既有慣例與真相來源；若發現多套互相衝突的狀態或規格，要指出衝突。
6. DoD 必須可驗證：能自動判斷者寫出實際命令或可觀察條件；視覺、UX、產品決策等另列人工驗收。
7. 在內部完成完整性檢查後再輸出；不要因資訊不完整而停下反問，請保留待確認項與它造成的影響。

## 任務類型：${template.label}

${template.description}

${template.instructions}

${outputContract}
`;
}

export function promptDownloadName(template: PromptTemplate, mode: PromptTemplateMode) {
  // 非安全字元轉成連字號，避免模板 id 產生意外路徑或空檔名。
  const safeId = template.id.replace(/[^a-z0-9._-]+/gi, "-").replace(/^-+|-+$/g, "") || "custom";
  return `${safeId}-${mode}-prompt.md`;
}

export function downloadPromptFile(content: string, filename: string) {
  // 以暫時 Blob URL 觸發本機下載，完成後立即撤銷 URL。
  const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
