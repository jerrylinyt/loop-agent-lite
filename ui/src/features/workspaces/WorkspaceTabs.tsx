/** Workspace 分頁列：parallel child 收在 parent 下，避免把一個 run 平鋪成 N+1 個無關 workspace。 */
import { Fragment, useMemo, useState } from "react";
import type { WorkspaceSummary } from "../../shared/api/types";
import { isFleetChildOfParent } from "./fleetViewModel";
import { parallelPhaseLabel, parallelVisualPhase } from "./parallelPhase";

function workspaceMeta(workspace: WorkspaceSummary) {
  if (workspace.workspace_kind === "fleet-parent") return parallelPhaseLabel(workspace.parallel_phase);
  if (workspace.workspace_kind === "fleet-child") return workspace.merge_stage ?? workspace.phase ?? "";
  if (workspace.phase === "exec") return `${workspace.completed ?? 0}/${workspace.plan_len ?? 0}`;
  if (workspace.phase === "plan") return `f${workspace.flag ?? 0}`;
  if (workspace.phase === "done") return "🏁";
  return "";
}

function workspaceVisualPhase(workspace: WorkspaceSummary) {
  return workspace.workspace_kind === "fleet-parent"
    ? parallelVisualPhase(workspace.parallel_phase, workspace.parallel_tracks, workspace.running)
    : workspace.phase ?? "unknown";
}

function WorkspaceTab({ workspace, selected, onSelect, child = false, disabled = false }: {
  workspace: WorkspaceSummary;
  selected: string;
  onSelect: (name: string) => void;
  child?: boolean;
  disabled?: boolean;
}) {
  const label = child ? workspace.track || workspace.name : workspace.name;
  return <button
    type="button"
    role="tab"
    aria-label={child ? `${workspace.fleet_parent} track ${label}` : undefined}
    aria-selected={workspace.name === selected}
    disabled={disabled}
    className={`workspace-tab${child ? " workspace-tab-child" : ""}${workspace.name === selected ? " active" : ""}`}
    onClick={() => onSelect(workspace.name)}
  >
    <span className={`status-dot phase-${workspaceVisualPhase(workspace)}`} aria-hidden="true" />
    {workspace.running && <span aria-label="執行中">▶</span>}
    <span>{child ? `↳ ${label}` : label}</span>
    {workspaceMeta(workspace) && <span className="tab-meta">{workspaceMeta(workspace)}</span>}
  </button>;
}

export default function WorkspaceTabs({
  workspaces,
  selected,
  disabled = false,
  onSelect
}: {
  workspaces: WorkspaceSummary[];
  selected: string;
  disabled?: boolean;
  onSelect: (name: string) => void;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const { roots, childrenByParent } = useMemo(() => {
    const byName = new Map(workspaces.map((workspace) => [workspace.name, workspace]));
    const grouped = new Map<string, WorkspaceSummary[]>();
    for (const workspace of workspaces) {
      const parentName = workspace.fleet_parent;
      const parent = parentName ? byName.get(parentName) : undefined;
      if (!isFleetChildOfParent(workspace, parent)) continue;
      const children = grouped.get(parentName!) ?? [];
      children.push(workspace);
      grouped.set(parentName!, children);
    }
    return {
      roots: workspaces.filter((workspace) => !isFleetChildOfParent(
        workspace,
        workspace.fleet_parent ? byName.get(workspace.fleet_parent) : undefined
      )),
      childrenByParent: grouped,
    };
  }, [workspaces]);
  const toggle = (name: string) => setCollapsed((current) => {
    const next = new Set(current);
    if (next.has(name)) next.delete(name); else next.add(name);
    return next;
  });

  return (
    <div className="workspace-tabs" role="tablist" aria-label="Workspaces">
      {roots.map((workspace) => {
        const children = childrenByParent.get(workspace.name) ?? [];
        const isCollapsed = collapsed.has(workspace.name);
        return <Fragment key={workspace.name}>
          <WorkspaceTab workspace={workspace} selected={selected} disabled={disabled} onSelect={onSelect} />
          {!!children.length && <button type="button" className="workspace-tab-toggle"
            aria-label={`${isCollapsed ? "展開" : "收合"} ${workspace.name} tracks`}
            aria-expanded={!isCollapsed} disabled={disabled} onClick={() => toggle(workspace.name)}>
            {isCollapsed ? `▸ ${children.length}` : "▾"}
          </button>}
          {!isCollapsed && children.map((child) => <WorkspaceTab key={child.name} workspace={child}
            selected={selected} disabled={disabled} onSelect={onSelect} child />)}
        </Fragment>;
      })}
    </div>
  );
}
