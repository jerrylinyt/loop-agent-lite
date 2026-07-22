/** Parallel dashboard recovery/control contracts that must remain stable across UI refactors. */
import { expect, test, type Page } from "@playwright/test";
import type { ParallelRunStatus, WorkspaceState, WorkspaceSummary } from "../src/shared/api/types";

const HEALTH = {
  schema_version: 1, status: "ok", workspace_count: 1, running: 1, attention: 0,
  error_count: 0, issues: 0, unread_issues: 0, agent_failures: 0,
  round_timeouts: 0, state_recoveries: 0, goal_changes: 0, stale_loop_pids: 0,
  generated_at: "2026-07-22T00:00:00Z",
};

function supervisorState(
  status: ParallelRunStatus,
  terminalIntent: "completed" | "cancelled" | null,
  error: string | null = null,
): WorkspaceState {
  return {
    runner: "parallel-supervisor", phase: "exec", round: 2, flag: 0,
    done_count: 0, red_streak: 0, stall_rounds: 0, plan_version: 1,
    current_order: 1, issues: [], config: { repo: "/repo" },
    plan: [{ order: 1, task: "contract task", stack: 1 }], completed: [],
    parallel: {
      run_id: "d".repeat(32), status, terminal_intent: terminalIntent, batch: 1, error,
      tasks: [{
        order: 1, batch: 1, outcome: "pending", resource_state: "queued",
        restart_count: 0, error: null,
      }],
    },
  };
}

async function mockShell(
  page: Page,
  workspace: WorkspaceSummary,
  readState: () => WorkspaceState,
): Promise<void> {
  await page.route("**/api/bootstrap", (route) => route.fulfill({
    json: { readonly: false, preselect: workspace.name },
  }));
  await page.route("**/api/workspaces", (route) => route.fulfill({ json: [workspace] }));
  await page.route("**/api/health", (route) => route.fulfill({ json: HEALTH }));
  await page.route("**/api/state?**", (route) => route.fulfill({ json: readState() }));
  await page.route("**/api/events?**", (route) => {
    const state = readState();
    return route.fulfill({
      contentType: "text/event-stream",
      body: `event: workspaces\ndata: ${JSON.stringify([workspace])}\n\nevent: state\ndata: ${JSON.stringify(state)}\n\n`,
    });
  });
}

test("Parallel controls follow the complete status and terminal-intent matrix", async ({ page }) => {
  const workspace: WorkspaceSummary = {
    name: "parallel-contract", runner: "parallel-supervisor", phase: "exec",
    running: true, round: 2, completed: 0, plan_len: 1, repo: "/repo",
    parallel: { run_id: "d".repeat(32), status: "running", batch: 1 },
  };
  let state = supervisorState("running", null);
  await mockShell(page, workspace, () => state);

  const cases: Array<{
    status: ParallelRunStatus;
    intent: "completed" | "cancelled" | null;
    pause: boolean;
    resume: boolean;
    abort: boolean;
    running?: boolean;
    pauseLabel?: string;
    resumeLabel?: string;
  }> = [
    { status: "initializing", intent: null, pause: true, resume: false, abort: true },
    { status: "running", intent: null, pause: true, resume: false, abort: true },
    { status: "pause_requested", intent: null, pause: true, resume: false, abort: true, pauseLabel: "重試 Pause" },
    { status: "paused", intent: null, pause: false, resume: true, abort: true },
    { status: "blocked", intent: null, pause: false, resume: true, abort: true },
    { status: "blocked", intent: "completed", pause: false, resume: true, abort: false, resumeLabel: "重試完成收尾" },
    { status: "blocked", intent: "cancelled", pause: false, resume: true, abort: false, resumeLabel: "重試取消清理" },
    { status: "cancel_requested", intent: "cancelled", pause: false, resume: false, abort: false },
    { status: "cancel_requested", intent: "cancelled", pause: false, resume: true, abort: false, running: false, resumeLabel: "重試取消清理" },
    { status: "finalizing", intent: "completed", pause: false, resume: false, abort: false, resumeLabel: "重試完成收尾" },
    { status: "finalizing", intent: "completed", pause: false, resume: true, abort: false, running: false, resumeLabel: "重試完成收尾" },
    { status: "finalizing_cancel", intent: "cancelled", pause: false, resume: false, abort: false },
    { status: "finalizing_cancel", intent: "cancelled", pause: false, resume: true, abort: false, running: false, resumeLabel: "重試取消清理" },
    { status: "completed", intent: "completed", pause: false, resume: false, abort: false },
    { status: "cancelled", intent: "cancelled", pause: false, resume: false, abort: false },
  ];

  for (const item of cases) {
    state = supervisorState(item.status, item.intent);
    workspace.running = item.running ?? true;
    await page.goto("/");
    await expect(page.getByRole("heading", { name: workspace.name })).toBeVisible();
    const pause = page.getByRole("button", { name: item.pauseLabel ?? "Pause", exact: true });
    const resume = page.getByRole("button", {
      name: item.resumeLabel ?? "Resume",
      exact: true,
    });
    const abort = page.getByRole("button", { name: "Abort", exact: true });
    await expect(pause)[item.pause ? "toBeEnabled" : "toBeDisabled"]();
    await expect(resume)[item.resume ? "toBeEnabled" : "toBeDisabled"]();
    await expect(abort)[item.abort ? "toBeEnabled" : "toBeDisabled"]();
  }
});

test("durable Parallel error remains visible when a recovery control also fails", async ({ page }) => {
  const workspace: WorkspaceSummary = {
    name: "parallel-errors", runner: "parallel-supervisor", phase: "exec",
    running: false, round: 2, completed: 0, plan_len: 1, repo: "/repo",
    parallel: { run_id: "e".repeat(32), status: "blocked", batch: 1 },
  };
  const state = supervisorState("blocked", null, "primary invariant mismatch");
  await mockShell(page, workspace, () => state);
  await page.route("**/api/resume", (route) => route.fulfill({ json: {
    ok: true, starting: true, name: workspace.name, pid: 901, job_id: "resume-error",
  } }));
  await page.route("**/api/job-startup?**", (route) => route.fulfill({ json: {
    status: "failed", pid: 901, rc: 3, error: "resume recovery failed",
  } }));

  await page.goto("/");
  await page.getByRole("button", { name: "Resume", exact: true }).click();
  await expect(page.getByText("primary invariant mismatch", { exact: true })).toBeVisible();
  await expect(page.getByText("錯誤：resume recovery failed", { exact: true })).toBeVisible();
});

test("terminal Parallel workspace can be deleted without touching the target repo", async ({ page }) => {
  const workspace: WorkspaceSummary = {
    name: "parallel-finished", runner: "parallel-supervisor", phase: "done",
    running: false, round: 2, completed: 1, plan_len: 1, repo: "/repo",
    parallel: { run_id: "8".repeat(32), status: "completed", batch: 1 },
  };
  const state = supervisorState("completed", "completed");
  state.phase = "done";
  let deletedName: string | undefined;
  await mockShell(page, workspace, () => state);
  await page.route("**/api/delete-workspace", async (route) => {
    deletedName = (await route.request().postDataJSON()).name;
    return route.fulfill({ json: { ok: true, deleted: true } });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "刪除", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "確認刪除 Parallel workspace" });
  await expect(dialog).toContainText("target repo 與已整合 commits 不受影響");
  await dialog.getByRole("button", { name: "永久刪除", exact: true }).click();
  await expect.poll(() => deletedName).toBe(workspace.name);
});

test("transition recovery controls use the idempotent backend routes", async ({ page }) => {
  const workspace: WorkspaceSummary = {
    name: "parallel-transition-recovery", runner: "parallel-supervisor", phase: "exec",
    running: false, round: 2, completed: 0, plan_len: 1, repo: "/repo",
    parallel: { run_id: "9".repeat(32), status: "pause_requested", batch: 1 },
  };
  let state = supervisorState("pause_requested", null);
  const calls: string[] = [];
  await mockShell(page, workspace, () => state);
  for (const [endpoint, action] of [["stop", "pause"], ["abort", "abort"], ["resume", "resume"]] as const) {
    await page.route(`**/api/${endpoint}`, (route) => {
      calls.push(action);
      return route.fulfill({ json: { ok: true } });
    });
  }

  await page.goto("/");
  await page.getByRole("button", { name: "重試 Pause", exact: true }).click();
  await expect.poll(() => calls).toEqual(["pause"]);

  state = supervisorState("finalizing_cancel", "cancelled");
  await page.goto("/");
  await page.getByRole("button", { name: "重試取消清理", exact: true }).click();
  await expect.poll(() => calls).toEqual(["pause", "resume"]);

  state = supervisorState("finalizing", "completed");
  await page.goto("/");
  await page.getByRole("button", { name: "重試完成收尾", exact: true }).click();
  await expect.poll(() => calls).toEqual(["pause", "resume", "resume"]);
});

test("managed_readonly is a fail-safe worker route even for legacy runner data", async ({ page }) => {
  const workspace: WorkspaceSummary = {
    name: "legacy-managed-worker", runner: "loop", managed_readonly: true,
    parent_workspace: "parallel-base", run_id: "f".repeat(32), assigned_order: 2,
    assignment: { status: "running" }, phase: "exec", running: true,
    round: 1, completed: 0, plan_len: 2, repo: "/worker",
  };
  const state: WorkspaceState = {
    runner: "loop", managed_readonly: true, parent_workspace: "parallel-base",
    run_id: "f".repeat(32), assigned_order: 2, assignment: { status: "running" },
    phase: "exec", round: 1, flag: 0, done_count: 0, red_streak: 0,
    stall_rounds: 0, plan_version: 1, completed: [],
    plan: [
      { order: 1, task: "not assigned", stack: 1 },
      { order: 2, task: "legacy assigned", stack: 1 },
    ],
  };
  await mockShell(page, workspace, () => state);

  await page.goto("/");
  await expect(page.getByText("Managed Worker", { exact: true })).toBeVisible();
  await expect(page.getByText("此 workspace 由 parent supervisor 管理，只提供唯讀狀態與 console。")).toBeVisible();
  const rows = page.locator(".parallel-task-panel tbody tr");
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText("legacy assigned");
  await expect(page.getByText("not assigned", { exact: true })).toHaveCount(0);
});

test("a launcher startup handshake cannot be dismissed before its durable result", async ({ page }) => {
  const workspace: WorkspaceSummary = {
    name: "parallel-existing", runner: "parallel-supervisor", phase: "exec",
    running: true, round: 2, completed: 0, plan_len: 1, repo: "/repo",
    parallel: { run_id: "a".repeat(32), status: "running", batch: 1 },
  };
  const state = supervisorState("running", null);
  let startupReady = false;
  await mockShell(page, workspace, () => state);
  await page.route("**/api/config", (route) => route.fulfill({ json: {
    agent_cmds: [{ label: "Agent", cmd: "agent --test" }],
    validate_cmds: [{ label: "Validate", cmd: "validate --test" }],
    repos: ["/repo"], defaults: {
      flag_threshold: 10, done_threshold: 3, round_timeout: 30,
      agent_backoff_max: 60, validate_timeout: 5, pause_after_plan: false,
      max_parallel: 2, worker_restart_limit: 3,
    },
  } }));
  await page.route("**/api/repo-status?**", (route) => route.fulfill({ json: {
    goal: "committed", tree_clean: true, branch: "main",
  } }));
  await page.route("**/api/launch", (route) => route.fulfill({ json: {
    ok: true, starting: true, name: "parallel-launched", pid: 920, startup_timeout: 5,
  } }));
  await page.route("**/api/job-startup?**", (route) => route.fulfill({ json: {
    status: startupReady ? "ready" : "starting", pid: 920,
  } }));

  await page.goto("/");
  await page.getByRole("button", { name: "＋ 啟動／管理" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  await launcher.getByRole("tab", { name: "Parallel Loop" }).click();
  await launcher.getByLabel("匯入 plan.json").fill(JSON.stringify([
    { order: 1, task: "handshake task", stack: 1 },
  ]));
  await launcher.getByLabel("Workspace 名稱").fill("parallel-launched");
  await launcher.getByRole("button", { name: "啟動", exact: true }).click();
  await expect(launcher.getByText("啟動前檢查中…", { exact: true })).toBeVisible();
  await expect(launcher.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "關閉對話框" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(launcher).toBeVisible();

  startupReady = true;
  await expect(launcher).toBeHidden();
});
