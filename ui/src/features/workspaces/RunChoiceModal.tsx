/** 停止狀態下的啟動選擇：一般執行保留完整 Preflight，Resume 可補登最小復原資料。 */
import { useState } from "react";
import Modal from "../../shared/components/Modal";

function toLocalInput(value?: string | null) {
  if (!value) return "";
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return "";
  const local = new Date(parsed.getTime() - parsed.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 19);
}

export default function RunChoiceModal({
  initialStartedAt,
  initialGreenSha,
  resumeAvailable,
  onRun,
  onResume,
  onClose
}: {
  initialStartedAt?: string | null;
  initialGreenSha?: string | null;
  resumeAvailable: boolean;
  onRun: () => void;
  onResume: (metadata: { round_started_at: string; last_green_sha: string }) => void;
  onClose: () => void;
}) {
  const [startedAt, setStartedAt] = useState(() => toLocalInput(initialStartedAt));
  const [greenSha, setGreenSha] = useState(initialGreenSha ?? "");
  const timestamp = startedAt ? new Date(startedAt) : null;
  const timestampValid = !!timestamp && Number.isFinite(timestamp.getTime()) && timestamp.getTime() < Date.now();
  const shaPresent = !!greenSha.trim();
  const canResume = timestampValid && shaPresent;

  const resume = () => {
    if (!timestamp || !canResume) return;
    onResume({ round_started_at: timestamp.toISOString(), last_green_sha: greenSha.trim() });
  };

  return (
    <Modal
      title="選擇啟動方式"
      description="一般執行會跑完整檢查；Resume 會保留目前 code repo 現場"
      onClose={onClose}
      footer={<>
        <button type="button" className="secondary-button" onClick={onClose}>取消</button>
        <button type="button" className="success-button" onClick={onRun}>一般執行</button>
        <button type="button" className="primary-button" disabled={!canResume} onClick={resume}>Resume</button>
      </>}
    >
      <div className="run-choice-grid">
        <section className="run-choice-card">
          <h3>一般執行</h3>
          <p>執行完整 Preflight 與啟動 Validate；適合乾淨且可正常驗證的工作區。</p>
        </section>
        <section className="run-choice-card resume-choice-card">
          <div className="run-choice-heading">
            <h3>Resume 現場</h3>
            <span className={`resume-readiness ${resumeAvailable ? "ready" : "needs-data"}`}>
              {resumeAvailable ? "現有資料可用" : "可補資料後啟動"}
            </span>
          </div>
          <p>略過啟動 Preflight／Validate。只驗證開始時間早於現在，且 SHA 存在於這個 code repo。</p>
          <div className="form-grid resume-metadata-form">
            <label>執行開始時間
              <input
                aria-label="Resume 執行開始時間"
                type="datetime-local"
                step="1"
                value={startedAt}
                aria-invalid={startedAt !== "" && !timestampValid}
                onChange={(event) => setStartedAt(event.target.value)}
              />
            </label>
            {!timestampValid && <p className="field-error">請填寫早於現在的時間。</p>}
            <label>綠點 commit SHA
              <input
                aria-label="Resume 綠點 commit SHA"
                autoComplete="off"
                spellCheck={false}
                placeholder="例如 73a9be0"
                value={greenSha}
                onChange={(event) => setGreenSha(event.target.value)}
              />
            </label>
            {!shaPresent && <p className="field-error">請填寫 code repo 內存在的 commit SHA。</p>}
          </div>
        </section>
      </div>
    </Modal>
  );
}
