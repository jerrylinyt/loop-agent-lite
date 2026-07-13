/** 個人 repo roots 編輯器：清理空白與重複路徑後交由後端做路徑邊界驗證。 */
import { useRef, useState } from "react";
import Modal from "../../shared/components/Modal";
import { postJson } from "../../shared/api/client";
import type { ConfigResponse } from "../../shared/api/types";
import type { BeginOperation, EndOperation } from "../../shared/operationGate";

export default function RepoRootsModal({ config, beginOperation, endOperation, onClose, onSaved }: {
  config: ConfigResponse;
  beginOperation: BeginOperation;
  endOperation: EndOperation;
  onClose: () => void;
  onSaved: (config: ConfigResponse) => void;
}) {
  const [roots, setRoots] = useState<string[]>(() => [...(config.repo_roots ?? ["~/IdeaProjects"])]);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const savingPending = useRef(false);

  const save = async () => {
    if (savingPending.current) return;
    const token = beginOperation("launcher:repo-roots-save");
    if (!token) return;
    savingPending.current = true;
    setSaving(true);
    setMessage("儲存並重新掃描中…");
    try {
      const response = await postJson<ConfigResponse>("/api/edit-repo-roots", { repo_roots: roots });
      if (response.error) {
        setMessage(`❌ ${response.error}`);
        return;
      }
      onSaved(response);
      onClose();
    } finally {
      savingPending.current = false;
      setSaving(false);
      endOperation(token);
    }
  };
  const requestClose = () => { if (!savingPending.current) onClose(); };

  return (
    <Modal title="Code Repo Roots 管理" description={`設定 dashboard 掃描 Git repo 的根目錄；只寫個人設定：${config.personal_config_path ?? config.config_path ?? "dashboard.config.local.json"}`} closeDisabled={saving} onClose={requestClose} footer={
      <><button type="button" className="secondary-button" disabled={saving} onClick={requestClose}>取消</button><button type="button" className="primary-button" disabled={saving} onClick={() => void save()}>{saving ? "重新掃描中…" : "儲存並重新掃描"}</button><span role="status">{message}</span></>
    }>
      <fieldset className="launcher-fieldset" disabled={saving}>
        <div className="repo-root-heading"><p>可加入多個目錄，支援 <code>~</code> 與 <code>$HOME</code>。每個 root 本身或其下一層 Git repo 會出現在下拉選單。</p><button type="button" className="secondary-button" onClick={() => setRoots((items) => [...items, ""])}>＋ 新增 Root</button></div>
        <div className="path-editor-list">
          {roots.map((root, index) => <div className="path-editor-row" key={index}><input aria-label={`Repo root ${index + 1}`} value={root} onChange={(event) => setRoots((items) => items.map((item, itemIndex) => itemIndex === index ? event.target.value : item))} placeholder="~/IdeaProjects" /><button type="button" className="danger-button" disabled={roots.length <= 1} onClick={() => setRoots((items) => items.filter((_, itemIndex) => itemIndex !== index))}>移除</button></div>)}
        </div>
        <p className="cli-path-tip">目前掃描到 {config.repos.length} 個 repo。儲存後下拉選單會立即更新。</p>
      </fieldset>
    </Modal>
  );
}
