/** 停止狀態下的 workspace 設定編輯器：載入可選命令、追蹤非同步測試，儲存時由後端重驗數值與白名單。 */
import { useEffect, useRef, useState } from "react";
import CliManagerModal from "../cli/CliManagerModal";
import Modal from "../../shared/components/Modal";
import { getJson, postJson } from "../../shared/api/client";
import useStaleGuard from "../../shared/hooks/useStaleGuard";
import type { ConfigResponse, DashboardConfig } from "../../shared/api/types";
import type { BeginOperation, EndOperation } from "../../shared/operationGate";

interface ValidateResponse { ok?: boolean; rc?: number; timeout?: boolean; timeout_seconds?: number; tail?: string }

export default function ConfigModal({
  workspace,
  workspaceGeneration,
  config,
  parallel = false,
  runId,
  operationPending,
  beginOperation,
  endOperation,
  onClose,
  onChanged
}: {
  workspace: string;
  workspaceGeneration?: string;
  config: DashboardConfig;
  parallel?: boolean;
  runId?: string | null;
  operationPending: boolean;
  beginOperation: BeginOperation;
  endOperation: EndOperation;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [available, setAvailable] = useState<ConfigResponse | null>(null);
  const [agentIndex, setAgentIndex] = useState("");
  const [agentSelectionRequired, setAgentSelectionRequired] = useState(false);
  const [draft, setDraft] = useState({
    validate_cmd: config.validate_cmd ?? "",
    flag_threshold: config.flag_threshold ?? 10,
    done_threshold: config.done_threshold ?? 3,
    round_timeout: config.round_timeout ?? 30,
    agent_backoff_max: config.agent_backoff_max ?? 60,
    validate_timeout: config.validate_timeout ?? 120,
    red_limit: config.red_limit ?? 20,
    stall_limit: config.stall_limit ?? 300
  });
  // 布林開關與數字欄位分開保存，numberField 的 draft 型別維持 string|number。
  const [pauseAfterPlan, setPauseAfterPlan] = useState(config.pause_after_plan ?? false);
  const [maxParallel, setMaxParallel] = useState(config.max_parallel ?? 4);
  const [maxChildRestarts, setMaxChildRestarts] = useState(config.max_child_restarts ?? 0);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const savingPending = useRef(false);
  const [validating, setValidating] = useState(false);
  const validatingPending = useRef(false);
  const [validateResult, setValidateResult] = useState<{ ok: boolean; text: string; tail: string } | null>(null);
  const [cliManagerOpen, setCliManagerOpen] = useState(false);
  const validateGuard = useStaleGuard();

  useEffect(() => {
    void getJson<ConfigResponse>("/api/config").then((response) => {
      setAvailable(response);
      if (!response) return;
      setAgentSelectionRequired(!!config.agent_cmd && !response.agent_cmds.some((agent) => agent.cmd === config.agent_cmd));
    });
  }, [config.agent_cmd]);
  useEffect(() => {
    validateGuard.cancelPending();
    setValidateResult(null);
  }, [draft.validate_cmd, draft.validate_timeout, validateGuard]);

  const save = async () => {
    if (savingPending.current) return;
    if (agentSelectionRequired && agentIndex === "") {
      setMessage("❌ 原 Agent CLI 已移除，請明確選擇目前可用的 Agent 命令");
      return;
    }
    const token = beginOperation(`workspace:${workspace}:config`);
    if (!token) {
      setMessage("❌ 另一個操作仍在進行中");
      return;
    }
    savingPending.current = true;
    setSaving(true);
    setMessage("儲存中…");
    try {
      const body: Record<string, string | number | boolean> = {
        name: workspace, ...draft, pause_after_plan: pauseAfterPlan,
        ...(parallel ? { run_id: runId ?? "", max_parallel: maxParallel, max_child_restarts: maxChildRestarts } : {}),
        ...(!parallel ? { workspace_generation: workspaceGeneration ?? "" } : {}),
      };
      if (agentIndex !== "") body.agent_idx = +agentIndex;
      const response = await postJson<{ changed?: string[] }>("/api/edit-config", body);
      if (response.error) {
        setMessage(`❌ ${response.error}`);
        return;
      }
      onChanged();
      onClose();
    } finally {
      savingPending.current = false;
      setSaving(false);
      endOperation(token);
    }
  };
  const requestClose = () => {
    if (!savingPending.current && !validatingPending.current && !operationPending) onClose();
  };

  const numberField = (key: keyof typeof draft, label: string, min: number) => (
    <label>{label}<input type="number" min={min} value={draft[key]} onChange={(event) => setDraft({ ...draft, [key]: +event.target.value })} /></label>
  );

  const verifyValidate = async () => {
    if (validatingPending.current || savingPending.current) return;
    const token = beginOperation(`workspace:${workspace}:validate`);
    if (!token) return;
    validatingPending.current = true;
    const isCurrent = validateGuard.begin();
    setValidating(true);
    setValidateResult(null);
    try {
      const response = await postJson<ValidateResponse>("/api/validate", {
        name: workspace,
        ...(parallel ? { run_id: runId ?? "" } : { workspace_generation: workspaceGeneration ?? "" }),
        validate_cmd: draft.validate_cmd,
        validate_timeout: draft.validate_timeout
      });
      if (!isCurrent()) return;
      if (response.error) {
        setValidateResult({ ok: false, text: `❌ ${response.error}`, tail: "" });
        return;
      }
      if (response.timeout) {
        setValidateResult({ ok: false, text: `❌ 執行逾時（${response.timeout_seconds ?? draft.validate_timeout} 秒）`, tail: response.tail ?? "" });
        return;
      }
      setValidateResult({
        ok: !!response.ok,
        text: response.ok ? "✅ Validate 通過（exit 0）" : `❌ Validate 失敗（exit ${response.rc ?? "?"}）`,
        tail: response.tail ?? ""
      });
    } finally {
      validatingPending.current = false;
      setValidating(false);
      endOperation(token);
    }
  };

  return (
    <Modal title="Workspace 設定" description={parallel ? "Parallel parent 已停止；修改 persisted 設定後於下一次 resume 生效" : "停止時才可修改，下一次運行生效"} closeDisabled={saving || validating || operationPending} onClose={requestClose} footer={
      <><button type="button" className="secondary-button" disabled={saving || validating || operationPending} onClick={requestClose}>取消</button><button type="button" className="primary-button" disabled={saving || validating || operationPending || (agentSelectionRequired && agentIndex === "")} onClick={() => void save()}>{saving ? "儲存中…" : "儲存設定"}</button><span role="status">{message}</span></>
    }>
      <fieldset className="form-grid launcher-fieldset" disabled={saving || validating || operationPending}>
        <div className="form-field agent-command-field"><span className="field-label-row"><span>Agent 命令</span></span>
          <div className="command-select-row"><select aria-label="Agent 命令" value={agentIndex} onChange={(event) => { setAgentIndex(event.target.value); if (event.target.value !== "") setAgentSelectionRequired(false); }}>
              <option value="" disabled={agentSelectionRequired}>{agentSelectionRequired ? "原 Agent CLI 已移除，請重新選擇" : `保持不變：${config.agent_cmd ?? "?"}`}</option>
              {(available?.agent_cmds ?? []).map((agent, index) => <option key={agent.cmd} value={index}>{agent.label} — {agent.cmd}</option>)}
            </select><button type="button" className="icon-button cli-gear-button" aria-label="管理 Agent CLI" disabled={!available} onClick={() => setCliManagerOpen(true)}>⚙</button></div>
        </div>
        <div className="form-field validate-command-field">
          <span className="field-label-row"><span>Validate 命令</span><button type="button" className="secondary-button compact-button" disabled={validating || !draft.validate_cmd.trim()} onClick={() => void verifyValidate()}>{validating ? "執行中…" : "執行確認"}</button></span>
          <input aria-label="Validate 命令" value={draft.validate_cmd} onChange={(event) => { setDraft({ ...draft, validate_cmd: event.target.value }); setValidateResult(null); }} />
        </div>
        {validateResult && <div className={`validate-result${validateResult.ok ? " success" : " error"}`} role="status"><strong>{validateResult.text}</strong>{validateResult.tail && <pre>{validateResult.tail}</pre>}</div>}
        <div className="number-grid">
          {numberField("flag_threshold", "flag 收斂（>）", 1)}
          {numberField("done_threshold", "done 收斂（≥）", 1)}
          {numberField("round_timeout", "單輪上限（分）", 0)}
          {numberField("agent_backoff_max", "Agent 異常退避上限（秒）", 0)}
          {numberField("validate_timeout", "Validate 上限（秒）", 1)}
        </div>
        <div className="number-grid two">
          {numberField("red_limit", "紅燈連跳 reset", 1)}
          {numberField("stall_limit", "HEAD 停滯 reset", 1)}
        </div>
        {parallel && <div className="number-grid two"><label>最大並行軌道<input type="number" min={1} max={8} value={maxParallel} onChange={(event) => setMaxParallel(+event.target.value)} /></label><label>Child restart 上限 <span className="label-help">0＝不限</span><input type="number" min={0} value={maxChildRestarts} onChange={(event) => setMaxChildRestarts(+event.target.value)} /></label></div>}
        <label className="checkbox-row"><input type="checkbox" checked={pauseAfterPlan} onChange={(event) => setPauseAfterPlan(event.target.checked)} />規劃收斂後暫停：不自動進入執行期，需按「▶ 運行」開始執行</label>
      </fieldset>
      {cliManagerOpen && available && <CliManagerModal config={available} repo={config.repo ?? ""} workspace={workspace} workspaceGeneration={!parallel ? workspaceGeneration : undefined} runId={parallel ? runId : undefined} beginOperation={beginOperation} endOperation={endOperation} onClose={() => setCliManagerOpen(false)} onSaved={(next) => {
        const selectedCommand = agentIndex === "" ? config.agent_cmd : available.agent_cmds[+agentIndex]?.cmd;
        setAvailable(next);
        const current = next.agent_cmds.findIndex((agent) => agent.cmd === selectedCommand);
        setAgentIndex(current >= 0 ? String(current) : "");
        setAgentSelectionRequired(!!selectedCommand && current < 0);
      }} />}
    </Modal>
  );
}
