/** Dashboard HTTP client：統一 JSON 錯誤處理，並輪詢 startup handshake，避免 UI 把「已 spawn」誤當成「已可用」。 */
import type { IncrementalResponse, RalphPrdResponse, StartupResponse, StartupStatus } from "./types";

export async function getJson<T>(url: string): Promise<T | null> {
  try {
    const response = await fetch(url);
    return await response.json() as T;
  } catch {
    return null;
  }
}

/** Ralph runner：讀取 PRD 全文與 story 明細（RALPH_CONTRACT §E）。 */
export async function getRalphPrd(ws: string): Promise<RalphPrdResponse | null> {
  return getJson<RalphPrdResponse>(`/api/ralph/prd?ws=${encodeURIComponent(ws)}`);
}

/** Ralph runner：以 byte offset 增量讀取 progress.txt（沿用 read_incremental，RALPH_CONTRACT §E）。 */
export async function getRalphProgress(ws: string, offset: number): Promise<IncrementalResponse | null> {
  return getJson<IncrementalResponse>(`/api/ralph/progress?ws=${encodeURIComponent(ws)}&offset=${offset}`);
}

export async function postJson<T>(url: string, body: unknown): Promise<T & { error?: string }> {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    return await response.json() as T & { error?: string };
  } catch {
    return { error: "連線失敗" } as T & { error?: string };
  }
}

export async function waitForJobStartup(
  name: string,
  pid: number,
  timeoutSeconds = 135,
  jobId?: string
): Promise<{ error?: string }> {
  const deadline = Date.now() + Math.max(1, timeoutSeconds) * 1000;
  const startupQuery = jobId
    ? `job_id=${encodeURIComponent(jobId)}`
    : `name=${encodeURIComponent(name)}&pid=${pid}`;
  while (Date.now() < deadline) {
    const result = await getJson<StartupStatus>(`/api/job-startup?${startupQuery}`);
    if (result && result.rc !== null && result.rc !== undefined && result.rc !== 0) {
      const detail = result.tail?.trim();
      return { error: `${result.error ?? `loop 啟動失敗（rc=${result.rc}）`}${detail ? `：\n${detail}` : ""}` };
    }
    if (result?.status === "ready") return {};
    if (result?.status === "failed") {
      const detail = result.tail?.trim();
      return { error: `${result.error ?? "loop 啟動失敗"}${detail ? `：\n${detail}` : ""}` };
    }
    await new Promise((resolve) => window.setTimeout(resolve, 250));
  }
  return { error: `等待 loop 啟動超過 ${timeoutSeconds} 秒` };
}

/** POST an action that may return an asynchronous dashboard job, then await its real result. */
export async function postJobActionAndWait(
  url: string,
  body: unknown,
  fallbackName: string,
  fallbackTimeoutSeconds = 30,
): Promise<{ error?: string }> {
  const response = await postJson<StartupResponse>(url, body);
  if (response.error) return { error: response.error };
  if (!response.starting) return {};
  if (!response.job_id && (!response.name || !response.pid)) {
    return { error: "後端未回傳 job_id 或 name/pid" };
  }
  return waitForJobStartup(
    response.name ?? fallbackName,
    response.pid ?? 0,
    response.startup_timeout ?? fallbackTimeoutSeconds,
    response.job_id,
  );
}
