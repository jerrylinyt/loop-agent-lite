/** 真實瀏覽器端到端流程：使用隔離 fixture 驗證啟動、SSE、操作防線與 Plan 編輯。 */
import { expect, test, type Page, type Route } from "@playwright/test";
import type { WorkspaceSummary } from "../src/shared/api/types";
import { fleetChildNamesForParent, overviewWorkspaceProjection, workspaceNeedsAttention } from "../src/features/workspaces/fleetViewModel";
import { issueIsUnread, issueMutationsLocked } from "../src/features/workspaces/issueViewModel";
import { parallelMutationBlocked, parallelVisualPhase, trackStatusLabel } from "../src/features/workspaces/parallelPhase";

const PLAN = JSON.stringify([
  { order: 1, task: "建立 E2E 第一項功能", ref: "README.md", track: "main" },
  { order: 2, task: "驗證 E2E 第二項功能", track: "main" }
], null, 2);

async function acceptConfirmation(page: Page, action: () => Promise<void>) {
  // 所有破壞性操作應先開啟共用確認視窗；helper 同時驗證這條 UI 契約。
  await action();
  const dialog = page.getByRole("dialog", { name: "請確認" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: /繼續|清空/ }).click();
}

async function expectCollapsedAgentConsoleHeight(page: Page, width: 700 | 800) {
  const agentConsole = page.getByRole("region", { name: "Agent 執行輸出", exact: true });
  await agentConsole.getByRole("button", { name: "收合Agent 執行輸出" }).click();
  const collapsed = page.getByRole("region", { name: "Agent 執行輸出（已收合）" });
  const box = await collapsed.boundingBox();
  expect(box, `${width}px collapsed Agent console bounding box`).not.toBeNull();
  expect(box?.height ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(44);
  expect(box?.width ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(width);
  await collapsed.getByRole("button", { name: "展開Agent 執行輸出" }).click();
}

test("parallel view model 將 failed/repairing 視為 attention 並正確標示 stopped", () => {
  const repairingParent = {
    name: "repairing-parent",
    workspace_kind: "fleet-parent",
    fleet_run_id: "run-current",
    phase: "exec",
    parallel_phase: "exec",
    running: false,
    parallel_tracks: [{
      name: "alpha", safe_name: "alpha", status: "repairing", child_workspace: "repairing-parent--alpha",
      branch_ref: "refs/heads/loop/run/alpha", worktree: "/tmp/alpha", restart_count: 1,
      integration_validate_failures: 1
    }]
  } satisfies WorkspaceSummary;
  expect(workspaceNeedsAttention(repairingParent)).toBe(true);
  expect(parallelVisualPhase(repairingParent.parallel_phase, repairingParent.parallel_tracks)).toBe("failed");
  expect(trackStatusLabel("stopped")).toBe("已停止");
  const healthyChild = { name: "repairing-parent--alpha", workspace_kind: "fleet-child", fleet_parent: "repairing-parent", fleet_run_id: "run-current", track: "alpha" } satisfies WorkspaceSummary;
  const orphanChild = { name: "orphan--alpha", workspace_kind: "fleet-child", fleet_parent: "missing-parent" } satisfies WorkspaceSummary;
  const staleRunChild = { name: "repairing-parent--old", workspace_kind: "fleet-child", fleet_parent: "repairing-parent", fleet_run_id: "run-old" } satisfies WorkspaceSummary;
  const unregisteredSameRunChild = { name: "repairing-parent--beta", workspace_kind: "fleet-child", fleet_parent: "repairing-parent", fleet_run_id: "run-current", track: "beta", error: "unregistered child" } satisfies WorkspaceSummary;
  const standaloneImpostor = { name: "not-a-parent", workspace_kind: "standalone" } satisfies WorkspaceSummary;
  const impostorChild = { name: "not-a-parent--child", workspace_kind: "fleet-child", fleet_parent: "not-a-parent", fleet_run_id: "run-current" } satisfies WorkspaceSummary;
  const fleet = [repairingParent, healthyChild, orphanChild, staleRunChild, unregisteredSameRunChild, standaloneImpostor, impostorChild];
  expect(overviewWorkspaceProjection(fleet).map((workspace) => workspace.name))
    .toEqual(["repairing-parent", "orphan--alpha", "repairing-parent--old", "repairing-parent--beta", "not-a-parent", "not-a-parent--child"]);
  expect(fleetChildNamesForParent(repairingParent, fleet)).toEqual(["repairing-parent--alpha"]);
  expect(parallelMutationBlocked("fleet truth 無法讀取", true)).toBe(true);
  expect(parallelMutationBlocked("", false)).toBe(true);
  expect(parallelMutationBlocked("", true)).toBe(false);
  expect(issueIsUnread({ round: 0, text: "validator failed", resolved: true, synthetic: true }, -1)).toBe(false);
  expect(issueMutationsLocked([{ round: 0, text: "validator failed", synthetic: true }], false)).toBe(true);
});

test("legacy v1 workspace 即使 state 讀取失敗也只提供永久刪除", async ({ page }) => {
  const legacyError = "state.json state_schema_version 必須是 2；舊 workspace 不支援續跑，請刪除後重新開始";
  const legacyWorkspace = {
    name: "legacy-v1",
    workspace_generation: "a".repeat(32),
    legacy_delete_only: true,
    error: legacyError,
    phase: null,
    running: false,
  } satisfies WorkspaceSummary;
  let deleted = false;
  let deleteCalls = 0;
  let deleteBody: unknown;
  let releaseDelete: (() => void) | undefined;
  const deleteGate = new Promise<void>((resolve) => { releaseDelete = resolve; });

  await page.route("**/api/workspaces", async (route) => {
    await route.fulfill({ json: deleted ? [] : [legacyWorkspace] });
  });
  await page.route("**/api/state?ws=legacy-v1", async (route) => {
    await route.fulfill({ json: { error: legacyError } });
  });
  await page.route("**/api/events?**", async (route) => {
    const workspaces = deleted ? [] : [legacyWorkspace];
    await route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" },
      body: `event: workspaces\ndata: ${JSON.stringify(workspaces)}\n\nevent: state\ndata: ${JSON.stringify({ error: legacyError })}\n\n`,
    });
  });
  await page.route("**/api/delete-workspace", async (route) => {
    deleteCalls += 1;
    deleteBody = route.request().postDataJSON();
    await deleteGate;
    deleted = true;
    await route.fulfill({ json: { ok: true, name: "legacy-v1", deleted: true } });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "legacy-v1" })).toBeVisible();
  await expect(page.getByText("舊版（僅可刪除）")).toBeVisible();
  await expect(page.getByRole("alert")).toContainText(legacyError);
  await expect(page.getByRole("alert")).toContainText("不能運行、重跑、編輯、設定或作為範本");
  const legacyPane = page.locator(".workspace-pane");
  await expect(legacyPane.getByRole("button")).toHaveCount(1);
  await expect(legacyPane.getByRole("button", { name: "🗑 永久刪除" })).toBeVisible();
  await expect(legacyPane.getByRole("button", { name: /運行|停止|編輯|設定|範本/ })).toHaveCount(0);

  await legacyPane.getByRole("button", { name: "🗑 永久刪除" }).click();
  const dialog = page.getByRole("dialog", { name: "請確認" });
  await expect(dialog).toContainText("舊版格式不支援運行、重跑或編輯");
  await expect(dialog).toContainText("target repo、commit 與程式碼");
  await dialog.getByRole("button", { name: "永久刪除" }).click();
  await expect(legacyPane.getByRole("button", { name: "刪除中…" })).toBeDisabled();
  await page.waitForTimeout(100);
  expect(deleteCalls).toBe(1);
  expect(deleteBody).toEqual({ name: "legacy-v1", workspace_generation: "a".repeat(32) });
  releaseDelete?.();
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toBeVisible();
});

test("同名 standalone generation 更新會重建 SSE identity 並關閉舊 modal", async ({ page }) => {
  let generation = "a".repeat(32);
  let marker = "old-generation";
  let eventConnections = 0;
  const workspace = () => ({
    name: "same-name", workspace_generation: generation, workspace_kind: "standalone" as const,
    fleet_run_id: null, phase: "plan" as const, running: false, round: 0, flag: 0,
    completed: 0, plan_len: 1, done_count: 0, issues: 0, unread_issues: 0,
  });
  const state = () => ({
    state_schema_version: 2, workspace_generation: generation,
    workspace_kind: "standalone" as const, fleet_run_id: null,
    phase: "plan" as const, round: 0, flag: 0, done_count: 0,
    red_streak: 0, stall_rounds: 0, plan_version: 1, current_order: 1,
    plan: [{ order: 1, task: marker, track: "main" }], completed: [], issues: [],
    config: { repo: "/tmp/replacement", agent_cmd: "true", validate_cmd: "true" },
  });
  await page.route("**/api/workspaces", async (route) => route.fulfill({ json: [workspace()] }));
  await page.route("**/api/state?ws=same-name", async (route) => route.fulfill({ json: state() }));
  await page.route("**/api/events?**", async (route) => {
    eventConnections += 1;
    await route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" },
      body: `retry: 50\nevent: workspaces\ndata: ${JSON.stringify([workspace()])}\n\nevent: state\ndata: ${JSON.stringify(state())}\n\n`,
    });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "same-name" })).toBeVisible();
  await expect(page.getByRole("button", { name: marker })).toBeVisible();
  await page.getByRole("button", { name: "⚙ 設定" }).click();
  const oldModal = page.getByRole("dialog", { name: "Workspace 設定" });
  await expect(oldModal).toBeVisible();
  const previousConnections = eventConnections;
  generation = "b".repeat(32);
  marker = "replacement-generation";
  await expect.poll(() => eventConnections, { timeout: 5_000 }).toBeGreaterThan(previousConnections);
  await expect(oldModal).toBeHidden({ timeout: 5_000 });
  await expect(page.getByRole("button", { name: marker })).toBeVisible();
});

test("Fleet 批次確認凍結同名 workspace identity snapshot", async ({ page }) => {
  let generation = "a".repeat(32);
  let activity = "old-generation-activity";
  let eventConnections = 0;
  let deleteBody: Record<string, unknown> | null = null;
  const workspace = () => ({
    name: "bulk-same-name", workspace_generation: generation,
    workspace_kind: "standalone" as const, fleet_run_id: null,
    phase: "exec" as const, running: false, round: 1, flag: 0,
    completed: 0, plan_len: 1, done_count: 0, issues: 0, unread_issues: 0,
    current_order: 1, current_task: activity,
  });
  const state = () => ({
    state_schema_version: 2, workspace_generation: generation,
    workspace_kind: "standalone" as const, fleet_run_id: null,
    phase: "exec" as const, round: 1, flag: 0, done_count: 0,
    red_streak: 0, stall_rounds: 0, plan_version: 1, current_order: 1,
    plan: [{ order: 1, task: activity, track: "main" }], completed: [], issues: [],
    config: { repo: "/tmp/bulk-replacement", agent_cmd: "true", validate_cmd: "true" },
  });
  await page.route("**/api/workspaces", async (route) => route.fulfill({ json: [workspace()] }));
  await page.route("**/api/state?ws=bulk-same-name", async (route) => route.fulfill({ json: state() }));
  await page.route("**/api/events?**", async (route) => {
    eventConnections += 1;
    await route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" },
      body: `retry: 50\nevent: workspaces\ndata: ${JSON.stringify([workspace()])}\n\nevent: state\ndata: ${JSON.stringify(state())}\n\n`,
    });
  });
  await page.route("**/api/delete-workspace", async (route) => {
    deleteBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({ status: 409, json: { error: "workspace generation 已變更" } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "📺 總覽" }).click();
  const overview = page.getByRole("main", { name: "工作區總覽" });
  await expect(overview.getByText("old-generation-activity")).toBeVisible();
  await overview.getByRole("button", { name: "☑ 批次操作" }).click();
  await overview.getByLabel("批次選擇 workspace").selectOption("bulk-same-name");
  const previousConnections = eventConnections;
  generation = "b".repeat(32);
  activity = "replacement-generation-activity";
  await expect.poll(() => eventConnections, { timeout: 5_000 }).toBeGreaterThan(previousConnections);
  await expect(overview.getByText("replacement-generation-activity"), "replacement 必須已進入最新 Fleet projection").toBeVisible();
  await overview.getByRole("button", { name: "刪除", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "確認批次操作" });
  await expect(confirmation).toContainText("bulk-same-name");
  await confirmation.getByRole("button", { name: "執行 1 個" }).click();
  await expect.poll(() => deleteBody).not.toBeNull();
  expect(deleteBody).toEqual({
    name: "bulk-same-name",
    workspace_generation: "a".repeat(32),
  });
  await expect(overview.locator(".bulk-toolbar [role='status']")).toContainText("1 個失敗");
});

test("較新的 SSE workspaces 不會被延遲 REST refresh 覆寫", async ({ page }) => {
  const generation = "c".repeat(32);
  let workspaceRequests = 0;
  let staleRestResolved = false;
  let releaseStaleRest!: () => void;
  let releaseWorkspaceSse!: () => void;
  const staleRestGate = new Promise<void>((resolve) => { releaseStaleRest = resolve; });
  const workspaceSseGate = new Promise<void>((resolve) => { releaseWorkspaceSse = resolve; });
  const workspace = {
    name: "rest-sse-workspace", workspace_generation: generation,
    workspace_kind: "standalone" as const, fleet_run_id: null,
    phase: "plan" as const, running: false, round: 0, flag: 0,
    completed: 0, plan_len: 1, done_count: 0, issues: 0, unread_issues: 0,
  };
  const state = {
    state_schema_version: 2, workspace_generation: generation,
    workspace_kind: "standalone" as const, fleet_run_id: null,
    phase: "plan" as const, round: 0, flag: 0, done_count: 0,
    red_streak: 0, stall_rounds: 0, plan_version: 1, current_order: 1,
    plan: [{ order: 1, task: "SSE workspace truth", track: "main" }], completed: [], issues: [],
    config: { repo: "/tmp/rest-sse", agent_cmd: "true", validate_cmd: "true" },
  };
  await page.route("**/api/workspaces", async (route) => {
    workspaceRequests += 1;
    if (workspaceRequests === 1) return route.fulfill({ json: [] });
    await staleRestGate;
    staleRestResolved = true;
    await route.fulfill({ json: [] });
  });
  let selectedEventConnections = 0;
  await page.route("**/api/events?**", async (route) => {
    if (!route.request().url().includes("ws=rest-sse-workspace")) {
      return route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" },
        body: "retry: 10000\n\n",
      });
    }
    selectedEventConnections += 1;
    if (selectedEventConnections > 1) {
      return route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" },
        body: "retry: 10000\n\n",
      });
    }
    await workspaceSseGate;
    await route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" },
      body: `retry: 10000\nevent: workspaces\ndata: ${JSON.stringify([workspace])}\n\nevent: state\ndata: ${JSON.stringify(state)}\n\n`,
    });
  });
  await page.route("**/api/launch", async (route) => route.fulfill({
    json: { ok: true, name: workspace.name, pid: 54321, starting: false }
  }));

  await page.goto("/");
  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  await launcher.getByLabel("Workspace 名稱 留空＝repo 目錄名").fill(workspace.name);
  await launcher.getByRole("button", { name: "▶ 啟動" }).click();
  await expect.poll(() => workspaceRequests, { timeout: 5_000 }).toBe(2);
  releaseWorkspaceSse();
  await expect(page.getByRole("tab", { name: new RegExp(workspace.name) })).toBeVisible();
  releaseStaleRest();
  await expect.poll(() => staleRestResolved).toBe(true);
  await page.waitForTimeout(150);
  await expect(page.getByRole("tab", { name: new RegExp(workspace.name) })).toBeVisible();
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toHaveCount(0);
});

test("較新的 SSE state 不會被延遲 REST refresh 覆寫", async ({ page }) => {
  const generation = "d".repeat(32);
  let activity = "baseline-state-truth";
  let stateRequests = 0;
  let staleRestResolved = false;
  let releaseStaleState!: () => void;
  let releaseNewStateSse!: () => void;
  const staleStateGate = new Promise<void>((resolve) => { releaseStaleState = resolve; });
  const newStateSseGate = new Promise<void>((resolve) => { releaseNewStateSse = resolve; });
  const workspace = () => ({
    name: "rest-sse-state", workspace_generation: generation,
    workspace_kind: "standalone" as const, fleet_run_id: null,
    phase: "plan" as const, running: false, round: 1, flag: 0,
    completed: 0, plan_len: 1, done_count: 0, issues: 0, unread_issues: 0,
  });
  const state = (task = activity) => ({
    state_schema_version: 2, workspace_generation: generation,
    workspace_kind: "standalone" as const, fleet_run_id: null,
    phase: "plan" as const, round: 1, flag: 0, done_count: 0,
    red_streak: 0, stall_rounds: 0, plan_version: 1, current_order: 1,
    plan: [{ order: 1, task, track: "main" }], completed: [], issues: [],
    config: { repo: "/tmp/rest-sse-state", agent_cmd: "true", validate_cmd: "true" },
  });
  await page.route("**/api/workspaces", async (route) => route.fulfill({ json: [workspace()] }));
  await page.route("**/api/state?ws=rest-sse-state", async (route) => {
    stateRequests += 1;
    const staleState = state("stale-delayed-rest-state");
    await staleStateGate;
    staleRestResolved = true;
    await route.fulfill({ json: staleState });
  });
  let eventConnections = 0;
  await page.route("**/api/events?**", async (route) => {
    eventConnections += 1;
    if (eventConnections === 2) await newStateSseGate;
    if (eventConnections > 2) {
      return route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" },
        body: "retry: 10000\n\n",
      });
    }
    await route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" },
      body: `retry: 50\nevent: workspaces\ndata: ${JSON.stringify([workspace()])}\n\nevent: state\ndata: ${JSON.stringify(state())}\n\n`,
    });
  });
  await page.route("**/api/run", async (route) => route.fulfill({ json: { ok: true } }));

  await page.goto("/");
  await expect(page.getByRole("button", { name: "baseline-state-truth" })).toBeVisible();
  await page.getByRole("button", { name: "▶ 運行" }).click();
  await expect.poll(() => stateRequests, { timeout: 5_000 }).toBe(1);
  activity = "newer-sse-state-truth";
  releaseNewStateSse();
  await expect(page.getByRole("button", { name: "newer-sse-state-truth" })).toBeVisible();
  releaseStaleState();
  await expect.poll(() => staleRestResolved).toBe(true);
  await page.waitForTimeout(150);
  await expect(page.getByRole("button", { name: "newer-sse-state-truth" })).toBeVisible();
  await expect(page.getByRole("button", { name: "stale-delayed-rest-state" })).toHaveCount(0);
});

test("Workspace 設定的既有 Agent 被刪除後必須明確重選", async ({ page }) => {
  const generation = "e".repeat(32);
  const workspace = {
    name: "missing-agent-config", workspace_generation: generation,
    workspace_kind: "standalone" as const, fleet_run_id: null,
    phase: "plan" as const, running: false, round: 0, flag: 0,
    completed: 0, plan_len: 1, done_count: 0, issues: 0, unread_issues: 0,
  };
  const state = {
    state_schema_version: 2, workspace_generation: generation,
    workspace_kind: "standalone" as const, fleet_run_id: null,
    phase: "plan" as const, round: 0, flag: 0, done_count: 0,
    red_streak: 0, stall_rounds: 0, plan_version: 1, current_order: 1,
    plan: [{ order: 1, task: "missing agent config", track: "main" }], completed: [], issues: [],
    config: { repo: "/tmp/missing-agent", agent_cmd: "removed-agent --run", validate_cmd: "true" },
  };
  await page.route("**/api/workspaces", async (route) => route.fulfill({ json: [workspace] }));
  await page.route("**/api/events?**", async (route) => route.fulfill({
    status: 200,
    headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" },
    body: `retry: 50\nevent: workspaces\ndata: ${JSON.stringify([workspace])}\n\nevent: state\ndata: ${JSON.stringify(state)}\n\n`,
  }));
  await page.route("**/api/config", async (route) => route.fulfill({ json: {
    agent_cmds: [{ label: "available agent", cmd: "available-agent --run" }],
    validate_cmds: [{ label: "green", cmd: "true" }], repos: ["/tmp/missing-agent"],
    defaults: { agent_cmd: "available-agent --run", validate_cmd: "true" },
  } }));
  await page.goto("/");
  await page.getByRole("button", { name: "⚙ 設定" }).click();
  const config = page.getByRole("dialog", { name: "Workspace 設定" });
  const agent = config.getByRole("combobox", { name: "Agent 命令" });
  await expect(agent).toHaveValue("");
  await expect(agent.getByRole("option", { name: "原 Agent CLI 已移除，請重新選擇" })).toBeDisabled();
  await expect(config.getByRole("button", { name: "儲存設定" })).toBeDisabled();
  await agent.selectOption("0");
  await expect(config.getByRole("button", { name: "儲存設定" })).toBeEnabled();
});

test("drain 與 cancel-drain 以 frozen PID 拒絕同 generation 新 session", async ({ page }) => {
  const generation = "f".repeat(32);
  let loopPid = 111;
  let draining = false;
  let activity = "session-111";
  let eventConnections = 0;
  let drainBody: Record<string, unknown> | null = null;
  let cancelBody: Record<string, unknown> | null = null;
  let releaseDrain!: () => void;
  let releaseCancel!: () => void;
  const drainGate = new Promise<void>((resolve) => { releaseDrain = resolve; });
  const cancelGate = new Promise<void>((resolve) => { releaseCancel = resolve; });
  const workspace = () => ({
    name: "pid-session", workspace_generation: generation,
    workspace_kind: "standalone" as const, fleet_run_id: null,
    phase: "plan" as const, running: true, draining, drain_claimed: false,
    loop_pid: loopPid, round: 1, flag: 0, completed: 0, plan_len: 1,
    done_count: 0, issues: 0, unread_issues: 0,
  });
  const state = () => ({
    state_schema_version: 2, workspace_generation: generation,
    workspace_kind: "standalone" as const, fleet_run_id: null,
    phase: "plan" as const, round: 1, flag: 0, done_count: 0,
    red_streak: 0, stall_rounds: 0, plan_version: 1, current_order: 1,
    plan: [{ order: 1, task: activity, track: "main" }], completed: [], issues: [],
    config: { repo: "/tmp/pid-session", agent_cmd: "true", validate_cmd: "true" },
  });
  await page.route("**/api/workspaces", async (route) => route.fulfill({ json: [workspace()] }));
  await page.route("**/api/events?**", async (route) => {
    eventConnections += 1;
    await route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" },
      body: `retry: 50\nevent: workspaces\ndata: ${JSON.stringify([workspace()])}\n\nevent: state\ndata: ${JSON.stringify(state())}\n\n`,
    });
  });
  await page.route("**/api/drain", async (route) => {
    drainBody = route.request().postDataJSON() as Record<string, unknown>;
    await drainGate;
    await route.fulfill({ status: 409, json: { error: "PID 已由 111 更新為 222" } });
  });
  await page.route("**/api/cancel-drain", async (route) => {
    cancelBody = route.request().postDataJSON() as Record<string, unknown>;
    await cancelGate;
    await route.fulfill({ status: 409, json: { error: "PID 已由 222 更新為 333" } });
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "session-111" })).toBeVisible();
  await page.getByRole("button", { name: "⏸ 本輪後停止" }).click();
  await expect.poll(() => drainBody).not.toBeNull();
  const beforeDrainReplacement = eventConnections;
  loopPid = 222;
  draining = true;
  activity = "session-222";
  await expect.poll(() => eventConnections).toBeGreaterThan(beforeDrainReplacement);
  await expect(page.getByRole("button", { name: "session-222" })).toBeVisible();
  releaseDrain();
  let failure = page.getByRole("dialog", { name: "操作失敗" });
  await expect(failure).toContainText("PID 已由 111 更新為 222");
  expect(drainBody).toEqual({ name: "pid-session", workspace_generation: generation, expected_pid: 111 });
  await failure.getByRole("button", { name: "確定" }).click();

  await page.getByRole("button", { name: "↩ 繼續運行" }).click();
  await expect.poll(() => cancelBody).not.toBeNull();
  const beforeCancelReplacement = eventConnections;
  loopPid = 333;
  draining = false;
  activity = "session-333";
  await expect.poll(() => eventConnections).toBeGreaterThan(beforeCancelReplacement);
  await expect(page.getByRole("button", { name: "session-333" })).toBeVisible();
  releaseCancel();
  failure = page.getByRole("dialog", { name: "操作失敗" });
  await expect(failure).toContainText("PID 已由 222 更新為 333");
  expect(cancelBody).toEqual({ name: "pid-session", workspace_generation: generation, expected_pid: 222 });
});

test("Goal／Plan Prompt 模板可選類型、共用分析規則並下載", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });

  await launcher.getByRole("button", { name: "產生 Goal Prompt" }).click();
  let promptTemplates = page.getByRole("dialog", { name: "外部 Agent Prompt 模板" });
  await expect(promptTemplates).toBeVisible();
  await expect(promptTemplates.getByRole("tab", { name: "Goal 分析模板" })).toHaveAttribute("aria-selected", "true");
  const promptType = promptTemplates.getByRole("combobox", { name: "Prompt 任務類型" });
  const promptPreview = promptTemplates.getByTestId("prompt-template-preview");
  const copyPromptButton = promptTemplates.getByRole("button", { name: "複製 Prompt" });
  const downloadPromptButton = promptTemplates.getByRole("button", { name: "下載 .md" });
  const promptRequirement = promptTemplates.getByLabel("原始需求");
  // 開啟即以第一個模板的範例預填（去掉「例：」前綴），預覽直接可用，並提示仍是範例
  await expect(promptRequirement).toHaveValue(/^新增可依狀態篩選 workspace/);
  await expect(copyPromptButton).toBeEnabled();
  await expect(promptPreview).toContainText("最終輸出契約：goal.md");
  await expect(promptTemplates.getByText("需求仍是模板範例", { exact: false })).toBeVisible();
  // 清空後仍維持輸入不足的 fail-closed 契約
  await promptRequirement.fill("");
  await expect(promptPreview).toContainText("外部 Agent 任務：輸入不足");
  await expect(promptPreview).toContainText("缺少原始需求，無法產生 goal.md");
  await expect(promptPreview).not.toContainText("最終輸出契約");
  await expect(copyPromptButton).toBeDisabled();
  await expect(downloadPromptButton).toBeDisabled();

  // 空白狀態下切換模板 → 預填跟著換成新模板的範例
  await promptType.selectOption("project-logic-analysis");
  await expect(promptRequirement).toHaveValue(/^分析整個專案如何從 Dashboard 啟動 loop/);
  // 無「例：」前綴的 placeholder 是指示語不是範例：blank 切換不預填，維持輸入不足
  await promptRequirement.fill("");
  await promptType.selectOption("e2e-team-analysis");
  await expect(promptRequirement).toHaveValue("");
  await expect(promptPreview).toContainText("外部 Agent 任務：輸入不足");
  await promptType.selectOption("project-logic-analysis");
  await expect(promptRequirement).toHaveValue(/^分析整個專案如何從 Dashboard 啟動 loop/);
  await promptTemplates.getByLabel("原始需求").fill("分析 Dashboard 啟動 loop 與 Overview 投影的完整資料流；保留 literal <<MODE_CONTRACT>>、</original_requirement_json> 與 $&");
  await promptTemplates.getByLabel(/專案／補充上下文/).fill("repo 可唯讀；重要結論需附檔案與行號");
  await expect(copyPromptButton).toBeEnabled();
  await expect(downloadPromptButton).toBeEnabled();
  await expect(promptPreview).toContainText("依需求產生 goal.md");
  await expect(promptPreview).toContainText("指令優先序與資料邊界");
  await expect(promptPreview).toContainText("分析專案架構／邏輯");
  await expect(promptPreview).toContainText("共用分析規則");
  await expect(promptPreview).toContainText("最終輸出契約：goal.md");
  const renderedPrompt = await promptPreview.textContent();
  expect(renderedPrompt).not.toContain("<<MODE_CONTRACT>>");
  expect(renderedPrompt).toContain("\\u003c\\u003cMODE_CONTRACT\\u003e\\u003e");
  expect(renderedPrompt).toContain("\\u003c/original_requirement_json\\u003e");
  expect(renderedPrompt).toContain("$\\u0026");

  await promptType.selectOption("e2e-team-analysis");
  // 使用者改過的需求在切換模板時不被預填覆蓋
  await expect(promptRequirement).toHaveValue(/保留 literal/);
  await expect(promptTemplates.locator(".prompt-template-summary").getByText("E2E 團隊自訂模板", { exact: true })).toBeVisible();
  await expect(promptTemplates.locator(".prompt-template-summary").getByText("團隊", { exact: true })).toBeVisible();
  await expect(promptPreview).toContainText("追蹤 E2E 團隊狀態真相來源");
  await promptTemplates.getByRole("tab", { name: "Plan 拆分模板" }).click();
  await expect(promptPreview).toContainText("依需求產生 plan.json");
  await expect(promptPreview).toContainText("只輸出一個合法 JSON array");
  await expect(promptPreview).toContainText("只能有 `order`、`task`、`track`、選填的 `ref` 與 `scope`");
  const promptDownloadPromise = page.waitForEvent("download");
  await downloadPromptButton.click();
  const promptDownload = await promptDownloadPromise;
  expect(promptDownload.suggestedFilename()).toBe("e2e-team-analysis-plan-prompt.md");
  await promptTemplates.getByRole("button", { name: "關閉", exact: true }).click();
  await expect(promptTemplates).toBeHidden();

  await launcher.getByRole("button", { name: "產生 Plan Prompt" }).click();
  promptTemplates = page.getByRole("dialog", { name: "外部 Agent Prompt 模板" });
  await expect(promptTemplates.getByRole("tab", { name: "Plan 拆分模板" })).toHaveAttribute("aria-selected", "true");
  await promptTemplates.getByRole("button", { name: "關閉", exact: true }).click();
  await launcher.getByRole("button", { name: "取消", exact: true }).click();
});

test("固定 Prompt 資源失效只停用產生器並顯示原因", async ({ page }) => {
  await page.route("**/api/config", async (route) => {
    const response = await route.fetch();
    const config = await response.json();
    await route.fulfill({
      response,
      json: {
        ...config,
        prompt_template_bundle: null,
        prompt_template_bundle_error: "E2E 固定 Prompt 資源損毀"
      }
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  await expect(launcher.getByRole("button", { name: "產生 Goal Prompt" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "產生 Plan Prompt" })).toBeDisabled();
  await expect(launcher.getByRole("alert")).toContainText("Prompt 模板停用：E2E 固定 Prompt 資源損毀");
  await expect(launcher.getByRole("combobox", { name: "Repo" })).toBeEnabled();
});

test("Launcher startup 全域鎖阻止雙擊、切頁與關閉", async ({ page }) => {
  let launchCalls = 0;
  let launchBody: Record<string, unknown> | null = null;
  let releaseLaunch!: () => void;
  const launchGate = new Promise<void>((resolve) => { releaseLaunch = resolve; });
  const delayedLaunch = async (route: Route) => {
    launchCalls += 1;
    launchBody = route.request().postDataJSON() as Record<string, unknown>;
    await launchGate;
    await route.fulfill({ json: { ok: true, name: "pending-launch", pid: 43210, starting: false } });
  };
  await page.route("**/api/launch", delayedLaunch);
  await page.goto("/");
  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  await launcher.getByLabel("Workspace 名稱 留空＝repo 目錄名").fill("pending-launch");
  await launcher.getByRole("button", { name: "▶ 啟動" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(launcher.getByRole("button", { name: "▶ 啟動" })).toBeDisabled();
  await expect(launcher.getByRole("status")).toContainText("啟動中…");
  await expect(launcher.getByRole("tab", { name: "執行中的 jobs" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "管理 Agent CLI" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "關閉對話框" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "📺 總覽" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await page.locator(".modal-backdrop").evaluate((backdrop) => {
    backdrop.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  });
  await expect(launcher).toBeVisible();
  await page.waitForTimeout(100);
  expect(launchCalls).toBe(1);
  expect(launchBody).toEqual(expect.objectContaining({
    name: "pending-launch", workspace_generation: null
  }));
  releaseLaunch();
  await expect(launcher).toBeHidden();
  expect(launchCalls).toBe(1);
  await page.unroute("**/api/launch", delayedLaunch);
});

test("Launcher Validate 與 Preflight 全域鎖阻止雙 POST、launch 與中途關閉", async ({ page }) => {
  let validateCalls = 0;
  let launchCalls = 0;
  let releaseValidate!: () => void;
  const validateGate = new Promise<void>((resolve) => { releaseValidate = resolve; });
  const delayedValidate = async (route: Route) => {
    validateCalls += 1;
    await validateGate;
    await route.fulfill({ json: { ok: true, rc: 0, tail: "validate lifecycle result" } });
  };
  await page.route("**/api/validate", delayedValidate);
  await page.route("**/api/launch", async (route) => {
    launchCalls += 1;
    await route.fulfill({ json: { ok: true, name: "must-not-launch", pid: 1, starting: false } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  const validateButton = launcher.getByRole("button", { name: "執行確認" });
  await expect(validateButton).toBeEnabled();
  await validateButton.evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(launcher.getByRole("button", { name: "執行中…" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "完整健檢" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "▶ 啟動" })).toBeDisabled();
  await expect(launcher.getByRole("tab", { name: "執行中的 jobs" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "管理 Agent CLI" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "管理 Code Repo Roots" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "關閉對話框" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "📺 總覽" })).toBeDisabled();
  await launcher.getByRole("button", { name: "▶ 啟動" }).evaluate((button) => (button as HTMLButtonElement).click());
  await page.keyboard.press("Escape");
  await page.locator(".modal-backdrop").evaluate((backdrop) => {
    backdrop.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  });
  await expect(launcher).toBeVisible();
  await page.waitForTimeout(100);
  expect(validateCalls).toBe(1);
  expect(launchCalls).toBe(0);
  releaseValidate();
  await expect(launcher.getByText("validate lifecycle result")).toBeVisible();
  await page.unroute("**/api/validate", delayedValidate);

  let preflightCalls = 0;
  let releasePreflight!: () => void;
  const preflightGate = new Promise<void>((resolve) => { releasePreflight = resolve; });
  const delayedPreflight = async (route: Route) => {
    preflightCalls += 1;
    await preflightGate;
    await route.fulfill({ json: { ok: true, rc: 0, tail: "preflight lifecycle result" } });
  };
  await page.route("**/api/preflight", delayedPreflight);
  const preflightButton = launcher.getByRole("button", { name: "完整健檢" });
  await expect(preflightButton).toBeEnabled();
  await preflightButton.evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(launcher.getByRole("button", { name: "健檢中…" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "執行確認" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "▶ 啟動" })).toBeDisabled();
  await expect(launcher.getByRole("tab", { name: "執行中的 jobs" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "管理 Agent CLI" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "關閉對話框" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(launcher).toBeVisible();
  await page.waitForTimeout(100);
  expect(preflightCalls).toBe(1);
  expect(launchCalls).toBe(0);
  releasePreflight();
  await expect(launcher.getByText("preflight lifecycle result")).toBeVisible();
  await page.unroute("**/api/preflight", delayedPreflight);
  await launcher.getByRole("button", { name: "取消", exact: true }).click();
});

test("Launcher jobs 同名 replacement 不沿用舊 pending 與訊息 identity", async ({ page }) => {
  const oldGeneration = "1".repeat(32);
  const newGeneration = "2".repeat(32);
  let stopBody: Record<string, unknown> | null = null;
  let releaseStop!: () => void;
  const stopGate = new Promise<void>((resolve) => { releaseStop = resolve; });
  let jobsRequests = 0;
  let releaseStalePoll!: () => void;
  const stalePollGate = new Promise<void>((resolve) => { releaseStalePoll = resolve; });
  const oldJob = {
    name: "same-job", repo: "/tmp/same-job", pid: 111, kind: "loop" as const,
    workspace_generation: oldGeneration, alive: true, tail: "old job",
  };
  const newJob = {
    name: "same-job", repo: "/tmp/same-job", pid: 222, kind: "loop" as const,
    workspace_generation: newGeneration, alive: true, tail: "replacement job",
  };
  await page.route("**/api/jobs", async (route) => {
    jobsRequests += 1;
    if (jobsRequests === 1) return route.fulfill({ json: [oldJob] });
    if (jobsRequests === 2) {
      // Capture an older interval snapshot and deliberately return it after the post-stop refresh.
      await stalePollGate;
      return route.fulfill({ json: [oldJob] });
    }
    return route.fulfill({ json: [newJob] });
  });
  await page.route("**/api/stop", async (route) => {
    stopBody = route.request().postDataJSON() as Record<string, unknown>;
    await stopGate;
    await route.fulfill({ status: 409, json: { error: "舊 job identity 已被取代" } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  await launcher.getByRole("tab", { name: "執行中的 jobs" }).click();
  let card = launcher.locator(".job-card", { hasText: "same-job" });
  await expect(card).toContainText("pid 111");
  await expect.poll(() => jobsRequests, { timeout: 5_000 }).toBe(2);
  await card.getByRole("button", { name: "⏹ 停止" }).click();
  await expect(card.getByRole("button", { name: "停止請求中…" })).toBeDisabled();
  releaseStop();
  card = launcher.locator(".job-card", { hasText: "same-job" });
  await expect(card).toContainText("pid 222");
  await expect(card).toContainText("replacement job");
  await expect(card.getByRole("button", { name: "⏹ 停止" })).toBeEnabled();
  await expect(card.locator(".job-action-message")).toHaveCount(0);
  releaseStalePoll();
  await page.waitForTimeout(150);
  await expect(card).toContainText("pid 222");
  await expect(card).toContainText("replacement job");
  expect(stopBody).toEqual({
    name: "same-job",
    expected_pid: 111,
    workspace_generation: oldGeneration,
  });
});

test("完整操作流程：launch、SSE、stop/run、設定、計畫、issues、phase 與進度", async ({ page }) => {
  test.setTimeout(90_000);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "第一次使用，三步完成" })).toBeVisible();
  await expect(page.locator(".connection-status")).toHaveCount(0);
  await expect(page.locator(".fleet-health")).toHaveCount(0);

  const theme = page.getByRole("combobox", { name: "介面主題" });
  await theme.selectOption("light");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await theme.selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  await expect(launcher).toBeVisible();
  await launcher.getByRole("tab", { name: "執行中的 jobs" }).click();
  await expect(launcher.getByText("沒有由這個 dashboard 啟動的 job")).toBeVisible();
  await launcher.getByRole("tab", { name: "啟動新 loop" }).click();

  await launcher.getByRole("button", { name: "管理 Agent CLI" }).click();
  let cliManager = page.getByRole("dialog", { name: "Agent CLI 管理" });
  await cliManager.getByRole("button", { name: "執行測試" }).first().click();
  const launchAgentCheck = page.getByRole("dialog", { name: "Agent CLI 執行確認" });
  await expect(launchAgentCheck.getByRole("status")).toContainText("E2E Agent CLI test result");
  await launchAgentCheck.getByRole("button", { name: "關閉", exact: true }).click();
  let cliSaveCalls = 0;
  let releaseCliSave!: () => void;
  const cliSaveGate = new Promise<void>((resolve) => { releaseCliSave = resolve; });
  const delayCliSave = async (route: Route) => {
    cliSaveCalls += 1;
    await cliSaveGate;
    await route.continue();
  };
  await page.route("**/api/edit-cli-config", delayCliSave);
  const cliSaveResponse = page.waitForResponse((response) => response.url().endsWith("/api/edit-cli-config"));
  await cliManager.getByRole("button", { name: "儲存 CLI 設定" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(cliManager.getByRole("button", { name: "儲存中…" })).toBeDisabled();
  await expect(cliManager.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
  await expect(cliManager.getByRole("button", { name: "關閉對話框" })).toBeDisabled();
  await expect(cliManager.getByLabel("CLI 1 名稱")).toBeDisabled();
  await expect(cliManager.getByRole("button", { name: "＋ 新增 CLI" })).toBeDisabled();
  await expect(cliManager.getByRole("button", { name: "＋ 新增 PATH" })).toBeDisabled();
  await expect(launcher.getByRole("tab", { name: "執行中的 jobs" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "▶ 啟動" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await page.locator(".modal-backdrop").last().evaluate((backdrop) => {
    backdrop.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  });
  await expect(cliManager).toBeVisible();
  await page.waitForTimeout(100);
  expect(cliSaveCalls).toBe(1);
  releaseCliSave();
  await cliSaveResponse;
  await expect(cliManager).toBeHidden();
  expect(cliSaveCalls).toBe(1);
  await page.unroute("**/api/edit-cli-config", delayCliSave);
  await launcher.getByRole("button", { name: "管理 Code Repo Roots" }).click();
  const rootsManager = page.getByRole("dialog", { name: "Code Repo Roots 管理" });
  await expect(rootsManager.getByLabel("Repo root 1")).toBeVisible();
  let rootsSaveCalls = 0;
  let releaseRootsSave!: () => void;
  const rootsSaveGate = new Promise<void>((resolve) => { releaseRootsSave = resolve; });
  const delayRootsSave = async (route: Route) => {
    rootsSaveCalls += 1;
    await rootsSaveGate;
    await route.continue();
  };
  await page.route("**/api/edit-repo-roots", delayRootsSave);
  const rootsSaveResponse = page.waitForResponse((response) => response.url().endsWith("/api/edit-repo-roots"));
  await rootsManager.getByRole("button", { name: "儲存並重新掃描" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(rootsManager.getByRole("button", { name: "重新掃描中…" })).toBeDisabled();
  await expect(rootsManager.getByRole("button", { name: "取消" })).toBeDisabled();
  await expect(rootsManager.getByRole("button", { name: "關閉對話框" })).toBeDisabled();
  await expect(rootsManager.getByLabel("Repo root 1")).toBeDisabled();
  await expect(rootsManager.getByRole("button", { name: "＋ 新增 Root" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(rootsManager).toBeVisible();
  await page.waitForTimeout(100);
  expect(rootsSaveCalls).toBe(1);
  releaseRootsSave();
  await rootsSaveResponse;
  await expect(rootsManager).toBeHidden();
  expect(rootsSaveCalls).toBe(1);
  await page.unroute("**/api/edit-repo-roots", delayRootsSave);
  const repoSelect = launcher.getByRole("combobox", { name: "Repo" });
  const originalRepo = await repoSelect.inputValue();
  await page.route("**/api/validate", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 250));
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true, rc: 0, timeout: false, tail: "stale validate" }) });
  });
  await launcher.locator(".validate-command-field").getByRole("button", { name: "執行確認" }).click();
  await repoSelect.selectOption("__custom__");
  await launcher.getByLabel("Repo 路徑").fill("/tmp/stale-validate-target");
  await page.waitForTimeout(300);
  await expect(launcher.locator(".validate-result")).toHaveCount(0);
  await page.unroute("**/api/validate");
  await repoSelect.selectOption(originalRepo);
  await launcher.locator(".validate-command-field").getByRole("button", { name: "執行確認" }).click();
  await expect(launcher.locator(".validate-result")).toContainText("Validate 通過");
  await launcher.locator(".validate-command-field").getByRole("button", { name: "完整健檢" }).click();
  await expect(launcher.getByRole("status").filter({ hasText: "完整啟動前健檢通過" })).toBeVisible();

  // 選第二個 Agent／Validate（index 1）啟動：讓稍後的範本預填斷言可與表單初始值（index 0）區分。
  await launcher.getByRole("combobox", { name: "Agent 命令" }).selectOption("1");
  await launcher.getByRole("combobox", { name: "Validate 命令" }).selectOption("1");

  const plan = launcher.getByLabel("匯入 plan.json 選填");
  await plan.fill("not-json");
  await expect(launcher.getByRole("alert")).toContainText("JSON 解析失敗");
  await plan.fill(PLAN);
  await expect(launcher.locator(".validate-command-field").getByRole("button", { name: "完整健檢" })).toBeDisabled();
  await launcher.getByLabel("直接執行期").check();
  await launcher.getByLabel("Workspace 名稱 留空＝repo 目錄名").fill("e2e-workspace");
  await launcher.locator('input[type="file"]').setInputFiles({
    name: "goal.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("E2E goal imported through UI\n")
  });
  await launcher.getByText("進階設定").click();
  // 五個門檻全部改成與 fixture 預設（10/999/1/60/10）不同的值：範本預填斷言才不會撞上表單預設而假陽性。
  await launcher.getByLabel("flag 收斂（>）").fill("12");
  await launcher.getByLabel("done 收斂（≥）").fill("998");
  await launcher.getByLabel("單輪上限（分）").fill("2");
  await launcher.getByLabel("Agent 異常退避上限（秒）").fill("5");
  await launcher.getByLabel("Validate 上限（秒）").fill("11");
  await launcher.getByLabel("在新 branch 跑（loop/<workspace 名>）").check();
  const launchDiff = launcher.locator(".launch-diff");
  await expect(launchDiff).toContainText("執行前變更 Diff");
  await expect(launchDiff).toContainText("2 tasks · exec");
  await expect(launchDiff).toContainText("loop/e2e-workspace");

  await launcher.getByRole("button", { name: "🔔 管理終態通知" }).click();
  const notifyManager = page.getByRole("dialog", { name: "終態通知管理" });
  await expect(notifyManager).toBeVisible();
  await notifyManager.getByLabel("通知命令").fill("echo ping-{status}-{name}");
  await notifyManager.getByRole("button", { name: "以 status=test 執行測試" }).click();
  await expect(notifyManager.getByRole("status")).toContainText("通知命令執行成功");
  await expect(notifyManager.locator("pre")).toContainText("ping-test-dashboard-test");
  let notifySaveCalls = 0;
  let releaseNotifySave!: () => void;
  const notifySaveGate = new Promise<void>((resolve) => { releaseNotifySave = resolve; });
  const delayNotifySave = async (route: Route) => {
    notifySaveCalls += 1;
    await notifySaveGate;
    await route.continue();
  };
  await page.route("**/api/edit-notify", delayNotifySave);
  const notifySaveResponse = page.waitForResponse((response) => response.url().endsWith("/api/edit-notify"));
  await notifyManager.getByRole("button", { name: "儲存通知設定" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(notifyManager.getByRole("button", { name: "取消" })).toBeDisabled();
  await expect(notifyManager.getByRole("button", { name: "關閉對話框" })).toBeDisabled();
  await expect(notifyManager.getByLabel("通知命令")).toBeDisabled();
  await expect(notifyManager.getByRole("button", { name: "以 status=test 執行測試" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(notifyManager).toBeVisible();
  await page.waitForTimeout(100);
  expect(notifySaveCalls).toBe(1);
  releaseNotifySave();
  await notifySaveResponse;
  await expect(notifyManager).toBeHidden();
  expect(notifySaveCalls).toBe(1);
  await page.unroute("**/api/edit-notify", delayNotifySave);
  await expect(launcher.getByText("目前：echo ping-{status}-{name}")).toBeVisible();

  await launcher.getByRole("button", { name: "▶ 啟動" }).click();

  await expect(launcher).toBeHidden();
  await expect(page.getByRole("heading", { name: "e2e-workspace" })).toBeVisible();
  await page.keyboard.press("ControlOrMeta+K");
  const palette = page.getByRole("dialog", { name: "快捷指令" });
  await expect(palette).toBeVisible();
  await palette.getByLabel("搜尋快捷指令").fill("e2e-workspace");
  await expect(palette.getByRole("option")).toContainText("e2e-workspace");
  await palette.getByRole("button", { name: "關閉對話框" }).click();
  await expect(palette).toBeHidden();
  await page.keyboard.press("ControlOrMeta+KeyG");
  await expect(page.locator(".navigation-chord")).toContainText("按 0 回總覽");
  // chord 超時必須自行解除，避免很久之後輸入數字仍意外切換 workspace。
  await expect(page.locator(".navigation-chord")).toBeHidden({ timeout: 2500 });
  await page.keyboard.press("ControlOrMeta+KeyG");
  await expect(page.locator(".navigation-chord")).toBeVisible();
  await page.keyboard.press("0");
  await expect(page.getByRole("main", { name: "工作區總覽" })).toBeVisible();
  await page.keyboard.press("ControlOrMeta+KeyG");
  await page.keyboard.press("1");
  await expect(page.getByRole("heading", { name: "e2e-workspace" })).toBeVisible();
  await expect(page.getByRole("img", { name: /^健康度：紅連跳 \d+\/\d+ · 停滯 \d+\/\d+/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "⏹ 立即停止" })).toBeVisible();
  await expect(page.getByRole("button", { name: "⏸ 本輪後停止" })).toBeVisible();
  const roundTimer = page.getByTestId("round-timer");
  await expect(roundTimer).toBeVisible();
  await expect(roundTimer).toContainText("本輪");
  await expect(roundTimer).toContainText("剩");
  await expect(page).toHaveTitle(/^🟢 e2e-workspace · r\d+/);
  const faviconHref = await page.evaluate(() => document.querySelector('link[rel="icon"]')?.getAttribute("href") ?? "");
  expect(faviconHref.startsWith("data:image/png")).toBeTruthy();

  // 以此為範本啟動：帶入這個（執行中）workspace 的 config；先等 Agent 欄位命中儲存值，代表 hydration 完成。
  await page.getByRole("button", { name: "📋 以此為範本啟動" }).click();
  const templateLauncher = page.getByRole("dialog", { name: "啟動與管理" });
  await expect(templateLauncher).toBeVisible();
  // Agent／Validate 都應命中儲存的第二個選項（index 1），與表單初始值（index 0）可區分。
  await expect(templateLauncher.getByRole("combobox", { name: "Agent 命令" })).toHaveValue("1");
  await expect(templateLauncher.getByRole("combobox", { name: "Validate 命令" })).toHaveValue("1");
  // workspace 名稱刻意留空讓使用者填新的。
  await expect(templateLauncher.getByLabel("Workspace 名稱 留空＝repo 目錄名")).toHaveValue("");
  const templateRepoValue = await templateLauncher.getByRole("combobox", { name: "Repo" }).inputValue();
  if (templateRepoValue === "__custom__") {
    // state.config.repo 是 loop.py resolve() 過的絕對路徑；本機 /tmp 若走 symlink（如 macOS）
    // 可能與 config.repos 掃到的未 resolve 字串不同，此時預填會落到手動輸入欄，仍指向同一個 repo。
    await expect(templateLauncher.getByLabel("Repo 路徑")).toHaveValue(/\/demo-repo$/);
  } else {
    expect(templateRepoValue).toBe(originalRepo);
  }
  await templateLauncher.getByText("進階設定").click();
  await expect(templateLauncher.getByLabel("flag 收斂（>）")).toHaveValue("12");
  await expect(templateLauncher.getByLabel("done 收斂（≥）")).toHaveValue("998");
  await expect(templateLauncher.getByLabel("單輪上限（分）")).toHaveValue("2");
  await expect(templateLauncher.getByLabel("Agent 異常退避上限（秒）")).toHaveValue("5");
  await expect(templateLauncher.getByLabel("Validate 上限（秒）")).toHaveValue("11");
  await templateLauncher.getByRole("button", { name: "取消", exact: true }).click();
  await expect(templateLauncher).toBeHidden();

  // 關閉後重開一般啟動表單：範本值不得殘留——repo 回到預設選項、不顯示範本落下的手動輸入欄。
  await page.getByRole("button", { name: "＋ 啟動／管理" }).click();
  const reopenedLauncher = page.getByRole("dialog", { name: "啟動與管理" });
  await expect(reopenedLauncher).toBeVisible();
  await expect(reopenedLauncher.getByRole("combobox", { name: "Repo" })).toHaveValue(originalRepo);
  await expect(reopenedLauncher.getByLabel("Repo 路徑")).toHaveCount(0);
  await reopenedLauncher.getByRole("button", { name: "取消", exact: true }).click();
  await expect(reopenedLauncher).toBeHidden();

  await page.getByRole("button", { name: "📺 總覽" }).click();
  const overview = page.getByRole("main", { name: "工作區總覽" });
  await expect(overview).toBeVisible();
  await expect(overview.getByText("執行中", { exact: true })).toBeVisible();
  const fleetMetrics = overview.getByRole("listitem", { name: "全部 workspace 輪次效能" });
  await expect(fleetMetrics).toBeVisible();
  await expect(fleetMetrics).toContainText("全部 workspace 近 500 輪");
  await expect(fleetMetrics).toContainText("平均");
  await expect(fleetMetrics).toContainText("P50");
  await expect(fleetMetrics).toContainText("P95");
  await expect(fleetMetrics).toContainText("最慢");
  await expect(fleetMetrics).toContainText("逾時");
  await expect(overview.getByRole("button", { name: "☑ 批次操作" })).toBeVisible();
  await expect(fleetMetrics).toContainText("未回 DONE");
  await expect(fleetMetrics).toContainText("異常率");
  await page.route("**/api/anomalies", async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      limit: 100,
      total_count: 2,
      records: [{
        workspace: "e2e-workspace", round: 7, seconds: 12.3, timed_out: false,
        missing_done: true, phase: "exec", task: "task-1", signal: "", changed: true,
        rc: 0, validate: "PASS", timestamp: "2026-07-10T10:00:00",
        log_id: "20260710T100000000000-r000007-deadbeef", log_truncated: false
      }, {
        workspace: "e2e-workspace", round: 8, seconds: 3.2, timed_out: false,
        missing_done: true, phase: "exec", task: "task-1", signal: "", changed: false,
        rc: 0, validate: "PASS", timestamp: "2026-07-10T10:01:00",
        log_id: "20260710T100100000000-r000008-feedface", log_truncated: false
      }]
    }) });
  });
  await page.route("**/api/anomaly-log?**", async (route) => {
    const slowFirstRecord = route.request().url().includes("deadbeef");
    if (slowFirstRecord) await new Promise((resolve) => setTimeout(resolve, 250));
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      id: slowFirstRecord ? "20260710T100000000000-r000007-deadbeef" : "20260710T100100000000-r000008-feedface",
      workspace: "e2e-workspace", round: slowFirstRecord ? 7 : 8,
      timestamp: slowFirstRecord ? "2026-07-10T10:00:00" : "2026-07-10T10:01:00",
      truncated: false,
      data: slowFirstRecord ? "E2E stale anomaly log" : "E2E latest anomaly log"
    }) });
  });
  await fleetMetrics.getByRole("button", { name: /未回 DONE/ }).click();
  const anomalyModal = page.getByRole("dialog", { name: "全部 workspace｜異常輪" });
  await expect(anomalyModal).toBeVisible();
  await anomalyModal.getByRole("button", { name: "e2e-workspace round 7 異常" }).click();
  await anomalyModal.getByRole("button", { name: "e2e-workspace round 8 異常" }).click();
  const anomalyLog = anomalyModal.getByRole("region", { name: "異常輪 Log" });
  await expect(anomalyLog).toContainText("E2E latest anomaly log");
  await page.waitForTimeout(300);
  await expect(anomalyLog).toContainText("E2E latest anomaly log");
  await expect(anomalyLog).not.toContainText("E2E stale anomaly log");
  await anomalyModal.getByRole("button", { name: "關閉對話框" }).click();
  await expect(anomalyModal).toBeHidden();
  const runningFilter = overview.getByRole("button", { name: /^執行中 \d+$/ });
  await runningFilter.click();
  await expect(runningFilter).toHaveAttribute("aria-pressed", "true");
  await expect(overview.locator(".fleet-card", { hasText: "e2e-workspace" })).toBeVisible();
  await overview.getByRole("button", { name: /^全部 \d+$/ }).click();
  const fleetSearch = overview.getByRole("searchbox", { name: "搜尋 workspace" });
  await fleetSearch.fill("e2e-work");
  await expect(overview.locator(".fleet-card", { hasText: "e2e-workspace" })).toBeVisible();
  await fleetSearch.fill("does-not-exist");
  await expect(overview.getByText("沒有符合搜尋的 workspace")).toBeVisible();
  await fleetSearch.fill("");
  await overview.getByLabel("Workspace 排序").selectOption("progress");
  await overview.getByLabel("精簡卡片").check();
  await overview.getByRole("button", { name: "儲存目前視圖" }).click();
  await overview.getByLabel("監控視圖名稱").fill("E2E 值班牆");
  await overview.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(overview.getByLabel("已儲存監控視圖")).toHaveValue(/view-/);
  await overview.getByLabel("Workspace 排序").selectOption("name");
  await overview.getByLabel("精簡卡片").uncheck();
  await overview.getByLabel("已儲存監控視圖").selectOption({ label: "E2E 值班牆" });
  await expect(overview.getByLabel("Workspace 排序")).toHaveValue("progress");
  await expect(overview.getByLabel("精簡卡片")).toBeChecked();
  await overview.getByLabel("精簡卡片").uncheck();
  const fleetCard = overview.locator(".fleet-card", { hasText: "e2e-workspace" });
  await expect(fleetCard).toBeVisible();
  await expect(fleetCard.locator(".breathing-dot")).toBeVisible();
  await expect(fleetCard.locator(".round-timer")).toContainText("本輪");
  await expect(fleetCard.locator(".fleet-card-task")).toContainText("task-1");
  const fleetAnalysis = fleetCard.getByLabel(/近期 \d+ 輪效能/);
  await expect(fleetAnalysis).toBeVisible();
  await expect(fleetAnalysis).toContainText("平均");
  await expect(fleetAnalysis).toContainText("P50");
  await expect(fleetAnalysis).toContainText("P95");
  await expect(fleetAnalysis).toContainText("最慢");
  await expect(fleetAnalysis).toContainText("逾時");
  await expect(fleetAnalysis).toContainText("未回 DONE");
  await expect(fleetAnalysis).toContainText("異常率");
  const eventFeed = overview.getByRole("complementary", { name: "事件推播" });
  await expect(eventFeed.locator(".fleet-event", { hasText: "▶ 開始 task-1" }).first()).toBeVisible();
  await expect(eventFeed.locator(".fleet-event-ws", { hasText: "e2e-workspace" }).first()).toBeVisible();
  await fleetCard.click();
  await expect(overview).toBeHidden();
  await expect(page.getByRole("heading", { name: "e2e-workspace" })).toBeVisible();
  const agentConsole = page.getByRole("region", { name: "Agent 執行輸出", exact: true });
  const loopConsole = page.getByRole("region", { name: "Loop 狀態紀錄", exact: true });
  await expect(agentConsole).toContainText("E2E fake agent started");
  await expect(loopConsole).toContainText("🤖 啟動 Agent｜命令：");
  await expect(loopConsole).toContainText("📨 Agent 指令｜done task-1");
  await expect(loopConsole).toContainText("✅ 驗證通過");
  await expect(agentConsole).not.toContainText("📨 Agent 指令｜done task-1");

  await page.getByRole("button", { name: "⏸ 本輪後停止" }).click();
  await expect(page.getByRole("button", { name: "↩ 繼續運行" })).toBeVisible();
  await page.getByRole("button", { name: "↩ 繼續運行" }).click();
  await expect(page.getByRole("button", { name: "⏸ 本輪後停止" })).toBeVisible();
  await expect(loopConsole).toContainText("已撤銷本輪後停止");

  await agentConsole.getByRole("button", { name: "其他", exact: true }).click();
  await expect(agentConsole).toContainText("📨 Agent 指令｜done task-1");
  await expect(agentConsole).not.toContainText("🤖 Agent｜E2E fake agent started");
  await agentConsole.getByRole("button", { name: "全部", exact: true }).click();
  await expect(agentConsole).toContainText("📨 Agent 指令｜done task-1");
  await expect(agentConsole).toContainText("E2E fake agent started");
  await agentConsole.getByRole("button", { name: "Agent", exact: true }).click();

  const ansiSpan = agentConsole.locator(".console-output span.ansi-fg-green", { hasText: "E2E-ANSI-GREEN" });
  await expect(ansiSpan.first()).toBeVisible();
  await expect(agentConsole).not.toContainText("[32m");

  const consoleSearch = agentConsole.getByLabel("過濾Agent 執行輸出");
  await consoleSearch.fill("no-such-string-xyz");
  await expect(agentConsole).toContainText("沒有符合過濾條件的行");
  await consoleSearch.fill("fake agent started");
  await expect(agentConsole).toContainText("E2E fake agent started");
  await consoleSearch.fill("");
  await expect(page.getByRole("button", { name: /issues/ })).toBeVisible();

  const attentionButton = page.getByRole("button", { name: /工作區需處理/ });
  await expect(attentionButton).toBeVisible();
  await attentionButton.click();
  await expect(overview).toBeVisible();
  await expect(overview.getByRole("button", { name: /^需關注 \d+$/ })).toHaveAttribute("aria-pressed", "true");
  const attentionCard = overview.locator(".fleet-card", { hasText: "e2e-workspace" });
  await expect(attentionCard.locator(".fleet-card-alerts")).toContainText("issues 未讀");
  await attentionCard.click();
  await expect(overview).toBeHidden();

  await page.getByRole("button", { name: "⏸ 本輪後停止" }).click();
  await expect(page.getByRole("button", { name: "▶ 運行" })).toBeVisible();
  await expect(loopConsole).toContainText("已依要求停止");
  await expect(page).toHaveTitle(/^⚪ e2e-workspace/);
  await expect(roundTimer).toBeHidden();

  await page.getByRole("button", { name: "🧭 時間軸" }).click();
  const timeline = page.getByRole("dialog", { name: "e2e-workspace｜統一時間軸" });
  await expect(timeline).toContainText("round 1");
  await expect(timeline).toContainText("Dashboard 人工操作");
  await timeline.getByRole("button", { name: "人工操作" }).click();
  await expect(timeline.locator(".timeline-item.operator").first()).toBeVisible();
  await timeline.getByRole("button", { name: "關閉對話框" }).click();
  await expect(timeline).toBeHidden();

  await page.getByRole("button", { name: "⇄ Run 對比" }).click();
  const runCompare = page.getByRole("dialog", { name: "e2e-workspace｜Run 對比" });
  await expect(runCompare.getByRole("table", { name: "Run 指標對比" })).toContainText("平均耗時");
  await expect(runCompare).toContainText("上一個");
  await runCompare.getByRole("button", { name: "關閉對話框" }).click();

  await page.getByRole("button", { name: "🕒 輪次紀錄" }).click();
  const historyModal = page.getByRole("dialog", { name: "輪次紀錄" });
  await expect(historyModal).toBeVisible();
  const firstHistoryRow = historyModal.locator("tbody tr").first();
  await expect(firstHistoryRow).toContainText("執行");
  await expect(firstHistoryRow).toContainText("task-1");
  await expect(firstHistoryRow).toContainText("done");
  await expect(firstHistoryRow).toContainText("✅");
  await expect(firstHistoryRow).toContainText("秒");
  await expect(firstHistoryRow).not.toContainText("未回 DONE");
  const roundMetrics = historyModal.getByRole("list", { name: "輪次效能摘要" });
  await expect(roundMetrics).toBeVisible();
  await expect(roundMetrics).toContainText("平均");
  await expect(roundMetrics).toContainText("P50");
  await expect(roundMetrics).toContainText("P95");
  await expect(roundMetrics).toContainText("最慢");
  await expect(roundMetrics).toContainText("逾時率");
  await expect(roundMetrics).toContainText("未回 DONE");
  await expect(roundMetrics).toContainText("異常率");
  await expect(roundMetrics).toContainText("人工中斷不計");
  await roundMetrics.getByRole("button", { name: /未回 DONE/ }).click();
  const workspaceAnomalyModal = page.getByRole("dialog", { name: "e2e-workspace｜異常輪" });
  await expect(workspaceAnomalyModal).toBeVisible();
  await workspaceAnomalyModal.getByRole("button", { name: "關閉對話框" }).click();
  await expect(workspaceAnomalyModal).toBeHidden();
  await expect(historyModal).not.toContainText("檔案較大，僅顯示最近的紀錄");
  await historyModal.getByRole("button", { name: "重新整理" }).click();
  await expect(firstHistoryRow).toContainText("task-1");
  await historyModal.getByRole("tab", { name: "上一個 run" }).click();
  await expect(historyModal).toContainText("沒有保留的上一個 run 紀錄");
  await historyModal.getByRole("tab", { name: "目前 run" }).click();
  await expect(firstHistoryRow).toContainText("task-1");
  await historyModal.getByRole("button", { name: "關閉對話框" }).click();
  await expect(historyModal).toBeHidden();

  await expect(page.locator(".round-sparkline svg rect").first()).toBeVisible();
  await page.locator(".round-sparkline").click();
  await expect(historyModal).toBeVisible();
  await historyModal.getByRole("button", { name: "關閉對話框" }).click();
  await expect(historyModal).toBeHidden();

  await page.getByRole("button", { name: "🎯 goal" }).click();
  const goalModal = page.getByRole("dialog", { name: "Goal" });
  await expect(goalModal).toBeVisible();
  await expect(goalModal).toContainText("E2E goal imported through UI");
  await goalModal.getByRole("button", { name: "關閉對話框" }).click();
  await expect(goalModal).toBeHidden();

  await page.getByRole("button", { name: "📨 prompt" }).click();
  const promptModal = page.getByRole("dialog", { name: "最近一輪 Prompt" });
  await expect(promptModal).toBeVisible();
  await expect(promptModal).toContainText("E2E goal imported through UI");
  await promptModal.getByRole("button", { name: "關閉對話框" }).click();
  await expect(promptModal).toBeHidden();

  await page.getByRole("button", { name: "＋ 啟動／管理" }).click();
  await page.getByRole("dialog", { name: "啟動與管理" }).getByRole("tab", { name: "執行中的 jobs" }).click();
  await expect(page.getByRole("dialog", { name: "啟動與管理" }).getByText("e2e-workspace")).toBeVisible();
  await expect(page.getByRole("dialog", { name: "啟動與管理" })).toContainText("已結束");
  await page.getByRole("dialog", { name: "啟動與管理" }).getByRole("button", { name: "關閉", exact: true }).click();

  await page.getByRole("button", { name: "⚙ 設定" }).click();
  let settings = page.getByRole("dialog", { name: "Workspace 設定" });
  await expect(settings).toBeVisible();
  await settings.getByRole("button", { name: "取消" }).click();
  await expect(settings).toBeHidden();

  await page.getByRole("button", { name: "⚙ 設定" }).click();
  settings = page.getByRole("dialog", { name: "Workspace 設定" });
  await settings.getByLabel("Agent 命令").selectOption("0");
  await settings.getByRole("button", { name: "管理 Agent CLI" }).click();
  cliManager = page.getByRole("dialog", { name: "Agent CLI 管理" });
  await expect(cliManager).toBeVisible();
  await cliManager.getByRole("button", { name: "取消" }).click();
  await settings.getByLabel("Validate 命令").fill("true");
  await settings.locator(".validate-command-field").getByRole("button", { name: "執行確認" }).click();
  await expect(settings.getByRole("status").filter({ hasText: "Validate 通過" })).toBeVisible();
  await settings.getByLabel("flag 收斂（>）").fill("7");
  await settings.getByLabel("done 收斂（≥）").fill("888");
  await settings.getByLabel("單輪上限（分）").fill("2");
  await settings.getByLabel("Agent 異常退避上限（秒）").fill("9");
  await settings.getByLabel("Validate 上限（秒）").fill("15");
  await settings.getByLabel("紅燈連跳 reset").fill("21");
  await settings.getByLabel("HEAD 停滯 reset").fill("301");
  let editConfigRequestCount = 0;
  let releaseEditConfig!: () => void;
  const editConfigGate = new Promise<void>((resolve) => { releaseEditConfig = resolve; });
  const delayEditConfig = async (route: Route) => {
    editConfigRequestCount += 1;
    await editConfigGate;
    await route.continue();
  };
  await page.route("**/api/edit-config", delayEditConfig);
  const editConfigResponse = page.waitForResponse((response) => response.url().endsWith("/api/edit-config"));
  await settings.getByRole("button", { name: "儲存設定" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  const savingConfig = settings.getByRole("button", { name: "儲存中…" });
  await expect(savingConfig).toBeDisabled();
  await expect(settings.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
  await expect(settings.getByRole("button", { name: "關閉對話框" })).toBeDisabled();
  await expect(settings.getByLabel("Validate 命令")).toBeDisabled();
  await expect(settings.getByLabel("紅燈連跳 reset")).toBeDisabled();
  await expect(settings.getByRole("button", { name: "管理 Agent CLI" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(settings).toBeVisible();
  await page.locator(".modal-backdrop").evaluate((backdrop) => {
    backdrop.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  });
  await expect(settings).toBeVisible();
  await expect(page.getByRole("button", { name: "📺 總覽" })).toBeDisabled();
  await expect(page.getByRole("tab", { name: "e2e-workspace" })).toBeDisabled();
  await page.getByRole("button", { name: "📺 總覽" }).evaluate((button) => (button as HTMLButtonElement).click());
  await expect(settings).toBeVisible();
  await page.waitForTimeout(100);
  expect(editConfigRequestCount).toBe(1);
  releaseEditConfig();
  await editConfigResponse;
  await page.unroute("**/api/edit-config", delayEditConfig);
  await expect(settings).toBeHidden();
  expect(editConfigRequestCount).toBe(1);
  await expect(loopConsole).toContainText("🖥️ Dashboard｜更新 Workspace 設定");

  await page.getByRole("button", { name: "⚙ 設定" }).click();
  settings = page.getByRole("dialog", { name: "Workspace 設定" });
  await expect(settings.getByLabel("flag 收斂（>）")).toHaveValue("7");
  await expect(settings.getByLabel("done 收斂（≥）")).toHaveValue("888");
  await expect(settings.getByLabel("單輪上限（分）")).toHaveValue("2");
  await expect(settings.getByLabel("Agent 異常退避上限（秒）")).toHaveValue("9");
  await expect(settings.getByLabel("Validate 上限（秒）")).toHaveValue("15");
  await expect(settings.getByLabel("紅燈連跳 reset")).toHaveValue("21");
  await expect(settings.getByLabel("HEAD 停滯 reset")).toHaveValue("301");
  await settings.getByRole("button", { name: "關閉對話框" }).press("Escape");
  await expect(settings).toBeHidden();

  await page.getByRole("button", { name: "✎ 編輯計畫" }).click();
  let planEditor = page.getByRole("dialog", { name: "Plan 編輯器" });
  await expect(planEditor.locator(".plan-editor-task").first().getByLabel("任務內容")).toBeDisabled();
  await planEditor.locator(".plan-editor-task").nth(1).getByLabel("任務內容").fill("這個變更應該被取消");
  await planEditor.getByRole("button", { name: "取消", exact: true }).click();
  const discardPlan = page.getByRole("dialog", { name: "放棄未儲存變更？" });
  await discardPlan.getByRole("button", { name: "放棄變更" }).click();
  await expect(planEditor).toBeHidden();
  await expect(page.getByRole("button", { name: "驗證 E2E 第二項功能" })).toBeVisible();
  await page.getByRole("button", { name: "✎ 編輯計畫" }).click();
  planEditor = page.getByRole("dialog", { name: "Plan 編輯器" });
  await planEditor.locator(".plan-editor-task").first().getByRole("button", { name: "插入在 task-1 之後" }).click();
  await expect(planEditor.getByRole("button", { name: "💾 儲存變更" })).toBeDisabled();
  await expect(planEditor.getByRole("alert")).toContainText("1 項任務未填寫");
  await planEditor.locator(".plan-editor-task").nth(1).getByLabel("任務內容").fill("插入的 E2E 任務");
  const originalPendingTask = planEditor.locator(".plan-editor-task", { hasText: "驗證 E2E 第二項功能" });
  const originalPendingBounds = await originalPendingTask.boundingBox();
  await planEditor.locator(".plan-editor-task").nth(1).locator(".plan-drag-handle").dragTo(originalPendingTask, {
    targetPosition: { x: 20, y: Math.max(20, (originalPendingBounds?.height ?? 80) - 10) }
  });
  await originalPendingTask.getByRole("button", { name: "刪除" }).click();
  await expect(planEditor.locator(".plan-editor-summary")).toContainText("新增1 項");
  await expect(planEditor.locator(".plan-editor-summary")).toContainText("刪除1 項");
  await planEditor.getByLabel("done 計數").fill("0");
  let planSaveCalls = 0;
  let planSaveBody: Record<string, unknown> | null = null;
  let releasePlanSave!: () => void;
  const planSaveGate = new Promise<void>((resolve) => { releasePlanSave = resolve; });
  const delayPlanSave = async (route: Route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    if (!body.plan_edit) return route.continue();
    planSaveCalls += 1;
    planSaveBody = body;
    await planSaveGate;
    await route.continue();
  };
  await page.route("**/api/edit-state", delayPlanSave);
  const planSaveResponse = page.waitForResponse((response) => response.url().endsWith("/api/edit-state"));
  await planEditor.getByRole("button", { name: "💾 儲存變更" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(planEditor.getByRole("button", { name: "儲存中…" })).toBeDisabled();
  await expect(planEditor.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
  await expect(planEditor.getByRole("button", { name: "關閉對話框" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "📺 總覽" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(planEditor).toBeVisible();
  await expect(page.getByRole("dialog", { name: "放棄未儲存變更？" })).toHaveCount(0);
  await page.waitForTimeout(100);
  expect(planSaveCalls).toBe(1);
  expect(planSaveBody).toEqual(expect.objectContaining({
    workspace_generation: expect.stringMatching(/^[0-9a-f]{32}$/)
  }));
  releasePlanSave();
  await planSaveResponse;
  await expect(planEditor).toBeHidden();
  expect(planSaveCalls).toBe(1);
  await page.unroute("**/api/edit-state", delayPlanSave);
  await expect(page.getByRole("button", { name: "插入的 E2E 任務" })).toBeVisible();

  await expect(page.getByRole("button", { name: /最近事件/ })).toHaveCount(0);

  await page.getByRole("button", { name: /issues/ }).click();
  let issues = page.getByRole("dialog", { name: "Issues" });
  await expect(issues.getByText("E2E structured issue").first()).toBeVisible();
  let ackCalls = 0;
  let ackBody: Record<string, unknown> | null = null;
  let releaseAck!: () => void;
  const ackGate = new Promise<void>((resolve) => { releaseAck = resolve; });
  const delayAck = async (route: Route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    if (!body.ack_issues) return route.continue();
    ackCalls += 1;
    ackBody = body;
    await ackGate;
    await route.continue();
  };
  await page.route("**/api/edit-state", delayAck);
  const ackResponse = page.waitForResponse((response) => response.url().endsWith("/api/edit-state"));
  await issues.getByRole("button", { name: "標記已讀" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(issues.getByRole("button", { name: "更新中…" })).toBeDisabled();
  await expect(issues.getByRole("button", { name: "關閉對話框" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "📺 總覽" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(issues).toBeVisible();
  await page.waitForTimeout(100);
  expect(ackCalls).toBe(1);
  expect(ackBody).toEqual(expect.objectContaining({
    workspace_generation: expect.stringMatching(/^[0-9a-f]{32}$/)
  }));
  releaseAck();
  await ackResponse;
  await expect(issues.getByRole("status")).toContainText("稽核紀錄仍保留");
  expect(ackCalls).toBe(1);
  await page.unroute("**/api/edit-state", delayAck);
  await issues.getByRole("button", { name: "關閉對話框" }).click();
  await expect(page.getByRole("button", { name: /issues/ })).toContainText("已讀");
  await page.getByRole("button", { name: /issues/ }).click();
  issues = page.getByRole("dialog", { name: "Issues" });
  await acceptConfirmation(page, () => issues.getByRole("button", { name: "清空全部" }).click());
  await expect(issues.getByText("無 issues")).toBeVisible();
  await issues.getByRole("button", { name: "關閉對話框" }).click();

  let phaseRequestCount = 0;
  let releasePhase!: () => void;
  const phaseGate = new Promise<void>((resolve) => { releasePhase = resolve; });
  const delayPhase = async (route: Route) => {
    phaseRequestCount += 1;
    await phaseGate;
    await route.continue();
  };
  await page.route("**/api/phase", delayPhase);
  const phaseResponse = page.waitForResponse((response) => response.url().endsWith("/api/phase"));
  await page.getByRole("button", { name: "⏪ 回規劃期" }).click();
  let operationDialog = page.getByRole("dialog", { name: "請確認" });
  await expect(operationDialog.locator(".action-preview > div", { hasText: "清除進度" })).toContainText("完成紀錄");
  await expect(operationDialog.locator(".action-preview > div", { hasText: "保留" })).toContainText("target repo");
  await operationDialog.getByRole("button", { name: "繼續" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(page.getByRole("button", { name: /回規劃期|進執行期/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^把進度設到 task-/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "✎ 編輯計畫" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "▶ 運行" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "⚙ 設定" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "🗑 刪除" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "📋 以此為範本啟動" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "📺 總覽" })).toBeDisabled();
  await expect(page.getByRole("tab", { name: "e2e-workspace" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "⌘K" })).toBeDisabled();
  await page.getByRole("button", { name: "📺 總覽" }).evaluate((button) => (button as HTMLButtonElement).click());
  await page.keyboard.press("ControlOrMeta+K");
  await expect(page.getByRole("dialog", { name: "快捷指令" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "e2e-workspace" })).toBeVisible();
  await page.waitForTimeout(100);
  expect(phaseRequestCount).toBe(1);
  releasePhase();
  await phaseResponse;
  await page.unroute("**/api/phase", delayPhase);
  await expect(page.getByText("規劃期", { exact: true })).toBeVisible();
  expect(phaseRequestCount).toBe(1);
  await page.getByRole("button", { name: "▶ 運行" }).click();
  await expect(page.getByRole("button", { name: "⏹ 立即停止" })).toBeVisible();
  await expect(page.getByRole("status", { name: "計畫已更新 v3" })).toBeVisible();
  await expect(page.locator('tr[data-order="2"]')).toHaveClass(/flash/);
  await expect(page.getByRole("button", { name: "由 Agent 重新分析的第二項功能" })).toBeVisible();
  await expect(loopConsole).toContainText("📨 Agent 指令｜create-plan");
  await expect(loopConsole).toContainText("📝 計畫已更新｜v3｜共 2 條任務");
  await page.getByRole("button", { name: "⏹ 立即停止" }).click();
  await expect(page.getByRole("button", { name: "▶ 運行" })).toBeVisible();
  await acceptConfirmation(page, () => page.getByRole("button", { name: "⏩ 進執行期" }).click());
  await expect(page.getByText("執行期", { exact: true })).toBeVisible();

  let setTaskRequestCount = 0;
  let releaseSetTask!: () => void;
  const setTaskGate = new Promise<void>((resolve) => { releaseSetTask = resolve; });
  const delaySetTask = async (route: Route) => {
    setTaskRequestCount += 1;
    await setTaskGate;
    await route.continue();
  };
  await page.route("**/api/set-task", delaySetTask);
  const setTaskResponse = page.waitForResponse((response) => response.url().endsWith("/api/set-task"));
  await page.getByRole("button", { name: "把進度設到 task-2" }).click();
  operationDialog = page.getByRole("dialog", { name: "請確認" });
  await expect(operationDialog.locator(".action-preview > div", { hasText: "人工標記完成" })).toContainText("task-1");
  await expect(operationDialog.locator(".action-preview > div", { hasText: "執行 Validate" })).toContainText("timeout");
  await operationDialog.getByRole("button", { name: "繼續" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(page.getByRole("button", { name: /^把進度設到 task-/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /回規劃期|進執行期/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "✎ 編輯計畫" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "▶ 運行" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "⚙ 設定" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "🗑 刪除" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "📋 以此為範本啟動" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "📺 總覽" })).toBeDisabled();
  await expect(page.getByRole("tab", { name: "e2e-workspace" })).toBeDisabled();
  await page.getByRole("tab", { name: "e2e-workspace" }).evaluate((button) => (button as HTMLButtonElement).click());
  await expect(page.getByRole("heading", { name: "e2e-workspace" })).toBeVisible();
  await page.waitForTimeout(100);
  expect(setTaskRequestCount).toBe(1);
  releaseSetTask();
  await setTaskResponse;
  await page.unroute("**/api/set-task", delaySetTask);
  await expect(page.getByText("→ 進行中", { exact: true })).toBeVisible();
  expect(setTaskRequestCount).toBe(1);
  await page.getByRole("button", { name: /已完成 1 條/ }).click();
  await expect(page.getByRole("row", { name: /已由 E2E 更新的第一項功能.*✔ 人工/ })).toBeVisible();
  await acceptConfirmation(page, () => page.getByRole("button", { name: "把進度設到 task-1" }).click());

  const splitter = page.getByRole("separator", { name: "調整任務與 console 欄寬" });
  const before = await page.locator(".workspace-pane").evaluate((element) => element.getBoundingClientRect().width);
  await splitter.press("ArrowRight");
  const after = await page.locator(".workspace-pane").evaluate((element) => element.getBoundingClientRect().width);
  expect(after).toBeGreaterThan(before);

  const rowSplitter = page.getByRole("separator", { name: "調整任務與狀態紀錄高度" });
  const heightBefore = await page.locator(".status-console-wrap").evaluate((element) => element.getBoundingClientRect().height);
  await rowSplitter.press("ArrowUp");
  const heightAfter = await page.locator(".status-console-wrap").evaluate((element) => element.getBoundingClientRect().height);
  expect(heightAfter).toBeGreaterThan(heightBefore);

  await loopConsole.getByRole("button", { name: "收合Loop 狀態紀錄" }).click();
  await expect(page.getByRole("button", { name: "展開Loop 狀態紀錄" })).toBeVisible();
  await page.getByRole("button", { name: "展開Loop 狀態紀錄" }).click();
  await agentConsole.getByRole("button", { name: "收合Agent 執行輸出" }).click();
  await expect(page.getByRole("button", { name: "展開Agent 執行輸出" })).toBeVisible();
  await page.getByRole("button", { name: "展開Agent 執行輸出" }).click();

  await page.getByRole("button", { name: "▶ 運行" }).click();
  await expect(page.getByRole("button", { name: "⏹ 立即停止" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Agent 執行輸出", exact: true })).toContainText("E2E fake agent started");
  await expect(page.locator(".chip.status-pulse").filter({ hasText: /^done / })).toBeVisible();
  await page.getByRole("button", { name: "⏹ 立即停止" }).click();
  await expect(page.getByRole("button", { name: "▶ 運行" })).toBeVisible();

  await page.getByRole("button", { name: "⚙ 設定" }).click();
  settings = page.getByRole("dialog", { name: "Workspace 設定" });
  await settings.getByLabel("done 收斂（≥）").fill("1");
  await settings.getByRole("button", { name: "儲存設定" }).click();
  await expect(settings).toBeHidden();

  await page.getByRole("button", { name: "▶ 運行" }).click();
  await expect(page.getByText("🏁 完成", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "▶ 運行" })).toBeVisible();
  await expect(page).toHaveTitle(/^🏁 e2e-workspace/);

  await page.getByRole("button", { name: "📄 完成報告" }).click();
  const reportModal = page.getByRole("dialog", { name: "完成報告" });
  await expect(reportModal).toBeVisible();
  await expect(reportModal).toContainText("loop-agent-lite RUN REPORT");
  await expect(reportModal).toContainText("task-1");
  await expect(reportModal).toContainText("task-2");
  await reportModal.getByRole("button", { name: "關閉對話框" }).click();
  await expect(reportModal).toBeHidden();

  await page.getByRole("button", { name: "📺 總覽" }).click();
  const finalOverview = page.getByRole("main", { name: "工作區總覽" });
  const finalFeed = finalOverview.getByRole("complementary", { name: "事件推播" });
  await expect(finalFeed.locator(".fleet-event", { hasText: "▶ 開始 task-2" }).first()).toBeVisible();
  await expect(finalFeed.locator(".fleet-event", { hasText: "▶ 開始 task-1" })).toHaveCount(2);
  await page.getByRole("button", { name: "📺 總覽" }).click();
  await expect(finalOverview).toBeHidden();

  await page.getByRole("button", { name: "🗑 刪除" }).click();
  const deleteDialog = page.getByRole("dialog", { name: "請確認" });
  await expect(deleteDialog).toContainText("無法復原");
  await expect(deleteDialog).toContainText("target repo");
  await deleteDialog.getByRole("button", { name: "永久刪除" }).click();
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toBeVisible();
});

test("parallel parent 經 UI 自動規劃、child grouping、graceful stop/resume 與群組刪除", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/");
  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  await launcher.getByLabel("Workspace 名稱 留空＝repo 目錄名").fill("e2e-parallel");
  await launcher.getByLabel("Parallel tracks（Agent 自動拆軌、worktree 隔離、CAS 合入）").check();
  await launcher.getByText("進階設定").click();
  await launcher.getByLabel("flag 收斂（>）").fill("1");
  await launcher.getByLabel("done 收斂（≥）").fill("1");
  await launcher.getByLabel("Child restart 上限 0＝不限").fill("2");
  await launcher.getByLabel("規劃收斂後暫停：不自動進入執行期，需回 Dashboard 按「▶ 運行」開始執行").check();
  const parallelDiff = launcher.locator(".launch-diff");
  await expect(parallelDiff).toContainText("integration ref refs/heads/");
  await expect(parallelDiff).toContainText("child restart ≤2");
  await launcher.getByRole("button", { name: "▶ 啟動" }).click();
  await expect(page.getByRole("heading", { name: "e2e-parallel" })).toBeVisible();
  await expect(page.locator(".workspace-title").getByText("等待核准", { exact: true })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "📺 總覽" }).click();
  const approvalStats = page.getByRole("main", { name: "工作區總覽" }).locator(".fleet-stat", { hasText: "規劃 / 執行 / 完成" });
  await expect(approvalStats.locator("strong")).toHaveText("1 / 0 / 0");
  await page.getByRole("button", { name: "📺 總覽" }).click();
  await page.getByRole("button", { name: "✎ 編輯計畫" }).click();
  const masterEditor = page.getByRole("dialog", { name: "Plan 編輯器" });
  await expect(masterEditor).toContainText("尚未建立 tracks，可編輯完整拆分");
  await masterEditor.getByLabel("任務內容").first().fill("建立 alpha.txt 並驗證 master plan 編輯");
  await masterEditor.getByRole("button", { name: "💾 儲存變更" }).click();
  await expect(masterEditor).toBeHidden();
  await expect(page.getByText("建立 alpha.txt 並驗證 master plan 編輯", { exact: true })).toBeVisible();
  let approvalRequest: Record<string, unknown> | null = null;
  let releaseApproval!: () => void;
  const approvalGate = new Promise<void>((resolve) => { releaseApproval = resolve; });
  const captureApproval = async (route: Route) => {
    approvalRequest = route.request().postDataJSON() as Record<string, unknown>;
    await approvalGate;
    await route.continue();
  };
  await page.route("**/api/run", captureApproval);
  const approvalResponse = page.waitForResponse((response) => response.url().endsWith("/api/run"));
  await page.getByRole("button", { name: "▶ 運行" }).click();
  await expect(page.getByRole("button", { name: "啟動中…" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "✎ 編輯計畫" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "⚙ 設定" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "🗑 刪除" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "📋 以此為範本啟動" })).toBeDisabled();
  releaseApproval();
  await approvalResponse;
  await page.unroute("**/api/run", captureApproval);
  expect(approvalRequest).toEqual(expect.objectContaining({
    run_id: expect.any(String),
    plan_generation: expect.any(Number),
    plan_sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
  }));
  const group = page.getByRole("region", { name: "Parallel run tracks" });
  await expect(group.locator(".parallel-track").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "📨 planning prompt" })).toBeVisible();
  // Bulk stop 必須把 fleet parent 呈現為 graceful，而不是誤稱「立即停止」。
  await page.getByRole("button", { name: "📺 總覽" }).click();
  const runningOverview = page.getByRole("main", { name: "工作區總覽" });
  await runningOverview.getByRole("button", { name: "☑ 批次操作" }).click();
  await runningOverview.getByLabel("批次選擇 workspace").selectOption("e2e-parallel");
  const gracefulBulkStop = runningOverview.getByRole("button", { name: "本輪後停止", exact: true });
  await expect(gracefulBulkStop).toBeVisible();
  let bulkStopRequest: Record<string, unknown> | null = null;
  let bulkStopRequestCount = 0;
  let releaseBulkStop!: () => void;
  const bulkStopGate = new Promise<void>((resolve) => { releaseBulkStop = resolve; });
  const captureBulkStop = async (route: Route) => {
    bulkStopRequestCount += 1;
    bulkStopRequest = route.request().postDataJSON() as Record<string, unknown>;
    await bulkStopGate;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, requested: true, graceful: true })
    });
  };
  await page.route("**/api/stop", captureBulkStop);
  await gracefulBulkStop.click();
  const bulkStopDialog = page.getByRole("dialog", { name: "確認批次操作" });
  await expect(bulkStopDialog).toContainText("active round 完整結束後停止");
  await expect(bulkStopDialog).toContainText("本輪後停止（Parallel）");
  await expect(bulkStopDialog).toContainText("e2e-parallel");
  const bulkStopResponse = page.waitForResponse((response) => response.url().endsWith("/api/stop"));
  await bulkStopDialog.getByRole("button", { name: "執行 1 個" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect(runningOverview.locator(".bulk-toolbar [role='status']")).toContainText("批次處理中");
  await expect(runningOverview.getByRole("button", { name: "☑ 批次操作" })).toBeDisabled();
  await expect(runningOverview.getByLabel("批次選擇 workspace")).toBeDisabled();
  await expect(gracefulBulkStop).toBeDisabled();
  await expect(page.getByRole("button", { name: "📺 總覽" })).toBeDisabled();
  await expect(runningOverview.locator(".fleet-card", { hasText: "e2e-parallel" })).toBeDisabled();
  await page.getByRole("button", { name: "📺 總覽" }).evaluate((button) => (button as HTMLButtonElement).click());
  await runningOverview.locator(".fleet-card", { hasText: "e2e-parallel" }).evaluate((button) => (button as HTMLButtonElement).click());
  await expect(runningOverview).toBeVisible();
  await page.waitForTimeout(100);
  expect(bulkStopRequestCount).toBe(1);
  releaseBulkStop();
  await bulkStopResponse;
  await expect(runningOverview.locator(".bulk-toolbar [role='status']")).toContainText("已處理 1/1");
  expect(bulkStopRequestCount).toBe(1);
  expect(bulkStopRequest).toEqual(expect.objectContaining({
    name: "e2e-parallel",
    run_id: expect.stringMatching(/^[0-9a-f]{32}$/),
    expected_pid: expect.any(Number),
  }));
  await page.unroute("**/api/stop", captureBulkStop);
  await page.getByRole("button", { name: "📺 總覽" }).click();
  await page.getByRole("button", { name: "＋ 啟動／管理" }).click();
  const jobs = page.getByRole("dialog", { name: "啟動與管理" });
  await jobs.getByRole("tab", { name: "執行中的 jobs" }).click();
  const fleetJob = jobs.locator(".job-card", { hasText: "e2e-parallel" });
  await expect(fleetJob).toContainText("Parallel fleet");
  const rejectStop = async (route: Route) => route.fulfill({
    status: 409,
    contentType: "application/json",
    body: JSON.stringify({ error: "E2E stale run identity" })
  });
  await page.route("**/api/stop", rejectStop);
  await fleetJob.getByRole("button", { name: "⏹ 停止" }).click();
  await expect(fleetJob.getByRole("status")).toContainText("E2E stale run identity");
  await expect(fleetJob.getByRole("button", { name: "⏹ 停止" })).toBeEnabled();
  await page.unroute("**/api/stop", rejectStop);
  await fleetJob.getByRole("button", { name: "⏹ 停止" }).click();
  await jobs.getByRole("button", { name: "關閉對話框" }).click();
  await expect(page.locator(".workspace-title").getByText("已停止", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("PID 殘留", { exact: false })).toHaveCount(0);

  const alphaTab = page.getByRole("tab", { name: "e2e-parallel track alpha" });
  await expect(alphaTab).toBeVisible();
  await page.getByRole("button", { name: "收合 e2e-parallel tracks" }).click();
  await expect(alphaTab).toBeHidden();
  await page.getByRole("button", { name: "展開 e2e-parallel tracks" }).click();
  await expect(alphaTab).toBeVisible();
  await alphaTab.click();
  await expect(page.getByRole("heading", { name: "e2e-parallel--alpha" })).toBeVisible();
  const breadcrumb = page.getByRole("navigation", { name: "Parallel run breadcrumb" });
  await expect(breadcrumb).toContainText("track alpha");
  await expect(page.getByRole("button", { name: /運行|立即停止|本輪後停止|編輯計畫|回規劃期|進執行期|設定|刪除|以此為範本/ })).toHaveCount(0);
  const childIssuesButton = page.getByRole("button", { name: /issues/ });
  await expect(childIssuesButton).toBeVisible();
  await childIssuesButton.click();
  const childIssues = page.getByRole("dialog", { name: "Issues" });
  await expect(childIssues.getByRole("button", { name: /標記已讀|清空全部/ })).toHaveCount(0);
  await childIssues.getByRole("button", { name: "關閉對話框" }).click();
  await page.getByRole("button", { name: "📨 prompt" }).click();
  await expect(page.getByRole("dialog", { name: "最近一輪 Prompt" })).toContainText("唯讀");
  await page.getByRole("dialog", { name: "最近一輪 Prompt" }).getByRole("button", { name: "關閉對話框" }).click();
  await page.getByRole("button", { name: "🕒 輪次紀錄" }).click();
  await expect(page.getByRole("dialog", { name: "輪次紀錄" })).toBeVisible();
  await page.getByRole("dialog", { name: "輪次紀錄" }).getByRole("button", { name: "關閉對話框" }).click();
  await breadcrumb.getByRole("button", { name: "e2e-parallel" }).click();
  await expect(page.getByRole("heading", { name: "e2e-parallel" })).toBeVisible();

  await page.getByRole("button", { name: "⚙ 設定" }).click();
  const parallelSettings = page.getByRole("dialog", { name: "Workspace 設定" });
  await expect(parallelSettings).toContainText("下一次 resume 生效");
  let parallelCliTestRequest: Record<string, unknown> | null = null;
  const captureParallelCliTest = async (route: Route) => {
    parallelCliTestRequest = route.request().postDataJSON() as Record<string, unknown>;
    await route.continue();
  };
  await page.route("**/api/test-cli", captureParallelCliTest);
  await parallelSettings.getByRole("button", { name: "管理 Agent CLI" }).click();
  const parallelCliManager = page.getByRole("dialog", { name: "Agent CLI 管理" });
  await parallelCliManager.getByRole("button", { name: "執行測試" }).first().click();
  const parallelCliResult = page.getByRole("dialog", { name: "Agent CLI 執行確認" });
  await expect(parallelCliResult.getByRole("status")).toContainText("E2E Agent CLI test result");
  expect(parallelCliTestRequest).toEqual(expect.objectContaining({
    name: "e2e-parallel",
    run_id: expect.stringMatching(/^[0-9a-f]{32}$/),
  }));
  await parallelCliResult.getByRole("button", { name: "關閉", exact: true }).click();
  await parallelCliManager.getByRole("button", { name: "取消" }).click();
  await page.unroute("**/api/test-cli", captureParallelCliTest);
  await parallelSettings.getByLabel("最大並行軌道").fill("3");
  await parallelSettings.getByLabel("Child restart 上限 0＝不限").fill("1");
  await parallelSettings.getByLabel("紅燈連跳 reset").fill("17");
  await parallelSettings.getByLabel("HEAD 停滯 reset").fill("222");
  await parallelSettings.getByRole("button", { name: "儲存設定" }).click();
  await expect(parallelSettings).toBeHidden();
  await expect(page.getByRole("button", { name: "⇄ Run 對比" })).toHaveCount(0);
  await page.getByRole("button", { name: "📋 以此為範本啟動" }).click();
  const parentTemplate = page.getByRole("dialog", { name: "啟動與管理" });
  await expect(parentTemplate.getByLabel("Parallel tracks（Agent 自動拆軌、worktree 隔離、CAS 合入）")).toBeChecked();
  await expect(parentTemplate.getByLabel("最大並行軌道")).toHaveValue("3");
  await expect(parentTemplate.getByLabel("Child restart 上限 0＝不限")).toHaveValue("1");
  await expect(parentTemplate.getByLabel("紅燈連跳 reset")).toHaveValue("17");
  await expect(parentTemplate.getByLabel("HEAD 停滯 reset")).toHaveValue("222");
  await parentTemplate.getByRole("button", { name: "取消", exact: true }).click();
  const removeTemplateAgent = async (route: Route) => {
    const response = await route.fetch();
    const payload = await response.json();
    await route.fulfill({ response, json: {
      ...payload,
      agent_cmds: [{ label: "Unavailable replacement", cmd: "false" }]
    } });
  };
  await page.route("**/api/config", removeTemplateAgent);
  await page.getByRole("button", { name: "📋 以此為範本啟動" }).click();
  const unavailableTemplate = page.getByRole("dialog", { name: "啟動與管理" });
  await expect(unavailableTemplate.getByRole("alert")).toContainText("Agent CLI 已不在目前清單");
  await expect(unavailableTemplate.getByRole("button", { name: "▶ 啟動" })).toBeDisabled();
  await unavailableTemplate.getByRole("button", { name: "取消", exact: true }).click();
  await page.unroute("**/api/config", removeTemplateAgent);
  await page.keyboard.press("ControlOrMeta+K");
  const parallelPalette = page.getByRole("dialog", { name: "快捷指令" });
  await parallelPalette.getByLabel("搜尋快捷指令").fill("alpha");
  await expect(parallelPalette.getByRole("option").first()).toContainText("alpha");
  await parallelPalette.getByRole("button", { name: "關閉對話框" }).click();

  await page.getByRole("button", { name: "📺 總覽" }).click();
  const parallelOverview = page.getByRole("main", { name: "工作區總覽" });
  await expect(parallelOverview.locator(".fleet-card")).toHaveCount(1);
  await expect(parallelOverview.locator(".fleet-card", { hasText: "e2e-parallel" })).toBeVisible();
  await expect(parallelOverview.locator(".fleet-card", { hasText: "e2e-parallel--alpha" })).toHaveCount(0);
  await page.getByRole("button", { name: "📺 總覽" }).click();

  const parentIssues = page.getByRole("button", { name: /issues/ });
  await expect(parentIssues).toBeVisible({ timeout: 30_000 });
  await parentIssues.click();
  const issuesModal = page.getByRole("dialog", { name: "Issues" });
  await expect(issuesModal.getByRole("button", { name: "標記已讀" })).toHaveCount(0);
  await issuesModal.getByRole("button", { name: "alpha", exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "e2e-parallel--alpha" })).toBeVisible();
  await page.getByRole("navigation", { name: "Parallel run breadcrumb" }).getByRole("button", { name: "e2e-parallel" }).click();

  // 啟動 handshake pending 時，其他 mutation 必須鎖住，避免只靠 backend 409 收尾。
  let releaseResume!: () => void;
  const resumeGate = new Promise<void>((resolve) => { releaseResume = resolve; });
  const delayResume = async (route: Route) => {
    await resumeGate;
    await route.continue();
  };
  await page.route("**/api/run", delayResume);
  const resumeResponse = page.waitForResponse((response) => response.url().endsWith("/api/run"));
  await page.getByRole("button", { name: "▶ 運行" }).click();
  await expect(page.getByRole("button", { name: "啟動中…" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "⚙ 設定" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "🗑 刪除" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "📋 以此為範本啟動" })).toBeDisabled();
  releaseResume();
  await resumeResponse;
  await page.unroute("**/api/run", delayResume);
  await expect(page.locator(".workspace-title").getByText("🏁 完成", { exact: true })).toBeVisible({ timeout: 60_000 });
  await expect(page).toHaveTitle(/^🏁 e2e-parallel/);
  await expect(page.getByRole("button", { name: "▶ 運行" })).toHaveCount(0);
  await expect(page.locator(".workspace-tab.active .status-dot")).toHaveClass(/phase-done/);
  await expect(page.getByRole("tab", { name: /e2e-parallel track/ })).toHaveCount(0);
  await page.getByRole("button", { name: /issues/ }).click();
  const cleanedIssues = page.getByRole("dialog", { name: "Issues" });
  await expect(cleanedIssues.getByRole("button", { name: "alpha", exact: true })).toHaveCount(0);
  await expect(cleanedIssues).toContainText("alpha（已清理）");
  await cleanedIssues.getByRole("button", { name: "關閉對話框" }).click();
  const completedGroup = page.getByRole("region", { name: "Parallel run tracks" });
  await expect(completedGroup.locator(".track-evidence")).toHaveCount(3);
  await completedGroup.locator(".track-evidence summary").first().click();
  await expect(completedGroup.locator(".track-evidence").first()).toContainText("sha256");
  await completedGroup.locator(".parallel-history summary").click();
  await expect(completedGroup.getByRole("list", { name: "Parallel phase history" })).toContainText("清理中");
  await expect(completedGroup.getByRole("list", { name: "Merge transaction history" })).toContainText("Integration 驗證通過");
  await page.getByRole("button", { name: "📄 完成報告" }).click();
  await expect(page.getByRole("dialog")).toContainText("Parallel Run Report");
  await expect(page.getByRole("dialog")).toContainText("Phase history");
  await page.getByRole("dialog").getByRole("button", { name: "關閉對話框" }).click();
  let releaseDelete!: () => void;
  const deleteGate = new Promise<void>((resolve) => { releaseDelete = resolve; });
  const delayDelete = async (route: Route) => {
    await deleteGate;
    await route.continue();
  };
  await page.route("**/api/delete-workspace", delayDelete);
  const deleteResponse = page.waitForResponse((response) => response.url().endsWith("/api/delete-workspace"));
  await page.getByRole("button", { name: "🗑 刪除" }).click();
  const parallelDelete = page.getByRole("dialog", { name: "請確認" });
  await expect(parallelDelete).toContainText("Run 身分");
  await expect(parallelDelete).toContainText("清理範圍");
  await parallelDelete.getByRole("button", { name: "永久刪除" }).click();
  await expect(page.getByRole("button", { name: "刪除中…" })).toBeDisabled();
  releaseDelete();
  await deleteResponse;
  await page.unroute("**/api/delete-workspace", delayDelete);
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toBeVisible();

  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const imported = page.getByRole("dialog", { name: "啟動與管理" });
  await imported.getByLabel("Workspace 名稱 留空＝repo 目錄名").fill("e2e-parallel-import");
  await imported.getByLabel("匯入 plan.json 選填").fill(JSON.stringify([
    { order: 1, task: "建立 alpha.txt；DoD: test -f alpha.txt", track: "alpha-long-track-name-24" },
    { order: 2, task: "建立 beta.txt；DoD: test -f beta.txt", track: "beta" },
    { order: 3, task: "建立 final.txt；DoD: test -f final.txt", track: "@final" }
  ]));
  await expect(imported.getByLabel("Parallel tracks（Agent 自動拆軌、worktree 隔離、CAS 合入）")).toBeChecked();
  await expect(imported.getByLabel("Parallel tracks（Agent 自動拆軌、worktree 隔離、CAS 合入）")).toBeDisabled();
  await expect(imported.getByLabel("直接執行期")).toBeChecked();
  await imported.getByText("進階設定").click();
  await imported.getByLabel("done 收斂（≥）").fill("1");
  await imported.getByRole("button", { name: "▶ 啟動" }).click();
  await expect(page.getByRole("heading", { name: "e2e-parallel-import" })).toBeVisible();
  await expect(page.locator(".workspace-title").getByText("🏁 完成", { exact: true })).toBeVisible({ timeout: 60_000 });
  await page.setViewportSize({ width: 800, height: 850 });
  expect(await page.evaluate(() => ({ viewport: window.innerWidth, document: document.documentElement.scrollWidth }))).toEqual({ viewport: 800, document: 800 });
  await expectCollapsedAgentConsoleHeight(page, 800);
  await page.setViewportSize({ width: 700, height: 850 });
  await expect(page.getByRole("region", { name: "Parallel run tracks" })).toContainText("alpha-long-track-name-24");
  await expectCollapsedAgentConsoleHeight(page, 700);
  // 版面專用極端 fixture：不改狀態語意，只驗證長 issue/console token 不會撐破窄版。
  const importedAgentConsole = page.getByRole("region", { name: "Agent 執行輸出", exact: true });
  await importedAgentConsole.locator(".console-output").evaluate((element) => {
    element.textContent = `LONG-CONSOLE-${"x".repeat(4_000)}`;
  });
  expect(await importedAgentConsole.locator(".console-output").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  const importedIssuesButton = page.getByRole("button", { name: /issues/ });
  await expect(importedIssuesButton).toBeVisible();
  await importedIssuesButton.click();
  const importedIssues = page.getByRole("dialog", { name: "Issues" });
  const issueContent = importedIssues.locator("tbody tr td:nth-child(4)").first();
  await issueContent.evaluate((element) => {
    element.textContent = `LONG-ISSUE-${"y".repeat(2_000)}`;
  });
  const issueBox = await issueContent.boundingBox();
  expect(issueBox?.width ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(700);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(700);
  await importedIssues.getByRole("button", { name: "關閉對話框" }).click();
  expect(await page.evaluate(() => ({ viewport: window.innerWidth, document: document.documentElement.scrollWidth }))).toEqual({ viewport: 700, document: 700 });
  await expect(page.getByRole("button", { name: "📄 完成報告" })).toBeInViewport();
});
