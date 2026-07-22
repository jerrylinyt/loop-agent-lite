/** plan.json 匯入欄位：即時顯示格式錯誤與起始 phase；前端檢查只是回饋，後端仍是最終防線。 */
import { useState } from "react";
import { getPlanBatchPreview, PARALLEL_PLAN_TEMPLATE, PLAN_TEMPLATE, validatePlan } from "./planValidation";

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
  const batchPreview = parallel && value.trim() && !error ? getPlanBatchPreview(value) : "";
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
      <div className="field-label-row"><label htmlFor="plan-json">匯入 plan.json <span>{parallel ? "必填（已凍結）" : "選填"}</span></label><span className="field-actions"><button type="button" className="text-button" disabled={!promptTemplateAvailable} onClick={onOpenPromptTemplate}>產生 Plan Prompt</button><button type="button" className="text-button" onClick={copyTemplate}>{copied ? "已複製" : parallel ? "複製 Parallel 範本" : "複製 JSON 範本"}</button></span></div>
      <textarea id="plan-json" rows={5} value={value} onChange={(event) => onChange(event.target.value)} placeholder={parallel ? "Parallel Loop 必須貼入已凍結的 plan.json" : "留空＝沿用既有計畫或從零規劃"} aria-invalid={!!error} aria-describedby={error ? "plan-error" : parallel ? "plan-mode-help" : undefined} />
      {error && <p id="plan-error" className="field-error" role="alert">{error}</p>}
      {value.trim() && !error && (
        parallel ? (
          <div id="plan-mode-help" className="inline-options">
            <strong>Parallel 固定從 exec 啟動。</strong>
            <span>{batchPreview}</span>
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
