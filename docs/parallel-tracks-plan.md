# 規劃書:任務並行拆分 × Worktree 隔離 × 收斂式合併(fleet 並行架構)

> 狀態:**v2,方向已拍板**(決議見 §14),可據此分期實施。
> 對應需求:「拆解任務時讓 agent 拆分成可並行模式,幫他們開 worktree,模式參照現在的方式
> 重複嘗試直到收斂,然後才同意合回來(由 agent 自己 merge main 到 worktree 自己解衝突),
> 由程式判斷 done ≥ threshold + 可 ff merge。」
> v2 變更:落地四項決議——①未決問題全採本文建議值;②worktree 預設放 workspace 目錄;
> ③**不做舊版相容**(track 必填、舊格式直接拒絕);④新增完整的 Prompt 調整設計(§7)。
> v3 變更:合併期拆成顯式子狀態(merge_stage 等欄位落 state),新增 Track 狀態機
> 與 Dashboard 觀測設計(§6.6);修正 phase 值域漏列 merge-ready。
> v4 變更:全面審視與既有功能的衝突(§15)——Plan 編輯器的前綴鎖定假設、封存/重置/
> 階段切換的生命週期耦合、監控投影誤判、監督政策修訂(不自動重啟);新增不變量 I10。

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
新功能的測試量級相當)、(4)四份 prompt 的弱模型收緊迭代(§7)。

**路線:四期分階,每期獨立可用、可停損**(§12)。粗估總工期 23–35 個工作天(單人);
MVP(Phase 0+1,並行執行+人工合併)約 9–13 個工作天。放棄舊版相容省下約 1 天,
v4 的既有功能整合(§15:編輯器改造、生命週期防護欄)加回約 2–3 天。

---

## 1. 需求還原

| # | 需求 | 本文件對應 |
|---|------|-----------|
| R1 | 規劃期 agent 把任務拆成「可並行的軌道(track)」 | §4 任務格式 v2、§7.2 規劃期 prompt |
| R2 | 程式替每條軌道開 Git worktree,各自隔離執行 | §3、§6.1 worktree 生命週期 |
| R3 | 每條軌道沿用現行模式:單輪無狀態 agent、共識 AND gate、重複嘗試直到收斂 | §5.2 子迴圈重用 |
| R4 | 收斂後才允許合併;合併前由 agent 在自己的 worktree 把 main merge 進來、自己解衝突 | §5.3 合併期、§7.4/§7.5 合併 prompt |
| R5 | 合併回 main 由**程式**判斷:done 共識達門檻 **且** 可 fast-forward merge | §6.2 合併閘門(CAS) |
| R6 | 任務格式調整以承載拆分資訊(**不需相容舊版**) | §4 |
| R7 | Prompt 如何調整 | **§7(本版新增)** |

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
│  3. 監督:讀子 state.json(唯讀)、重啟異常子迴圈、維護 fleet.json                          │
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
   │        └─ --pause-after-plan:人工審核拆分(並行模式預設開,§14)
   ▼
[父] 對每條 track:git worktree add + branch + 子 workspace + 切片計畫(order 重編 1..n)
   ▼
[子]×N 並行:執行期照舊逐 task 收斂(實作輪/驗收輪、done ≥ threshold、reset 防線)
   │        全部 task 收斂後 → 進入合併期(merge phase)
   ▼
[子] 合併期(重複直到收斂):
   │   main 不是我的祖先?→ 派「併入輪」(§7.4):git merge --no-commit、解衝突、
   │                        驗證綠才 commit(HEAD 動了 → done 自動歸零,照現行規則)
   │   main 已是我的祖先?→ 派「整合確認輪」(§7.5):逐條重跑 DoD + validate,
   │                        無異動 → done 回報 → done+1
   │   done ≥ merge 門檻(2)且 ancestor 條件成立 → 寫 merge-ready(tip SHA)→ 子迴圈正常停止
   ▼
[父] 合併閘門(逐軌序列化):
   │   在主 worktree 執行 `git merge --ff-only <tip>`
   │   ├─ 成功 → 標記 merged、在 main 重跑 validate(預設開,§14)、處理下一軌
   │   └─ 失敗(main 已被別軌推進)→ 重啟該子迴圈(resume 回合併期)→ 它會再 merge 一次 main
   ▼
[父] 全軌 merged → 主 worktree 跑 @final 整合任務(既有循序迴圈)→ 聚合 REPORT → 完成
```

「重複嘗試直到收斂」出現在兩層:軌道內逐 task 的既有收斂,以及合併期
「merge → 重新累積共識 → 閘門 → 失敗再 merge」的外圈,兩者用的是同一套 done 共識機制。

---

## 4. 任務格式 v2(**track 必填,不相容舊版**)

### 4.1 格式

```json
[
  {"order": 1, "task": "……含 DoD……", "ref": "PLAN.md#auth", "track": "auth",
   "scope": ["src/auth/**", "tests/auth/**"]},
  {"order": 2, "task": "……含 DoD……",                        "track": "auth",
   "scope": ["src/auth/**", "tests/auth/**"]},
  {"order": 3, "task": "……含 DoD……", "ref": "PLAN.md#report", "track": "report",
   "scope": ["src/report/**"]},
  {"order": 4, "task": "跨模組整合驗收:……(在 main 上執行)",     "track": "@final"}
]
```

規則(現行 order/task/ref 規則全數保留,疊加以下):

- `order`:**維持全域 1..N 連續遞增、唯一**。仍是任務身分、稽核與 UI 的主鍵。
- `track`:**必填**字串,`[a-z0-9._-]{1,24}`;`@final` 是唯一允許以 `@` 開頭的保留名。
  決議「不需相容舊版」後 track 從選填改必填:**沒有「缺省=舊行為」的混合狀態**,
  規則只有一條——每條任務都寫 track,循序計畫就是全部填同一個名字(慣例用 `main`)。
  這正符合本 repo「收緊契約、消除弱模型可偷懶的模糊空間」的既有哲學(commit 4880782)。
- 同一 track 內的任務按 `order` 循序執行(軌內語意 = 今天的整條計畫);
  不同 track 並行。`@final` 在所有一般軌道 merge 回 main 之後,於主 worktree 循序執行。
- 跨軌依賴不支援顯式 DAG:一律用「拆進同一軌」或「放進 `@final`」表達(§13)。
- `scope`:字串陣列(路徑 glob)。**多於一軌時,非 `@final` 任務必填**;單軌計畫與
  `@final` 免填。P0 只進格式與校驗,重疊警告到 P3 才實作(決議 §14)。
- 上限:track 數(含 @final)≤ 8;非 @final 軌 ≤ 1 時 fleet 退化為現行循序模式(不開 worktree)。

### 4.2 不相容舊版的落地方式

- `engine/work.py:validate_plan`(`engine/work.py:70-99`):track 必填 + 命名規則 +
  上限 + scope 條件必填。**舊格式(無 track)直接校驗失敗**,錯誤訊息附新格式範例
  (沿用現有「錯在哪+合法格式+正確範例」的回報模式)。
- `engine/loop.py:validate_state_shape`(`engine/loop.py:794-806`):plan 條目要求 track。
  **舊 state 直接判定不合法** → 沿用既有 fail-closed 路徑停機,錯誤訊息明確指示:
  「格式已升級,請 `--reset-state` 或重新匯入計畫」。不寫任何自動遷移碼。
- `ui/planValidation.ts`、`PLAN_TEMPLATE`:同步收緊(欄位白名單 + track 必填 + scope 規則),
  規則與 work.py 完全一致(該檔案第 1 行的既有約定)。
- `engine/prompts/external-agent-plan.md`:輸出契約直接改為 v2(§7.6),不保留雙格式敘述。
- 子迴圈吃**切片後重編 1..n 的計畫**(validate_plan 的連續性要求維持不動);
  全域 order ↔ 軌內 order 對照表由 fleet.json 保存,總 REPORT 用它還原全域編號。

### 4.3 為什麼選 track 而不是 DAG

- DAG(每 task 帶 `deps:[...]`)表達力最強,但:弱模型很難穩定產出正確依賴圖、
  排程/回退語意複雜化(某 task reset 時下游要不要跟著退?)、UI(PlanTable/編輯器)
  複雜度暴增。本 repo 的 prompt 歷史(commit 4880782、797fb77)顯示「給弱模型的契約
  必須收得很緊」,DAG 與這個哲學相悖。
- track 模型 = 「幾條今天的計畫並排」,每一軌的心智模型、防線、測試全部沿用;
  表達不了的依賴自然退化成同軌循序或 @final,**錯誤的代價是變慢,不是變錯**。

---

## 5. 子迴圈(軌道)設計

### 5.1 規劃期

規劃仍在主 worktree 以現行規劃迴圈進行,收斂出帶 track 的計畫;
`pause_after_plan` 在並行模式預設開啟,人工審核拆分後放行。
規劃 prompt 的具體增修見 §7.2。

### 5.2 執行期:零語意變更

子迴圈就是今天的 `loop.py`,吃切片計畫、在自己的 worktree/branch/workspace 上跑:
逐 task 共識、驗證、紅燈/停滯 reset、受保護檔案快照、issue 回報、異常 log——全部照舊。
唯二差異:

1. 啟動參數多 `--merge-target main`(由 fleet 帶入):最後一個 task 收斂後不進 `done`,
   改進 `merge` phase。
2. 受保護檔案快照來源是該 worktree 內的 goal/plan-doc(內容與 main 相同,見不變量 I5)。

執行 prompt 的並行情境增修(接觸面紀律)見 §7.3。

### 5.3 合併期(merge phase,新增)

`state.phase` 值域擴為 `plan | exec | merge | merge-ready | done`(`validate_state_shape`
同步放寬;v2 草稿漏列 merge-ready,此處修正)。合併期內部再以**顯式欄位**拆出
「併入/確認」兩個子狀態——Dashboard 與 status CLI 是唯讀 state 投影、不執行 git 命令,
子狀態不落 state 就分辨不出來(完整狀態機與 UI 對照見 §6.6):

- `merge_stage`:`"sync"`(併入中)或 `"confirm"`(確認中);coordinator 每輪派工前
  判定 ancestor 後,與 dispatch.json 一起寫入 state(沿用「spawn 前先落地」的既有時序,
  `engine/loop.py:1891`);非合併期為 null。
- `merge_target_tip`:派工當下的 main tip SHA(與 I9 檢查、prompt 的 `<<MAIN_TIP>>` 同源)。
- `merge_ready_sha`:進入 merge-ready 時鎖定的本軌 tip(§6.2 G1 比對用)。

每輪流程:

```text
輪初:coordinator 檢查 git merge-base --is-ancestor refs/heads/main HEAD,
     並 rev-parse 當下 main tip 寫入 dispatch.json(供 prompt 注入與 I9 檢查同源)
 ├─ 否(main 有我沒有的 commit)→ 派「併入輪」:dispatch phase=merge, task_id="merge-main",
 │    模板 merge-sync.md(§7.4)。統一走 git merge --no-commit <main-tip>:
 │    無衝突與有衝突同一條路徑——解衝突(若有)→ 驗證綠 → 才 commit。
 │    輪末:HEAD 動了 → changed → done 歸零(現行機制,engine/loop.py:1594-1603)
 │    機械檢查(新,I9):merge commit 的第二 parent 必須是派工當下記錄的 main tip,
 │                  否則視同竄改、該輪作廢(防 agent 亂 rebase/亂 reset)。
 └─ 是 → 派「整合確認輪」:模板 merge-confirm.md(§7.5)——逐條重跑本軌 DoD + validate,
      什麼都不動,`work.py done merge-main` 回報;無異動+綠+正常退出 → done+1。
輪末:done ≥ merge_threshold(預設 2)且 ancestor 條件仍成立
      → state.phase = "merge-ready"、記錄 merge_ready_sha → 子迴圈正常停止(exit 0)
```

history.log 的合併期行加 `stage=sync|confirm` 欄位(既有 key=value 格式,
`round_metrics_from_history` 只取已知欄位、天然向前相容;samples 投影加 stage 供
逐輪判定顯示)。「未回 DONE」異常判定沿用現行規則:併入輪如同執行期實作輪,
以 commit 為產出、不回 done;確認輪以 done 為完成回報。

- `merge_threshold` 預設 2(執行期維持 3):merge 後的樹已被軌內收斂驗過,
  這裡主要驗「合併本身」(決議 §14)。
- 紅燈/停滯 reset 在合併期照常生效:reset --hard 回軌道綠點 = **自動撤銷失敗的 merge**,
  下一輪重新 merge——「重複嘗試直到收斂」的錯誤恢復不用另外寫。
- 衝突解不動(連續 reset)→ 既有 stuck-stop / issue 機制升級人工,不會無限燒錢。
- 收拾現場規則對 merge 半成品的處理(一律 `git merge --abort` 重來)寫死在模板(§7.4),
  determinism 優先於接手省時間:半解的衝突現場對全新 context 的 agent 是負資產。

### 5.4 work.py 變更

- `cmd_done`:接受 `phase == "merge"` 且 `task_id == "merge-main"`(其餘驗證照舊,
  `engine/work.py:138-150`)。
- 其他命令不變;合併期打 `create-plan` 比照執行期:忽略 + 票作廢(現行語意)。

---

## 6. 父協調器(fleet)設計

新檔 `engine/fleet.py`,職責刻意薄:**它不實作收斂,只做生命週期與合併閘門**。

### 6.1 Worktree / branch / workspace 生命週期

- **位置(已定案,§14)**:`workspace/<parent>/worktrees/<track>/`。
  好處:隨 workspace 封存/還原/刪除一起走、天然在 target repo 之外、
  沿用既有 workspace 目錄的 O_NOFOLLOW/symlink 檢查紀律。
  注意事項:workspace 封存(整目錄搬移)前必須先 `git worktree remove`——
  搬走一個活的 worktree 會讓 repo 的 worktree 註冊表指向失效路徑;
  封存流程加一步「worktree 已全數移除才可封存」的 fail-closed 檢查。
- 建立(冪等):`git worktree add workspace/<parent>/worktrees/<track> -b loop/<run-id>/<track> <base-sha>`;
  已存在且指向同 branch → 沿用(resume);存在但不符 → fail-closed 停機交人。
- 子 workspace 命名 `"<parent>--<track>"`(決議 §14)。
- 回收:merged 後 `git worktree remove` + branch 保留(稽核);失敗軌道保留現場。
- 磁碟預算:N × (checkout + build 產物)。Java 專案粗估每軌 0.5–1 GiB,
  `--max-parallel` 預設 4(決議 §14),啟動表單顯示估算。

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

合併後在 main 重跑一次 validate(**預設開**,決議 §14)。ff 保證樹 identical,
這一步驗的是環境耦合(路徑寫死、cache 差異);紅了就是重大訊號,停機交人。

### 6.3 合併佇列與「合併風暴」控制

- 佇列嚴格序列化:一次只 ff 一軌。每次 main 前進後,其餘 merge-ready 軌道的 G4 必然失效
  → fleet 以 resume 重啟它們,自然回到合併期再 merge 一次。
- 成本上界:K 軌並行、最後合併的軌道最多重併 K-1 次,每次 ≈ 1 併入輪 + merge_threshold
  確認輪。K=4、threshold=2 時最多約 9 輪額外開銷——這是共識模型的固有代價,
  用「佇列按 readiness 先到先合」與「建議 track ≤ 4」控制。
- `--pause-before-merge`(每次 ff 前等人工核可,比照 pause_after_plan)**預設關**(決議 §14)。

### 6.4 fleet 真相與 resume

`workspace/<parent>/fleet.json`(原子寫、比照 state.json 的 last-good checkpoint 防線):

```json
{
  "schema_version": 1,
  "run_id": "…", "phase": "exec | merging | final | done",
  "base_branch": "main", "base_sha": "…",
  "order_map": {"auth": {"1": 1, "2": 2}, "report": {"1": 3}},
  "tracks": [
    {"name": "auth", "branch": "loop/…/auth",
     "worktree": "workspace/<parent>/worktrees/auth", "workspace": "<parent>--auth",
     "status": "running | merge-ready | merging | merged | failed", "merged_sha": null,
     "merge_attempts": 0}
  ],
  "merge_queue": ["report"]
}
```

- 父崩潰恢復:fleet.json + 各子 state.json + git 現場(branch/worktree 是否存在、
  merge-base 關係)三方比對重建;所有步驟冪等(worktree add 檢查既存、ff merge 天然冪等)。
- 子迴圈本來就 resume-first(state.json 續跑),fleet 重啟子迴圈 = 重跑同一條命令。
- 監督:fleet 唯讀輪詢子 state(比照 Dashboard 的 read-only 投影),不寫子 workspace
  (不變量 I8);監督迴圈同時比對 goal hash,變更即平順停全艦(§15.3)。
  **fleet 預設不自動重啟異常退出或被人工停止的子軌**——標記需關注、交人決定;
  只在它擁有的既定轉移重啟(初次派發、ff 失敗重排);政策理由見 §15.4。

### 6.5 CLI / Dashboard 掛法

- 新入口 `python -m engine.fleet --repo … --parallel --max-parallel 4 …`,
  參數集 = loop.py 全集 + fleet 專屬(merge_threshold、max-parallel、pause-before-merge)。
  非 @final 軌 ≤ 1 時 fleet 直接委派 loop.py 循序執行(不開 worktree)。
- Dashboard:啟動表單加「並行模式」區塊(track 預覽、磁碟估算、max-parallel);
  spawn fleet 而非 loop(`engine/dashboard.py:596-622` 加一個分支)。
  子軌道自動以普通 workspace 卡片出現在總覽(零改動);父卡片顯示 fleet phase 與
  merge queue(Phase 3 再做分組 UX)。

### 6.6 Track 狀態機與 Dashboard 觀測(v3 補充)

「併入」不是單一狀態:從軌道收斂到進 main,一條軌道會經過多個必須可分辨的狀態。
真相分兩層——**子 state.json**(軌道自己的進度)與 **fleet.json**(佇列與合併結果)。
Dashboard 顯示時以 fleet 層覆蓋:當 `fleet.track.status ∈ {merging, merged, failed}`
時優先於子 state 的 merge-ready——子迴圈此時已停止,它的 state 不會再前進,
之後的真相只在 fleet 這層。

| 狀態 | 判定(真相來源) | 卡片 pill 文案(建議) |
|---|---|---|
| 執行中 | child:phase=exec(現行) | 執行中 task-N(現行不變) |
| 併入中 | child:phase=merge ∧ merge_stage=sync | 🔀 併入 main(解衝突)@`merge_target_tip` 短碼 |
| 重併中 | 同上 ∧ fleet:merge_attempts ≥ 1 | 🔁 第 n 次重併(main 已前進) |
| 整合確認中 | child:phase=merge ∧ merge_stage=confirm | 🗳 整合確認 done x/2 |
| 待合併 | child:phase=merge-ready(fleet 給佇列順位) | ⏳ 待合併(佇列第 k) |
| 合併中 | fleet:track.status=merging(ff + main validate 執行中) | ⤴ 合併進 main 中 |
| 已合併 | fleet:track.status=merged | ✔ 已合併 @`merged_sha` 短碼 |
| 合併失敗停機 | fleet:track.status=failed(attempts 封頂/外力動 main) | ⛔ 合併失敗,需人工 |
| 軌道停機/需關注 | child:既有停止、stuck、state 錯誤等判定 | 現行文案不變 |

落地面:

- `engine/status.py`:投影新增 phase(含 merge-ready)、merge_stage、merge_target_tip、
  merge_ready_sha 與 done/merge_threshold;維持「純唯讀、不碰 git」的原則——
  這正是子狀態必須顯式落在 state.json 的原因。
- `validate_state_shape`:merge_stage ∈ {null, "sync", "confirm"};兩個 SHA 欄位
  比照既有 40/64 hex 或 null 規則;phase enum 擴充。
- 新 API `GET /api/fleet-state?ws=<parent>`(唯讀,O_NOFOLLOW 讀 fleet.json,
  比照既有 bounded/safe reader):回傳 fleet phase、各軌 status/attempts/merged_sha
  與 merge_queue;SSE 加 `fleet-state` event(與 health/fleet-round-metrics 同一連線、
  同一原則:只推投影,不推原始檔)。
- 子軌道卡片:pill 依上表;「fleet 覆蓋 child」的優先序在前端 view model
  (`fleetViewModel.ts`)實作,資料來源 = 子 workspace state(既有掃描)⊕ fleet-state event。
- 父 workspace 卡片:顯示 fleet phase、佇列(誰在合併、誰在等、各軌 attempts)、
  已合併軌道與 merged_sha 清單;P1 期間(尚無自動合併)改列「track X 已完成於
  branch Y,待人工合併」。
- 輪次紀錄/時間軸:逐輪判定顯示 stage 標籤(併入/確認);sparkline 維持
  「綠紅=驗證結果」語彙,merge 輪的視覺標記(如描邊)留待 P3 定稿——
  pill 與逐輪文字已足以分辨,sparkline 只是輔助。
- 分期:P1 交付「已完成待人工合併」狀態與父卡片 branch 清單;P2 交付 merge 子狀態
  欄位、status.py 投影、fleet-state API/SSE 與覆蓋邏輯(自動合併與其觀測同期落地);
  P3 交付分組視覺與 sparkline 標記。

---

## 7. Prompt 調整設計(本版重點)

### 7.1 設計原則

沿用既有任務卡的全部結構慣例,四份 prompt 一體適用:

1. **單輪無狀態身分**:開頭固定交代「你是收斂迴圈中的一輪,前人可能做錯/做一半,
   你的產出會被之後輪次獨立檢驗」——這是共識機制成立的心理前提。
2. **互斥動作、二選一**:每張卡收斂到「改東西(本輪不算票)」或「什麼都不動 + 回報」
   兩條互斥路徑;同輪兩者都做 = 票作廢(機制已保證,prompt 明說)。
3. **非互動與停止規則**逐卡照抄現行條款式樣:不得提問、命令成功即停、
   權限被拒輸出 `tool permission blocked`、修不綠就輸出進度後停。
4. **弱模型防偷懶**:動詞全部具體(「實際執行」「取得通過結果」),
   禁止憑印象/前人紀錄/靜態對照;預期會像 4880782、797fb77 那樣做 2–3 輪收緊迭代。
5. **placeholder 純文字替換**:維持 `build_prompt` 的 `<<KEY>>` 機制
   (`engine/loop.py:1287-1292`),模板不含邏輯,分支由 coordinator 選模板/選值。
6. **併入與確認分成兩張卡**:coordinator 在派工當下就知道 main 是否已是祖先,
   與其在一張卡裡寫條件分支讓 agent 自判,不如直接派正確的卡——
   消除誤判空間,也讓兩張卡各自保持「單一工作」的清晰度。

### 7.2 `engine/prompts/plan.md` 增修

**a. 「計畫 JSON 格式」段全面改寫**(格式 v2,不留舊格式敘述):

範例更新為 §4.1 的形狀;欄位說明在現有 order/task/ref 三條之後新增:

> - `track`:必填,`[a-z0-9._-]{1,24}`;同一 track 的任務依 order 循序執行,
>   不同 track 並行。整個計畫無法並行就全部填同一個名字(慣例 `main`)——
>   這是完全合法的計畫,不要為了並行而硬拆。
>   `@final` 是唯一允許以 `@` 開頭的保留名:放跨軌整合與最終驗收,
>   會在所有軌道合併回主幹後、於主幹上循序執行。
> - `scope`:計畫多於一軌時,每條非 `@final` 任務必填——列出該任務預期會修改的
>   路徑 glob(如 `["src/auth/**", "tests/auth/**"]`);單軌計畫與 `@final` 省略。

**b. 完整性定義修一句**(現行第 1 點「它只看得到該條 task 的全文與每行截 80 字的
任務總覽」):

> 每一條 task 都會交給一個**全新 context 的 agent** 在隔離的 worktree 獨立執行,
> 它只看得到該條 task 的全文與**同一軌道**每行截 80 字的任務總覽——
> 看不到其他軌道的任何內容;每條 task 都自足到能直接動工、逐條做完目標就達成,
> 計畫才算完整。

**c. 新增「拆軌規則」一節**(插在「計畫 JSON 格式」之後,全文):

> ## 拆軌規則(並行執行)
>
> 計畫會按 `track` 拆成並行軌道:每條軌道在各自隔離的 Git worktree 上由獨立迴圈
> 循序執行;軌道全部任務收斂後,先把主幹最新變更併入自己的分支、解衝突並重新收斂,
> 才會被合回主幹;`@final` 軌最後在主幹上循序執行。據此拆分:
>
> - 會修改同一批檔案或同一模組的任務 → **必須同軌**。分軌的唯一目的是加速;
>   拆錯的代價是合併衝突與重工。**不確定能不能分,就放同一軌**——循序永遠是安全預設。
> - 軌內任務只能依賴同軌前置任務與主幹起跑點;**不得依賴其他軌道尚未合回的產出**。
>   「前置:task-N 完成」只能指同一軌的任務。
> - 跨軌整合、跨模組驗收、需要全部軌道成果才能做的工作 → 一律放 `@final`。
> - 多於一軌時逐任務填 `scope`,並自行核對:不同軌道的 scope 不得重疊;
>   重疊就表示拆分錯誤,把相關任務併回同一軌。
> - 軌道數(含 `@final`)不得超過 8;超過 4 通常已無加速收益,優先合併相近軌道。

**d. 「計畫已完整」宣告的語意擴充**(在二選一的 plan-ok 分支加一句):

> 宣告完整同時代表你已核對拆軌規則:接觸面不相交、無跨軌依賴、scope 已填。
> 拆軌不符規則的計畫視為有缺,應重交新計畫而非 plan-ok。

**e. placeholder**:無新增(拆軌規則是靜態文字;上限 8/4 與 validate_plan 寫死同值)。

### 7.3 `engine/prompts/exec.md` 增修

改動刻意最小——執行輪的心智模型不變,只加「接觸面紀律」:

**a. 新增 placeholder `<<TRACK_CONTEXT>>`**(插在「你的工作」段之後、參考段落之前),
由 coordinator 決定值:

- 多軌 fleet 模式:
  > 本任務屬於並行軌道 `<track>`;其他軌道正在別的 worktree 同時進行,完成後會互相合併。
  > 只改動完成本任務所必需的檔案(參考任務的 scope 聲明);不要順手重構、搬移、
  > 重新排版或「順便修」與本任務無關的程式碼——無謂的接觸面會直接變成合併衝突。
- 單軌/現行模式:注入空字串(模板行留白,不影響現行輸出)。

**b. commit message 規則改為 `<<TASK_TAG>>`**(現行第 34 行「commit message 帶上
<<TASK_ID>>」):值由 coordinator 決定——多軌 = `<track>/task-N`(main 歷史裡兩條軌
都有 task-1,不帶軌名無法稽核),單軌 = `task-N`(輸出與今天一字不差)。
`work.py done` 的核對仍用 `<<TASK_ID>>`(軌內 id),兩者用途不同、並存。

**c. 「任務總覽」標題**改為「本軌道任務總覽」(單軌時語意仍正確:整個計畫就是一軌)。

### 7.4 `engine/prompts/merge-sync.md`(新檔,併入輪,完整草稿)

````markdown
# 併入輪任務卡(單輪、無狀態 agent)

你是一個收斂迴圈中的一輪。本分支(並行軌道 <<TRACK_NAME>>)的全部任務已逐一收斂;
現在主幹 <<MERGE_TARGET>> 有本分支沒有的新變更,必須先併進來、解掉衝突並讓驗證通過,
本分支才有資格被合回主幹。前面可能已有 agent 併到一半 crash 或解錯衝突;
你的產出也會被之後的輪次獨立驗收。

## 目標(大方向,唯讀)

<<GOAL>>

## 本軌道已收斂的任務(唯讀,回顧用)

```
<<TASK_LIST>>
```

## 你的工作:merge-main(把 <<MERGE_TARGET>> 併入本分支)

要併入的確切 commit:<<MAIN_TIP>>

## 步驟(依序)

1. **收拾現場**:
   - `git status` 顯示 merge 進行中(有 unmerged paths,或 `.git/MERGE_HEAD` 存在)→
     那是前人併到一半的現場,一律 `git merge --abort` 回到乾淨狀態,從步驟 2 重新開始;
     不要嘗試接手半解的衝突。
   - 其他未 commit 殘留:對應得上「併入後的修復」就接手;對應不上就
     `git reset --hard` + `git clean -fd` 清掉(staged 的一併清),
     清完用 `git status` 確認乾淨。
2. **併入**:執行 `git merge --no-commit <<MAIN_TIP>>`(必須是這個確切 commit)。
   不得 rebase、不得 cherry-pick、不得 reset 到主幹、不得改寫或 amend 任何既有 commit。
3. **解衝突**(若有):
   - 主幹既有的行為與測試必須保留;本分支為達成 goal 所做的變更也必須保留其效果。
   - 兩邊都改過的地方,以「合併後同時滿足雙方意圖」為準重寫;不確定時以測試可通過為準。
   - 不得用單邊策略(ours/theirs)整檔覆蓋另一邊的實質變更。
   - 解完把所有衝突檔 `git add`。
4. **驗證**:實際跑 <<VALIDATE_CMD>> 直到綠;需要的修復一併改好(仍屬本輪併入工作)。
5. **提交**:驗證綠之後才 `git commit`(沿用預設 merge 訊息即可);
   工作區收乾淨,不留未 commit 的檔案。
   **本輪已產生 merge commit,不必也不得執行 done——整合是否正確由之後的輪次獨立確認。**

## 非互動與停止規則

- 這是非互動式自動執行環境。不得詢問使用者、等待確認、列出選項或只建議「下一步」。
- 完成步驟 5 後工作即結束:**立即停止**,不要繼續分析、修改檔案或執行其他命令。
- 多次嘗試仍無法把驗證修綠:若 merge 尚未 commit,先 `git merge --abort` 還原現場;
  輸出目前進度、已嘗試方向與驗證失敗尾段後直接停止,交由下一輪重試。
  不得 commit 驗證仍紅的 merge 結果,也不得執行 done。
- 衝突涉及真正的人類決策(兩邊意圖互斥、無法同時滿足)→ 執行
  `<<ISSUE_CMD>> "一句話描述矛盾"` 回報,`git merge --abort` 還原現場後停止。
- 若執行工具或權限遭拒,明確輸出 `tool permission blocked` 與原始錯誤後立即停止;
  不得假裝命令已執行。

## 禁區

- goal、參考文件、workspace 的 state.json / state.last-good.json / 計畫 JSON 是受保護真相:
  直接改檔會被偵測、還原、該輪作廢。
- 不得切換分支、不得 checkout 或推進 <<MERGE_TARGET>> 本身、不得 push、
  不得操作其他 worktree 或其目錄。
- 計畫已凍結:不能新增/修改/跳過任務。

## 本輪情報(迴圈注入)

<<NOTES>>
````

### 7.5 `engine/prompts/merge-confirm.md`(新檔,整合確認輪,完整草稿)

````markdown
# 整合確認輪任務卡(單輪、無狀態 agent)

你是一個收斂迴圈中的一輪。本分支(並行軌道 <<TRACK_NAME>>)已完成全部任務,
且主幹 <<MERGE_TARGET>>(commit <<MAIN_TIP>>)已併入本分支。你要獨立驗收
「整合後的分支」是否正確:連續多輪 agent 一致同意且無任何異動,本分支才會被合回主幹。
前面的輪次可能解錯衝突、或以為修好了;不要輕信任何前人紀錄。

## 目標(大方向,唯讀)

<<GOAL>>

## 本軌道任務全文(唯讀;驗收以每條任務的 DoD 為準)

<<TRACK_TASKS_FULL>>

## 你的工作:merge-main(驗收整合結果)

## 步驟(依序)

1. **收拾現場**:
   - merge 進行中(unmerged paths / `.git/MERGE_HEAD`)→ 前人半成品,
     `git merge --abort` 清掉後繼續驗收目前的 HEAD。
   - 其他未 commit 殘留:對應得上「整合修復」就接手做完;對應不上就
     `git reset --hard` + `git clean -fd` 清掉,清完 `git status` 確認乾淨。
2. **逐條驗收**:對上面列出的每一條任務,實際執行其 DoD 指定的命令或檢查,
   並取得該 DoD 明定的通過結果。只確認「測試檔存在」「程式碼有對到」不算執行,
   不得只憑印象或前人紀錄認定。
3. **整體驗證**:之後實際跑 <<VALIDATE_CMD>> 為綠。
4. 二選一(互斥):
   - **發現缺陷**(任一 DoD 失敗、驗證紅,或整合遺失了主幹或本軌道的既有行為)→
     修復;跑 <<VALIDATE_CMD>> 直到綠;只 commit 屬於整合修復的變更,
     commit message 帶上 merge-fix;工作區收乾淨。
     **本輪有任何 commit 就不必執行 done**——確認留給之後的輪次獨立判斷。
   - **全部通過** → 什麼檔案都不要動、不要 commit,執行:
     <<DONE_CMD>>
     - 任一結果為失敗、未知或未執行,不得執行 done。
     - 驗證產物必須是 gitignored;若驗證命令留下殘留,先還原現場再 done:
       未追蹤的殘留直接刪掉,被改動的受版控檔案用 `git restore <路徑>` 還原。
       跑完用 `git status` 確認無新異動;工作區有任何變更,該輪 done 票會作廢。

## 非互動與停止規則

- 這是非互動式自動執行環境。不得詢問使用者、等待確認、列出選項或只建議「下一步」。
- 若本輪完成修復並已通過驗證、commit 且工作區乾淨,工作到此完成:**立即停止**;
  不要再執行 done,也不要繼續分析、修改檔案或執行其他命令。
- 若本輪判定整合正確,必須實際執行 done;命令成功後立即停止。
- 若多次嘗試仍無法把驗證修綠:不要 commit 紅燈變更、不要硬跑 done;
  輸出目前進度、已嘗試方向與驗證失敗尾段後直接停止,殘留交由下一輪依「收拾現場」判斷。
- 只有整合本身存在人類才能裁決的矛盾時,才執行 `<<ISSUE_CMD>> "一句話描述問題"`
  回報後停止。
- 若執行工具或權限遭拒,明確輸出 `tool permission blocked` 與原始錯誤後立即停止;
  不得假裝命令已執行。

## 禁區

- goal、參考文件、workspace 的 state.json / state.last-good.json / 計畫 JSON 是受保護真相:
  直接改檔會被偵測、還原、該輪作廢。
- 不得切換分支、不得 checkout 或推進 <<MERGE_TARGET>> 本身、不得 push、
  不得操作其他 worktree 或其目錄。
- 計畫已凍結:不能新增/修改/跳過任務。

## 本輪情報(迴圈注入)

<<NOTES>>
````

設計備註:確認輪注入的是 `<<TRACK_TASKS_FULL>>`(軌道任務**全文**逐條列出,含 DoD),
不是截斷 80 字的 `<<TASK_LIST>>`——DoD 被截斷就無法驗收。切片後單軌任務數少,
prompt 大小可控;fleet 在拆分時對「單軌任務全文總長」設上限告警(不截斷,超長改建議拆軌)。

### 7.6 `engine/prompts/external-agent-plan.md` 契約 v2 增修

不保留雙格式,直接改寫(對照現行行號):

- **第 9 行**改為:「每個元素只能有 `order`、`task`、`track`,選填的 `ref` 與 `scope`,
  不得出現其他欄位。」
- **新增 track 條目**(第 10 行 order 條目之後):
  > `track` 必須是非空字串,格式 `[a-z0-9._-]{1,24}`;`@final` 是唯一允許以 `@` 開頭的
  > 保留名。同 track 任務依 order 循序執行,異 track 並行(各自在隔離 worktree 執行,
  > 完成後合回主幹);`@final` 在全部軌道合回後於主幹循序執行。會修改同一批檔案或
  > 同一模組的任務必須同 track;不確定就同 track。無法並行的計畫全部填同一名(慣例 `main`)。
  > track 總數(含 `@final`)不得超過 8。
- **新增 scope 條目**:
  > 計畫多於一軌時,每條非 `@final` 任務必須有 `scope`:字串陣列,列出預期修改的路徑
  > glob。輸出前自檢不同 track 的 scope 互不重疊;重疊代表拆分錯誤,必須併軌後再輸出。
  > 單軌計畫與 `@final` 任務省略 scope。
- **拆分規則區**(現行第 15–25 行)追加兩條:
  > - 跨 track 依賴不得存在:「前置:order N 完成」只能引用同 track 的任務;
  >   需要其他 track 產出的工作,放進該 track 或 `@final`。
  > - 跨模組整合、端到端驗收與需要全部成果的工作一律放 `@final`。
- **第 27 行合法形狀示意**:每個元素補上 `"track"`(示意用兩軌 + scope),
  維持「內容必須改成實際分析結果」的既有告誡。

### 7.7 Placeholder 對照與工程注意

| 模板 | 既有 placeholder | 新增 |
|---|---|---|
| plan.md | GOAL, PLAN_DOC, PLAN_JSON, CREATE_CMD, PLANOK_CMD, ISSUE_CMD, NOTES | (無) |
| exec.md | GOAL, PLAN_DOC, TASK_ID, TASK_TEXT, TASK_REF, TASK_LIST, DONE_CMD, ISSUE_CMD, VALIDATE_CMD, NOTES | TRACK_CONTEXT, TASK_TAG |
| merge-sync.md(新) | — | GOAL, TRACK_NAME, TASK_LIST, MERGE_TARGET, MAIN_TIP, VALIDATE_CMD, ISSUE_CMD, NOTES |
| merge-confirm.md(新) | — | GOAL, TRACK_NAME, TRACK_TASKS_FULL, MERGE_TARGET, MAIN_TIP, DONE_CMD, VALIDATE_CMD, ISSUE_CMD, NOTES |

- `MAIN_TIP` 由 coordinator 在派工當下 `rev-parse` 並同步寫入 dispatch.json——
  prompt 注入值與 I9 機械檢查(merge commit 第二 parent)同源,agent 看到的和
  程式驗的是同一個 SHA。
- `TRACK_CONTEXT`/`TASK_TAG` 由 coordinator 決定值:單軌模式分別注入空字串與
  `task-N`,多軌注入並行提示與 `<track>/task-N`。模板無條件邏輯。
- 外部產生器資源有 placeholder 漂移偵測(README:「placeholder 漂移時停用 Prompt
  產生器」),`prompt_templates.py` 的預期清單需與 §7.6 同步更新;
  `tests/test_prompt_templates.py` 對應調整。
- engine 內部四份模板納入測試:檔案存在、`<<KEY>>` 全數被替換、無殘留 placeholder
  (比照現有測試風格,新增於 test_guards 或獨立檔)。
- 弱模型收緊迭代:merge 兩卡是全新文本,預期需 2–3 輪像 4880782/797fb77 的
  「同視角全面掃描」修訂;估時已含在 §10。

---

## 8. 不變量(fail-closed 清單)

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
| I9 | merge commit 的第二 parent 必須是派工當下的 main tip | §5.3 新機械檢查(與 prompt 注入的 MAIN_TIP 同源) |
| I10 | fleet-managed 子 workspace 不接受 Dashboard 的 reset/匯入/回規劃期;計畫調整只能走「子軌停止時的既有編輯器路徑」或父層 fleet 動作 | 子 config 帶 `fleet_parent` 旗標 + Dashboard 路由檢查(§15.2) |

殘餘風險(機械防線蓋不住、須誠實列出):agent 在解衝突時以「ours」策略**內容上**
覆蓋掉 main 的既有行為——歷史上 main 的 commit 仍在(I9 擋不掉內容回退)。
緩解:merge-sync 卡明文禁止單邊覆蓋(§7.4);main 的測試隨 merge 進入軌道、由 validate
執行;merge-confirm 卡要求驗收「整合未遺失主幹既有行為」;@final 整合驗收;
必要時開 pause-before-merge 人工抽查。這與現行單軌「agent 亂寫但驗證綠」的殘餘風險
同級,不是並行新增的風險類別。

---

## 9. 影響面盤點

| 檔案 | 變更 | 量級 |
|---|---|---|
| `engine/work.py` | validate_plan:track 必填/命名/上限、scope 條件必填;done 接受 merge phase | 小 |
| `engine/loop.py` | validate_state_shape 要求 track(舊 state 拒絕)+ merge 狀態欄位 + current_task_entered_round;merge phase 狀態機(約 100–150 行)含 merge_stage/merge_target_tip/merge_ready_sha 落盤與 history stage 欄;`--merge-target`/`--merge-threshold`/`--expect-plan-version`;TASK_TAG/TRACK_CONTEXT 注入;render_task_list 分軌標示 | 中 |
| `engine/fleet.py`(新) | 生命週期、監督、合併佇列、fleet.json、resume、總 REPORT、封存前 worktree 檢查 | 大(估 600–900 行,本 repo 風格含大量防線) |
| `engine/prompts/merge-sync.md`、`merge-confirm.md`(新) | §7.4/§7.5 草稿落地 + 弱模型收緊迭代 | 小(文本)+迭代 |
| `engine/prompts/plan.md`、`exec.md` | §7.2/§7.3 增修 | 小 |
| `engine/prompts/external-agent-plan.md`、`prompt_templates.py` | §7.6 契約 v2 + placeholder 清單同步 | 小–中 |
| `engine/dashboard.py` | 並行啟動分支、`/api/fleet-state` + `fleet-state` SSE(§6.6)、normalize_plan_edit v2 欄位與 merge 期規則(§15.1)、fleet_parent 攔截與封存前提(§15.2)、fleet 動作(確認拆分/解散/清理)、pid 判斷修正(§15.3) | 中–大 |
| `engine/status.py` | merge 子狀態與 fleet 欄位投影(§6.6);pid_is_loop_alive 認得 engine.fleet(§15.3 M1) | 小 |
| `ui/` planValidation.ts、PlanTable、PlanEditorModal、LauncherModal、fleetViewModel | 格式 v2(track 必填)+ 啟動選項 + 狀態 pill 與 fleet 覆蓋邏輯(§6.6)+ 編輯器 track/scope 欄位與規則(§15.1);分組 UX 延後 | 中 |
| `tests/`(test_guards 擴充 + 新 test_fleet.py) | worktree fixture、閘門矩陣、resume/崩潰、外力動 main、竄改×合併期、prompt placeholder 檢查 | **大**(估與 fleet.py 本體等量或更多) |
| `README.md`、`templates/GUIDE.md` | 流程圖、並行章節、格式 v2(含「舊格式需 reset/重匯入」升級說明) | 小–中 |

不做舊版相容省下的工:格式雙軌驗證矩陣、state 自動遷移碼、外部契約雙格式敘述、
「缺省=舊行為」的條件分支與其測試——合計約 1 個工作天,並永久降低格式規則的複雜度。

---

## 10. 難度評估與工期

單人、含測試與文件、按本 repo 現行品質標準(fail-closed、O_NOFOLLOW 檔案紀律、
原子寫、防竄改)估算:

| 元件 | 難度 | 主要風險點 | 估時 |
|---|---|---|---|
| 任務格式 v2(全契約面,無相容包袱) | ★★☆☆☆ | 機械但漣漪廣(10+ 檔案) | 2–3 天 |
| Worktree 生命週期(含封存整合) | ★★★☆☆ | 冪等/殘留/鎖死/磁碟;封存前移除檢查;submodule 明示不支援 | 3–4 天 |
| fleet 協調器(監督+resume) | ★★★★☆ | 父崩潰恢復、部分失敗、與子鎖的交接時序 | 5–8 天 |
| 合併期 merge phase | ★★★☆☆ | 狀態機插入點、I9 檢查 | 2–3 天 |
| Prompt 四卡(§7)含收緊迭代 | ★★★☆☆ | 弱模型偷懶出口;merge 卡全新文本需實測迭代 | 2–3 天 |
| 合併閘門+佇列 | ★★★☆☆ | 程式碼小、正確性要求高;競態論證 | 2–3 天 |
| 測試工程 | ★★★★☆ | 多 worktree fixture、長場景、時序注入 | 5–7 天 |
| Dashboard/UI(MVP→分組) | ★★★☆☆ | MVP 近零;分組/佇列視圖另計 | 2–3 天(MVP)+3–5 天(UX) |
| 既有功能整合(§15:編輯器 v2、生命週期防護欄、投影修正) | ★★★☆☆ | 前綴鎖定改造波及測試;攔截規則的完整矩陣 | 2–3 天 |
| **合計** | **中高** | | **23–35 天;MVP(Phase 0+1)9–13 天** |

判定:**難度的重心不在「合併協定」**(它小而清晰,且是業界成熟模式——本質上是
bors/merge-queue 的本地化:在分支上驗合併結果、只允許 ff 推進主幹),
而在 fleet 的可靠性工程與測試量。沒有需要發明的新理論。

---

## 11. 風險與對策

| # | 風險 | 對策 |
|---|---|---|
| R-1 | 弱模型解衝突品質差,合併期不收斂 | red-limit reset 自動撤銷壞 merge 重試;merge_attempts 封頂→停機;issue 升級;可開 pause-before-merge |
| R-2 | 規劃期拆分不獨立 → 衝突風暴 | plan.md 拆軌規則(§7.2)+ scope 必填自檢;pause_after_plan 人工審核;「不確定就同軌」的保守預設 |
| R-3 | 資源倍增(N agent + N validate 同時跑) | --max-parallel(預設 4);validate 產物天然 per-worktree;共享 cache(如 ~/.m2)併發問題文件化,必要時 per-track repo.local |
| R-4 | 外力動 main / 人在跑動中操作 repo | I1 偵測停機;README 明示運行中不要手動碰 main |
| R-5 | 收斂輪次成本疊加(§6.3) | merge_threshold=2;track ≤ 4 建議;佇列先到先合 |
| R-6 | fleet 單點故障 | fleet.json checkpoint + 全步驟冪等 + resume 一級公民 + 崩潰注入測試 |
| R-7 | 內容級回退 main 行為(§8 殘餘風險) | merge 兩卡明文條款 + validate 含 main 測試 + @final 驗收 + 人工抽查 |
| R-8 | 測試量失控 | 分期交付;worktree fixture 抽象化;單軌回歸靠既有 4,107 行測試守住 |
| R-9 | 舊 workspace/計畫升級後直接不可用 | 刻意接受(決議③);錯誤訊息明確指示 reset-state/重匯入;README 升級說明 |

---

## 12. 分期路線圖(每期獨立可用、可停損)

| 期 | 內容 | DoD | 估時 |
|---|---|---|---|
| **P0 格式 v2** | track(必填)/scope 貫通所有契約面;plan.md/exec.md/external 契約增修(§7.2/7.3/7.6);編輯器 v2 欄位 + current_task_entered_round 解鎖(§15.1);單軌行為與今天等價 | 全測試綠;**舊格式被明確拒絕並提示升級路徑**;單軌(全同 track)計畫跑完整流程,輸出與現行一致(TASK_TAG=task-N、TRACK_CONTEXT 空);規劃剛收斂、exec 未跑輪前 task-1 可編輯 | 2–4 天 |
| **P1 並行執行(人工合併)** | fleet 建 worktree(workspace 目錄下)、切片派發、並行監督、resume;fleet_parent 攔截、封存前提、pid 修正、「確認拆分並啟動」(§15.2/15.3);軌道收斂即停在 branch 上,**合併由人做** | K 軌並行完跑;父/子任一崩潰可 resume;Dashboard 可監控各軌並列出「已完成待人工合併」branch 清單;子軌 reset/匯入/階段切換被正確攔截;父封存在 worktree 未清時被擋 | 6–9 天 |
| **P2 自動合併** | merge phase + merge 兩卡(§7.4/7.5)+ 閘門 + 佇列 + @final 匯流 + 總 REPORT + §6.6 狀態觀測(merge 子狀態、fleet-state API/SSE、pill 覆蓋)+ §15 P2 項(merge 期編輯規則、--expect-plan-version、統一通知、goal 監看) | 全自動端到端:含「兩軌衝突→agent 解掉→ff 進 main」整合測試;外力動 main 停機測試;merge 卡通過弱模型實測;**併入中/確認中/待合併/合併中/已合併在 Dashboard 全程可分辨**;編輯×fleet 重啟的版本握手測試 | 7–10 天 |
| **P3 營運強化** | Dashboard 分組/佇列視圖、scope 重疊警告、pause-before-merge、磁碟/併發調參 | UX 驗收;文件與 GUIDE 更新 | 5–8 天 |

P1 結束就有實際價值(隔離並行 + 人工整合);P2 才兌現全自動;P3 是體驗與營運。

---

## 13. 替代方案與否決理由

| 方案 | 否決理由 |
|---|---|
| 單行程多工(一個 loop 同時開 N 個 agent) | run_agent/signal/state 全部假設單輪序列;侵入式改造風險遠高於父子行程模型,且失去「子=今天的 loop」的重用與回歸保障 |
| rebase 取代 merge | agent 對 rebase 衝突的處理更差;改寫 SHA 會打亂 completed 錨定與 changed 偵測;merge commit 保留稽核鏈且 I9 可機械驗證 |
| 任務級 DAG 依賴 | 見 §4.3;弱模型產出品質與 UI/回退複雜度不成比例 |
| 每完成一個 task 就合併回 main | 合併頻率×共識成本爆炸;軌道等於失去隔離意義。粒度定為「軌道完成才合併」 |
| 用多個 clone 而非 worktree | 失去共享 refs/objects,合併協定要走 push/fetch,複雜且慢;worktree 的「main 不可雙 checkout」保險也沒了 |
| 一張 merge 卡讓 agent 自判「該併入還是該確認」 | coordinator 派工當下已知 ancestor 狀態;讓 agent 自判只是新增誤判面(§7.1 原則 6) |
| track 選填、缺省=單軌(v1 草案) | 決議不相容舊版後,必填規則更簡單、無混合狀態;舊計畫本來就要重新規劃 |

---

## 14. 已拍板決議(2026-07-12)

| # | 議題 | 決議 |
|---|---|---|
| D1 | pause_after_plan(審拆分)/ pause-before-merge(審合併)預設 | 並行模式:前者**開**、後者**關** |
| D2 | merge_threshold | **2**(執行期維持 3) |
| D3 | track 上限 / --max-parallel 預設 | **8 / 4** |
| D4 | 合併後在 main 重跑 validate | **開** |
| D5 | 子 workspace 命名 | **`<parent>--<track>`** |
| D6 | worktree 存放位置 | **`workspace/<parent>/worktrees/<track>/`**(隨 workspace 生命週期管理;封存前強制移除 worktree) |
| D7 | scope 欄位時程 | **P0 進格式與校驗,P3 才做重疊警告** |
| D8 | 舊版相容 | **不做**:track 必填;舊 plan/state 直接拒絕並提示 `--reset-state` 或重新匯入;無遷移碼 |

---

## 15. 既有功能衝突審視與調整(v4)

逐一對照現有功能面與 v2/v3 設計,共識別 6 個結構衝突(Plan 編輯器)、
7 類生命週期操作衝突、4 個監控投影衝突,及 1 項監督政策修訂。

### 15.1 Plan 編輯器(核心結構衝突)

現況語意(`engine/dashboard.py:1401-1480, 2102-2132`):停止狀態下整份快照編輯;
鎖定範圍 = 「已完成任務 + 執行期目前任務」構成的**連續前綴**
(`locked_plan_task_count` 取鎖定任務的最大 index+1);鎖定前綴的 order/內容不可變,
pending 可增刪移並重新編號 1..N;`plan_version` 樂觀鎖防跨畫面覆蓋(409);
phase=done 時 pending 不可調。

衝突點:

| # | 衝突 | 說明 |
|---|---|---|
| C1 | **連續前綴假設在拆分後的父 master plan 失效** | A 軌已完成 order 5、B 軌 order 3 還 pending → completed 不再是全域 order 的前綴;鎖定計算、「重新編號不改鎖定前綴」的驗證全部失真 |
| C2 | **編輯真相層級不明** | 拆分後子軌各持重編 1..n 的切片;改父 master 不會自動到子,改子不會回寫父 |
| C3 | **pause_after_plan 停下時 task-1 已被鎖** | 暫停點的 state 已切 exec 且 current_order=1(`engine/loop.py:1547-1550, 2092`)→ 現行規則鎖住 task-1 的內容與位置,**正好擋掉「審核拆分時調整任務」這個主要使用場景**;此問題今天就存在,並行只是放大它 |
| C4 | **合併期無對應規則** | merge/merge-ready 不在編輯器的 phase 分支裡(只有 done 擋 pending)→ 會允許對 merge-ready 軌道加 pending task,而該轉移語意未定義 |
| C5 | **fleet 重啟與人工編輯的寫入競態** | 編輯落盤若晚於子迴圈 load_state,輪末竄改偵測會把人工編輯當 tamper 還原——編輯靜默消失。今天單人操作罕見,fleet 自動化會讓它常態化 |
| C6 | **欄位不含 track/scope** | 編輯 payload、鎖定內容比對(text/ref)、前端驗證都未涵蓋新欄位 |

**建議調整——核心原則:編輯永遠發生在「循序性成立」的那一層,讓既有鎖定語意原封不動:**

| 生命週期點 | 編輯對象 | 規則 |
|---|---|---|
| 拆分前(規劃期/規劃後暫停) | 父 master plan | 全開放:track/scope 可改,跨軌移動 = 改 track 欄位;儲存時整包過 work.py v2 驗證 |
| 拆分後,軌道 exec 中 | **該子 workspace 的切片**(既有編輯器原樣適用——子軌嚴格循序,前綴假設完整成立) | 沿用「執行中鎖死、停止才可編輯」;track 欄唯讀顯示,scope 可改;跨軌移動不做原地支援,以「A 軌刪除 + B 軌新增」兩步表達(P3 再做 UI 糖) |
| 軌道 merge / merge-ready / merged | 該子切片 | **比照 done**:「沒有可調整的 pending task」——v1 不支援對已離開 exec 的軌道加任務;要加就放 @final,或走解散重規劃 |
| @final 執行期 | 父 workspace(此時是普通循序迴圈) | 既有編輯器原樣適用 |

配套修改:

- `normalize_plan_edit` / `planValidation.ts`:欄位擴充 track/scope;鎖定前綴的
  內容不可變比對涵蓋新欄位;最終正規化結果交 `work.py validate_plan` 複驗
  (單一真相,dashboard 不自維一套欄位規則);phase ∈ {merge, merge-ready} 併入
  done 分支的「pending 不可調」規則(解 C4)。
- 新 state 欄位 `current_task_entered_round`(進入或切換 current task 時記當下
  round;僅當 `state.round > 該值`——即該任務真的跑過至少一輪——才鎖定 current):
  解 C3。規劃剛收斂、exec 輪一輪未跑時,task-1 完整可編輯;
  **這對單軌使用者同樣是體驗修正**,故放 P0。validate_state_shape 同步。
- 子迴圈新 CLI 參數 `--expect-plan-version N`:fleet 重啟子軌時帶上它讀到的版本,
  loop 於 load_state 後不符即乾淨退出、fleet 重讀重派——把 C5 的時序競態變成
  顯式握手(解 C5;搭配 §15.4 的「不自動重啟」政策,常態下根本不會發生)。
- 拆分後父 master 降級為快照:真相 = 各子切片 + fleet.json 的 order_map;
  REPORT 聚合對「編輯後新增的任務」發新全域編號(解 C1/C2——父層不再有
  需要鎖定語意的活 plan)。父 PlanTable 執行期顯示唯讀聚合視圖
  (P2 定資料契約、P3 做視覺)。

### 15.2 生命週期操作衝突

| 功能 | 衝突 | 調整 |
|---|---|---|
| 封存(api_archive) | worktree 在父 workspace 目錄內(D6):父帶著活 worktree 封存 = 整目錄搬移毀掉 git worktree 註冊表、子 state.config.repo 指向失效 | 前提檢查(fail-closed):父封存需「全部子軌已封存或刪除,且 worktrees 已 `git worktree remove` + prune」;子軌封存需自己的 worktree 已移除。fleet 提供「清理已合併軌道」動作(移 worktree、留 branch)。還原不重建 worktree——fleet resume 冪等重建 |
| 重置/匯入(reset-state、import plan) | 父在子軌存在時重置 → 孤兒子軌/branch/worktree;子軌被直接 import → 切片與 order_map 失聯 | 父 reset 前提 = 無 fleet-managed 子軌(或明確 cascade 雙重確認);**子軌的 reset/匯入一律拒絕**(I10,config 帶 `fleet_parent` 旗標,dashboard 路由據此擋並提示從父層操作) |
| 階段切換(api_phase) | 子軌「回規劃期」無意義(切片沒有規劃期);父「回規劃期」= 解散拆分,具破壞性 | 子軌:擋(I10)。父:改為 fleet 動作「解散並重規劃」——僅當所有軌道無未合併 commit 才直接放行,否則列出將棄置的 branch 清單要求雙重確認(branch 一律保留供稽核) |
| 停止 | 停父(fleet)是否級聯殺子?語意未定義 | 預設**不級聯**:子軌自主收斂,鎖保證安全,merge-ready 的原地等待;「停止整個 fleet(含子軌)」做成明確批次動作(順序:先父後子)。子軌單獨停止 = 合法人工介入,fleet 標記「已停止/需關注」,不自動搶回(§15.4) |
| 一鍵重跑(api_run) | run-from-config 只會 spawn engine.loop | 父 config 帶並行旗標 → respawn `engine.fleet`;子 config 帶 fleet 參數照常 respawn engine.loop(單軌人工續跑合法);config 白名單驗證涵蓋新入口與新參數 |
| 以此為範本啟動 | 子軌 config 含 fleet 專屬欄位,複製會產生殘缺設定 | 範本預填時剝除 `merge_target`/`fleet_parent` 等 fleet-only 欄位 |
| 批次操作 | 預覽的跳過原因不認得 fleet 語意 | 跳過原因新增「fleet 管理中」類別;其餘沿用既有逐筆安全 API |

另:規劃後暫停點的「▶ 運行」在並行模式改為「**確認拆分並啟動**」fleet 動作——
先跑 §4 v2 整包校驗(含 track 數、scope 規則),過了才建 worktree 派發;
這是 D1(pause_after_plan 預設開)與編輯器(15.1 拆分前全開放)的自然銜接點。

### 15.3 監控與投影衝突

| # | 衝突 | 調整 |
|---|---|---|
| M1 | `status.py:70` 的 `pid_is_loop_alive` 只認 `loop.py`/`engine.loop` → 父 workspace 由 engine.fleet 持有 pid 時被判「已停止」,整個執行期父卡片假死 | 判斷式納入 `engine.fleet`;dashboard 端同型 pid 檢查一併修 |
| M2 | 執行期父 workspace 沒有輪次 → 停滯/需關注 heuristics 誤判 | health 對「fleet 執行中」的父 workspace 停用輪次型告警,以 §6.6 fleet-state 為準 |
| M3 | notify_cmd:N 個子軌各自發終態通知 → 轟炸 | 子軌一律以空 notify 啟動;fleet 統一發 `track_merged` / `merge_failed` / `fleet_completed` / `plan_paused` / `stuck` |
| M4 | goal 變更偵測空窗:執行期父不跑輪、子軌在隔離 worktree 永遠看不到 main 上的 goal 變更 | fleet 監督迴圈每輪比對 goal hash;變更即平順停全艦 + 需關注(拆分依據的 goal 已失效,必須人工重規劃) |

### 15.4 監督政策修訂

v2 原句「子迴圈異常退出 → 有限次數重啟 + 指數退避」**收回**。修訂為:
fleet 預設**不自動重啟**異常退出或被人工停止的子軌——一律標記需關注、交人決定。
理由:①符合本 repo「機械防線不猜測人類意圖」的一貫哲學(人工停止可能正是為了編輯
計畫或檢查現場,自動搶回會直接製造 C5 競態);②crash 的根因(CLI 環境壞掉、磁碟滿)
重啟通常無效,既有 agent-backoff 已在子迴圈內處理 CLI 級異常,fleet 層再疊一層
重啟只是把問題藏久一點。fleet 只在**它擁有的既定轉移**重啟子軌:初次派發、
ff 失敗重排回合併期、人工按下「恢復軌道」。`--auto-restart`(含退避與次數上限)
留作 P3 選配。

### 15.5 調整的分期歸屬

- **P0**:編輯器 track/scope 欄位 + work.py 共用複驗;`current_task_entered_round`
  解鎖修正(單軌即受益);planValidation 同步。
- **P1**:`fleet_parent` 旗標與 dashboard 攔截(reset/匯入/階段切換,I10);
  封存前提檢查與「清理已合併軌道」;M1 pid 判斷修正;「確認拆分並啟動」動作;
  停止語意(不級聯 + 批次動作)。
- **P2**:merge/merge-ready 的編輯規則(C4);`--expect-plan-version` 握手(C5);
  M2 health 調整、M3 fleet 統一通知、M4 goal 監看;父唯讀聚合視圖資料契約。
- **P3**:跨軌移動 UI 糖;解散重規劃流程精緻化;`--auto-restart` 選配;父聚合視覺。

---

## 附錄 A:合併閘門時序(兩軌衝突場景)

```text
t0  main=M0;track-A、track-B 各自從 M0 開跑
t1  A 全 task 收斂 → merge phase:is-ancestor(M0, A)成立 → 確認輪×2 → merge-ready(tipA)
t2  fleet:ff merge tipA → main=A ✔(main 上 validate 綠)
t3  B 全 task 收斂 → merge phase:is-ancestor(main=A, B)不成立
      → 併入輪:agent 在 wt/B `git merge --no-commit A`,解衝突,validate 綠,commit
        (HEAD 動 → done=0)
      → 確認輪×2(逐條 DoD + validate,無異動)→ merge-ready(tipB')
t4  fleet:ff merge tipB' → main=B' ✔ → 進 @final
若 t3 的 merge 把東西弄壞:validate 紅 → done 歸零重試;連紅達 red-limit →
reset --hard 回 B 的綠點(= 撤銷整個 merge)→ 下一輪重新 merge——直到收斂或 stuck-stop。
```

## 附錄 B:與現行文件的銜接

- README「流程」章節在 P2 落地時加並行分支圖;README.md:34 的「目前不會自動拆任務、
  合併分支」敘述屆時移除;新增「升級注意:任務格式 v2 不相容舊版」段落。
- `templates/GUIDE.md` 與 Prompt 產生器補「什麼樣的 goal 適合並行」指引。
