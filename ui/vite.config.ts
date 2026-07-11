/** Vite 僅負責編譯 React 與開發期代理；production 靜態檔由 Python Dashboard 提供。 */
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    // Production assets 跟著 engine package 安裝，`loop dashboard` 不依賴 checkout 的 ui/ 路徑。
    outDir: "../engine/ui",
    emptyOutDir: true
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765"
    }
  }
});
