/** Playwright 使用單一隔離可寫服務驗證完整 Dashboard 流程。 */
import { defineConfig } from "@playwright/test";

const port = Number(process.env.LOOP_E2E_PORT);
if (!Number.isInteger(port) || port <= 0) {
  throw new Error("quick E2E 必須透過 scripts/run-playwright-e2e.mjs 分配唯一 loopback port");
}
const baseURL = process.env.LOOP_E2E_URL ?? `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./e2e",
  testIgnore: "parallel-real-dry-run.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 12_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure"
  },
  webServer: {
    command: `python3 ../tests/e2e_server.py --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 20_000
  }
});
