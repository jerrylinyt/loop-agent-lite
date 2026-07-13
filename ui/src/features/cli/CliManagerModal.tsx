/** 個人 Agent CLI 設定編輯器：驗證欄位並透過後端儲存，不修改團隊共用設定。 */
import { useRef, useState } from "react";
import { postJson } from "../../shared/api/client";
import type { ConfigResponse, SelectCommand } from "../../shared/api/types";
import CommandTestDialog, { type CommandTestState } from "../../shared/components/CommandTestDialog";
import Modal from "../../shared/components/Modal";
import type { BeginOperation, EndOperation } from "../../shared/operationGate";

interface AgentTestResponse { ok?: boolean; rc?: number; timeout?: boolean; output?: string }

export default function CliManagerModal({
  config,
  repo,
  workspace,
  workspaceGeneration,
  runId,
  beginOperation,
  endOperation,
  onClose,
  onSaved
}: {
  config: ConfigResponse;
  repo: string;
  workspace?: string;
  workspaceGeneration?: string;
  /** fleet-parent 的 CLI 測試同樣是 run-bound mutation，避免舊頁面操作同名新 run。 */
  runId?: string | null;
  beginOperation: BeginOperation;
  endOperation: EndOperation;
  onClose: () => void;
  onSaved: (config: ConfigResponse) => void;
}) {
  const [agents, setAgents] = useState<SelectCommand[]>(() => config.agent_cmds.map((item) => ({ ...item })));
  const [paths, setPaths] = useState<string[]>(() => [...(config.extra_path_dirs ?? [])]);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const [test, setTest] = useState<CommandTestState | null>(null);

  const updateAgent = (index: number, patch: Partial<SelectCommand>) => {
    setAgents((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  };

  const testAgent = async (agent: SelectCommand) => {
    if (pendingRef.current) return;
    const token = beginOperation(`cli:${workspace ?? repo}:test`);
    if (!token) return;
    pendingRef.current = true;
    setPending(true);
    setTest({ loading: true });
    try {
      const response = await postJson<AgentTestResponse>("/api/test-cli", {
        ...(workspace ? { name: workspace } : { repo }),
        ...(workspace && runId ? { run_id: runId } : {}),
        ...(workspace && !runId ? { workspace_generation: workspaceGeneration } : {}),
        agent_cmd: agent.cmd,
        extra_path_dirs: paths
      });
      if (response.error) {
        setTest({ loading: false, ok: false, text: `❌ ${response.error}`, output: "" });
        return;
      }
      setTest({
        loading: false,
        ok: !!response.ok,
        text: response.timeout ? "❌ 執行逾時（60 秒）" : response.ok ? `✅ Agent CLI 完成（exit ${response.rc ?? 0}）` : `❌ Agent CLI 失敗（exit ${response.rc ?? "?"}）`,
        output: response.output ?? ""
      });
    } finally {
      pendingRef.current = false;
      setPending(false);
      endOperation(token);
    }
  };

  const save = async () => {
    if (pendingRef.current) return;
    const token = beginOperation("cli:config-save");
    if (!token) return;
    pendingRef.current = true;
    setPending(true);
    setSaving(true);
    setMessage("儲存中…");
    try {
      const response = await postJson<ConfigResponse>("/api/edit-cli-config", {
        agent_cmds: agents,
        extra_path_dirs: paths
      });
      if (response.error) {
        setMessage(`❌ ${response.error}`);
        return;
      }
      onSaved(response);
      onClose();
    } finally {
      pendingRef.current = false;
      setPending(false);
      setSaving(false);
      endOperation(token);
    }
  };
  const requestClose = () => { if (!pendingRef.current) onClose(); };

  return (
    <Modal title="Agent CLI 管理" description={`新增、修改、刪除與測試 CLI；只寫個人設定：${config.personal_config_path ?? config.config_path ?? "dashboard.config.local.json"}`} closeDisabled={pending} onClose={requestClose} wide footer={
      <><button type="button" className="secondary-button" disabled={pending} onClick={requestClose}>取消</button><button type="button" className="primary-button" disabled={pending} onClick={() => void save()}>{saving ? "儲存中…" : "儲存 CLI 設定"}</button><span role="status">{message}</span></>
    }>
      <fieldset className="launcher-fieldset" disabled={pending}>
        <section className="cli-manager-section">
          <div className="cli-manager-heading"><div><h3>Agent CLI</h3><p>command 會以固定 prompt「test」執行確認。</p></div><button type="button" className="secondary-button" onClick={() => setAgents((items) => [...items, { label: "", cmd: "" }])}>＋ 新增 CLI</button></div>
          <div className="cli-editor-list">
            {agents.map((agent, index) => (
              <div className="cli-editor-row" key={index}>
                <label>名稱<input aria-label={`CLI ${index + 1} 名稱`} value={agent.label} onChange={(event) => updateAgent(index, { label: event.target.value })} placeholder="例如 Claude" /></label>
                <label>Command<input aria-label={`CLI ${index + 1} Command`} value={agent.cmd} onChange={(event) => updateAgent(index, { cmd: event.target.value })} placeholder="例如 claude --model haiku -p" /></label>
                <div className="cli-row-actions"><button type="button" className="secondary-button" disabled={!repo || !agent.cmd.trim()} onClick={() => void testAgent(agent)}>執行測試</button><button type="button" className="danger-button" disabled={agents.length <= 1} onClick={() => setAgents((items) => items.filter((_, itemIndex) => itemIndex !== index))}>刪除</button></div>
              </div>
            ))}
          </div>
        </section>
        <section className="cli-manager-section">
          <div className="cli-manager-heading"><div><h3>額外 PATH 目錄</h3><p>GUI／IDE 不一定載入 shell profile。支援 <code>~</code>、<code>$HOME</code>，例如 <code>~/.local/bin</code>。</p></div><button type="button" className="secondary-button" onClick={() => setPaths((items) => [...items, ""])}>＋ 新增 PATH</button></div>
          <div className="path-editor-list">
            {paths.map((path, index) => <div className="path-editor-row" key={index}><input aria-label={`PATH 目錄 ${index + 1}`} value={path} onChange={(event) => setPaths((items) => items.map((item, itemIndex) => itemIndex === index ? event.target.value : item))} placeholder="~/.local/bin" /><button type="button" className="danger-button" onClick={() => setPaths((items) => items.filter((_, itemIndex) => itemIndex !== index))}>移除</button></div>)}
            {!paths.length && <p className="empty-inline">沒有額外 PATH，僅使用 dashboard 啟動環境。</p>}
          </div>
          <p className="cli-path-tip">找不到 CLI 時，在終端執行 <code>command -v claude</code>，把輸出檔案的所在目錄加入這裡。</p>
        </section>
      </fieldset>
      {test && <CommandTestDialog state={test} onClose={() => { if (!pendingRef.current) setTest(null); }} />}
    </Modal>
  );
}
