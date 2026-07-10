import { useState } from "react";
import { PLAN_TEMPLATE, validatePlan } from "./planValidation";

export default function PlanImportField({
  value,
  onChange,
  startPhase,
  onStartPhaseChange,
  onOpenPromptTemplate,
  promptTemplateAvailable
}: {
  value: string;
  onChange: (value: string) => void;
  startPhase: "plan" | "exec";
  onStartPhaseChange: (value: "plan" | "exec") => void;
  onOpenPromptTemplate: () => void;
  promptTemplateAvailable: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const error = validatePlan(value);
  const copyTemplate = async () => {
    try {
      await navigator.clipboard.writeText(PLAN_TEMPLATE);
      setCopied(true);
    } catch {
      if (!value.trim()) onChange(PLAN_TEMPLATE);
    }
    window.setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="form-field">
      <div className="field-label-row"><label htmlFor="plan-json">匯入 plan.json <span>選填</span></label><span className="field-actions"><button type="button" className="text-button" disabled={!promptTemplateAvailable} onClick={onOpenPromptTemplate}>產生 Plan Prompt</button><button type="button" className="text-button" onClick={copyTemplate}>{copied ? "✅ 已複製" : "複製 JSON 範本"}</button></span></div>
      <textarea id="plan-json" rows={5} value={value} onChange={(event) => onChange(event.target.value)} placeholder="留空＝沿用既有計畫或從零規劃" aria-invalid={!!error} aria-describedby={error ? "plan-error" : undefined} />
      {error && <p id="plan-error" className="field-error" role="alert">{error}</p>}
      {value.trim() && !error && (
        <div className="inline-options">
          <strong>匯入會建立全新 state：</strong>
          <label><input type="radio" name="start-phase" checked={startPhase === "plan"} onChange={() => onStartPhaseChange("plan")} /> 規劃期</label>
          <label><input type="radio" name="start-phase" checked={startPhase === "exec"} onChange={() => onStartPhaseChange("exec")} /> 直接執行期</label>
        </div>
      )}
    </div>
  );
}
