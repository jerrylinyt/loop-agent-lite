/** Plan 主視圖：投影完成/目前狀態、追蹤版本更新閃爍，停止時才開啟獨立編輯器。 */
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import type { PlanEditTask, WorkspaceState } from "../../shared/api/types";
import PlanEditorModal from "./PlanEditorModal";

const TaskDiffModal = lazy(() => import("./TaskDiffModal"));

export default function PlanTable({
  state,
  workspace,
  canEdit,
  onSave,
  onGoto
}: {
  state: WorkspaceState;
  workspace: string;
  canEdit: boolean;
  onSave: (tasks: PlanEditTask[], doneCount: number) => Promise<string>;
  onGoto: (order: number) => void;
}) {
  const [showDone, setShowDone] = useState(() => localStorage.getItem("showdone") === "1");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [editorOpen, setEditorOpen] = useState(false);
  const [diffTask, setDiffTask] = useState<{ order: number; title: string; sha: string } | null>(null);
  const [currentOffscreen, setCurrentOffscreen] = useState(false);
  const [flashOrders, setFlashOrders] = useState<Set<number>>(new Set());
  const [updatedVersion, setUpdatedVersion] = useState<number | null>(null);
  const previous = useRef<WorkspaceState | null>(null);
  const flashTimer = useRef<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!previous.current || state.plan_version <= previous.current.plan_version) {
      previous.current = state;
      return;
    }
    const old = new Map((previous.current.plan ?? []).map((task) => [task.order, `${task.task}|${task.ref ?? ""}`]));
    const changed = new Set((state.plan ?? [])
      .filter((task) => old.get(task.order) !== `${task.task}|${task.ref ?? ""}`)
      .map((task) => task.order));
    setFlashOrders(changed);
    setUpdatedVersion(state.plan_version);
    previous.current = state;
    if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => {
      setFlashOrders(new Set());
      setUpdatedVersion(null);
      flashTimer.current = null;
    }, 2400);
  }, [state]);

  useEffect(() => () => {
    if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
  }, []);

  useEffect(() => {
    const current = scrollRef.current?.querySelector<HTMLElement>("tr.current");
    current?.scrollIntoView({ block: "center" });
  }, [state.current_order]);

  const completed = useMemo(
    () => new Map((state.completed ?? []).map((entry) => [entry.order, entry])),
    [state.completed]
  );

  const checkCurrentVisibility = () => {
    const wrap = scrollRef.current;
    const row = wrap?.querySelector<HTMLElement>("tr.current");
    if (!wrap || !row) return setCurrentOffscreen(false);
    const bounds = wrap.getBoundingClientRect();
    const current = row.getBoundingClientRect();
    setCurrentOffscreen(current.bottom < bounds.top || current.top > bounds.bottom);
  };

  const plan = state.plan ?? [];
  const visibleTasks = plan.filter((task) => showDone || !completed.has(task.order));
  return (<>
    <section className="plan-pane">
      <header className={`pane-header plan-header${updatedVersion !== null ? " updated" : ""}`}>
        <div>
          <strong>任務計畫</strong>
          <span>{completed.size}/{plan.length} 已完成</span>
          {updatedVersion !== null && <span className="plan-update-badge" role="status" aria-label={`計畫已更新 v${updatedVersion}`}>計畫已更新 v{updatedVersion}</span>}
        </div>
        {canEdit && plan.length > 0 && <button type="button" className="secondary-button" onClick={() => setEditorOpen(true)}>編輯計畫</button>}
      </header>
      <div className="table-scroll" ref={scrollRef} onScroll={checkCurrentVisibility}>
        <table>
          <thead><tr><th className="number-column">#</th><th>任務</th><th className="status-column">狀態</th></tr></thead>
          <tbody>
            {completed.size > 0 && (
              <tr className="completed-summary"><td colSpan={3}>
                <button
                  type="button"
                  onClick={() => {
                    const next = !showDone;
                    setShowDone(next);
                    localStorage.setItem("showdone", next ? "1" : "0");
                  }}
                  aria-expanded={showDone}
                >
                  {showDone ? "隱藏" : "顯示"}已完成 {completed.size} 條
                </button>
              </td></tr>
            )}
            {visibleTasks.map((task) => {
              const done = completed.get(task.order);
              const current = state.phase !== "plan" && task.order === state.current_order && !done;
              const resetCount = state.task_reset_counts?.[String(task.order)];
              return (
                <tr
                  key={task.order}
                  data-order={task.order}
                  className={`${done ? "completed" : ""}${current ? " current" : ""}${flashOrders.has(task.order) ? " flash" : ""}`}
                >
                  <td>{task.order}</td>
                  <td className="task-cell">
                    <button
                      type="button"
                      className={`task-toggle${expanded.has(task.order) || current ? " expanded" : ""}`}
                      aria-expanded={expanded.has(task.order) || current}
                      onClick={() => setExpanded((orders) => {
                        const next = new Set(orders);
                        if (next.has(task.order)) next.delete(task.order); else next.add(task.order);
                        return next;
                      })}
                    >{task.task}</button>
                    {task.ref && <div className="task-ref">ref: {task.ref}</div>}
                  </td>
                  <td className="task-status">
                    {done ? <span className="task-completion">
                      <span>完成{done.human ? " 人工" : ""}</span>
                      <button type="button" className="commit-sha-button" title={`查看 task-${task.order} 的完整 Git 變更`}
                        aria-label={`查看 task-${task.order} Git 變更 ${done.sha.slice(0, 8)}`}
                        onClick={() => setDiffTask({ order: task.order, title: task.task, sha: done.sha })}>
                        {done.sha.slice(0, 8)}
                      </button>
                    </span> : current ? "進行中" : "等待"}
                    {resetCount ? ` 重置 ${resetCount}` : ""}
                    {canEdit && (state.phase === "exec" || state.phase === "done") && task.order !== state.current_order && (
                      <button type="button" className="goto-button" onClick={() => onGoto(task.order)} aria-label={`把進度設到 task-${task.order}`}>前往</button>
                    )}
                  </td>
                </tr>
              );
            })}
            {!plan.length && <tr><td colSpan={3} className="table-empty">規劃期：計畫尚未建立</td></tr>}
          </tbody>
        </table>
      </div>
      {currentOffscreen && (
        <button type="button" className="floating-button current-jump" onClick={() => {
          scrollRef.current?.querySelector<HTMLElement>("tr.current")?.scrollIntoView({ block: "center" });
        }}>回到執行中</button>
      )}
    </section>
    {editorOpen && <PlanEditorModal state={state} onClose={() => setEditorOpen(false)} onSave={onSave} />}
    {diffTask && <Suspense fallback={null}><TaskDiffModal workspace={workspace} order={diffTask.order} fallbackTitle={diffTask.title}
      fallbackSha={diffTask.sha} onClose={() => setDiffTask(null)} /></Suspense>}
  </>);
}
