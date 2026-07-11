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
  const nextId = useRef(1);
  const existingOrders = new Set(drafts.flatMap((task) => task.order === null ? [] : [task.order]));
  const deleted = original.slice(lockedCount).filter((task) => !existingOrders.has(task.order));
  const inserted = drafts.filter((task) => task.order === null);
  const moved = drafts.slice(lockedCount).filter((task, index) => task.order !== null && task.order !== lockedCount + index + 1);
  const changedText = drafts.filter((task) => task.order !== null && original[task.order - 1] &&
    (task.task !== original[task.order - 1].task || (task.ref ?? null) !== (original[task.order - 1].ref ?? null)));
  const dirty = JSON.stringify(drafts.map(({ order, task, ref }) => ({ order, task, ref: ref ?? null }))) !==
    JSON.stringify(original.map(({ order, task, ref }) => ({ order, task, ref: ref ?? null }))) || doneCount !== (state.done_count ?? 0);
  const canInsert = state.phase !== "done";
  const update = (index: number, values: Partial<DraftTask>) => setDrafts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ...values } : item));
  const move = (index: number, direction: -1 | 1) => setDrafts((items) => {
    const target = index + direction;
    if (index < lockedCount || target < lockedCount || target >= items.length) return items;
    const next = [...items];
    [next[index], next[target]] = [next[target], next[index]];
    return next;
  });
  const insertAfter = (index: number) => setDrafts((items) => {
    const next = [...items];
    next.splice(index + 1, 0, { id: `new-${nextId.current++}`, order: null, task: "", ref: null });
    return next;
  });
  const remove = (index: number) => setDrafts((items) => index < lockedCount ? items : items.filter((_, itemIndex) => itemIndex !== index));
  const requestClose = () => dirty ? setConfirmClose(true) : onClose();
  const save = async () => {
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
      <button type="button" className="primary-button" disabled={saving || !dirty} onClick={() => void save()}>{saving ? "儲存中…" : "💾 儲存變更"}</button>
      <span className="inline-message" role="status">{message}</span>
    </>}>
      <div className="plan-editor-layout">
        <div className="plan-editor-list">
          {drafts.map((task, index) => {
            const locked = index < lockedCount;
            return <div className={`plan-editor-task${locked ? " locked" : ""}`} key={task.id}>
              <header><span><strong>task-{index + 1}</strong>{task.order === null && <em>新增</em>}{locked && <em>已完成／目前任務，鎖定</em>}</span><span className="plan-editor-actions">
                <button type="button" className="icon-button" aria-label={`上移 task-${index + 1}`} disabled={locked || index === lockedCount} onClick={() => move(index, -1)}>↑</button>
                <button type="button" className="icon-button" aria-label={`下移 task-${index + 1}`} disabled={locked || index === drafts.length - 1} onClick={() => move(index, 1)}>↓</button>
                <button type="button" className="danger-button compact-button" disabled={locked} onClick={() => remove(index)}>刪除</button>
              </span></header>
              <label>任務內容<textarea rows={3} disabled={locked} value={task.task} onChange={(event) => update(index, { task: event.target.value })} /></label>
              <label>Ref（選填）<input disabled={locked} value={task.ref ?? ""} onChange={(event) => update(index, { ref: event.target.value })} /></label>
              {canInsert && index >= lockedCount - 1 && <button type="button" className="insert-task-button" onClick={() => insertAfter(index)}>＋ 插入在此任務之後</button>}
            </div>;
          })}
        </div>
        <aside className="plan-editor-summary">
          <h3>變更摘要</h3>
          <dl><div><dt>鎖定</dt><dd>{lockedCount} 項</dd></div><div><dt>新增</dt><dd>{inserted.length} 項</dd></div><div><dt>刪除</dt><dd>{deleted.length} 項</dd></div><div><dt>移動</dt><dd>{moved.length} 項</dd></div><div><dt>文字／Ref</dt><dd>{changedText.length} 項</dd></div></dl>
          {deleted.length > 0 && <div className="plan-editor-diff"><strong>將刪除</strong>{deleted.map((task) => <span key={task.order}>− task-{task.order}：{task.task}</span>)}</div>}
          {inserted.length > 0 && <div className="plan-editor-diff safe"><strong>將新增</strong>{inserted.map((task) => <span key={task.id}>＋ {task.task || "（尚未填寫）"}</span>)}</div>}
          <label>done 計數<input type="number" min={0} value={doneCount} onChange={(event) => setDoneCount(+event.target.value)} /></label>
          <p>儲存後 pending tasks 會依畫面順序重新編號；歷史紀錄、完成 commit 與目前任務不改寫。</p>
        </aside>
      </div>
    </Modal>
    {confirmClose && <ActionDialog title="放棄未儲存變更？" message="排序、插入、刪除與文字修改都會消失。" confirmLabel="放棄變更" danger onClose={() => setConfirmClose(false)} onConfirm={onClose} />}
  </>;
}
