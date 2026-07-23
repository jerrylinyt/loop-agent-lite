/** 檢查 production HTML/CSS 不引用外部網路資產，確保 Dashboard 可在離線環境執行。 */
import { readFile, readdir } from "node:fs/promises";

const dist = new URL("../../engine/ui/", import.meta.url);
const index = await readFile(new URL("index.html", dist), "utf8");
const assets = new URL("assets/", dist);
const assetNames = await readdir(assets);
const cssFiles = await Promise.all(
  assetNames.filter((name) => name.endsWith(".css"))
    .map((name) => readFile(new URL(name, assets), "utf8"))
);

const externalHtmlAsset = /<(?:script|link|img)\b[^>]*(?:src|href)=["'](?:https?:)?\/\//i.test(index);
const externalCssAsset = cssFiles.some((css) => /url\(\s*["']?(?:https?:)?\/\//i.test(css));
if (externalHtmlAsset || externalCssAsset) {
  throw new Error("dist 含外部 runtime 資源；內網 build 必須全部使用本機 asset");
}
console.log(`offline assets ok: ${assetNames.length} local files`);
