/** 全域鍵盤導覽：管理 ⌘/Ctrl+K 與 ⌘/Ctrl+G、0～5 chord 的計時與清理。 */
import { useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { WorkspaceSummary } from "../shared/api/types";

export default function useWorkspaceNavigation({ workspaces, selectWorkspace, setOverviewOpen, setPaletteOpen }: {
  workspaces: WorkspaceSummary[];
  selectWorkspace: (name: string) => void;
  setOverviewOpen: Dispatch<SetStateAction<boolean>>;
  setPaletteOpen: Dispatch<SetStateAction<boolean>>;
}): boolean {
  const [armed, setArmed] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    const disarm = () => {
      setArmed(false);
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = null;
    };
    const listener = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen(true);
        return;
      }
      const target = event.target as HTMLElement | null;
      const editing = !!target?.closest("input, textarea, select, [contenteditable='true']");
      const modalOpen = !!document.querySelector("[role='dialog']");
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "g") {
        if (editing || modalOpen) return;
        event.preventDefault();
        setArmed(true);
        if (timer.current !== null) window.clearTimeout(timer.current);
        timer.current = window.setTimeout(disarm, 1500);
        return;
      }
      if (!armed || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey || !/^[0-5]$/.test(event.key)) return;
      event.preventDefault();
      disarm();
      if (event.key === "0") {
        setOverviewOpen(true);
        localStorage.setItem("fleet-overview", "1");
        return;
      }
      const next = workspaces[Number(event.key) - 1];
      // 超出現有 workspace 數量時維持原畫面，不把空位置當成錯誤。
      if (!next) return;
      selectWorkspace(next.name);
      setOverviewOpen(false);
      localStorage.setItem("fleet-overview", "0");
    };
    document.addEventListener("keydown", listener);
    return () => document.removeEventListener("keydown", listener);
  }, [armed, selectWorkspace, setOverviewOpen, setPaletteOpen, workspaces]);

  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);

  return armed;
}
