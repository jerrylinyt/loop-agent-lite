/** Fleet 監控總覽：聚合工作區、效能與事件，並處理篩選、排序及有條件的批次操作。 */
import { useEffect, useMemo, useState } from "react";
import type { FleetHistoryEntry, FleetRoundMetrics, WorkspaceSummary } from "../../shared/api/types";
import AnomalyLogModal from "./AnomalyLogModal";
import { deriveFleetEvents } from "./fleetEvents";
import { useRoundNow } from "./roundTiming";
import { postJson } from "../../shared/api/client";
import ActionDialog from "../../shared/components/ActionDialog";
import FleetWorkspaceCard from "./FleetWorkspaceCard";
import {
  formatMetric, initialFleetFilter, initialFleetSort,
  visibleFleetWorkspaces, workspaceNeedsAttention,
} from "./fleetViewModel";
import type { FleetFilter, FleetSort } from "./fleetViewModel";

/** 監控電視牆:聚合統計 + 全 fleet 即時卡片 + 事件推播。
 * 卡片與歷史事件都走同一條 SSE；事件流仍由前端從 history 尾段推導。 */
export default function FleetOverview({ workspaces, fleetHistory, fleetMetrics, attentionRequest, readonly, onSelect, onChanged }: {
  workspaces: WorkspaceSummary[];
  fleetHistory: FleetHistoryEntry[];
  fleetMetrics: FleetRoundMetrics | null;
  attentionRequest: number;
  readonly: boolean;
  onSelect: (name: string) => void;
  onChanged: () => void | Promise<void>;
}) {
  const events = useMemo(() => deriveFleetEvents(fleetHistory), [fleetHistory]);
  const metricsByWorkspace = useMemo(
    () => new Map(fleetHistory.map((entry) => [entry.name, entry.metrics])),
    [fleetHistory]
  );
  const roundNow = useRoundNow(workspaces.some((workspace) =>
    Boolean(workspace.round_started_at && !workspace.round_interrupted_at)));
  const [filter, setFilter] = useState<FleetFilter>(initialFleetFilter);
  const [search, setSearch] = useState(() => localStorage.getItem("fleet-search") ?? "");
  const [sort, setSort] = useState<FleetSort>(initialFleetSort);
  const [compact, setCompact] = useState(() => localStorage.getItem("fleet-compact") === "1");
  const [anomaliesOpen, setAnomaliesOpen] = useState(false);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [selectedNames, setSelectedNames] = useState<string[]>([]);
  const [bulkAction, setBulkAction] = useState<"ack" | "stop" | null>(null);
  const [bulkMessage, setBulkMessage] = useState("");
  const selectedWorkspaces = workspaces.filter((workspace) => selectedNames.includes(workspace.name));
  // 每種批次操作都有不同前置條件。確認視窗同時列出 eligible 與被跳過項目，
  // 避免使用者以為「選到了」就一定會被修改。
  const eligible = bulkAction === "ack" ? selectedWorkspaces.filter((workspace) => (workspace.unread_issues ?? workspace.issues ?? 0) > 0 && !workspace.running)
    : bulkAction === "stop" ? selectedWorkspaces.filter((workspace) => workspace.running) : [];
  const runBulk = async () => {
    if (!bulkAction) return;
    const targets = [...eligible]; setBulkAction(null);
    let failed = 0;
    // 刻意逐筆呼叫既有單 workspace API：單筆鎖或狀態失敗不回滾其他 workspace，
    // 也不另開一條能繞過既有安全檢查的批次後端捷徑。
    for (const workspace of targets) {
      const response = bulkAction === "ack" ? await postJson("/api/edit-state", { name: workspace.name, ack_issues: true })
        : await postJson("/api/stop", { name: workspace.name });
      if (response.error) failed += 1;
    }
    setBulkMessage(`已處理 ${targets.length - failed}/${targets.length} 個 workspace${failed ? `，${failed} 個失敗` : ""}`);
    setSelectedNames([]); await Promise.resolve(onChanged());
  };

  useEffect(() => {
    if (attentionRequest > 0) {
      setFilter("attention");
      setSearch("");
      localStorage.setItem("fleet-filter", "attention");
      localStorage.setItem("fleet-search", "");
    }
  }, [attentionRequest]);

  const changeFilter = (next: FleetFilter) => {
    setFilter(next);
    localStorage.setItem("fleet-filter", next);
  };
  const changeSearch = (next: string) => {
    setSearch(next);
    localStorage.setItem("fleet-search", next);
  };
  const changeSort = (next: FleetSort) => {
    setSort(next);
    localStorage.setItem("fleet-sort", next);
  };
  const changeCompact = (next: boolean) => {
    setCompact(next);
    localStorage.setItem("fleet-compact", next ? "1" : "0");
  };

  const running = workspaces.filter((workspace) => workspace.running).length;
  const done = workspaces.filter((workspace) => workspace.phase === "done").length;
  const planning = workspaces.filter((workspace) => workspace.phase === "plan").length;
  const executing = workspaces.filter((workspace) => workspace.phase === "exec").length;
  const totalTasks = workspaces.reduce((sum, workspace) => sum + (workspace.plan_len ?? 0), 0);
  const doneTasks = workspaces.reduce((sum, workspace) => sum + (workspace.completed ?? 0), 0);
  const alerts = workspaces.filter(workspaceNeedsAttention).length;
  const visibleWorkspaces = useMemo(
    () => visibleFleetWorkspaces(workspaces, filter, search, sort),
    [filter, search, sort, workspaces]
  );
  const filters: Array<{ id: FleetFilter; label: string; count: number }> = [
    { id: "all", label: "全部", count: workspaces.length },
    { id: "attention", label: "需關注", count: alerts },
    { id: "running", label: "執行中", count: running },
    { id: "done", label: "已完成", count: done },
  ];
  const taskPct = totalTasks > 0 ? Math.round((doneTasks / totalTasks) * 100) : 0;

  return (
    <main id="main-content" tabIndex={-1} className={`fleet-overview${compact ? " compact-cards" : ""}`} aria-label="工作區總覽">
      <div className="fleet-stats" role="list" aria-label="工作區統計">
        <div className="fleet-stat" role="listitem"><strong>{workspaces.length}</strong><span>workspaces</span></div>
        <div className="fleet-stat running" role="listitem"><strong>{running}</strong><span>執行中</span></div>
        <div className="fleet-stat" role="listitem"><strong>{planning} / {executing} / {done}</strong><span>規劃 / 執行 / 完成</span></div>
        <div className={`fleet-stat${alerts > 0 ? " warning" : ""}`} role="listitem"><strong>{alerts}</strong><span>需要關注</span></div>
        <div className="fleet-stat tasks" role="listitem">
          <strong>{doneTasks} / {totalTasks}<em>（{taskPct}%）</em></strong>
          <span>任務完成</span>
          <div className="fleet-progress"><div className="fleet-progress-fill" style={{ width: `${taskPct}%` }} /></div>
        </div>
        <div className="fleet-stat fleet-performance" role="listitem" aria-label="全部 workspace 輪次效能">
          <strong>{fleetMetrics?.sample_count ?? 0} 輪</strong>
          <span>全部 workspace 近 500 輪</span>
          {fleetMetrics && fleetMetrics.sample_count > 0 ? (
            <div className="fleet-performance-summary" title={`${fleetMetrics.workspace_count} 個 workspace 合併後，取時間最新 ${fleetMetrics.limit} 個已結束輪次；Plan 以 create-plan / plan-ok、Exec 以 done 作為完成回報；有 Git 變更但未回報仍算異常，人工中斷輪不計`}>
              <div className="fleet-performance-grid">
                <span><small>平均</small><b>{formatMetric(fleetMetrics.average_seconds)}</b></span>
                <span><small>P50</small><b>{formatMetric(fleetMetrics.p50_seconds)}</b></span>
                <span><small>P95</small><b>{formatMetric(fleetMetrics.p95_seconds)}</b></span>
                <span><small>最慢</small><b>{formatMetric(fleetMetrics.max_seconds)}</b></span>
                <span className={fleetMetrics.timeout_count ? "warning" : ""}><small>逾時</small><b>{fleetMetrics.timeout_rate_pct}%</b></span>
              </div>
              <div className="fleet-anomaly-grid">
                <button type="button" className={`fleet-anomaly-button${fleetMetrics.missing_done_count ? " warning" : ""}`} onClick={() => setAnomaliesOpen(true)}><small>未回 DONE</small><b>{fleetMetrics.missing_done_count} 次</b><i>查看</i></button>
                <span className={fleetMetrics.missing_done_count ? "warning" : ""}><small>異常率</small><b>{fleetMetrics.missing_done_rate_pct}%</b></span>
              </div>
            </div>
          ) : <small className="fleet-performance-empty">尚無輪次資料</small>}
        </div>
      </div>
      <div className="fleet-filter-row">
        <div className="fleet-filters" role="group" aria-label="Workspace 篩選">
          {filters.map((item) => (
            <button key={item.id} type="button" className={filter === item.id ? "active" : ""}
              aria-pressed={filter === item.id} onClick={() => changeFilter(item.id)}>
              {item.label} <span>{item.count}</span>
            </button>
          ))}
        </div>
        <input className="fleet-search" type="search" aria-label="搜尋 workspace" placeholder="搜尋 workspace…"
          value={search} onChange={(event) => changeSearch(event.target.value)} />
        <select className="fleet-sort" aria-label="Workspace 排序" value={sort} onChange={(event) => changeSort(event.target.value as FleetSort)}>
          <option value="name">名稱排序</option><option value="attention">需關注優先</option><option value="running">執行中優先</option><option value="progress">完成度優先</option>
        </select>
        <label className="compact-toggle"><input type="checkbox" checked={compact} onChange={(event) => changeCompact(event.target.checked)} /> 精簡卡片</label>
        <span className="muted">顯示 {visibleWorkspaces.length} / {workspaces.length}</span>
      </div>
      {!readonly && <div className="bulk-toolbar">
        <button type="button" className="secondary-button compact-button" aria-expanded={bulkOpen} onClick={() => setBulkOpen((value) => !value)}>批次操作</button>
        {bulkOpen && <><select multiple aria-label="批次選擇 workspace" value={selectedNames} onChange={(event) => setSelectedNames([...event.target.selectedOptions].map((option) => option.value))}>{visibleWorkspaces.map((workspace) => <option key={workspace.name} value={workspace.name}>{workspace.name} · {workspace.running ? "執行中" : "已停止"}</option>)}</select>
          <button type="button" className="secondary-button compact-button" disabled={!selectedNames.length} onClick={() => setBulkAction("ack")}>Issues 已讀</button>
          <button type="button" className="danger-button compact-button" disabled={!selectedNames.length} onClick={() => setBulkAction("stop")}>立即停止</button></>}
        <span className="muted" role="status">{bulkMessage || (selectedNames.length ? `已選 ${selectedNames.length} 個` : "")}</span>
      </div>}
      <div className="fleet-body">
        <div className="fleet-grid">
          {visibleWorkspaces.map((workspace) => (
            <FleetWorkspaceCard key={workspace.name} workspace={workspace}
              metrics={metricsByWorkspace.get(workspace.name)} roundNow={roundNow} onSelect={onSelect} />
          ))}
          {!visibleWorkspaces.length && <div className="empty-inline">{search.trim() ? "沒有符合搜尋的 workspace" : filter === "all" ? "尚未建立 workspace" : "沒有符合目前篩選的 workspace"}</div>}
        </div>
        <aside className="fleet-events" aria-label="事件推播">
          <div className="fleet-events-head"><strong>事件推播</strong><span className="muted">最近 {events.length} 則</span></div>
          <div className="fleet-events-list">
            {events.map((event, index) => (
              <button key={`${event.ws}-${event.ts}-${index}`} type="button" className="fleet-event" onClick={() => onSelect(event.ws)}>
                <span className="fleet-event-time">{event.time}</span>
                <span className="fleet-event-ws">{event.ws}</span>
                <span className="fleet-event-text">{event.text}</span>
              </button>
            ))}
            {!events.length && <div className="empty-inline">尚無事件——loop 跑完第一輪後會出現在這裡。</div>}
          </div>
        </aside>
      </div>
      {anomaliesOpen && <AnomalyLogModal onClose={() => setAnomaliesOpen(false)} />}
      {bulkAction && <ActionDialog title="確認批次操作" message={`將對 ${eligible.length} 個符合條件的 workspace 執行「${bulkAction === "ack" ? "Issues 已讀" : "立即停止"}」；不符合條件者會跳過。`} confirmLabel={`執行 ${eligible.length} 個`} danger={bulkAction !== "ack"} preview={[
        { label: "符合條件", value: eligible.map((workspace) => workspace.name).join(", ") || "無" },
        { label: "自動跳過", value: selectedWorkspaces.filter((workspace) => !eligible.includes(workspace)).map((workspace) => workspace.name).join(", ") || "無", tone: "safe" },
        { label: "執行方式", value: "逐 workspace 呼叫既有安全 API；單筆失敗不會阻止其他項目" }
      ]} onClose={() => setBulkAction(null)} onConfirm={() => void runBulk()} />}
    </main>
  );
}
