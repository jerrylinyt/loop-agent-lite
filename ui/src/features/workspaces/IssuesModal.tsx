import { useState } from "react";
import ActionDialog from "../../shared/components/ActionDialog";
import Modal from "../../shared/components/Modal";
import { postJson } from "../../shared/api/client";
import type { Issue } from "../../shared/api/types";

export default function IssuesModal({
  workspace,
  issues,
  readonly,
  onClose,
  onChanged
}: {
  workspace: string;
  issues: Issue[];
  readonly: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [message, setMessage] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const clear = async () => {
    setConfirmClear(false);
    const response = await postJson<Record<string, never>>("/api/edit-state", { name: workspace, clear_issues: true });
    setMessage(response.error ? `❌ ${response.error}` : "✅ 已清空");
    if (!response.error) onChanged();
  };
  return (
    <>
      <Modal title="Issues" description="Agent 回報的結構化問題，不影響計數" onClose={onClose} wide footer={!readonly && (
        <><button type="button" className="danger-button" onClick={() => setConfirmClear(true)}>清空全部</button><span role="status">{message}</span></>
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
