/** Playwright 使用隔離本機服務：可寫實例驗證完整流程，唯讀實例驗證所有 POST 都被拒絕，
 *  另一個 ralph 實例（真跑 ralph.sh 到完成）驗證 Ralph runner 前端。 */
import { defineConfig } from "@playwright/test";

const writableUrl = "http://127.0.0.1:8876";
const readonlyUrl = "http://127.0.0.1:8877";
const ralphUrl = "http://127.0.0.1:8878";
const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const pythonCommand = /\s/.test(python) ? `"${python.replace(/"/g, '\\"')}"` : python;
const webServerEnv = {
  ...process.env,
  // Dashboard subprocesses exchange UTF-8 JSON/logs.  Force the Windows E2E
  // interpreter to decode captured child output consistently instead of CP950.
  PYTHONUTF8: "1",
  PYTHONIOENCODING: "utf-8",
} as Record<string, string>;

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
    video: "retain-on-failure",
    // 預設用 Playwright 自管的瀏覽器；受管環境可用 PW_CHROMIUM 指向預裝 Chromium。
    launchOptions: { executablePath: process.env.PW_CHROMIUM || undefined }
  },
  projects: [
    { name: "dashboard", testIgnore: /ralph-flow/, use: { baseURL: writableUrl } },
    { name: "ralph", testMatch: /ralph-flow/, use: { baseURL: ralphUrl } }
  ],
  webServer: [
    {
      command: `${pythonCommand} ../tests/e2e_server.py --port 8876`,
      url: writableUrl,
      reuseExistingServer: false,
      timeout: 20_000,
      env: webServerEnv
    },
    {
      command: `${pythonCommand} ../tests/e2e_server.py --port 8877 --read-only`,
      url: readonlyUrl,
      reuseExistingServer: false,
      timeout: 20_000,
      env: webServerEnv
    },
    {
      // 真 clone snarktank/ralph（離線退回本地 fake ralph）並真跑到完成，較慢，放寬 timeout。
      command: `${pythonCommand} ../tests/e2e_ralph_server.py --port 8878`,
      url: ralphUrl,
      reuseExistingServer: false,
      timeout: 150_000,
      env: webServerEnv
    }
  ]
});
