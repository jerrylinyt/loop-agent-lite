/** Ralph runner 操作面（WorkspaceView 的 ralph 對應）：狀態列＋PRD 檢核表＋progress.txt viewer，只做停止／重啟。 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { getRalphPrd, postJson, waitForJobStartup } from "../../shared/api/client";
import type {
  RalphPrdResponse, RalphState, RalphStory, RalphUsageLimit,
  StartupResponse, WorkspaceState, WorkspaceSummary,
} from "../../shared/api/types";
import HorizontalSplitter from "../layout/HorizontalSplitter";
import Modal from "../../shared/components/Modal";
import { searchConsoleText } from "../console/consoleText";
import { formatRoundClock, useRoundNow } from "./roundTiming";
import {
  RALPH_EXIT, formatResumeAt, isModelDowngraded, resumeCountdownSeconds, storyProgressPct, useRalphProgress,
} from "./ralphViewModel";

export default function RalphView({
  workspace,
  state,
  readonly,
  onRefresh,
  onRefreshWorkspaces,
}: {
  workspace?: WorkspaceSummary;
  state: WorkspaceState | null;
  /** ralph stdout 由 App 的右側 ConsolePane 呈現；RalphView 是左側 pane，故不直接使用 consoleText。 */
  consoleText: string;
  readonly: boolean;
  onRefresh: () => void;
  onRefreshWorkspaces: () => void | Promise<void>;
}) {
  const workspaceName = workspace?.name ?? "";
  const running = workspace?.running ?? false;
  const [busy, setBusy] = useState<"stop" | "run" | null>(null);
  const [actionError, setActionError] = useState("");
  const [prd, setPrd] = useState<RalphPrdResponse | null>(null);
  const [prdModalOpen, setPrdModalOpen] = useState(false);
  const [statusHeight, setStatusHeight] = useState(() => +(localStorage.getItem("ralph-progress-height") || 240));
  const [statusCollapsed, setStatusCollapsed] = useState(() => localStorage.getItem("ralph-progress-collapsed") === "1");
  const progressText = useRalphProgress(workspaceName, running);

  useEffect(() => {
    // 讀 PRD 全文供檢核表補上 acceptanceCriteria／description；live 通過狀態仍以 state.ralph.stories 為準。
    if (!workspaceName) { setPrd(null); return; }
    let active = true;
    void getRalphPrd(workspaceName).then((response) => { if (active) setPrd(response); });
    return () => { active = false; };
  }, [workspaceName]);

  const resizeStatus = (pixels: number) => {
    setStatusHeight(pixels);
    localStorage.setItem("ralph-progress-height", String(pixels));
  };
  const toggleStatus = () => {
    setStatusCollapsed((value) => {
      localStorage.setItem("ralph-progress-collapsed", value ? "0" : "1");
      return !value;
    });
  };

  if (!state) return <section className="workspace-pane"><div className="loading-state">載入 workspace…</div></section>;
  if (state.error) return <section className="workspace-pane"><div className="loading-state error">{state.error === "busy" ? "state 更新中…" : state.error}</div></section>;

  const mutate = async (action: "stop" | "run") => {
    setBusy(action);
    setActionError("");
    try {
      const response = await postJson<StartupResponse>(action === "stop" ? "/api/stop" : "/api/run", { name: workspaceName });
      if (response.error) { setActionError(response.error); return; }
      if (response.starting && response.name && response.pid) {
        // /api/run 會重新 spawn ralph（重啟即續跑）；收到 pid 只代表已 spawn，須等 ready marker。
        const startup = await waitForJobStartup(response.name, response.pid, response.startup_timeout);
        if (startup.error) setActionError(startup.error);
      }
      await Promise.all([Promise.resolve(onRefresh()), Promise.resolve(onRefreshWorkspaces())]);
    } finally {
      setBusy(null);
    }
  };

  const ralph: RalphState = state.ralph ?? {};
  const stories = ralph.stories ?? [];
  const storiesDone = ralph.stories_done ?? 0;
  const storiesTotal = ralph.stories_total ?? stories.length;
  const pct = storyProgressPct(storiesDone, storiesTotal);
  const exit = ralph.exit_reason ? RALPH_EXIT[ralph.exit_reason] : null;
  const downgraded = isModelDowngraded(ralph.active_model, state.config?.model);
  const usageLimit = ralph.usage_limit ?? null;

  return (
    <section className="workspace-pane">
      <header className="workspace-header">
        <div className="workspace-title-row">
          <div className="workspace-title">
            <h1>{workspaceName || "ralph"}</h1>
            {exit
              ? <span className={`phase-badge ralph-exit-${exit.tone}`}>{exit.label}</span>
              : running
                ? <span className="phase-badge phase-exec">執行中</span>
                : <span className="phase-badge ralph-exit-muted">已停止</span>}
            <span className="chip subdued ralph-runner-tag">Ralph</span>
          </div>
          {!readonly && workspace && <div className="workspace-actions">
            {running
              ? <button type="button" className="danger-button" disabled={busy !== null} onClick={() => void mutate("stop")}>{busy === "stop" ? "停止中…" : "立即停止"}</button>
              : <button type="button" className="success-button" disabled={busy !== null} onClick={() => void mutate("run")}>{busy === "run" ? "啟動中…" : "重新啟動"}</button>}
          </div>}
        </div>
        {usageLimit && <RalphUsageBanner usageLimit={usageLimit} restartAttempt={ralph.restart_attempt} autoRestartMax={state.config?.auto_restart_max} />}
        <div className="workspace-status-row">
          <div className="primary-status">
            {ralph.project && <span className="chip subdued" title={`專案 ${ralph.project}`}>{ralph.project}</span>}
            {ralph.branch_name && <span className="chip subdued" title={`分支 ${ralph.branch_name}`}>⎇ {ralph.branch_name}</span>}
            <div className="fleet-progress ralph-stories-progress" role="img" aria-label={`Stories ${storiesDone}/${storiesTotal}`}>
              <div className="fleet-progress-fill" style={{ width: `${pct}%` }} />
              <span className="fleet-progress-text">Stories {storiesDone}/{storiesTotal}</span>
            </div>
            <span className="chip">迭代 {ralph.iteration ?? 0}/{ralph.max_iterations ?? "?"}</span>
            {ralph.active_model && <span className={`chip${downgraded ? " warning" : " subdued"}`} title={downgraded ? `已從設定模型 ${state.config?.model} 降級` : "目前使用的模型"}>模型 {ralph.active_model}{downgraded ? " · 已降級" : ""}</span>}
          </div>
          <div className="health-status">
            {ralph.stalled && <span className="chip warning" title="長時間無 stdout／檔案／HEAD 變化">停滯：長時間無進展</span>}
            {ralph.sentinel_complete && !exit && <span className="chip subdued" title="stdout 已出現 &lt;promise&gt;COMPLETE&lt;/promise&gt;">已見完成訊號</span>}
            {typeof ralph.commit_count === "number" && ralph.commit_count > 0 && <span className="chip subdued" title={ralph.last_commit ? `最新 commit：${ralph.last_commit}` : undefined}>commits {ralph.commit_count}</span>}
            {ralph.prd_error && <span className="chip warning" title={ralph.prd_error}>PRD 錯誤</span>}
          </div>
        </div>
        {actionError && <div className="goal-warning" role="alert">操作失敗：{actionError}</div>}
      </header>
      <div className="workspace-main">
        <RalphChecklist
          stories={stories}
          prd={prd}
          prdError={ralph.prd_error}
          storiesDone={storiesDone}
          storiesTotal={storiesTotal}
          onViewRaw={() => setPrdModalOpen(true)}
        />
        {!statusCollapsed && <HorizontalSplitter onResize={resizeStatus} />}
        <div className={`status-console-wrap${statusCollapsed ? " collapsed" : ""}`} style={{ height: statusCollapsed ? 40 : statusHeight }}>
          <RalphProgressPane text={progressText} running={running} iteration={ralph.iteration ?? 0} collapsed={statusCollapsed} onToggleCollapse={toggleStatus} />
        </div>
      </div>
      {prdModalOpen && <RalphPrdModal workspaceName={workspaceName} onClose={() => setPrdModalOpen(false)} />}
    </section>
  );
}

/** usage-limit 顯眼橫幅（RALPH_CONTRACT §I）：等待重啟時顯示 resume_at 與即時倒數；降級／放棄各自文案。 */
function RalphUsageBanner({ usageLimit, restartAttempt, autoRestartMax }: {
  usageLimit: RalphUsageLimit;
  restartAttempt?: number;
  autoRestartMax?: number;
}) {
  const waiting = usageLimit.action === "waiting";
  const now = useRoundNow(waiting && !!usageLimit.resume_at);
  const remaining = waiting ? resumeCountdownSeconds(usageLimit.resume_at, now) : null;
  const attempt = `第 ${restartAttempt ?? 0}/${autoRestartMax ?? "?"} 次`;
  let icon = "⏳";
  let tone = "waiting";
  let message: ReactNode;
  let announce: string;
  if (usageLimit.action === "downgraded") {
    icon = "⬇";
    tone = "downgraded";
    message = <>已降級至 <strong>{usageLimit.to_model ?? "備援模型"}</strong> 並重啟（{attempt}）{usageLimit.from_model ? <> · 原模型 {usageLimit.from_model}</> : null}</>;
    announce = `Agent 用量上限，已降級至 ${usageLimit.to_model ?? "備援模型"} 並重啟，${attempt}`;
  } else if (usageLimit.action === "giveup") {
    icon = "⛔";
    tone = "giveup";
    message = <>已達自動重啟上限（{autoRestartMax ?? "?"} 次），停止自動重啟。</>;
    announce = `Agent 用量上限，已達自動重啟上限 ${autoRestartMax ?? "?"} 次，停止自動重啟`;
  } else {
    const at = formatResumeAt(usageLimit.resume_at);
    message = <>Agent 用量上限，將於 <strong>{at || "稍後"}</strong> 自動重啟（{attempt}）{remaining !== null ? <> · 倒數 <strong aria-hidden="true">{formatRoundClock(remaining)}</strong></> : null}</>;
    announce = `Agent 用量上限，將於 ${at || "稍後"} 自動重啟，${attempt}`;
  }
  return (
    <div className={`ralph-usage-banner ralph-usage-${tone}`} role="status" aria-label={announce}>
      <span className="ralph-usage-icon" aria-hidden="true">{icon}</span>
      <div className="ralph-usage-text">
        <p aria-hidden="true">{message}</p>
        {usageLimit.matched && <p className="ralph-usage-matched" title={usageLimit.matched}>偵測訊號：{usageLimit.matched}</p>}
      </div>
    </div>
  );
}

/** PRD 檢核表：live 通過狀態來自 state.ralph.stories，展開列出 acceptanceCriteria（來自 PRD 全文）。 */
function RalphChecklist({ stories, prd, prdError, storiesDone, storiesTotal, onViewRaw }: {
  stories: RalphStory[];
  prd: RalphPrdResponse | null;
  prdError?: string | null;
  storiesDone: number;
  storiesTotal: number;
  onViewRaw: () => void;
}) {
  const detail = useMemo(() => {
    const map = new Map<string, RalphStory>();
    (prd?.stories ?? []).forEach((story) => map.set(story.id, story));
    return map;
  }, [prd]);

  return (
    <section className="plan-pane ralph-checklist">
      <header className="pane-header">
        <div><strong>PRD 檢核表</strong><span>{storiesDone}/{storiesTotal} 通過</span></div>
        <button type="button" className="secondary-button" onClick={onViewRaw}>查看 PRD 原文</button>
      </header>
      <div className="table-scroll ralph-story-list">
        {prdError && <div className="ralph-prd-error" role="alert">PRD 解析錯誤：{prdError}</div>}
        {!stories.length && !prdError && <div className="ralph-story-empty">尚無 user story；ralph 尚未讀到 PRD 或 PRD 為空。</div>}
        {stories.map((story) => {
          const info = detail.get(story.id);
          const criteria = info?.acceptanceCriteria ?? [];
          const description = info?.description ?? story.description;
          const notes = info?.notes ?? story.notes;
          const hasDetail = !!description || criteria.length > 0 || !!notes;
          return (
            <details key={story.id} className={`ralph-story${story.passes ? " pass" : ""}`}>
              <summary>
                <span className={`ralph-story-check ${story.passes ? "pass" : "fail"}`} aria-hidden="true">{story.passes ? "✓" : "○"}</span>
                <span className="ralph-story-id">{story.id}</span>
                {typeof story.priority === "number" && <span className="chip subdued ralph-story-priority" title={`優先序 ${story.priority}`}>P{story.priority}</span>}
                <span className="ralph-story-title">{story.title}</span>
                <span className="sr-only">{story.passes ? "已通過" : "未通過"}</span>
              </summary>
              <div className="ralph-story-body">
                {description && <p>{description}</p>}
                {criteria.length > 0 && <ul className="ralph-criteria">{criteria.map((item, index) => <li key={index}>{item}</li>)}</ul>}
                {notes && <p className="muted">備註：{notes}</p>}
                {!hasDetail && <p className="muted">按上方「查看 PRD 原文」載入完整驗收條件。</p>}
              </div>
            </details>
          );
        })}
      </div>
    </section>
  );
}

/** progress.txt 尾段檢視器（append-only 進度紀錄）：跟隨最新、可搜尋、可收合。 */
function RalphProgressPane({ text, running, iteration, collapsed, onToggleCollapse }: {
  text: string;
  running: boolean;
  iteration: number;
  collapsed: boolean;
  onToggleCollapse: () => void;
}) {
  const consoleRef = useRef<HTMLPreElement>(null);
  const [follow, setFollow] = useState(true);
  const [search, setSearch] = useState("");
  const visibleText = useMemo(() => searchConsoleText(text, search), [text, search]);

  useEffect(() => {
    if (follow && consoleRef.current) consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
  }, [visibleText, follow]);

  const onScroll = () => {
    const element = consoleRef.current;
    if (!element) return;
    setFollow(element.scrollTop + element.clientHeight >= element.scrollHeight - 60);
  };

  if (collapsed) {
    return (
      <section className="console-pane console-collapsed" aria-label="Ralph 進度紀錄（已收合）">
        <button type="button" className="console-expand-button" onClick={onToggleCollapse} aria-label="展開進度紀錄" title="展開進度紀錄">
          <strong>進度紀錄</strong>
        </button>
      </section>
    );
  }

  return (
    <section className="console-pane" aria-label="Ralph 進度紀錄">
      <header className="pane-header console-header">
        <div className="console-heading">
          <strong>進度紀錄</strong>
          <span>progress.txt · 迭代 {iteration}</span>
        </div>
        <div className="console-tools">
          <input type="search" className="console-search" aria-label="過濾進度紀錄" placeholder="過濾…" value={search} onChange={(event) => setSearch(event.target.value)} />
          <span className={`live-status ${running ? "running" : "idle"}`}><span aria-hidden="true" />{running ? "live" : "idle"}</span>
          <button type="button" className="text-button console-collapse-button" onClick={onToggleCollapse} aria-label="收合進度紀錄" title="收合進度紀錄">收合</button>
        </div>
      </header>
      <pre ref={consoleRef} className="console-output" onScroll={onScroll} tabIndex={0}>
        {visibleText || (search.trim() ? "沒有符合過濾條件的行。" : "尚無進度紀錄；ralph 尚未寫入 progress.txt。")}
      </pre>
      {!follow && <button type="button" className="floating-button" onClick={() => setFollow(true)}>跟到最新</button>}
    </section>
  );
}

/** 「查看 PRD 原文」：開啟即呼叫 getRalphPrd 取最新 raw 與 meta（RALPH_CONTRACT §E）。 */
function RalphPrdModal({ workspaceName, onClose }: { workspaceName: string; onClose: () => void }) {
  const [prd, setPrd] = useState<RalphPrdResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void getRalphPrd(workspaceName).then((response) => {
      if (!active) return;
      setPrd(response);
      setLoading(false);
    });
    return () => { active = false; };
  }, [workspaceName]);

  return (
    <Modal title="PRD 原文" description={`workspace「${workspaceName}」的 PRD 來源檔`} onClose={onClose} wide footer={<button type="button" className="secondary-button" onClick={onClose}>關閉</button>}>
      {loading ? <div className="loading-state">載入 PRD…</div>
        : !prd || prd.error ? <div className="validate-result error" role="alert"><strong>{prd?.error ? `錯誤：${prd.error}` : "無法讀取 PRD"}</strong></div>
          : <>
            <div className="ralph-prd-meta">
              <span>專案：{prd.project || "—"}</span>
              <span>分支：{prd.branch_name || "—"}</span>
              <span>格式：{prd.prd_format || "—"}</span>
              <span>路徑：{prd.prd_path || "—"}</span>
              <span>{prd.stories_done ?? 0}/{prd.stories_total ?? 0} 通過</span>
            </div>
            <pre className="ralph-prd-raw">{prd.raw || "（PRD 原文為空）"}</pre>
          </>}
    </Modal>
  );
}
