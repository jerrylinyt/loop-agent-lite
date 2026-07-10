import { useEffect, useRef, useState } from "react";
import type { WorkspaceState } from "../../shared/api/types";

type PulseTarget = "phase" | "task" | "flag" | "done" | "health";

export default function useStatusPulse(state: WorkspaceState | null) {
  const previous = useRef<WorkspaceState | null>(null);
  const timer = useRef<number | null>(null);
  const [targets, setTargets] = useState<Set<PulseTarget>>(new Set());

  useEffect(() => {
    const before = previous.current;
    previous.current = state;
    if (!before || !state) return;

    const changed = new Set<PulseTarget>();
    if (before.phase !== state.phase) changed.add("phase");
    if (before.current_order !== state.current_order || before.completed?.length !== state.completed?.length) changed.add("task");
    if (before.flag !== state.flag) changed.add("flag");
    if (before.done_count !== state.done_count) changed.add("done");
    if (before.red_streak !== state.red_streak || before.stall_rounds !== state.stall_rounds) changed.add("health");
    if (!changed.size) return;

    setTargets(changed);
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      setTargets(new Set());
      timer.current = null;
    }, 1800);
  }, [state]);

  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);

  return targets;
}
