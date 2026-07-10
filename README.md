# loop-agent-lite

markdown/JSON 規劃 + python 無窮迴圈的極簡 agent 迴圈。
程式笨、agent 聰明:python 只管計數、派工、當場校驗、竄改還原與 reset;
所有語意判斷(計畫好壞、前人工作完整性)留給每輪全新 context 的 agent。

## 流程

```
(loop 外)模板 templates/ + 一般 agent session → 產 goal.md + PLAN.md → 人審 → commit 進 repo
    │
    ▼
python3 loop.py --repo <repo>
    │  preflight:validate 綠 + 工作樹乾淨 + goal/PLAN 已 commit,不合格第一行就擋
    ▼
規劃期:agent 讀 goal/PLAN/現有計畫 → create-plan(當場校驗 order)或 plan-ok
    │  共識:plan-ok 且無任何異動 → flag+1;create-plan 被 call(不論成敗)/有異動 → 歸零
    ▼  flag > 10
執行期:依 order 派工 task-N → agent 收拾現場 → 實作+commit,或 done task-N
    │  共識:done 且 HEAD 沒動+工作樹乾淨+驗證綠 → done+1;有異動/驗證紅 → 歸零
    │  done ≥ 3 → 記完成(含 sha)→ 派下一個
    ▼  最後一個任務收斂
REPORT.md
```

防線(全機械):驗證連紅 20 輪或 HEAD 停滯 300 輪 → `git reset --hard` 回最後綠點,
任務指標依「完成 sha 是否仍在歷史裡」一次退到位;同一任務 reset 達 100 次停機(`--stuck-stop`,預設關)。
goal / state.json 是受保護真相:agent 直接改檔會被偵測、還原、該輪作廢——
計畫只能透過 `work.py create-plan` 寫入(執行期凍結)。goal 另有雙重守門:
**每輪 spawn 前**不存在就 fail-closed 停機;**每輪結束後**消失就整輪 `reset --hard` 回輪初 sha
(該輪所有變更含 commit 一併作廢)。

流程加固:
- preflight:**首跑必須綠**;resume 已有綠點可錨定就放行紅燈啟動(紅燈連跳防線會自行 reset 回綠),
  紅燈訊息餵給下一輪 agent 先修;
- **goal 變更偵測**:停機期間改了 goal(commit)再 resume,會偵測 hash 變化 → console 警告+
  note 餵下輪 agent + dashboard 顯示「⚠ goal 已變更」chip;回規劃期或 plan 重新收斂後解除;
- prompts/ 只留最近 5 輪(稽核夠用不爆量);round log 只留當前輪;
- `loop.py --import-plan plan.json --start-phase exec`:dashboard 匯入的 CLI 等價(重置 state);
- 所有預設值(flag/done/timeout/red/stall/stuck)統一住 `dashboard.config.json` 的 `defaults` 區塊,
  表單可覆蓋前三顆,防線參數只在 config 改。

## 快速開始

```bash
# 1) 用 templates/ 對應模板,在一般 session 產 goal.md + PLAN.md,審完 commit 進 target repo
# 2) 跑(公司 CLI 用 --agent-cmd 換掉;prompt 走 stdin)
python3 loop.py --repo /path/to/repo \
  --agent-cmd "your-agent-cli -p" \
  --validate-cmd "mvn -q test"
```

常用參數:`--flag-threshold 10`、`--done-threshold 3`、`--red-limit 20`、`--stall-limit 300`、
`--round-timeout 30`(分鐘,0=不限;逾時 SIGKILL 整個 process group,殘留交下一輪判斷)、
`--stuck-stop --stuck-stop-count 100`、`--reset-state`、`--name <workspace>`。
agent 的 stdout/stderr 逐行直播在 console(前綴 `│`),同時落 `workspace/<name>/logs/`。
中斷直接 Ctrl-C,state 已落地,重跑同一條命令續跑;停機期間人工改 goal/PLAN 記得 commit(重啟會重拍快照)。

## Dashboard(唯讀,選用)

```bash
python3 dashboard.py            # 一個進程管 workspace/ 底下全部;--name 只是預選;--port 8765 起自動找
```

Dashboard 前端是獨立的 React + TypeScript + Vite 專案，原始碼在 `ui/src/`；
`dashboard.py` 只提供既有 `/api/*` 與本機 `ui/dist/` 靜態資源。Production 不需要 Node、
不連 CDN，也不會在執行時下載字型、icon 或其他資源。重新 build 前端:

```bash
cd ui
npm install
npm run build
```

前端 build 需要 Node.js 20.19 以上；這項需求只存在開發／建置機。
`ui/dist/` 會進版控，讓只有 Python 的內網環境可直接執行 dashboard。開發模式可先跑
`python3 dashboard.py --port 8765`，再於另一個 terminal 執行 `cd ui && npm run dev`；
Vite 會把 `/api` proxy 到本機 dashboard。

介面提供深色／淺色／跟隨系統三種主題，偏好、左右欄寬、完成任務與事件區塊的展開狀態
都只存在瀏覽器 `localStorage`，不會修改 workspace truth。

頂部 tabs = fleet 總覽(每個 workspace 的 phase 色點+進度),點擊即切換(支援 #hash 深連結)。
主畫面的 fleet/state/history/console 由單向 SSE (`/api/events`) 增量推送；瀏覽器斷線會自動重連，
寫入操作仍使用既有 REST POST。只有「執行中的 jobs」面板在打開時每 2 秒查詢一次。
**版面鎖 100vh**:頁面永不捲動,左(計畫表格)右(console)兩欄各自內部 scroll。
多個 loop 同時跑各自 workspace,開一個 dashboard 就夠;`--read-only` 起唯讀實例分享給別人看。

左欄(計畫表格):
- 已完成任務預設收合成一行(點擊展開/收合,記憶偏好);切進 workspace 自動捲到進行中任務;
- 任務文字 clamp 3 行(點擊展開;進行中任務不縮);React 保留未變區塊與你的捲動位置;
  捲離進行中任務時出現「→ 回到執行中」浮鈕一鍵跳回;
- header 有 任務 n/N 進度、⚠ issues 紅章——點擊開**彈窗表格**(round/位置/內容/時間,最新在上、
  可捲動、開著會隨 SSE 即時更新,內含清空鈕);issues 來自 agent 的 `work.py issue` 結構化回報;
- 規劃期 plan 更新時**變動的列亮一閃+plan chip 閃**(v4 動態樹的極簡版),
  plan 版本異常增長(≥10)標黃提示可能震盪。

右欄(console 直播):
- 超長 log 首抓只載尾段(64KB)秒開;在底部就跟著 tail,永遠看得到最新 print;
- 往上翻閱時出現「⤓ 跟到最新」浮鈕一鍵回底;顯示緩衝上限 300KB,最舊自動丟棄。

**Launcher(＋ 啟動/管理)**:表單填一填直接開 loop。
- agent 命令 = `dashboard.config.json` 固定選項(前端只能選,塞不進任意命令);
- validate = 預設選項(mvn compile / mvn test / react 三層)或手寫;
- repo = config `repo_roots` 掃出來的 git repo 下拉點選,或手動輸入;
- goal.md(gate#1):表單選檔,**隨啟動自動 commit**(固定檔名、指定 pathspec 絕不掃到其他 WIP;
  內容沒變不產生新 commit;留空=沿用 repo 已 commit 的版本);
- 「在新 branch 跑」勾選:啟動前 `git checkout -b loop/<ws>`(已存在就 checkout 續用),不弄髒主線,
  deliver=你自己 merge;
- config `notify_cmd`:終態通知(completed / stuck_stop / goal_missing),佔位符 `{status}` `{name}`,
  跑整夜結果推到 webhook,失敗只記 warning 不擋主流程;
- **匯入 plan.json**(v4 import plan 的 lite 版):貼上任務清單 JSON(create-plan 同一套校驗;
  貼上當下即時紅框警告格式錯誤,「📋 複製範本」一鍵拿格式),
  = 建全新 state(舊進度清除),並由你選擇**從規劃期開始(讓 agent 補完)或直接進執行期**;
- 「重置 workspace state」勾選 = `--reset-state`(沒貼 plan.json 時才有意義);
- 同名/同 repo 已在跑會擋;preflight 失敗直接顯示在 job 輸出;
- ⚠️ **關閉 dashboard 會停掉由它啟動的全部 loop**:先 SIGINT 優雅收尾(loop 存 state、
  殺掉自己的 agent process group),8 秒沒死再 SIGKILL。state 已落地,重啟即續跑。

**Workspace 控制(header 按鈕;全部只在停止時可用,執行中鎖死)**:
- workspace 清單=掃 `workspace/*/state.json`(loop 會把 repo path/agent/validate 命令與
  自己的 pid 寫進去),所以**外部啟動的 loop 一樣能看到、能停**;
- ▶ 運行:設定全部取自 state.json,agent 命令執行前再過一次 config 白名單
  (state 是 agent 摸得到的檔案,白名單不過=拒跑、改走啟動表單);
- ⏹ 停止:SIGINT 優雅收尾(dashboard job 或外部 pid 皆可);
- ✎ 編輯計畫:改任務文字敘述與 done 計數(不能增刪任務);
- ⚙ 設定:改 agent 命令(config 白名單)、validate 命令、五顆旋鈕(flag/done/timeout/red/stall),
  存回 state.config、下次 ▶ 運行生效——就是啟動表單那些設定,停止後隨時能調;
- ⏪ 回規劃期 / ⏩ 進執行期:phase 切換;回規劃期會把執行進度全部歸零(計畫保留);
- **進度管理(任務列 ⏵)**:退回 task-N=清掉 N 起的完成紀錄重做(code 不動,交輪次驗收);
  往前跳=中間任務標「✔ 人工」,且**先跑 validate、綠燈才放行**(同 preflight 原則)。
  loop 每輪只保留當前輪的 agent log(round-*.log),舊輪自動清除。

## Workspace 佈局(`workspace/<name>/`)

- `state.json` — 唯一真相:phase / flag / plan / 進度 / 完成 sha / reset 統計
- `history.log` — 一輪一行;`prompts/`、`logs/` — 每輪 prompt 原文與 agent stdout
- `REPORT.md` — 收斂後的總結

## agent 可用命令(prompt 內已附完整指令)

- `work.py create-plan [json檔]` — 整包重交計畫(stdin 或檔案);order 必須 1..N 連續不重複
- `work.py plan-ok` — 宣告計畫完整
- `work.py done task-N` — 宣告當前任務完成(task id 核對,錯了當場退)
