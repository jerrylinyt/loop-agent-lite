/** Parallel v1 UI contract: frozen-plan launcher, status table, and typed controls. */
import { expect, test } from "@playwright/test";
import type { WorkspaceState, WorkspaceSummary } from "../src/shared/api/types";

test("Parallel launcher and workspace use frozen plan plus Pause/Resume/Abort routes", async ({ page }) => {
  const workspace: WorkspaceSummary = {
    name: "parallel-base", runner: "parallel-supervisor", phase: "exec",
    running: true, round: 3, completed: 1, plan_len: 2, repo: "/repo",
    parallel: { run_id: "a".repeat(32), status: "running", batch: 1 },
  };
  let state: WorkspaceState = {
    runner: "parallel-supervisor", phase: "exec", round: 3, flag: 0,
    done_count: 0, red_streak: 0, stall_rounds: 0, plan_version: 1,
    current_order: 2, issues: [], config: { repo: "/repo" },
    plan: [
      { order: 1, task: "整合第一項", stack: 1 },
      { order: 2, task: "執行第二項", stack: 1 },
    ],
    completed: [{ order: 1, base_sha: "1".repeat(40), sha: "2".repeat(40), round: 2 }],
    parallel: {
      run_id: "a".repeat(32), status: "running" as const,
      terminal_intent: null, batch: 1, error: null,
      tasks: [
        { order: 1, batch: 1, outcome: "integrated", resource_state: "cleaned", restart_count: 0, error: null },
        { order: 2, batch: 1, outcome: "pending", resource_state: "running", restart_count: 1, error: null },
      ],
    },
  };
  const controls: string[] = [];
  const startupJobIds: string[] = [];
  let launchBody: Record<string, unknown> | null = null;
  let delayStateHydration = false;

  await page.route("**/api/bootstrap", (route) => route.fulfill({ json: { readonly: false, preselect: "parallel-base" } }));
  await page.route("**/api/workspaces", (route) => route.fulfill({ json: [workspace] }));
  await page.route("**/api/health", (route) => route.fulfill({ json: {
    schema_version: 1, status: "ok", workspace_count: 1, running: 1, attention: 0,
    error_count: 0, issues: 0, unread_issues: 0, agent_failures: 0,
    round_timeouts: 0, state_recoveries: 0, goal_changes: 0, stale_loop_pids: 0,
    generated_at: "2026-07-22T00:00:00Z",
  } }));
  await page.route("**/api/state?**", async (route) => {
    if (delayStateHydration) await new Promise((resolve) => setTimeout(resolve, 250));
    await route.fulfill({ json: state });
  });
  await page.route("**/api/events?**", (route) => route.fulfill({
    contentType: "text/event-stream",
    body: `event: workspaces\ndata: ${JSON.stringify([workspace])}\n\nevent: state\ndata: ${JSON.stringify(state)}\n\n`,
  }));
  await page.route("**/api/job-startup?**", (route) => {
    const jobId = new URL(route.request().url()).searchParams.get("job_id") ?? "";
    startupJobIds.push(jobId);
    if (jobId === "control-abort") {
      return route.fulfill({ json: {
        status: "failed", pid: 703, rc: 3, error: "Parallel control failed",
      } });
    }
    return route.fulfill({ json: { status: "ready", pid: 700 + startupJobIds.length } });
  });
  await page.route("**/api/stop", async (route) => {
    controls.push("pause");
    state = { ...state, parallel: { ...state.parallel, status: "paused" as const } };
    await route.fulfill({ json: {
      ok: true, starting: true, control: "pause", job_id: "control-pause", name: "parallel-base", pid: 701,
    } });
  });
  await page.route("**/api/resume", async (route) => {
    controls.push("resume");
    state = { ...state, parallel: { ...state.parallel, status: "running" as const } };
    await route.fulfill({ json: {
      ok: true, starting: true, job_id: "control-resume", name: "parallel-base", pid: 702,
    } });
  });
  await page.route("**/api/abort", async (route) => {
    controls.push("abort");
    await route.fulfill({ json: {
      ok: true, starting: true, control: "abort", job_id: "control-abort", name: "parallel-base", pid: 703,
    } });
  });
  await page.route("**/api/config", async (route) => {
    const response = await route.fetch();
    const config = await response.json();
    await route.fulfill({ response, json: {
      ...config,
      agent_cmds: [{ label: "Agent", cmd: "agent --test" }],
      validate_cmds: [{ label: "Validate", cmd: "validator --test" }],
      repos: ["/repo"], defaults: {
        ...config.defaults,
        flag_threshold: 10, done_threshold: 3, round_timeout: 30,
        agent_backoff_max: 60, validate_timeout: 120, pause_after_plan: false,
        max_parallel: 2, worker_restart_limit: 3,
      },
    } });
  });
  await page.route("**/api/repo-status?**", (route) => route.fulfill({ json: {
    goal: "committed", tree_clean: true, branch: "main",
  } }));
  await page.route("**/api/launch", async (route) => {
    launchBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({ json: { ok: true, name: "parallel-new", pid: 777 } });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "parallel-base" })).toBeVisible();
  await expect(page.getByText("Parallel", { exact: true })).toBeVisible();
  await expect(page.getByText("batch 1", { exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "執行第二項" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "running" })).toBeVisible();
  await expect(page.getByRole("button", { name: "查看 task-1 Git 變更 22222222" })).toBeVisible();

  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "已送出 Pause" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Resume", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Resume", exact: true }).click();
  await page.getByRole("button", { name: "Abort", exact: true }).click();
  const abortDialog = page.getByRole("dialog", { name: "確認 Abort Parallel run" });
  await expect(abortDialog).toBeVisible();
  await abortDialog.getByRole("button", { name: "Abort", exact: true }).click();
  await expect.poll(() => controls).toEqual(["pause", "resume", "abort"]);
  await expect.poll(() => startupJobIds).toEqual(["control-pause", "control-resume", "control-abort"]);
  await expect(page.getByRole("alert")).toContainText("Parallel control failed");

  // Keep the existing-workspace hydration in flight while the user types a
  // new name.  Its late response must not replace the explicit input.
  delayStateHydration = true;
  await page.getByRole("button", { name: "＋ 啟動／管理" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  await launcher.getByRole("tab", { name: "Parallel Loop" }).click();
  await expect(launcher.getByLabel("goal.md")).toBeDisabled();
  await expect(launcher.getByText("Parallel 固定從 exec 啟動。")).toHaveCount(0);
  await expect(launcher.getByTestId("parallel-plan-review-guidance")).toContainText("先產生基礎 Plan，再由人類標註 stack");
  await expect(launcher.getByRole("button", { name: "產生 Plan Prompt", exact: true })).toHaveCount(0);

  await launcher.getByRole("button", { name: "Goal 成果模板" }).click();
  const goalTemplate = page.getByRole("dialog", { name: "Goal 成果模板" });
  await expect(goalTemplate.getByTestId("parallel-goal-template-guidance")).toContainText("不要在 Goal 成果模板加入 stack");
  await goalTemplate.getByRole("button", { name: "上一頁", exact: false }).click();

  await launcher.getByRole("button", { name: "Goal 產生器 Prompt" }).click();
  let promptTemplates = page.getByRole("dialog", { name: "外部 Agent 產生器 Prompt" });
  await expect(promptTemplates.getByTestId("parallel-goal-prompt-guidance")).toContainText("Goal 只描述共享目標、限制與驗收");
  const promptPreview = promptTemplates.getByTestId("prompt-template-preview");
  await expect(promptPreview).not.toContainText("Parallel 基礎 Plan 準備契約");
  await promptTemplates.getByLabel(/同時產生初版 plan\.json/).check();
  await expect(promptTemplates.getByTestId("parallel-plan-prompt-guidance")).toContainText("只產生不含 stack 的基礎 plan");
  await expect(promptPreview).toContainText("不得自行推論、建議或輸出 `stack`");
  await promptTemplates.getByRole("button", { name: "上一頁", exact: false }).click();

  const basicPlanPrompt = launcher.getByRole("button", { name: "產生基礎 Plan Prompt（不含 stack）", exact: true });
  await expect(basicPlanPrompt).toBeEnabled();
  await basicPlanPrompt.click();
  promptTemplates = page.getByRole("dialog", { name: "外部 Agent 產生器 Prompt" });
  await expect(promptTemplates.getByRole("tab", { name: "基礎 Plan 拆分模板" })).toHaveAttribute("aria-selected", "true");
  await expect(promptTemplates.getByTestId("parallel-plan-prompt-guidance")).toContainText("人工檢查任務獨立性");
  await expect(promptTemplates.getByTestId("parallel-plan-prompt-guidance")).toContainText("人工加入相同的 stack 正整數");
  await expect(promptTemplates.getByTestId("prompt-template-preview")).toContainText("`stack` 必須由人類讀完任務邊界");
  await promptTemplates.getByRole("button", { name: "上一頁", exact: false }).click();

  const plan = launcher.getByLabel("匯入 plan.json");
  await plan.fill(JSON.stringify([
    { order: 1, task: "serial one" },
    { order: 2, task: "serial two" },
  ]));
  await expect(launcher.getByText("2 個 batch：#1 → #2")).toBeVisible();
  await expect(launcher.getByTestId("parallel-plan-concurrency-warning")).toContainText("所有任務會依序執行");
  await expect(launcher.getByRole("button", { name: "啟動", exact: true })).toBeEnabled();
  await plan.fill(JSON.stringify([
    { order: 1, task: "parallel one", stack: 1 },
    { order: 2, task: "parallel two", stack: 1 },
    { order: 3, task: "serial three" },
  ]));
  await expect(launcher.getByText("2 個 batch：stack 1 (#1–#2) → #3")).toBeVisible();
  await expect(launcher.getByTestId("parallel-plan-concurrency-warning")).toHaveCount(0);
  await launcher.getByLabel("Workspace 名稱").fill("parallel-new");
  await page.waitForTimeout(300);
  await expect(launcher.getByLabel("Workspace 名稱")).toHaveValue("parallel-new");
  delayStateHydration = false;
  await launcher.getByText("進階設定").click();
  await launcher.getByLabel("最大並行 workers").fill("2");
  await launcher.getByRole("button", { name: "啟動", exact: true }).click();
  await expect.poll(() => launchBody).not.toBeNull();
  expect(launchBody).toMatchObject({
    runner: "parallel", name: "parallel-new", start_phase: "exec",
    reset_state: false, new_branch: false, max_parallel: 2,
  });
});

test("Parallel blocked/error workspaces appear in the fleet attention filter", async ({ page }) => {
  const workspace: WorkspaceSummary = {
    name: "parallel-blocked", runner: "parallel-supervisor", phase: "exec",
    running: false, round: 4, completed: 0, plan_len: 2, repo: "/repo",
    parallel: {
      run_id: "b".repeat(32), status: "blocked", batch: 1,
      error: "worker restart limit reached",
    },
  };
  const state: WorkspaceState = {
    runner: "parallel-supervisor", phase: "exec", round: 4, flag: 0,
    done_count: 0, red_streak: 0, stall_rounds: 0, plan_version: 1,
    plan: [{ order: 1, task: "blocked task", stack: 1 }], completed: [],
    parallel: workspace.parallel,
  };

  await page.addInitScript(() => {
    localStorage.setItem("fleet-overview", "1");
    localStorage.setItem("fleet-filter", "attention");
  });
  await page.route("**/api/bootstrap", (route) => route.fulfill({ json: { readonly: false, preselect: workspace.name } }));
  await page.route("**/api/workspaces", (route) => route.fulfill({ json: [workspace] }));
  await page.route("**/api/health", (route) => route.fulfill({ json: {
    schema_version: 1, status: "degraded", workspace_count: 1, running: 0, attention: 1,
    error_count: 1, issues: 0, unread_issues: 0, agent_failures: 0,
    round_timeouts: 0, state_recoveries: 0, goal_changes: 0, stale_loop_pids: 0,
    generated_at: "2026-07-22T00:00:00Z",
  } }));
  await page.route("**/api/state?**", (route) => route.fulfill({ json: state }));
  await page.route("**/api/events?**", (route) => route.fulfill({
    contentType: "text/event-stream",
    body: `event: workspaces\ndata: ${JSON.stringify([workspace])}\n\nevent: state\ndata: ${JSON.stringify(state)}\n\n`,
  }));

  await page.goto("/");
  await expect(page.getByRole("main", { name: "工作區總覽" })).toBeVisible();
  await expect(page.getByRole("button", { name: /需關注 1/ })).toHaveAttribute("aria-pressed", "true");
  const card = page.locator(".fleet-card", { hasText: workspace.name });
  await expect(card).toBeVisible();
  await expect(card.getByText("Parallel blocked", { exact: true })).toBeVisible();
  await expect(card.getByText("Parallel：worker restart limit reached", { exact: true })).toBeVisible();
});

test("Launcher jobs awaits typed Pause jobs, reports failures, and ignores stale polls", async ({ page }) => {
  const stopNames: string[] = [];
  const startupJobIds: string[] = [];
  let jobs = [
    {
      id: "parallel-ok", kind: "parallel-supervisor", name: "parallel-ok",
      repo: "/repo-ok", pid: 801, alive: true, rc: null, tail: "supervisor running",
    },
    {
      id: "parallel-fail", kind: "parallel-supervisor", name: "parallel-fail",
      repo: "/repo-fail", pid: 802, alive: true, rc: null, tail: "supervisor running",
    },
    {
      id: "parallel-ok:abort:1234abcd", kind: "parallel-abort-control", name: "parallel-ok",
      repo: "/repo-ok", pid: 803, alive: true, rc: null, tail: "abort pending",
    },
    {
      id: "ordinary-loop", kind: "runner", name: "ordinary-loop",
      repo: "/repo-loop", pid: 804, alive: true, rc: null, tail: "loop running",
    },
  ];
  let jobsRequests = 0;
  let staleJobsRequested = false;
  let releaseStaleJobs!: () => void;
  const staleJobsGate = new Promise<void>((resolve) => { releaseStaleJobs = resolve; });
  let releasePauseSuccess!: () => void;
  const pauseSuccessGate = new Promise<void>((resolve) => { releasePauseSuccess = resolve; });

  await page.route("**/api/bootstrap", (route) => route.fulfill({ json: { readonly: false, preselect: "" } }));
  await page.route("**/api/workspaces", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/health", (route) => route.fulfill({ json: {
    schema_version: 1, status: "ok", workspace_count: 0, running: 0, attention: 0,
    error_count: 0, issues: 0, unread_issues: 0, agent_failures: 0,
    round_timeouts: 0, state_recoveries: 0, goal_changes: 0, stale_loop_pids: 0,
    generated_at: "2026-07-22T00:00:00Z",
  } }));
  await page.route("**/api/events?**", (route) => route.fulfill({
    contentType: "text/event-stream", body: "event: workspaces\ndata: []\n\n",
  }));
  await page.route("**/api/config", (route) => route.fulfill({ json: {
    agent_cmds: [], validate_cmds: [], repos: [], defaults: {},
  } }));
  await page.route("**/api/jobs", async (route) => {
    jobsRequests += 1;
    const snapshot = jobs.map((job) => ({ ...job }));
    if (jobsRequests === 2) {
      staleJobsRequested = true;
      await staleJobsGate;
    }
    await route.fulfill({ json: snapshot });
  });
  await page.route("**/api/stop", async (route) => {
    const name = (route.request().postDataJSON() as { name: string }).name;
    stopNames.push(name);
    if (name === "ordinary-loop") {
      jobs = jobs.map((job) => job.name === name ? { ...job, alive: false, rc: 0, tail: "stopped" } : job);
      return route.fulfill({ json: { ok: true, name } });
    }
    if (name === "parallel-ok") {
      jobs = jobs.map((job) => job.name === name && job.kind === "parallel-supervisor"
        ? { ...job, alive: false, rc: 0, tail: "paused" } : job);
      return route.fulfill({ json: {
        ok: true, starting: true, control: "pause", job_id: "control-pause-ok",
        name, pid: 811, startup_timeout: 5,
      } });
    }
    return route.fulfill({ json: {
      ok: true, starting: true, control: "pause", job_id: "control-pause-fail",
      name, pid: 812, startup_timeout: 5,
    } });
  });
  await page.route("**/api/job-startup?**", async (route) => {
    const jobId = new URL(route.request().url()).searchParams.get("job_id") ?? "";
    startupJobIds.push(jobId);
    if (jobId === "control-pause-ok") {
      await pauseSuccessGate;
      return route.fulfill({ json: { status: "ready", pid: 811, rc: 0 } });
    }
    return route.fulfill({ json: {
      status: "failed", pid: 812, rc: 3, error: "Pause control failed",
    } });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  await launcher.getByRole("tab", { name: "執行中的 jobs" }).click();
  const successSupervisor = launcher.locator(".job-card", { hasText: "parallel-ok" }).filter({ hasText: "parallel-supervisor" });
  const failedSupervisor = launcher.locator(".job-card", { hasText: "parallel-fail" });
  const control = launcher.locator(".job-card", { hasText: "parallel-abort-control" });
  const ordinary = launcher.locator(".job-card", { hasText: "ordinary-loop" });
  await expect(successSupervisor.getByRole("button", { name: "Pause" })).toBeVisible();
  await expect(failedSupervisor.getByRole("button", { name: "Pause" })).toBeVisible();
  await expect(control.getByRole("button")).toHaveCount(0);
  await expect(ordinary.getByRole("button", { name: "停止" })).toBeVisible();

  // Keep the periodic request in flight with the pre-action snapshot.  The
  // explicit refresh after Pause must win even when this older response lands last.
  await expect.poll(() => staleJobsRequested).toBe(true);
  await successSupervisor.getByRole("button", { name: "Pause" }).click();
  await expect(successSupervisor.getByRole("button", { name: "Pause 中…" })).toBeDisabled();
  await expect.poll(() => startupJobIds).toContain("control-pause-ok");
  await expect.poll(() => stopNames).toEqual(["parallel-ok"]);
  releasePauseSuccess();
  await expect(launcher.getByRole("status")).toContainText("parallel-ok Pause 完成");
  await expect(successSupervisor).toContainText("已結束 rc=0");
  releaseStaleJobs();
  await page.waitForTimeout(100);
  await expect(successSupervisor).toContainText("已結束 rc=0");

  await failedSupervisor.getByRole("button", { name: "Pause" }).click();
  await expect(launcher.getByRole("alert")).toContainText("parallel-fail Pause 失敗：Pause control failed");
  await expect.poll(() => stopNames).toEqual(["parallel-ok", "parallel-fail"]);
  await expect.poll(() => startupJobIds).toContain("control-pause-fail");

  const startupCallsBeforeOrdinaryStop = startupJobIds.length;
  await ordinary.getByRole("button", { name: "停止" }).click();
  await expect(launcher.getByRole("status")).toContainText("ordinary-loop 停止 完成");
  await expect.poll(() => stopNames).toEqual(["parallel-ok", "parallel-fail", "ordinary-loop"]);
  expect(startupJobIds).toHaveLength(startupCallsBeforeOrdinaryStop);
});

test("Fleet bulk stop awaits Parallel Pause and reports each failed control", async ({ page }) => {
  const parallelRunning: WorkspaceSummary = {
    name: "parallel-running", runner: "parallel-supervisor", phase: "exec",
    running: true, round: 2, completed: 0, plan_len: 2, repo: "/parallel-running",
    parallel: { run_id: "d".repeat(8), status: "running", batch: 1 },
  };
  const ordinary: WorkspaceSummary = {
    name: "ordinary-loop", runner: "loop", phase: "exec",
    running: true, round: 1, completed: 0, plan_len: 1, repo: "/ordinary",
  };
  const parallelFinalizing: WorkspaceSummary = {
    name: "parallel-finalizing", runner: "parallel-supervisor", phase: "exec",
    running: true, round: 3, completed: 2, plan_len: 2, repo: "/parallel-finalizing",
    parallel: { run_id: "e".repeat(8), status: "finalizing", batch: 1 },
  };
  const workspaces = [parallelRunning, ordinary, parallelFinalizing];
  const stopNames: string[] = [];
  const startupJobIds: string[] = [];
  let releasePauseFailure!: () => void;
  const pauseFailureGate = new Promise<void>((resolve) => { releasePauseFailure = resolve; });

  await page.addInitScript(() => {
    localStorage.setItem("fleet-overview", "1");
    localStorage.setItem("fleet-filter", "all");
  });
  await page.route("**/api/bootstrap", (route) => route.fulfill({ json: { readonly: false, preselect: parallelRunning.name } }));
  await page.route("**/api/workspaces", (route) => route.fulfill({ json: workspaces }));
  await page.route("**/api/health", (route) => route.fulfill({ json: {
    schema_version: 1, status: "ok", workspace_count: 3, running: 3, attention: 0,
    error_count: 0, issues: 0, unread_issues: 0, agent_failures: 0,
    round_timeouts: 0, state_recoveries: 0, goal_changes: 0, stale_loop_pids: 0,
    generated_at: "2026-07-22T00:00:00Z",
  } }));
  await page.route("**/api/events?**", (route) => route.fulfill({
    contentType: "text/event-stream",
    body: `event: workspaces\ndata: ${JSON.stringify(workspaces)}\n\n`,
  }));
  await page.route("**/api/stop", async (route) => {
    const name = (route.request().postDataJSON() as { name: string }).name;
    stopNames.push(name);
    if (name === ordinary.name) return route.fulfill({ json: { ok: true, name } });
    return route.fulfill({ json: {
      ok: true, starting: true, control: "pause", job_id: "fleet-pause-fail",
      name, pid: 901, startup_timeout: 5,
    } });
  });
  await page.route("**/api/job-startup?**", async (route) => {
    const jobId = new URL(route.request().url()).searchParams.get("job_id") ?? "";
    startupJobIds.push(jobId);
    await pauseFailureGate;
    await route.fulfill({ json: {
      status: "failed", pid: 901, rc: 3, error: "Fleet Pause control failed",
    } });
  });

  await page.goto("/");
  const overview = page.getByRole("main", { name: "工作區總覽" });
  await expect(overview).toBeVisible();
  await overview.getByRole("button", { name: "批次操作" }).click();
  await overview.getByLabel("批次選擇 workspace").selectOption(workspaces.map((workspace) => workspace.name));
  await overview.getByRole("button", { name: "停止 / Pause" }).click();
  const dialog = page.getByRole("dialog", { name: "確認批次操作" });
  await expect(dialog).toContainText("parallel-running（Pause）");
  await expect(dialog.locator(".action-preview > div", { hasText: "自動跳過" })).toContainText("parallel-finalizing（Parallel finalizing）");
  await dialog.getByRole("button", { name: "執行 2 個" }).click();
  await expect(overview.getByRole("button", { name: "批次操作" })).toBeDisabled();
  await expect(overview.locator(".bulk-toolbar").getByRole("status")).toContainText("處理中 0/2");
  await expect.poll(() => startupJobIds).toEqual(["fleet-pause-fail"]);
  releasePauseFailure();
  await expect(overview.locator(".bulk-toolbar").getByRole("alert")).toContainText("已處理 1/2 個 workspace");
  await expect(overview.locator(".bulk-toolbar").getByRole("alert")).toContainText("parallel-running（Pause）：Fleet Pause control failed");
  await expect.poll(() => stopNames).toEqual(["parallel-running", "ordinary-loop"]);
  expect(stopNames).not.toContain("parallel-finalizing");
});

test("Managed parallel worker only projects its assigned task", async ({ page }) => {
  const base: WorkspaceSummary = {
    name: "parallel-base", runner: "parallel-supervisor", phase: "exec",
    running: true, round: 2, completed: 0, plan_len: 3, repo: "/repo",
    parallel: { run_id: "c".repeat(8), status: "running", batch: 1 },
  };
  const workspace: WorkspaceSummary = {
    name: "parallel-base--cccccccc-task-2", runner: "parallel-worker", phase: "exec",
    running: true, round: 2, completed: 0, plan_len: 1, repo: "/worker-repo",
    managed_readonly: true, parent_workspace: base.name,
    run_id: "c".repeat(8), assigned_order: 2, assignment: { status: "running" },
  };
  const state: WorkspaceState = {
    runner: "parallel-worker", managed_readonly: true,
    parent_workspace: base.name, run_id: "c".repeat(8), assigned_order: 2,
    assignment: { status: "running" },
    phase: "exec", round: 2, flag: 0, done_count: 0,
    red_streak: 0, stall_rounds: 0, plan_version: 1,
    plan: [
      { order: 1, task: "another worker task", stack: 1 },
      { order: 2, task: "this worker assigned task", stack: 1 },
      { order: 3, task: "future serial task" },
    ],
    completed: [],
  };

  await page.route("**/api/bootstrap", (route) => route.fulfill({ json: { readonly: false, preselect: workspace.name } }));
  await page.route("**/api/workspaces", (route) => route.fulfill({ json: [base, workspace] }));
  await page.route("**/api/health", (route) => route.fulfill({ json: {
    schema_version: 1, status: "ok", workspace_count: 1, running: 1, attention: 0,
    error_count: 0, issues: 0, unread_issues: 0, agent_failures: 0,
    round_timeouts: 0, state_recoveries: 0, goal_changes: 0, stale_loop_pids: 0,
    generated_at: "2026-07-22T00:00:00Z",
  } }));
  await page.route("**/api/state?**", (route) => route.fulfill({ json: state }));
  await page.route("**/api/events?**", (route) => route.fulfill({
    contentType: "text/event-stream",
    body: `event: workspaces\ndata: ${JSON.stringify([base, workspace])}\n\nevent: state\ndata: ${JSON.stringify(state)}\n\n`,
  }));

  await page.goto("/");
  await expect(page.getByRole("heading", { name: workspace.name })).toBeVisible();
  await expect(page.getByRole("tab", { name: new RegExp(workspace.name) })).toBeVisible();
  const rows = page.locator(".parallel-task-panel tbody tr");
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText("this worker assigned task");
  await expect(page.getByText("another worker task", { exact: true })).toHaveCount(0);
  await expect(page.getByText("future serial task", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "總覽" }).click();
  await expect(page.locator(".fleet-card", { hasText: base.name })).toBeVisible();
  await expect(page.locator(".fleet-card", { hasText: workspace.name })).toHaveCount(0);
});

test("Parallel UI reaches real supervisor receipts, cleanup, and task diff", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/");
  await page.getByRole("button", { name: /啟動.*管理|啟動.*loop/ }).first().click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  await launcher.getByRole("tab", { name: "Parallel Loop" }).click();
  await launcher.getByLabel("匯入 plan.json").fill(JSON.stringify([
    { order: 1, task: "real parallel one", stack: 1 },
    { order: 2, task: "real parallel two", stack: 1 },
  ]));
  await launcher.getByLabel("Workspace 名稱").fill("parallel-real-e2e");
  await launcher.getByRole("combobox", { name: "Validate 命令" }).selectOption("1");
  await launcher.getByText("進階設定").click();
  await launcher.getByLabel("done 收斂（≥）").fill("1");
  await launcher.getByLabel("最大並行 workers").fill("2");
  await launcher.getByRole("button", { name: "啟動", exact: true }).click();

  await expect(launcher).toBeHidden({ timeout: 45_000 });
  await expect(page.getByRole("heading", { name: "parallel-real-e2e" })).toBeVisible();
  await expect(page.getByText("已完成", { exact: true })).toBeVisible({ timeout: 90_000 });
  const rows = page.locator(".parallel-task-table tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.filter({ hasText: "real parallel one" })).toContainText("cleaned");
  await expect(rows.filter({ hasText: "real parallel two" })).toContainText("cleaned");

  const stateResponse = await page.request.get("/api/state?ws=parallel-real-e2e");
  expect(stateResponse.ok()).toBeTruthy();
  const state = await stateResponse.json() as WorkspaceState;
  expect(state.parallel?.status).toBe("completed");
  expect(state.parallel?.tasks?.map((task) => task.resource_state)).toEqual([
    "cleaned", "cleaned",
  ]);
  expect(state.completed).toHaveLength(2);

  await rows.filter({ hasText: "real parallel one" }).getByRole("button", { name: /查看 task-1 Git 變更/ }).click();
  const diff = page.getByRole("dialog", { name: "task-1｜Git 變更" });
  await expect(diff).toContainText("parallel-e2e-task-1.txt");
});
