import { expect, test, type Page } from "@playwright/test";

const PLAN = JSON.stringify([
  { order: 1, task: "建立 E2E 第一項功能", ref: "README.md" },
  { order: 2, task: "驗證 E2E 第二項功能" }
], null, 2);

async function acceptConfirmation(page: Page, action: () => Promise<void>) {
  await action();
  const dialog = page.getByRole("dialog", { name: "請確認" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: /繼續|清空/ }).click();
}

test("完整操作流程：launch、SSE、stop/run、設定、計畫、issues、phase 與進度", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toBeVisible();
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
  await cliManager.getByRole("button", { name: "執行測試" }).click();
  const launchAgentCheck = page.getByRole("dialog", { name: "Agent CLI 執行確認" });
  await expect(launchAgentCheck.getByRole("status")).toContainText("E2E Agent CLI test result");
  await launchAgentCheck.getByRole("button", { name: "關閉", exact: true }).click();
  await cliManager.getByRole("button", { name: "儲存 CLI 設定" }).click();
  await expect(cliManager).toBeHidden();
  await launcher.getByRole("button", { name: "管理 Code Repo Roots" }).click();
  const rootsManager = page.getByRole("dialog", { name: "Code Repo Roots 管理" });
  await expect(rootsManager.getByLabel("Repo root 1")).toBeVisible();
  await rootsManager.getByRole("button", { name: "取消" }).click();
  await launcher.locator(".validate-command-field").getByRole("button", { name: "執行確認" }).click();
  await expect(launcher.locator(".validate-result")).toContainText("Validate 通過");
  await launcher.locator(".validate-command-field").getByRole("button", { name: "完整健檢" }).click();
  await expect(launcher.getByRole("status").filter({ hasText: "完整啟動前健檢通過" })).toBeVisible();

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
  await launcher.getByLabel("done 收斂（≥）").fill("999");
  await launcher.getByLabel("單輪上限（分）").fill("1");
  await launcher.getByLabel("Agent 異常退避上限（秒）").fill("5");
  await launcher.getByLabel("在新 branch 跑（loop/<workspace 名>）").check();

  await launcher.getByRole("button", { name: "🔔 管理終態通知" }).click();
  const notifyManager = page.getByRole("dialog", { name: "終態通知管理" });
  await expect(notifyManager).toBeVisible();
  await notifyManager.getByLabel("通知命令").fill("echo ping-{status}-{name}");
  await notifyManager.getByRole("button", { name: "以 status=test 執行測試" }).click();
  await expect(notifyManager.getByRole("status")).toContainText("通知命令執行成功");
  await expect(notifyManager.locator("pre")).toContainText("ping-test-dashboard-test");
  await notifyManager.getByRole("button", { name: "儲存通知設定" }).click();
  await expect(notifyManager).toBeHidden();
  await expect(launcher.getByText("目前：echo ping-{status}-{name}")).toBeVisible();

  await launcher.getByRole("button", { name: "▶ 啟動" }).click();

  await expect(launcher).toBeHidden();
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

  await page.getByRole("button", { name: "📺 總覽" }).click();
  const overview = page.getByRole("main", { name: "工作區總覽" });
  await expect(overview).toBeVisible();
  await expect(overview.getByText("執行中", { exact: true })).toBeVisible();
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

  await page.getByRole("button", { name: "🕒 輪次紀錄" }).click();
  const historyModal = page.getByRole("dialog", { name: "輪次紀錄" });
  await expect(historyModal).toBeVisible();
  const firstHistoryRow = historyModal.locator("tbody tr").first();
  await expect(firstHistoryRow).toContainText("執行");
  await expect(firstHistoryRow).toContainText("task-1");
  await expect(firstHistoryRow).toContainText("done");
  await expect(firstHistoryRow).toContainText("✅");
  await expect(firstHistoryRow).toContainText("秒");
  const roundMetrics = historyModal.getByRole("list", { name: "輪次效能摘要" });
  await expect(roundMetrics).toBeVisible();
  await expect(roundMetrics).toContainText("平均");
  await expect(roundMetrics).toContainText("P50");
  await expect(roundMetrics).toContainText("P95");
  await expect(roundMetrics).toContainText("最慢");
  await expect(roundMetrics).toContainText("逾時率");
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
  await settings.getByRole("button", { name: "儲存設定" }).click();
  await expect(settings).toBeHidden();
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
  await page.getByLabel("task-1").fill("這個變更應該被取消");
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByRole("button", { name: "建立 E2E 第一項功能" })).toBeVisible();
  await page.getByRole("button", { name: "✎ 編輯計畫" }).click();
  await page.getByLabel("task-1").fill("已由 E2E 更新的第一項功能");
  await page.getByLabel("done 計數").fill("0");
  await page.getByRole("button", { name: "💾 儲存" }).click();
  await expect(page.getByRole("button", { name: "已由 E2E 更新的第一項功能" })).toBeVisible();

  await expect(page.getByRole("button", { name: /最近事件/ })).toHaveCount(0);

  await page.getByRole("button", { name: /issues/ }).click();
  let issues = page.getByRole("dialog", { name: "Issues" });
  await expect(issues.getByText("E2E structured issue").first()).toBeVisible();
  await issues.getByRole("button", { name: "標記已讀" }).click();
  await expect(issues.getByRole("status")).toContainText("稽核紀錄仍保留");
  await issues.getByRole("button", { name: "關閉對話框" }).click();
  await expect(page.getByRole("button", { name: /issues/ })).toContainText("已讀");
  await page.getByRole("button", { name: /issues/ }).click();
  issues = page.getByRole("dialog", { name: "Issues" });
  await acceptConfirmation(page, () => issues.getByRole("button", { name: "清空全部" }).click());
  await expect(issues.getByText("無 issues")).toBeVisible();
  await issues.getByRole("button", { name: "關閉對話框" }).click();

  await acceptConfirmation(page, () => page.getByRole("button", { name: "⏪ 回規劃期" }).click());
  await expect(page.getByText("規劃期", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "▶ 運行" }).click();
  await expect(page.getByRole("button", { name: "⏹ 立即停止" })).toBeVisible();
  await expect(page.getByRole("status", { name: "計畫已更新 v2" })).toBeVisible();
  await expect(page.locator('tr[data-order="2"]')).toHaveClass(/flash/);
  await expect(page.getByRole("button", { name: "由 Agent 重新分析的第二項功能" })).toBeVisible();
  await expect(loopConsole).toContainText("📨 Agent 指令｜create-plan");
  await expect(loopConsole).toContainText("📝 計畫已更新｜v2｜共 2 條任務");
  await page.getByRole("button", { name: "⏹ 立即停止" }).click();
  await expect(page.getByRole("button", { name: "▶ 運行" })).toBeVisible();
  await acceptConfirmation(page, () => page.getByRole("button", { name: "⏩ 進執行期" }).click());
  await expect(page.getByText("執行期", { exact: true })).toBeVisible();

  await acceptConfirmation(page, () => page.getByRole("button", { name: "把進度設到 task-2" }).click());
  await expect(page.getByText("→ 進行中", { exact: true })).toBeVisible();
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

  await page.getByRole("button", { name: "🗄 封存" }).click();
  const archiveDialog = page.getByRole("dialog", { name: "請確認" });
  await expect(archiveDialog).toContainText("已封存");
  await archiveDialog.getByRole("button", { name: "封存" }).click();
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toBeVisible();

  await page.getByRole("button", { name: "🗃 已封存" }).click();
  const archivesModal = page.getByRole("dialog", { name: "已封存 workspace" });
  await expect(archivesModal).toContainText("e2e-workspace");
  await archivesModal.getByRole("button", { name: "還原 e2e-workspace" }).click();
  const restoreDialog = page.getByRole("dialog", { name: "確認還原" });
  await expect(restoreDialog).toContainText("不會自動啟動 loop");
  await restoreDialog.getByRole("button", { name: "還原" }).click();
  await expect(page.getByRole("heading", { name: "e2e-workspace" })).toBeVisible();
  await expect(page.getByRole("button", { name: "▶ 運行" })).toBeVisible();

  await page.getByRole("button", { name: "🗄 封存" }).click();
  const secondArchiveDialog = page.getByRole("dialog", { name: "請確認" });
  await secondArchiveDialog.getByRole("button", { name: "封存" }).click();
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toBeVisible();
  await page.getByRole("button", { name: "🗃 已封存" }).click();
  const deleteArchivesModal = page.getByRole("dialog", { name: "已封存 workspace" });
  await deleteArchivesModal.getByRole("button", { name: "永久刪除" }).click();
  const deleteDialog = page.getByRole("dialog", { name: "確認永久刪除" });
  await expect(deleteDialog).toContainText("無法還原");
  await deleteDialog.getByRole("button", { name: "永久刪除" }).click();
  await expect(deleteArchivesModal).toContainText("目前沒有已封存的 workspace");
});

test("read-only instance 隱藏寫入控制並拒絕 POST", async ({ page, request }) => {
  await page.goto("http://127.0.0.1:8877/");
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toBeVisible();
  await expect(page.getByRole("button", { name: /啟動/ })).toHaveCount(0);
  await page.getByRole("button", { name: "🗃 已封存" }).click();
  await expect(page.getByRole("dialog", { name: "已封存 workspace" })).toBeVisible();
  await expect(page.getByRole("button", { name: /還原 / })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "永久刪除" })).toHaveCount(0);
  const response = await request.post("http://127.0.0.1:8877/api/run", { data: { name: "anything" } });
  expect(response.status()).toBe(403);
  expect(await response.json()).toMatchObject({ error: expect.stringContaining("唯讀模式") });
});
