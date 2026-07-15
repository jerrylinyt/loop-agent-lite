# 流程 03：啟動新的 loop

## 目的

用 Dashboard 建立一個 workspace，綁定 target repo、Goal／Plan、Agent、Validate、收斂門檻與安全選項，完成 preflight 後真正啟動 loop。

## 前置條件

- Dashboard 已啟動。
- [個人 CLI 與 Repo Roots](01-first-time-personal-settings.md) 已設定。
- Target repo 是 Git repo。
- `goal.md` 已 commit，或已準備要匯入的新 Goal。
- 已決定 Validate 命令，並知道它在 target repo 可執行。

## 進入啟動表單

點右上角「＋ 啟動／管理」→「啟動新 loop」。

![啟動表單上半部完整標註](../assets/dashboard-guide/annotated/launcher-basic.jpg)

## 步驟 1：選 Repo

1. 在 Repo 下拉選單選 target repo。
2. 如果沒有出現，點「管理」補 Code Repo Root，或選「手動輸入…」填絕對路徑。
3. 讀 Repo 狀態列：
   - `goal.md 已 commit`：最理想。
   - `工作樹 乾淨`：可跑一般 preflight。
   - `工作樹 髒`：一般啟動會被擋；先確認、commit 或處理變更。
   - `workspace「…」已存在`：這次不是全新名稱，需特別核對是否要沿用／重建。

## 步驟 2：決定 Goal 與 Plan

- `goal.md` 留空：沿用 repo 已 commit 版本。
- 選擇 goal 檔：匯入新版本。
- `plan.json` 留空：沿用既有計畫，或新 workspace 從規劃期建立。
- 貼入 Plan：建立全新 state，並選「規劃期」或「直接執行期」。

詳細規則見 [準備 Goal 與 Plan](02-prepare-goal-and-plan.md)。

## 步驟 3：填 Workspace 名稱

留空會使用 repo 目錄名。自訂名稱只允許英數、`.`、`_`、`-`，不可是 `.`、`..`，也不可用 `.` 開頭。

Workspace 名稱識別 coordinator 資料，不會替 target repo 改名。

## 步驟 4：選 Agent 命令

選擇已測試成功的 CLI。按「管理」可編輯命令與額外 PATH。不要只看名稱；確認下拉選項後半段的完整 Command 是你預期的模型與權限模式。

## 步驟 5：選 Validate 命令

Validate 是每輪判定綠／紅的客觀門檻，例如單元測試、lint＋test 或 build。

- 「執行確認」：只在 target repo 執行這條 Validate，適合快速確認命令與依賴。
- 「完整健檢」：檢查 Git、單 writer 鎖、乾淨工作樹、Goal 與 Validate；不建立 state、不啟動 Agent。
- 「手寫…」：輸入自訂命令；輸入後再執行確認。

完整健檢在待匯入 Goal／Plan、勾 reset 或建立新 branch 時會停用，因為草稿狀態尚未落地；實際啟動仍會重新驗證。

## 步驟 6：展開進階設定

![進階設定完整標註](../assets/dashboard-guide/annotated/launcher-advanced.jpg)

### 門檻與 timeout

- `flag 收斂（>）`：規劃期 flag 必須「大於」此值才收斂。例如填 2，要到 3 才通過。
- `done 收斂（≥）`：執行期 done 達到或超過此值才確認 task 完成。
- `單輪上限（分）`：一輪 Agent 的最長執行時間。
- `Agent 異常退避上限（秒）`：CLI 連續異常時，重試前退避的最大值。
- `Validate 上限（秒）`：單次驗證最長時間；逾時會終止 validator process group。

第一次使用建議先沿用團隊預設，除非你知道任務的典型執行時間與團隊共識策略。

### 三個核取方塊

- 「規劃收斂後暫停」：計畫收斂後停在執行期起點，讓人審核後再按「運行」。高風險專案建議使用。
- 「重置 workspace state」：清除舊進度重建；是重大操作，但新流程未通過 preflight 前舊 state 仍保留。
- 「在新 branch 跑」：建立 `loop/<workspace 名>` 並切換。若同時匯入 Goal，Goal 安全檢查會先於 checkout。

終態通知設定見 [第一次個人設定](01-first-time-personal-settings.md)。

## 步驟 7：讀執行前變更 Diff

![執行前變更 Diff 完整標註](../assets/dashboard-guide/annotated/launch-diff.jpg)

每一列都要核對：

- `goal.md`：沿用、缺少、修改未 commit，或將由上傳檔取代。
- `plan / phase`：新 workspace、沿用、重建，或匯入幾個 tasks／起始階段。
- `Agent`：本次實際命令。
- `Validate`：實際命令與 timeout。
- `收斂 / timeout`：flag、done、round、backoff 與是否規劃後暫停。
- `Git branch`：保持目前 branch 或建立新 branch。

粉紅色 `−` 是目前／既有值，綠色 `＋` 是這次送出值。Diff 不符合預期就回上方修改，不要先啟動再補救。

## 步驟 8：按「啟動」

啟動成功前視窗不會關閉。請看狀態訊息與 log 尾段：

- 成功：workspace 出現在上方頁籤，並開始規劃／執行。
- Preflight 失敗：不啟動 Agent；舊 state 保留。
- Validate 失敗／逾時：修正 repo、依賴、命令或 timeout 後再試。
- 單 writer 衝突：同一 Git worktree 已有 loop；先找出並停止原本的 writer。

## 啟動成功判定

- [ ] Workspace 頁籤出現正確名稱。
- [ ] 詳細頁顯示正確 target repo、階段與 round。
- [ ] Agent 命令與 Validate 命令在 console 開頭符合預期。
- [ ] 若勾「規劃後暫停」，規劃收斂後沒有自動跑 task，等待你按「運行」。
- [ ] 沒有 stale PID、state 錯誤、Goal 變更或未讀 issue 警示。

下一步：[監看 Fleet 總覽](04-monitor-fleet-overview.md)。
