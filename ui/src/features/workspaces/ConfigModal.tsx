/** 停止狀態下的 workspace 設定編輯器：載入可選命令、追蹤非同步測試，儲存時由後端重驗數值與白名單。 */
import { useEffect, useState } from "react";
import CliManagerModal from "../cli/CliManagerModal";
import Modal from "../../shared/components/Modal";
import { getJson, postJson } from "../../shared/api/client";
import useStaleGuard from "../../shared/hooks/useStaleGuard";
import type { ConfigResponse, DashboardConfig } from "../../shared/api/types";

interface ValidateResponse { ok?: boolean; rc?: number; timeout?: boolean; timeout_seconds?: number; tail?: string }

export default function ConfigModal({
  workspace,
  config,
  onClose,
  onChanged
}: {
  workspace: string;
  config: DashboardConfig;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [available, setAvailable] = useState<ConfigResponse | null>(null);
  const [agentIndex, setAgentIndex] = useState("");
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
  const [message, setMessage] = useState("");
  const [validating, setValidating] = useState(false);
  const [validateResult, setValidateResult] = useState<{ ok: boolean; text: string; tail: string } | null>(null);
  const [cliManagerOpen, setCliManagerOpen] = useState(false);
  const validateGuard = useStaleGuard();

  useEffect(() => { void getJson<ConfigResponse>("/api/config").then(setAvailable); }, []);
  useEffect(() => {
    validateGuard.cancelPending();
    setValidating(false);
    setValidateResult(null);
  }, [draft.validate_cmd, draft.validate_timeout, validateGuard]);

  const save = async () => {
    setMessage("儲存中…");
    const body: Record<string, string | number | boolean> = { name: workspace, ...draft, pause_after_plan: pauseAfterPlan };
    if (agentIndex !== "") body.agent_idx = +agentIndex;
    const response = await postJson<{ changed?: string[] }>("/api/edit-config", body);
    if (response.error) return setMessage(`❌ ${response.error}`);
    onChanged();
    onClose();
  };

  const numberField = (key: keyof typeof draft, label: string, min: number) => (
    <label>{label}<input type="number" min={min} value={draft[key]} onChange={(event) => setDraft({ ...draft, [key]: +event.target.value })} /></label>
  );

  const verifyValidate = async () => {
    const isCurrent = validateGuard.begin();
    setValidating(true);
    setValidateResult(null);
    const response = await postJson<ValidateResponse>("/api/validate", {
      name: workspace,
      validate_cmd: draft.validate_cmd,
      validate_timeout: draft.validate_timeout
    });
    if (!isCurrent()) return;
    setValidating(false);
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
  };

  return (
    <Modal title="Workspace 設定" description="停止時才可修改，下一次運行生效" onClose={onClose} footer={
      <><button type="button" className="secondary-button" onClick={onClose}>取消</button><button type="button" className="primary-button" onClick={save}>儲存設定</button><span role="status">{message}</span></>
    }>
      <div className="form-grid">
        <div className="form-field agent-command-field"><span className="field-label-row"><span>Agent 命令</span></span>
          <div className="command-select-row"><select aria-label="Agent 命令" value={agentIndex} onChange={(event) => setAgentIndex(event.target.value)}>
              <option value="">保持不變：{config.agent_cmd ?? "?"}</option>
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
        <label className="checkbox-row"><input type="checkbox" checked={pauseAfterPlan} onChange={(event) => setPauseAfterPlan(event.target.checked)} />規劃收斂後暫停：不自動進入執行期，需按「▶ 運行」開始執行</label>
      </div>
      {cliManagerOpen && available && <CliManagerModal config={available} repo={config.repo ?? ""} workspace={workspace} onClose={() => setCliManagerOpen(false)} onSaved={(next) => {
        setAvailable(next);
        const current = next.agent_cmds.findIndex((agent) => agent.cmd === config.agent_cmd);
        setAgentIndex(String(current >= 0 ? current : 0));
      }} />}
    </Modal>
  );
}
