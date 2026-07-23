/** Ralph runner 前端端到端：對真跑完成的 ralph workspace 驗證 RalphView，並驗證 Ralph 啟動表單。
 *  後端由 tests/e2e_ralph_server.py 提供（真 clone snarktank/ralph，離線退回本地 fake ralph，
 *  兩者皆真跑 ralph.sh 迴圈到完成）。 */
import { expect, test } from "@playwright/test";

test.describe("Ralph runner UI", () => {
  test("RalphView 顯示 PRD 檢核表、進度紀錄與完成狀態", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("tab", { name: /ralph-live/ }).click();

    const pane = page.locator(".workspace-pane");
    await expect(pane.getByRole("heading", { name: "ralph-live" })).toBeVisible();
    await expect(pane.locator(".ralph-runner-tag")).toHaveText("Ralph");
    // 完成終態徽章（RALPH_EXIT.completed.label）
    await expect(pane.locator(".phase-badge.ralph-exit-success")).toHaveText("已完成");
    // story 進度
    await expect(pane.getByText("Stories 2/2")).toBeVisible();
    // PRD 檢核表：2/2 通過、兩個 story 都在（scope 到檢核表，避開進度紀錄裡也含 US-1 字樣）
    const checklist = pane.locator(".ralph-checklist");
    await expect(checklist.getByText("PRD 檢核表")).toBeVisible();
    await expect(checklist.getByText("2/2 通過")).toBeVisible();
    await expect(checklist.locator(".ralph-story")).toHaveCount(2);
    await expect(checklist.locator(".ralph-story-id", { hasText: "US-1" })).toHaveCount(1);
    await expect(checklist.locator(".ralph-story-id", { hasText: "US-2" })).toHaveCount(1);
    // 兩個 story 都應打勾
    await expect(checklist.locator(".ralph-story-check.pass")).toHaveCount(2);
    // 進度紀錄面板
    await expect(pane.getByText("進度紀錄")).toBeVisible();

    // loop 專屬控制項不得出現
    await expect(pane.getByRole("button", { name: "進執行期" })).toHaveCount(0);
    await expect(pane.getByRole("button", { name: "回規劃期" })).toHaveCount(0);

    // 查看 PRD 原文
    await pane.getByRole("button", { name: "查看 PRD 原文" }).click();
    const modal = page.getByRole("dialog", { name: "PRD 原文" });
    await expect(modal).toBeVisible();
    await expect(modal.getByText(/US-1/)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(modal).toBeHidden();
  });

  test("用量上限橫幅渲染（等待重啟＋偵測訊號）", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("tab", { name: /ralph-limit/ }).click();

    const banner = page.locator(".ralph-usage-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("用量上限");
    await expect(banner).toContainText("自動重啟");
    await expect(banner).toContainText("偵測訊號");

    // 狀態列：降級/重啟次數與目前模型
    const pane = page.locator(".workspace-pane");
    await expect(pane).toContainText("第 2/6 次");
    await expect(pane).toContainText("opus");
  });

  test("啟動器 Ralph 模式只顯示 ralph 參數", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "＋ 啟動／管理" }).click();
    const launcher = page.getByRole("dialog", { name: "啟動與管理" });
    await expect(launcher).toBeVisible();

    await launcher.getByRole("tab", { name: "Ralph" }).click();
    await expect(launcher.getByText("Ralph 命令")).toBeVisible();
    await expect(launcher.getByText("迭代上限")).toBeVisible();
    await expect(launcher.getByText("進階設定：用量上限自動重啟")).toBeVisible();
    // ralph 模式不顯示 loop 專屬欄位
    await expect(launcher.getByText("Validate 命令")).toHaveCount(0);

    // 切回 Loop 應恢復既有欄位
    await launcher.getByRole("tab", { name: "Loop coordinator" }).click();
    await expect(launcher.getByText("Ralph 命令")).toHaveCount(0);
  });
});
