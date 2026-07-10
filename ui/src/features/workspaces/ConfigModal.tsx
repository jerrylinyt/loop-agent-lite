import { useEffect, useState } from "react";
import Modal from "../../shared/components/Modal";
import { getJson, postJson } from "../../shared/api/client";
import type { ConfigResponse, DashboardConfig } from "../../shared/api/types";

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
    red_limit: config.red_limit ?? 20,
    stall_limit: config.stall_limit ?? 300
  });
  const [message, setMessage] = useState("");

  useEffect(() => { void getJson<ConfigResponse>("/api/config").then(setAvailable); }, []);

  const save = async () => {
    setMessage("儲存中…");
    const body: Record<string, string | number> = { name: workspace, ...draft };
    if (agentIndex !== "") body.agent_idx = +agentIndex;
    const response = await postJson<{ changed?: string[] }>("/api/edit-config", body);
    if (response.error) return setMessage(`❌ ${response.error}`);
    setMessage(`✅ 已儲存 ${response.changed?.join(", ") || "（無變更）"}`);
    onChanged();
  };

  const numberField = (key: keyof typeof draft, label: string, min: number) => (
    <label>{label}<input type="number" min={min} value={draft[key]} onChange={(event) => setDraft({ ...draft, [key]: +event.target.value })} /></label>
  );

  return (
    <Modal title="Workspace 設定" description="停止時才可修改，下一次運行生效" onClose={onClose} footer={
      <><button type="button" className="secondary-button" onClick={onClose}>取消</button><button type="button" className="primary-button" onClick={save}>儲存設定</button><span role="status">{message}</span></>
    }>
      <div className="form-grid">
        <label>Agent 命令
          <select value={agentIndex} onChange={(event) => setAgentIndex(event.target.value)}>
            <option value="">保持不變：{config.agent_cmd ?? "?"}</option>
            {(available?.agent_cmds ?? []).map((agent, index) => <option key={agent.cmd} value={index}>{agent.label} — {agent.cmd}</option>)}
          </select>
        </label>
        <label>Validate 命令<input value={draft.validate_cmd} onChange={(event) => setDraft({ ...draft, validate_cmd: event.target.value })} /></label>
        <div className="number-grid">
          {numberField("flag_threshold", "flag 收斂（>）", 1)}
          {numberField("done_threshold", "done 收斂（≥）", 1)}
          {numberField("round_timeout", "單輪上限（分）", 0)}
        </div>
        <div className="number-grid two">
          {numberField("red_limit", "紅燈連跳 reset", 1)}
          {numberField("stall_limit", "HEAD 停滯 reset", 1)}
        </div>
      </div>
    </Modal>
  );
}
