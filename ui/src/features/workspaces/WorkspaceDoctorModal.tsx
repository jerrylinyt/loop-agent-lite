import { useMemo, useState } from "react";
import Modal from "../../shared/components/Modal";
import type { WorkspaceSummary } from "../../shared/api/types";
import { workspaceDiagnostics } from "./workspaceDiagnostics";

export default function WorkspaceDoctorModal({ workspaces, onClose, onSelect }: {
  workspaces: WorkspaceSummary[];
  onClose: () => void;
  onSelect: (name: string) => void;
}) {
  const [query, setQuery] = useState("");
  const diagnosed = useMemo(() => workspaces.map((workspace) => ({
    workspace,
    diagnostics: workspaceDiagnostics(workspace)
  })).filter((item) => item.diagnostics.length), [workspaces]);
  const normalized = query.trim().toLowerCase();
  const visible = diagnosed.filter(({ workspace, diagnostics }) => !normalized ||
    workspace.name.toLowerCase().includes(normalized) ||
    diagnostics.some((item) => `${item.title} ${item.evidence} ${item.recommendation}`.toLowerCase().includes(normalized)));
  const issueCount = diagnosed.reduce((sum, item) => sum + item.diagnostics.length, 0);

  return <Modal title="Workspace Doctor" description={`問題中心 · ${diagnosed.length} 個 workspace、${issueCount} 項可處理訊號`} onClose={onClose} extraWide>
    <div className="doctor-toolbar">
      <input type="search" aria-label="搜尋問題中心" placeholder="搜尋 workspace、原因或建議…" value={query} onChange={(event) => setQuery(event.target.value)} data-autofocus />
      <span className="muted">顯示 {visible.length} / {diagnosed.length}</span>
    </div>
    <div className="doctor-list">
      {visible.map(({ workspace, diagnostics }) => <section className="doctor-workspace" key={workspace.name}>
        <header>
          <div><strong>{workspace.name}</strong><span className={`phase-badge phase-${workspace.phase ?? "unknown"}`}>{workspace.phase ?? "unknown"}</span></div>
          <button type="button" className="primary-button compact-button" onClick={() => onSelect(workspace.name)}>前往處理</button>
        </header>
        <div className="doctor-diagnostics">
          {diagnostics.map((diagnostic) => <article className={`doctor-diagnostic ${diagnostic.severity}`} key={diagnostic.id}>
            <div><strong>{diagnostic.title}</strong><span>{diagnostic.severity === "error" ? "需立即確認" : diagnostic.severity === "warning" ? "需要處理" : "建議檢查"}</span></div>
            <p>{diagnostic.evidence}</p>
            <small>建議：{diagnostic.recommendation}</small>
          </article>)}
        </div>
      </section>)}
      {!visible.length && <div className="loading-state">{diagnosed.length ? "沒有符合搜尋的問題" : "目前沒有需要處理的 workspace"}</div>}
    </div>
  </Modal>;
}
