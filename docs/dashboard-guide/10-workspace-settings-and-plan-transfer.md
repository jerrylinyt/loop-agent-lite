# 流程 10：修改 Workspace 設定與轉移 Plan

## 目的

在 workspace 停止時調整下一次運行的 Agent、Validate、收斂與 reset 防線；或匯出純 Plan、以完整重置方式匯入另一份 Plan。

> 本頁的「設定」「匯出」「匯入並完整重置」只適用普通 Loop。Parallel base 的 run config 與 frozen plan 是 immutable artifacts；managed worker 也唯讀。Parallel 的新設定／Plan 必須透過 Launcher 建立新 run。

## 進入方式

1. 確認是普通 Loop，並正常停止 workspace。
2. 按「設定」。

![Workspace 設定完整標註](../assets/dashboard-guide/annotated/workspace-settings.jpg)

## A. 修改 Agent 命令

- 下拉第一項「保持不變」可避免誤覆寫既有命令。
- 選其他已設定 CLI，會從下一次運行生效。
- 「管理」開個人 CLI／PATH 設定；變更可能影響其他 workspace 的可選命令。
- 換 Agent 前應先用管理器執行測試。

## B. 修改 Validate 命令

1. 輸入完整命令。
2. 按「執行確認」。
3. 讀 exit code／timeout 與輸出尾段。
4. 成功後才儲存設定。

Validate 是每輪綠紅判定，不應為了讓 loop 看起來通過而把它改成空命令、`true` 或只測一小部分。應能代表 Goal 的實際 DoD。

## C. 修改收斂與時間參數

| 欄位 | 判定 | 調低的效果 | 調高的效果 |
|---|---|---|---|
| flag 收斂（>） | flag 必須嚴格大於值 | 更快離開規劃期、較少獨立確認 | 更多規劃共識、較慢 |
| done 收斂（≥） | done 達值 | 更快完成 task、較少獨立確認 | 更多完成確認、較慢 |
| 單輪上限（分） | Agent round timeout | 長任務較易被中斷 | 卡住時等待更久 |
| Agent 異常退避上限（秒） | 連續 CLI 失敗的最大 backoff | 重試更密集 | 降低失敗風暴但恢復較慢 |
| Validate 上限（秒） | validator timeout | 慢測試較易逾時 | 卡住 validator 更晚被殺 |

沒有團隊共識時，不要只為「跑快一點」降低 flag／done。

## D. 修改 Reset 防線

- 紅燈連跳 reset：連續驗證紅燈達門檻時回復最近綠點。
- HEAD 停滯 reset：Git HEAD 無進展達門檻時觸發防線。

門檻太低可能打斷合理的多輪修正；太高則讓無效迴圈持續很久。先看實際 P95、任務大小與常見修復輪數再調。

## E. 規劃收斂後暫停

勾選後，規劃收斂會停在執行期起點，不自動跑 task。適合：

- 高風險 repo。
- 新團隊第一次使用。
- Plan 必須由人審核相依順序／DoD。
- 需要先用 Plan 編輯器調整 pending tasks。

## F. 匯出 Plan

按「匯出 plan.json」會下載 `<workspace>.plan.json`，只包含：

```json
[
  { "order": 1, "task": "...", "ref": "..." }
]
```

不包含完成進度、round、issues、done／flag、SHA 或 workspace 執行設定。這是刻意的安全邊界。

用途：審查、版本保存、在另一個 workspace 匯入、用文字工具比較任務變更。

一般新建的普通 Loop Plan 不含 `stack`；匯出會忠實保留既有 Plan 的欄位，因此明確以 serial-stack 相容模式建立的 Plan 仍可能帶有 `stack`。匯出檔可作為 Parallel 基礎 Plan，但在 Parallel Launcher 前仍須由人核對 working set、生成物、依賴與共享驗證資源，再人工加入或重新確認合法、連續的 stack；不確定就不標。

## G. 匯入並完整重置

1. 先匯出現有 Plan 作備份。
2. 按「匯入並完整重置」選 JSON。
3. 前端先驗證純 Plan schema。
4. 在確認視窗核對檔名、task 數、清除與保留項目。
5. 按「完整重置並匯入」。

會清除：round、completed、current task、issues、done／flag、舊 run 產物。

會保留：workspace 執行設定與 target repo 程式碼。

匯入後：plan v1、規劃期，可人工按「進執行期」。這項操作無法復原；匯入檔中的任何完成欄位都不會採用。

普通 Loop 匯入只接受 `order`／`task`／選填 `ref`。含 `stack` 的檔案會被拒絕，避免把 Parallel batch 靜默當成串行。請改到 Parallel Launcher 匯入；不要刪除 stack 後假裝語意相同。

## Parallel Plan／設定轉移

- 既有 Parallel run 即使 `paused`／`blocked` 也不能改 Agent、Validate、門檻、最大 workers、Plan、stack 或 batch。
- 若只需恢復同一 run，用 Resume；它沿用原 immutable run config 與 frozen plan。
- 若要變更 Goal、設定或 Plan，先讓舊 run 安全完成或 Abort，保留需要的 plan／report／logs，再以新 workspace 名稱從 Parallel Launcher 建立新 run。
- Frozen plan 的 `stack` 只可由 Parallel Launcher 接受；相同 stack 必須位於同一段連續 orders，未標 stack 的 task 自成 singleton batch。

## H. 儲存設定

按「儲存設定」後，從下一次運行生效。儲存本身不會自動啟動 loop。回到詳細頁後再按「運行」，通常選一般執行，確認新 Validate 的啟動檢查也能通過。

## 完成檢查

- [ ] Workspace 已停止。
- [ ] 已確認是普通 Loop；Parallel 變更應建立新 frozen run。
- [ ] 新 Agent 已測試。
- [ ] 新 Validate 已按「執行確認」且 exit 0。
- [ ] 門檻調整有具體理由。
- [ ] Plan 匯入前已備份並讀完清除預覽。
- [ ] 已按「儲存設定」，且知道不會立即啟動。

相關：[欄位與控制項完整說明](fields-reference.md)。
