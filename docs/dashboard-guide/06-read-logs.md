# 流程 06：讀懂 Loop 狀態與 Agent 執行紀錄

## 目的

用正確的 console 找到「coordinator 做了什麼」與「Agent 實際輸出了什麼」，並用來源篩選與文字過濾快速定位錯誤。

## 兩個 Console 的分工

| Console | 內容 | 最適合回答 |
|---|---|---|
| Loop 狀態紀錄 | Coordinator 事件、preflight、Validate、共識、task 切換、停止、reset、Dashboard 人工操作 | 為什麼進到這個階段？Validate 是否通過？為什麼停止／reset？ |
| Agent 執行輸出 | Agent stdout／stderr、工具呼叫、命令結果、回覆 | Agent 做了哪些檔案與命令？在哪一步失敗？ |

兩者都來自 workspace 的 `console.log` 投影，但預設來源篩選不同。完整原始資料仍保存在 workspace 日誌邊界內。

## A. 讀 Loop 狀態紀錄

先從時間順序找以下節點：

1. `新的 loop session`。
2. `Loop 啟動` 與 repo 路徑。
3. `啟動前檢查`／`Resume 中斷現場`。
4. `恢復進度`：階段與 round。
5. `執行設定`：Agent、Validate。
6. `收斂門檻`：flag、done、reset、timeout。
7. `第 N 輪開始`：階段與 task。
8. `Agent 結束`：exit code 與耗時。
9. `執行驗證` 與通過／失敗。
10. `第 N 輪結束`：變更、驗證、signal、事件。

診斷時不要只複製最後一行；至少保留「輪開始→Agent 結束→Validate→輪結束」的完整區間。

## B. 讀 Agent 執行輸出

右側可切：

- Agent：只看 Agent 來源，日常最清楚。
- 其他：排除 Agent，偏 coordinator／Dashboard 訊息。
- 全部：需要重建完整時序時使用。

Agent ANSI 色碼會直接上色。看到命令 `succeeded` 不代表整個 task 已完成；仍要看之後的 Validate、工作樹狀態與 done 共識。

## C. 使用「過濾…」

兩個 console 都可輸入文字搜尋。過濾會保留包含該文字的完整行，不修改原始 log。

常用搜尋詞：

```text
錯誤
失敗
timeout
Validate
exit code
task-3
reset
issue
permission
not found
```

無結果時會顯示「沒有符合過濾條件的行」。先清空搜尋，再確認是否選錯 Agent／其他來源。

## D. 跟到最新與保留閱讀位置

當捲動停在最底部附近，console 會自動跟隨新輸出。往上閱讀後，自動跟隨會暫停並出現「跟到最新」；按它才跳回尾端。這避免新 log 不斷把你從正在看的錯誤推走。

狀態：

- `live`：workspace 正在運行。
- `idle`：目前停止；不表示 workspace 完成。

## E. 收合與調整版面

- 點「收合」只隱藏 pane，不停止 loop。
- 點收合後的標題可展開。
- 拖曳水平分隔線調 Loop 狀態高度。
- 拖曳垂直分隔線調任務區與 Agent console 欄寬。
- 大小與收合設定保存在瀏覽器。

## F. Console 尾段與長期稽核的差別

- Dashboard 單次 console 投影只保留最新 64 KiB，前端累積尾段也按完整行截斷。
- `console.log` 單檔有輪替上限；歷史輪判定在 `history.log`。
- 每輪 Agent 原始輸出另存 `logs/`。
- 最近 prompt 稽核副本在 `prompts/`。
- 異常輪若在功能啟用後發生，可能另保留最多 2 MiB 的異常 log 尾段。

因此「右側看不到很早以前的行」不等於從未發生；改看輪次紀錄、時間軸或 workspace 檔案。

## 常見判讀錯誤

- Agent 說「完成」就當作 task 完成：要以 Validate、乾淨工作樹、done signal 與共識為準。
- Validate 通過就當作 Agent 有回 DONE：兩者分開，未回 DONE 仍會算異常。
- `idle` 當成完成：idle 只代表沒有 process 正在跑。
- 只看 Agent console、不看 Loop console：會漏掉 preflight、Validate、reset 與 coordinator 判定。
- 搜尋後忘了清除：會誤以為 log 消失。

## 問題回報時應附的資料

- Workspace 名稱、phase、round、task。
- 問題發生的本機時間。
- Loop 狀態從該輪開始到結束的片段。
- Agent 錯誤附近片段。
- Validate 命令、exit code／timeout。
- 是否一般執行或 Resume。
- `git status --short` 與目前 HEAD（在 target repo 執行）。
- 不要貼 token、密鑰、cookie 或私有 URL 參數。

下一步：[停止、一般執行與 Resume](07-stop-run-and-resume.md)。
