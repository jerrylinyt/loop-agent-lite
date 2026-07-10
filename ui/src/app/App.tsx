import { useMemo, useState } from "react";
import ConsolePane from "../features/console/ConsolePane";
import Splitter from "../features/layout/Splitter";
import LauncherModal from "../features/launcher/LauncherModal";
import ThemePicker from "../features/theme/ThemePicker";
import ArchivesModal from "../features/workspaces/ArchivesModal";
import WorkspaceTabs from "../features/workspaces/WorkspaceTabs";
import WorkspaceView from "../features/workspaces/WorkspaceView";
import useDashboardData from "./useDashboardData";

export default function App() {
  const dashboard = useDashboardData();
  const [launcherOpen, setLauncherOpen] = useState(false);
  const [archivesOpen, setArchivesOpen] = useState(false);
  const [leftWidth, setLeftWidth] = useState(() => +(localStorage.getItem("left-pane-width") || Math.round(window.innerWidth * 0.44)));
  const [rightCollapsed, setRightCollapsed] = useState(() => localStorage.getItem("agent-console-collapsed") === "1");
  const workspace = useMemo(
    () => dashboard.workspaces.find((item) => item.name === dashboard.selected),
    [dashboard.workspaces, dashboard.selected]
  );
  const resize = (pixels: number) => {
    setLeftWidth(pixels);
    localStorage.setItem("left-pane-width", String(pixels));
  };
  const launched = (name: string) => {
    setLauncherOpen(false);
    dashboard.selectWorkspace(name);
    void dashboard.refreshWorkspaces();
  };
  const toggleRight = () => {
    setRightCollapsed((value) => {
      localStorage.setItem("agent-console-collapsed", value ? "0" : "1");
      return !value;
    });
  };
  const restored = async (name: string) => {
    dashboard.selectWorkspace(name);
    await dashboard.refreshWorkspaces();
    setArchivesOpen(false);
  };

  return (
    <>
      <div id="app-shell">
        <header className="app-toolbar">
          <WorkspaceTabs workspaces={dashboard.workspaces} selected={dashboard.selected} onSelect={dashboard.selectWorkspace} />
          <div className="toolbar-actions">
            <ThemePicker />
            <button type="button" className="secondary-button" onClick={() => setArchivesOpen(true)}>🗃 已封存</button>
            {!dashboard.bootstrap.readonly && <button type="button" className="success-button" onClick={() => setLauncherOpen(true)}>＋ 啟動／管理</button>}
          </div>
        </header>
        {!dashboard.initialized ? (
          <main className="empty-state" aria-busy="true">
            <div className="empty-icon" aria-hidden="true">⌁</div>
            <h1>載入 dashboard…</h1>
          </main>
        ) : !dashboard.workspaces.length ? (
          <main className="empty-state">
            <div className="empty-icon" aria-hidden="true">⌁</div>
            <h1>尚未建立 workspace</h1>
            <p>啟動第一個 loop 後，任務計畫、執行狀態與完整流程紀錄會顯示在這裡。</p>
            {!dashboard.bootstrap.readonly && <button type="button" className="primary-button" onClick={() => setLauncherOpen(true)}>＋ 啟動第一個 loop</button>}
          </main>
        ) : (
          <main className="dashboard-grid" style={{ gridTemplateColumns: `${leftWidth}px 6px ${rightCollapsed ? "42px" : "minmax(0, 1fr)"}` }}>
            <WorkspaceView key={dashboard.selected} workspace={workspace} state={dashboard.state} consoleText={dashboard.consoleText} readonly={dashboard.bootstrap.readonly} onRefresh={dashboard.refreshState} onRefreshWorkspaces={dashboard.refreshWorkspaces} />
            <Splitter onResize={resize} />
            <ConsolePane text={dashboard.consoleText} round={dashboard.state?.round ?? 0} running={workspace?.running ?? false} hasWorkspace={!!dashboard.selected} collapsed={rightCollapsed} onToggleCollapse={toggleRight} />
          </main>
        )}
      </div>
      {launcherOpen && <LauncherModal workspaces={dashboard.workspaces} onClose={() => setLauncherOpen(false)} onLaunched={launched} />}
      {archivesOpen && <ArchivesModal readonly={dashboard.bootstrap.readonly} onClose={() => setArchivesOpen(false)} onRestored={restored} />}
    </>
  );
}
