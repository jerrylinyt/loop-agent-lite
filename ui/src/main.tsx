/** React 入口：掛載唯一 App root；StrictMode 用來提早暴露副作用清理問題。 */
import React from "react";
import { createRoot } from "react-dom/client";
import App from "./app/App";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
