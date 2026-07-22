# loop-agent-lite

用 Python 協調 agent 的規劃／執行迴圈；可從終端機管理單一 workspace，也可用瀏覽器 Dashboard 管理多個 workspace。

終端機流程請看：[單一 Workspace CLI 完整圖解](docs/cli-guide/README.md)。Dashboard 第一次使用請看：[Dashboard 完整操作圖解](docs/dashboard-guide/README.md)；逐欄查詢請看：[欄位與控制項完整說明](docs/dashboard-guide/fields-reference.md)。要用 Dashboard 操作 ralph 迴圈請看：[Ralph runner 使用圖解](docs/ralph-guide/README.md)。

![Dashboard 執行中展示](docs/dashboard-running.jpg)

展示圖以 mock fleet 呈現執行中、規劃中、驗收中與已完成等 workspace 狀態；實際資料會由 `workspace/*/state.json` 提供。左側顯示 Loop 狀態與驗證紀錄，右側顯示 Agent 輸出；兩側可拖曳調整寬度或收合。

## 流程

普通 Loop coordinator 維持既有的單 workspace、單工作樹串行流程：

```text
準備 target repo
  └─ goal.md（以及選配的 plan-doc）已審核並 commit
          │
          ▼
`python loop.py init ...` 或 Dashboard 建立 workspace
          │
          ▼
`python loop.py run <workspace>` 或 Dashboard 啟動 coordinator
          │
          ├─ preflight：validate、工作樹、goal/plan-doc commit 檢查
          │       └─ 失敗：保留舊 state，不啟動新工作
          │
          ▼
規劃期：agent 建立或確認計畫
          │  flag 達門檻後進入執行期
          ▼
執行期：依序處理 task-N，完成後 validate
          │  done 達門檻後記錄完成 SHA，繼續下一個任務
          ▼
全部任務收斂 → REPORT.md；完成後 loop 自動停止
```

Parallel Loop 不改寫上面的 convergence engine，而是在外層加一個 supervisor：

```text
人工審核 frozen plan，為可安全同批執行的連續 tasks 標註相同 stack
          │
          ▼
Parallel supervisor 持有 base workspace 與 primary repo 的唯一寫入權
          │
          ├─ 每個 task 建立受管 linked worktree、task branch 與原生 engine.loop worker
          │  同一 batch 最多同時執行 max_parallel 個 worker
          ▼
worker 達 done 門檻並驗證 exact SHA → supervisor gate 序列化 ff-only 整合
          │
          ▼
本 batch 全部整合與清理後才進下一 batch → 完成報告與通知
```

每輪都會保護 `goal.md`、計畫與 state。驗證失敗或偵測到竄改時，會回到最後綠點。`--reset-state` 和 Dashboard 的 plan 匯入都是交易式操作：新流程未通過啟動檢查時，舊進度仍保留。
Dashboard 啟動若同時要求匯入 goal 與新 branch，會先完成 goal 路徑安全檢查，再進行 branch checkout；任何 goal 錯誤都不會留下半套 Git branch mutation。

普通 Loop 仍以 OS 鎖維持單 writer：同一 workspace 或同一 Git worktree 不能同時跑兩個普通 loop（即使來自不同 Dashboard／終端機）。需要並行時請使用 Parallel Loop；它由一個 supervisor 統一建立 worker worktree／branch、保存多份受管 state，並以 mechanical gate 序列化整合。不要自行啟動多個普通 Loop 共寫同一 repo 來模擬 Parallel。

## 安裝與啟動

需要 Python 3.10 以上與 Git；若要修改或測試 Dashboard 前端，另需 Node.js 22。所有正式 runtime Python 依賴都只使用標準函式庫。

Linux／Ubuntu（bash）在專案根目錄建立 `.venv`：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows（PowerShell）使用對應的啟用腳本：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

之後回到專案時，Linux／Ubuntu 執行 `source .venv/bin/activate`，Windows PowerShell 執行 `.\.venv\Scripts\Activate.ps1`，再選擇下方的單 workspace CLI 或 Dashboard 流程。
目前 runtime 只使用 Python 標準函式庫，因此 `requirements.txt` 暫無第三方套件；檔案仍是固定的依賴安裝入口。
workspace 與個人設定預設固定保存在這份 loop-agent-lite 專案內，不會依安裝型態改放到使用者資料目錄。

## 單一 Workspace CLI（快速開始）

CLI 會把第一次設定保存在 `workspace/<name>/state.json` 的 `config`；之後 `run`／`restart` 只需 workspace 名稱，不會把 Agent、Validate 或門檻偷偷換回預設值。

先在 target repo 建立並 commit `goal.md`，確認工作樹乾淨且 Validate 命令可成功，再從本專案根目錄執行：

```bash
# 1. 完整 preflight 後初始化；此步不啟動 Agent
python loop.py init \
  --name my-work \
  --repo /absolute/path/to/target-repo \
  --agent-cmd 'claude -p' \
  --validate-cmd 'python -m unittest discover -s tests -t . -q'

# 2. 可選：再確認保存的設定與 preflight
python loop.py config my-work
python loop.py check my-work

# 3. 前景執行；Ctrl-C 會保存 state 後停止
python loop.py run my-work
```

若已有人工審核過的 Plan，可在 init 時匯入並直接從執行期 task-1 開始；`--plan` 與 `--import-plan` 同義：

```bash
python loop.py init \
  --name my-work \
  --repo /absolute/path/to/target-repo \
  --agent-cmd 'claude -p' \
  --validate-cmd 'pytest -q' \
  --plan /absolute/path/to/plan.json \
  --start-phase exec
```

另一個終端機可觀察或要求平順停止：

```bash
python loop.py status my-work --watch --metrics 20
python loop.py stop my-work          # 目前 round 完整落盤後停止
python loop.py stop my-work --now    # Agent 失控時才立即中斷
```

正常停止後，下列兩條完全同義，都會用保存設定重新做 Preflight／啟動 Validate，並從原 state 接續：

```bash
python loop.py run my-work
python loop.py restart my-work
```

明確要保留中斷中的 dirty 現場時才用 `--resume-interrupted`；要捨棄 coordinator 進度、交易式開始新 run 才用 `--reset-state`：

```bash
python loop.py run my-work --resume-interrupted
python loop.py run my-work --reset-state
```

安全修改 `state.json` 內可設定參數時，不要直接開檔覆寫；先停 loop，再用 `config`，primary 與 checkpoint 會一起原子更新：

```bash
python loop.py config my-work \
  --done-threshold 5 \
  --round-timeout 45 \
  --validate-timeout 300 \
  --pause-after-plan
```

若要隔離 workspace root，全域選項必須放在子命令之前，例如 Linux／Ubuntu 使用 `python loop.py --workspace-root /tmp/loops status my-work`，Windows 使用 `python loop.py --workspace-root C:\loops status my-work`；也可設定 `LOOP_AGENT_WORKSPACE_ROOT`。完整 init、plan 匯入、參數表、state 欄位所有權、重啟決策與疑難排解都在 [CLI 完整圖解](docs/cli-guide/README.md)。

## Dashboard 啟動

執行 `python dashboard.py`，再開啟終端顯示的本機網址（預設從 <http://127.0.0.1:8765/> 開始；port 被占用會自動往上找）。

### 1. 準備 target repo

在 target repo 建立並 commit `goal.md`；若有傳入 `plan-doc`，該檔也必須 commit。普通 Loop 的 Plan 可由規劃期 Agent 建立，或在啟動時匯入 JSON array。Parallel Loop 則必須匯入已由人類審核的 frozen plan，並固定從 exec 開始。確認驗證命令可在該 repo 執行。

### 啟動選項

```bash
python dashboard.py --port 8766
python dashboard.py --name <workspace>
python dashboard.py --read-only
```

開啟 <http://127.0.0.1:8766/>，在「啟動／管理」先選 `Loop coordinator`、`Parallel Loop` 或 `Ralph`，再設定：

- target repo
- Agent CLI（例如 `claude -p`）
- validate command（例如 `python -m unittest discover -s tests -t . -q`）

找不到 CLI 時，點 Agent CLI 旁的齒輪，設定 CLI 命令及其 PATH 目錄；也可以直接填可執行檔的絕對路徑，再按「測試」。

Agent prompt 會經由 stdin 傳入，stdout／stderr 會逐行寫入 workspace log。每輪都有獨立 token，舊輪殘留命令不會被下一輪誤收；engine 主程序退出時也會清理同 process-group 的背景子行程。中斷後可直接在 Dashboard 按「運行」從 `state.json` 續跑。Dashboard 的 SSE 變更最多每 3 秒整理推送一次；console 單次只保留最新 64 KiB，前端累積尾段也會按完整行截斷。

Prompt 內容會由 engine 寫入 stdin pipe，Agent 不會取得 prompt 檔案路徑；workspace 內的 `prompts/` 僅保留稽核副本。另有 `LOOP_WS`（workspace 目錄）與 `LOOP_ROUND_TOKEN`（本輪 token，`work.py` 靠它核對呼叫來源）可用。

Dashboard 也提供唯讀 `GET /api/health`，回傳 `schema_version: 1`、`status`（`ok`／`degraded`／`error`）與 workspace、執行中、需關注、state 錯誤、issues、Agent 異常、最近一輪逾時、state 復原、goal 變更及 stale PID 摘要；適合本機探針或外部監控。加上 `?strict=1` 時，`degraded`／`error` 會以 HTTP 503 回應，方便 readiness probe 直接判斷；預設仍維持 HTTP 200 並讓呼叫端讀取 status。瀏覽器頁首與即時 SSE 的 `health` event 使用同一份 projection，不會修復或改寫任何 workspace。

停止中的普通 workspace 可從「設定」把目前純 plan 陣列匯出為 `<workspace>.plan.json`。設定內匯入只支援普通 Loop 的 non-stack plan 完整重置：驗證 `order`／`task`／`ref`，拒絕 `stack`、整份 state 或完成進度，保留 workspace 執行設定與 target repo，清除 coordinator 進度及舊 run 產物後停在規劃期。Parallel frozen plan 必須從 Parallel launcher 匯入，不能用設定頁把普通 workspace 原地轉成 Parallel。
`GET /api/round-metrics?ws=<name>&run=current&limit=100` 使用同一套 bounded/safe history reader，供 Dashboard 與外部觀測工具取得近期效能摘要、逐輪樣本，以及 Agent 結束但未送出 phase 完成回報的異常次數／異常率；Plan 以 `create-plan`／`plan-ok`、Exec 以 `done` 作為完成回報，即使本輪有 Git 變更，沒有回報仍算異常。人工立即中斷的未完成輪不寫入 history，因此不進入異常分子或總輪數；`run=previous` 可分析保留的上一個 run。
`GET /api/fleet-round-metrics` 會將所有 workspace 的輪次依 timestamp 合併，只聚合全體最新 500 筆並回傳效能、未回 DONE 異常次數與全域異常率摘要；SSE 的 `fleet-round-metrics` event 使用同一 projection，瀏覽器不會收到原始樣本。
`GET /api/anomalies[?ws=<name>&run=current]` 列出與 Overview 全域 500 輪或 workspace 100 輪統計一致的最近異常（最多 100 筆）；新發生的異常會把 Agent log 尾端最多 2 MiB 保存在 `logs/anomalies/`，每個 workspace 最多 100 份。`GET /api/anomaly-log?ws=<name>&id=<id>` 安全讀取其中一份；舊異常仍可列出輪次判定，但功能啟用前沒有可回補的 Agent log。

常用選項：

```text
python dashboard.py --name <workspace>  預選 workspace
python dashboard.py --port <port>       指定起始 port
python dashboard.py --read-only         啟動唯讀看板
```

Agent、Validate、收斂門檻、timeout、plan 匯入、state 重置與新 branch 都在 Dashboard
啟動表單設定。所有數值會在建立 workspace 或啟動 Agent 前由前後端重新驗證。
workspace 名稱只允許英數、`.`、`_`、`-`，且不可為 `.`、`..` 或以 `.` 開頭；若 repo
目錄本身是 hidden 目錄，請明確以 `--name` 指定一個符合規則的名稱。
workspace 的 `state`、console/history、prompt、round log、REPORT 與其 `logs/`、`prompts/`、`snapshots/` 父目錄也會以 `O_NOFOLLOW`／regular-file 檢查；若被替換成 symlink、FIFO 或其他非預期類型，loop 與 Dashboard 會拒絕讀寫。
Agent 使用的 `engine.work` marker、plan proposal、issue 與 loop 的單 writer lock 也沿用同一套檢查；不安全的協調檔會直接拒絕該命令，不會跟隨連結寫到 workspace 外。
單筆 issue 最多 2000 字、每輪最多 100 條；state 最多保留最新 200 條 issue，避免異常 Agent 輸出無限膨脹 coordinator state。人員可在 Dashboard 將目前 issues 標記已讀：只寫入 round watermark、保留原始紀錄；後續新 round 回報的 issue 會再次成為未讀並觸發 fleet health。
Dashboard 匯入 `goal.md`、讀取團隊／個人設定與儲存設定時也會拒絕 symlink、FIFO 或非 JSON object，避免 UI 操作意外觸碰檔案邊界外。

## Dashboard 操作

- 左側是 Loop 狀態；右側是 Agent 輸出，可切換 Agent／其他／全部，並可用「過濾…」輸入框對長 log 做文字過濾；Agent 的 ANSI 色碼會直接上色。
- 瀏覽器 tab 標題會顯示「執行中／警告／完成／已停止」，favicon 以綠、紅、藍、灰狀態點同步呈現，掛在背景 tab 也能監控。
- workspace header 有輪次 sparkline（綠紅灰橙＝驗證綠／紅／規劃／reset，點擊開逐輪判定）與頂部健康色帶（越紅越接近 reset 防線）；進行中 round 會每秒顯示 elapsed 與 timeout 剩餘時間，最後 60 秒轉為警示。立即停止會凍結並保留中斷輪次時間；SIGKILL 無法留下停止時間時顯示「至少」已執行多久。若 loop 被強制終止後留下 stale PID，詳細頁也會保留警示。
- 工具列「總覽」切換電視牆模式：頂部在「任務完成」右側整合所有 workspace 依時間最新 500 筆輪次的平均、P50、P95、最慢、逾時率、未回 DONE 次數與全域異常率；點「未回 DONE」可展開異常 workspace／round 清單，再點輪次查看保留的 Agent log。下方各 workspace 卡片仍各自顯示近期最多 100 輪摘要及相同異常統計，點卡片切入；輪次紀錄中的異常數也可開啟同一種清單與 log 檢視。整合卡只透過 SSE 傳統計結果，不傳 500 筆原始樣本；卡片與事件推播共用同一連線，不另開輪詢，輪次計時由瀏覽器依 state 時間戳本地更新，不為時鐘製造高頻 SSE；可用名稱搜尋與「全部／需關注／執行中／已完成」篩選卡片，選擇會保存在瀏覽器；頁首只有在真的有問題時才顯示可點擊的「工作區需處理」，點下會直接篩出問題卡片，卡片列出原因並可切入指定 workspace。已完成 workspace 的歷史停滯／紅燈不再誤算為目前告警；未讀 issues、checkpoint、goal 變更、stale PID 與 state 錯誤仍會標示。搭配 `--read-only` 適合掛牆監控。
- 已完成 task 右側的 Git SHA 可開啟全畫面變更瀏覽器：左側依狀態列出變更檔案，右側可切換並排／單欄 diff、語法高亮與自動換行，上方另列出該 task 涵蓋的 commit 清單。新版 state 會在 task 啟動時固化 base SHA，因此同一 task 的多個 commit 會合併成「起點→完成」淨變更；舊 state 依序退回前一 task SHA 或單一完成 commit，且會在畫面明示相容模式。Binary 與過大 patch 不會強行當文字展開。
- workspace 狀態列的「時間軸」把歷史輪次、異常與目前 console 的操作紀錄整合成單一時間序；只有時間而沒有日期的 console 紀錄會明確標示為本機時間，避免把推定時間當成精確事實。
- 階段切換、任務跳轉、Validate 與永久刪除 workspace 等操作，在確認視窗先列出將改變的 state、命令、timeout、workspace 目錄與不受影響的 target repo，讓操作者能在送出前核對影響範圍。
- 普通 Loop 停止後可用全畫面 Plan 編輯器修改 pending tasks：已完成與目前任務鎖定，後方尚未執行的任務可從專用把手拖移，也可用上移／下移按鈕調整、刪除，或在兩項之間／尾端插入新任務。儲存以 plan version 防止覆蓋新狀態，並由後端原子驗證、重新編號；歷史與完成 commit 不改寫。含 `stack` 的 frozen plan 與受管 Parallel workspace 是唯讀，不會由編輯器默默移除人工標註。
- 普通 Loop 的啟動表單進階設定與 workspace「設定」都可勾選「規劃收斂後暫停」：計畫收斂後 loop 停在執行期起點、不自動開始執行，人工核對（或用 Plan 編輯器調整）後按「運行」才進入執行輪；規劃期狀態列會顯示「規劃後暫停」提示，`notify_cmd` 會收到 `plan_paused` 終態通知，團隊預設值在 shared 設定的 `defaults.pause_after_plan`。Parallel 不經規劃期，固定匯入 frozen plan 後從 exec 起跑。
- 啟動表單的「執行前變更 Diff」會比較既有 repo／workspace 與本次 goal、plan、phase、Agent、Validate、門檻、timeout 及 branch 選擇；有待匯入內容時自動展開。
- 普通 workspace 詳細頁的「以此為範本啟動」會以該 workspace 的 repo、Agent、Validate 與門檻／timeout 設定預填啟動表單，workspace 名稱刻意留空讓你填新的；執行中、停止或已完成的普通 workspace 都可當範本，送出仍走原本的驗證與啟動流程。
- 普通 workspace 的「⇄ Run 對比」並排顯示目前與上一個 run 的樣本數、平均、P95、最慢、逾時率、未回 DONE 與異常數；沒有 per-run snapshot 的設定與 commit 不會推測比較。
- `⌘K`／`Ctrl+K` 可開啟快捷指令，搜尋 workspace 或前往總覽與啟動管理。
- 按 `Ctrl+G`（macOS 為 `⌘G`）後再按 `0` 可回總覽、按 `1～5` 可切換前五個 workspace；第二鍵需在 1.5 秒內輸入，表單或對話框開啟時不觸發。
- 總覽的批次操作可多選 workspace 並標記 issues 已讀或停止；普通 Loop 使用既有立即停止，Parallel supervisor 則送出 Pause、在安全邊界 quiesce workers。不符合動作前置條件的項目會在確認預覽中列為跳過，符合者仍逐筆使用既有安全 API。
- Dashboard 提供跳至主要內容、清楚的 focus outline、Modal focus trap／Esc／焦點回復、reduced-motion 與 forced-colors 支援；首次空畫面則提供 repo、goal/plan、Validate 三步引導與常見失敗原因。
- 分隔線可拖曳調整欄寬；箭頭可收合，設定會保存在瀏覽器。
- 狀態列的「Goal」「輪次紀錄」「Prompt」chips 分別顯示目前 goal 內容、history.log 逐輪判定（含每輪 Agent 耗時／逾時／是否未回 phase DONE）、以及最近一輪送給 Agent 的完整 prompt（全部唯讀）；「輪次紀錄」保留最近 100 輪的樣本數、平均、P50、P95、最慢輪、逾時率、未回 DONE 次數與異常率完整分析，Overview 卡片同步提供快速摘要。goal 在停機期間變更時，Goal 視窗會用保存的計畫基準 hash 從 Git 歷史重建並顯示 unified diff。
- Issues 視窗可「標記已讀」而不刪除稽核紀錄；只有未讀 issues 會讓 fleet 顯示需關注，仍可用「清空全部」永久移除紀錄。
- 全部任務收斂後，狀態列出現「完成報告」直接檢視 REPORT.md；Parallel Abort 則產生 cancelled partial report，不冒充完成。
- 停止狀態可按「刪除」永久移除對應的完整 workspace 目錄；確認視窗會列出 state、history、console、logs、prompts、snapshots 與 REPORT 等受影響資料，並明示 target repo 不受影響。執行中、鎖定中或 workspace 路徑是 symlink 時一律拒絕；Parallel 另只允許已 `completed`／`cancelled` 且 owner 停止的 base workspace 刪除，paused／blocked 必須先 Resume 收斂或 Abort 清理。刪除會先把 root entry 原子改成隱藏暫存名稱，再以不跟隨 symlink 的方式移除整棵目錄，無法復原。
- 啟動表單進階設定內的「管理終態通知」可編輯、儲存並以 `status=test` 實測 `notify_cmd`（佔位符 `{status}`、`{name}`）。
- 啟動表單的「完整健檢」會檢查目前已 commit repo 的 git／鎖／乾淨工作樹／goal 與 Validate，不建 state、不啟動 Agent；待匯入 goal、plan、reset 或新 branch 時會停用，實際啟動仍會再驗一次。
- 啟動表單在 `goal.md` 旁將「Goal 產生器 Prompt」與「Goal 成果模板」分成兩個入口：前者把需求、選填的專案限制與任務類型編譯成可直接貼給 Agent 的自然 Markdown，後者逐類提供符合現行八段契約、具 SC／AC 追溯與 DoD 骨架的 `goal.md` 參考模板。Goal 描述結果與限制，不承載 worker 拆分或 `stack` 等執行拓撲。普通 Plan 產生器只輸出 `order/task/ref` JSON array；Parallel 頁的「產生基礎 Plan Prompt（不含 stack）」會額外要求每項 task 明列 working set、schema／生成物、語意依賴與 validator 外部資源，但仍禁止 Agent 推論 `stack`。人類審核證據後才可替連續且確實獨立的 tasks 加上相同正整數 `stack`。以上操作都只在瀏覽器進行，不會改動 repo 或 workspace。
- 普通 Loop 正常要停時用「本輪後停止」：目前 Agent、Validate 與 state/history 落盤完成後才停，不會啟動下一輪。Parallel 使用 Pause，由 supervisor 停止派工、裁決已 claim 的 gate，並在 workers quiesce 後退出。
- 普通 Loop 本輪尚未結束前按「繼續運行」可撤銷平順停止；如果 loop 已取走請求，會明確告知這一輪仍會收尾停止。
- 普通 Loop 停止狀態下按「運行」會先開啟選擇視窗：可選一般執行（完整 Preflight／啟動 Validate），或保留目前現場的 Resume。Resume 只要求執行開始時間早於現在，且綠點 SHA 是 target code repo 內存在的 commit；舊 state 缺少任一資料時可直接在視窗補填，通過後會寫回 state。Resume 會略過啟動時的 dirty-tree、Validate、HEAD 祖先與 protected snapshot 比對，目前的 protected 檔案會成為後續輪次的新防竄改基準；Git／單 writer lock 與 workspace identity 仍保留，下一輪結束後也仍須照常 Validate。這不是 Parallel Resume；Parallel 必須先由 supervisor reconcile durable state。
- 普通 Loop 的 Agent CLI 卡死或明顯失控時用「立即停止」；它會中斷目前 round，state 可在下次運行時續用。Parallel 請優先 Pause；確定放棄 run 才 Abort。
- 普通 Loop 停止後可編輯計畫、切換階段或修改 agent／validate 設定，再按「運行」。Parallel frozen plan 與 managed workers 不開放這些 mutation。
- 普通 Loop 的「重置 workspace state」會保留舊 state，直到新流程通過 preflight。
- 普通 Loop 的「匯入 plan」會建立全新的 state，可選擇從規劃期或執行期開始；Parallel 必須匯入非空 frozen plan，固定從 exec 開始。
- 迴圈完成、停止或發生啟動錯誤時，Dashboard 會顯示結果與 log 尾段；啟動成功前不會關閉視窗。
- 「執行中的 jobs」分頁會保留最近 50 個已結束 job 的尾段供稽核；更早的 job 會自動淘汰，活躍中的 job 不受限制，workspace state 與 history 不會被清除。Job 的停止動作會依 runner 路由：普通 Loop 是立即停止，Parallel 是 Pause。
- Dashboard 的 REST POST body 上限為 8 MiB；超過會在讀取 JSON 前回 413，避免過大的 goal/plan 或異常請求拖垮長跑服務。

## Parallel Loop 模式（可選）

Parallel Loop 適合 plan 中確實存在可獨立實作、獨立驗證的任務。它不是讓 planner 自動猜哪些工作能並行：Goal 與 Goal 成果模板維持執行拓撲中立，外部 Agent 只產生不含 `stack` 的基礎 Plan；`stack` 必須由人類讀完 repo 證據後加入。

Parallel plan 沿用 `order`、`task`、選填 `ref`，另允許選填的正整數 `stack`：

- 相同 `stack` 必須只出現在一個連續的 order 區段，該區段形成一個 batch。
- 未標 `stack` 的 task 自成一個 singleton batch；只替單一 task 標 stack 也不會產生並行。
- batches 依 order 串行；同一 batch 內最多同時執行 `max_parallel` 個 workers。
- 同 stack tasks 的 working set、schema、生成物與語意／資料依賴不得重疊，validator 使用的固定 port、DB、cache、lock、外部服務或全域環境也必須隔離；任一條不確定就不標 stack。

Dashboard 在「啟動／管理」切到 `Parallel Loop` 後，必須貼入非空 frozen plan。它使用目前 branch 上已 commit 的 `goal.md`，固定從 exec 開始，並停用 goal 上傳、規劃後暫停、reset 與新 branch 選項。CLI 等效入口例如：

```bash
python -m engine.parallel start \
  --name my-parallel-work \
  --repo /absolute/path/to/target-repo \
  --agent-cmd 'claude -p' \
  --validate-cmd 'pytest -q' \
  --import-plan /absolute/path/to/parallel-plan.json \
  --max-parallel 2 \
  --worker-restart-limit 3
```

Supervisor 是 base workspace 與 primary branch 的唯一 owner。每個 worker 在獨立 linked worktree 與 task branch 上繼續使用原生 `engine.loop` 收斂；達 done threshold 後只送 gate request，真正的 ff-only integration 由 supervisor 對 exact validated SHA 序列化執行。Worker 不得直接 checkout primary、合併 peer branch 或改寫 shared refs。

Parallel 詳細頁只提供符合 durable lifecycle 的控制：

- `Pause`：停止新派工，在安全邊界 quiesce workers；未整合 worktree 留給 Resume。
- `Resume`：啟動新的 supervisor owner，先 reconcile receipts、gate、child 與 repo identity，再恢復可安全繼續的工作；`blocked` 若仍無法證明安全會保持 blocked。
- `Abort`：停止 workers、取消未整合 tasks，保留已整合 commits，只清理由 supervisor 證明可安全移除的 worktrees；不會 rollback primary。

普通 Loop 匯入或載入含 `stack` 的 plan 會預設拒絕，避免靜默串行；只有 CLI 明確使用 `--allow-serial-stack` 且直接從 exec 起跑時，才會忽略 batch 並依 order 串行。規劃期 `create-plan` 與 plan→exec transition 一律不接受 `stack`。完整架構、crash recovery 與安全 invariant 見 [Worker Agent 並行執行設計](docs/feature/parallel-workers.md)。

## Ralph runner 模式（可選）

除了內建的 loop coordinator，Dashboard 也能直接操作公司內既有的 [ralph](https://github.com/snarktank/ralph)
迴圈（`ralph.sh`）。ralph 自成完整迴圈引擎（每輪起新 agent、自己 commit、以 `prd.json`／`prd.md`
與 `progress.txt` 為狀態），因此這是一種**唯讀投影＋監督**的 runner，與 loop coordinator 並存、
互不干擾；`engine/ralph.py` 只負責 spawn／監控／把進度投影進 `state.json`，不套用 loop 的共識、
validate 或防竄改機制。**操作圖解見 [Ralph runner 使用圖解](docs/ralph-guide/README.md)**；架構與設計取捨見
[Ralph 模式接入設計](docs/ralph-mode-design.md)。

在啟動表單切到「Ralph」模式，只需填 ralph 需要的參數：target repo、`ralph.sh` 命令（可從團隊
`ralph.scripts` 白名單選，或直接手填）、iterations、tool（如 `opencode`／`claude`）、model、
參數風格（`positional` 公司版＝`<iters> <tool> <model>`；`snarktank` 原版＝`--tool <tool> <iters>`），
以及選填的 PRD 匯入。啟動後 RalphView 以 PRD 檢核表、progress.txt 檢視器與共用 console 監控，
可停止／重啟（重啟即從 PRD 未完成項續跑），不顯示 loop 專屬的計畫／階段／門檻等控制。

**用量上限自動重啟／模型降級**：長跑 ralph 撞到 agent 用量上限時，`ralph.sh` 會空轉燒迭代。
監督層以 heuristic 偵測 agent stdout 的用量上限訊號（且該輪無實質進展才算），殺掉空轉的 ralph，
再依設定「等 reset 後重啟」或「沿 `fallback_models` 降級模型即刻重啟」，達安全上限（預設 6 次）則停在
`usage_limit_giveup`。偵測 pattern 可在團隊 `ralph.usage_limit_patterns` 追加公司 opencode 專屬訊息；
state 明確標示 `detection: "heuristic"` 與觸發的原始行供調參。細節見設計文件的 usage-limit 章節。

團隊 ralph 設定（`ralph.scripts`／`tools`／`usage_limit_patterns` 等）放在 shared config，範例見
`engine/dashboard.config.shared.json`。

## 團隊設定與個人設定

- `engine/dashboard.config.shared.json`：專案內的團隊預設值；可用 `LOOP_AGENT_DASHBOARD_PROJECT_CONFIG` 指向另一份 shared config。
- `dashboard.config.local.json`：專案內的個人 CLI、PATH、repo roots 與通知設定，已加入 `.gitignore`。
- `workspace/`：固定放在專案根目錄；隔離測試可顯式使用 `LOOP_AGENT_WORKSPACE_ROOT` 覆寫。

第一次使用請在 Dashboard 的設定頁完成個人 CLI／PATH／repo roots 設定；不同電腦只需各自建立 local 設定，不會改動團隊檔案。

Prompt 模板的共用核心與 Goal／Plan 輸出契約由 `engine/prompts/external-agent-*.md` 資源載入，UI 只負責替換經驗證的 placeholder，不再內嵌 prompt 長字串；修改資源後需重新啟動 Dashboard。任一固定資源缺失、過大或 placeholder 漂移時只會停用 Prompt 產生器並顯示原因，不影響其他 Dashboard 功能。團隊只在 shared 設定的 `prompt_templates` 新增任務專屬指引。`id` 必須是唯一的小寫英數／`.`／`_`／`-`，單一 Dashboard 最多載入 50 筆團隊模板；不合法或和內建模板重複的項目會略過並在模板視窗提示。例：

```json
{
  "prompt_templates": [
    {
      "id": "team-payment-flow",
      "label": "分析團隊付款流程",
      "category": "團隊",
      "description": "追蹤付款狀態、補償與通知。",
      "requirement_placeholder": "例：分析退款失敗後的補償流程。",
      "instructions": "- 盤點付款狀態機與真相來源。\n- 追蹤重試、冪等、補償與通知邊界。"
    }
  ]
}
```

## Workspace 檔案

```text
workspace/<name>/
├── state.json       目前進度與執行設定
├── state.last-good.json  最近一次合法 state 的復原副本（主檔不可讀時才使用）
├── stop-after-round.json  暫時的 session-scoped 平順停止請求（loop 消耗後刪除）
├── .run.lock         單 writer flock 檔（檔案存在不等於正在執行）
├── console.log      完整流程紀錄（單檔上限 5 MiB，輪替保留 3 代）
├── history.log      逐輪判定（當前 run 上限 10 MiB，超出保留最新尾段）
├── logs/             每輪 Agent 原始輸出
├── prompts/          最近幾輪送出的 prompt
├── snapshots/        goal／plan-doc 的防竄改基準
├── parallel/<run_id>/  Parallel 才有：immutable plan/config/manifest、aggregate、gate/receipt、control、child 與 finalization artifacts
├── worktrees/<run_id>-task-N/  Parallel 執行中才有：受管 linked worktree；安全終結後由 supervisor 清理
└── REPORT.md        全部任務完成後的摘要
```

Parallel worker 另使用 `workspace/<base>--<run_id>-task-N/` 保存原生 loop state、history、logs 與 prompt。這些 workspace 由 parent supervisor 管理，在 Dashboard 只提供唯讀投影；終態 cleanup 會先確認 child 已退出、lock 已釋放且 worktree 可安全移除，再歸檔或清理。不要手動刪除這些目錄、task refs 或 `parallel/<run_id>/` durable artifacts。

## 常見問題

**Agent CLI 顯示找不到檔案**

在 Agent CLI 管理器填入正確命令，並把 CLI 所在目錄加入 PATH；按「測試」確認後再啟動。GUI 啟動的 PATH 可能和終端機不同。

**validate 失敗或逾時**

先在 target repo 手動執行同一個 command，確認工作目錄與依賴正確，再回 Dashboard 修改命令或 timeout。逾時會終止 validator 的整個 process group。

**workspace 顯示沒有 state.json**

若 `state.last-good.json` 存在，Dashboard／loop 會自動復原主檔並留下 state 復原紀錄；兩份都不存在時，請執行 `python loop.py init ...`，或從 Dashboard 啟動表單重新建立。不要在 loop 執行中手動刪除 workspace 檔案。
state 若是合法 JSON 但核心欄位型別、phase、loop PID/session 或退避／復原時間 metadata 不合法，也會依同一套 checkpoint 防線復原；primary 與 checkpoint 都不符合 schema 時會 fail-closed，避免半合法資料讓 loop 晚發崩潰。

## 開發與測試

```bash
# Python 協調層
python -m unittest discover -s tests -t . -q

# Dashboard 前端（需要 Node.js）
cd ui
npm install
npm run check
```

`engine/ui/` 已包含 production 靜態檔；只有 Python 的環境也能從專案根目錄執行 `python dashboard.py`。
