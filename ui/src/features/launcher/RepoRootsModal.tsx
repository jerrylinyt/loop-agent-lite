/** 個人 repo roots 編輯器：清理空白與重複路徑後交由後端做路徑邊界驗證。 */
import { useState } from "react";
import Modal from "../../shared/components/Modal";
import { postJson } from "../../shared/api/client";
import type { ConfigResponse } from "../../shared/api/types";

export default function RepoRootsModal({ config, onClose, onSaved }: {
  config: ConfigResponse;
  onClose: () => void;
  onSaved: (config: ConfigResponse) => void;
}) {
  const [roots, setRoots] = useState<string[]>(() => [...(config.repo_roots ?? ["~/IdeaProjects"])]);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    setMessage("儲存並重新掃描中…");
    const response = await postJson<ConfigResponse>("/api/edit-repo-roots", { repo_roots: roots });
    setSaving(false);
    if (response.error) {
      setMessage(`錯誤：${response.error}`);
      return;
    }
    onSaved(response);
    onClose();
  };

  return (
    <Modal title="Code Repo Roots 管理" description={`設定 dashboard 掃描 Git repo 的根目錄；只寫個人設定：${config.personal_config_path ?? config.config_path ?? "dashboard.config.local.json"}`} onClose={onClose} footer={
      <><button type="button" className="secondary-button" onClick={onClose}>取消</button><button type="button" className="primary-button" disabled={saving} onClick={() => void save()}>{saving ? "重新掃描中…" : "儲存並重新掃描"}</button><span role="status">{message}</span></>
    }>
      <div className="repo-root-heading"><p>可加入多個目錄，支援 <code>~</code> 與 <code>$HOME</code>。每個 root 本身或其下一層 Git repo 會出現在下拉選單。</p><button type="button" className="secondary-button" onClick={() => setRoots((items) => [...items, ""])}>＋ 新增 Root</button></div>
      <div className="path-editor-list">
        {roots.map((root, index) => <div className="path-editor-row" key={index}><input aria-label={`Repo root ${index + 1}`} value={root} onChange={(event) => setRoots((items) => items.map((item, itemIndex) => itemIndex === index ? event.target.value : item))} placeholder="~/IdeaProjects" /><button type="button" className="danger-button" disabled={roots.length <= 1} onClick={() => setRoots((items) => items.filter((_, itemIndex) => itemIndex !== index))}>移除</button></div>)}
      </div>
      <p className="cli-path-tip">目前掃描到 {config.repos.length} 個 repo。儲存後下拉選單會立即更新。</p>
    </Modal>
  );
}
