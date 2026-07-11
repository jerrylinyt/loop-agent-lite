export type ConsoleFilter = "agent" | "other" | "all";

/** 依固定的 Agent 行標記分流；all 保留原文，其他模式仍保留換行順序。 */
export function filterConsoleText(text: string, filter: ConsoleFilter) {
  if (filter === "all") return text;
  const wantAgent = filter === "agent";
  return text.split("\n").filter((line) => line.includes("🤖 Agent｜") === wantAgent).join("\n");
}

/** 不區分大小寫的逐行搜尋，空 query 直接回傳原文。 */
export function searchConsoleText(text: string, query: string) {
  const needle = query.trim().toLowerCase();
  if (!needle) return text;
  return text.split("\n").filter((line) => line.toLowerCase().includes(needle)).join("\n");
}
