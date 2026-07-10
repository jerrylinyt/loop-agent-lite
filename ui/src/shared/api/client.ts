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
