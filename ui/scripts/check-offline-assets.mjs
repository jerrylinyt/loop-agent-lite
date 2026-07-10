import { readFile, readdir } from "node:fs/promises";
import { join } from "node:path";

const dist = new URL("../dist/", import.meta.url);
const index = await readFile(new URL("index.html", dist), "utf8");
const assetNames = await readdir(new URL("assets/", dist));
const cssFiles = await Promise.all(
  assetNames.filter((name) => name.endsWith(".css")).map((name) => readFile(join(dist.pathname, "assets", name), "utf8"))
);

const externalHtmlAsset = /<(?:script|link|img)\b[^>]*(?:src|href)=["'](?:https?:)?\/\//i.test(index);
const externalCssAsset = cssFiles.some((css) => /url\(\s*["']?(?:https?:)?\/\//i.test(css));
if (externalHtmlAsset || externalCssAsset) {
  throw new Error("dist 含外部 runtime 資源；內網 build 必須全部使用本機 asset");
}
console.log(`offline assets ok: ${assetNames.length} local files`);
