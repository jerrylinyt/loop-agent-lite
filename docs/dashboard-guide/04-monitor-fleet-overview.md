# 流程 04：用 Fleet 總覽監看所有 Workspace

## 目的

在不逐一進入 workspace 的情況下，先回答三個問題：哪些正在跑、哪些需要人處理、整體輪次是否變慢或出現未回 DONE。Fleet 同時支援普通 Loop、Ralph 與 Parallel base workspace；Parallel 的 managed workers 不重複計入 Fleet。

## 進入方式

點右上角「總覽」，或按 `⌘K`／`Ctrl+K` 搜尋「開啟 Fleet 總覽」。

![Fleet 總覽完整標註](../assets/dashboard-guide/annotated/overview.jpg)

## 建議閱讀順序

### 1. 先看頂部五張摘要卡

1. `workspaces`：Dashboard 目前載入的頂層 workspace 數。Parallel managed worker 由 parent 管理，不列入這個數字。
2. `執行中`：目前正在跑的頂層 runner 數；Parallel 以 base supervisor 為一個 runner。
3. `規劃 / 執行 / 完成`：各階段分布。
4. `需要關注`：普通 Loop 的未讀 issues、state／Goal／PID／checkpoint／Agent 異常，或 Parallel base 的 `blocked`／`parallel.error` 等需要處理的 workspace。
5. `任務完成`：所有頂層 workspace 已完成 task／總 task 與百分比。Parallel 的完成數來自 supervisor 已接受的 integration receipts，不是 worker 自行宣稱的 done。

如果「需要關注」不是 0，先按「需關注」篩選，不要只看完成百分比。

### 2. 再看跨工作區輪次效能

此卡把所有 workspace 依時間合併，取最新最多 500 個已結束輪次：

- 平均：全部樣本平均耗時，容易受極慢輪影響。
- P50：一半輪次比此值快，較接近日常體感。
- P95：95% 輪次不超過此值，用來找尾端延遲。
- 最慢：樣本中的最大耗時。
- 逾時：已知逾時輪占比。
- 未回 DONE：Agent 已結束，但沒送出該階段預期完成 signal 的輪數。
- 異常率：未回 DONE／納入統計的輪次；人工立即中斷不計。

不要把 P95 當成「95% 成功率」。它是耗時百分位。

### 3. 點「未回 DONE」看異常輪

![異常輪完整標註](../assets/dashboard-guide/annotated/anomalies.jpg)

操作：

1. 點效能卡的「未回 DONE N 次／查看」。
2. 左側選一個 workspace／round。
3. 讀階段、task、signal 與 Git 狀態。
4. 如果有保留 Agent log，右側會顯示；若顯示「無歷史 log」，代表無法回補，不要猜測內容。

Git 有變更但 Agent 沒回完成 signal 仍算異常，因為 coordinator 不能只靠檔案變更推定任務完成。

### 4. 用篩選、搜尋與排序縮小範圍

- 全部：顯示所有卡片。
- 需關注：只看有目前告警／人工待辦的 workspace。
- 執行中：只看 `running=true`。
- 已完成：只看 phase done。
- 搜尋：依 workspace 名稱過濾。
- 排序：名稱、需關注優先、執行中優先、完成度優先。
- 精簡卡片：縮小每張卡片資訊，適合大量 workspace 電視牆。

篩選選擇會保存在瀏覽器；下次覺得「卡片怎麼不見」時，先檢查目前篩選與搜尋字串。

### 5. 讀單張 Workspace 卡片

卡片通常包含：名稱、階段、round、flag／done、計時、任務進度、目前 task、近 100 輪效能、警示原因與 repo 路徑。點卡片進入詳細頁。

已完成 workspace 的歷史紅燈／停滯不會被當成目前告警；但未讀 issues、state 復原、Goal 變更、stale PID 或 state 錯誤仍可能需要關注。

Parallel base 卡片另有：

- `Parallel` badge。
- durable run status，例如 `initializing`、`running`、`paused`、`blocked`、`completed` 或 `cancelled`。
- 目前 batch；不同 batch 仍依序執行，只有同一 batch 內多個 task 才可能並行。
- `Parallel blocked` 或完整 Parallel error。這兩種狀況會進入「需關注」。

Parallel base 的 phase 只是既有 Dashboard schema 的投影：完成 run 才是 `done`；暫停、阻擋或取消中的 run 通常仍投影為 `exec`。判讀時應優先看 `Parallel` status，不要只看 phase badge。Managed worker 仍可出現在頂部 workspace 分頁供診斷，但不出現在 Fleet 卡片、摘要或健康總數，避免一個 run 被重複計算。

### 6. 讀事件推播

右側最近事件依時間顯示任務開始、完成、規劃收斂、驗證轉紅等。點事件可切入相關 workspace。它適合快速掌握變化，不取代完整 history。

## 批次操作

![Fleet 批次操作實際畫面](../assets/dashboard-guide/raw/overview-bulk.jpg)

1. 點「批次操作」。
2. 多選 workspace。選單會顯示 ordinary 的執行／停止狀態，Parallel 則顯示 durable run status。
3. 選「Issues 已讀」或停止操作：
   - 全是普通 workspace：按鈕是「立即停止」。
   - 全是 Parallel base：按鈕是「Pause」。
   - 兩者混選：按鈕是「停止 / Pause」。
4. 讀確認預覽：普通 workspace 只有目前 `running=true` 才會立即停止；Parallel 只有 `initializing`／`running` 才會送出 typed Pause。已 `pause_requested`、`paused`、`blocked` 或終態的 Parallel 會自動跳過，需進 base 詳細頁處理。
5. 確認後再送出。Dashboard 逐 workspace 呼叫原本的安全 API；單筆失敗不會回滾或阻止其他項目。

普通 workspace 的批次「立即停止」是緊急操作，不是日常結束方式。Parallel 的 `Pause` 則是正常控制：停止新派工，讓 workers 在安全邊界停下並保留未整合現場供 Resume；它不等同直接 kill workers。

## 每日巡檢建議

- [ ] 「需要關注」是否為 0；若不是，逐項處理原因。
- [ ] 執行中數量是否符合預期。
- [ ] P95 是否突然高於平常。
- [ ] 未回 DONE 與異常率是否增加。
- [ ] 是否有同一 task 長時間沒有開始／完成事件。
- [ ] 已完成比例是否合理前進。
- [ ] Parallel 卡片是否停在 `pause_requested`／`finalizing*`，或出現 `blocked`／`parallel.error`。
- [ ] 判讀 Parallel 時是否看 durable status，而不是只看 phase／running。

下一步：[監看單一 Workspace](05-monitor-workspace.md)。
