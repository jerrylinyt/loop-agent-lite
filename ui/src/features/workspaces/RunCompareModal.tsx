/** Run 對比：只比較後端實際保留的 current/previous metrics 與 anomaly 數，不臆測未保存資料。 */
import { useEffect, useState } from "react";
import { getJson } from "../../shared/api/client";
import type { AnomalyListResponse, RoundMetrics } from "../../shared/api/types";
import Modal from "../../shared/components/Modal";

function metric(value: number | null, suffix = "s") { return value === null ? "—" : `${value}${suffix}`; }
function delta(current: number | null, previous: number | null, lowerIsBetter = true) {
  // 沒有任一側樣本就不比較；耗時/異常採越低越好，樣本數則可反轉判斷。
  if (current === null || previous === null) return { text: "無法比較", tone: "" };
  const value = Math.round((current - previous) * 100) / 100;
  if (value === 0) return { text: "＝ 無變化", tone: "" };
  const improved = lowerIsBetter ? value < 0 : value > 0;
  return { text: `${value > 0 ? "＋" : ""}${value}`, tone: improved ? "improved" : "regressed" };
}

export default function RunCompareModal({ workspace, onClose }: { workspace: string; onClose: () => void }) {
  const [current, setCurrent] = useState<RoundMetrics | null>(null);
  const [previous, setPrevious] = useState<RoundMetrics | null>(null);
  const [anomalies, setAnomalies] = useState<[number, number] | null>(null);
  useEffect(() => {
    const ws = encodeURIComponent(workspace);
    void Promise.all([
      getJson<RoundMetrics>(`/api/round-metrics?ws=${ws}&run=current&limit=100`),
      getJson<RoundMetrics>(`/api/round-metrics?ws=${ws}&run=previous&limit=100`),
      getJson<AnomalyListResponse>(`/api/anomalies?ws=${ws}&run=current`),
      getJson<AnomalyListResponse>(`/api/anomalies?ws=${ws}&run=previous`)
    ]).then(([now, before, nowAnomalies, beforeAnomalies]) => {
      setCurrent(now); setPrevious(before);
      setAnomalies([nowAnomalies?.total_count ?? 0, beforeAnomalies?.total_count ?? 0]);
    });
  }, [workspace]);
  const rows = current && previous ? [
    ["樣本數", current.sample_count, previous.sample_count, delta(current.sample_count, previous.sample_count, false)],
    ["平均耗時", metric(current.average_seconds), metric(previous.average_seconds), delta(current.average_seconds, previous.average_seconds)],
    ["P95", metric(current.p95_seconds), metric(previous.p95_seconds), delta(current.p95_seconds, previous.p95_seconds)],
    ["最慢輪", metric(current.max_seconds), metric(previous.max_seconds), delta(current.max_seconds, previous.max_seconds)],
    ["逾時率", metric(current.timeout_rate_pct, "%"), metric(previous.timeout_rate_pct, "%"), delta(current.timeout_rate_pct, previous.timeout_rate_pct)],
    ["未回 DONE", current.missing_done_count, previous.missing_done_count, delta(current.missing_done_count, previous.missing_done_count)],
    ["異常紀錄", anomalies?.[0] ?? 0, anomalies?.[1] ?? 0, delta(anomalies?.[0] ?? 0, anomalies?.[1] ?? 0)],
  ] as const : [];
  return <Modal title={`${workspace}｜Run 對比`} description="目前 run 與輪替保留的上一個 run；只比較 coordinator 有保存的客觀資料" onClose={onClose} extraWide>
    {!current || !previous || !anomalies ? <div className="loading-state">載入兩次 run…</div> : <>
      <div className="run-compare-table" role="table" aria-label="Run 指標對比">
        <div className="run-compare-head" role="row"><strong>指標</strong><strong>目前</strong><strong>上一個</strong><strong>變化</strong></div>
        {rows.map(([label, now, before, change]) => <div className="run-compare-row" role="row" key={label}><strong>{label}</strong><span>{now}</span><span>{before}</span><span className={change.tone}>{change.text}</span></div>)}
      </div>
      {!previous.sample_count && <p className="modal-note">上一個 run 沒有可比較的輪次樣本；首次執行或歷史尚未輪替時屬正常設計。</p>}
      <p className="modal-note">設定與 commit 清單目前沒有 per-run snapshot，因此不推測差異；本表僅呈現 history 與 anomaly 保存的資料。</p>
    </>}
  </Modal>;
}
