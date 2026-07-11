/** 將固定契約、任務模板與使用者需求組合成可交給外部 Agent 的純文字 prompt。 */
import type { PromptTemplate, PromptTemplateBundle } from "../../shared/api/types";

export type PromptTemplateMode = "goal" | "plan";

const RESOURCE_PLACEHOLDER_RE = /<<[A-Z][A-Z0-9_]*>>/g;
const RESOURCE_MARKER_RE = /<<[^\r\n]*?>>/g;
const REQUIRED_BUNDLE_FIELDS = ["base", "goal", "plan", "missing_requirement", "default_context", "team_template_example"] as const;
const BASE_PLACEHOLDERS = [
  "<<OUTPUT_NAME>>", "<<ORIGINAL_REQUIREMENT_JSON>>", "<<PROJECT_CONTEXT_JSON>>",
  "<<TEMPLATE_LABEL_JSON>>", "<<TEMPLATE_DESCRIPTION_JSON>>",
  "<<TEMPLATE_INSTRUCTIONS_JSON>>", "<<MODE_CONTRACT>>"
] as const;

function hasExactPlaceholders(source: string, expected: readonly string[]) {
  const valid = source.match(RESOURCE_PLACEHOLDER_RE) ?? [];
  const all = source.match(RESOURCE_MARKER_RE) ?? [];
  const expectedCounts = new Map<string, number>();
  const actualCounts = new Map<string, number>();
  for (const placeholder of expected) expectedCounts.set(placeholder, (expectedCounts.get(placeholder) ?? 0) + 1);
  for (const placeholder of valid) actualCounts.set(placeholder, (actualCounts.get(placeholder) ?? 0) + 1);
  return valid.length === all.length
    && source.split("<<").length - 1 === all.length
    && source.split(">>").length - 1 === all.length
    && valid.length === expected.length
    && [...expectedCounts].every(([placeholder, count]) => actualCounts.get(placeholder) === count);
}

export function isPromptTemplateBundleSupported(bundle: PromptTemplateBundle | null | undefined): bundle is PromptTemplateBundle {
  return !!bundle
    && bundle.schema_version === 1
    && REQUIRED_BUNDLE_FIELDS.every((field) => typeof bundle[field] === "string" && !!bundle[field].trim())
    && hasExactPlaceholders(bundle.base, BASE_PLACEHOLDERS)
    && bundle.base.trimEnd().endsWith("<<MODE_CONTRACT>>")
    && hasExactPlaceholders(bundle.missing_requirement, ["<<OUTPUT_NAME>>", "<<OUTPUT_NAME>>"])
    && [bundle.goal, bundle.plan, bundle.default_context, bundle.team_template_example]
      .every((resource) => hasExactPlaceholders(resource, []));
}

function renderPromptResource(source: string, replacements: Record<string, string>) {
  // 單次掃描原始資源；使用者輸入若剛好含 placeholder 外觀，不會被第二輪替換。
  let valid = true;
  const rendered = source.replace(RESOURCE_PLACEHOLDER_RE, (placeholder) => {
    if (!Object.prototype.hasOwnProperty.call(replacements, placeholder)) {
      valid = false;
      return placeholder;
    }
    return replacements[placeholder];
  });
  return valid ? rendered : "";
}

function encodePromptData(value: string) {
  // JSON string 保留換行、反斜線與 `$&` 等原文；跳脫 angle brackets 避免關閉資料區標籤。
  return JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026");
}

export function buildExternalAgentPrompt({
  template,
  bundle,
  mode,
  requirement,
  projectContext
}: {
  template: PromptTemplate;
  bundle: PromptTemplateBundle;
  mode: PromptTemplateMode;
  requirement: string;
  projectContext: string;
}) {
  if (!isPromptTemplateBundleSupported(bundle)) return "";

  const outputName = mode === "goal" ? "goal.md" : "plan.json";
  const requirementText = requirement.trim();
  if (!requirementText) {
    return renderPromptResource(bundle.missing_requirement, {
      "<<OUTPUT_NAME>>": outputName
    });
  }

  return renderPromptResource(bundle.base, {
    "<<OUTPUT_NAME>>": outputName,
    "<<ORIGINAL_REQUIREMENT_JSON>>": encodePromptData(requirementText),
    "<<PROJECT_CONTEXT_JSON>>": encodePromptData(projectContext.trim() || bundle.default_context),
    "<<TEMPLATE_LABEL_JSON>>": encodePromptData(template.label),
    "<<TEMPLATE_DESCRIPTION_JSON>>": encodePromptData(template.description),
    "<<TEMPLATE_INSTRUCTIONS_JSON>>": encodePromptData(template.instructions),
    "<<MODE_CONTRACT>>": mode === "goal" ? bundle.goal : bundle.plan
  });
}

export function promptDownloadName(template: PromptTemplate, mode: PromptTemplateMode) {
  // 非安全字元轉成連字號，避免模板 id 產生意外路徑或空檔名。
  const safeId = template.id.replace(/[^a-z0-9._-]+/gi, "-").replace(/^-+|-+$/g, "") || "custom";
  return `${safeId}-${mode}-prompt.md`;
}

export function downloadPromptFile(content: string, filename: string) {
  // 以暫時 Blob URL 觸發本機下載，完成後立即撤銷 URL。
  const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
