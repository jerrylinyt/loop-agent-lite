/** Dashboard 資料層：以 REST 做首次讀取、SSE 接收增量 truth，並確保切換 workspace 時不套用舊連線事件。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { getJson } from "../shared/api/client";
import type { BootstrapResponse, FleetHealth, FleetHistoryEntry, FleetRoundMetrics, WorkspaceState, WorkspaceSummary } from "../shared/api/types";
import { appendConsoleText } from "../features/console/consoleText";

const CONSOLE_LIMIT = 300_000;
export type ConnectionStatus = "connecting" | "connected" | "reconnecting";

export default function useDashboardData() {
  const [bootstrap, setBootstrap] = useState<BootstrapResponse>({ readonly: true, preselect: "" });
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [health, setHealth] = useState<FleetHealth | null>(null);
  const [fleetHistory, setFleetHistory] = useState<FleetHistoryEntry[]>([]);
  const [fleetMetrics, setFleetMetrics] = useState<FleetRoundMetrics | null>(null);
  const [selected, setSelected] = useState("");
  const [state, setState] = useState<WorkspaceState | null>(null);
  const [consoleText, setConsoleText] = useState("");
  const [initialized, setInitialized] = useState(false);
  const [connection, setConnection] = useState<ConnectionStatus>("connecting");
  const selectedRef = useRef("");
  const bootstrapRef = useRef(bootstrap);

  useEffect(() => { selectedRef.current = selected; }, [selected]);
  useEffect(() => { bootstrapRef.current = bootstrap; }, [bootstrap]);

  const selectWorkspace = useCallback((name: string) => {
    // 先清掉上一個 workspace 的 state/console，避免新 SSE 建立前短暫顯示錯誤內容。
    if (!name || name === selectedRef.current) return;
    setSelected(name);
    setState(null);
    setConsoleText("");
    localStorage.setItem("workspace", name);
    history.replaceState(null, "", `#${encodeURIComponent(name)}`);
  }, []);

  const applyWorkspaces = useCallback((list: WorkspaceSummary[]) => {
    setWorkspaces(list);
    if (!list.length) {
      setSelected("");
      setState(null);
      return;
    }
    if (!selectedRef.current || !list.some((workspace) => workspace.name === selectedRef.current)) {
      const hash = decodeURIComponent(location.hash.replace(/^#/, ""));
      // 選取優先序：明確 hash > 後端 preselect > 本機上次選擇 > fleet 第一筆。
      const preferred = [hash, bootstrapRef.current.preselect, localStorage.getItem("workspace")]
        .find((name) => name && list.some((workspace) => workspace.name === name));
      selectWorkspace(preferred || list[0].name);
    }
  }, [selectWorkspace]);

  const refreshWorkspaces = useCallback(async () => {
    const list = await getJson<WorkspaceSummary[]>("/api/workspaces");
    if (list) applyWorkspaces(list);
  }, [applyWorkspaces]);

  const refreshHealth = useCallback(async () => {
    const next = await getJson<FleetHealth>("/api/health");
    if (next) setHealth(next);
  }, []);

  const refreshState = useCallback(async () => {
    const workspace = selectedRef.current;
    if (!workspace) return;
    const next = await getJson<WorkspaceState>(`/api/state?ws=${encodeURIComponent(workspace)}`);
    if (workspace === selectedRef.current && next) setState(next);
  }, []);

  useEffect(() => {
    void (async () => {
      const value = await getJson<BootstrapResponse>("/api/bootstrap");
      if (value) {
        bootstrapRef.current = value;
        setBootstrap(value);
      }
      await refreshWorkspaces();
      await refreshHealth();
      setInitialized(true);
    })();
  }, [refreshHealth, refreshWorkspaces]);

  useEffect(() => {
    if (!initialized) return;
    setConnection("connecting");
    const params = new URLSearchParams({ fleet: "1" });
    if (selected) params.set("ws", selected);
    // selected 改變就關閉舊 EventSource 並建立新連線；cleanup 是防止跨 workspace 串流混入的關鍵。
    const source = new EventSource(`/api/events?${params.toString()}`);
    source.onopen = () => {
      setConnection("connected");
      setConsoleText("");
    };
    source.onerror = () => setConnection("reconnecting");
    source.addEventListener("workspaces", (event) => applyWorkspaces(JSON.parse(event.data) as WorkspaceSummary[]));
    source.addEventListener("health", (event) => setHealth(JSON.parse(event.data) as FleetHealth));
    source.addEventListener("state", (event) => setState(JSON.parse(event.data) as WorkspaceState));
    source.addEventListener("fleet-history", (event) => {
      setFleetHistory(JSON.parse(event.data) as FleetHistoryEntry[]);
    });
    source.addEventListener("fleet-round-metrics", (event) => {
      setFleetMetrics(JSON.parse(event.data) as FleetRoundMetrics);
    });
    source.addEventListener("console", (event) => {
      const { data } = JSON.parse(event.data) as { data: string };
      setConsoleText((text) => appendConsoleText(text, data, CONSOLE_LIMIT));
    });
    return () => source.close();
  }, [applyWorkspaces, initialized, selected]);

  return {
    initialized,
    connection,
    bootstrap,
    workspaces,
    health,
    selected,
    state,
    fleetHistory,
    fleetMetrics,
    consoleText,
    selectWorkspace,
    refreshState,
    refreshWorkspaces,
    refreshHealth
  };
}
