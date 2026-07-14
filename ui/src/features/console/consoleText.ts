export type ConsoleFilter = "agent" | "other" | "all";
const AGENT_MARKER = `${String.fromCodePoint(0x1f916)} Agent｜`;

/** 顯示層移除 log protocol 中的 emoji；保留原始文字供來源分流與舊紀錄解析。 */
export function withoutEmojiIcons(text: string) {
  return text
    .replace(/\p{Extended_Pictographic}\uFE0F?/gu, "")
    .replace(/\uFE0F/gu, "")
    .replace(/[ \t]{2,}/g, " ");
}

/** 依固定的 Agent 行標記分流；all 保留原文，其他模式仍保留換行順序。 */
export function filterConsoleText(text: string, filter: ConsoleFilter) {
  if (filter === "all") return text;
  const wantAgent = filter === "agent";
  return text.split("\n").filter((line) => line.includes(AGENT_MARKER) === wantAgent).join("\n");
}

/** 保留 console 尾端但不從完整行中間起頭；單一超長 Agent 行仍補回來源標記。 */
export function trimConsoleText(text: string, limit: number) {
  if (text.length <= limit) return text;
  const cutAt = text.length - limit;
  const tail = text.slice(cutAt);
  const firstNewline = tail.indexOf("\n");
  if (firstNewline >= 0) return tail.slice(firstNewline + 1);

  // 整個 tail 都屬於同一條超長行。判斷被切掉的前段是否為 Agent 行，避免它被
  // filter 誤歸到左下角「其他」；後續再 append 同一行時也能持續保留這個標記。
  const lineStart = text.lastIndexOf("\n", cutAt - 1) + 1;
  const removedPrefix = text.slice(lineStart, cutAt);
  const prefix = removedPrefix.includes(AGENT_MARKER) ? `${AGENT_MARKER}…前段已截斷…` : "…前段已截斷…";
  return prefix.length >= limit ? prefix.slice(0, limit) : prefix + tail.slice(-(limit - prefix.length));
}

export function appendConsoleText(current: string, incoming: string, limit: number) {
  return trimConsoleText(current + incoming, limit);
}

/** 不區分大小寫的逐行搜尋，空 query 直接回傳原文。 */
export function searchConsoleText(text: string, query: string) {
  const needle = query.trim().toLowerCase();
  if (!needle) return text;
  return text.split("\n").filter((line) => line.toLowerCase().includes(needle)).join("\n");
}
