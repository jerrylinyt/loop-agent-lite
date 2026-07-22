# 流程 13：從範本啟動與查看 Dashboard Jobs

## 目的

快速用現有 workspace 的成熟設定建立另一個 workspace，或查 Dashboard 啟動過的 background jobs 與輸出尾段。

## A. 以目前 Workspace 為範本啟動

「以此為範本啟動」目前由普通 Loop 詳細頁提供。Parallel base／managed worker 沒有此操作；要建立另一個 Parallel run，請開啟 Launcher 的 `Parallel Loop` 分頁，重新核對 immutable config 與 frozen plan。

### 可預填的內容

在詳細頁按「以此為範本啟動」後，啟動表單會預填：

- Target repo。
- Agent 命令。
- Validate 命令。
- flag／done 門檻。
- round、backoff、Validate timeout。
- reset 防線與規劃後暫停等 workspace config。

執行中、停止或完成的 workspace 都可當範本；只要 state 有 config 區塊。

### 刻意不預填的內容

Workspace 名稱留空，避免誤覆寫原 workspace。你必須填新名稱或明確接受 repo 目錄名。

範本不是 clone state：不複製 round、completed、done／flag、issues、history、logs 或完成 SHA。新啟動仍走完整表單驗證與 preflight。

### 操作步驟

1. 在來源 workspace 核對 Agent、Validate 與門檻確實適合作為範本。
2. 按「以此為範本啟動」。
3. 填新的 Workspace 名稱。
4. 決定 Goal 與 Plan：沿用同 repo、上傳新 Goal，或匯入新 Plan。
5. 核對 branch 選項，避免兩個 workspace 使用同一 Git worktree 同時跑。
6. 逐列讀「執行前變更 Diff」。
7. 按啟動，等待 preflight 成功。

不要用多個普通 Loop 或手動建立多個 worker worktree 來模擬 Parallel。真正需要同一 Plan 並行時，使用 `Parallel Loop`：base supervisor 會自行建立、驗證、整合與清理每個 managed linked worktree／task branch。不同 workspace 名稱不能解除 owner／writer 防線；若同一 repository 已有普通 owner，先完成或停止它；既有 Parallel run 則必須完成，或 Abort 並收斂到 `cancelled`。Pause 只供同一 run Resume，不會釋出 repo 給新的 writer。

## B. 查看「執行中的 jobs」

點「＋ 啟動／管理」→「執行中的 jobs」。

![Dashboard Jobs 實際畫面](../assets/dashboard-guide/raw/jobs.jpg)

每張 job 卡顯示：

- Workspace 名稱。
- PID。
- `執行中` 或 `已結束 rc=N`。
- Target repo 路徑。
- 輸出尾段。
- job kind，例如普通 `runner`、長跑 `parallel-supervisor`，或短暫的 `parallel-pause-control`／`parallel-abort-control`。
- 普通活躍 job 的「停止」，或 Parallel supervisor 的「Pause」按鈕。

Pause／Abort 會建立獨立、短暫的 `parallel-pause-control`／`parallel-abort-control` job。這些 control job 是一次性的 durable protocol client，卡片不提供第二個停止按鈕；應回 Parallel base 看最終 status，而不是只看 control process rc。Resume 不建立 `parallel-resume-control`：它會啟動新的長跑 `parallel-supervisor` owner，先完成 recovery audit，再持續執行同一 frozen run，直到之後 Pause、blocked 或進入終態。

清單每 2 秒更新。Dashboard 保留最近 50 個已結束 job 的尾段供稽核；更早的已結束 job 自動淘汰，活躍 job 不受此限制。淘汰 job 卡片不會刪 workspace state 或 history。

## 關閉 Dashboard 的影響

關閉 Dashboard process 會停止它管理的普通 loops；對 Parallel 則保留較長的 bounded shutdown/Pause 寬限，避免截斷控制協議。若寬限內無法安全收斂，base 會保留 durable 狀態供 recovery，不能因 Dashboard process 已退出就假定 run 已 paused。重開後先讀 base status／error，再重試 Pause、Resume 或 terminal cleanup。只關瀏覽器 tab 不等於停止後端 process。

## Job 停止與 Workspace 停止

- 普通 Job 卡「停止」會呼叫 workspace 的立即停止 API；日常停止仍建議回詳細頁用「本輪後停止」。
- Parallel supervisor Job 卡顯示「Pause」，呼叫 typed Pause；不是普通 signal stop，也不等於 Abort。
- Parallel Pause／Abort control job 無控制按鈕；Resume 後的長跑 supervisor 則仍從 base 使用 Pause。所有 durable 結果都回 base 判讀。
- Job 已結束但 workspace phase 尚未 done 很正常，表示 process 停止、協調任務未完成。

## 完成檢查

- [ ] 新 workspace 使用新的明確名稱。
- [ ] 範本只複製 config，沒有誤認會複製進度。
- [ ] 同一 Git worktree 沒有兩個 writer。
- [ ] Parallel workers／worktrees 由 supervisor 建立，沒有手動啟動或刪除。
- [ ] 執行前 Diff 已核對 Goal、Plan、Agent、Validate、門檻與 branch。
- [ ] Job process、workspace phase 與 Parallel durable status 分開判讀。

相關：[啟動新的 loop](03-launch-new-loop.md)。
