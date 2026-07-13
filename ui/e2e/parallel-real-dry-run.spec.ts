import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";

const scenario = process.env.LOOP_L4_SCENARIO;
const repo = process.env.LOOP_L4_REPO ?? "";
const validate = process.env.LOOP_L4_VALIDATE ?? "";
const validateTimeout = process.env.LOOP_L4_VALIDATE_TIMEOUT ?? "";
const importedPlan = process.env.LOOP_L4_PLAN ?? "";
const isExpectedLongRunNetworkSuspend = (message: string) =>
  message.trim() === "Failed to load resource: net::ERR_NETWORK_IO_SUSPENDED";

async function screenshot(page: Page, testInfo: TestInfo, name: string) {
  await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true });
}

async function closeModal(page: Page) {
  await page.getByRole("dialog").getByRole("button", { name: "關閉對話框" }).click();
}

async function waitForWorkspaceOrLaunchError(page: Page, workspaceName: string, timeoutMs: number) {
  const heading = page.getByRole("heading", { name: workspaceName });
  const launchStatus = page.getByRole("dialog", { name: "啟動與管理" }).locator(".inline-message");
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await heading.isVisible()) return;
    const status = (await launchStatus.textContent({ timeout: 500 }).catch(() => ""))?.trim() ?? "";
    if (status.startsWith("❌")) throw new Error(`L4 launch failed before workspace startup: ${status}`);
    await page.waitForTimeout(500);
  }
  throw new Error(`L4 workspace ${workspaceName} did not start within ${Math.round(timeoutMs / 1000)} seconds`);
}

async function terminalFleetFailure(page: Page) {
  const alerts = page.locator(".parallel-run-alert");
  for (let index = 0; index < await alerts.count(); index += 1) {
    const alert = alerts.nth(index);
    const title = (await alert.locator("strong").textContent())?.trim() ?? "";
    if (["Parallel run 已失敗", "Parallel truth 無法讀取"].includes(title)) {
      return (await alert.textContent())?.trim() ?? title;
    }
  }
  return "";
}

async function waitForVisibleOrFleetFailure(
  page: Page, target: Locator, label: string, timeoutMs: number
) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await target.isVisible()) return;
    const failure = await terminalFleetFailure(page);
    if (failure) throw new Error(`L4 ${label} aborted by terminal Fleet failure: ${failure}`);
    await page.waitForTimeout(500);
  }
  throw new Error(`L4 ${label} was not visible within ${Math.round(timeoutMs / 1000)} seconds`);
}

function collectPageErrors(page: Page) {
  const browserErrors: string[] = [];
  const networkErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !isExpectedLongRunNetworkSuspend(message.text())) browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("requestfailed", (request) => {
    const failure = request.failure()?.errorText ?? "unknown failure";
    if (request.resourceType() === "eventsource" && ["net::ERR_ABORTED", "net::ERR_NETWORK_IO_SUSPENDED"].includes(failure)) return;
    networkErrors.push(`${request.method()} ${request.url()} · ${failure}`);
  });
  page.on("response", (response) => {
    if (response.status() >= 400) networkErrors.push(`${response.status()} ${response.request().method()} ${response.url()}`);
  });
  return {
    assertEmpty() {
      expect(browserErrors, browserErrors.join("\n")).toEqual([]);
      expect(networkErrors, networkErrors.join("\n")).toEqual([]);
    }
  };
}

async function exerciseConsoleControls(page: Page) {
  const agentConsole = page.getByRole("region", { name: "Agent 執行輸出", exact: true });
  const loopConsole = page.getByRole("region", { name: "Loop 狀態紀錄", exact: true });
  await expect(agentConsole).toBeVisible();
  await expect(loopConsole).toBeVisible();

  await agentConsole.getByRole("button", { name: "全部", exact: true }).click();
  await expect(agentConsole.getByRole("button", { name: "全部", exact: true })).toHaveAttribute("aria-pressed", "true");
  const agentFilter = agentConsole.getByLabel("過濾Agent 執行輸出");
  await agentFilter.fill("__L4-NO-CONSOLE-MATCH__");
  await expect(agentConsole).toContainText("沒有符合過濾條件的行");
  await agentFilter.fill("");
  await agentConsole.getByRole("button", { name: "Agent", exact: true }).click();
  await expect(agentConsole.getByRole("button", { name: "Agent", exact: true })).toHaveAttribute("aria-pressed", "true");

  const loopFilter = loopConsole.getByLabel("過濾Loop 狀態紀錄");
  await loopFilter.fill("__L4-NO-CONSOLE-MATCH__");
  await expect(loopConsole).toContainText("沒有符合過濾條件的行");
  await loopFilter.fill("");

  await loopConsole.getByRole("button", { name: "收合Loop 狀態紀錄" }).click();
  await page.getByRole("button", { name: "展開Loop 狀態紀錄" }).click();
  await agentConsole.getByRole("button", { name: "收合Agent 執行輸出" }).click();
  await page.getByRole("button", { name: "展開Agent 執行輸出" }).click();
}

async function assertCollapsedAgentConsoleFits(page: Page, width: 700 | 800) {
  await page.setViewportSize({ width, height: width === 700 ? 850 : 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
  const agentConsole = page.getByRole("region", { name: "Agent 執行輸出", exact: true });
  await agentConsole.getByRole("button", { name: "收合Agent 執行輸出" }).click();
  const collapsed = page.getByRole("region", { name: "Agent 執行輸出（已收合）" });
  const box = await collapsed.boundingBox();
  expect(box, `${width}px collapsed Agent console must have a bounding box`).not.toBeNull();
  expect(box?.height ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(44);
  expect(box?.width ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(width);
  await collapsed.getByRole("button", { name: "展開Agent 執行輸出" }).click();
}

async function inspectGoal(page: Page) {
  await page.getByRole("button", { name: "🎯 goal" }).click();
  const goal = page.getByRole("dialog", { name: "Goal" });
  await expect(goal.locator(".report-content")).toBeVisible({ timeout: 30_000 });
  await closeModal(page);
}

async function inspectTimeline(page: Page, workspace: string) {
  await page.getByRole("button", { name: "🧭 時間軸" }).click();
  const timeline = page.getByRole("dialog", { name: `${workspace}｜統一時間軸` });
  await expect(timeline).toBeVisible();
  await timeline.getByRole("button", { name: "輪次", exact: true }).click();
  await expect(timeline.getByRole("button", { name: "輪次", exact: true })).toHaveAttribute("aria-pressed", "true");
  const search = timeline.getByLabel("搜尋時間軸");
  await search.fill("__L4-NO-TIMELINE-MATCH__");
  await expect(timeline).toContainText("沒有符合條件的時間軸事件");
  await search.fill("");
  await timeline.getByRole("button", { name: "全部", exact: true }).click();
  await closeModal(page);
}

async function inspectChildPromptAndHistory(page: Page) {
  await page.getByRole("button", { name: "📨 prompt", exact: true }).click();
  const prompt = page.getByRole("dialog", { name: "最近一輪 Prompt" });
  await expect(prompt.locator(".report-content")).not.toBeEmpty();
  await expect(prompt).toContainText(/fleet|track|並行軌道/i);
  await closeModal(page);

  await page.getByRole("button", { name: "🕒 輪次紀錄", exact: true }).click();
  const history = page.getByRole("dialog", { name: "輪次紀錄" });
  await expect(history).toBeVisible();
  await expect(history.locator("tbody tr").first()).not.toContainText("尚無輪次紀錄");
  await expect(history).toContainText(/exec|merge|執行|整合/i);
  await closeModal(page);
}

test("full-project parallel run through production UI", async ({ page }, testInfo) => {
  test.skip(process.env.LOOP_L4_DELETE_PHASE === "1", "delete phase only");
  if (!scenario || !repo || !validate || !validateTimeout) throw new Error("L4 environment is incomplete");
  const validateTimeoutSeconds = Number(validateTimeout);
  if (!Number.isFinite(validateTimeoutSeconds) || validateTimeoutSeconds <= 0) {
    throw new Error(`invalid LOOP_L4_VALIDATE_TIMEOUT: ${validateTimeout}`);
  }
  const pageErrors = collectPageErrors(page);

  await page.goto("/");
  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });
  await launcher.getByRole("combobox", { name: "Repo" }).selectOption(repo);
  await launcher.getByLabel("Workspace 名稱 留空＝repo 目錄名").fill(`l4-${scenario}`);
  await launcher.getByLabel("Parallel tracks（Agent 自動拆軌、worktree 隔離、CAS 合入）").check();
  await expect(launcher.getByLabel("最大並行軌道")).toHaveValue("4");
  await launcher.getByRole("combobox", { name: "Agent 命令" }).selectOption("0");
  await expect(launcher.getByRole("combobox", { name: "Agent 命令" }).locator("option:checked")).toContainText("codex");
  await launcher.getByRole("combobox", { name: "Validate 命令" }).selectOption("__custom__");
  await launcher.getByLabel("自訂 Validate 命令").fill(validate);
  await launcher.getByText("進階設定").click();
  await expect(launcher.getByLabel("Validate 上限（秒）")).toHaveValue(validateTimeout);
  if (scenario === "dr1") {
    await launcher.getByLabel(/規劃收斂後暫停/).check();
  }
  const launchDiff = launcher.locator(".launch-diff");
  await expect(launchDiff).toContainText("integration ref refs/heads/");
  await expect(launchDiff).toContainText("Parallel run · 最多 4 軌");
  if (scenario === "dr2") {
    await launcher.getByLabel("匯入 plan.json 選填").fill(importedPlan);
    await launcher.getByLabel("直接執行期").check();
  }
  await screenshot(page, testInfo, "01-launcher-ready");
  await launcher.getByRole("button", { name: "▶ 啟動" }).click();
  // Startup handshake 只證明 coordinator truth 已落盤；baseline failure 由後續 Fleet wait fail-fast。
  await waitForWorkspaceOrLaunchError(
    page, `l4-${scenario}`, (validateTimeoutSeconds + 30) * 1000
  );
  if (scenario === "dr1") {
    await waitForVisibleOrFleetFailure(
      page, page.locator(".workspace-title").getByText("等待核准", { exact: true }),
      "planning approval", 30 * 60 * 1000
    );
    await screenshot(page, testInfo, "02-awaiting-approval");
    await page.getByRole("button", { name: "✎ 編輯計畫" }).click();
    const planEditor = page.getByRole("dialog", { name: "Plan 編輯器" });
    await expect(planEditor).toContainText("尚未建立 tracks，可編輯完整拆分");
    const firstTask = planEditor.getByLabel("任務內容").first();
    const originalTask = await firstTask.inputValue();
    const clarifiedTask = `${originalTask}（僅補充說明：原驗收條件不變）`;
    await firstTask.fill(clarifiedTask);
    await planEditor.getByRole("button", { name: "💾 儲存變更" }).click();
    await expect(planEditor).toBeHidden();
    await expect(page.getByText(clarifiedTask, { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "▶ 運行" }).click();
  }
  let group = page.getByRole("region", { name: "Parallel run tracks" });
  await expect(group).toBeVisible();
  await waitForVisibleOrFleetFailure(
    page, group.locator(".parallel-track").first(), "track creation", 30 * 60 * 1000
  );
  if (scenario === "dr1") {
    await expect(group.locator(".parallel-run-head")).toHaveAttribute(
      "aria-label", /^Parallel tracks \d+\/\d+ merged$/
    );
  }
  await screenshot(page, testInfo, "03-tracks-created");

  // child 活躍期間實際操作 parent/child 診斷視圖與 console；track tabs 也必須可收合後復原。
  await inspectGoal(page);
  await exerciseConsoleControls(page);
  const childTab = page.locator(".workspace-tab-child").first();
  await waitForVisibleOrFleetFailure(page, childTab, "child registration", 30 * 60 * 1000);
  const collapseTracks = page.getByRole("button", { name: `收合 l4-${scenario} tracks` });
  await collapseTracks.click();
  await expect(page.getByRole("button", { name: `展開 l4-${scenario} tracks` })).toHaveAttribute("aria-expanded", "false");
  await expect(childTab).toBeHidden();
  const expandTracks = page.getByRole("button", { name: `展開 l4-${scenario} tracks` });
  await expandTracks.click();
  await expect(page.getByRole("button", { name: `收合 l4-${scenario} tracks` })).toHaveAttribute("aria-expanded", "true");
  await expect(childTab).toBeVisible();
  const activeChildTab = page.locator(".workspace-tab-child").filter({ has: page.locator('[aria-label="執行中"]') }).first();
  await waitForVisibleOrFleetFailure(page, activeChildTab, "active child", 30 * 60 * 1000);
  await activeChildTab.click();
  const breadcrumb = page.getByRole("navigation", { name: "Parallel run breadcrumb" });
  await expect(breadcrumb).toBeVisible();
  const childWorkspace = (await page.locator(".workspace-title h1").textContent())?.trim() ?? "";
  expect(childWorkspace).toContain(`l4-${scenario}`);
  await expect(page.getByRole("button", { name: /運行|立即停止|本輪後停止|編輯計畫|回規劃期|進執行期|設定|刪除|以此為範本/ })).toHaveCount(0);
  await inspectGoal(page);
  await inspectChildPromptAndHistory(page);
  await inspectTimeline(page, childWorkspace);
  await exerciseConsoleControls(page);
  // 用 palette 做一次真正的 child → parent 導航，避免之後 cleanup 已移除 child 時再依賴舊 tab。
  await page.keyboard.press("ControlOrMeta+K");
  const childPalette = page.getByRole("dialog", { name: "快捷指令" });
  await childPalette.getByLabel("搜尋快捷指令").fill(`l4-${scenario}`);
  const parentOption = childPalette.getByRole("option").filter({
    has: page.getByText(`l4-${scenario}`, { exact: true })
  }).first();
  await expect(parentOption).toBeVisible();
  await parentOption.click();
  await expect(page.getByRole("heading", { name: `l4-${scenario}` })).toBeVisible();
  await expect(page.getByRole("button", { name: "⚙ 設定" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "⇄ Run 對比" })).toHaveCount(0);

  if (scenario === "dr1") {
    const stop = page.getByRole("button", { name: "⏸ 本輪後停止" });
    await waitForVisibleOrFleetFailure(page, stop, "graceful-stop action", 30 * 60 * 1000);
    await stop.click();
    await waitForVisibleOrFleetFailure(
      page, page.locator(".workspace-title").getByText("已停止", { exact: true }),
      "graceful stop", 40 * 60 * 1000
    );
    await expect(page.getByText("PID 殘留", { exact: false })).toHaveCount(0);
    group = page.getByRole("region", { name: "Parallel run tracks" });
    await expect(group.locator(".parallel-track.status-running")).toHaveCount(0);
    await expect(group.locator(".parallel-track.status-stopped").first()).toBeVisible();
    await expect(group.getByText(/^pid \d+$/)).toHaveCount(0);
    await screenshot(page, testInfo, "06-graceful-stopped");
    await page.getByRole("button", { name: "⚙ 設定" }).click();
    let settings = page.getByRole("dialog", { name: "Workspace 設定" });
    await expect(settings).toContainText("下一次 resume 生效");
    await settings.getByLabel("Child restart 上限 0＝不限").fill("1");
    let stateRefresh = page.waitForResponse((response) => response.ok() && response.url().includes(`/api/state?ws=${encodeURIComponent(`l4-${scenario}`)}`));
    await settings.getByRole("button", { name: "儲存設定" }).click();
    await stateRefresh;
    await expect(settings).toBeHidden();
    await page.getByRole("button", { name: "⚙ 設定" }).click();
    settings = page.getByRole("dialog", { name: "Workspace 設定" });
    await expect(settings.getByLabel("Child restart 上限 0＝不限")).toHaveValue("1");
    await settings.getByLabel("Child restart 上限 0＝不限").fill("0");
    stateRefresh = page.waitForResponse((response) => response.ok() && response.url().includes(`/api/state?ws=${encodeURIComponent(`l4-${scenario}`)}`));
    await settings.getByRole("button", { name: "儲存設定" }).click();
    await stateRefresh;
    await expect(settings).toBeHidden();
    await page.getByRole("button", { name: "⚙ 設定" }).click();
    settings = page.getByRole("dialog", { name: "Workspace 設定" });
    await expect(settings.getByLabel("Child restart 上限 0＝不限")).toHaveValue("0");
    await settings.getByRole("button", { name: "取消" }).click();

    await page.getByRole("button", { name: "📋 以此為範本啟動" }).click();
    const template = page.getByRole("dialog", { name: "啟動與管理" });
    await expect(template.getByRole("combobox", { name: "Repo" })).toHaveValue(repo);
    await expect(template.getByRole("combobox", { name: "Agent 命令" }).locator("option:checked")).toContainText("codex");
    const templateValidate = template.getByRole("combobox", { name: "Validate 命令" });
    if (await templateValidate.inputValue() === "__custom__") {
      await expect(template.getByLabel("自訂 Validate 命令")).toHaveValue(validate);
    } else {
      await expect(templateValidate.locator("option:checked")).toContainText(validate);
    }
    await expect(template.getByLabel("Parallel tracks（Agent 自動拆軌、worktree 隔離、CAS 合入）")).toBeChecked();
    await expect(template.getByLabel("最大並行軌道")).toHaveValue("4");
    await expect(template.getByLabel("Child restart 上限 0＝不限")).toHaveValue("0");
    await template.getByRole("button", { name: "取消", exact: true }).click();
    await page.getByRole("button", { name: "▶ 運行" }).click();
    await waitForVisibleOrFleetFailure(page, group.locator(".track-status-history").filter({
      hasText: /執行中.*已停止.*執行中/
    }).first(), "stop/resume history", 30 * 60 * 1000);
  } else {
    await waitForVisibleOrFleetFailure(
      page, group.getByText(/rollback [1-9]/).first(), "rollback evidence", 3 * 60 * 60 * 1000
    );
    const failedTrack = group.locator(".parallel-track").filter({ has: group.locator(".track-error-summary") }).first();
    await expect(failedTrack.locator(".track-error-summary")).toBeVisible();
    const failedTrackName = (await failedTrack.locator("strong").first().innerText()).trim();
    // 真 integration validator/rollback projection 才是診斷依據；不再用 agent prompt sentinel 當機械檢核。
    const issueButton = page.getByRole("button", { name: /issues/ });
    await expect(issueButton).toBeVisible();
    await issueButton.click();
    const issues = page.getByRole("dialog", { name: "Issues" });
    const integrationIssue = issues.getByRole("row").filter({ hasText: failedTrackName }).filter({
      hasText: /validator|rollback|integration/i
    }).first();
    await expect(integrationIssue).toBeVisible();
    const issueTrackLink = integrationIssue.getByRole("button", { name: failedTrackName, exact: true });
    if (await issueTrackLink.count()) {
      await issueTrackLink.click();
      await expect(page.getByRole("navigation", { name: "Parallel run breadcrumb" })).toBeVisible();
      await page.getByRole("navigation", { name: "Parallel run breadcrumb" }).getByRole("button", { name: `l4-${scenario}` }).click();
      await expect(page.getByRole("heading", { name: `l4-${scenario}` })).toBeVisible();
    } else {
      // cleanup 若恰好先完成，來源保留為不可點擊的 evidence 診斷，不留下死 child 連結。
      await expect(integrationIssue).toContainText("（已清理）");
      await closeModal(page);
    }
    await expect(page.locator(".workspace-title").getByText("🏁 完成", { exact: true })).toHaveCount(0);
    await screenshot(page, testInfo, "06-rollback-repair");
  }

  // 先完成 DR1 graceful stop/resume 或 DR2 rollback 驗收，再做較慢的 overview/窄版操作，
  // 避免 active child 在 UI 檢查期間自然完成，造成 stop race。
  await page.getByRole("button", { name: "📺 總覽" }).click();
  const overview = page.getByRole("main", { name: "工作區總覽" });
  await expect(overview.locator(".fleet-card")).toHaveCount(1);
  await expect(overview.locator(".fleet-card", { hasText: `l4-${scenario}` })).toBeVisible();
  const search = overview.getByRole("searchbox", { name: "搜尋 workspace" });
  await search.fill(`l4-${scenario}`);
  await overview.getByLabel("Workspace 排序").selectOption("progress");
  await search.fill("");
  await page.getByRole("button", { name: "📺 總覽" }).click();

  await assertCollapsedAgentConsoleFits(page, 800);
  await inspectGoal(page);
  await screenshot(page, testInfo, "04-viewport-800");
  await assertCollapsedAgentConsoleFits(page, 700);
  await page.keyboard.press("ControlOrMeta+K");
  const narrowPalette = page.getByRole("dialog", { name: "快捷指令" });
  await narrowPalette.getByLabel("搜尋快捷指令").fill(`l4-${scenario}`);
  await narrowPalette.getByRole("option").filter({
    has: page.getByText(`l4-${scenario}`, { exact: true })
  }).first().click();
  await expect(page.getByRole("heading", { name: `l4-${scenario}` })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(701);
  await screenshot(page, testInfo, "05-viewport-700");
  await page.setViewportSize({ width: 1280, height: 900 });
  const faviconHref = await page.evaluate(() => document.querySelector('link[rel="icon"]')?.getAttribute("href") ?? "");
  expect(faviconHref.startsWith("data:image/png")).toBeTruthy();

  await waitForVisibleOrFleetFailure(
    page, page.locator(".workspace-title").getByText("🏁 完成", { exact: true }),
    "Fleet completion", 4 * 60 * 60 * 1000
  );
  await screenshot(page, testInfo, "07-done");
  await page.getByRole("button", { name: "📄 完成報告" }).click();
  const report = page.getByRole("dialog", { name: "完成報告" });
  await expect(report).toContainText("Parallel Run Report");
  await expect(report).toContainText("Phase history");
  await closeModal(page);

  // 只有 DR1 實際執行 parent planning rounds，因此才有 planning prompt/history/timeline。
  // DR2 是 imported-plan round 0，改由下方每軌 event/evidence 與 integration issue 做 cleanup 後診斷。
  if (scenario === "dr1") {
    await page.getByRole("button", { name: "📨 planning prompt" }).click();
    await expect(page.getByRole("dialog", { name: "最近一輪 Prompt" })).toBeVisible();
    await closeModal(page);
    await page.getByRole("button", { name: "🕒 輪次紀錄" }).click();
    await expect(page.getByRole("dialog", { name: "輪次紀錄" })).toBeVisible();
    await closeModal(page);
    await inspectTimeline(page, `l4-${scenario}`);
  }
  const completedIssuesButton = page.getByRole("button", { name: /issues/ });
  if (scenario === "dr2") {
    await expect(completedIssuesButton).toBeVisible();
    await completedIssuesButton.click();
    const completedIssues = page.getByRole("dialog", { name: "Issues" });
    await expect(completedIssues).toBeVisible();
    const integrationIssue = completedIssues.getByRole("row").filter({
      hasText: "fleet-integration-rollback"
    }).first();
    await expect(integrationIssue).toContainText("已修復");
    await expect(integrationIssue).toContainText("（已清理）");
    await expect(integrationIssue.getByRole("button")).toHaveCount(0);
    await closeModal(page);
  }

  // 完成後仍可由 UI 稽核所有短暫 phase / CAS stage，不依賴 timing 撞瞬時狀態。
  group = page.getByRole("region", { name: "Parallel run tracks" });
  const completedTracks = group.locator(".parallel-track");
  const completedTrackCount = await completedTracks.count();
  expect(completedTrackCount).toBeGreaterThan(0);
  let tracksWithSync = 0;
  for (let index = 0; index < completedTrackCount; index += 1) {
    const track = completedTracks.nth(index);
    const trackName = (await track.locator("strong").first().innerText()).trim();
    await expect(track.locator(".track-status-history"), `${trackName} status history`).toContainText("已合併");
    await expect(track.locator(".track-status-history"), `${trackName} cleanup status`).toContainText("已清理");
    await track.locator(".track-evidence summary").click();
    await expect(track.locator(".track-evidence code"), `${trackName} cleanup evidence path`).not.toHaveText("");
    await expect(track.locator(".track-evidence"), `${trackName} cleanup evidence hash`).toContainText("sha256");
    await track.locator(".track-event-history summary").click();
    const eventHistory = track.getByRole("list", { name: `${trackName} track event history` });
    const eventNames = await eventHistory.locator("code").allTextContents();
    for (const required of ["queued", "merge-prepared", "merged", "cleanup-evidence-captured", "cleaned"]) {
      expect(eventNames, `${trackName} missing event ${required}`).toContain(required);
    }
    await expect(eventHistory, `${trackName} child merge confirm history`).toContainText("merge confirm");
    if ((await eventHistory.innerText()).includes("merge sync")) tracksWithSync += 1;
  }
  expect(tracksWithSync, "at least one track lagging integration must execute merge sync").toBeGreaterThanOrEqual(1);
  await expect(completedTracks.filter({ hasText: "@final" })).toHaveCount(1);
  await group.locator(".parallel-history summary").click();
  const phaseHistory = group.getByRole("list", { name: "Parallel phase history" });
  const phaseText = await phaseHistory.innerText();
  const ordered = ["規劃中", "建立軌道中", "平行執行中", "整合中", "最終驗收中", "清理中", "🏁 完成"];
  let previousIndex = -1;
  for (const label of ordered) {
    const index = phaseText.indexOf(label);
    expect(index, `missing/out-of-order phase ${label}\n${phaseText}`).toBeGreaterThan(previousIndex);
    previousIndex = index;
  }
  const mergeHistory = group.getByRole("list", { name: "Merge transaction history" });
  await expect(mergeHistory).toContainText("CAS 準備");
  await expect(mergeHistory).toContainText("Integration 驗證中");
  if (scenario === "dr2") {
    await expect(mergeHistory).toContainText("Rollback 準備");
    await expect(mergeHistory).toContainText("Rollback 已完成");
    await expect(group.locator(".track-status-history").filter({ hasText: "修復中" }).first()).toBeVisible();
  }

  // reload 建立新 SSE 後仍保持 done，不得被舊 generation 倒退。
  await page.reload();
  await expect(page.getByRole("heading", { name: `l4-${scenario}` })).toBeVisible();
  await expect(page.locator(".workspace-title").getByText("🏁 完成", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "📄 完成報告" }).click();
  await expect(page.getByRole("dialog", { name: "完成報告" })).toContainText("Parallel Run Report");
  await closeModal(page);

  await page.setViewportSize({ width: 700, height: 850 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(701);
  await expect(page.getByRole("button", { name: "📄 完成報告" })).toBeInViewport();
  await page.getByRole("button", { name: "📄 完成報告" }).click();
  await expect(page.getByRole("dialog", { name: "完成報告" })).toContainText("Parallel Run Report");
  await closeModal(page);
  await screenshot(page, testInfo, "08-done-narrow-viewport");
  pageErrors.assertEmpty();
});

test("production writable UI group delete", async ({ page }) => {
  test.skip(process.env.LOOP_L4_DELETE_PHASE !== "1", "run phase only");
  if (!scenario) throw new Error("L4 delete environment is incomplete");
  const pageErrors = collectPageErrors(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: `l4-${scenario}` })).toBeVisible();
  await page.getByRole("button", { name: "🗑 刪除" }).click();
  const confirmation = page.getByRole("dialog", { name: "請確認" });
  await expect(confirmation).toContainText("Run 身分");
  await expect(confirmation).toContainText("清理範圍");
  await confirmation.getByRole("button", { name: "永久刪除" }).click();
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toBeVisible();
  await expect(page.getByRole("tab")).toHaveCount(0);
  pageErrors.assertEmpty();
});
