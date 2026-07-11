/** 輪次歷史檢視器：切換 current/previous run，並行讀取 bounded history 與 metrics，顯示 coordinator 判定。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { getJson } from "../../shared/api/client";
import Modal from "../../shared/components/Modal";
import type { IncrementalResponse, RoundMetrics } from "../../shared/api/types";
import AnomalyLogModal from "./AnomalyLogModal";
import { parseHistory, type HistoryRow } from "./historyParser";

function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  return `${seconds < 1 ? seconds.toFixed(2) : seconds.toFixed(1)} 秒`;
}

export default function HistoryModal({ workspace, onClose }: { workspace: string; onClose: () => void }) {
  const [run, setRun] = useState<"current" | "previous">("current");
  const [rows, setRows] = useState<HistoryRow[]>([]);
  const [metrics, setMetrics] = useState<RoundMetrics | null>(null);
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(true);
  const requestSeq = useRef(0);
  const [anomaliesOpen, setAnomaliesOpen] = useState(false);

  const load = useCallback(async () => {
    // 切換 run 分頁/按重新整理會連發請求;舊請求晚回不得覆蓋新分頁的結果
    const seq = requestSeq.current + 1;
    requestSeq.current = seq;
    setLoading(true);
    setMetrics(null);
    const encodedWorkspace = encodeURIComponent(workspace);
    const [response, metricProjection] = await Promise.all([
      getJson<IncrementalResponse>(`/api/history?ws=${encodedWorkspace}&offset=-1&run=${run}`),
      getJson<RoundMetrics>(`/api/round-metrics?ws=${encodedWorkspace}&run=${run}&limit=100`)
    ]);
    if (seq !== requestSeq.current) return;
    setLoading(false);
    if (!response || response.error) {
      setNote("❌ 讀取 history.log 失敗");
      return;
    }
    setMetrics(metricProjection && !metricProjection.error ? metricProjection : null);
    const { rows: parsed, unparsed } = parseHistory(response.data);
    setRows(parsed);
    const notes: string[] = [];
    if (run === "previous" && !response.data) notes.push("沒有保留的上一個 run 紀錄");
    else if (response.truncated) notes.push("檔案較大，僅顯示最近的紀錄");
    if (unparsed > 0) notes.push(`${unparsed} 行無法解析（舊格式）`);
    if (!metricProjection || metricProjection.error) notes.push("效能摘要讀取失敗");
    else if (metricProjection.history_truncated) notes.push("效能摘要取自 history 尾端");
    setNote(notes.join("；"));
  }, [run, workspace]);

  useEffect(() => { void load(); }, [load]);

  return (
    <Modal title="輪次紀錄" description={`${run === "current" ? "目前" : "上一個"} run 的 coordinator 判定（唯讀）`} onClose={onClose} extraWide footer={
      <><button type="button" className="secondary-button" onClick={() => void load()} disabled={loading}>{loading ? "載入中…" : "重新整理"}</button><span role="status" className="muted">{note}</span></>
    }>
      <div className="segmented-tabs history-run-tabs" role="tablist" aria-label="歷史 run">
        <button type="button" role="tab" aria-selected={run === "current"} className={run === "current" ? "active" : ""} onClick={() => setRun("current")}>目前 run</button>
        <button type="button" role="tab" aria-selected={run === "previous"} className={run === "previous" ? "active" : ""} onClick={() => setRun("previous")}>上一個 run</button>
      </div>
      {metrics && metrics.sample_count > 0 && (
        <section className="history-analysis" aria-label="近 100 輪效能分析">
          <div className="history-analysis-head"><strong>近 100 輪效能分析</strong><span>目前共 {metrics.sample_count} 輪樣本</span></div>
          <div className="history-metrics" role="list" aria-label="輪次效能摘要">
            <div className="history-metric" role="listitem"><span>樣本</span><strong>{metrics.sample_count} 輪</strong></div>
            <div className="history-metric" role="listitem"><span>平均</span><strong>{formatDuration(metrics.average_seconds)}</strong></div>
            <div className="history-metric" role="listitem"><span>P50</span><strong>{formatDuration(metrics.p50_seconds)}</strong></div>
            <div className="history-metric" role="listitem"><span>P95</span><strong>{formatDuration(metrics.p95_seconds)}</strong></div>
            <div className="history-metric" role="listitem"><span>最慢</span><strong>{formatDuration(metrics.max_seconds)}</strong><small>round {metrics.slowest_round}</small></div>
            <div className={`history-metric${metrics.timeout_count ? " warning" : ""}`} role="listitem"><span>逾時率</span><strong>{metrics.timeout_rate_pct}%</strong><small>{metrics.timeout_count} 輪</small></div>
            <div className={`history-metric history-metric-action${metrics.missing_done_count ? " warning" : ""}`} role="listitem"><button type="button" className="history-metric-button" onClick={() => setAnomaliesOpen(true)}><span>未回 DONE</span><strong>{metrics.missing_done_count} 次</strong><small>點擊查看輪次與 log</small></button></div>
            <div className={`history-metric${metrics.missing_done_count ? " warning" : ""}`} role="listitem"><span>異常率</span><strong>{metrics.missing_done_rate_pct}%</strong><small>人工中斷不計</small></div>
          </div>
        </section>
      )}
      <div className="modal-table-scroll">
        <table>
          <thead><tr><th>輪</th><th>時間</th><th>耗時</th><th>階段</th><th>任務</th><th>訊號</th><th>驗證</th><th>flag</th><th>done</th><th>事件</th></tr></thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={`${row.round}-${row.time}-${index}`}>
                <td>{row.round}</td>
                <td className="muted">{row.time}</td>
                <td className={row.timedOut ? "warning" : "muted"}>{formatDuration(row.durationSeconds)}{row.timedOut ? "（逾時）" : ""}</td>
                <td>{row.phase}</td>
                <td>{row.task}</td>
                <td>{row.signal}</td>
                <td>{row.validate === "PASS" ? "✅" : row.validate === "FAIL" ? "❌" : "—"}</td>
                <td>{row.flag}</td>
                <td>{row.done}</td>
                <td>{`${row.missingDone ? "⚠️ 未回 DONE " : ""}${row.tamper ? "⚠️ 竄改 " : ""}${row.agentOk ? "" : "⚠️ Agent 異常 "}${row.event}`}</td>
              </tr>
            ))}
            {!rows.length && !loading && <tr><td colSpan={10} className="table-empty">尚無輪次紀錄</td></tr>}
          </tbody>
        </table>
      </div>
      {anomaliesOpen && <AnomalyLogModal workspace={workspace} run={run} onClose={() => setAnomaliesOpen(false)} />}
    </Modal>
  );
}
