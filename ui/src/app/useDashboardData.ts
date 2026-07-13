/** Dashboard 資料層：以 REST 做首次讀取、SSE 接收增量 truth，並確保切換 workspace 時不套用舊連線事件。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { getJson } from "../shared/api/client";
import type { BootstrapResponse, FleetHealth, FleetHistoryEntry, FleetRoundMetrics, WorkspaceState, WorkspaceSummary } from "../shared/api/types";

const CONSOLE_LIMIT = 300_000;
export type ConnectionStatus = "connecting" | "connected" | "reconnecting";

export default function useDashboardData() {
  const [bootstrap, setBootstrap] = useState<BootstrapResponse>({ preselect: "" });
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
  const streamGeneration = useRef(0);
  const workspacesRevision = useRef(0);
  const stateRevision = useRef(0);
  const selectedWorkspace = workspaces.find((workspace) => workspace.name === selected);
  const selectedRunId = selectedWorkspace?.fleet_run_id ?? "";
  const selectedGeneration = selectedWorkspace?.workspace_generation ?? "";
  // name 相同但 run-id / generation / kind 改變代表完全不同的 coordinator；不可沿用舊 SSE 或 modal state。
  const selectedIdentity = selectedWorkspace
    ? `${selectedWorkspace.workspace_kind ?? "unknown"}:${selectedRunId || "no-run-id"}:${selectedGeneration || "no-generation"}`
    : "";
  const selectedRunIdRef = useRef(selectedRunId);
  const selectedGenerationRef = useRef(selectedGeneration);

  useEffect(() => { selectedRef.current = selected; }, [selected]);
  useEffect(() => { bootstrapRef.current = bootstrap; }, [bootstrap]);
  useEffect(() => { selectedRunIdRef.current = selectedRunId; }, [selectedRunId]);
  useEffect(() => { selectedGenerationRef.current = selectedGeneration; }, [selectedGeneration]);

  const selectWorkspace = useCallback((name: string) => {
    // 先清掉上一個 workspace 的 state/console，避免新 SSE 建立前短暫顯示錯誤內容。
    if (!name || name === selectedRef.current) return;
    stateRevision.current += 1;
    selectedRef.current = name;
    setSelected(name);
    setState(null);
    setConsoleText("");
    localStorage.setItem("workspace", name);
    history.replaceState(null, "", `#${encodeURIComponent(name)}`);
  }, []);

  const applyWorkspaces = useCallback((list: WorkspaceSummary[]) => {
    const currentName = selectedRef.current;
    const nextSelected = list.find((workspace) => workspace.name === currentName);
    if (currentName && nextSelected) {
      const nextRunId = nextSelected.fleet_run_id ?? "";
      const nextGeneration = nextSelected.workspace_generation ?? "";
      if (nextRunId !== selectedRunIdRef.current || nextGeneration !== selectedGenerationRef.current) {
        // 同名 replacement 也是新的 state identity；同步讓舊 REST state request 作廢。
        stateRevision.current += 1;
        selectedRunIdRef.current = nextRunId;
        selectedGenerationRef.current = nextGeneration;
        setState(null);
      }
    }
    setWorkspaces(list);
    if (!list.length) {
      stateRevision.current += 1;
      selectedRef.current = "";
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
    const revision = workspacesRevision.current + 1;
    workspacesRevision.current = revision;
    const workspace = selectedRef.current;
    const runId = selectedRunIdRef.current;
    const generation = selectedGenerationRef.current;
    const list = await getJson<WorkspaceSummary[]>("/api/workspaces");
    if (list && revision === workspacesRevision.current &&
        workspace === selectedRef.current && runId === selectedRunIdRef.current &&
        generation === selectedGenerationRef.current) applyWorkspaces(list);
  }, [applyWorkspaces]);

  const refreshHealth = useCallback(async () => {
    const next = await getJson<FleetHealth>("/api/health");
    if (next) setHealth(next);
  }, []);

  const refreshState = useCallback(async () => {
    const workspace = selectedRef.current;
    const runId = selectedRunIdRef.current;
    const generation = selectedGenerationRef.current;
    if (!workspace) return;
    const revision = stateRevision.current + 1;
    stateRevision.current = revision;
    const next = await getJson<WorkspaceState>(`/api/state?ws=${encodeURIComponent(workspace)}`);
    if (revision === stateRevision.current && workspace === selectedRef.current &&
        runId === selectedRunIdRef.current && generation === selectedGenerationRef.current && next &&
        (!runId || next.error || next.fleet_run_id === runId) &&
        (!generation || next.error || next.workspace_generation === generation)) setState(next);
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
    stateRevision.current += 1;
    setState(null);
    setConsoleText("");
    const generation = ++streamGeneration.current;
    const streamWorkspace = selected;
    const streamRunId = selectedRunId;
    const streamWorkspaceGeneration = selectedGeneration;
    const isCurrentStreamIdentity = () => generation === streamGeneration.current &&
      selectedRef.current === streamWorkspace && selectedRunIdRef.current === streamRunId &&
      selectedGenerationRef.current === streamWorkspaceGeneration;
    const params = new URLSearchParams({ fleet: "1" });
    if (selected) params.set("ws", selected);
    // selected 改變就關閉舊 EventSource 並建立新連線；cleanup 是防止跨 workspace 串流混入的關鍵。
    const source = new EventSource(`/api/events?${params.toString()}`);
    source.onopen = () => {
      if (!isCurrentStreamIdentity()) return;
      setConnection("connected");
      setConsoleText("");
    };
    source.onerror = () => {
      if (isCurrentStreamIdentity()) setConnection("reconnecting");
    };
    source.addEventListener("workspaces", (event) => {
      if (isCurrentStreamIdentity()) {
        workspacesRevision.current += 1;
        applyWorkspaces(JSON.parse(event.data) as WorkspaceSummary[]);
      }
    });
    source.addEventListener("health", (event) => {
      if (isCurrentStreamIdentity()) setHealth(JSON.parse(event.data) as FleetHealth);
    });
    source.addEventListener("state", (event) => {
      if (!isCurrentStreamIdentity()) return;
      const next = JSON.parse(event.data) as WorkspaceState;
      // 同名 workspace 被刪除重建時，舊連線可能在 cleanup 前送來新 run 的 state；
      // 先拒絕 identity 不符資料，等 selectedIdentity 觸發新 EventSource。
      if (streamRunId && !next.error && next.fleet_run_id !== streamRunId) return;
      if (streamWorkspaceGeneration && !next.error && next.workspace_generation !== streamWorkspaceGeneration) return;
      stateRevision.current += 1;
      setState(next);
    });
    source.addEventListener("fleet-history", (event) => {
      if (isCurrentStreamIdentity()) setFleetHistory(JSON.parse(event.data) as FleetHistoryEntry[]);
    });
    source.addEventListener("fleet-round-metrics", (event) => {
      if (isCurrentStreamIdentity()) setFleetMetrics(JSON.parse(event.data) as FleetRoundMetrics);
    });
    source.addEventListener("console", (event) => {
      if (!isCurrentStreamIdentity()) return;
      const { data } = JSON.parse(event.data) as { data: string };
      setConsoleText((text) => (text + data).slice(-CONSOLE_LIMIT));
    });
    return () => {
      source.close();
      if (streamGeneration.current === generation) streamGeneration.current += 1;
    };
  }, [applyWorkspaces, initialized, selected, selectedGeneration, selectedIdentity, selectedRunId]);

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
