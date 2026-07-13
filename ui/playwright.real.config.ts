import { defineConfig } from "@playwright/test";

const baseURL = process.env.LOOP_L4_BASE_URL;
if (!baseURL) throw new Error("LOOP_L4_BASE_URL is required");

export default defineConfig({
  testDir: "./e2e",
  testMatch: "parallel-real-dry-run.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 4 * 60 * 60 * 1000,
  expect: { timeout: 30_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report-real" }]],
  outputDir: process.env.LOOP_L4_ARTIFACTS
    ? `${process.env.LOOP_L4_ARTIFACTS}/playwright-${process.env.LOOP_L4_DELETE_PHASE === "1" ? "delete" : "run"}`
    : "test-results-real",
  use: {
    baseURL,
    trace: "on",
    screenshot: "on",
    video: "on"
  }
});
