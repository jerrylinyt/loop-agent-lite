/** 外部 Agent prompt 產生器：在瀏覽器組合、預覽與下載文字，不接觸 repo 或 workspace。 */
import { useMemo, useState } from "react";
import Modal from "../../shared/components/Modal";
import type { PromptTemplate, PromptTemplateBundle } from "../../shared/api/types";
import {
  buildExternalAgentPrompt,
  downloadPromptFile,
  promptRequirementSeed,
  promptDownloadName,
  type PromptTemplateMode
} from "./promptTemplateBuilder";

export default function PromptTemplateModal({
  templates,
  bundle,
  warnings,
  projectConfigPath,
  initialMode,
  onClose
}: {
  templates: PromptTemplate[];
  bundle: PromptTemplateBundle;
  warnings?: string[];
  projectConfigPath?: string;
  initialMode: PromptTemplateMode;
  onClose: () => void;
}) {
  const [mode, setMode] = useState<PromptTemplateMode>(initialMode);
  const [templateId, setTemplateId] = useState(templates[0]?.id ?? "");
  const [requirement, setRequirement] = useState(() => promptRequirementSeed(templates[0]));
  // 只要使用者親手改過需求欄就不再被切換模板覆蓋；用旗標而非字串比較，避免「手動輸入剛好
  // 等於範例文字」被誤判成未修改。
  const [requirementTouched, setRequirementTouched] = useState(false);
  const [projectContext, setProjectContext] = useState("");
  const [message, setMessage] = useState("");
  const template = templates.find((item) => item.id === templateId) ?? templates[0];

  const selectTemplate = (id: string) => {
    const next = templates.find((item) => item.id === id);
    if (!requirement.trim() || !requirementTouched) {
      setRequirement(promptRequirementSeed(next));
    }
    setTemplateId(id);
  };
  const groups = useMemo(() => {
    const grouped = new Map<string, PromptTemplate[]>();
    for (const item of templates) {
      const current = grouped.get(item.category) ?? [];
      current.push(item);
      grouped.set(item.category, current);
    }
    return [...grouped.entries()];
  }, [templates]);
  const prompt = template
    ? buildExternalAgentPrompt({ template, bundle, mode, requirement, projectContext })
    : "";
  const hasRequirement = !!requirement.trim();
  const requirementIsUntouchedSeed = hasRequirement && !requirementTouched;
  const outputName = mode === "goal" ? "goal.md" : "plan.json";

  const copyPrompt = async () => {
    try {
      await navigator.clipboard.writeText(prompt);
      setMessage("✅ Prompt 已複製");
    } catch {
      setMessage("❌ 無法寫入剪貼簿，請從右側預覽手動複製");
    }
  };

  const downloadPrompt = () => {
    if (!template) return;
    downloadPromptFile(prompt, promptDownloadName(template, mode));
    setMessage(`✅ 已下載 ${promptDownloadName(template, mode)}`);
  };

  const footer = (
    <>
      <button type="button" className="secondary-button" onClick={onClose}>← 上一頁</button>
      <button type="button" className="secondary-button" disabled={!template || !hasRequirement || !prompt} onClick={() => void copyPrompt()}>複製 Prompt</button>
      <button type="button" className="primary-button" disabled={!template || !hasRequirement || !prompt} onClick={downloadPrompt}>下載 .md</button>
      <span className="inline-message" role="status" aria-live="polite">
        {message || (requirementIsUntouchedSeed ? "⚠ 需求仍是模板範例，下載前請改成實際需求" : "")}
      </span>
    </>
  );

  return (
    <Modal
      title="外部 Agent 產生器 Prompt"
      description={`複製後可直接交給 Agent 分析並產生 ${outputName}；這裡不會修改 repo 或 workspace`}
      onClose={onClose}
      extraWide
      footer={footer}
    >
      <div className="prompt-template-toolbar">
        <div className="segmented-tabs prompt-mode-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={mode === "goal"}
            className={mode === "goal" ? "active" : ""}
            onClick={() => { setMode("goal"); setMessage(""); }}
            data-autofocus
          >
            Goal 產生器 Prompt
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "plan"}
            className={mode === "plan" ? "active" : ""}
            onClick={() => { setMode("plan"); setMessage(""); }}
          >
            Plan 拆分模板
          </button>
        </div>
        <label>
          任務類型
          <select aria-label="Prompt 任務類型" value={template?.id ?? ""} onChange={(event) => selectTemplate(event.target.value)}>
            {groups.map(([category, items]) => (
              <optgroup key={category} label={category}>
                {items.map((item) => <option key={item.id} value={item.id}>{item.label}{item.source === "team" ? "（團隊）" : ""}</option>)}
              </optgroup>
            ))}
          </select>
        </label>
      </div>

      {!!warnings?.length && (
        <div className="prompt-template-warning" role="alert">
          <strong>部分團隊模板未載入</strong>
          <ul>{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
        </div>
      )}

      <div className="prompt-template-grid">
        <section className="prompt-template-editor form-grid" aria-label="Prompt 輸入">
          {template ? (
            <div className="prompt-template-summary">
              <div>
                <strong>{template.label}</strong>
                <span className={`prompt-source-badge ${template.source}`}>{template.source === "team" ? "團隊" : "內建"}</span>
              </div>
              <p>{template.description}</p>
            </div>
          ) : <div className="loading-state error">沒有可用的 Prompt 模板</div>}

          <label htmlFor="prompt-requirement">
            原始需求 <span className="label-help">必填；已預填範例，請改成實際需求再產生</span>
            <textarea
              id="prompt-requirement"
              rows={8}
              required
              value={requirement}
              onChange={(event) => { setRequirement(event.target.value); setRequirementTouched(true); }}
              placeholder={template?.requirement_placeholder}
            />
          </label>
          <label htmlFor="prompt-project-context">
            已知專案資訊／限制 <span className="label-help">選填；只填 Agent 無法從 repo 直接確認的背景</span>
            <textarea
              id="prompt-project-context"
              rows={5}
              value={projectContext}
              onChange={(event) => setProjectContext(event.target.value)}
              placeholder="例：必須相容尚未搬移的舊版 consumer；正式環境只能在指定維護窗口切換"
            />
          </label>

          <details className="team-template-help">
            <summary>團隊成員如何新增模板</summary>
            <p>將下列結構加入 Git 共用設定 <code>{projectConfigPath || "dashboard.config.shared.json"}</code>。重新開啟啟動視窗後就會出現在任務類型清單。</p>
            <pre>{bundle.team_template_example}</pre>
            <p>團隊只維護任務專屬指引；共用分析規則及 Goal／Plan 輸出契約由系統統一套用。</p>
          </details>
        </section>

        <section className="prompt-preview-panel" aria-label="Prompt 預覽">
          <header>
            <div>
              <strong>即時預覽</strong>
              <span>下載後交給可讀取專案的外部 Agent</span>
            </div>
            <code>{template ? promptDownloadName(template, mode) : ""}</code>
          </header>
          <pre data-testid="prompt-template-preview">{prompt}</pre>
        </section>
      </div>
    </Modal>
  );
}
