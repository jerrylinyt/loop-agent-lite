/** Fleet 監控總覽：聚合工作區、效能與事件，並處理本機視圖、篩選、排序及有條件的批次操作。 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { FleetHistoryEntry, FleetRoundMetrics, WorkspaceSummary } from "../../shared/api/types";
import AnomalyLogModal from "./AnomalyLogModal";
import { deriveFleetEvents } from "./fleetEvents";
import { useRoundNow } from "./roundTiming";
import { postJson } from "../../shared/api/client";
import ActionDialog from "../../shared/components/ActionDialog";
import FleetWorkspaceCard from "./FleetWorkspaceCard";
import {
  formatMetric, initialFleetFilter, initialFleetSort, loadSavedViews,
  overviewWorkspaceProjection, visibleFleetWorkspaces, workspaceNeedsAttention,
} from "./fleetViewModel";
import type { FleetFilter, FleetSort, SavedFleetView } from "./fleetViewModel";
import type { BeginOperation, EndOperation } from "../../shared/operationGate";

type BulkAction = "ack" | "stop" | "delete";
interface BulkTargetSnapshot {
  name: string;
  workspace_kind?: WorkspaceSummary["workspace_kind"];
  fleet_run_id?: string | null;
  workspace_generation?: string;
  loop_pid?: number | null;
  issues?: number;
  unread_issues?: number;
  running: boolean;
}
interface BulkConfirmation {
  action: BulkAction;
  eligible: BulkTargetSnapshot[];
  skipped: BulkTargetSnapshot[];
}

function snapshotBulkTarget(workspace: WorkspaceSummary): BulkTargetSnapshot {
  return {
    name: workspace.name,
    workspace_kind: workspace.workspace_kind,
    fleet_run_id: workspace.fleet_run_id,
    workspace_generation: workspace.workspace_generation,
    loop_pid: workspace.loop_pid,
    issues: workspace.issues,
    unread_issues: workspace.unread_issues,
    running: workspace.running,
  };
}

/** 監控電視牆:聚合統計 + 全 fleet 即時卡片 + 事件推播。
 * 卡片與歷史事件都走同一條 SSE；事件流仍由前端從 history 尾段推導。 */
export default function FleetOverview({ workspaces, fleetHistory, fleetMetrics, attentionRequest, operationPending, beginOperation, endOperation, onSelect, onChanged }: {
  workspaces: WorkspaceSummary[];
  fleetHistory: FleetHistoryEntry[];
  fleetMetrics: FleetRoundMetrics | null;
  attentionRequest: number;
  operationPending: boolean;
  beginOperation: BeginOperation;
  endOperation: EndOperation;
  onSelect: (name: string) => void;
  onChanged: () => void | Promise<void>;
}) {
  // fleet-child 由 parent 卡片的 track grid 投影；總覽不再把同一個 parallel run
  // 重複算成 parent + N 個獨立 workspace。child 仍可由 workspace tabs 直接查看診斷。
  const overviewWorkspaces = useMemo(() => {
    // 正常 child 由 parent 卡片聚合；orphan 則保留獨立卡片供診斷。
    return overviewWorkspaceProjection(workspaces);
  }, [workspaces]);
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
  const [savedViews, setSavedViews] = useState<SavedFleetView[]>(loadSavedViews);
  const [selectedView, setSelectedView] = useState("");
  const [savingView, setSavingView] = useState(false);
  const [viewName, setViewName] = useState("");
  const [viewMessage, setViewMessage] = useState("");
  const [anomaliesOpen, setAnomaliesOpen] = useState(false);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [selectedTargets, setSelectedTargets] = useState<BulkTargetSnapshot[]>([]);
  const [bulkConfirmation, setBulkConfirmation] = useState<BulkConfirmation | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const bulkPending = useRef(false);
  const [bulkMessage, setBulkMessage] = useState("");
  const bulkBlocked = bulkBusy || operationPending;
  const selectedStoppable = selectedTargets.filter((workspace) => workspace.running && workspace.workspace_kind !== "fleet-child");
  const selectedGracefulStops = selectedStoppable.filter((workspace) => workspace.workspace_kind === "fleet-parent");
  const selectedImmediateStops = selectedStoppable.filter((workspace) => workspace.workspace_kind !== "fleet-parent");
  const stopActionLabel = selectedGracefulStops.length && !selectedImmediateStops.length ? "本輪後停止"
    : selectedImmediateStops.length && !selectedGracefulStops.length ? "立即停止"
      : "停止（依類型）";
  const openBulkConfirmation = (action: BulkAction) => {
    // Eligibility and identity are one confirmation snapshot. A same-name replacement arriving
    // over SSE while the dialog is open must never retarget the pending confirmation.
    const candidates = [...selectedTargets];
    const eligible = action === "ack" ? candidates.filter((workspace) => workspace.workspace_kind === "standalone" && (workspace.unread_issues ?? workspace.issues ?? 0) > 0 && !workspace.running)
      : action === "stop" ? candidates.filter((workspace) => workspace.running && workspace.workspace_kind !== "fleet-child")
        : candidates.filter((workspace) => !workspace.running && workspace.workspace_kind !== "fleet-child");
    const eligibleNames = new Set(eligible.map((workspace) => workspace.name));
    setBulkConfirmation({
      action,
      eligible: [...eligible],
      skipped: candidates.filter((workspace) => !eligibleNames.has(workspace.name)),
    });
  };
  const gracefulStopTargets = bulkConfirmation?.action === "stop"
    ? bulkConfirmation.eligible.filter((workspace) => workspace.workspace_kind === "fleet-parent") : [];
  const immediateStopTargets = bulkConfirmation?.action === "stop"
    ? bulkConfirmation.eligible.filter((workspace) => workspace.workspace_kind !== "fleet-parent") : [];
  const runBulk = async () => {
    if (!bulkConfirmation || bulkBlocked || bulkPending.current) return;
    const { action, eligible: targets } = bulkConfirmation;
    const token = beginOperation(`bulk:${action}`);
    if (!token) return;
    bulkPending.current = true;
    setBulkBusy(true);
    setBulkMessage("批次處理中…");
    setBulkConfirmation(null);
    let failed = 0;
    try {
      // 刻意逐筆呼叫既有單 workspace API：單筆鎖或狀態失敗不回滾其他 workspace，
      // 也不另開一條能繞過既有安全檢查的批次後端捷徑。
      for (const workspace of targets) {
        const expectedPid = action === "stop" && Number.isInteger(workspace.loop_pid)
          ? { expected_pid: workspace.loop_pid }
          : {};
        const identity = workspace.workspace_kind === "fleet-parent"
          ? { run_id: workspace.fleet_run_id, ...expectedPid }
          : {
              workspace_generation: workspace.workspace_generation,
              ...expectedPid,
            };
        const response = action === "ack" ? await postJson("/api/edit-state", { name: workspace.name, ack_issues: true, ...identity })
          : action === "stop" ? await postJson("/api/stop", { name: workspace.name, ...identity })
            : await postJson("/api/delete-workspace", { name: workspace.name, ...identity });
        if (response.error) failed += 1;
      }
      setBulkMessage(`已處理 ${targets.length - failed}/${targets.length} 個 workspace${failed ? `，${failed} 個失敗` : ""}`);
      setSelectedTargets([]);
      await Promise.resolve(onChanged());
    } finally {
      bulkPending.current = false;
      setBulkBusy(false);
      endOperation(token);
    }
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
    setSelectedView("");
    localStorage.setItem("fleet-filter", next);
  };
  const changeSearch = (next: string) => {
    setSearch(next);
    setSelectedView("");
    localStorage.setItem("fleet-search", next);
  };
  const changeSort = (next: FleetSort) => {
    setSort(next);
    setSelectedView("");
    localStorage.setItem("fleet-sort", next);
  };
  const changeCompact = (next: boolean) => {
    setCompact(next);
    setSelectedView("");
    localStorage.setItem("fleet-compact", next ? "1" : "0");
  };
  const persistViews = (views: SavedFleetView[]) => {
    setSavedViews(views);
    localStorage.setItem("fleet-saved-views", JSON.stringify(views));
  };
  const saveView = () => {
    const name = viewName.trim();
    if (!name) return setViewMessage("請輸入視圖名稱");
    if (name.length > 40) return setViewMessage("視圖名稱不可超過 40 字");
    const existing = savedViews.find((item) => item.name === name);
    const next: SavedFleetView = {
      id: existing?.id ?? `view-${Date.now().toString(36)}`,
      name, filter, search, sort, compact
    };
    const views = existing ? savedViews.map((item) => item.id === existing.id ? next : item)
      : [...savedViews, next].slice(-20);
    persistViews(views);
    setSelectedView(next.id);
    setSavingView(false);
    setViewName("");
    setViewMessage(`已儲存「${name}」`);
  };
  const applyView = (id: string) => {
    setSelectedView(id);
    const view = savedViews.find((item) => item.id === id);
    if (!view) return;
    setFilter(view.filter);
    setSearch(view.search);
    setSort(view.sort);
    setCompact(view.compact);
    localStorage.setItem("fleet-filter", view.filter);
    localStorage.setItem("fleet-search", view.search);
    localStorage.setItem("fleet-sort", view.sort);
    localStorage.setItem("fleet-compact", view.compact ? "1" : "0");
    setViewMessage(`已套用「${view.name}」`);
  };
  const deleteView = () => {
    const selected = savedViews.find((item) => item.id === selectedView);
    if (!selected) return;
    persistViews(savedViews.filter((item) => item.id !== selected.id));
    setSelectedView("");
    setViewMessage(`已刪除「${selected.name}」`);
  };

  const running = overviewWorkspaces.filter((workspace) => workspace.running).length;
  const done = overviewWorkspaces.filter((workspace) => workspace.phase === "done" || workspace.parallel_phase === "done").length;
  const planning = overviewWorkspaces.filter((workspace) => workspace.workspace_kind === "fleet-parent"
    ? ["planning", "awaiting-approval"].includes(workspace.parallel_phase ?? "planning")
    : workspace.phase === "plan").length;
  const executing = overviewWorkspaces.filter((workspace) => workspace.workspace_kind === "fleet-parent"
    ? !["planning", "awaiting-approval", "done", "failed", "stopped"].includes(workspace.parallel_phase ?? "planning")
    : workspace.phase === "exec").length;
  const totalTasks = overviewWorkspaces.reduce((sum, workspace) => sum + (workspace.plan_len ?? 0), 0);
  const doneTasks = overviewWorkspaces.reduce((sum, workspace) => sum + (workspace.completed ?? 0), 0);
  const alerts = overviewWorkspaces.filter(workspaceNeedsAttention).length;
  const visibleWorkspaces = useMemo(
    () => visibleFleetWorkspaces(overviewWorkspaces, filter, search, sort),
    [filter, search, sort, overviewWorkspaces]
  );
  const filters: Array<{ id: FleetFilter; label: string; count: number }> = [
    { id: "all", label: "全部", count: overviewWorkspaces.length },
    { id: "attention", label: "需關注", count: alerts },
    { id: "running", label: "執行中", count: running },
    { id: "done", label: "已完成", count: done },
  ];
  const taskPct = totalTasks > 0 ? Math.round((doneTasks / totalTasks) * 100) : 0;

  return (
    <main id="main-content" tabIndex={-1} className={`fleet-overview${compact ? " compact-cards" : ""}`} aria-label="工作區總覽">
      <div className="fleet-stats" role="list" aria-label="工作區統計">
        <div className="fleet-stat" role="listitem"><strong>{overviewWorkspaces.length}</strong><span>workspace groups</span></div>
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
        <span className="muted">顯示 {visibleWorkspaces.length} / {overviewWorkspaces.length}</span>
      </div>
      <div className="saved-view-row">
        <select aria-label="已儲存監控視圖" value={selectedView} onChange={(event) => applyView(event.target.value)}>
          <option value="">已儲存視圖…</option>
          {savedViews.map((view) => <option key={view.id} value={view.id}>{view.name}</option>)}
        </select>
        {!savingView && <button type="button" className="secondary-button compact-button" onClick={() => { setSavingView(true); setViewMessage(""); }}>儲存目前視圖</button>}
        {savingView && <><input aria-label="監控視圖名稱" placeholder="例如：值班問題牆" value={viewName} onChange={(event) => setViewName(event.target.value)} maxLength={40} /><button type="button" className="primary-button compact-button" onClick={saveView}>儲存</button><button type="button" className="secondary-button compact-button" onClick={() => setSavingView(false)}>取消</button></>}
        <button type="button" className="danger-button compact-button" disabled={!selectedView} onClick={deleteView}>刪除視圖</button>
        <span className="muted" role="status">{viewMessage || `${savedViews.length}/20 個個人視圖`}</span>
      </div>
      <div className="bulk-toolbar">
        <button type="button" className="secondary-button compact-button" aria-expanded={bulkOpen} disabled={bulkBlocked} onClick={() => setBulkOpen((value) => !value)}>☑ 批次操作</button>
        {bulkOpen && <><select multiple aria-label="批次選擇 workspace" disabled={bulkBlocked} value={selectedTargets.map((workspace) => workspace.name)} onChange={(event) => {
          const names = [...event.target.selectedOptions].map((option) => option.value);
          setSelectedTargets((current) => {
            const frozen = new Map(current.map((workspace) => [workspace.name, workspace]));
            const visible = new Map(overviewWorkspaces.map((workspace) => [workspace.name, workspace]));
            return names.flatMap((name) => {
              const snapshot = frozen.get(name) ?? (visible.get(name) ? snapshotBulkTarget(visible.get(name)!) : undefined);
              return snapshot ? [snapshot] : [];
            });
          });
        }}>{visibleWorkspaces.map((workspace) => <option key={workspace.name} value={workspace.name}>{workspace.name} · {workspace.running ? "執行中" : "已停止"}</option>)}</select>
          <button type="button" className="secondary-button compact-button" disabled={bulkBlocked || !selectedTargets.length} onClick={() => openBulkConfirmation("ack")}>Issues 已讀</button>
          <button type="button" className="danger-button compact-button" disabled={bulkBlocked || !selectedTargets.length} onClick={() => openBulkConfirmation("stop")}>{stopActionLabel}</button>
          <button type="button" className="danger-button compact-button" disabled={bulkBlocked || !selectedTargets.length} onClick={() => openBulkConfirmation("delete")}>刪除</button></>}
        <span className="muted" role="status">{bulkMessage || (selectedTargets.length ? `已選 ${selectedTargets.length} 個` : "")}</span>
      </div>
      <div className="fleet-body">
        <div className="fleet-grid">
          {visibleWorkspaces.map((workspace) => (
            <FleetWorkspaceCard key={workspace.name} workspace={workspace}
              metrics={metricsByWorkspace.get(workspace.name)} roundNow={roundNow} disabled={operationPending} onSelect={onSelect} />
          ))}
          {!visibleWorkspaces.length && <div className="empty-inline">{search.trim() ? "沒有符合搜尋的 workspace" : filter === "all" ? "尚未建立 workspace" : "沒有符合目前篩選的 workspace"}</div>}
        </div>
        <aside className="fleet-events" aria-label="事件推播">
          <div className="fleet-events-head"><strong>事件推播</strong><span className="muted">最近 {events.length} 則</span></div>
          <div className="fleet-events-list">
            {events.map((event, index) => (
              <button key={`${event.ws}-${event.ts}-${index}`} type="button" className="fleet-event" disabled={operationPending} onClick={() => onSelect(event.ws)}>
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
      {bulkConfirmation && <ActionDialog title="確認批次操作" message={bulkConfirmation.action === "stop"
        ? `將停止 ${bulkConfirmation.eligible.length} 個符合條件的 workspace：Parallel parent 會在 active round 完整結束後停止；Standalone 會立即停止。不符合條件者會跳過。`
        : `將對 ${bulkConfirmation.eligible.length} 個符合條件的 workspace 執行「${bulkConfirmation.action === "ack" ? "Issues 已讀" : "永久刪除"}」；不符合條件者會跳過。`} confirmLabel={`執行 ${bulkConfirmation.eligible.length} 個`} danger={bulkConfirmation.action !== "ack"} preview={[
        ...(bulkConfirmation.action === "stop" ? [
          { label: "本輪後停止（Parallel）", value: gracefulStopTargets.map((workspace) => workspace.name).join(", ") || "無" },
          { label: "立即停止（Standalone）", value: immediateStopTargets.map((workspace) => workspace.name).join(", ") || "無" },
        ] : [{ label: "符合條件", value: bulkConfirmation.eligible.map((workspace) => workspace.name).join(", ") || "無" }]),
        { label: "自動跳過", value: bulkConfirmation.skipped.map((workspace) => workspace.name).join(", ") || "無", tone: "safe" },
        { label: "執行方式", value: "逐 workspace 呼叫既有安全 API；單筆失敗不會阻止其他項目" }
      ]} onClose={() => setBulkConfirmation(null)} onConfirm={() => void runBulk()} />}
    </main>
  );
}
