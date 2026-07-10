import { useState } from "react";
import ActionDialog from "../../shared/components/ActionDialog";
import Modal from "../../shared/components/Modal";
import { postJson } from "../../shared/api/client";
import type { Issue } from "../../shared/api/types";

export default function IssuesModal({
  workspace,
  issues,
  unreadIssues,
  readonly,
  onClose,
  onChanged
}: {
  workspace: string;
  issues: Issue[];
  unreadIssues: number;
  readonly: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [message, setMessage] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const [updating, setUpdating] = useState(false);
  const acknowledge = async () => {
    setUpdating(true);
    try {
      const response = await postJson<Record<string, never>>("/api/edit-state", { name: workspace, ack_issues: true });
      setMessage(response.error ? `❌ ${response.error}` : "✅ 已標記為已讀，稽核紀錄仍保留");
      if (!response.error) onChanged();
    } finally {
      setUpdating(false);
    }
  };
  const clear = async () => {
    setConfirmClear(false);
    setUpdating(true);
    try {
      const response = await postJson<Record<string, never>>("/api/edit-state", { name: workspace, clear_issues: true });
      setMessage(response.error ? `❌ ${response.error}` : "✅ 已清空");
      if (!response.error) onChanged();
    } finally {
      setUpdating(false);
    }
  };
  return (
    <>
      <Modal title="Issues" description="Agent 回報的結構化問題；標記已讀會保留完整稽核紀錄" onClose={onClose} wide footer={(
        <>{!readonly && unreadIssues > 0 && <button type="button" className="secondary-button" disabled={updating} onClick={() => void acknowledge()}>{updating ? "更新中…" : "標記已讀"}</button>}{!readonly && issues.length > 0 && <button type="button" className="danger-button" disabled={updating} onClick={() => setConfirmClear(true)}>清空全部</button>}<span role="status" className="muted">{message || `${unreadIssues} 條未讀／保留 ${issues.length} 條`}</span></>
      )}>
        <div className="modal-table-scroll">
          <table>
            <thead><tr><th>round</th><th>位置</th><th>內容</th><th>時間</th></tr></thead>
            <tbody>
              {[...issues].reverse().map((issue, index) => (
                <tr key={`${issue.round}-${index}`}><td>{issue.round}</td><td>{issue.where ?? ""}</td><td>{issue.text}</td><td className="muted">{(issue.ts ?? "").replace("T", " ")}</td></tr>
              ))}
              {!issues.length && <tr><td colSpan={4} className="table-empty">無 issues</td></tr>}
            </tbody>
          </table>
        </div>
      </Modal>
      {confirmClear && <ActionDialog title="請確認" message="清空全部 issues？此操作無法復原。" confirmLabel="清空" danger onClose={() => setConfirmClear(false)} onConfirm={() => void clear()} />}
    </>
  );
}
