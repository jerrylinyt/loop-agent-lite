/** Vite 僅負責編譯 React 與開發期代理；production 靜態檔由 Python Dashboard 提供。 */
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    // Production assets 固定輸出到專案內 engine/ui，供 Python Dashboard 離線提供。
    outDir: "../engine/ui",
    emptyOutDir: true
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765"
    }
  }
});
