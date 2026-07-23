# 流程 05：監看單一 Workspace

## 目的

讀懂一個 workspace 現在在哪個階段、正在做哪個 task、是否健康、是否接近 timeout，以及下一個安全操作是什麼。

## 進入方式

- 從 Fleet 總覽點 workspace 卡片。
- 點頂部 workspace 頁籤。
- 用 `⌘K`／`Ctrl+K` 搜尋名稱。
- 按 `⌘G`／`Ctrl+G` 後，在 1.5 秒內按 `1`～`5` 切到前五個 workspace。

![Workspace 詳細頁完整標註](../assets/dashboard-guide/annotated/workspace.jpg)

## 建議閱讀順序

### 1. 看名稱、階段與是否正在運行

標題旁 badge：

- 規劃期：Agent 應建立／確認 plan。
- 執行期：Agent 應處理目前 task。
- 完成：所有 task 已收斂，可看完成報告。

「階段」與「running」是兩件事：執行期也可能已停止；規劃期也可能正在跑。按鈕顯示「運行」代表 process 目前已停止，顯示「立即停止」代表正在運行。

### 2. 看主要狀態 chips

- Goal：開目前 `goal.md`；若停機期間 Goal 變更，視窗可顯示基準差異。
- round N：coordinator 已開始／完成到第幾輪。
- 任務 X/Y：已完成 task 數／總 task 數，不是目前 task 編號。
- flag X / >N：只在規劃期；必須大於門檻才收斂。
- done X / ≥N：只在執行期；達門檻才完成目前 task。
- 規劃後暫停：計畫收斂後不自動跑執行期，需人工按「運行」。
- 完成報告：只在完成階段出現，開啟 `REPORT.md`。

### 3. 看頂部健康色帶與輪次趨勢

頁面最上方細色帶是健康度：取「紅連跳／red limit」與「停滯／stall limit」兩者中較接近門檻的一個。越紅越接近 reset 防線；完成 workspace 顯示健康完成狀態。

輪次迷你趨勢：

- 綠：Validate 通過。
- 紅：Validate 失敗或受保護內容異常。
- 灰：規劃輪。
- 橙：reset。

點趨勢會開輪次紀錄。

### 4. 看健康相關 chips

- 紅連跳 N：連續紅燈輪數。
- 停滯 N：HEAD 沒有進展的輪數。
- plan vN：計畫版本；規劃期版本達 10 以上會警告可能震盪。
- Agent 異常 N · X 秒後重試：CLI 連續失敗與目前退避。
- round 計時：執行中每秒更新 elapsed／剩餘時間；最後 60 秒轉警示。手動中斷會凍結時間。
- 上輪 X 秒 · 逾時：上一輪耗時與是否 timeout。
- state 復原 N：主 state 曾由 checkpoint 復原。
- 正從 checkpoint 唯讀顯示：主檔有問題，畫面暫以 checkpoint 顯示；先查 log，不要直接寫 state。
- 警告：PID 殘留：state 記得 PID，但程序不存在；需確認 process 與 lock 狀態。
- 警告：issues U/T：U 個未讀／共 T 個人工議題。

### 5. 看任務表

- 完成列：可展開；右側 SHA 點下看該 task 的 Git Diff。
- 目前任務：反白並標「進行中」。
- 等待任務：尚未開始。
- 「前往」：人工跳到該 task；會先列出哪些 task 被人工標完成並執行 Validate，屬高風險操作。
- 「編輯計畫」：停止狀態下打開全畫面 Plan 編輯器。

任務完成數、目前 task 編號與 done 共識不是同一數值，不要互相替代解讀。

### 6. 看觀測入口

- 輪次紀錄：最近最多 N 輪客觀指標與逐輪判定（N 依 Dashboard 設定，預設 1000）。
- 時間軸：合併歷史輪次、異常與 Dashboard 人工操作。
- ⇄ Run 對比：目前 run 對上一個 run 的樣本、耗時與異常比較。
- Prompt：最近一輪實際送入 Agent 的完整 prompt。
- Issues：Agent 明確回報等待人類決策時出現。
- Goal／Prompt／紀錄／時間軸／Run 對比都是唯讀。

## 何時需要人工介入

| 畫面現象 | 建議 |
|---|---|
| 短暫一輪紅燈後恢復 | 先觀察下一輪與 Validate 訊息。 |
| 紅連跳持續增加 | 讀 Loop 狀態與 Agent 輸出，確認測試、protected file 或工具錯誤。 |
| 停滯持續增加 | 檢查 Agent 是否反覆得出相同結論、task 是否不可行、DoD 是否含人工決策。 |
| Agent 異常與退避 | 先測 CLI、PATH、權限、網路／配額與命令參數。 |
| Issues 未讀 | 停止後進入 Issues，由人做決策；不要讓 Agent代替人決定。 |
| Goal 已變更 | 點 Goal 看差異，通常回規劃期重新收斂。 |
| PID 殘留／state 復原 | 先查 console 與 process，避免第二個 writer。 |
| round 倒數最後 60 秒 | 觀察是否正常收尾；不要只因接近 timeout 就立刻 kill。 |

## 完成檢查

- [ ] 能分辨 phase 與 process running 狀態。
- [ ] 知道目前 task、完成任務數、flag／done 各代表什麼。
- [ ] 看過健康色帶、紅連跳、停滯與 round 計時。
- [ ] 發現警示時知道從 Loop／Agent log 開始查。

下一步：[讀懂 Loop 與 Agent 紀錄](06-read-logs.md)。
