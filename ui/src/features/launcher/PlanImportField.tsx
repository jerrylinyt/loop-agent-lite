/** plan.json 匯入欄位：即時顯示格式錯誤與起始 phase；前端檢查只是回饋，後端仍是最終防線。 */
import { useState } from "react";
import { getPlanBatchAnalysis, PARALLEL_PLAN_TEMPLATE, PLAN_TEMPLATE, validatePlan } from "./planValidation";

export default function PlanImportField({
  value,
  onChange,
  startPhase,
  onStartPhaseChange,
  onOpenPromptTemplate,
  promptTemplateAvailable,
  parallel = false
}: {
  value: string;
  onChange: (value: string) => void;
  startPhase: "plan" | "exec";
  onStartPhaseChange: (value: "plan" | "exec") => void;
  onOpenPromptTemplate: () => void;
  promptTemplateAvailable: boolean;
  parallel?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const error = validatePlan(value, { required: parallel, allowStack: parallel });
  const template = parallel ? PARALLEL_PLAN_TEMPLATE : PLAN_TEMPLATE;
  const batchAnalysis = parallel && value.trim() && !error ? getPlanBatchAnalysis(value) : null;
  const copyTemplate = async () => {
    try {
      await navigator.clipboard.writeText(template);
      setCopied(true);
    } catch {
      if (!value.trim()) onChange(template);
    }
    window.setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="form-field">
      <div className="field-label-row"><label htmlFor="plan-json">匯入 plan.json <span>{parallel ? "必填（已凍結）" : "選填"}</span></label><span className="field-actions"><button type="button" className="text-button" disabled={!promptTemplateAvailable} onClick={onOpenPromptTemplate}>{parallel ? "產生基礎 Plan Prompt（不含 stack）" : "產生 Plan Prompt"}</button><button type="button" className="text-button" onClick={copyTemplate}>{copied ? "已複製" : parallel ? "複製 Parallel 範本" : "複製 JSON 範本"}</button></span></div>
      <textarea id="plan-json" rows={5} value={value} onChange={(event) => onChange(event.target.value)} placeholder={parallel ? "Parallel Loop 必須貼入已凍結的 plan.json" : "留空＝沿用既有計畫或從零規劃"} aria-invalid={!!error} aria-describedby={error ? "plan-error" : parallel ? "plan-mode-help" : undefined} />
      {parallel && (
        <div className="parallel-plan-guidance" data-testid="parallel-plan-review-guidance">
          <strong>先產生基礎 Plan，再由人類標註 stack。</strong>
          <p>Agent 只輸出 <code>order/task/ref</code>；人工讀完任務邊界後，才為可安全同批執行的連續 task 加上相同正整數 <code>stack</code>。</p>
          <details>
            <summary>可標成同一 stack 的必要條件</summary>
            <ul>
              <li>working set、schema 與生成物不重疊，也沒有語意或資料前置依賴。</li>
              <li>validator 使用的 port、DB、cache、lock、外部服務與全域環境已隔離。</li>
              <li>每項都能獨立驗證；任一條不確定就不標 stack、維持串行。</li>
            </ul>
          </details>
        </div>
      )}
      {error && <p id="plan-error" className="field-error" role="alert">{error}</p>}
      {value.trim() && !error && (
        parallel ? (
          <div id="plan-mode-help" className="inline-options parallel-plan-summary">
            <strong>Parallel 固定從 exec 啟動。</strong>
            <span>{batchAnalysis?.preview}</span>
            {batchAnalysis && !batchAnalysis.hasParallelBatch && (
              <strong data-testid="parallel-plan-concurrency-warning">目前沒有可並行的 batch；所有任務會依序執行。</strong>
            )}
          </div>
        ) : (
          <div className="inline-options">
            <strong>匯入會建立全新 state：</strong>
            <label><input type="radio" name="start-phase" checked={startPhase === "plan"} onChange={() => onStartPhaseChange("plan")} /> 規劃期</label>
            <label><input type="radio" name="start-phase" checked={startPhase === "exec"} onChange={() => onStartPhaseChange("exec")} /> 直接執行期</label>
          </div>
        )
      )}
    </div>
  );
}
