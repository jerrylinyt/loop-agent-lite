/** 結構化 issue 檢視器：標記已讀只更新 watermark，永久清除則要求額外確認。 */
import { useRef, useState } from "react";
import ActionDialog from "../../shared/components/ActionDialog";
import Modal from "../../shared/components/Modal";
import { postJson } from "../../shared/api/client";
import type { Issue } from "../../shared/api/types";
import type { BeginOperation, EndOperation } from "../../shared/operationGate";
import { issueMutationsLocked } from "./issueViewModel";

export default function IssuesModal({
  workspace,
  workspaceGeneration,
  issues,
  unreadIssues,
  readonly,
  beginOperation,
  endOperation,
  navigableWorkspaces = [],
  onNavigateWorkspace,
  onClose,
  onChanged
}: {
  workspace: string;
  workspaceGeneration?: string;
  issues: Issue[];
  unreadIssues: number;
  readonly: boolean;
  beginOperation: BeginOperation;
  endOperation: EndOperation;
  /** 只允許導向目前 workspace projection 中仍存在的 child；cleanup 後保留 track 文字但不留死連結。 */
  navigableWorkspaces?: string[];
  onNavigateWorkspace?: (name: string) => void;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [message, setMessage] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const [updating, setUpdating] = useState(false);
  const updatingPending = useRef(false);
  const mutationsLocked = issueMutationsLocked(issues, readonly);
  const acknowledge = async () => {
    if (updatingPending.current) return;
    const token = beginOperation(`workspace:${workspace}:issues-ack`);
    if (!token) return setMessage("❌ 另一個操作仍在進行中");
    updatingPending.current = true;
    setUpdating(true);
    try {
      const response = await postJson<Record<string, never>>("/api/edit-state", {
        name: workspace, workspace_generation: workspaceGeneration, ack_issues: true
      });
      setMessage(response.error ? `❌ ${response.error}` : "✅ 已標記為已讀，稽核紀錄仍保留");
      if (!response.error) onChanged();
    } finally {
      updatingPending.current = false;
      setUpdating(false);
      endOperation(token);
    }
  };
  const clear = async () => {
    if (updatingPending.current) return;
    const token = beginOperation(`workspace:${workspace}:issues-clear`);
    if (!token) return setMessage("❌ 另一個操作仍在進行中");
    updatingPending.current = true;
    setConfirmClear(false);
    setUpdating(true);
    try {
      const response = await postJson<Record<string, never>>("/api/edit-state", {
        name: workspace, workspace_generation: workspaceGeneration, clear_issues: true
      });
      setMessage(response.error ? `❌ ${response.error}` : "✅ 已清空");
      if (!response.error) onChanged();
    } finally {
      updatingPending.current = false;
      setUpdating(false);
      endOperation(token);
    }
  };
  const requestClose = () => { if (!updatingPending.current) onClose(); };
  return (
    <>
      <Modal title="Issues" description="Agent 回報的結構化問題；標記已讀會保留完整稽核紀錄" closeDisabled={updating} onClose={requestClose} wide footer={(
        <>{!mutationsLocked && unreadIssues > 0 && <button type="button" className="secondary-button" disabled={updating} onClick={() => void acknowledge()}>{updating ? "更新中…" : "標記已讀"}</button>}{!mutationsLocked && issues.length > 0 && <button type="button" className="danger-button" disabled={updating} onClick={() => setConfirmClear(true)}>清空全部</button>}<span role="status" className="muted">{message || `${unreadIssues} 條未讀／保留 ${issues.length} 條${mutationsLocked && issues.some((issue) => issue.synthetic || issue.read_only) ? " · 含唯讀整合診斷" : ""}`}</span></>
      )}>
        <div className="modal-table-scroll">
          <table>
            <thead><tr><th>round</th><th>track</th><th>位置</th><th>內容</th><th>時間</th></tr></thead>
            <tbody>
              {[...issues].reverse().map((issue, index) => (
                <tr key={`${issue.round}-${index}`}><td>{issue.round}</td><td>{issue.track ? (issue.child_workspace && onNavigateWorkspace && navigableWorkspaces.includes(issue.child_workspace) ? <button type="button" className="link-button" onClick={() => onNavigateWorkspace(issue.child_workspace!)}>{issue.track}</button> : <span title={issue.child_workspace ? "來源 child 已清理或目前不存在" : undefined}>{issue.track}{issue.child_workspace && !navigableWorkspaces.includes(issue.child_workspace) ? "（已清理）" : ""}</span>) : "—"}</td><td>{issue.where ?? ""}</td><td><div className="issue-content">{issue.resolved && <span className="chip subdued">已修復</span>}<span>{issue.text}</span></div></td><td className="muted">{(issue.ts ?? "").replace("T", " ")}</td></tr>
              ))}
              {!issues.length && <tr><td colSpan={5} className="table-empty">無 issues</td></tr>}
            </tbody>
          </table>
        </div>
      </Modal>
      {confirmClear && <ActionDialog title="請確認" message="清空全部 issues？此操作無法復原。" confirmLabel="清空" danger onClose={() => setConfirmClear(false)} onConfirm={() => void clear()} />}
    </>
  );
}
