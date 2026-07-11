import { useEffect, useMemo, useState } from "react";
import { getJson } from "../../shared/api/client";
import Modal from "../../shared/components/Modal";
import type { AnomalyListResponse, IncrementalResponse } from "../../shared/api/types";
import { parseHistory } from "./historyParser";

type TimelineKind = "round" | "operator" | "anomaly";
interface TimelineItem {
  id: string;
  kind: TimelineKind;
  sortKey: number;
  time: string;
  title: string;
  detail: string;
  tone?: "warning" | "error";
}

function dashboardActions(consoleText: string, datePrefix?: string): TimelineItem[] {
  const items: TimelineItem[] = [];
  for (const [index, line] of consoleText.split("\n").entries()) {
    const matched = line.match(/^\[(\d{2}:\d{2}:\d{2})\] 🖥️ Dashboard｜(.+)$/);
    if (!matched) continue;
    items.push({
      id: `operator-${index}-${matched[1]}`,
      kind: "operator",
      sortKey: datePrefix ? Date.parse(`${datePrefix}T${matched[1]}`) + index / 1000 : index,
      time: `${matched[1]}（本機時間）`,
      title: "Dashboard 人工操作",
      detail: matched[2],
      tone: /失敗|停止|拒絕|錯誤/.test(matched[2]) ? "warning" : undefined
    });
  }
    return items;
}

export default function TimelineModal({ workspace, consoleText, onClose }: {
  workspace: string;
  consoleText: string;
  onClose: () => void;
}) {
  const [history, setHistory] = useState<IncrementalResponse | null>(null);
  const [anomalies, setAnomalies] = useState<AnomalyListResponse | null>(null);
  const [filter, setFilter] = useState<"all" | TimelineKind>("all");
  const [query, setQuery] = useState("");

  useEffect(() => {
    let active = true;
    const encoded = encodeURIComponent(workspace);
    void Promise.all([
      getJson<IncrementalResponse>(`/api/history?ws=${encoded}&offset=-1`),
      getJson<AnomalyListResponse>(`/api/anomalies?ws=${encoded}`)
    ]).then(([nextHistory, nextAnomalies]) => {
      if (!active) return;
      setHistory(nextHistory ?? { size: 0, data: "", error: "history 讀取失敗" });
      setAnomalies(nextAnomalies ?? { limit: 100, total_count: 0, records: [], error: "異常清單讀取失敗" });
    });
    return () => { active = false; };
  }, [workspace]);

  const items = useMemo(() => {
    const savedAnomalies = new Map((anomalies?.records ?? []).map((item) => [
      `${item.timestamp}-${item.round}`,
      item
    ]));
    const rows = history?.data ? parseHistory(history.data).rows : [];
    const roundItems: TimelineItem[] = rows.map((row, index) => {
      const anomaly = savedAnomalies.get(`${row.ts}-${row.round}`);
      const signals = [
        row.phase,
        row.task || null,
        row.signal ? `signal ${row.signal}` : null,
        row.validate !== "-" ? `validate ${row.validate}` : null,
        row.durationSeconds !== null ? `${row.durationSeconds}s` : null,
        row.event || null
      ].filter(Boolean).join(" · ");
      return {
        id: `round-${row.round}-${row.ts}-${index}`,
        kind: anomaly ? "anomaly" : "round",
        sortKey: Date.parse(row.ts) || 1_000_000 - index,
        time: row.ts.replace("T", " "),
        title: anomaly ? `round ${row.round} · 未回 DONE` : `round ${row.round} · ${row.phase}`,
        detail: `${signals}${anomaly?.log_id ? " · 有保留 Agent log" : anomaly ? " · 無歷史 Agent log" : ""}`,
        tone: row.timedOut || row.validate === "FAIL" ? "error" : anomaly || row.tamper || !row.agentOk ? "warning" : undefined
      } satisfies TimelineItem;
    });
    const datePrefix = rows.find((row) => row.ts.includes("T"))?.ts.split("T", 1)[0];
    return [...roundItems, ...dashboardActions(consoleText, datePrefix)]
      .sort((left, right) => right.sortKey - left.sortKey);
  }, [anomalies, consoleText, history]);
  const normalized = query.trim().toLowerCase();
  const visible = items.filter((item) => (filter === "all" || item.kind === filter) &&
    (!normalized || `${item.title} ${item.detail} ${item.time}`.toLowerCase().includes(normalized)));

  return <Modal title={`${workspace}｜統一時間軸`} description="輪次判定、異常關聯與 Dashboard 人工操作的唯讀時序" onClose={onClose} extraWide>
    <div className="timeline-toolbar">
      <div className="segmented-tabs" role="group" aria-label="時間軸篩選">
        {(["all", "round", "anomaly", "operator"] as const).map((value) => <button type="button" key={value} className={filter === value ? "active" : ""} aria-pressed={filter === value} onClick={() => setFilter(value)}>{value === "all" ? "全部" : value === "round" ? "輪次" : value === "anomaly" ? "異常" : "人工操作"}</button>)}
      </div>
      <input type="search" aria-label="搜尋時間軸" placeholder="搜尋 task、事件或操作…" value={query} onChange={(event) => setQuery(event.target.value)} />
    </div>
    {!history || !anomalies ? <div className="loading-state">載入時間軸…</div> : <div className="timeline-list">
      {visible.map((item) => <article className={`timeline-item ${item.kind}${item.tone ? ` ${item.tone}` : ""}`} key={item.id}>
        <div className="timeline-marker" aria-hidden="true" />
        <div><header><strong>{item.title}</strong><time>{item.time}</time></header><p>{item.detail}</p></div>
      </article>)}
      {!visible.length && <div className="loading-state">沒有符合條件的時間軸事件</div>}
    </div>}
    {(history?.error || anomalies?.error) && <p className="field-error" role="alert">{history?.error || anomalies?.error}</p>}
  </Modal>;
}
