# loop-agent-lite Dashboard 新手操作圖解

這套文件是給「第一次使用、還不熟悉 loop-agent-lite」的人。照著新手路線走完，你會知道如何準備 repo、設定 Agent CLI、建立 Goal／Plan、選擇普通 Loop 或 Parallel Loop、讀懂每個狀態、處理中斷，以及安全地修改或刪除 workspace。

> 圖片來源：2026-07-15 直接操作本機實際 Dashboard 擷取，範例 workspace 為 `dryrun-lab`。圖上的數值、路徑與任務是範例，你的畫面會依 repo、runner 與執行狀態不同；較早的截圖可能尚未顯示後來加入的 `Parallel Loop` runner 分頁。橘色箭頭與中文標籤是文件說明層，底下的 Dashboard 像素沒有重製。

![Fleet 總覽完整標註](../assets/dashboard-guide/annotated/overview.jpg)

## 第一次使用：請依這個順序

1. [安裝並啟動 Dashboard](00-install-and-start.md)
2. [完成第一次個人設定](01-first-time-personal-settings.md)
3. [準備 Goal 與 Plan](02-prepare-goal-and-plan.md)
4. [啟動新的 loop](03-launch-new-loop.md)
5. [用 Fleet 總覽找出需要注意的 workspace](04-monitor-fleet-overview.md)
6. [進入 workspace 監看任務與健康度](05-monitor-workspace.md)
7. [讀懂 Loop 與 Agent 紀錄](06-read-logs.md)
8. 遇到中斷時看 [停止、一般執行與 Resume](07-stop-run-and-resume.md)

## 依操作目的查文件

| 你想做什麼 | 文件 |
|---|---|
| 安裝、找網址、確認 Dashboard 有啟動 | [00 安裝並啟動](00-install-and-start.md) |
| 設定 Agent CLI、GUI PATH、Repo Roots、終態通知 | [01 第一次個人設定](01-first-time-personal-settings.md) |
| 撰寫或產生 `goal.md`、匯入 `plan.json`、人工標註 Parallel `stack` | [02 準備 Goal 與 Plan](02-prepare-goal-and-plan.md) |
| 選擇普通／Parallel runner，新建或重建 loop | [03 啟動新的 loop](03-launch-new-loop.md) |
| 同時監看所有 workspace、篩選警示、批次操作 | [04 Fleet 總覽](04-monitor-fleet-overview.md) |
| 看單一 workspace 的階段、進度、健康度與任務 | [05 Workspace 監看](05-monitor-workspace.md) |
| 篩選 console、分辨 Loop 紀錄與 Agent 原始輸出 | [06 讀取紀錄](06-read-logs.md) |
| 平順停止、立即停止、續跑或保留現場 Resume | [07 停止與 Resume](07-stop-run-and-resume.md) |
| 編輯 pending tasks、插入／排序／刪除、跳任務 | [08 編輯 Plan 與切換任務](08-edit-plan-and-change-task.md) |
| 從執行期回規劃期、從規劃期進執行期 | [09 切換階段](09-change-phase.md) |
| 改 Agent／Validate／門檻、匯出或重置匯入 Plan | [10 Workspace 設定與 Plan 轉移](10-workspace-settings-and-plan-transfer.md) |
| 查輪次、時間軸、異常、Run 對比與 Git Diff | [11 歷程、異常與差異檢視](11-history-anomalies-and-diff.md) |
| 處理 Agent 回報的人工決策問題 | [12 Issues 與人工介入](12-issues-and-human-intervention.md) |
| 複製設定建立新 workspace、查啟動 job | [13 範本啟動與 Jobs](13-template-launch-and-jobs.md) |
| 永久刪除 workspace | [14 刪除 Workspace](14-delete-workspace.md) |
| 快捷鍵、主題、欄寬、收合與可及性操作 | [15 快捷鍵與版面](15-shortcuts-theme-and-layout.md) |
| 查每一個欄位、按鈕、狀態 chip 的用途 | [欄位與控制項完整說明](fields-reference.md) |
| 啟動失敗、CLI 找不到、Validate 紅燈等 | [疑難排解](troubleshooting.md) |

## 先記住的安全原則

- 正常停止優先用「本輪後停止」；只有 Agent 明顯卡死或失控才用「立即停止」。
- 一般續跑優先選「一般執行」；只有確定要保留髒工作樹／中斷現場時才用 `Resume`。
- `Resume` 會略過啟動 Preflight 與 Validate，風險高於一般執行。
- 「回規劃期」、「跳到 task-N」、「匯入並完整重置」與「永久刪除」都會先顯示影響預覽；逐列讀完再確認。
- 刪除 workspace 不會刪 target repo，但會永久刪除該 workspace 的 state、history、logs、prompts、snapshots 與 REPORT。
- 同一個 Git worktree 同一時間只能有一個 loop writer；不要從兩個 Dashboard 同時跑它。
- Parallel 的 planner／Plan Prompt 不會替你判斷 `stack`；只有人類核對 working set、依賴與共享驗證資源後才能標註。不確定就不標，讓任務串行。
- Parallel 的 managed worker 是 parent supervisor 管理的唯讀 workspace；Pause、Resume、Abort、重試收尾或刪除都從 Parallel base workspace 操作。
- 不要手動修改 `workspace/*/state.json`、`state.last-good.json`、計畫真相或受保護的 Goal；請使用 Dashboard 提供的操作。

## 畫面上的狀態詞

| 詞語 | 新手版解釋 |
|---|---|
| 規劃期 | Agent 正在建立／確認任務計畫；以 `flag` 共識判定計畫是否收斂。 |
| 執行期 | Agent 逐項處理 task；以 `done` 共識判定目前 task 是否完成。 |
| 完成 | 所有 task 都已收斂，workspace 產生 `REPORT.md` 並停止。 |
| Parallel Loop | 由 base supervisor 依 frozen plan 派出受管 workers；每個 worker 仍使用原生 loop 收斂。 |
| `stack` | 人工加在 `plan.json` task 上的正整數分組；相同值且連續的 tasks 形成同一個 batch。 |
| batch | Parallel 的排程單位；batch 依 order 串行，只有同一 batch 內的多個 tasks 才可能並行。未標 `stack` 的 task 自成一批。 |
| Managed Worker | Parallel parent 建立的單一 task 執行 workspace；畫面只提供狀態、歷史、console 與唯讀 frozen task。 |
| 綠點 | 最近一次合法、Validate 通過且可回復的 Git commit。 |
| 紅連跳 | 連續驗證失敗／受保護內容異常的輪數；接近門檻時健康色帶變紅。 |
| 停滯 | HEAD 沒有前進的輪數；到門檻會觸發 reset 防線。 |
| 未回 DONE | Agent 輪次結束，但沒有送出該階段預期的完成 signal；有 Git 變更仍算異常。 |
| workspace | loop-agent-lite 保存 coordinator state、history 與 logs 的資料目錄；不是 target repo。 |
| target repo | Agent 實際讀寫與 commit 程式碼的 Git repository。 |

下一步：[安裝並啟動 Dashboard](00-install-and-start.md)。
