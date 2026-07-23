/** Dashboard 全域設定編輯器：統計輪數、launch 預設值與 Validate 命令清單，儲存至團隊設定檔。 */
import { useEffect, useState } from "react";
import { getJson, postJson } from "../../shared/api/client";
import Modal from "../../shared/components/Modal";
import type { ConfigResponse, SelectCommand } from "../../shared/api/types";

const ROUNDS_MAX = 5000;

export default function SettingsModal({ onClose, onSaved }: {
  onClose: () => void;
  onSaved?: (config: ConfigResponse) => void;
}) {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [metrics, setMetrics] = useState({ workspace_rounds: 1000, fleet_rounds: 3000 });
  const [defaults, setDefaults] = useState({
    flag_threshold: 10,
    done_threshold: 3,
    round_timeout: 30,
    agent_backoff_max: 60,
    validate_timeout: 120,
    red_limit: 20,
    stall_limit: 300,
    stuck_stop_count: 100
  });
  const [stuckStop, setStuckStop] = useState(false);
  const [pauseAfterPlan, setPauseAfterPlan] = useState(false);
  const [validates, setValidates] = useState<SelectCommand[]>([]);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void getJson<ConfigResponse>("/api/config").then((response) => {
      if (!response || response.error) {
        setMessage(`錯誤：${response?.error ?? "設定讀取失敗"}`);
        return;
      }
      setConfig(response);
      if (response.metrics) setMetrics(response.metrics);
      const d = response.defaults ?? {};
      setDefaults({
        flag_threshold: d.flag_threshold ?? 10,
        done_threshold: d.done_threshold ?? 3,
        round_timeout: d.round_timeout ?? 30,
        agent_backoff_max: d.agent_backoff_max ?? 60,
        validate_timeout: d.validate_timeout ?? 120,
        red_limit: d.red_limit ?? 20,
        stall_limit: d.stall_limit ?? 300,
        stuck_stop_count: d.stuck_stop_count ?? 100
      });
      setStuckStop(d.stuck_stop ?? false);
      setPauseAfterPlan(d.pause_after_plan ?? false);
      setValidates((response.validate_cmds ?? []).map((item) => ({ ...item })));
    });
  }, []);

  const save = async () => {
    setSaving(true);
    setMessage("儲存中…");
    const response = await postJson<ConfigResponse>("/api/edit-settings", {
      metrics,
      defaults: { ...defaults, stuck_stop: stuckStop, pause_after_plan: pauseAfterPlan },
      validate_cmds: validates
    });
    setSaving(false);
    if (response.error) {
      setMessage(`錯誤：${response.error}`);
      return;
    }
    onSaved?.(response);
    onClose();
  };

  const metricsField = (key: keyof typeof metrics, label: string, hint: string) => (
    <label>{label} <span className="label-help">{hint}</span>
      <input type="number" min={1} max={ROUNDS_MAX} value={metrics[key]}
        onChange={(event) => setMetrics({ ...metrics, [key]: +event.target.value })} />
    </label>
  );

  const defaultField = (key: keyof typeof defaults, label: string, min: number) => (
    <label>{label}<input type="number" min={min} value={defaults[key]}
      onChange={(event) => setDefaults({ ...defaults, [key]: +event.target.value })} /></label>
  );

  const updateValidate = (index: number, patch: Partial<SelectCommand>) => {
    setValidates((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  };

  return (
    <Modal title="Dashboard 設定" description={`統計輪數與各項預設值；儲存至團隊設定檔：${config?.project_config_path ?? "…"}`} onClose={onClose} wide footer={
      <><button type="button" className="secondary-button" onClick={onClose}>取消</button><button type="button" className="primary-button" disabled={saving || !config} onClick={() => void save()}>{saving ? "儲存中…" : "儲存設定"}</button><span role="status">{message}</span></>
    }>
      <section className="cli-manager-section" aria-labelledby="settings-metrics-title">
        <div className="cli-manager-heading"><div><h3 id="settings-metrics-title">統計輪數</h3><p>效能統計是從 history.log 尾端即時計算的讀取上限，調大不會增加硬碟用量；儲存後於下一批推送（約 3 秒）生效。</p></div></div>
        <div className="number-grid two">
          {metricsField("workspace_rounds", "單 workspace 統計輪數", `fleet 卡片、輪次紀錄與 Run 對比的取樣上限（1～${ROUNDS_MAX}）`)}
          {metricsField("fleet_rounds", "全部 workspace 合併筆數", `總覽聚合統計取時間最新的筆數（1～${ROUNDS_MAX}）`)}
        </div>
      </section>
      <section className="cli-manager-section" aria-labelledby="settings-defaults-title">
        <div className="cli-manager-heading"><div><h3 id="settings-defaults-title">啟動預設值</h3><p>新 workspace 啟動表單的預設參數；既有 workspace 不受影響，可在各自的 Workspace 設定調整。</p></div></div>
        <div className="number-grid">
          {defaultField("flag_threshold", "flag 收斂（>）", 1)}
          {defaultField("done_threshold", "done 收斂（≥）", 1)}
          {defaultField("round_timeout", "單輪上限（分）", 0)}
          {defaultField("agent_backoff_max", "Agent 異常退避上限（秒）", 0)}
          {defaultField("validate_timeout", "Validate 上限（秒）", 1)}
        </div>
        <div className="number-grid two">
          {defaultField("red_limit", "紅燈連跳 reset", 1)}
          {defaultField("stall_limit", "HEAD 停滯 reset", 1)}
        </div>
        <label className="checkbox-row"><input type="checkbox" checked={stuckStop} onChange={(event) => setStuckStop(event.target.checked)} />卡死自動停止：同一任務重複 reset 達上限時停止 loop</label>
        {stuckStop && <div className="number-grid two">{defaultField("stuck_stop_count", "卡死停止門檻（輪）", 1)}</div>}
        <label className="checkbox-row"><input type="checkbox" checked={pauseAfterPlan} onChange={(event) => setPauseAfterPlan(event.target.checked)} />規劃收斂後暫停：不自動進入執行期，需按「運行」開始執行</label>
      </section>
      <section className="cli-manager-section" aria-labelledby="settings-validate-title">
        <div className="cli-manager-heading"><div><h3 id="settings-validate-title">Validate 命令清單</h3><p>啟動表單與 Workspace 設定的 Validate 選項；exit 0 視為驗證通過。</p></div><button type="button" className="secondary-button" onClick={() => setValidates((items) => [...items, { label: "", cmd: "" }])}>＋ 新增命令</button></div>
        <div className="cli-editor-list">
          {validates.map((item, index) => (
            <div className="cli-editor-row" key={index}>
              <label>名稱<input aria-label={`Validate ${index + 1} 名稱`} value={item.label} onChange={(event) => updateValidate(index, { label: event.target.value })} placeholder="例如 python unittest" /></label>
              <label>Command<input aria-label={`Validate ${index + 1} Command`} value={item.cmd} onChange={(event) => updateValidate(index, { cmd: event.target.value })} placeholder="例如 npm test" /></label>
              <div className="cli-row-actions"><button type="button" className="danger-button" onClick={() => setValidates((items) => items.filter((_, itemIndex) => itemIndex !== index))}>刪除</button></div>
            </div>
          ))}
          {!validates.length && <p className="empty-inline">沒有預設 Validate 命令；啟動時仍可手動輸入。</p>}
        </div>
      </section>
    </Modal>
  );
}
