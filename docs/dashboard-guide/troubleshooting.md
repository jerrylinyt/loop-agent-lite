# Dashboard 疑難排解

先找畫面上的「精確錯誤」、workspace／round／task，再依下列類型處理。不要為了快速消除錯誤直接改 `workspace/*/state.json`，也不要把 Resume 當成繞過所有安全檢查的通用按鈕。

## 1. Dashboard 網址打不開

檢查：

1. 啟動 Dashboard 的終端機是否仍在執行。
2. 使用終端機實際印出的 URL，不要固定假設 8765。
3. 是否誤在另一個專案目錄啟動。
4. 終端機是否已有 traceback。

重新啟動：

```bash
source .venv/bin/activate
python dashboard.py
```

若指定 port：

```bash
python dashboard.py --port 8766
```

## 2. Repo 下拉選單找不到專案

1. 「＋ 啟動／管理」→ Repo 旁「管理」。
2. 加入 repo 的 parent directory，例如 `~/IdeaProjects`。
3. 按「儲存並重新掃描」。
4. 確認該目錄本身是 Git repo，或位於 root 的下一層。

仍找不到時選「手動輸入…」填絕對路徑，並確認：

```bash
git -C /path/to/repo rev-parse --show-toplevel
```

## 3. Workspace 名稱不合法

只使用英數、`.`、`_`、`-`，且不可是 `.`、`..` 或以 `.` 開頭。Repo 是 hidden directory 時不要留空，明確填合法名稱。

## 4. `goal.md` 缺少、未 commit 或已修改

在 target repo：

```bash
git status --short -- goal.md
git log -1 --oneline -- goal.md
```

處理：

- 尚未建立：依 [Goal 指南](02-prepare-goal-and-plan.md) 建立。
- 已確認內容：commit 後一般啟動。
- 想用新檔：在啟動表單選擇檔案，核對執行前 Diff。
- Workspace 顯示 Goal 已變更：點 Goal 看 diff，通常回規劃期重新收斂。

不要直接改 workspace 中保存的 Goal 基準 hash。

## 5. 一般啟動被「工作樹不乾淨」擋住

在 target repo：

```bash
git status --short
git diff
git diff --cached
```

逐檔判斷：

- 合法、已完成的工作：測試後 commit。
- 合法但未完成的中斷現場：先理解來源；若必須原樣接手，才評估 Resume。
- 不相關或不確定：不要盲目刪除／reset，先找負責人或比對 history。

若能整理成乾淨且 Validate 通過的現場，優先一般執行。

## 6. Agent CLI 顯示 not found

在終端機：

```bash
command -v codex
command -v claude
```

把輸出 executable 的「所在目錄」加入 Agent CLI 管理器的額外 PATH，或在 Command 使用 executable 絕對路徑。按「執行測試」，成功後記得「儲存 CLI 設定」。

GUI／IDE 啟動的 process 不一定讀 shell profile，所以「終端機能跑」不足以證明 Dashboard 能跑。

## 7. Agent CLI 測試卡住或逾時

CLI 測試最長 60 秒。常見原因：

- Command 進入互動式對話，缺少 print／non-interactive／stdin prompt 參數。
- 第一次登入或權限確認尚未完成。
- 模型名稱、provider、配額或網路錯誤。
- CLI 啟動了背景 process 而主 process 不退出。

先在 target repo 用等效方式測命令，確認它能在 stdin 收到 `test` 後自行結束。不要單純把 timeout 無限調高。

## 8. Validate 失敗

從畫面複製「完全相同」命令，在 target repo 執行：

```bash
cd /path/to/target-repo
<validate-command>
```

檢查：

- Working directory 是否正確。
- 依賴、虛擬環境、環境變數是否可用。
- 測試本身是否紅燈。
- 命令是否只在 shell profile 中定義 alias／function。
- 驗證是否留下未追蹤產物，使下一輪工作樹變髒。

修好後在 Workspace 設定按「執行確認」，再一般執行。

## 9. Validate 逾時

逾時會終止 validator process group。處理順序：

1. 在 target repo 計時執行相同命令。
2. 查是否真正卡住、等待網路／服務／互動輸入。
3. 若正常但合理超過目前上限，再調 `Validate 上限（秒）`。
4. 不要用縮減驗證範圍掩蓋 DoD。

## 10. 完整健檢按鈕停用

有尚未落地的 Goal／Plan、reset 或新 branch 草稿時，完整健檢無法代表真正啟動狀態，因此停用。可先：

- 用「執行確認」測 Validate。
- 核對表單草稿與執行前 Diff。
- 正式按啟動；實際啟動仍會完整重新驗證。

## 11. Plan JSON 顯示紅字

先確認 Launcher runner：

- 普通 Loop：只允許 `order/task/ref`；看到 `stack` 會拒絕，避免靜默串行。
- Parallel Loop：必須匯入非空 frozen plan，允許 `order/task/ref/stack`；`stack` 必須是正整數且同一值只能出現在一段連續 orders。

共同錯誤：

- JSON 解析失敗：修正逗號、引號、括號。
- 必須是非空物件陣列：最外層不可是 object／空陣列。
- 未知欄位：移除 schema 之外的欄位。
- order 非 int／重複／不連續：改成 1..N。
- task 空白：補可執行內容。
- ref 類型錯誤：字串、`null` 或省略。

可按普通「複製 JSON 範本」或「複製 Parallel 範本」重新對照。Parallel 若警告「目前沒有可並行 batch」，代表所有 batch 都是 singleton：至少兩個連續 task 必須共用同一 stack 才會實際並行；只有在人工確認獨立後才標註。

## 12. Repo 已有 owner／Loop

Workspace、worktree 與 Git common-dir 的 owner／operation fence 會拒絕衝突 writer，即使來自不同 Dashboard／終端機。不同 workspace 名稱不能繞過防線。

處理：

1. 從 Fleet／jobs 找原 loop。
2. 正常用「本輪後停止」。
3. 確認 process 已結束、workspace idle。
4. 再啟動新的 writer。

需要同一 Plan 並行時，請用 Parallel Launcher；supervisor 會自行建立並管理 linked worktrees、task branches 與唯一整合 gate。不要手動啟動多個普通 Loop 或 managed worker。

## 13. 顯示「警告：PID 殘留」

State 保留的 PID 已不存在，常見於強制終止或主 process crash。先查：

- Jobs 是否仍顯示其他 process。
- OS 中是否有相關 loop process。
- Console 最後的啟動／停止紀錄。
- Git worktree lock 是否仍被合法 writer 持有。

確認沒有 writer 後再一般執行。不要因看到 stale PID 就直接手動刪 state。

## 14. 顯示 state 復原或 checkpoint 唯讀

`state.last-good.json` 存在時，主 state 不可讀或 schema 不合法可自動復原並留下紀錄。若顯示「正從 checkpoint 唯讀顯示」：

1. 停止 mutation。
2. 看 Loop console 的復原／錯誤行。
3. 保存兩份 state 與 log 供診斷。
4. 確認是否有磁碟、權限、非 regular file／symlink 或外部手動寫入。

Primary 與 checkpoint 都不合法時會 fail-closed；應從 Dashboard 安全重建，不要拼湊半合法 JSON。

## 15. 紅連跳持續增加

依 Loop console 找：

- Validate FAIL／timeout。
- Goal／Plan／protected file 竄改。
- Agent 產出紅燈 commit。
- Reset 是否已發生。

再看 Agent console 與 task Git Diff。紅燈防線的目的是回最近綠點；不要為消除紅色隨意提高 red limit，先解決根因。

## 16. 停滯持續增加

停滯表示 HEAD 沒有前進，不必然是錯誤：獨立 done 確認輪本來可能不 commit。但長時間增加時檢查：

- Agent 是否反覆驗證已完成但沒有正確 `done`。
- Task 是否描述不清／不可行。
- 是否有 human gate 應回 issue。
- Plan 是否震盪或目前 task 已被 code 事實滿足。

搭配 history 的 signal、事件與未回 DONE 判讀，不要只看單一計數。

## 17. Plan version 很高並顯示可能震盪

規劃期 `plan v10+` 會警告。檢查：

- Goal 是否矛盾或過度模糊。
- Agent 是否每輪改寫相同 task。
- Flag 門檻是否與團隊策略不合。
- 是否缺少不可由 Agent 決定的範圍。

平順停止，人工審 Goal／Plan；必要時修 Goal 後回規劃期，而不是單純繼續燒輪次。

## 18. 未回 DONE／異常率升高

1. 點「未回 DONE」開異常輪。
2. 看 phase、task、signal、Git 變更。
3. 有保存 log 時讀 Agent 結尾。
4. 開 Prompt 確認本輪指令。
5. 查 CLI 是否在完成後異常退出、輸出 marker 失敗或被 timeout。

Validate PASS 與 DONE signal 分開；不要因測試綠就忽略協議錯誤。

## 19. Resume 按鈕不能按

必須同時具備：

- 有效、早於現在的執行開始時間。
- 非空綠點 SHA。

按下後後端仍驗證 SHA 是否存在於 target repo。若沒有可信資料，不要隨便填；改整理現場後一般執行。

## 20. Resume 啟動失敗

檢查：

```bash
git -C /path/to/repo show --no-patch --oneline <sha>
```

並確認時間的時區／日期、repo 路徑、workspace identity 與單 writer lock。Resume 不會繞過 Git／lock／identity。

## 21. 看不到 Workspace 卡片

依序清除：

- 搜尋字串。
- 「需關注／執行中／已完成」篩選，改回全部。
- 確認 `顯示 X/Y`。
- 檢查是否已永久刪除。

篩選會保存在瀏覽器，重新整理不一定重設。

## 22. Console 看不到預期紀錄

- 清空「過濾…」。
- Agent console 切「全部」。
- 展開已收合 pane。
- 點「跟到最新」。
- 很早的資料改看 history、timeline、`logs/` 或輪替的 `console.log`。

## 23. 刪除按鈕被拒絕

普通刪除只允許停止、未鎖定、安全 regular directory 的 workspace。Parallel base 另須是終態 `completed`／`cancelled`；`paused`、`blocked`、`finalizing*` 都要先 recovery 或 Abort 收尾。Managed worker 由 parent 管理，不可直接刪除。symlink workspace 一律拒絕，不要手動跟隨 symlink 刪除。

## 24. 畫面只有監看、按鈕不可用

可能以 `python dashboard.py --read-only` 啟動。關閉該 process，改用一般模式重啟，並確認你有權執行 mutation。唯讀模式適合監控，不適合設定／啟動／停止。

## 25. 請求過大／HTTP 413

Dashboard POST body 上限 8 MiB。過大的 Goal／Plan 會在 JSON 解析前拒絕。縮小檔案：移除不必要輸出、binary、巨大內嵌資料；Goal 應引用 repo 內文件，而不是把整份資料集貼入。

## 26. Parallel 一直在 `pause_requested`

Pause 是 bounded quiesce，不是直接 kill。先看 base console 與各 task Resource；若原 control job 已結束但仍未 paused，從 base 按「重試 Pause」。不要直接停止或刪 managed worker。若 child identity、gate 或 worktree 無法證明安全，run 會 fail closed 為 `blocked`。

## 27. Parallel `blocked`／Resume 失敗

先看 task 表：

- 有 `Outcome=阻擋`：這是 managed worker 的 terminal block，同一 frozen run 不會重派。完成決策後 Abort，再用修正過的 committed Goal／新 frozen plan 啟動新 workspace。
- 沒有 blocked outcome：讀 base error，處理 receipt／ref／owner／child／cleanup 的精確 recovery 原因後再 Resume。

不要編輯 `state.json`、aggregate、receipt、task ref 或 safe integration ref。無法證明安全時使用 Abort，不要反覆盲按 Resume。

## 28. Parallel 卡在 `finalizing`／`finalizing_cancel`

先確認沒有 live supervisor job。完成 intent 已固定時按「重試完成收尾」；cancel intent 已固定時按「重試取消清理」。此操作只重播原 terminal intent，不能把完成改成 Abort，或把 Abort 改回繼續執行。

## 29. Task Resource 是 `cleanup_failed` 或 worktree 還在

Outcome 與 Resource 分開：已整合不代表 child 已 reap／worktree 已清。查 base error、live process、`.run.lock`、dirty worktree 與 Git worktree registry；修復後從 base 重試相應 Resume／terminal cleanup。Supervisor 無法證明安全時會刻意保留現場，請勿手動刪目錄或 refs。

## 30. Managed Worker 只有唯讀畫面

這是預期行為。它只提供 assigned task、status／exit reason、history 與 console；Run、Stop、Resume、設定、Plan、phase、跳 task、刪除都由 parent supervisor 禁止。回 parent Parallel base 使用 Pause／Resume／Abort。

## 31. Parallel Job 已結束但 run 沒有完成

Job 是 process／control client，Parallel status 才是 durable run 真相。`parallel-pause-control`／`parallel-abort-control` 的 rc=0 只表示請求已被協議處理，不等於整個 run 完成；Resume 則會啟動新的長跑 `parallel-supervisor`，startup ready 也只表示 recovery owner 已安全接手。回 base 看 `paused`、`blocked`、`completed` 或 `cancelled`；關閉 Dashboard 後也要重新確認，不要只靠 PID 推論。

## 收集完整診斷資料

請保存：

- Dashboard 啟動終端機錯誤。
- Workspace、phase、round、task、running 狀態。
- Loop console 該輪完整片段。
- Agent console 錯誤附近片段。
- History／timeline／異常輪資訊。
- Agent 與 Validate 完整命令（去除秘密）。
- Target repo 的 `git status --short`、`git rev-parse HEAD`。
- 是否一般執行或 Resume。
- Runner 類型；Parallel 另附 base status／terminal intent、task Outcome／Resource、run id 與 control job kind。
- 問題發生的日期、時間與時區。

切勿在診斷資料中附 API key、token、cookie、私有憑證或通知 URL 的秘密參數。
