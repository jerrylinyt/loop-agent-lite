import { useEffect, useState } from "react";
import { getJson } from "../../shared/api/client";
import Modal from "../../shared/components/Modal";

interface GoalResponse {
  content?: string;
  path?: string;
  goal_changed?: boolean;
  previous_content?: string;
  previous_hash?: string;
  diff?: string;
  diff_error?: string;
  error?: string;
}

function DiffContent({ value }: { value: string }) {
  return <pre className="report-content goal-diff">{value.split("\n").map((line, index) => {
    const kind = line.startsWith("+++") || line.startsWith("---") ? "header"
      : line.startsWith("@@") ? "hunk"
        : line.startsWith("+") ? "add"
          : line.startsWith("-") ? "remove" : "context";
    return <span key={`${index}-${line}`} className={`goal-diff-line ${kind}`}>{line}{"\n"}</span>;
  })}</pre>;
}

export default function GoalModal({ workspace, onClose }: { workspace: string; onClose: () => void }) {
  const [goal, setGoal] = useState<GoalResponse | null>(null);
  const [view, setView] = useState<"current" | "diff">("current");

  useEffect(() => {
    void (async () => {
      const response = await getJson<GoalResponse>(`/api/goal?ws=${encodeURIComponent(workspace)}`);
      const next: GoalResponse = response ?? { error: "讀取 goal 失敗" };
      setGoal(next);
      setView(next.goal_changed && (next.diff !== undefined || next.diff_error) ? "diff" : "current");
    })();
  }, [workspace]);

  return (
    <Modal title="Goal" description={goal?.path ?? "loop 正在為這個 goal 工作（人類真相，唯讀）"} onClose={onClose} wide>
      {!goal ? <div className="loading-state">載入 goal…</div>
        : goal.error ? <div className="loading-state error">{goal.error}</div>
        : <>
            {goal.goal_changed && <div className="goal-warning">⚠ goal 已變更，現有計畫是舊 goal 收斂的；請檢視差異後回規劃期重新收斂</div>}
            {goal.goal_changed && <div className="segmented-tabs goal-view-tabs" role="tablist" aria-label="Goal 檢視">
              <button type="button" role="tab" aria-selected={view === "current"} className={view === "current" ? "active" : ""} onClick={() => setView("current")}>目前 goal</button>
              <button type="button" role="tab" aria-selected={view === "diff"} className={view === "diff" ? "active" : ""} onClick={() => setView("diff")}>變更差異</button>
            </div>}
            {view === "diff" && goal.goal_changed
              ? goal.diff_error ? <div className="loading-state error">{goal.diff_error}</div>
                : goal.diff ? <DiffContent value={goal.diff} />
                  : <div className="loading-state">沒有文字差異</div>
              : <pre className="report-content">{goal.content}</pre>}
          </>}
    </Modal>
  );
}
