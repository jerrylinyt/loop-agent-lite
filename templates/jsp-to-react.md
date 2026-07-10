# 模板:JSP → React 搬移(不熟的系統也能 fail-closed 展開)

> 在 loop 外的 session 使用:把「全部」變成可枚舉清單是唯一解——盤點表沒有的列,
> 就沒有「搬完」的定義。產出 goal.md + plan.json(任務清單)+ 分析文件(commit 進 repo 供 ref)交人審。
> validate cmd 建議:`npm run build && npm test -- --run && npx playwright test`

## 0. Goal(同步產出 goal.md)

把 `<舊系統路徑>` 的全部 JSP 頁面搬到 React,行為等價。
粗略即可:頁面全搬、行為等價、三層驗證綠(build / 元件測試 / e2e)。

## 1. DoD 定義(為什麼是三層)

React 編譯過照樣可以整頁白屏,所以「可以運作」的機器可驗定義是:

1. `npm run build`(含 tsc)綠 —— 等價於 mvn compile。
2. Vitest + React Testing Library 元件測試綠 —— 釘邏輯。
3. **Playwright e2e 跑在 MSW mock 後端上**綠 —— 釘「頁面真的會動」:路由渲染、表單提交、驗證觸發。
   mock 是刻意的:loop 內要確定性、不依賴真後端;對真後端的煙測屬於人工驗收,不進迴圈。

## 2. Phase 0 盤點(每支 JSP 一列)

| JSP | URL 入口 | include/taglib | scriptlet 邏輯塊 | JSTL 條件(特別是角色/權限渲染) | form 提交目標 | 內嵌 JS | session/request attributes | i18n key | 檔:行 |
|---|---|---|---|---|---|---|---|---|---|
| `<xxx.jsp>` | | | | | | | | | |

用 `find . -name '*.jsp'` 起手,一支不漏;共用 fragment(header/選單)單獨列。

## 3. Phase 1 行為規格(每頁)

- 輸入 → 輸出、驗證規則、錯誤訊息、跳轉流程。
- 證據一律 `xxx.jsp:行號`,不准憑印象。

## 4. Phase 2 拆件決策規則(寫死,執行 agent 不用猜)

- 碰 DB / session 的邏輯 → 後端 API(列出需要新開/沿用的端點)。
- 純呈現邏輯 → React 元件。
- 共用 fragment → 共用元件。
- 伺服器端驗證 → 後端保留,前端複製一份即時提示(兩邊都要列)。

## 5. Phase 3 任務化(loop 規劃期會轉成 plan JSON 並補漏)

- T01: 建 React 專案骨架 + Vitest + Playwright + MSW,三層命令全綠(空殼)
- T02: 搬共用 fragment(header/選單/layout)
- T03: 搬 `<頁面A>` —— 含該頁行為條目 × e2e 覆蓋對照表
- ...(每頁一群任務;粒度一輪做得完)

## 6. 完整性 gate

盤點表每列 → 對得到「已完成任務 + 測試檔:行(元件測試與 e2e spec)」。
對不上的列 = 沒搬完。規劃期的多輪 fresh agent 會反覆掃 JSP 樹補漏列。

## 7. 人工驗收(不進迴圈)

- 視覺還原度、UX 手感
- 對真後端的整合煙測
