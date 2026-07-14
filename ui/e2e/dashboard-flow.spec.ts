/** 真實瀏覽器端到端流程：使用隔離 fixture 驗證啟動、SSE、操作防線、Plan 編輯與唯讀模式。 */
import { expect, test, type Page } from "@playwright/test";

const PLAN = JSON.stringify([
  { order: 1, task: "建立 E2E 第一項功能", ref: "README.md" },
  { order: 2, task: "驗證 E2E 第二項功能" }
], null, 2);

async function acceptConfirmation(page: Page, action: () => Promise<void>) {
  // 所有破壞性操作應先開啟共用確認視窗；helper 同時驗證這條 UI 契約。
  await action();
  const dialog = page.getByRole("dialog", { name: "請確認" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: /繼續|清空/ }).click();
}

async function runNormally(page: Page) {
  await page.getByRole("button", { name: "運行", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "選擇啟動方式" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "一般執行", exact: true }).click();
}

test("Goal 產生器 Prompt 與 Goal 成果模板分開，且 Plan 仍可使用", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "＋ 啟動第一個 loop" }).click();
  const launcher = page.getByRole("dialog", { name: "啟動與管理" });

  await launcher.getByRole("button", { name: "Goal 成果模板" }).click();
  const goalTemplate = page.getByRole("dialog", { name: "Goal 成果模板" });
  await expect(goalTemplate).toBeVisible();
  await expect(goalTemplate).toContainText("提供與 Goal 產生器相同的 25 種任務類型");
  const goalTemplateType = goalTemplate.getByRole("combobox", { name: "Goal 模板類型" });
  const goalTemplateTypeIds = await goalTemplateType.locator("option").evaluateAll(
    (options) => options.map((option) => (option as HTMLOptionElement).value)
  );
  expect(goalTemplateTypeIds).toHaveLength(25);
  const goalTemplatePreview = goalTemplate.getByTestId("goal-template-preview");
  for (const id of goalTemplateTypeIds) {
    await goalTemplateType.selectOption(id);
    const rendered = await goalTemplatePreview.textContent();
    expect(rendered).toMatch(/^# Goal/);
    expect(rendered?.match(/^## /gm)).toHaveLength(8);
    expect(rendered).not.toContain("<<TEMPLATE_");
    expect(rendered).not.toContain("<<REQUIREMENT_EXAMPLE>>");
  }
  await goalTemplateType.selectOption("jsp-react-migration");
  const finalGoal = await goalTemplatePreview.textContent();
  expect(finalGoal).toMatch(/^# Goal/);
  expect(finalGoal?.match(/^## /gm)).toHaveLength(8);
  expect(finalGoal).toContain("## 完成定義（DoD）");
  expect(finalGoal).toContain("JSP → React 搬移");
  expect(finalGoal).toContain("每支 JSP／fragment");
  expect(finalGoal).toContain("SC-1");
  expect(finalGoal).toContain("AC-1");
  expect(finalGoal).not.toContain("<original_requirement_json>");
  expect(finalGoal).not.toContain("<<MODE_CONTRACT>>");
  expect(finalGoal).not.toContain("外部 Agent 任務：");
  const goalDownloadPromise = page.waitForEvent("download");
  await goalTemplate.getByRole("button", { name: "下載 jsp-react-migration-goal-template.md" }).click();
  const goalDownload = await goalDownloadPromise;
  expect(goalDownload.suggestedFilename()).toBe("jsp-react-migration-goal-template.md");
  const goalDownloadStream = await goalDownload.createReadStream();
  let downloadedGoal = "";
  for await (const chunk of goalDownloadStream) downloadedGoal += chunk.toString();
  expect(downloadedGoal).toBe(finalGoal);
  await goalTemplate.getByRole("button", { name: "上一頁", exact: false }).click();
  await expect(goalTemplate).toBeHidden();
  await expect(launcher).toBeVisible();

  await launcher.getByRole("button", { name: "Goal 產生器 Prompt" }).click();
  let promptTemplates = page.getByRole("dialog", { name: "外部 Agent 產生器 Prompt" });
  await expect(promptTemplates).toBeVisible();
  await expect(promptTemplates.getByRole("tab", { name: "Goal 產生器 Prompt" })).toHaveAttribute("aria-selected", "true");
  const promptType = promptTemplates.getByRole("combobox", { name: "Prompt 任務類型" });
  const promptTypeIds = await promptType.locator("option").evaluateAll(
    (options) => options.map((option) => (option as HTMLOptionElement).value)
  );
  expect(promptTypeIds).toEqual(goalTemplateTypeIds);
  const promptPreview = promptTemplates.getByTestId("prompt-template-preview");
  const copyPromptButton = promptTemplates.getByRole("button", { name: "複製 Prompt" });
  const downloadPromptButton = promptTemplates.getByRole("button", { name: "下載 .md" });
  const promptRequirement = promptTemplates.getByLabel("原始需求");
  // 開啟即以第一個模板的範例預填（去掉「例：」前綴），預覽直接可用，並提示仍是範例
  await expect(promptRequirement).toHaveValue(/^新增可依狀態篩選 workspace/);
  await expect(copyPromptButton).toBeEnabled();
  await expect(promptPreview).toContainText("最終輸出契約：goal.md");
  expect(await promptPreview.textContent()).not.toContain("## 已知專案資訊與限制");
  await expect(promptPreview).not.toContainText("_json");
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
  await promptTemplates.getByLabel(/已知專案資訊／限制/).fill("正式環境只能在指定維護窗口切換");
  await expect(copyPromptButton).toBeEnabled();
  await expect(downloadPromptButton).toBeEnabled();
  await expect(promptPreview).toContainText("分析需求並產生 goal.md");
  await expect(promptPreview).toContainText("使用邊界");
  await expect(promptPreview).toContainText("已知專案資訊與限制");
  await expect(promptPreview).toContainText("分析專案架構／邏輯");
  await expect(promptPreview).toContainText("共用分析規則");
  await expect(promptPreview).toContainText("最終輸出契約：goal.md");
  const renderedPrompt = await promptPreview.textContent();
  expect(renderedPrompt).toContain("> 分析 Dashboard 啟動 loop");
  expect(renderedPrompt).toContain("<<MODE_CONTRACT>>");
  expect(renderedPrompt).toContain("</original_requirement_json>");
  expect(renderedPrompt).toContain("$&");
  expect(renderedPrompt).not.toContain("<original_requirement_json>");
  expect(renderedPrompt).not.toContain("<template_instructions_json>");
  expect(renderedPrompt).not.toContain("\\u003c");

  await promptType.selectOption("e2e-team-analysis");
  // 使用者改過的需求在切換模板時不被預填覆蓋
  await expect(promptRequirement).toHaveValue(/保留 literal/);
  await expect(promptTemplates.locator(".prompt-template-summary").getByText("E2E 團隊自訂模板", { exact: true })).toBeVisible();
  await expect(promptTemplates.locator(".prompt-template-summary").getByText("團隊", { exact: true })).toBeVisible();
  await expect(promptPreview).toContainText("追蹤 E2E 團隊狀態真相來源");
  await promptTemplates.getByRole("tab", { name: "Plan 拆分模板" }).click();
  await expect(promptPreview).toContainText("分析需求並產生 plan.json");
  await expect(promptPreview).toContainText("只輸出一個合法 JSON array");
  await expect(promptPreview).toContainText("只能有 `order`、`task`、選填的 `ref`");
  const promptDownloadPromise = page.waitForEvent("download");
  await downloadPromptButton.click();
  const promptDownload = await promptDownloadPromise;
  expect(promptDownload.suggestedFilename()).toBe("e2e-team-analysis-plan-prompt.md");
  await promptTemplates.getByRole("button", { name: "上一頁", exact: false }).click();
  await expect(promptTemplates).toBeHidden();
  await expect(launcher).toBeVisible();

  await launcher.getByRole("button", { name: "產生 Plan Prompt" }).click();
  promptTemplates = page.getByRole("dialog", { name: "外部 Agent 產生器 Prompt" });
  await expect(promptTemplates.getByRole("tab", { name: "Plan 拆分模板" })).toHaveAttribute("aria-selected", "true");
  await promptTemplates.getByRole("button", { name: "上一頁", exact: false }).click();
  await launcher.getByRole("button", { name: "取消", exact: true }).click();
});

test("固定 Prompt 資源失效會停用產生器與成果模板並顯示原因", async ({ page }) => {
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
  await expect(launcher.getByRole("button", { name: "Goal 產生器 Prompt" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "Goal 成果模板" })).toBeDisabled();
  await expect(launcher.getByRole("button", { name: "產生 Plan Prompt" })).toBeDisabled();
  await expect(launcher.getByRole("alert")).toContainText("Prompt 模板停用：E2E 固定 Prompt 資源損毀");
  await expect(launcher.getByRole("combobox", { name: "Repo" })).toBeEnabled();
});

test("運行先選擇啟動方式，缺少 Resume 資料時可補填後呼叫獨立 endpoint", async ({ page }) => {
  const workspace = {
    name: "resume-ready", phase: "exec", running: false, round: 3,
    completed: 0, plan_len: 1, done_count: 0, resume_available: false,
  };
  const state = {
    phase: "exec", round: 3, flag: 0, done_count: 0, red_streak: 0, stall_rounds: 0,
    plan_version: 1, current_order: 1, completed: [], issues: [],
    round_started_at: null,
    round_deadline_at: null,
    round_interrupted_at: null,
    last_green_sha: null,
    plan: [{ order: 1, task: "接手 Agent 未完成變更", ref: null }],
    config: { flag_threshold: 10, done_threshold: 3, red_limit: 20, stall_limit: 300 },
  };
  let resumeBody: Record<string, string> | null = null;
  await page.route("**/api/workspaces", async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify([workspace]) });
  });
  await page.route("**/api/health", async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      schema_version: 1, status: "ok", workspace_count: 1, running: 0, attention: 0,
      error_count: 0, issues: 0, unread_issues: 0, agent_failures: 0,
      round_timeouts: 0, state_recoveries: 0, goal_changes: 0, stale_loop_pids: 0,
      generated_at: "2026-07-14T10:05:00+08:00",
    }) });
  });
  await page.route("**/api/events?**", async (route) => {
    await route.fulfill({
      contentType: "text/event-stream",
      body: `event: workspaces\ndata: ${JSON.stringify([workspace])}\n\nevent: state\ndata: ${JSON.stringify(state)}\n\n`,
    });
  });
  await page.route("**/api/resume", async (route) => {
    resumeBody = route.request().postDataJSON() as Record<string, string>;
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true }) });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "resume-ready" })).toBeVisible();
  await page.getByRole("button", { name: "運行", exact: true }).click();
  const launchChoice = page.getByRole("dialog", { name: "選擇啟動方式" });
  await expect(launchChoice).toContainText("可補資料後啟動");
  const resume = launchChoice.getByRole("button", { name: "Resume", exact: true });
  await expect(resume).toBeDisabled();
  await launchChoice.getByLabel("Resume 執行開始時間").fill("2020-01-02T03:04:05");
  await launchChoice.getByLabel("Resume 綠點 commit SHA").fill("73a9be0");
  await expect(resume).toBeEnabled();
  await resume.click();
  expect(resumeBody).toMatchObject({ name: "resume-ready", last_green_sha: "73a9be0" });
  expect(Date.parse(resumeBody?.round_started_at ?? "")).toBeLessThan(Date.now());
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
  await cliManager.getByRole("button", { name: "儲存 CLI 設定" }).click();
  await expect(cliManager).toBeHidden();
  await launcher.getByRole("button", { name: "管理 Code Repo Roots" }).click();
  const rootsManager = page.getByRole("dialog", { name: "Code Repo Roots 管理" });
  await expect(rootsManager.getByLabel("Repo root 1")).toBeVisible();
  await rootsManager.getByRole("button", { name: "取消" }).click();
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

  await launcher.getByRole("button", { name: "管理終態通知" }).click();
  const notifyManager = page.getByRole("dialog", { name: "終態通知管理" });
  await expect(notifyManager).toBeVisible();
  await notifyManager.getByLabel("通知命令").fill("echo ping-{status}-{name}");
  await notifyManager.getByRole("button", { name: "以 status=test 執行測試" }).click();
  await expect(notifyManager.getByRole("status")).toContainText("通知命令執行成功");
  await expect(notifyManager.locator("pre")).toContainText("ping-test-dashboard-test");
  await notifyManager.getByRole("button", { name: "儲存通知設定" }).click();
  await expect(notifyManager).toBeHidden();
  await expect(launcher.getByText("目前：echo ping-{status}-{name}")).toBeVisible();

  await launcher.getByRole("button", { name: "啟動" }).click();

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
  await expect(page.getByRole("button", { name: "立即停止" })).toBeVisible();
  await expect(page.getByRole("button", { name: "本輪後停止" })).toBeVisible();
  const roundTimer = page.getByTestId("round-timer");
  await expect(roundTimer).toBeVisible();
  await expect(roundTimer).toContainText("本輪");
  await expect(roundTimer).toContainText("剩");
  await expect(page).toHaveTitle(/^執行中 e2e-workspace · r\d+/);
  const faviconHref = await page.evaluate(() => document.querySelector('link[rel="icon"]')?.getAttribute("href") ?? "");
  expect(faviconHref.startsWith("data:image/png")).toBeTruthy();

  // 以此為範本啟動：帶入這個（執行中）workspace 的 config；先等 Agent 欄位命中儲存值，代表 hydration 完成。
  await page.getByRole("button", { name: "以此為範本啟動" }).click();
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

  await page.getByRole("button", { name: "總覽" }).click();
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
  await expect(overview.getByRole("button", { name: "批次操作" })).toBeVisible();
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
  await expect(eventFeed.locator(".fleet-event", { hasText: "開始 task-1" }).first()).toBeVisible();
  await expect(eventFeed.locator(".fleet-event-ws", { hasText: "e2e-workspace" }).first()).toBeVisible();
  await fleetCard.click();
  await expect(overview).toBeHidden();
  await expect(page.getByRole("heading", { name: "e2e-workspace" })).toBeVisible();
  const agentConsole = page.getByRole("region", { name: "Agent 執行輸出", exact: true });
  const loopConsole = page.getByRole("region", { name: "Loop 狀態紀錄", exact: true });
  await expect(agentConsole).toContainText("E2E fake agent started");
  await expect(loopConsole).toContainText("啟動 Agent｜命令：");
  await expect(loopConsole).toContainText("Agent 指令｜done task-1");
  await expect(loopConsole).toContainText("驗證通過");
  await expect(agentConsole).not.toContainText("Agent 指令｜done task-1");

  await page.getByRole("button", { name: "本輪後停止" }).click();
  await expect(page.getByRole("button", { name: "繼續運行" })).toBeVisible();
  await page.getByRole("button", { name: "繼續運行" }).click();
  await expect(page.getByRole("button", { name: "本輪後停止" })).toBeVisible();
  await expect(loopConsole).toContainText("已撤銷本輪後停止");

  await agentConsole.getByRole("button", { name: "其他", exact: true }).click();
  await expect(agentConsole).toContainText("Agent 指令｜done task-1");
  await expect(agentConsole).not.toContainText("Agent｜E2E fake agent started");
  await agentConsole.getByRole("button", { name: "全部", exact: true }).click();
  await expect(agentConsole).toContainText("Agent 指令｜done task-1");
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

  await page.getByRole("button", { name: "本輪後停止" }).click();
  await expect(page.getByRole("button", { name: "運行" })).toBeVisible();
  await expect(loopConsole).toContainText("已依要求停止");
  await expect(page).toHaveTitle(/^已停止 e2e-workspace/);
  await expect(roundTimer).toBeHidden();

  await page.getByRole("button", { name: "時間軸" }).click();
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

  await page.getByRole("button", { name: "輪次紀錄" }).click();
  const historyModal = page.getByRole("dialog", { name: "輪次紀錄" });
  await expect(historyModal).toBeVisible();
  const firstHistoryRow = historyModal.locator("tbody tr").first();
  await expect(firstHistoryRow).toContainText("執行");
  await expect(firstHistoryRow).toContainText("task-1");
  await expect(firstHistoryRow).toContainText("done");
  await expect(firstHistoryRow).toContainText("通過");
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

  await page.getByRole("button", { name: "Goal" }).click();
  const goalModal = page.getByRole("dialog", { name: "Goal" });
  await expect(goalModal).toBeVisible();
  await expect(goalModal).toContainText("E2E goal imported through UI");
  await goalModal.getByRole("button", { name: "關閉對話框" }).click();
  await expect(goalModal).toBeHidden();

  await page.getByRole("button", { name: "Prompt" }).click();
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

  await page.getByRole("button", { name: "設定" }).click();
  let settings = page.getByRole("dialog", { name: "Workspace 設定" });
  await expect(settings).toBeVisible();
  await settings.getByRole("button", { name: "取消" }).click();
  await expect(settings).toBeHidden();

  await page.getByRole("button", { name: "設定" }).click();
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
  await expect(loopConsole).toContainText("Dashboard｜更新 Workspace 設定");

  await page.getByRole("button", { name: "設定" }).click();
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

  await page.getByRole("button", { name: "編輯計畫" }).click();
  let planEditor = page.getByRole("dialog", { name: "Plan 編輯器" });
  await expect(planEditor.locator(".plan-editor-task").first().getByLabel("任務內容")).toBeDisabled();
  await planEditor.locator(".plan-editor-task").nth(1).getByLabel("任務內容").fill("這個變更應該被取消");
  await planEditor.getByRole("button", { name: "取消", exact: true }).click();
  const discardPlan = page.getByRole("dialog", { name: "放棄未儲存變更？" });
  await discardPlan.getByRole("button", { name: "放棄變更" }).click();
  await expect(planEditor).toBeHidden();
  await expect(page.getByRole("button", { name: "驗證 E2E 第二項功能" })).toBeVisible();
  await page.getByRole("button", { name: "編輯計畫" }).click();
  planEditor = page.getByRole("dialog", { name: "Plan 編輯器" });
  await planEditor.locator(".plan-editor-task").first().getByRole("button", { name: "插入在 task-1 之後" }).click();
  await expect(planEditor.getByRole("button", { name: "儲存變更" })).toBeDisabled();
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
  await planEditor.getByRole("button", { name: "儲存變更" }).click();
  await expect(planEditor).toBeHidden();
  await expect(page.getByRole("button", { name: "插入的 E2E 任務" })).toBeVisible();

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

  await page.getByRole("button", { name: "回規劃期" }).click();
  let operationDialog = page.getByRole("dialog", { name: "請確認" });
  await expect(operationDialog.locator(".action-preview > div", { hasText: "清除進度" })).toContainText("完成紀錄");
  await expect(operationDialog.locator(".action-preview > div", { hasText: "保留" })).toContainText("target repo");
  await operationDialog.getByRole("button", { name: "繼續" }).click();
  await expect(page.getByText("規劃期", { exact: true })).toBeVisible();
  await runNormally(page);
  await expect(page.getByRole("button", { name: "立即停止" })).toBeVisible();
  await expect(page.getByRole("status", { name: "計畫已更新 v3" })).toBeVisible();
  await expect(page.locator('tr[data-order="2"]')).toHaveClass(/flash/);
  await expect(page.getByRole("button", { name: "由 Agent 重新分析的第二項功能" })).toBeVisible();
  await expect(loopConsole).toContainText("Agent 指令｜create-plan");
  await expect(loopConsole).toContainText("計畫已更新｜v3｜共 2 條任務");
  await page.getByRole("button", { name: "立即停止" }).click();
  await expect(page.getByRole("button", { name: "運行" })).toBeVisible();
  await acceptConfirmation(page, () => page.getByRole("button", { name: "進執行期" }).click());
  await expect(page.getByText("執行期", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "把進度設到 task-2" }).click();
  operationDialog = page.getByRole("dialog", { name: "請確認" });
  await expect(operationDialog.locator(".action-preview > div", { hasText: "人工標記完成" })).toContainText("task-1");
  await expect(operationDialog.locator(".action-preview > div", { hasText: "執行 Validate" })).toContainText("timeout");
  await operationDialog.getByRole("button", { name: "繼續" }).click();
  await expect(page.getByText("進行中", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /已完成 1 條/ }).click();
  await expect(page.getByRole("row", { name: /已由 E2E 更新的第一項功能.*完成 人工/ })).toBeVisible();
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

  await runNormally(page);
  await expect(page.getByRole("button", { name: "立即停止" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Agent 執行輸出", exact: true })).toContainText("E2E fake agent started");
  await expect(page.locator(".chip.status-pulse").filter({ hasText: /^done / })).toBeVisible();
  await page.getByRole("button", { name: "立即停止" }).click();
  await expect(page.getByRole("button", { name: "運行" })).toBeVisible();

  await page.getByRole("button", { name: "設定" }).click();
  settings = page.getByRole("dialog", { name: "Workspace 設定" });
  await settings.getByLabel("done 收斂（≥）").fill("1");
  await settings.getByRole("button", { name: "儲存設定" }).click();
  await expect(settings).toBeHidden();

  await runNormally(page);
  await expect(page.locator("#main-content .phase-badge", { hasText: "完成" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "運行" })).toBeVisible();
  await expect(page).toHaveTitle(/^完成 e2e-workspace/);

  await page.getByRole("button", { name: "完成報告" }).click();
  const reportModal = page.getByRole("dialog", { name: "完成報告" });
  await expect(reportModal).toBeVisible();
  await expect(reportModal).toContainText("loop-agent-lite RUN REPORT");
  await expect(reportModal).toContainText("task-1");
  await expect(reportModal).toContainText("task-2");
  await reportModal.getByRole("button", { name: "關閉對話框" }).click();
  await expect(reportModal).toBeHidden();

  await page.getByRole("button", { name: "總覽" }).click();
  const finalOverview = page.getByRole("main", { name: "工作區總覽" });
  const finalFeed = finalOverview.getByRole("complementary", { name: "事件推播" });
  await expect(finalFeed.locator(".fleet-event", { hasText: "開始 task-2" }).first()).toBeVisible();
  await expect(finalFeed.locator(".fleet-event", { hasText: "開始 task-1" })).toHaveCount(2);
  await page.getByRole("button", { name: "總覽" }).click();
  await expect(finalOverview).toBeHidden();

  await page.getByRole("button", { name: "刪除" }).click();
  const deleteDialog = page.getByRole("dialog", { name: "確認刪除 workspace" });
  await expect(deleteDialog).toContainText("整個 workspace 的資料會直接移除");
  await expect(deleteDialog).toContainText("target repo 與程式碼不受影響");
  await deleteDialog.getByRole("button", { name: "永久刪除" }).click();
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toBeVisible();
  await expect(page.getByRole("button", { name: "已封存" })).toHaveCount(0);
});

test("read-only instance 隱藏寫入控制並拒絕 POST", async ({ page, request }) => {
  await page.goto("http://127.0.0.1:8877/");
  await expect(page.getByRole("heading", { name: "尚未建立 workspace" })).toBeVisible();
  await expect(page.getByRole("button", { name: /啟動/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "已封存" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "刪除" })).toHaveCount(0);
  const response = await request.post("http://127.0.0.1:8877/api/delete-workspace", { data: { name: "anything" } });
  expect(response.status()).toBe(403);
  expect(await response.json()).toMatchObject({ error: expect.stringContaining("唯讀模式") });
});
