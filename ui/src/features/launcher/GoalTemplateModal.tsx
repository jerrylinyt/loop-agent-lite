/** Goal 成果參考：沿用 Goal 產生器的任務類型，呈現可直接複製的八段 goal.md 骨架。 */
import { useMemo, useState } from "react";
import Modal from "../../shared/components/Modal";
import type { PromptTemplate, PromptTemplateBundle } from "../../shared/api/types";
import {
  buildGoalArtifactTemplate,
  downloadPromptFile,
  goalTemplateDownloadName
} from "./promptTemplateBuilder";

export default function GoalTemplateModal({ templates, bundle, warnings, onClose }: {
  templates: PromptTemplate[];
  bundle: PromptTemplateBundle;
  warnings?: string[];
  onClose: () => void;
}) {
  const [templateId, setTemplateId] = useState(templates[0]?.id ?? "");
  const [message, setMessage] = useState("");
  const template = templates.find((item) => item.id === templateId) ?? templates[0];
  const groups = useMemo(() => {
    const grouped = new Map<string, PromptTemplate[]>();
    for (const item of templates) {
      const current = grouped.get(item.category) ?? [];
      current.push(item);
      grouped.set(item.category, current);
    }
    return [...grouped.entries()];
  }, [templates]);
  const content = template ? buildGoalArtifactTemplate(template, bundle) : "";
  const filename = template ? goalTemplateDownloadName(template) : "goal-template.md";

  const copyTemplate = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setMessage("✅ goal.md 成果模板已複製");
    } catch {
      setMessage("❌ 無法寫入剪貼簿，請從預覽手動複製");
    }
  };

  const downloadTemplate = () => {
    if (!content) return;
    downloadPromptFile(content, filename);
    setMessage(`✅ 已下載 ${filename}`);
  };

  return (
    <Modal
      title="Goal 成果模板"
      description={`提供與 Goal 產生器相同的 ${templates.length} 種任務類型；內容是八段 goal.md 參考骨架，不會修改 repo 或 workspace`}
      onClose={onClose}
      extraWide
      footer={(
        <div className="goal-template-footer">
          <button type="button" className="secondary-button" onClick={onClose}>← 上一頁</button>
          <button type="button" className="secondary-button" disabled={!content} onClick={() => void copyTemplate()}>複製 goal.md</button>
          <button type="button" className="primary-button" disabled={!content} onClick={downloadTemplate}>下載 {filename}</button>
          <span className="inline-message" role="status" aria-live="polite">{message}</span>
        </div>
      )}
    >
      <div className="prompt-template-toolbar goal-template-toolbar">
        <label>
          Goal 模板類型
          <select
            data-autofocus
            aria-label="Goal 模板類型"
            value={template?.id ?? ""}
            onChange={(event) => { setTemplateId(event.target.value); setMessage(""); }}
          >
            {groups.map(([category, items]) => (
              <optgroup key={category} label={category}>
                {items.map((item) => <option key={item.id} value={item.id}>{item.label}{item.source === "team" ? "（團隊）" : ""}</option>)}
              </optgroup>
            ))}
          </select>
        </label>
        {template && (
          <div className="prompt-template-summary">
            <div>
              <strong>{template.label}</strong>
              <span className={`prompt-source-badge ${template.source}`}>{template.source === "team" ? "團隊" : "內建"}</span>
            </div>
            <p>{template.description}</p>
          </div>
        )}
      </div>

      {!!warnings?.length && (
        <div className="prompt-template-warning" role="alert">
          <strong>部分團隊模板未載入</strong>
          <ul>{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
        </div>
      )}

      <section className="prompt-preview-panel goal-template-preview" aria-label="Goal 成果模板預覽">
        <header>
          <div>
            <strong>最終成果結構參考</strong>
            <span>依選取的任務類型帶入分析重點；方括號內容需依實際需求與 repo 證據改寫</span>
          </div>
          <code>{filename}</code>
        </header>
        <pre data-testid="goal-template-preview">{content}</pre>
      </section>
    </Modal>
  );
}
