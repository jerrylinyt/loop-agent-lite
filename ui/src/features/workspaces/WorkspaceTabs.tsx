import type { WorkspaceSummary } from "../../shared/api/types";

function workspaceMeta(workspace: WorkspaceSummary) {
  if (workspace.phase === "exec") return `${workspace.completed ?? 0}/${workspace.plan_len ?? 0}`;
  if (workspace.phase === "plan") return `f${workspace.flag ?? 0}`;
  if (workspace.phase === "done") return "🏁";
  return "";
}

export default function WorkspaceTabs({
  workspaces,
  selected,
  onSelect
}: {
  workspaces: WorkspaceSummary[];
  selected: string;
  onSelect: (name: string) => void;
}) {
  return (
    <div className="workspace-tabs" role="tablist" aria-label="Workspaces">
      {workspaces.map((workspace) => (
        <button
          key={workspace.name}
          type="button"
          role="tab"
          aria-selected={workspace.name === selected}
          className={`workspace-tab${workspace.name === selected ? " active" : ""}`}
          onClick={() => onSelect(workspace.name)}
        >
          <span className={`status-dot phase-${workspace.phase ?? "unknown"}`} aria-hidden="true" />
          {workspace.running && <span aria-label="執行中">▶</span>}
          <span>{workspace.name}</span>
          {workspaceMeta(workspace) && <span className="tab-meta">{workspaceMeta(workspace)}</span>}
        </button>
      ))}
    </div>
  );
}
