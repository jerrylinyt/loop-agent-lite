import { useEffect, useRef, useState } from "react";
import { getJson } from "../../shared/api/client";
import type { AnomalyListResponse, AnomalyLogResponse, AnomalyRecord } from "../../shared/api/types";
import Modal from "../../shared/components/Modal";

const PHASE_NAMES: Record<string, string> = { plan: "規劃", exec: "執行" };

function displayTime(timestamp: string): string {
  return timestamp.includes("T") ? timestamp.replace("T", " ") : timestamp;
}

export default function AnomalyLogModal({ workspace, run = "current", onClose }: {
  workspace?: string;
  run?: "current" | "previous";
  onClose: () => void;
}) {
  const [projection, setProjection] = useState<AnomalyListResponse | null>(null);
  const [selected, setSelected] = useState<AnomalyRecord | null>(null);
  const [log, setLog] = useState<AnomalyLogResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const logRequestSeq = useRef(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    const query = workspace
      ? `?ws=${encodeURIComponent(workspace)}&run=${run}`
      : "";
    void getJson<AnomalyListResponse>(`/api/anomalies${query}`).then((response) => {
      if (!active) return;
      setProjection(response);
      setLoading(false);
    });
    return () => { active = false; };
  }, [run, workspace]);

  const openRecord = async (record: AnomalyRecord) => {
    const seq = logRequestSeq.current + 1;
    logRequestSeq.current = seq;
    setSelected(record);
    if (!record.log_id) {
      setLog({ error: "此異常發生在 log 保留功能啟用前，只有輪次判定，沒有 Agent log。" });
      return;
    }
    setLog(null);
    const response = await getJson<AnomalyLogResponse>(
      `/api/anomaly-log?ws=${encodeURIComponent(record.workspace)}&id=${encodeURIComponent(record.log_id)}`
    );
    if (seq !== logRequestSeq.current) return;
    setLog(response ?? { error: "異常 log 讀取失敗" });
  };

  const records = projection?.records ?? [];
  const title = workspace ? `${workspace}｜異常輪` : "全部 workspace｜異常輪";
  return (
    <Modal title={title} description={`最多列出最近 ${projection?.limit ?? 100} 筆；人工中斷不計，Git 有變更但未回完成訊號仍算異常`} onClose={onClose} extraWide>
      <div className="anomaly-explorer">
        <section className="anomaly-list" aria-label="異常輪清單">
          <header><strong>異常輪</strong><span>{projection?.total_count ?? 0} 筆</span></header>
          {loading && <div className="empty-inline">載入中…</div>}
          {!loading && projection?.error && <div className="empty-inline warning">{projection.error}</div>}
          {!loading && !projection?.error && !records.length && <div className="empty-inline">目前統計範圍內沒有異常輪。</div>}
          {records.map((record) => (
            <button key={`${record.workspace}-${record.timestamp}-${record.round}`} type="button"
              className={`anomaly-record${selected === record ? " active" : ""}`}
              aria-label={`${record.workspace} round ${record.round} 異常`}
              onClick={() => void openRecord(record)}>
              <span><strong>{record.workspace}</strong><b>round {record.round}</b><em>{PHASE_NAMES[record.phase] ?? record.phase}</em></span>
              <small>{displayTime(record.timestamp)}</small>
              <small>{record.task || "—"} · signal {record.signal || "—"} · Git {record.changed ? "有變更" : "無變更"}</small>
              <i className={record.log_id ? "log-saved" : "log-missing"}>{record.log_id ? "有保留 log" : "無歷史 log"}</i>
            </button>
          ))}
        </section>
        <section className="anomaly-log-viewer" aria-label="異常輪 Log">
          <header><strong>{selected ? `${selected.workspace} · round ${selected.round}` : "選擇異常輪查看 Log"}</strong>{log?.truncated && <span>只保留尾端 2 MiB</span>}</header>
          {!selected && <div className="empty-inline">點選左側異常輪後顯示 Agent log。</div>}
          {selected && !log && <div className="empty-inline">讀取 log…</div>}
          {log?.error && <div className="empty-inline warning">{log.error}</div>}
          {log?.data !== undefined && <pre tabIndex={0}>{log.data || "（空 log）"}</pre>}
        </section>
      </div>
    </Modal>
  );
}
