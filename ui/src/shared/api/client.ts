import type { StartupStatus } from "./types";

export async function getJson<T>(url: string): Promise<T | null> {
  try {
    const response = await fetch(url);
    return await response.json() as T;
  } catch {
    return null;
  }
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

export async function waitForJobStartup(name: string, pid: number, timeoutSeconds = 135): Promise<{ error?: string }> {
  const deadline = Date.now() + Math.max(1, timeoutSeconds) * 1000;
  while (Date.now() < deadline) {
    const result = await getJson<StartupStatus>(`/api/job-startup?name=${encodeURIComponent(name)}&pid=${pid}`);
    if (result?.status === "ready") return {};
    if (result?.status === "failed") {
      const detail = result.tail?.trim();
      return { error: `${result.error ?? "loop 啟動失敗"}${detail ? `：\n${detail}` : ""}` };
    }
    await new Promise((resolve) => window.setTimeout(resolve, 250));
  }
  return { error: `等待 loop 啟動超過 ${timeoutSeconds} 秒` };
}
