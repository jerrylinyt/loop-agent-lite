# 流程 12：處理 Issues 與人工介入

## 目的

當 Agent 判斷任務含不可自行決定的 human gate、描述錯誤或不可行條件時，透過結構化 Issues 讓人做決策，保留稽核紀錄，再安全恢復 loop。

普通 Loop 使用 Issues；Parallel managed worker 不會寫 ordinary Issues，而是以 terminal `block --reason` 把 assigned task 與 parent run 置為 blocked。兩條處理流程不可混用。

## Issues 何時出現

以下 Issues chip、標記已讀與清空流程只適用普通 Loop。

Agent 可透過 coordinator 的 issue 命令回報結構化問題。常見情況：

- 需求有互斥選項，需要產品／架構決策。
- 缺少密鑰、外部權限或人工核准。
- Task 描述與 Goal 衝突。
- DoD 不可能達成或參考文件錯誤。
- 涉及不可由 Agent 代替人的風險取捨。

Issues chip 顯示：

- `警告：issues U/T`：U 條未讀、總共 T 條。
- `issues T（已讀）`：目前全已讀，但稽核資料仍保留。

只有未讀 issues 會讓 Fleet 顯示需關注；後續新 round 的 issue 仍會再次成為未讀。

## 操作流程

### 1. 停止 Workspace

正常情況用「本輪後停止」。Issues 的標記／清空操作在 running 或唯讀模式會停用，避免執行中同時改人工狀態。

### 2. 開啟 Issues

點狀態列的 issues chip。表格欄位：

- round：哪一輪回報。
- 位置：Agent 指出的 task／檔案／步驟等位置。
- 內容：完整問題描述。
- 時間：回報時間。

新項目排在前面。單筆最多 2000 字、每輪最多 100 條，state 保留最新最多 200 條。

### 3. 做真正的人類決策

先查 Goal、Prompt、task、repo 現場與相關文件。決策結果應落在適合的真相來源：

- Goal／驗收範圍改變：修改並 commit `goal.md`，回規劃期。
- 只是 pending task 描述不清：用 Plan 編輯器補清楚。
- CLI／權限／依賴：修個人設定或 repo 環境。
- 外部核准完成：把可驗證結果寫入專案約定的文件／設定，讓 Agent 能讀取。

不要只按「已讀」卻沒有讓後續 Agent 看得到決策結果。

### 4. 標記已讀

按「標記已讀」只更新 round watermark：

- 原始 issues 全部保留。
- Fleet 不再因這批舊 issue 顯示需關注。
- 新 round 回報的新 issue 仍會未讀。

這是日常處理完成後的建議操作。

### 5. 清空全部（極少使用）

按「清空全部」會再次確認，確認後永久刪除所有 issue 紀錄，無法復原。只有資料確實錯誤、敏感或已另行完整保存時才用；一般「已處理」應標記已讀，不應清空。

### 6. 恢復運行

1. 確認決策已寫入 Agent 能看到的合法來源。
2. 若 Goal 變更，回規劃期重新收斂。
3. 若只修環境／pending task，保持適當階段。
4. 按「運行」，通常選一般執行，讓 Preflight／Validate 重新確認。

## 一個完整範例

Issue：「付款失敗後要自動退款還是人工審核，Goal 未定義。」

正確處理：

1. 由產品／風控決定「高於某金額人工審核，其餘自動退款」。
2. 修改 `goal.md` 的狀態機、限制、AC 與 DoD，commit。
3. 停止 workspace，回規劃期。
4. 標記舊 issue 已讀，不清空。
5. 一般執行，讓 Plan 依新 Goal 收斂。

錯誤處理：只按已讀後直接 Resume，且沒有任何可供 Agent 讀取的決策紀錄。

## 完成檢查

- [ ] 已讀完整 issue，而不是只看數量。
- [ ] 真正的人類決策已完成。
- [ ] 結果已寫入適合的 Goal／Plan／環境來源。
- [ ] 一般情況使用「標記已讀」，保留稽核。
- [ ] 只有明確理由才永久清空。
- [ ] 恢復前選對階段與一般執行／Resume。

## Parallel：處理 managed worker block

managed worker 遇到 human gate、缺少不可自行取得的權限、任務與 Goal 衝突、未知 merge 狀態或其他不可安全繼續條件時，執行 `engine.work block --reason <原因>`。這會：

- 將 worker assignment 記為 `blocked` 並保存 `exit_reason`。
- 由 parent 將該 task Outcome 記為 `blocked`，base run 進入 `blocked`／Fleet 需關注。
- 不把它轉成 ordinary issue，也不允許 worker 等待人類後自行繼續、修改 Plan 或操作 primary branch。

處理步驟：

1. 從 Parallel base 找 `Outcome=阻擋` 的 task，讀 task error；再開 managed worker 的 history／console 與 `exit_reason`。
2. 完成真正的人類決策，寫入並 commit 合法真相來源；需要新 task／stack 時，另外準備並審核新的 frozen plan。
3. 不要只按 Resume：task outcome block 是本 run 的 terminal task 結果，不會重派。對舊 base 執行 Abort，讓 supervisor 保留已整合 commits 並安全清理未整合資源。
4. 用新 workspace 名稱啟動新的 Parallel run，沿用已修正的 repo、Goal、設定與 frozen plan。

若 base 是 `blocked`，但沒有 task 的 Outcome 為 blocked，才可能是 crash／gate／cleanup 等 recovery block；修復精確原因後可從 base Resume。仍無法證明安全時 Abort。無論哪一類，都不要直接改 base／worker state、task refs、worktrees 或 receipts。

Parallel 完成檢查：

- [ ] 已從 base 與 worker log 讀到完整 block reason。
- [ ] 人類決策已落到 committed Goal、可驗證環境或新 frozen plan。
- [ ] task outcome block 使用 Abort＋新 run；只有 recovery block 才嘗試 Resume。

相關：[疑難排解](troubleshooting.md)。
