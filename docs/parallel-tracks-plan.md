# 規劃書:任務並行拆分 × Worktree 隔離 × 收斂式合併(fleet 並行架構)

> 狀態:提案(未實作)。本文件是可行性規劃書,供人工審閱後拍板分期實施。
> 對應需求:「拆解任務時讓 agent 拆分成可並行模式,幫他們開 worktree,模式參照現在的方式
> 重複嘗試直到收斂,然後才同意合回來(由 agent 自己 merge main 到 worktree 自己解衝突),
> 由程式判斷 done ≥ threshold + 可 ff merge。」

---

## 0. TL;DR 結論

**可行,而且與現有架構高度相容。** 現有程式碼已為這個方向預留了關鍵邊界:

- 單 writer 鎖是「每個 Git worktree 一把」(`engine/loop.py:1416`,鎖檔放在 worktree 專屬
  git-dir),README 明文保留「不同 Git worktree 可各自運行」的並行隔離邊界(README.md:34)。
- 收斂語意天然可組合:執行期「任何變更 → done 歸零」(`engine/loop.py:1594-1603`)正好就是
  「merge main 進來 → 重新累積共識」所需要的機制,不用發明新規則。
- `--import-plan --start-phase exec`(`engine/loop.py:1712-1728`)讓子迴圈可以直接吃一份
  切好的計畫從執行期開跑;`--pause-after-plan`(`engine/loop.py:2092-2096`)是現成的
  「拆分結果人工審核點」。
- Dashboard 本來就以 workspace 為單位監控多個 loop(`engine/dashboard.py:429`),
  每條並行軌道就是一個普通 workspace,MVP 期間 UI 幾乎零改動。

**難度總評:中高。** 合併閘門本身(done 共識 + ff-only)反而是小而清晰的一塊;
真正的工程量在(1)任務格式 v2 的全面漣漪、(2)父協調器(fleet)的生命週期與 resume 可靠性、
(3)符合本 repo fail-closed 標準的測試(現有 `tests/test_guards.py` 已有 4,107 行,
新功能的測試量級相當)。

**建議路線:四期分階,每期獨立可用、可停損**(§11)。粗估總工期 22–33 個工作天(單人);
MVP(Phase 0+1,並行執行+人工合併)約 8–12 個工作天。

---

## 1. 需求還原

把一句話需求拆成可驗收的條目:

| # | 需求 | 本文件對應 |
|---|------|-----------|
| R1 | 規劃期 agent 把任務拆成「可並行的軌道(track)」 | §4 任務格式 v2、§5.1 規劃期 prompt |
| R2 | 程式替每條軌道開 Git worktree,各自隔離執行 | §3、§6.1 worktree 生命週期 |
| R3 | 每條軌道沿用現行模式:單輪無狀態 agent、共識 AND gate、重複嘗試直到收斂 | §5.2 子迴圈重用 |
| R4 | 收斂後才允許合併;合併前由 agent 在自己的 worktree 把 main merge 進來、自己解衝突 | §5.3 合併期(merge phase) |
| R5 | 合併回 main 由**程式**判斷:done 共識達門檻 **且** 可 fast-forward merge | §6.2 合併閘門(CAS) |
| R6 | 任務格式需調整以承載拆分資訊 | §4 |

註:現行執行期收斂條件是 `done_count >= done_threshold`(預設 3,`engine/loop.py:1605`),
本文統一寫 `done ≥ threshold`。

---

## 2. 現況盤點:哪些機制直接可重用

### 2.1 現行單軌流程(基準)

```text
規劃期  agent 每輪 create-plan / plan-ok;plan-ok 且無任何異動 → flag+1;
        有變動/異常 → flag 歸零;flag > 門檻 → 執行期(plan 凍結)
執行期  依 current_order 派 task-N;agent 實作+commit(該輪不算票),
        或什麼都不動 + done 回報;無異動+驗證綠+正常退出 → done+1;
        任何異動/紅燈/異常 → done 歸零;done ≥ 門檻 → 記完成 SHA,派下一個
防線    受保護檔案快照/還原(goal、plan-doc)、state 竄改偵測、round token 防舊輪污染、
        紅燈連跳/HEAD 停滯 → reset --hard 回綠點、stuck-stop、單 writer 鎖(workspace+git-dir)
```

### 2.2 可重用清單(可行性的主要依據)

| 現有機制 | 位置 | 在並行架構中的角色 |
|---|---|---|
| git-dir 級單 writer 鎖 | `engine/loop.py:1408-1416` | 每個 worktree 有自己的 git-dir(`.git/worktrees/<name>`),天然允許 N 個子迴圈並行、又擋住同 worktree 雙寫 |
| workspace 級單 writer 鎖 | `engine/loop.py:1677` | 每條軌道配一個 workspace,互不干擾 |
| 「任何變更 → done 歸零」 | `engine/loop.py:1594-1603` | merge main 進 worktree 後自動重新累積共識 = 「重複嘗試直到收斂」免費獲得 |
| 綠點錨定 + reset 防線 | `engine/loop.py:643-662, 1620-1656` | 每軌獨立回退;合併把事情弄壞時 reset 會自動撤銷未收斂的 merge 再重試 |
| `--import-plan --start-phase exec` | `engine/loop.py:1712-1728` | 父協調器把切好的軌道計畫餵給子迴圈,子迴圈不需要知道「拆分」這件事的全貌 |
| `--pause-after-plan` | `engine/loop.py:2092-2096` | 規劃收斂後停下 → 人工審核「拆分是否真的獨立」的現成掛點 |
| dispatch.json + round token | `engine/loop.py:1102-1108`、`engine/work.py:42-59` | 合併期沿用同一套派工/簽核協定,只是 phase 多一種 |
| 受保護檔案快照 | `engine/loop.py:1114-1148, 1959-1971` | 每個子 workspace 各自快照 goal/plan-doc;任何軌道都動不了人類真相 |
| Dashboard 多 workspace 監控 | `engine/dashboard.py:429, 596-622`、`ui/FleetOverview` | 子軌道以普通 workspace 卡片出現,MVP 零 UI 改動 |
| 封存/健檢/異常 log 等營運面 | 既有 | 子 workspace 全部直接繼承 |

### 2.3 Git worktree 的關鍵性質(設計前提)

- `git worktree add` 共享 object store 與 **refs**:子 worktree 內直接
  `git merge main`、`git merge-base --is-ancestor main HEAD` 都是本地操作,
  不需要任何跨 repo 傳輸——這讓合併協定變得非常簡單。
- 磁碟成本 = 一份 checkout + 各自的 build 產物(object 不重複)。
- Git 禁止同一分支同時被兩個 worktree checkout → main 只會在主 worktree 出現,
  是「main 單一寫入點」不變量的免費保險。

---

## 3. 總體設計

### 3.1 角色

```text
┌──────────────────────────── fleet 協調器(新,engine/fleet.py)────────────────────────────┐
│  主 worktree(main)                                                                      │
│  1. 規劃期:直接跑既有 loop.py --pause-after-plan → 收斂出「帶 track 標記的計畫」          │
│  2. 拆分:按 track 切片計畫、建 worktree + branch、建立子 workspace、--import-plan 派發    │
│  3. 監督:讀子 state.json(唯讀)、重啟異常子迴圈、维護 fleet.json                          │
│  4. 合併佇列:逐一處理 merge-ready 的軌道 → 在主 worktree `git merge --ff-only <tip>`     │
│  5. 匯流:全部軌道併回後,在主 worktree 跑既有 loop 執行 @final 整合任務 → 總 REPORT       │
└───────┬───────────────────────┬───────────────────────┬─────────────────────────────────┘
        │ spawn                 │ spawn                 │ spawn
┌───────▼────────┐     ┌────────▼───────┐      ┌────────▼───────┐
│ 子迴圈 track-A │     │ 子迴圈 track-B │      │ 子迴圈 track-C │   ← 全部是「今天的 loop.py」
│ worktree wt/A  │     │ worktree wt/B  │      │ worktree wt/C  │     + 新增合併期(§5.3)
│ branch loop/A  │     │ branch loop/B  │      │ branch loop/C  │
│ workspace ws--A│     │ workspace ws--B│      │ workspace ws--C│
└────────────────┘     └────────────────┘      └────────────────┘
```

### 3.2 生命週期

```text
準備 target repo(goal.md + PLAN.md 已 commit,validate 綠)
   │
   ▼
[父] 規劃期:沿用現行規劃迴圈,收斂出計畫 v2(任務帶 track)
   │        └─ --pause-after-plan:人工審核拆分(建議預設開)
   ▼
[父] 對每條 track:git worktree add + branch + 子 workspace + 切片計畫(order 重編 1..n)
   ▼
[子]×N 並行:執行期照舊逐 task 收斂(實作輪/驗收輪、done ≥ threshold、reset 防線)
   │        全部 task 收斂後 → 進入合併期(merge phase)
   ▼
[子] 合併期(重複直到收斂):
   │   main 不是我的祖先?→ 本輪任務=「git merge main、解衝突、修到 validate 綠、commit」
   │                        (HEAD 動了 → done 自動歸零,照現行規則)
   │   main 已是我的祖先?→ 本輪任務=「什麼都不動,驗證後 done 回報」→ done+1
   │   done ≥ merge 門檻 且 ancestor 條件成立 → 寫 merge-ready(tip SHA)→ 子迴圈正常停止
   ▼
[父] 合併閘門(逐軌序列化):
   │   在主 worktree 執行 `git merge --ff-only <tip>`
   │   ├─ 成功 → 標記 merged、(選配)在 main 重跑 validate、處理下一軌
   │   └─ 失敗(main 已被別軌推進)→ 重啟該子迴圈(resume 回合併期)→ 它會再 merge 一次 main
   ▼
[父] 全軌 merged → 主 worktree 跑 @final 整合任務(既有循序迴圈)→ 聚合 REPORT → 完成
```

「重複嘗試直到收斂」出現在兩層:軌道內逐 task 的既有收斂,以及合併期
「merge → 重新累積共識 → 閘門 → 失敗再 merge」的外圈,兩者用的是同一套 done 共識機制。

---

## 4. 任務格式 v2

### 4.1 格式

```json
[
  {"order": 1, "task": "……含 DoD……", "ref": "PLAN.md#auth", "track": "auth"},
  {"order": 2, "task": "……",                                  "track": "auth"},
  {"order": 3, "task": "……含 DoD……", "ref": "PLAN.md#report", "track": "report"},
  {"order": 4, "task": "跨模組整合驗收:……(在 main 上執行)",     "track": "@final"}
]
```

規則(在現行規則之上疊加,現行規則全數保留):

- `order`:**維持全域 1..N 連續遞增、唯一**。它仍是任務身分、稽核與 UI 的主鍵,不因分軌改變。
- `track`:選填字串,`[a-z0-9._-]{1,24}`。**缺省 = 全部同一軌**,行為與今天完全相同
  (向後相容的關鍵:舊計畫、舊 state 不需遷移)。
- 同一 track 內的任務按 `order` 循序執行(軌內語意 = 今天的整條計畫)。
- 保留字 `@final`:匯流軌。所有一般軌道 merge 回 main 之後,在主 worktree 循序執行。
  跨軌依賴 v1 **不支援**顯式 DAG,一律用「拆進同一軌」或「放進 @final」表達(見 §12)。
- 選填 `scope`(v1 可延後):字串陣列,聲明該任務預期觸碰的路徑 glob。**純提示性**——
  給規劃期 agent 自我檢查、給人工審核看、給程式做「重疊警告」(不阻擋,warning-only)。
- 上限:track 數 ≤ 8(含 @final);超過視為計畫校驗失敗。

### 4.2 校驗規則變更點

- `engine/work.py:validate_plan`(`engine/work.py:70-99`):允許 `track`/`scope` 欄位、
  track 命名規則、track 數上限;`@final` 以外不得以 `@` 開頭。
- `engine/loop.py:validate_state_shape`(`engine/loop.py:794-806`):plan 條目同步放寬。
- `ui/src/features/launcher/planValidation.ts`:與 work.py 契約一致(該檔案第 17 行的
  未知欄位白名單、第 26-27 行的連續 order 檢查維持)。
- `engine/prompts/external-agent-plan.md`:輸出契約由「欄位限於 order/task/ref」
  擴為「order/task/ref/track(/scope)」,並補拆分指引。
- 子迴圈吃到的是**切片後重編 1..n 的計畫**(`validate_plan` 要求從 1 連續,維持不動),
  全域 order ↔ 軌內 order 的對照表由 fleet.json 保存,總 REPORT 用它還原全域編號。

### 4.3 為什麼選 track 而不是 DAG

- DAG(每 task 帶 `deps:[...]`)表達力最強,但:弱模型很難穩定產出正確依賴圖、
  排程/回退語意複雜化(某 task reset 時下游要不要跟著退?)、UI(PlanTable/編輯器)
  複雜度暴增。本 repo 的 prompt 歷史(commit 4880782、797fb77)顯示「給弱模型的契約
  必須收得很緊」,DAG 與這個哲學相悖。
- track 模型 = 「幾條今天的計畫並排」,每一軌的心智模型、防線、測試全部沿用;
  表達不了的依賴自然退化成同軌循序或 @final,**錯誤的代價是變慢,不是變錯**。

---

## 5. 子迴圈(軌道)設計

### 5.1 規劃期 prompt 增修(`engine/prompts/plan.md`)

- 新增拆分指引:「先按模組/目錄邊界分析檔案接觸面,接觸面不相交的任務群才可分軌;
  不確定就放同一軌——分軌錯誤的代價是合併衝突,循序永遠是安全預設」。
- 要求每軌自足:每條 task 的 DoD 仍必須逐條寫全(現行規則),且**不得依賴其他軌道
  尚未完成的工作**;跨軌驗收一律寫進 `@final`。
- `pause_after_plan` 建議在並行模式下預設開啟:人工看一眼拆分是否合理再放行
  (沿用現成開關,零新機制)。

### 5.2 執行期:零語意變更

子迴圈就是今天的 `loop.py`,吃切片計畫、在自己的 worktree/branch/workspace 上跑:
逐 task 共識、驗證、紅燈/停滯 reset、受保護檔案快照、issue 回報、異常 log——全部照舊。
唯二差異:

1. 啟動參數多 `--merge-target main`(由 fleet 帶入):最後一個 task 收斂後不進 `done`,
   改進 `merge` phase。
2. 受保護檔案快照來源是該 worktree 內的 goal/plan-doc(內容與 main 相同,見不變量 I5)。

### 5.3 合併期(merge phase,新增)

`state.phase` 值域擴為 `plan | exec | merge | done`(`validate_state_shape` 同步放寬)。
每輪流程:

```text
輪初:coordinator 檢查 git merge-base --is-ancestor refs/heads/main HEAD
 ├─ 否(main 有我沒有的 commit)→ 派「併入輪」:dispatch phase=merge, task_id="merge-main"
 │    prompt(新 engine/prompts/merge.md):
 │      「git merge main;解衝突以達成 goal 為準、不得丟棄 main 既有行為;
 │        修到 <<VALIDATE_CMD>> 綠;commit(merge commit);工作區收乾淨。
 │        本輪有 merge commit 就不必 done——確認留給之後輪次。」
 │    輪末:HEAD 動了 → changed → done 歸零(現行機制,engine/loop.py:1594-1603)
 │    機械檢查(新):merge commit 的第二 parent 必須是派工當下記錄的 main tip,
 │                  否則視同竄改、該輪作廢(防 agent 亂 rebase/亂 reset)。
 └─ 是 → 派「確認輪」:等同執行期「已完成」分支——什麼都不動,實跑 DoD/validate,
      `work.py done merge-main` 回報;無異動+綠+正常退出 → done+1。
輪末:done ≥ merge_threshold 且 ancestor 條件仍成立
      → state.phase = "merge-ready"、記錄 tip SHA → 子迴圈正常停止(exit 0)
```

- `merge_threshold` 新參數,預設 2(比執行期的 3 低:merge 之後的樹已被軌內收斂驗過,
  這裡主要驗「合併本身」;可調)。
- 紅燈/停滯 reset 在合併期照常生效:reset --hard 回軌道綠點 = **自動撤銷失敗的 merge**,
  下一輪重新 merge——「重複嘗試直到收斂」的錯誤恢復不用另外寫。
- 衝突解不動(連續 reset)→ 既有 stuck-stop / issue 機制升級人工,不會無限燒錢。

### 5.4 work.py 變更

- `cmd_done`:接受 `phase == "merge"` 且 `task_id == "merge-main"`(其餘驗證照舊,
  `engine/work.py:138-150`)。
- 其他命令不變;合併期打 `create-plan` 比照執行期:忽略 + 票作廢(現行語意)。

---

## 6. 父協調器(fleet)設計

新檔 `engine/fleet.py`,職責刻意薄:**它不實作收斂,只做生命週期與合併閘門**。

### 6.1 Worktree / branch / workspace 生命週期

- 建立(冪等):`git worktree add <root>/wt/<track> -b loop/<run-id>/<track> <base-sha>`;
  已存在且指向同 branch → 沿用(resume);存在但不符 → fail-closed 停機交人。
- worktree 放 repo 外(如 `workspace/<name>/worktrees/<track>/`,或使用者指定 root),
  避免污染 target repo;路徑進 fleet.json。
- 子 workspace 命名 `"<parent>--<track>"`(沿用現行命名規則,`--` 作視覺分隔)。
- 回收:merged 後 `git worktree remove` + branch 保留(稽核);失敗軌道保留現場。
  子 workspace 用既有封存機制。
- 磁碟預算:N × (checkout + build 產物)。Java 專案粗估每軌 0.5–1 GiB,
  預設 `--max-parallel 4` 並在啟動表單顯示估算。

### 6.2 合併閘門(程式判斷,R5 核心)

放行條件(全部機械檢查,缺一不可):

```text
G1 子 state.phase == "merge-ready",且記錄的 tip SHA == 該 branch 目前 tip
G2 tip 在子 workspace 的 last_green_sha 上(= 該 SHA 驗證綠、工作樹乾淨時記錄)
G3 done 共識已達 merge_threshold(G1 蘊含,由子迴圈落盤)
G4 refs/heads/main 是 tip 的祖先(可 fast-forward)
G5 主 worktree 乾淨、無其他 fleet 正在合併(fleet 持有主 worktree 的既有 git-dir 鎖)
```

執行:在主 worktree `git merge --ff-only <tip>`。因為 G5 保證 fleet 是 main 的唯一
本地寫入者,`--ff-only` 本身就是安全的 CAS:main 若在 G4 檢查後被推進(理論競態),
merge 直接失敗,fleet 把該軌重新排隊。**任何非 fleet 造成的 main 移動 =
外力介入 → fleet 停機交人**(不變量 I1)。

合併後(選配,建議開):在 main 重跑一次 validate。ff 保證樹 identical,
這一步驗的是環境耦合(路徑寫死、cache 差異);紅了就是重大訊號,停機交人。

### 6.3 合併佇列與「合併風暴」控制

- 佇列嚴格序列化:一次只 ff 一軌。每次 main 前進後,其餘 merge-ready 軌道的 G4 必然失效
  → fleet 以 resume 重啟它們,自然回到合併期再 merge 一次。
- 成本上界:K 軌並行、最後合併的軌道最多重併 K-1 次,每次 ≈ 1 併入輪 + merge_threshold
  確認輪。K=4、threshold=2 時最多約 9 輪額外開銷——這是共識模型的固有代價,
  用「佇列按 readiness 先到先合」與「建議 track ≤ 4」控制。
- 選配 `--pause-before-merge`:每次 ff 前停下等人工核可(比照 pause_after_plan;
  高風險 repo 的保險絲)。

### 6.4 fleet 真相與 resume

`workspace/<parent>/fleet.json`(原子寫、比照 state.json 的 last-good checkpoint 防線):

```json
{
  "schema_version": 1,
  "run_id": "…", "phase": "exec | merging | final | done",
  "base_branch": "main", "base_sha": "…",
  "order_map": {"auth": {"1": 1, "2": 2}, "report": {"1": 3}},
  "tracks": [
    {"name": "auth", "branch": "loop/…/auth", "worktree": "…", "workspace": "ws--auth",
     "status": "running | merge-ready | merged | failed", "merged_sha": null,
     "merge_attempts": 0}
  ],
  "merge_queue": ["report"]
}
```

- 父崩潰恢復:fleet.json + 各子 state.json + git 現場(branch/worktree 是否存在、
  merge-base 關係)三方比對重建;所有步驟冪等(worktree add 檢查既存、ff merge 天然冪等)。
- 子迴圈本來就 resume-first(state.json 續跑),fleet 重啟子迴圈 = 重跑同一條命令。
- 監督:fleet 唯讀輪詢子 state(比照 Dashboard 的 read-only 投影),不寫子 workspace
  (不變量 I8);子迴圈異常退出 → 有限次數重啟 + 指數退避(沿用 agent backoff 的參數哲學)。

### 6.5 CLI / Dashboard 掛法

- 新入口 `python -m engine.fleet --repo … --parallel --max-parallel 4 …`,
  參數集 = loop.py 全集 + fleet 專屬(merge_threshold、max-parallel、pause-before-merge、
  worktree-root)。單軌計畫時 fleet 直接退化為 exec 委派給 loop.py(行為 = 今天)。
- Dashboard:啟動表單加「並行模式」區塊(track 預覽、磁碟估算、max-parallel);
  spawn fleet 而非 loop(`engine/dashboard.py:596-622` 加一個分支)。
  子軌道自動以普通 workspace 卡片出現在總覽(零改動);父卡片顯示 fleet phase 與
  merge queue(Phase 3 再做分組 UX)。

---

## 7. 不變量(fail-closed 清單)

| # | 不變量 | 保證機制 |
|---|--------|---------|
| I1 | 本地 refs/heads/main 只能由 fleet 以 ff 前進;偵測到任何外力移動 → 停機交人 | G4/G5 + base_sha 血緣檢查;git 禁止 main 被第二個 worktree checkout |
| I2 | 任一 worktree 同時最多一個 writer(子迴圈或 fleet,不同時) | 既有 git-dir 鎖 + 「子停止後父才動它的分支」的停止式交接(§5.3) |
| I3 | 進 main 的每個 SHA:在其 worktree 驗證綠、工作樹乾淨、done 共識達標、共識期間 HEAD 未動 | 既有執行期機制 + G1-G3 |
| I4 | 不可 ff 就不合;合併失敗軌道回合併期重來,次數封頂後停機 | G4 + merge_attempts 上限 |
| I5 | goal/plan-doc 在所有 worktree 受保護;fleet 運行期間 main 上的 goal 不變 | 既有快照防線(每子 workspace 各一份)+ I1 推論 |
| I6 | 軌道間唯一耦合點是 main;reset/綠點/任務指標全部軌內獨立 | worktree + workspace 隔離 |
| I7 | 計畫(含拆分)在規劃收斂時凍結;執行期/合併期不得改 | 既有「執行期計畫凍結」延伸 |
| I8 | 子 state 是子迴圈真相、fleet.json 是父真相;互相只讀不寫 | 檔案所有權約定 + 既有竄改偵測 |
| I9 | merge commit 的第二 parent 必須是派工當下的 main tip | §5.3 新機械檢查 |

殘餘風險(機械防線蓋不住、須誠實列出):agent 在解衝突時以「ours」策略**內容上**
覆蓋掉 main 的既有行為——歷史上 main 的 commit 仍在(I9 擋不掉內容回退)。
緩解:main 的測試隨 merge 進入軌道、由 validate 執行;@final 整合驗收;
pause-before-merge 人工抽查。這與現行單軌「agent 亂寫但驗證綠」的殘餘風險同級,
不是並行新增的風險類別。

---

## 8. 影響面盤點

| 檔案 | 變更 | 量級 |
|---|---|---|
| `engine/work.py` | validate_plan 加 track/scope;done 接受 merge phase | 小 |
| `engine/loop.py` | validate_state_shape 放寬;merge phase 狀態機(比照 process_exec_round 約 100–150 行);`--merge-target`/`--merge-threshold`;render_task_list 分軌標示 | 中 |
| `engine/fleet.py`(新) | 生命週期、監督、合併佇列、fleet.json、resume、總 REPORT | 大(估 600–900 行,本 repo 風格含大量防線) |
| `engine/prompts/merge.md`(新) | 合併輪任務卡 | 小(但需迭代打磨) |
| `engine/prompts/plan.md`、`external-agent-plan.md`、`prompt_templates.py` | 拆分指引 + 契約 v2 | 小–中 |
| `engine/dashboard.py` | 並行啟動分支、fleet 狀態投影 API | 中 |
| `engine/status.py` | track/fleet 欄位投影 | 小 |
| `ui/` planValidation.ts、PlanTable、PlanEditorModal、LauncherModal | 格式 v2 + 啟動選項;分組 UX 延後 | 中 |
| `tests/`(test_guards 擴充 + 新 test_fleet.py) | worktree fixture、閘門矩陣、resume/崩潰、外力動 main、竄改×合併期 | **大**(估與 fleet.py 本體等量或更多) |
| `README.md`、`templates/GUIDE.md` | 流程圖、並行章節、格式 v2 | 小–中 |

---

## 9. 難度評估與工期

單人、含測試與文件、按本 repo 現行品質標準(fail-closed、O_NOFOLLOW 檔案紀律、
原子寫、防竄改)估算:

| 元件 | 難度 | 主要風險點 | 估時 |
|---|---|---|---|
| 任務格式 v2(全契約面) | ★★☆☆☆ | 機械但漣漪廣(10+ 檔案);向後相容驗證 | 2–4 天 |
| Worktree 生命週期 | ★★★☆☆ | 冪等/殘留/鎖死/磁碟;submodule 等邊角(v1 明示不支援) | 3–4 天 |
| fleet 協調器(監督+resume) | ★★★★☆ | 父崩潰恢復、部分失敗、與子鎖的交接時序 | 5–8 天 |
| 合併期 merge phase | ★★★☆☆ | 狀態機插入點、I9 檢查、prompt 打磨(弱模型) | 3–4 天 |
| 合併閘門+佇列 | ★★★☆☆ | 程式碼小、正確性要求高;競態論證 | 2–3 天 |
| 測試工程 | ★★★★☆ | 多 worktree fixture、長場景、時序注入 | 5–7 天 |
| Dashboard/UI(MVP→分組) | ★★★☆☆ | MVP 近零;分組/佇列視圖另計 | 2–3 天(MVP)+3–5 天(UX) |
| **合計** | **中高** | | **22–33 天;MVP(Phase 0+1)8–12 天** |

判定:**難度的重心不在「合併協定」**(它小而清晰,且是業界成熟模式——本質上是
bors/merge-queue 的本地化:在分支上驗合併結果、只允許 ff 推進主幹),
而在 fleet 的可靠性工程與測試量。沒有需要發明的新理論。

---

## 10. 風險與對策

| # | 風險 | 對策 |
|---|---|---|
| R-1 | 弱模型解衝突品質差,合併期不收斂 | red-limit reset 自動撤銷壞 merge 重試;merge_attempts 封頂→停機;issue 升級;pause-before-merge |
| R-2 | 規劃期拆分不獨立 → 衝突風暴 | prompt 拆分指引 + scope 聲明 + 重疊 warning;pause_after_plan 人工審核;「不確定就同軌」的保守預設 |
| R-3 | 資源倍增(N agent + N validate 同時跑) | --max-parallel;validate 產物天然 per-worktree;共享 cache(如 ~/.m2)併發問題文件化,必要時 per-track repo.local |
| R-4 | 外力動 main / 人在跑動中操作 repo | I1 偵測停機;README 明示運行中不要手動碰 main |
| R-5 | 收斂輪次成本疊加(§6.3) | merge_threshold=2;track ≤ 4 建議;佇列先到先合 |
| R-6 | fleet 單點故障 | fleet.json checkpoint + 全步驟冪等 + resume 一級公民 + 崩潰注入測試 |
| R-7 | 內容級回退 main 行為(§7 殘餘風險) | validate 含 main 測試、@final 驗收、人工抽查 |
| R-8 | 測試量失控 | 分期交付;worktree fixture 抽象化;單軌回歸靠既有 4,107 行測試守住 |

---

## 11. 分期路線圖(每期獨立可用、可停損)

| 期 | 內容 | DoD | 估時 |
|---|---|---|---|
| **P0 格式 v2** | track/scope 欄位貫通所有契約面;**行為零變**(缺省單軌) | 全測試綠;舊 plan/state 原樣可跑;外部產生器輸出 v2 可匯入 | 2–4 天 |
| **P1 並行執行(人工合併)** | fleet 建 worktree、切片派發、並行監督、resume;軌道收斂即停在 branch 上,**合併由人做** | K 軌並行完跑;父/子任一崩潰可 resume;Dashboard 可監控各軌 | 6–8 天 |
| **P2 自動合併** | merge phase + merge.md + 閘門 + 佇列 + @final 匯流 + 總 REPORT | 全自動端到端:含「兩軌衝突→agent 解掉→ff 進 main」的整合測試;外力動 main 停機測試 | 6–9 天 |
| **P3 營運強化** | Dashboard 分組/佇列視圖、scope 重疊警告、pause-before-merge、磁碟/併發調參 | UX 驗收;文件與 GUIDE 更新 | 5–8 天 |

P1 結束就有實際價值(隔離並行 + 人工整合);P2 才兌現全自動;P3 是體驗與營運。

---

## 12. 替代方案與否決理由

| 方案 | 否決理由 |
|---|---|
| 單行程多工(一個 loop 同時開 N 個 agent) | run_agent/signal/state 全部假設單輪序列;侵入式改造風險遠高於父子行程模型,且失去「子=今天的 loop」的重用與回歸保障 |
| rebase 取代 merge | agent 對 rebase 衝突的處理更差;改寫 SHA 會打亂 completed 錨定與 changed 偵測;merge commit 保留稽核鏈且 I9 可機械驗證 |
| 任務級 DAG 依賴 | 見 §4.3;弱模型產出品質與 UI/回退複雜度不成比例 |
| 每完成一個 task 就合併回 main | 合併頻率×共識成本爆炸;軌道等於失去隔離意義。粒度定為「軌道完成才合併」 |
| 用多個 clone 而非 worktree | 失去共享 refs/objects,合併協定要走 push/fetch,複雜且慢;worktree 的「main 不可雙 checkout」保險也沒了 |

---

## 13. 未決問題(需人工拍板)

1. `pause_after_plan`(審拆分)與 `pause-before-merge`(審合併)在並行模式的**預設值**?
   建議:前者預設開、後者預設關。
2. `merge_threshold` 預設 2 是否可接受?(執行期維持 3)
3. track 上限與 `--max-parallel` 預設(建議 8 / 4)。
4. 合併後在 main 重跑 validate:預設開(多一次成本)或關?建議開。
5. 子 workspace 命名 `"<parent>--<track>"` 是否採納(影響 Dashboard 顯示與封存)。
6. worktree 存放位置:workspace 目錄下(隨封存走)vs 使用者指定 root(磁碟彈性)。
7. `scope` 欄位進 P0 還是延到 P3(warning 功能反正在 P3)?建議 P0 就進格式、P3 才用。

---

## 附錄 A:合併閘門時序(兩軌衝突場景)

```text
t0  main=M0;track-A、track-B 各自從 M0 開跑
t1  A 全 task 收斂 → merge phase:is-ancestor(M0, A)成立 → 確認輪×2 → merge-ready(tipA)
t2  fleet:ff merge tipA → main=A ✔
t3  B 全 task 收斂 → merge phase:is-ancestor(main=A, B)不成立
      → 併入輪:agent 在 wt/B `git merge main`,解衝突,validate 綠,commit(HEAD 動→done=0)
      → 確認輪×2(無異動+綠)→ merge-ready(tipB')
t4  fleet:ff merge tipB' → main=B' ✔ → 進 @final
若 t3 的 merge 把東西弄壞:validate 紅 → done 歸零重試;連紅達 red-limit →
reset --hard 回 B 的綠點(= 撤銷整個 merge)→ 下一輪重新 merge——直到收斂或 stuck-stop。
```

## 附錄 B:與現行文件的銜接

- README「流程」章節在 P2 落地時加並行分支圖;README.md:34 的「目前不會自動拆任務、
  合併分支」敘述屆時移除。
- `templates/GUIDE.md` 與 Prompt 產生器補「什麼樣的 goal 適合並行」指引。
