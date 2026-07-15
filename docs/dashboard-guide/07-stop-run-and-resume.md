# 流程 07：停止、一般執行與 Resume

## 目的

依情境選擇平順停止、緊急停止、一般續跑或保留中斷現場 Resume，避免為了「續跑」不必要地略過安全檢查。

## 一張表先選對操作

| 情境 | 使用 |
|---|---|
| 正常維護、想讓目前 round 完整收尾 | 本輪後停止 |
| 已要求平順停止，但 loop 還沒接手且想取消 | 繼續運行 |
| Agent CLI 卡死、失控、跑錯危險命令 | 立即停止 |
| 停止後工作樹乾淨、Validate 可過 | 運行 → 一般執行 |
| 明確要保留中斷中的髒工作樹，且有可信綠點 | 運行 → Resume |

## A. 本輪後停止（正常首選）

1. Workspace 正在運行時按「本輪後停止」。
2. 按鈕可能變成：
   - 「繼續運行」：停止請求尚未被 loop 取走，可撤銷。
   - 「本輪收尾中」：loop 已接手，這輪會完成 Agent、Validate 與 state/history 落盤後停止，不能再撤銷。
3. 等到按鈕變回「運行」，console 顯示停止完成。

這是最安全的正常停止方式，因為不會留下半輪判定。

## B. 立即停止（緊急使用）

1. 按紅色「立即停止」。
2. 目前 round 會中斷，state 落地；下次可續用。
3. 檢查 target repo 工作樹，因為 Agent 可能停在尚未 commit／尚未 Validate 的中間狀態。

人工立即中斷的未完成輪不寫入完整 history，因此不納入未回 DONE 異常分母／分子。畫面計時會凍結在中斷時間。

## C. 停止後按「運行」

按「運行」會先出現選擇視窗，不會立刻執行：

![一般執行與 Resume 完整標註](../assets/dashboard-guide/annotated/run-choice.jpg)

### 一般執行

會執行：

- 完整 Preflight。
- 啟動 Validate。
- dirty tree、Goal／Plan／protected snapshot、HEAD 基準與單 writer lock 等檢查。

適用：工作樹乾淨、可正常驗證，或你不確定是否需要 Resume。一般執行應是預設答案。

### Resume 現場

會保留目前 code repo 現場，並略過啟動：

- dirty-tree 檢查。
- 啟動 Validate。
- HEAD 祖先檢查。
- protected snapshot 比對。

仍會保留：Git／單 writer lock、workspace identity；下一輪結束後仍須照常 Validate。

Resume 只驗證兩個欄位：

- 執行開始時間：必須是有效時間且早於現在。
- 綠點 commit SHA：必須存在於這個 target repo。

目前 protected 檔案會成為後續輪次的新防竄改基準，所以填錯綠點或在不理解現場時 Resume，可能把不應接受的狀態當成新基準。

## Resume 前必要檢查

在 target repo 執行：

```bash
git status --short
git rev-parse HEAD
git show --no-patch --oneline <綠點-SHA>
```

再確認：

- 工作樹變更確實屬於目前 task，而不是無關殘留。
- 綠點是中斷前最近一次可信、Validate 通過的 commit。
- 開始時間對應中斷 round，且早於現在。
- 沒有另一個 loop process 使用同一 Git worktree。
- 你接受略過啟動 Validate；若其實能先清理並驗證，改用一般執行。

## 一般執行失敗後，不要立刻改 Resume

先依錯誤處理：

- dirty tree：判斷變更來源，完成／commit 或清理不需要的內容。
- Validate 失敗：在 target repo 手動跑同一命令，修正依賴或測試。
- Goal 變更：查看 Goal diff，通常回規劃期重新收斂。
- lock 衝突：找出原 writer，不要用 Resume 繞鎖。
- protected snapshot 不一致：查明被改動的真相檔，不能用 Resume 當作消除警示的捷徑。

## 操作完成判定

- 平順停止：console 顯示本輪完整收尾，workspace 變 idle。
- 立即停止：console 記錄人工中斷，計時凍結，target repo 現場已人工核對。
- 一般執行：console 顯示 preflight 與啟動 Validate 通過，再開始新輪。
- Resume：console 明示「Resume 中斷現場」與沿用的綠點，下一輪結束仍有 Validate。

下一步：需要調整任務時看 [編輯 Plan 與切換任務](08-edit-plan-and-change-task.md)。
