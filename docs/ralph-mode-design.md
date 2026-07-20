# Ralph 模式接入設計

> 狀態：設計提案（尚未實作）。
> 目標：讓 Dashboard 能以「Ralph 原生格式」啟動與監控公司內的 `ralph.sh`，
> 與既有 loop coordinator 並存，互不干擾。

## 1. 背景

公司環境只允許以固定介面執行 Ralph：

```bash
sh ralph.sh 5000 opencode "xxmodel"
#           │    │         └ 模型字串
#           │    └ agent CLI（opencode / claude / amp ...）
#           └ 最大迭代次數
```

Ralph（[snarktank/ralph](https://github.com/snarktank/ralph)、源自 Geoffrey Huntley 的
Ralph Wiggum 技法）是一個純 bash 迴圈：每輪 spawn 一個**全新 context** 的 agent，
挑出 PRD 裡最高優先且未完成的 user story 實作、跑檢查、commit、更新狀態，直到全部完成。

Ralph 的「狀態」不在任何 coordinator 裡，而是 repo 內的檔案：

| 檔案 | 角色 |
|---|---|
| `prd.json` / `prd.md` | 任務真相：user stories 清單，每項帶完成標記（`passes: true/false` 或 checkbox） |
| `progress.txt` | append-only 的逐輪紀錄與學習筆記，供後續迭代的 fresh-context agent 讀取 |
| `prompt.md` / `AGENTS.md` | 每輪餵給 agent 的 prompt 模板與專案慣例 |
| `archive/YYYY-MM-DD-*/` | 換 feature（不同 branchName）時自動歸檔的舊 run |

完成偵測：所有 story `passes: true`，agent 輸出 `<promise>COMPLETE</promise>` sentinel，
ralph.sh 隨之退出；或達到最大迭代次數。

> ⚠️ 公司內部版 ralph.sh 的介面需實作前逐項確認（見 §9 開放問題）：
> 參數順序、PRD 實際檔名與位置、progress.txt 位置、每輪 stdout 是否有 banner、
> 退出碼語意。本文以 snarktank 慣例為基準描述。

## 2. 核心決策：原生 Ralph 模式，而非轉譯成既有格式

兩條路：

- **A. 轉譯層**：把 ralph 的產出映射進既有 `state.json` schema（story → plan task、
  iteration → round），讓現有 UI 原樣可用。
- **B. 原生模式（建議）**：新增一種 `runner: "ralph"` 的 workspace 型別，
  以 ralph 自己的檔案（PRD + progress.txt + git log + stdout）為唯一真相，
  Dashboard 只做**唯讀投影**；沿用啟動、console、SSE、fleet 等通用管線。

**建議採 B**，理由：

1. **既有 coordinator 機制與 ralph 根本衝突，不只是「不合用」。**
   `engine/loop.py` 的核心價值在共識 gate（flag/done 門檻）、每輪 validate、
   防竄改（agent 直接改受保護檔會被 `git reset --hard` 作廢該輪，`engine/loop.py:2211-2244`）。
   ralph 的正常行為恰好是這些機制眼中的違規：agent 自己改 PRD、自己 commit、
   自己判定完成。硬套 = 每一輪都被 reset。
2. **執行引擎不可替換。** 公司限制只能跑 `sh ralph.sh ...`，無法把 loop coordinator
   塞進去，也不能改 ralph.sh 加回報鉤子。能做的只有：spawn 它、讀它的 stdout、
   watch 它的檔案。這天然就是「唯讀投影」架構。
3. **語意對不上，轉譯會說謊。** flag/done 計數、紅連跳、停滯 reset、plan version、
   round token——這些欄位在 ralph 下沒有對應事實；填假值會讓監控畫面失去可信度，
   而監控可信正是這個 Dashboard 的立身之本。
4. **ralph 的檔案本來就是為「被讀」設計的。** PRD 的完成標記與 progress.txt 的
   append-only 格式，唯讀 parser 即可穩定投影，不需要寫入路徑，也就沒有競態。

既有 loop 模式**完全不動**：兩種 runner 並存，同一個 Dashboard、同一個 fleet 畫面。

## 3. 架構總覽

```text
Dashboard（engine/dashboard.py）
  │  POST /api/launch  { runner: "ralph", repo, name, iterations, tool, model, prd? }
  ▼
spawn_loop 泛化 → subprocess: python -m engine.ralph --repo ... --name ...
  │                （沿用 Job 註冊表、startup handshake、SIGINT→SIGKILL 停止）
  ▼
engine/ralph.py（新增的薄監督層，supervisor）
  ├─ 建 workspace/<name>/、acquire_run_lock（單 writer 鎖沿用）
  ├─ 寫 minimal state.json：runner/loop:{pid,session_id}/config/ralph 投影區塊
  ├─ spawn: sh ralph.sh <iterations> <tool> <model>（cwd=target repo, 獨立 process group）
  │     stdout/stderr → console.log（🤖 前綴）＋ logs/ralph-run.log
  ├─ watcher：輪詢 PRD / progress.txt / git HEAD → 更新 state.json 的 ralph 區塊
  └─ ralph.sh 退出 → 判定完成/中斷 → phase=done 或標記 interrupted → notify_cmd
  ▼
SSE（既有 /api/events）把 state.json 與 console 推給前端
  ▼
UI：runner==="ralph" 的 workspace 改渲染 RalphView
  （story checklist ＋ iteration 進度 ＋ progress.txt ＋ 共用 ConsolePane）
```

關鍵 seam（來自架構盤點）：`/api/state` 與 SSE `state` event 都是把 `state.json`
原樣送出（`engine/dashboard.py:1804-1808`），schema 的解讀全部在 React 元件層。
因此後端只要寫出一份「型別合法」的 state.json，前端加一個 runner 分支即可，
不需要動 SSE、console tail、fleet 傳輸任何一行。

## 4. 後端設計

### 4.1 `engine/ralph.py`（新檔，supervisor process）

`python -m engine.ralph --repo <path> --name <ws> --ralph-cmd <script> --iterations N --tool opencode --model <str> [--notify-cmd ...]`

職責刻意薄——**不指揮 ralph，只看著它**：

- **啟動**：沿用 `Workspace` 目錄建立與安全檢查（O_NOFOLLOW、workspace 名稱規則、
  `acquire_run_lock` 單 writer 鎖，`engine/loop.py:614`）。preflight 只檢查：
  repo 是 git repo、ralph script 存在可執行、PRD 檔存在（或本次啟動有匯入）。
  **不要求工作樹乾淨、不跑 validate**——那是 loop 模式的契約，ralph 自己管。
- **spawn**：`subprocess.Popen(["sh", ralph_script, str(iterations), tool, model], cwd=repo, start_new_session=True, stdout=PIPE, stderr=STDOUT)`，
  reader thread 把 stdout 逐行寫入共用 `console.log`（沿用 `append_console` 的
  跨 process 鎖與輪替）與 `logs/ralph-run.log`。寫入 `startup_ready.json`
  完成既有的啟動 handshake（`job_startup_status`，`engine/dashboard.py:528`）。
- **watcher（輪詢，2–3 秒一次，與 SSE 節流同級）**：
  - 讀 PRD → 解析 stories 與完成標記；
  - `progress.txt` 檔案大小/mtime → 有 append 視為一次迭代活動；
  - `git -C repo log` 計數（自啟動基準 SHA 起）→ commits 數與最新 commit 摘要；
  - stdout 掃描迭代 banner 與 `<promise>COMPLETE</promise>` sentinel；
  - 任何變化 → 原子改寫 state.json 的 `ralph` 區塊（`atomic_write_bytes` 沿用）。
- **結束判定**：ralph.sh 正常退出且 PRD 全部完成（或偵測到 sentinel）→ `phase: "done"`、
  觸發 `notify_cmd`；退出但未全完成 → 標記 `ralph.exit: "iterations_exhausted" | "failed"`，
  phase 仍轉 `done` 以外的處理見 §6 停止語意。
- **停滯警示**：可設定 N 分鐘內 stdout 無輸出、PRD/progress/HEAD 皆無變化 →
  `ralph.stalled: true`（只警示、不 kill；ralph 單輪可能合法地跑很久）。

### 4.2 state.json 形狀（ralph workspace）

必須通過 `validate_state_shape`（`engine/loop.py:859`）——它只檢查**已知欄位存在時**
的型別，未知欄位放行。設計上取交集：

```json
{
  "runner": "ralph",
  "phase": "exec",
  "loop": {"pid": 12345, "session_id": "…", "started_at": "…"},
  "config": {
    "repo": "/path/to/target",
    "ralph_cmd": "sh /path/to/ralph.sh",
    "iterations": 5000,
    "tool": "opencode",
    "model": "xxmodel",
    "notify_cmd": ""
  },
  "ralph": {
    "prd_path": "prd.json",
    "stories": [{"id": "US-1", "title": "…", "passes": true}],
    "stories_done": 3,
    "stories_total": 8,
    "iteration_activity": 17,
    "base_sha": "…",
    "head_sha": "…",
    "commit_count": 12,
    "last_commit": "feat: …",
    "progress_bytes": 48213,
    "sentinel_complete": false,
    "stalled": false,
    "exit": null
  }
}
```

- `phase` 借用既有枚舉：執行中 `exec`、結束 `done`（fleet 卡片與 favicon 的
  執行中/完成邏輯直接可用）。`plan` 不使用。
- `flag/done_count/plan/completed` 等 coordinator 欄位**一律不寫**——寧缺勿假。
- `loop.pid` 沿用，讓既有的 stale-PID 偵測、`/api/stop` 的 pid fallback
  （`engine/dashboard.py:3119-3146`）與 fleet 的 running 判定原樣工作。

### 4.3 Dashboard API 變更

- **`POST /api/launch`** 增加 `runner` 欄位（預設 `"loop"`，向後相容）。
  `runner: "ralph"` 時走新分支 `api_launch_ralph`：
  - 參數：`repo`、`name`、`iterations`（1–100000）、`tool`（shared config 白名單）、
    `model`（自由字串，長度上限）、`prd_content` + `prd_format`（選填，匯入時寫入
    repo 並 commit，與現行 `goal_content` 匯入同一套安全檢查與交易語意，
    `engine/dashboard.py:2153-2173`）、`new_branch`（選填）。
  - `ralph.sh` 路徑**不由前端傳入**：從 shared config 的 `ralph.scripts` 白名單
    選 index（同 `agent_idx` 的設計，避免任意命令注入，`engine/dashboard.py:2048`）。
  - 沿用 `JOBS_LOCK` 下的衝突檢查：同 workspace 或同 repo 已有活 runner（不論
    loop 或 ralph）一律拒絕（`engine/dashboard.py:2128-2138`）。
  - `spawn_loop` 泛化為 `spawn_runner(name, repo, argv)`：`Job` 註冊表、stdout deque、
    startup handshake、`Job.stop()` 全部照舊（`engine/dashboard.py:326-487`）。
- **`GET /api/ralph/prd?ws=<name>`**：回傳解析後的 stories ＋ PRD 原文（唯讀）。
- **`GET /api/ralph/progress?ws=<name>&offset=N`**：progress.txt 增量讀取，
  直接沿用 `read_incremental` 的 byte-offset ＋ inode 輪替處理（`engine/dashboard.py:1503`）。
- **`POST /api/stop`**：不變。ralph workspace 的停止語意見 §6。
- **不提供**給 ralph 的端點：`/api/drain`（ralph.sh 無輪間停止點）、`/api/phase`、
  `/api/set-task`、`/api/import-plan`、`/api/edit-state`——後端在這些 handler 開頭
  檢查 `runner` 並拒絕，避免誤操作。

### 4.4 shared config 增補（`engine/dashboard.config.shared.json`）

```json
{
  "ralph": {
    "scripts": [{"label": "公司 ralph", "cmd": "sh /opt/tools/ralph.sh"}],
    "tools": ["opencode", "claude", "amp"],
    "default_iterations": 100,
    "prd_filenames": ["prd.json", "prd.md"]
  }
}
```

個人覆寫走既有 `dashboard.config.local.json` 機制。

## 5. 前端設計

### 5.1 型別與資料流

- `types.ts`：`WorkspaceSummary` 與 `WorkspaceState` 加 `runner?: "loop" | "ralph"`；
  新增 `RalphState`（對應 §4.2 的 `ralph` 區塊）。缺 `runner` 欄位 = 舊 loop workspace。
- `useDashboardData` **零修改**：SSE `state`/`console`/`workspaces` 事件原樣流入。

### 5.2 `RalphView`（新元件，取代 ralph workspace 的 `WorkspaceView` 主體）

`App.tsx` 依 `state.runner` 分流；共用外框（header、splitter、ConsolePane）不動。

- **狀態列**：`Stories 3/8` 進度、活動計數（progress append 次數）、commit 數與
  最新 commit、stalled 警示、`iterations` 上限、啟動時間與 elapsed。
- **PRD checklist 面板**（取代 PlanTable 的位置）：story 清單、完成打勾、
  唯讀。資料來自 SSE state 的 `ralph.stories`，點開叫 `/api/ralph/prd` 看原文。
- **Progress 面板**：progress.txt 尾段 viewer（增量拉取），這是 ralph 的
  「學習筆記」，價值等同 loop 模式的輪次紀錄。
- **ConsolePane 原樣共用**：ralph stdout 已寫入 console.log，來源前綴過濾照舊。
- **隱藏/不渲染**：phase 切換鈕、flag/done chips、RoundSparkline、PlanEditor、
  TaskDiff、Issues、Timeline、RunCompare 等 coordinator 專屬 UI（盤點見架構
  報告 §7；這些元件對 ralph 無對應事實）。

### 5.3 Launcher

`LauncherModal` 加模式切換（分頁或 radio）：「Loop coordinator」｜「Ralph」。
Ralph 表單欄位：

- target repo（沿用 repo roots 選擇器與 `/api/repo-status`）
- workspace 名稱（沿用命名規則）
- ralph script（shared config 白名單下拉）
- iterations（數字，預設取 shared config）
- tool（白名單下拉）＋ model（文字）
- PRD：三選一——「repo 已有 PRD」（顯示偵測結果）／「貼上匯入」／「PRD 產生器」
- 選填 new branch

**PRD 產生器**：沿用 `engine/prompts/external-agent-*.md` 的資源模板機制，
新增 `external-agent-ralph-prd.md`：把需求編譯成「user stories JSON（含
`branchName`、每 story 的驗收標準與 `passes: false`）」的產生 prompt，
使用者貼給任意 agent 產出後貼回匯入。與現行 Goal/Plan 產生器同一互動模式，
純瀏覽器操作、不動 repo。

### 5.4 Fleet 整合

- ralph workspace 卡片：顯示 `Stories x/y` 與活動計數，取代 round/flag/done 區塊；
  running/done/停止狀態燈號沿用（由 `phase` ＋ `loop.pid` 驅動，免費獲得）。
- 全域輪次統計（未回 DONE 異常率等）**不納入** ralph workspace——樣本語意不同，
  混入會污染統計。卡片標示 runner 型別即可。

## 6. 停止語意與風險邊界

- **「本輪後停止」不提供。** ralph.sh 沒有輪間控制點，我們也不能改它。
  UI 只保留「立即停止」，確認視窗明示語意差異。
- **立即停止**：SIGINT → 8 秒 → SIGKILL 整個 process group（既有 `Job.stop()`）。
  中斷可能在 target repo 留下未 commit 殘留——這在 ralph 哲學裡是**可接受的**：
  下一次啟動的 fresh-context agent 本來就被要求先收拾現場。停止後 state 標記
  `ralph.exit: "interrupted"`，卡片顯示「已中斷，可重啟續跑」。
- **重啟 = 重新 launch**：ralph 天然可續跑（PRD 未完成項就是 resume point），
  不需要 loop 模式那套 resume 檢查。`/api/run` 對 ralph workspace 的語意就是
  以保存的 config 重新 spawn。
- **防竄改機制不適用也不啟用**：不做 goal/plan 快照、不 reset、不驗證綠點。
  workspace 檔案層的安全檢查（O_NOFOLLOW、symlink 拒絕、原子寫、單 writer 鎖、
  名稱規則）**全部保留**——那是 Dashboard 自身的完整性，與 runner 無關。
- **信任邊界**：ralph.sh 與 tool 白名單由 shared config（team 檔案，進版控）
  控制；前端只能傳 index 與受驗證的純量參數，不能組任意命令。

## 7. 實作里程碑

| 里程碑 | 內容 | 驗收 |
|---|---|---|
| **M1 跑起來** | `engine/ralph.py` spawn/console/state 骨架；`/api/launch` runner 分支＋`spawn_runner` 泛化；`RalphView` 最小版（狀態列＋ConsolePane）；停止/重啟 | Dashboard 能啟動 fake ralph.sh、看 console、停止；loop 模式回歸測試全綠 |
| **M2 看得懂** | PRD parser（`prd.json` passes ＋ `prd.md` checkbox 雙格式）；watcher 投影；PRD checklist ＋ progress 面板；fleet 卡片；完成偵測＋notify | stories 進度即時更新；全完成後卡片轉「完成」；stalled 警示可觸發 |
| **M3 好用** | PRD 產生器模板；PRD 匯入（寫 repo ＋ commit）；new branch；archive 瀏覽（唯讀）；中斷/耗盡迭代的明確終態 UI | 全流程不離開 Dashboard：產 PRD → 啟動 → 監控 → 完成報告 |

測試策略：

- unittest：PRD parser 雙格式與畸形輸入 fail-closed；state 投影通過
  `validate_state_shape`；watcher 對檔案輪替/刪除的容錯。
- fixture：`tests/fake_ralph.sh`——模擬逐輪輸出、更新 PRD、append progress、
  sentinel 退出；供 CLI 與 Dashboard e2e 共用。
- e2e（Playwright）：launch ralph → checklist 進度 → 停止 → 重啟續跑。

## 8. 明確的非目標

- 不修改、不包裝、不 fork ralph.sh；不注入額外 prompt 或鉤子。
- 不把 loop coordinator 的共識/validate/reset 機制套到 ralph 上。
- 不提供從 Dashboard 編輯 PRD 的寫入路徑（M3 的匯入是「啟動前交易」，
  不是執行中編輯）；執行中的 PRD 唯一 writer 是 ralph 的 agent。
- 不做兩種 runner 間的 workspace 轉換。

## 9. 開放問題（實作前需向公司環境確認）

1. **ralph.sh 確切介面**：參數順序是否為 `<iterations> <tool> <model>`？
   有無其他參數（PRD 路徑？工作目錄假設？）退出碼語意？
2. **PRD 檔名與格式**：`prd.json`（snarktank 式 `passes` 布林）還是 `prd.md`
   （checkbox）？（原需求提到「prd.sh」，需確認是否為筆誤。）放 repo root
   還是 `scripts/ralph/`？
3. **progress.txt 位置**：repo root？是否隨 archive 搬移？
4. **每輪 stdout 有無 banner**：決定 iteration 計數用 stdout 解析還是只能靠
   progress append 推定。
5. **公司 agent CLI（opencode）的非互動行為**：SIGINT 是否乾淨退出？
   決定停止流程是否需要調整 8 秒的 SIGKILL 緩衝。
