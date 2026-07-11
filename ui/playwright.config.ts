/** Playwright 使用兩個隔離本機服務：可寫實例驗證完整流程，唯讀實例驗證所有 POST 都被拒絕。 */
import { defineConfig } from "@playwright/test";

const writableUrl = "http://127.0.0.1:8876";
const readonlyUrl = "http://127.0.0.1:8877";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 12_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: writableUrl,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure"
  },
  webServer: [
    {
      command: "python3 scripts/e2e_server.py --port 8876",
      url: writableUrl,
      reuseExistingServer: false,
      timeout: 20_000
    },
    {
      command: "python3 scripts/e2e_server.py --port 8877 --read-only",
      url: readonlyUrl,
      reuseExistingServer: false,
      timeout: 20_000
    }
  ]
});
