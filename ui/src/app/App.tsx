/** Dashboard 根元件：協調全域工具列、workspace/總覽切換、Modal 與鍵盤導覽；業務資料由 useDashboardData 統一提供。 */
import { useMemo, useState } from "react";
import ConsolePane from "../features/console/ConsolePane";
import Splitter from "../features/layout/Splitter";
import LauncherModal from "../features/launcher/LauncherModal";
import ThemePicker from "../features/theme/ThemePicker";
import FleetOverview from "../features/workspaces/FleetOverview";
import WorkspaceTabs from "../features/workspaces/WorkspaceTabs";
import WorkspaceView from "../features/workspaces/WorkspaceView";
import CommandPalette from "../features/workspaces/CommandPalette";
import useDashboardData from "./useDashboardData";
import useStatusFavicon from "./useStatusFavicon";
import GettingStarted from "../features/launcher/GettingStarted";
import useWorkspaceNavigation from "./useWorkspaceNavigation";
import type { DashboardConfig } from "../shared/api/types";

export default function App() {
  const dashboard = useDashboardData();
  const [launcherOpen, setLauncherOpen] = useState(false);
  const [launcherTemplate, setLauncherTemplate] = useState<DashboardConfig | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [overviewOpen, setOverviewOpen] = useState(() => localStorage.getItem("fleet-overview") === "1");
  const [attentionRequest, setAttentionRequest] = useState(0);
  const [leftWidth, setLeftWidth] = useState(() => +(localStorage.getItem("left-pane-width") || Math.round(window.innerWidth * 0.44)));
  const [rightCollapsed, setRightCollapsed] = useState(() => localStorage.getItem("agent-console-collapsed") === "1");
  const workspace = useMemo(
    () => dashboard.workspaces.find((item) => item.name === dashboard.selected),
    [dashboard.workspaces, dashboard.selected]
  );
  useStatusFavicon(workspace, dashboard.state, dashboard.selected);
  const health = dashboard.health;
  const healthText = !health ? "工作區狀態載入中" : health.status === "error" ? "工作區狀態錯誤" : health.status === "degraded" ? "工作區需處理" : "工作區正常";
  const healthLabel = health
    ? `${healthText}：${health.workspace_count} 個 workspace，${health.attention} 項需關注`
    : healthText;
  const resize = (pixels: number) => {
    setLeftWidth(pixels);
    localStorage.setItem("left-pane-width", String(pixels));
  };
  const openLauncher = () => {
    setLauncherTemplate(null);
    setLauncherOpen(true);
  };
  const openLauncherFromTemplate = (config: DashboardConfig) => {
    setLauncherTemplate(config);
    setLauncherOpen(true);
  };
  const closeLauncher = () => {
    setLauncherOpen(false);
    setLauncherTemplate(null);
  };
  const launched = (name: string) => {
    closeLauncher();
    dashboard.selectWorkspace(name);
    void dashboard.refreshWorkspaces();
  };
  const toggleRight = () => {
    setRightCollapsed((value) => {
      localStorage.setItem("agent-console-collapsed", value ? "0" : "1");
      return !value;
    });
  };
  const toggleOverview = () => {
    setOverviewOpen((value) => {
      localStorage.setItem("fleet-overview", value ? "0" : "1");
      return !value;
    });
  };
  const selectFromOverview = (name: string) => {
    dashboard.selectWorkspace(name);
    setOverviewOpen(false);
    localStorage.setItem("fleet-overview", "0");
  };
  const showAttention = () => {
    localStorage.setItem("fleet-filter", "attention");
    localStorage.setItem("fleet-search", "");
    localStorage.setItem("fleet-overview", "1");
    setAttentionRequest((value) => value + 1);
    setOverviewOpen(true);
  };
  const navigationChord = useWorkspaceNavigation({
    workspaces: dashboard.workspaces,
    selectWorkspace: dashboard.selectWorkspace,
    setOverviewOpen,
    setPaletteOpen,
  });
  const paletteCommands = useMemo(() => [
    { id: "overview", label: "開啟 Fleet 總覽", hint: "監控所有 workspace", run: () => { setOverviewOpen(true); localStorage.setItem("fleet-overview", "1"); } },
    ...(!dashboard.bootstrap.readonly ? [{ id: "launch", label: "啟動／管理", hint: "建立或重新啟動 loop", run: () => openLauncher() }] : [])
  ], [dashboard.bootstrap.readonly]);

  return (
    <>
      <a className="skip-link" href="#main-content">跳到主要內容</a>
      <div id="app-shell">
        <header className="app-toolbar">
          <WorkspaceTabs workspaces={dashboard.workspaces} selected={dashboard.selected} onSelect={dashboard.selectWorkspace} />
          <div className="toolbar-actions">
            {dashboard.connection !== "connected" && <span className={`connection-status ${dashboard.connection}`} role="status" aria-live="polite" aria-label={dashboard.connection === "reconnecting" ? "本機連線中斷" : "連線中"} title={dashboard.connection === "reconnecting" ? "Dashboard 與本機服務的事件串流中斷，正在重新連線" : "Dashboard 正在連接本機事件串流"}>
              <span aria-hidden="true">●</span>
              {dashboard.connection === "reconnecting" ? "本機連線中斷，重試中…" : "連線中…"}
            </span>}
            {health && health.status !== "ok" && <button type="button" className={`fleet-health ${health.status}`} aria-label={`${healthLabel}；點擊查看問題`} title="點擊查看需處理的工作區" onClick={showAttention}>
              <span aria-hidden="true">●</span>{healthText}{health.attention > 0 ? ` · ${health.attention}` : ""}
            </button>}
            <ThemePicker />
            <button type="button" className="secondary-button command-palette-trigger" aria-keyshortcuts="Meta+K Control+K" onClick={() => setPaletteOpen(true)}>⌘K</button>
            <button type="button" className={`secondary-button${overviewOpen ? " active-toggle" : ""}`} aria-pressed={overviewOpen} onClick={toggleOverview}>總覽</button>
            {!dashboard.bootstrap.readonly && <button type="button" className="success-button" onClick={() => openLauncher()}>＋ 啟動／管理</button>}
          </div>
        </header>
        {!dashboard.initialized ? (
          <main id="main-content" tabIndex={-1} className="empty-state" aria-busy="true">
            <div className="empty-icon" aria-hidden="true">⌁</div>
            <h1>載入 dashboard…</h1>
          </main>
        ) : !dashboard.workspaces.length ? (
          <main id="main-content" tabIndex={-1} className="empty-state">
            <div className="empty-icon" aria-hidden="true">⌁</div>
            <h1>尚未建立 workspace</h1>
            <p>啟動第一個 loop 後，任務計畫、執行狀態與完整流程紀錄會顯示在這裡。</p>
            {!dashboard.bootstrap.readonly && <button type="button" className="primary-button" onClick={() => openLauncher()}>＋ 啟動第一個 loop</button>}
            <GettingStarted readonly={dashboard.bootstrap.readonly} onLaunch={() => openLauncher()} />
          </main>
        ) : overviewOpen ? (
          <FleetOverview workspaces={dashboard.workspaces} fleetHistory={dashboard.fleetHistory} fleetMetrics={dashboard.fleetMetrics} attentionRequest={attentionRequest} readonly={dashboard.bootstrap.readonly} onSelect={selectFromOverview} onChanged={dashboard.refreshWorkspaces} />
        ) : (
          <main id="main-content" tabIndex={-1} className="dashboard-grid" style={{ gridTemplateColumns: `${leftWidth}px 6px ${rightCollapsed ? "42px" : "minmax(0, 1fr)"}` }}>
            <WorkspaceView key={dashboard.selected} workspace={workspace} state={dashboard.state} consoleText={dashboard.consoleText} readonly={dashboard.bootstrap.readonly} onRefresh={dashboard.refreshState} onRefreshWorkspaces={dashboard.refreshWorkspaces} onLaunchFromTemplate={openLauncherFromTemplate} />
            <Splitter onResize={resize} />
            <ConsolePane text={dashboard.consoleText} round={dashboard.state?.round ?? 0} running={workspace?.running ?? false} hasWorkspace={!!dashboard.selected} collapsed={rightCollapsed} onToggleCollapse={toggleRight} />
          </main>
        )}
      </div>
      {navigationChord && <div className="navigation-chord" role="status">導覽：按 0 回總覽，1～5 切換 workspace</div>}
      {launcherOpen && <LauncherModal workspaces={dashboard.workspaces} templateConfig={launcherTemplate} onClose={closeLauncher} onLaunched={launched} />}
      {paletteOpen && <CommandPalette workspaces={dashboard.workspaces} commands={paletteCommands} onClose={() => setPaletteOpen(false)} onSelectWorkspace={(name) => { dashboard.selectWorkspace(name); setOverviewOpen(false); }} />}
    </>
  );
}
