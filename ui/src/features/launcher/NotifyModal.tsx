/** 終態通知命令編輯器：支援安全試跑與佔位符預覽，實際命令限制由後端再次驗證。 */
import { useState } from "react";
import Modal from "../../shared/components/Modal";
import { postJson } from "../../shared/api/client";
import type { ConfigResponse } from "../../shared/api/types";

interface TestResponse {
  ok?: boolean;
  rc?: number;
  timeout?: boolean;
  output?: string;
  error?: string;
}

export default function NotifyModal({ config, onClose, onSaved }: {
  config: ConfigResponse;
  onClose: () => void;
  onSaved: (config: ConfigResponse) => void;
}) {
  const [cmd, setCmd] = useState(config.notify_cmd ?? "");
  const [message, setMessage] = useState("");
  const [testOutput, setTestOutput] = useState("");
  const [busy, setBusy] = useState(false);

  const test = async () => {
    setBusy(true);
    setMessage("測試中…");
    setTestOutput("");
    const response = await postJson<TestResponse>("/api/test-notify", { notify_cmd: cmd.trim() });
    setBusy(false);
    if (response.error) {
      setMessage(`錯誤：${response.error}`);
      return;
    }
    setMessage(response.timeout ? "錯誤：執行逾時（15 秒）" : response.ok ? "成功：通知命令執行成功（exit 0）" : `錯誤：執行失敗（exit ${response.rc ?? "?"}）`);
    setTestOutput(response.output ?? "");
  };

  const save = async () => {
    setBusy(true);
    setMessage("儲存中…");
    const response = await postJson<ConfigResponse>("/api/edit-notify", { notify_cmd: cmd.trim() });
    setBusy(false);
    if (response.error) {
      setMessage(`錯誤：${response.error}`);
      return;
    }
    onSaved(response);
    onClose();
  };

  return (
    <Modal title="終態通知管理" description={`loop 完成／停機時執行的命令；只寫個人設定：${config.personal_config_path ?? "dashboard.config.local.json"}`} onClose={onClose} footer={
      <><button type="button" className="secondary-button" onClick={onClose}>取消</button><button type="button" className="primary-button" disabled={busy} onClick={() => void save()}>儲存通知設定</button><span role="status">{message}</span></>
    }>
      <label className="form-field">通知命令 <span className="label-help">留空＝不通知；佔位符 {"{status}"}（completed/stuck_stop/goal_missing…）與 {"{name}"}（workspace）</span>
        <input aria-label="通知命令" value={cmd} onChange={(event) => { setCmd(event.target.value); setTestOutput(""); }} placeholder={'sh -c \'curl -s -X POST https://ntfy.example/loop -d "{name}: {status}"\''} />
      </label>
      <div className="notify-test-row"><button type="button" className="secondary-button" disabled={busy || !cmd.trim()} onClick={() => void test()}>以 status=test 執行測試</button></div>
      {testOutput && <div className="agent-test-result"><pre>{testOutput}</pre></div>}
      <p className="cli-path-tip">測試會實際執行命令（替換規則與 loop 終態通知相同，逾時 15 秒）；正式通知失敗只記 warning，不會擋 loop。</p>
    </Modal>
  );
}
