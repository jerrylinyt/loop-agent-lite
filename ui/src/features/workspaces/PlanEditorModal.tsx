/** 全畫面 Plan 編輯器：鎖住已完成/目前任務，只讓 pending 區段插入、刪除、拖移與送出版本化快照。 */
import { useMemo, useRef, useState } from "react";
import type { PlanEditTask, WorkspaceState } from "../../shared/api/types";
import ActionDialog from "../../shared/components/ActionDialog";
import Modal from "../../shared/components/Modal";

interface DraftTask extends PlanEditTask { id: string }

export default function PlanEditorModal({ state, onClose, onSave }: {
  state: WorkspaceState;
  onClose: () => void;
  onSave: (tasks: PlanEditTask[], doneCount: number) => Promise<string>;
}) {
  const original = state.plan ?? [];
  const completed = useMemo(() => new Set((state.completed ?? []).map((entry) => entry.order)), [state.completed]);
  const lockedCount = useMemo(() => {
    // 鎖定區採「前綴」而不是零散列：完成任務或目前任務之前的順序都屬既有執行歷史，
    // 即使其中某列不是 completed，也不能讓 pending task 穿越這條邊界。
    const locked = new Set(completed);
    if (state.phase === "exec" && state.current_order) locked.add(state.current_order);
    return Math.max(-1, ...original.map((task, index) => locked.has(task.order) ? index : -1)) + 1;
  }, [completed, original, state.current_order, state.phase]);
  const initial = useMemo<DraftTask[]>(() => original.map((task) => ({ ...task, id: `task-${task.order}` })), [original]);
  const [drafts, setDrafts] = useState(initial);
  const [doneCount, setDoneCount] = useState(state.done_count ?? 0);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [dropIndex, setDropIndex] = useState<number | null>(null);
  const nextId = useRef(1);
  const existingOrders = new Set(drafts.flatMap((task) => task.order === null ? [] : [task.order]));
  // 以來源 order 辨識既有任務；新任務的 order=null，真正連續編號只由後端儲存時產生。
  const deleted = original.slice(lockedCount).filter((task) => !existingOrders.has(task.order));
  const inserted = drafts.filter((task) => task.order === null);
  const moved = drafts.slice(lockedCount).filter((task, index) => task.order !== null && task.order !== lockedCount + index + 1);
  const changedText = drafts.filter((task) => task.order !== null && original[task.order - 1] &&
    (task.task !== original[task.order - 1].task || (task.ref ?? null) !== (original[task.order - 1].ref ?? null)));
  const emptyTaskCount = drafts.filter((task) => !task.task.trim()).length;
  const dirty = JSON.stringify(drafts.map(({ order, task, ref }) => ({ order, task, ref: ref ?? null }))) !==
    JSON.stringify(original.map(({ order, task, ref }) => ({ order, task, ref: ref ?? null }))) || doneCount !== (state.done_count ?? 0);
  const canInsert = state.phase !== "done";
  const update = (index: number, values: Partial<DraftTask>) => setDrafts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ...values } : item));
  const move = (index: number, direction: -1 | 1) => setDrafts((items) => {
    // 按鈕與拖曳共用相同鎖定邊界；任何來源或目標落在 locked prefix 都直接不動。
    const target = index + direction;
    if (index < lockedCount || target < lockedCount || target >= items.length) return items;
    const next = [...items];
    [next[index], next[target]] = [next[target], next[index]];
    return next;
  });
  const dropAt = (targetIndex: number) => {
    if (!draggingId) return;
    setDrafts((items) => {
      const sourceIndex = items.findIndex((item) => item.id === draggingId);
      if (sourceIndex < lockedCount || targetIndex < lockedCount || sourceIndex < 0) return items;
      const next = [...items];
      const [dragged] = next.splice(sourceIndex, 1);
      // 先移除來源後，若原位置在插入點之前，陣列索引會左移一格，必須修正目標位置。
      const adjustedTarget = sourceIndex < targetIndex ? targetIndex - 1 : targetIndex;
      next.splice(Math.max(lockedCount, Math.min(adjustedTarget, next.length)), 0, dragged);
      return next;
    });
    setDraggingId(null); setDropIndex(null);
  };
  const insertAfter = (index: number) => setDrafts((items) => {
    const next = [...items];
    next.splice(index + 1, 0, { id: `new-${nextId.current++}`, order: null, task: "", ref: null });
    return next;
  });
  const remove = (index: number) => setDrafts((items) => index < lockedCount ? items : items.filter((_, itemIndex) => itemIndex !== index));
  const requestClose = () => dirty ? setConfirmClose(true) : onClose();
  const save = async () => {
    // UI 在送出前先做完整性檢查；後端仍會在 workspace lock 內重做非空、版本與鎖定前綴校驗。
    if (!drafts.length) return setMessage("❌ plan 必須保留至少一項任務");
    if (drafts.some((task) => !task.task.trim())) return setMessage("❌ 每項任務都必須有內容");
    setSaving(true);
    const result = await onSave(drafts.map(({ order, task, ref }) => ({ order, task: task.trim(), ref: ref?.trim() || null })), doneCount);
    setSaving(false); setMessage(result);
    if (result.startsWith("✅")) onClose();
  };
  return <>
    <Modal title="Plan 編輯器" description={`plan v${state.plan_version} · 只有停止狀態下、尚未執行的任務可排序、刪除或插入`} onClose={requestClose} fullScreen footer={<>
      <button type="button" className="secondary-button" disabled={saving} onClick={requestClose}>取消</button>
      <button type="button" className="primary-button" disabled={saving || !dirty || emptyTaskCount > 0} onClick={() => void save()}>{saving ? "儲存中…" : "💾 儲存變更"}</button>
      <span className="inline-message" role="status">{message}</span>
    </>}>
      <div className="plan-editor-layout">
        <div className="plan-editor-list">
          {drafts.map((task, index) => {
            const locked = index < lockedCount;
            return <div className={`plan-editor-task${locked ? " locked" : ""}${draggingId === task.id ? " dragging" : ""}${dropIndex === index ? " drop-before" : ""}`} key={task.id}
              onDragOver={(event) => { if (!locked && draggingId) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; const bounds = event.currentTarget.getBoundingClientRect(); setDropIndex(event.clientY > bounds.top + bounds.height / 2 ? index + 1 : index); } }}
              onDrop={(event) => { event.preventDefault(); const bounds = event.currentTarget.getBoundingClientRect(); dropAt(event.clientY > bounds.top + bounds.height / 2 ? index + 1 : index); }}>
              <header><span>{!locked && <span className="plan-drag-handle" draggable aria-hidden="true" title="按住這裡拖移任務"
                onDragStart={(event) => { setDraggingId(task.id); setDropIndex(index); event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", task.id); }}
                onDragEnd={() => { setDraggingId(null); setDropIndex(null); }}>⠿</span>}<strong>task-{index + 1}</strong>{task.order === null && <em>新增</em>}{locked && <em>已完成／目前任務，鎖定</em>}</span><span className="plan-editor-actions">
                <button type="button" className="icon-button" aria-label={`上移 task-${index + 1}`} disabled={locked || index === lockedCount} onClick={() => move(index, -1)}>↑</button>
                <button type="button" className="icon-button" aria-label={`下移 task-${index + 1}`} disabled={locked || index === drafts.length - 1} onClick={() => move(index, 1)}>↓</button>
                <button type="button" className="danger-button compact-button" disabled={locked} onClick={() => remove(index)}>刪除</button>
              </span></header>
              <label>任務內容<textarea rows={3} disabled={locked} aria-invalid={!task.task.trim()} value={task.task} onChange={(event) => update(index, { task: event.target.value })} /></label>
              <label>Ref（選填）<input disabled={locked} value={task.ref ?? ""} onChange={(event) => update(index, { ref: event.target.value })} /></label>
              {canInsert && index >= lockedCount - 1 && <button type="button" className="insert-task-button" aria-label={`插入在 task-${index + 1} 之後`} title={`插入在 task-${index + 1} 之後`} onClick={() => insertAfter(index)}>＋</button>}
            </div>;
          })}
          {draggingId && <div className={`plan-drop-tail${dropIndex === drafts.length ? " active" : ""}`}
            onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "move"; setDropIndex(drafts.length); }}
            onDrop={(event) => { event.preventDefault(); dropAt(drafts.length); }}>拖到最後</div>}
        </div>
        <aside className="plan-editor-summary">
          <h3>變更摘要</h3>
          <dl><div><dt>鎖定</dt><dd>{lockedCount} 項</dd></div><div><dt>新增</dt><dd>{inserted.length} 項</dd></div><div><dt>刪除</dt><dd>{deleted.length} 項</dd></div><div><dt>移動</dt><dd>{moved.length} 項</dd></div><div><dt>文字／Ref</dt><dd>{changedText.length} 項</dd></div></dl>
          {deleted.length > 0 && <div className="plan-editor-diff"><strong>將刪除</strong>{deleted.map((task) => <span key={task.order}>− task-{task.order}：{task.task}</span>)}</div>}
          {inserted.length > 0 && <div className="plan-editor-diff safe"><strong>將新增</strong>{inserted.map((task) => <span key={task.id}>＋ {task.task || "（尚未填寫）"}</span>)}</div>}
          {emptyTaskCount > 0 && <p className="plan-editor-validation" role="alert">尚有 {emptyTaskCount} 項任務未填寫，完成前不可儲存。</p>}
          <label>done 計數<input type="number" min={0} value={doneCount} onChange={(event) => setDoneCount(+event.target.value)} /></label>
          <p>儲存後 pending tasks 會依畫面順序重新編號；歷史紀錄、完成 commit 與目前任務不改寫。</p>
        </aside>
      </div>
    </Modal>
    {confirmClose && <ActionDialog title="放棄未儲存變更？" message="排序、插入、刪除與文字修改都會消失。" confirmLabel="放棄變更" danger onClose={() => setConfirmClose(false)} onConfirm={onClose} />}
  </>;
}
