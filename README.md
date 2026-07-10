# loop-agent-lite

用 Python 協調 agent 的規劃／執行迴圈，並提供一個可在瀏覽器操作的 Dashboard。

![Dashboard 執行中展示](docs/dashboard-running.jpg)

展示圖以 mock fleet 呈現執行中、規劃中、驗收中與已完成等 workspace 狀態；實際資料會由 `workspace/*/state.json` 提供。左側顯示 Loop 狀態與驗證紀錄，右側顯示 Agent 輸出；兩側可拖曳調整寬度或收合。

## 流程

```text
準備 target repo
  └─ goal.md + PLAN.md 已審核並 commit
          │
          ▼
Dashboard 啟動 loop（或直接執行 loop.py）
          │
          ├─ preflight：validate、工作樹、goal/PLAN commit 檢查
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

每輪都會保護 `goal.md`、計畫與 state。驗證失敗或偵測到竄改時，會回到最後綠點。`--reset-state` 和 Dashboard 的 plan 匯入都是交易式操作：新流程未通過啟動檢查時，舊進度仍保留。

Loop 另以 OS 鎖維持單 writer：同一 workspace 或同一 Git worktree 不能同時跑兩個 loop（即使來自不同 Dashboard／終端機）。不同 Git worktree 可各自運行，保留日後有限並行的隔離邊界；目前不會自動拆任務、合併分支或建立多份協調 state。

## 快速開始

### 1. 準備 target repo

在 target repo 建立並 commit `goal.md`、`PLAN.md`，確認驗證命令可在該 repo 執行。

### 2. 啟動 Dashboard（推薦）

```bash
python3 dashboard.py --port 8766
```

開啟 <http://127.0.0.1:8766/>，在「啟動／管理」設定：

- target repo
- Agent CLI（例如 `claude -p`）
- validate command（例如 `python3 -m unittest discover -s tests -t . -q`）

找不到 CLI 時，點 Agent CLI 旁的齒輪，設定 CLI 命令及其 PATH 目錄；也可以直接填可執行檔的絕對路徑，再按「測試」。

### 3. 直接執行 loop（可選）

```bash
python3 loop.py \
  --repo /path/to/repo \
  --agent-cmd "claude -p" \
  --validate-cmd "python3 -m unittest discover -s tests -t . -q"
```

Agent prompt 會經由 stdin 傳入，stdout／stderr 會逐行寫入 workspace log。每輪都有獨立 token，舊輪殘留命令不會被下一輪誤收；CLI 主程序退出時也會清理同 process-group 的背景子行程。中斷後重新執行相同命令即可從 `state.json` 繼續。

常用選項：

```text
--name <workspace>       指定 workspace 名稱
--reset-state             清除舊進度後重新開始（驗證成功才套用）
--import-plan <file>      匯入 plan JSON 並建立新 state
--start-phase exec        搭配匯入 plan，直接進入執行期
--round-timeout <分鐘>    單輪上限，0 表示不限
--agent-backoff-max <秒>   CLI 連續異常時 1,2,4…秒退避上限，0 表示關閉
--validate-timeout <秒>   驗證命令上限
--stuck-stop              同一任務反覆 reset 達上限時停機
```

## Dashboard 操作

- 左側是 Loop 狀態；右側是 Agent 輸出，可切換 Agent／其他／全部。
- 分隔線可拖曳調整欄寬；箭頭可收合，設定會保存在瀏覽器。
- 正常要停時用「本輪後停止」：目前 Agent、Validate 與 state/history 落盤完成後才停，不會啟動下一輪。
- Agent CLI 卡死或明顯失控時用「立即停止」；它會中斷目前 round，state 可在下次運行時續用。
- 停止後可編輯計畫、切換階段或修改 agent／validate 設定，再按 ▶ 運行。
- 「重置 workspace state」會保留舊 state，直到新流程通過 preflight。
- 「匯入 plan」會建立全新的 state；可選擇從規劃期或執行期開始。
- 迴圈完成、停止或發生啟動錯誤時，Dashboard 會顯示結果與 log 尾段；啟動成功前不會關閉視窗。

## 團隊設定與個人設定

- `dashboard.config.shared.json`：團隊共用、應提交到 Git 的預設值。
- `dashboard.config.local.json`：個人 CLI、PATH、repo roots 與通知設定，已加入 `.gitignore`，不應提交。

第一次使用請在 Dashboard 的設定頁完成個人 CLI／PATH／repo roots 設定；不同電腦只需各自建立 local 設定，不會改動團隊檔案。

## Workspace 檔案

```text
workspace/<name>/
├── state.json       目前進度與執行設定
├── state.last-good.json  最近一次合法 state 的復原副本（主檔不可讀時才使用）
├── stop-after-round.json  暫時的 session-scoped 平順停止請求（loop 消耗後刪除）
├── console.log      完整流程紀錄
├── logs/             每輪 Agent 原始輸出
├── prompts/          最近幾輪送出的 prompt
└── REPORT.md        全部任務完成後的摘要
```

## 常見問題

**Agent CLI 顯示找不到檔案**

在 Agent CLI 管理器填入正確命令，並把 CLI 所在目錄加入 PATH；按「測試」確認後再啟動。GUI 啟動的 PATH 可能和終端機不同。

**validate 失敗或逾時**

先在 target repo 手動執行同一個 command，確認工作目錄與依賴正確，再回 Dashboard 修改命令或 timeout。逾時會終止 validator 的整個 process group。

**workspace 顯示沒有 state.json**

若 `state.last-good.json` 存在，Dashboard／loop 會自動復原主檔並留下 🛟 紀錄；兩份都不存在時，請從 Dashboard 的啟動表單重新啟動或使用 `--reset-state`。不要在 loop 執行中手動刪除 workspace 檔案。

## 開發與測試

```bash
# Python 協調層
python3 -m unittest tests.test_guards

# Dashboard 前端（需要 Node.js）
cd ui
npm install
npm run check
```

`ui/dist/` 已包含 production 靜態檔，只有 Python 的環境也能直接執行 Dashboard。
