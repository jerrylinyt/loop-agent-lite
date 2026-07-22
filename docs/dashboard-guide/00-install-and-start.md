# 流程 00：安裝並啟動 Dashboard

## 目的

把 loop-agent-lite 的本機 Dashboard 跑起來，確認瀏覽器能開啟，並了解普通 Loop、Parallel Loop 與受管 worker 的資料會存在哪裡。這個流程只安裝與啟動控制台，不會啟動任何 Agent loop。

## 前置條件

- 已安裝 Python 3。
- 已下載或 clone `loop-agent-lite`。
- 終端機目前位於 loop-agent-lite 專案根目錄。
- 另有一個要讓 Agent 工作的 Git repo；本流程稱它為 target repo。

## 操作步驟

### 1. 建立隔離的 Python 環境

在 loop-agent-lite 根目錄執行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

成功判定：終端機提示字首通常會出現 `(.venv)`，而安裝命令沒有錯誤。即使目前 `requirements.txt` 沒有第三方套件，仍應執行，因為這是專案固定的依賴入口。

### 2. 啟動 Dashboard

```bash
python dashboard.py
```

終端機會印出本機網址。預設從 `http://127.0.0.1:8765/` 開始；如果 port 已被占用，程式會自動往後找可用 port，所以應以終端機實際顯示的網址為準。

### 3. 在瀏覽器開啟網址

第一次且尚無 workspace 時，畫面會提供三步引導：選 repo、準備 Goal／Plan、確認 Validate 後啟動。已有 workspace 時，會直接看到 workspace 詳細頁或 Fleet 總覽。

成功判定：頁面上方看得到「總覽」、「⌘K」與「＋ 啟動／管理」。

點開「＋ 啟動／管理」→「啟動新 loop」後，會看到 runner 分頁：

- `Loop coordinator`：既有的規劃期／執行期單一 coordinator。
- `Parallel Loop`：匯入人工審核過的 frozen plan，固定從 exec 啟動，再由 supervisor 管理並行 workers。
- `Ralph`：Ralph runner 的獨立啟動表單。

切換分頁只是在查看表單；尚未按「啟動」前不會建立 workspace 或執行 Agent。

### 4. 確認這次只是啟動 Dashboard

看到頁面不代表 Agent 已開始工作。只有完成啟動表單並按「啟動」，或在停止的 workspace 按「運行」後，loop 才會執行。

## 每次回來怎麼啟動

```bash
source .venv/bin/activate
python dashboard.py
```

## 可選啟動方式

```bash
# 指定起始 port
python dashboard.py --port 8766

# 開啟後預選某個 workspace
python dashboard.py --name my-workspace

# 純監看，停用會改變狀態的操作
python dashboard.py --read-only
```

`--read-only` 適合電視牆或監控頁；它不是建立／修改 loop 的模式。

## 資料會存在哪裡

- 個人設定：`dashboard.config.local.json`，已加入 `.gitignore`。
- 團隊預設：`engine/dashboard.config.shared.json`。
- Workspace 資料：loop-agent-lite 專案內的 `workspace/<name>/`。
- 實際程式碼：在你選擇的 target repo，不在 workspace 資料夾。

典型 workspace 內容：

```text
workspace/<name>/
├── state.json
├── state.last-good.json
├── console.log
├── history.log
├── logs/
├── prompts/
├── snapshots/
└── REPORT.md
```

Parallel 會以一個 base workspace 保存 supervisor state，並在 run 期間保存 durable parallel artifacts；目前活躍的每個 task 可能另有受管 worker workspace：

```text
workspace/<base>/
├── state.json
├── console.log
├── REPORT.md
└── parallel/<run-id>/

workspace/<base>--<run-id>-task-<N>/   # 活躍或保留診斷現場的 managed worker
```

managed worker 由 parent supervisor 建立、恢復、清理或歸檔。不要手動啟動、編輯、重設或刪除它；Dashboard 也只提供唯讀畫面。所有控制都回到 `<base>` 的 Parallel workspace 操作。

## 新手常犯錯誤

- 在 target repo 執行 `python dashboard.py`：Dashboard 應從 loop-agent-lite 根目錄啟動。
- 關掉終端機後仍期待頁面工作：Dashboard Python process 停掉，瀏覽器就無法更新。
- 固定輸入 8765：若 port 被占用，請看終端機實際網址。
- 把 workspace 當成 repo：workspace 放協調資料；target repo 才是程式碼工作區。
- 把 Parallel worker 當成一般 loop：worker 只執行被指派的 task，生命週期與 Git 整合都由 parent supervisor 管理。
- 同時用普通 Loop 與 Parallel 操作同一個 target repo：兩者共用 writer／owner 防線；普通 owner 要先完成或停止，Parallel run 則要完成或 Abort 到 `cancelled`，才能啟動另一個 runner。Pause 只供同一 run Resume。
- 直接雙擊 Python 檔：這樣不容易看到錯誤與實際 port，建議從終端機啟動。

## 完成檢查

- [ ] `.venv` 已建立並啟用。
- [ ] `python dashboard.py` 正在執行。
- [ ] 瀏覽器可開啟終端機顯示的網址。
- [ ] 能看到「＋ 啟動／管理」。
- [ ] 打開啟動表單時看得到 `Loop coordinator`、`Parallel Loop` 與 `Ralph` runner 分頁。
- [ ] 尚未誤按啟動任何 workspace。

下一步：[完成第一次個人設定](01-first-time-personal-settings.md)。
