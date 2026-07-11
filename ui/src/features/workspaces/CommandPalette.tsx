import { useMemo, useState } from "react";
import type { WorkspaceSummary } from "../../shared/api/types";
import Modal from "../../shared/components/Modal";

export interface PaletteCommand { id: string; label: string; hint: string; run: () => void }
export default function CommandPalette({ workspaces, commands, onSelectWorkspace, onClose }: {
  workspaces: WorkspaceSummary[]; commands: PaletteCommand[]; onSelectWorkspace: (name: string) => void; onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const entries = useMemo(() => {
    const common = commands.map((command) => ({ ...command, group: "操作" }));
    const workspaceEntries = workspaces.map((workspace) => ({ id: `ws:${workspace.name}`, label: workspace.name, hint: `${workspace.phase ?? "—"} · ${workspace.running ? "執行中" : "已停止"}`, group: "Workspace", run: () => onSelectWorkspace(workspace.name) }));
    const needle = query.trim().toLowerCase();
    return [...common, ...workspaceEntries].filter((entry) => !needle || `${entry.label} ${entry.hint}`.toLowerCase().includes(needle)).slice(0, 30);
  }, [commands, onSelectWorkspace, query, workspaces]);
  const execute = (run: () => void) => { onClose(); run(); };
  return <Modal title="快捷指令" description="搜尋 workspace 或執行 Dashboard 操作 · ⌘K / Ctrl+K" onClose={onClose} wide compact>
    <input data-autofocus className="command-palette-search" type="search" aria-label="搜尋快捷指令" placeholder="輸入 workspace 或操作名稱…" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => {
      if (event.key === "Enter" && entries[0]) execute(entries[0].run);
    }} />
    <div className="command-palette-list" role="listbox" aria-label="快捷指令結果">
      {entries.map((entry) => <button type="button" role="option" aria-selected="false" key={entry.id} onClick={() => execute(entry.run)}><span><small>{entry.group}</small><strong>{entry.label}</strong></span><em>{entry.hint}</em></button>)}
      {!entries.length && <div className="empty-inline">找不到符合的指令或 workspace</div>}
    </div>
  </Modal>;
}
