# 規劃書：任務並行拆分 × Worktree 隔離 × Agent 收斂式合併

> 狀態：**v6，agent-first 方向已定案，可據此分期實作。**
>
> 核心需求：規劃 agent 將任務拆成可並行 track；程式建立隔離 worktree；每條 track
> 沿用現有無狀態多輪 agent 收斂；track 完成後由 agent 自行整合最新主幹與解衝突；
> 程式只在 done 共識、驗證與 fast-forward 條件成立時推進整合分支。
>
> v6 繼承 v5：①人工審核全部改為選配且預設關閉；②語意判斷交給 agent，多輪自動重試；
> ③機械層只守 repo/state/worktree 不被破壞；④真正 CAS + journal + validate 失敗自動 rollback；
> ⑤`@final` 也在隔離 worktree 執行；⑥移除 archive/restore 與舊 workspace resume；
> ⑦fleet-managed plan 拆分後唯讀，移除複雜人工跨軌編輯；⑧移除 Dashboard read-only mode；
> ⑨Dashboard 每次啟動在 serve 前必須停止全部已辨識 coordinator，不自動 resume，改由使用者手動啟動。

---

## 0. TL;DR

方案可行。現有 loop 已具備可重用的單 worktree writer lock、無狀態 agent round、done
共識、綠點 reset、受保護檔案與 import-plan。新工程量集中在：

1. `engine/fleet.py` 的父子生命週期與 crash resume。
2. child 的 `merge / merge-ready` 狀態機。
3. integration ref 的 CAS transaction、integration validate 與 rollback。
4. Dashboard 的 fleet-parent / fleet-child 所有權與控制投影。
5. 多 worktree、ref 競態、rollback 與自動修復整合測試。

正常流程不需要人工介入：規劃收斂後自動拆分、執行、重併、驗收、CAS 合入、跑
`@final`、清理 child。只有 state/repo 身分損壞、未知外力移動 integration ref、權限或
磁碟等結構性錯誤才停機交人。

---

## 1. 需求與設計原則

| # | 需求 |
|---|---|
| R1 | 規劃 agent 產生帶 `track` 的可並行計畫 |
| R2 | 每條一般 track 使用獨立 branch、Git worktree、workspace |
| R3 | 每軌沿用現有單輪無狀態 agent + done AND gate，直到收斂 |
| R4 | track 收斂後由 agent 在自己的 worktree 整合最新 integration tip、解衝突並再收斂 |
| R5 | 程式確認 done、綠燈、乾淨、祖先與 CAS 條件後才推進 integration ref |
| R6 | integration worktree validate 失敗時自動 rollback，錯誤交回原 track 再修 |
| R7 | 全部一般 track 完成後自動執行 `@final`，全程無預設人工 gate |
| R8 | 不相容舊版 plan/state；不支援 archive/restore 或跨大版本 resume |

### 1.1 Agent 與機械層的責任邊界

Agent 負責語意：

- 如何拆 track、是否需要縮併 track。
- 如何實作任務、如何解衝突、如何修整合缺陷。
- DoD 是否真正滿足、是否還需要修改程式。
- issue 是提供下一輪 agent 的 context，不是預設人工 gate。

機械層只負責安全與交易：

- workspace、state、branch、worktree、repo 身分與 schema 合法。
- 同一 worktree 同時只有一個 writer。
- goal、plan-doc、coordinator state 不可由 agent 改寫。
- candidate 包含派發時的 integration tip、工作樹乾淨、validate 綠、done 達標。
- integration ref 以 expected-old SHA 做 CAS；integration validate 紅時可自動 rollback。
- crash 後只能從 journal 證據恢復，不猜測未知 Git 現場。

scope、檔案接觸面與 merge commit 形狀都不是 hard gate。Prompt 會給保守建議，但程式不以
scope overlap 或「第二 parent 必須等於 integration tip」拒絕一個已通過祖先、驗證與共識條件的結果。

---

## 2. 現有機制：可重用與不可誤認的邊界

### 2.1 可直接重用

| 現有機制 | 新用途 |
|---|---|
| linked worktree 專屬 git-dir lock | 每個 child loop 各自單 writer |
| workspace `.run.lock` | 每個 child state 各自單 writer |
| `--import-plan --start-phase exec` | child 直接吃切片計畫 |
| round token / dispatch.json / work.py | merge round 也走同一套回報協定 |
| 任一變更使 done 歸零 | merge 或 merge-fix 後自動重新累積確認 |
| last-green + reset | 撤銷未收斂的壞整合結果 |
| protected snapshot | 每條 worktree 保護 goal 與 plan-doc |
| Dashboard 多 workspace projection | 顯示 parent 與 child，不直接寫 Git 真相 |

### 2.2 不可直接沿用的假設

- 現有 reset 判斷只處理 `exec`；`merge` 必須有自己的 no-progress、red/reset 與 ready 邏輯。
- Git 禁止同一 branch 被兩個 worktree checkout，**不等於**其他 process 無法
  `git update-ref` 修改該 branch。因此 `git merge --ff-only` 不是完整 CAS。
- worktree 只隔離 checkout/build 目錄，不隔離 port、DB、Docker、`~/.m2`、npm cache 等外部資源。
- Dashboard 現有 run/edit/phase/archive 都假設單一 standalone workspace，fleet child 不能直接繼承。
- 主 worktree 不一定 checkout `main`；實作不得把 integration branch 寫死為 main。

---

## 3. 名詞與角色

- **integration ref**：本次 fleet 要推進的本地 branch 完整 ref，例如 `refs/heads/main`。
- **integration worktree**：啟動 fleet 時 checkout integration ref 的主 worktree。
- **fleet-parent**：保存完整規劃快照、fleet.json、總 REPORT 與操作入口的 workspace。
- **fleet-child**：一條一般 track 或 `@final` 的 loop workspace。
- **candidate**：child 收斂後準備以 fast-forward 推進 integration ref 的 branch tip。

```text
fleet-parent / engine.fleet
  ├─ planning handoff：既有 loop 只跑規劃，收斂後交回 fleet
  ├─ track auth   → worktree/auth   + workspace parent--auth
  ├─ track report → worktree/report + workspace parent--report
  ├─ merge queue  → CAS transaction → integration worktree validate
  └─ @final       → worktree/_final + workspace parent--_final → 同一 CAS gate
```

Agent 永遠不在 integration worktree 執行。integration worktree 只由 fleet 做 CAS 後的同步、
validate 與必要 rollback。

---

## 4. 計畫格式 v2

```json
[
  {
    "order": 1,
    "task": "實作 auth，包含可執行 DoD",
    "ref": "PLAN.md#auth",
    "track": "auth",
    "scope": ["src/auth/**", "tests/auth/**"]
  },
  {
    "order": 2,
    "task": "實作 report，包含可執行 DoD",
    "track": "report",
    "scope": ["src/report/**"]
  },
  {
    "order": 3,
    "task": "跨模組整合與端到端驗收",
    "track": "@final"
  }
]
```

### 4.1 Hard schema

- `order`：全域唯一且為 `1..N` 連續整數。
- `task`：非空字串，必須自足並包含可執行 DoD。
- `ref`：選填字串或 null。
- `track`：必填；一般名稱使用 `[a-z0-9][a-z0-9_-]{0,23}`。
- `@final`：唯一 `@` 保留名稱；fleet 在一般 track 全部合入後才建立及執行。
- track 總數含 `@final` 最多 8；`--max-parallel` 預設 4。
- `scope`：選填非空字串陣列，只作 agent 提示與 UI 顯示，不驅動檔案操作、不做 overlap hard gate。
- 未知欄位拒絕，避免契約靜默漂移。

Prompt 建議把 `@final` 放在全域 order 尾端，但 scheduler 依 track 名提取，並不依位置判斷。

### 4.2 單軌與多軌入口

- 普通 `engine.loop` 只執行一個一般 track 的 plan。
- `engine.fleet` 負責多 track 與含 `@final` 的 plan。
- fleet 規劃 subprocess 使用新旗標 `--handoff-after-plan`：規劃一收斂就停止並交回 fleet，
  不會誤進 master plan 的 exec。
- 只有一個一般 track且沒有 `@final` 時，fleet 可退化委派 standalone loop，不建 worktree。
- 只有 `@final` 時視為 standalone task plan；不需要先建立空的一般 track。

### 4.3 Breaking upgrade

- parent/child/standalone state 新增必填 `state_schema_version: 2` 與隨 workspace generation
  建立的 32 字元 `workspace_generation`；reset/recreate 產生新值，刪除 journal 不得只靠可重用 inode
  判斷同名目錄身分。
- fleet.json 使用獨立 `schema_version: 1`。
- 舊 plan 缺 track 直接拒絕。
- 舊 workspace 不遷移、不 restore、不允許執行或重跑；Dashboard 只在確認讀到 v1 standalone
  state 時投影 delete-only 畫面，允許安全永久刪除後重新開始。損壞或缺 generation 的 v2 state
  不得降級成 legacy delete-only。

---

## 5. 全生命週期

```text
1. preflight
   - integration worktree 是實體 Git worktree、乾淨、validate 綠
   - integration ref 正由該 worktree checkout，記錄 expected SHA
   - goal/plan-doc 已 tracked 且在 HEAD
   - submodule repo 目前明確不支援

2. planning
   - fleet spawn loop --handoff-after-plan
   - agent 多輪 create-plan / plan-ok 收斂
   - loop 停在 handoff 點，fleet 驗證 plan v2 並凍結 plan hash
   - pause-after-plan 預設 off；若選配 on，才等待綁 plan hash 的 approve token

3. split + exec
   - 每條一般 track 建 branch/worktree/child workspace
   - 計畫切片，child order 重編 1..n，fleet 保存 global/local order map
   - 最多 max-parallel 個 child 自動執行至全部 task done

4. child merge convergence
   - integration tip 不是 child HEAD 祖先 → merge-sync agent
   - 已是祖先 → merge-confirm agent
   - 修改、修復、issue、驗證失敗都自動進下一輪
   - 連續無異動 done 達 merge threshold → merge-ready

5. fleet merge transaction
   - gate 通過 → CAS 推進 integration ref
   - 同步 integration worktree → integration validate
   - PASS：track merged
   - FAIL：自動 rollback；錯誤送回 child；child 重新確認/修復後再排隊

6. @final
   - 所有一般 track merged 後，從最新 integration SHA 建 final worktree
   - 執行 @final 切片並走同一 merge-ready / CAS / validate 流程

7. finish
   - 聚合 REPORT
   - 自動移除已合併 child/final worktree 與 child workspace
   - branch 保留供稽核；fleet-parent 與 REPORT 保留，使用者可安全刪除
```

---

## 6. Child loop 狀態機

### 6.1 State 欄位

```json
{
  "state_schema_version": 2,
  "workspace_generation": "<32-char-random-hex>",
  "workspace_kind": "fleet-child",
  "fleet_parent": "orders",
  "fleet_run_id": "...",
  "track": "auth",
  "phase": "exec | merge | merge-ready | done",
  "merge_stage": "sync | confirm | null",
  "merge_target_ref": "refs/heads/main",
  "merge_target_tip": "<sha-or-null>",
  "merge_ready_sha": "<sha-or-null>",
  "merge_control_generation": 3
}
```

Standalone state 仍使用 `plan | exec | done`，`workspace_kind=standalone`。

### 6.2 Exec → merge

- standalone 最後一個 task 完成後進 `done`。
- fleet-child 最後一個 task 完成後進 `merge`，清空 done、red、stall 等暫態。
- completed SHA 必須持續是目前 HEAD 的祖先；agent 不得改寫既有 track history。

### 6.3 Merge round 選擇

輪初取得 `integration_tip` 並寫入 state + dispatch：

```text
is-ancestor(integration_tip, HEAD)?
  no  → stage=sync，派 merge-sync.md
  yes → stage=confirm，派 merge-confirm.md
```

sync agent 的目標是「讓派發的 integration tip 成為 HEAD 祖先且驗證綠」。Prompt 建議
`git merge --no-commit <tip>`，但機械層不要求固定第二 parent。Agent 可在不改寫既有歷史、
不修改 integration ref 的前提下，自行解衝突並建立必要修復 commit。

### 6.4 Merge round 結果

| 結果 | Coordinator 動作 |
|---|---|
| HEAD/工作樹有變更且 validate 綠 | 更新 last-green；done 歸零；下一輪獨立確認 |
| dirty 或 validate 紅 | done 歸零；依 merge red/stall 防線 reset 後自動下一輪 |
| 無變更、沒有 done、正常退出 | 視為 no-progress；保留 issue/notes；自動換下一輪 agent |
| confirm 無變更、validate 綠且 done | done +1 |
| agent crash/timeout | signal 作廢；保留安全現場；child loop 依既有 backoff 自動重試 |
| protected/state/ref 協定被破壞 | 回到 round-start SHA；結構性錯誤才停機 |

語意型 retry 預設不設次數上限，與現有 loop 的「直到收斂」一致。營運者可用既有
語意修復預設持續收斂，不設機械 retry 次數 gate；營運上若 child coordinator 本身反覆 crash，
可用 fleet 的 `--max-child-restarts N` 選配封頂，`0` 表示不限且為預設。

### 6.5 進入 merge-ready

必須同時成立：

- `done_count >= merge_threshold`，預設 2。
- 最新 `integration_tip` 是 HEAD 祖先。
- HEAD == `last_green_sha`。
- worktree 乾淨。
- completed anchors 都是 HEAD 祖先。

成立後保存 `merge_ready_sha=HEAD`、phase=`merge-ready`，child 正常停止。

### 6.6 merge-ready 自動重開

Child resume 時先檢查：

- integration ref 已前進且新 tip 不是 HEAD 祖先 → 自行切回 `merge/sync`。
- 收到較新的 fleet repair control → 自行切回 `merge/confirm`，把 integration validate 錯誤注入 notes。
- branch tip、HEAD、merge_ready_sha 不一致 → 不猜測，停機顯示結構錯誤。
- 條件仍成立且沒有 control → 保持 merge-ready，不多跑 agent。

Fleet 不直接寫 child state；它只原子寫入 token/run-id/generation 綁定的 sideband control。
Child 驗證 control 後自行轉移並更新 state，維持單一 state writer。

Sideband control schema 固定為：

```json
{
  "schema_version": 1,
  "run_id": "32-char-lowercase-hex",
  "track": "auth",
  "generation": 4,
  "action": "repair | stop | lease",
  "expected_child_sha": "...",
  "integration_sha": "...",
  "note": "bounded validate tail"
}
```

`note` 套用固定 bytes/chars 上限；舊 generation、錯 run-id/track/SHA、未知 action 一律忽略並
記 anomaly，不可讓殘留控制檔影響新的 fleet run。

---

## 7. Fleet coordinator 與 merge transaction

### 7.1 Fleet 真相

`workspace/<parent>/fleet.json` 使用 checkpointed atomic write：

```json
{
  "schema_version": 1,
  "run_id": "...",
  "workspace_kind": "fleet-parent",
  "phase": "planning | awaiting-approval | splitting | exec | merging | final | cleaning | stopped | done | failed",
  "resume_phase": "planning | splitting | exec | final | cleaning | null",
  "integration_ref": "refs/heads/main",
  "integration_worktree": "/absolute/repo/path",
  "expected_integration_sha": "...",
  "plan_sha256": "...",
  "order_map": {"auth": {"1": 1}},
  "tracks": [],
  "merge_queue": [],
  "merge_tx": null,
  "loop": {"pid": 123, "session_id": "...", "started_at": "..."}
}
```

`failed` 必須保存合法 `resume_phase` 與 bounded `last_error`；下一次 resume 先回到該階段，
不把 `failed` 當成可直接執行的 scheduler phase。每次 coordinator session 另記入最多 100 筆
`supervisor_session_history`，供舊 child adoption、lease 與安全刪除核對；UI 顯示上次錯誤稽核，
但只在 truth 無法安全讀取或 backend 明示不可續跑時隱藏 Run。

Track 至少保存 logical name、safe directory、branch full ref、worktree canonical path、child
workspace、status、tip、restart count、integration validate failure count、control generation。
Track status 值域固定為
`pending | running | merge-ready | merging | repairing | merged | stopped | failed | cleaned`；
未知值拒絕，避免 Dashboard 與 resume 對同一狀態各自猜測。

### 7.2 Gate

合入前只做必要機械檢查：

```text
G1 child phase=merge-ready，child coordinator 已停止
G2 child HEAD = branch tip = merge_ready_sha = last_green_sha
G3 child worktree 乾淨；completed anchors 都在 candidate 祖先鏈
G4 expected integration SHA 是 candidate 祖先（可 ff）
G5 integration ref 仍精確等於 fleet expected SHA
G6 integration worktree 乾淨，fleet 持有其 git-dir writer lock
```

不檢查 scope overlap、commit message、merge commit 第二 parent或特定衝突解法。

### 7.3 真正 CAS 與 journal

`git merge --ff-only` 不當作 CAS。流程如下：

```text
1. fleet.json merge_tx = {
     track, expected_sha, candidate_sha, stage: "prepared"
   }
2. git update-ref <integration_ref> <candidate_sha> <expected_sha>
3. merge_tx.stage = "ref-updated"
4. integration worktree: git reset --hard <candidate_sha>
5. merge_tx.stage = "validating"
6. run validate
```

Validate PASS：

```text
merge_tx.stage = "validated"
expected_integration_sha = candidate_sha
track.status = merged
merge_tx = null
```

Validate FAIL：

```text
merge_tx.stage = "rollback-prepared"
git update-ref <integration_ref> <expected_sha> <candidate_sha>
git reset --hard <expected_sha>
重跑 baseline validate
merge_tx.stage = "rolled-back"
寫 child repair control（含失敗輸出尾段）
重啟 child，自動重新確認/修復
merge_tx = null
```

Rollback 後 baseline 也紅、ref 出現第三個未知 SHA、或 integration worktree 有非交易造成的
dirty 狀態時，才停機交人；不得自行 reset 未知的人類變更。

### 7.4 Crash resume 表

| Journal / Git 現場 | Resume |
|---|---|
| prepared + ref=expected | 重新執行 CAS |
| prepared + ref=candidate | 視為 CAS 已成功，切 ref-updated |
| ref-updated/validating + ref=candidate | 同步 worktree並重新 validate |
| rollback-prepared + ref=candidate | 重試 rollback CAS |
| rollback-prepared + ref=expected | 完成 worktree rollback與 baseline validate |
| transaction 中 ref=第三個 SHA | 未知外力介入，停機 |
| validated 但 track 尚未標 merged | 由 ref/candidate/validate checkpoint 補完冪等狀態 |

所有 Git 副作用前先寫 intent；所有 journal 清除前先把完成結果落 fleet checkpoint。

### 7.5 Merge queue

- 一次只處理一條 candidate。
- integration ref 每次前進後，其餘 ready child 由 §6.6 自動回 merge。
- readiness 先到先服務；不以 scope、track 大小或人工優先序重新排序。
- merge queue 進入後由 coordinator 自動完成 CAS、validate、rollback 與修復回送，不設人工
  pause-before-merge。需要停止時只使用 parent graceful stop，且不切斷已開始的 transaction。

---

## 8. Supervisor、停止與 workspace 所有權

### 8.1 Workspace kind

```text
standalone   → 現有 run/edit/phase/delete 語意
fleet-parent → fleet 唯一操作入口；plan 拆分後唯讀
fleet-child  → Dashboard 唯讀；只由 fleet spawn/resume/stop/delete
```

Dashboard 與 API 必須由 `workspace_kind + fleet_run_id` 判斷，不只依可被覆寫或遺漏的 config。
Fleet planning subprocess 從建立 fresh state 起就帶
`--workspace-kind fleet-parent --fleet-run-id <id>`，因此 parent `state.json` 在 fleet.json 尚未
完成第一個 checkpoint 前也不會被誤認成 standalone；handoff 後 Dashboard 以 fleet.json 的
pid/phase 覆蓋 planning state 中已清空的 loop pid。Child import-plan 則由 fleet 明確注入
`workspace_kind=fleet-child`，不得由 plan JSON 或 agent 輸出決定。

### 8.2 Plan 編輯

- 規劃收斂前：沿用 create-plan / plan-ok。
- 選配 pause-after-plan 時：允許在 parent 編輯完整 master plan，儲存後重算 plan hash。
- 一旦 splitting 開始：parent master 與所有 child slice 全部唯讀。
- 不支援拆分後人工跨軌移動、child 新增任務或 PlanEditor 修改 slice。
- 需要新計畫時，停止並安全刪除 fleet 後重新規劃；branch 仍保留供取回成果。

因此不需要 v4 的 `current_task_entered_round`、跨軌 A 刪/B 加、父聚合 plan 再編號，亦不需要
以人工編輯為前提的 `--expect-plan-version`。

### 8.3 自動 restart 與人工 stop 的區分

- child coordinator 非預期退出：fleet 指數退避後自動重啟。
- agent CLI crash/timeout：由 child loop 既有 backoff 處理，不需要 fleet 重啟 coordinator。
- child 收到 fleet 內部帶 session/run-id 的 stop control：停止後不自動重啟。
- Dashboard 不提供 child stop 入口或 child mutation API；一般與診斷操作都由 parent 統一管理。
- parent graceful stop：先停止派發與 merge，再要求所有 child 完成本輪後停止，最後 parent 退出。
- fleet process crash：child 在每輪邊界檢查 parent 持有的 kernel flock lease；process death 時
  由 kernel 立即釋鎖，child 完成當輪即停，避免依賴時鐘 timeout 的 orphan writer。
- resume fleet：以 O_NOFOLLOW 讀取並比對 fleet.json/checkpoint、child state、Git
  worktree/branch/ref；仍持有合法 `.run.lock` 的舊 child 先 adoption，不重複 spawn，並把
  `child-adopted` 寫入 bounded event history。

`--max-child-restarts 0` 表示不限，預設 0；權限、state schema、repo 身分等結構錯誤不重啟。

### 8.4 Planning handoff 與選配 approval

Fleet process 從 preflight 起持有 parent `.fleet.run.lock`，並在 planning 前先取得 standalone 與
Fleet 共用的 integration worktree 單 writer lock。Planning subprocess 另持有 parent `.run.lock`，
且只接受同一 run-id 由 Fleet 透過 inherited FD 傳入的**同一把** integration lock；因此
planning process 啟動、退出與 split 之間沒有 writer 空窗。Fleet 一直持鎖到 coordinator 退出。

若使用者開啟 pause-after-plan，fleet 進 `awaiting-approval`，Dashboard 寫入包含
`run_id + plan_sha256 + generation` 的一次性 approval control。預設 off 時不建立人工等待點。

### 8.5 Goal 變更

fleet 監看 integration worktree 中 tracked goal 的 HEAD blob/hash。運行期間偵測到 goal/ref
被未知外力改變時，平順停 fleet並保留所有 branch。這是人類改變需求真相，不由 agent
自行猜測是否可沿用舊計畫。

---

## 9. Worktree、清理與刪除

### 9.1 建立

- 路徑：`workspace/<parent>/worktrees/<safe-track>/`；`@final` 使用普通 track 不可能產生的 safe 名 `_final`。
- `run_id` 使用 32 字元小寫 hex UUID；只作 ref/control 身分，不接受使用者自由文字。
- branch：`refs/heads/loop/<run-id>/<safe-track>`，建立前以 `git check-ref-format` 驗證。
- `git worktree add -b <branch> <path> <base-sha>`。
- 建立前驗證 parent、`<parent>--<track>`、worktree component 與完整路徑長度；任何 component
  超過檔案系統 `NAME_MAX` 或組合後 workspace 名不合法就於 preflight 拒絕。
- resume 時 branch、HEAD、canonical path、common git-dir、fleet run-id 全部吻合才沿用。
- 存在但不吻合不自動刪除或 force，停機保留證據。

Worktree 是 checkout 隔離，不是 OS sandbox；agent 仍可能看見同使用者可讀路徑。Prompt 禁止
操作其他 worktree/coordinator，fleet 以 ref/state 身分檢查偵測越界結果。

### 9.2 成功清理

每條 track 通過 CAS + integration validate 後：

1. 確認 child process 已停、worktree 乾淨、branch tip 已是 integration ref 祖先。
2. 在 parent `evidence/tracks/<safe>/evidence.json` 先保存 bounded state/no-progress、agent/validate
   command hash、prompt hash/copies、console/history tails 與 event history，寫入 path + SHA-256。
3. 依 `evidence-captured → worktree-removed → child-removing → child-removed → complete` journal
   執行 `git worktree remove`、prune、child tombstone rename/rmtree；每一階段都可 crash/resume。
4. 清除 per-track runtime temp/cache；branch 保留。

全部完成後保留 fleet-parent、fleet.json、history 與 REPORT。

### 9.3 安全刪除

- v5 移除 archive/restore API 與 UI；不支援封存後重跑。
- 既有 `.archive` 資料不自動刪除，但新版 UI 不提供 restore；由文件說明人工備份/移除。
- standalone 停止後可直接安全刪除 workspace。
- fleet-child 不接受單獨一般刪除。
- fleet-parent 執行中只提供 group graceful stop；確認 parent/children 都已停止後，才提供永久刪除。
  刪除本身是全 preflight 群組交易：核對 run/session/track/plan/command/branch/tip/tombstone 與所有
  writer locks → 移除 registered worktree → prune → 刪 child workspace → 刪 parent workspace。
- standalone/group 在第一個 Git remove 或 workspace rename 前，於 workspace root 的外部 operation
  目錄持久化 bounded delete journal；journal 綁定 logical name、run-id、Git ref/tip/common-dir、每個
  source inode、不可重用 generation 與 deterministic tombstone。v2 使用 state 的
  `workspace_generation`；legacy delete-only 在持有 writer lock 的 workspace 內先建立並 fsync 獨立
  `.delete-generation` transaction marker，再寫外部 journal。marker→journal 間 crash 時下次刪除重用
  同一 marker，不依賴 legacy state migration。API 先恢復 pending journal，才讀一般 state，因此
  rename、unlink、rmdir、prune 或回應前任一步驟中斷都可用同一請求續跑。
- standalone 首次刪除的 state/kind/liveness revalidation、generation/marker 建立、journal 與 rename
  必須綁在同一個 writer-locked directory descriptor；modern 比對 preflight `workspace_generation`，
  legacy 比對 descriptor snapshot inode 並在鎖內重讀全部 state candidates。path preflight 後不得重新
  open 未綁定身分的同名目錄，避免 rename replacement race。
- recovery 只沿用 journal 精確記錄且 descriptor/inode/ref 仍吻合的項目；同名新 workspace、不同 branch
  worktree 或被替換的 tombstone 一律保留並回 409 要求 fresh confirmation，不移除新 workspace 的 job、
  不建立新的 writer lock，也不掃描任意 `.delete-*`。即使 filesystem 重用舊 inode，也必須以 generation
  mismatch 辨識 replacement；只有無鎖 generation 初讀與取得舊 writer locks 後重讀都吻合才可續刪。
  全部完成後才移除 journal；任一步失敗就停止並保留剩餘現場，絕不先遞迴刪 worktree 目錄再修
  Git registration。

---

## 10. Prompt 調整

### 10.1 plan.md

新增 track/schema 範例與拆軌原則：

- 不確定能否獨立就同軌；不要為並行而硬拆。
- 同一 track 只依賴 integration 起點與同軌前置任務。
- 真正跨軌工作放 `@final`。
- scope 是預期接觸面提示，不是承諾或機械限制；實作需要時 agent 可修改 scope 外檔案。
- plan-ok 表示 agent 已獨立檢查無明顯跨軌依賴，但不需要人工確認才生效。
- 本階段只允許 goal/ref/plan 影響面內的針對性取證，不做全 repo 列檔或 generated 產物巡檢。
- 未指定的一般實作細節由 agent 依 goal、ref 與 repo 慣例自行決定；只有需求意圖、安全／
  不可逆外部狀態或新外部權限的重大未決衝突才 issue。

### 10.2 exec.md

新增：

- `TRACK_CONTEXT`：說明目前 track、其他 worktree 同時進行及避免無關大改的成本。
- `TASK_TAG`：多軌 commit 建議 `<track>/task-N`，但 commit message 不作 gate。
- 任務總覽只列同軌；task 全文與 DoD 仍是本輪真相。
- crash 殘留只能依 diff/任務證據精準接手或清理，不允許全域 `git reset --hard` / `git clean -fd`
  刪除未知工作。

### 10.3 merge-sync.md

任務卡要求：

- 整合確切 `INTEGRATION_TIP`，使它成為 HEAD 祖先。
- 先檢查/收拾半成品，保留兩邊行為，實際跑 validate。
- 不得修改 integration ref、切換 branch、push、改寫既有 track history或操作其他 worktree。
- 建議 `git merge --no-commit <tip>`，但允許 agent自行建立必要整合/修復 commit。
- 有修改就不 done；無法完成可 issue 後結束，本輪資訊自動交給下一 agent，不直接要求人工。

### 10.4 merge-confirm.md

- 注入本軌所有 task 全文與 DoD，不使用 80 字截斷版。
- 實跑每條 DoD + validate。
- 發現缺陷就修、commit、停止；下一輪再獨立確認。
- 全部通過且無異動才 `done merge-main`。
- integration validate rollback 的輸出透過 NOTES 注入，且是權威失敗證據；integration-only
  invariant 在 child 無法本地重現屬預期，agent 仍須依錯誤內容修復，不得把本地 PASS 當成完成或轉成人工 gate。

### 10.5 Placeholder

| 模板 | 新增 placeholder |
|---|---|
| exec.md | TRACK_CONTEXT、TASK_TAG |
| merge-sync.md | TRACK_NAME、MERGE_TARGET、INTEGRATION_TIP、TRACK_TASKS_FULL |
| merge-confirm.md | TRACK_NAME、MERGE_TARGET、INTEGRATION_TIP、TRACK_TASKS_FULL、REPAIR_CONTEXT、ISSUE_CMD |

所有模板測試檔案存在、placeholder 完整替換且無殘留。外部 plan generator 契約同步更新。
外部 generator 同樣限制在需求/context/ref 明列路徑內針對性取證，只有跨 task 才要求穩定 ID；
未知命令改寫為可觀測結果與既有 script 的限定查找，不因命令名稱未知自動建立 human gate。

---

## 11. Dashboard、CLI 與觀測

### 11.1 CLI

```text
python -m engine.fleet
  --repo <integration-worktree>
  --name <parent>
  --integration-branch <optional; default=current branch>
  --max-parallel 4
  --merge-threshold 2
  --pause-after-plan            # default off
  --max-child-restarts 0        # 0=unlimited
```

Preflight 取得 current full ref；detached HEAD、integration branch 被別的 worktree checkout、
dirty、goal 未 commit、validate 紅、submodule repo 都明確拒絕。

### 11.2 Dashboard ownership

- Launcher 加並行模式與 max-parallel；並行模式 spawn `engine.fleet`。
- parent 顯示 fleet phase、queue、各 track 狀態與整體控制。
- child 顯示 loop round、task、merge stage、issues、logs，但 mutation 預設隱藏。
- parent 的 Run/Stop/Delete 分別對應 fleet resume、graceful cascade stop、群組安全刪除。
- parent 只有在選配 `pause-after-plan` 的 `awaiting-approval` 階段可編輯完整 master plan；split
  後不提供 fleet-managed plan edit、phase、set-task、reset/import 或 archive/restore。
- 所有 parent mutation 都帶 immutable run-id；plan edit 另帶 plan generation，避免舊畫面操作
  同名重建後的新 run 或覆蓋更新後的 plan。
- 「以此為範本」剝除 run-id、fleet-parent、merge target 等執行期欄位。

### 11.3 Track 狀態

| 狀態 | 來源 |
|---|---|
| 執行中 task-N | child state exec |
| 併入 integration | child merge/sync |
| 整合確認 done x/2 | child merge/confirm |
| 待合併 | child merge-ready + queue position |
| CAS / integration validate | fleet merge_tx |
| integration validate 失敗，回送修復 | fleet rolled-back + child control |
| 已合併 | fleet track merged |
| 自動重啟/backoff | fleet restart metadata |
| 結構錯誤停機 | fleet/child failed reason |

Dashboard 以 fleet track 狀態覆蓋已停止 child 的舊 merge-ready 投影。status CLI、Dashboard
PID 判斷需同時認得 `engine.loop` 與 `engine.fleet`，父 health 不套用 round stall heuristic。

history.log merge round 增加 `stage=sync|confirm`；missing-DONE 判定：sync 有有效變更不要求
done，confirm 才以 done 作為完成訊號。

### 11.4 通知

child `notify_cmd` 一律空白；fleet 統一發：

- `track_merged`
- `track_repairing`
- `fleet_completed`
- `fleet_stopped`
- `fleet_failed`

不為每次 agent/no-progress retry 發終態通知，避免轟炸。

### 11.5 UI 既有功能逐元件盤點

現有 `FleetOverview` 的「Fleet」代表全域所有 workspace；新 `engine.fleet` 則代表單次並行
run。前端命名不可混用：保留 `FleetOverview` 作全域頁，單次並行群組使用
`ParallelRunGroup` / `ParallelRunState`。API 可以沿用 `/api/fleet-state`，但 TypeScript model
與 view component 必須區分 global fleet metrics 與單次 parallel run truth。

| 現有 UI/資料層 | v6 調整 |
|---|---|
| `App.tsx` / workspace navigation | parent 是群組入口；child 仍可開詳情，但 breadcrumb 顯示所屬 parent/track；parent 被刪除後清掉所有 child tab/selection；Dashboard 啟動不提供唯讀模式，所有 mutation 仍以 workspace kind、run/generation 與執行中 operation gate 控制 |
| `useDashboardData.ts` | 合併 workspace SSE 與 parallel-run state；處理 parent/child 晚到、刪除與 stale generation；REST refresh 與 SSE callback 都必須核對目前 selection、identity 與 projection epoch，不得用舊 response/event 覆蓋新 run |
| `shared/api/types.ts` | 分開 `FleetHealth`（全域）、`ParallelRunState`（單次）、`WorkspaceKind`、`MergeTransaction`、`TrackState`；Phase 納入 merge/merge-ready |
| `LauncherModal` | 並行開關、max-parallel、選配 pause/retry cap；並行模式停用「另開 loop branch」等衝突選項；diff preview 顯示 integration ref、預估 track/worktree；validate/preflight/launch 共用全域 operation gate，進行中禁止重送、關閉、切 tab 或啟動另一個 mutation |
| `PlanImportField` / `planValidation.ts` | plan v2 track hard schema、scope 選填；多 track plan 自動路由 fleet；舊格式顯示刪除/重建提示 |
| `PlanTable` / `PlanEditorModal` | planning/選配 approval 前可編輯 master 的 track/scope；splitting 後 parent/child 全唯讀；不顯示跨軌拖曳或 done-count 人工修改 |
| `WorkspaceView` | standalone 保留現有操作；parent 顯示 Run/Stop/Delete/queue/REPORT；child 隱藏 run/edit/phase/set-task/reset/import/archive/stop，只提供唯讀 logs/issues/prompt；所有停止統一由 parent 管理 |
| `WorkspaceTabs` | child tab 顯示 track 與 merge stage；只在 parent kind、`fleet_parent`、`fleet_run_id` 全部相符，且 child workspace/track 存在 parent authoritative track mapping 時折疊在 parent 下；未登記 child 保持 orphan 可見；parent 完成後不把已清理 child 當失聯錯誤 |
| `FleetOverview` / `FleetWorkspaceCard` | parent 卡顯示群組聚合與 attention；child 預設收在 exact-run 群組內，避免平鋪 N+1 張卡；可展開查看各軌；批次選取當下凍結 name/kind/generation/run-id/PID，identity replacement 必須清除選取並要求重選 |
| `fleetViewModel.ts` | 全域 workspace/task 統計只計 standalone + fleet-parent 聚合，只排除 exact-run 且由 parent authoritative track mapping 登記的 child；mismatch-run、未登記或 orphan child 保持可見且其 error 不得從 health 消失；child error/issue 向正確 parent attention 彙總 |
| `useStatusFavicon.ts` | 任一 running parallel run 視為 running；rollback/repairing 視為 warning；完成 parent 不受已清理 child 影響 |
| `ConfigModal` | parent 可改下一次 resume 使用的營運參數；執行中的交易參數唯讀；child config 全唯讀且不暴露 fleet-only 複製值；目前 agent 命令若被 CLI 管理器移除，欄位維持未選並要求使用者明選，不得靜默 fallback 到第一項；送出 mutation 後整個表單與 nested manager 入口鎖定，不能讓延遲 response 吃掉後續輸入 |
| `ConsolePane` | parent console 顯示 fleet/supervisor/CAS/rollback；child console 保留 agent round；不把所有 child stdout 混入 parent 造成無界輸出 |
| `HistoryModal` / `TimelineModal` / `RoundSparkline` | child 顯示 exec/sync/confirm agent round；parent 的既有 history/timeline 只標示 planning round與 Dashboard 操作，fleet phase/CAS/rollback 由 `ParallelRunGroup` 的 bounded phase/merge history 顯示；不把 parent planning sample 假裝成 child aggregate |
| `IssuesModal` | parent 只聚合 exact kind/parent/run/track 的 child 未讀數並標 track；點擊只導向同一 run 的 child；同名 replacement 不可借用或污染舊 run 診斷；已清理 child 的 issue 摘要保留在 parent REPORT/fleet history |
| `PromptModal` | child 顯示 round prompt；parent 只在 planning/awaiting-approval 顯示明確標名的 planning prompt，splitting 後隱藏，不虛構目前 prompt |
| `ReportModal` | parent 顯示全域 order、各 track branch/SHA、rollback/retry 摘要與 @final；child report 只作診斷，不當成 fleet 完成報告 |
| `GoalModal` | parent 從 integration worktree 讀 goal；child 顯示唯讀快照並標示來源，不提供改 goal |
| `RunCompareModal` | standalone 與 child 沿用各自 loop round；parent 隱藏，因本版刪除後重跑、不保留可比較的 previous fleet aggregate，避免混用 planning/child round 指標 |
| `CommandPalette` / 批次操作 | fleet-child mutation 跳過並說明「由 parent 管理」；對 parent 的 stop/delete 呼叫群組 API，不逐 child 拼裝非交易批次 |
| `LauncherJobs` | job 類型明確顯示 loop/fleet；row/action/message identity 至少包含 name+PID，已有 state 時再含 generation/run-id；poll 與 stop 後 refresh 共用 monotonic response revision，舊 GET 不得覆蓋較新的 stopped/replacement 結果；fleet stop 帶目前 PID 對應的 immutable run-id，啟動期尚未落 state 才以 job process 身分停止；完成狀態以 workspace/fleet UI 為真相，不把 process rc 當完成報告 |
| `CliManagerModal` / `NotifyModal` / `RepoRootsModal` | mutation pending 時整個可編輯 fieldset、nested action 與關閉入口鎖定；request 完成後才恢復，避免已送出的舊快照與畫面最後輸入不一致 |
| `ArchivesModal` | v5 移除入口、route 與按鈕；既有 `.archive` 僅在升級說明提示，不在 UI 自動搬移或刪除 |
| `ActionDialog` / stale guard | stop/delete/選配 approve 顯示 run-id、track 數、worktree 清理範圍；所有 parent mutation 帶 run-id，plan/approve 再帶 generation/hash 防 stale 操作 |
| styles / badges | 新增 sync、confirm、queued、CAS、validating、rollback、repairing、merged、failed；顏色語意與全域 health 一致 |

全域 Overview 去重規則是資料契約而非純視覺：parent 的 `tasks_total/tasks_completed` 由
fleet order map 聚合；child 只在 group detail 中計數，避免 workspace/task/attention 重複。
全域「輪次效能」與事件 feed 仍可包含 child 的真 agent round，因 parent 並沒有等價 aggregate
round；資料以 child workspace 名標示來源，不把它再加進 workspace/task group 數。

### 11.6 UI/API 控制矩陣

| 動作 | standalone | fleet-parent | fleet-child |
|---|---|---|---|
| Run/resume | 現有 loop | resume fleet | 拒絕；由 parent 管理 |
| Graceful stop | 現有單 loop | 停派發後級聯 child | 拒絕；一律由 parent 管理 |
| Edit plan | 停止時依現有規則 | 僅 planning/awaiting-approval | 拒絕 |
| Change phase / set task | 現有規則 | 拒絕；重新規劃需刪除重建 | 拒絕 |
| Reset/import | 現有規則 | 拒絕既有 run；新 run 從 Launcher 建立 | 拒絕 |
| Delete | 安全刪單 workspace | 群組 stop→remove worktree→刪 children→刪 parent | 拒絕單獨一般刪除 |
| Archive/restore | v5 移除 | v5 移除 | v5 移除 |
| Copy as template | 保留 standalone 設定 | 保留使用者輸入設定，剝除 run/runtime 欄位 | 隱藏或導向 parent |

---

## 12. 自動恢復與少數人工終態

### 12.1 自動處理

- agent 非零、timeout、未回 done。
- merge conflict、merge abort、issue、驗證紅、無進展。
- integration ref 因其他合法 track 前進而導致 candidate 過期。
- integration validate 失敗且 rollback 成功。
- child coordinator 非預期退出。
- fleet 在已知 journal stage crash 後 resume。
- 成功 track 的 worktree/workspace 清理。

### 12.2 必須停機

- primary + checkpoint state 都損壞或 schema 不符。
- fleet.json、child state、branch/worktree/common git-dir 身分互相矛盾。
- integration ref 出現非 fleet expected/candidate 的未知 SHA。
- rollback 後 baseline validate 仍紅。
- 權限拒絕、磁碟不足、Git object/ref 損壞、工具不存在。
- tracked goal/plan-doc 被人類改變，原計畫真相已失效。
- worktree 路徑存在但不是 fleet 記錄的實體 worktree。

這些終態不靠更多 agent retry 能安全解決，停機是保護使用者資料，不是語意型品質裁決。

---

## 13. 不變量

| # | 不變量 |
|---|---|
| I1 | integration ref 只由 fleet 以 expected-old CAS 前進或 rollback |
| I2 | agent 永遠不在 integration worktree 執行 |
| I3 | 任一 child worktree同時最多一個 loop writer |
| I4 | 進 integration ref 的 candidate 必須包含 expected tip、乾淨、綠燈、done 達標 |
| I5 | integration validate 紅的 candidate 不留在 integration ref；可證明時自動 rollback |
| I6 | child state 只由 child loop 寫；fleet 只寫具 run-id/generation 的 sideband control |
| I7 | fleet.json 只由 fleet 寫；Dashboard只讀投影或寫控制請求 |
| I8 | goal、plan-doc、凍結 plan hash 在 fleet run 期間不變 |
| I9 | 拆分後 parent plan 與 child slice 唯讀，不存在雙向同步問題 |
| I10 | fleet-child 不接受 standalone 的 run/edit/phase/reset/import/archive 操作 |
| I11 | worktree registration 未安全移除前，不遞迴刪 worktree 目錄 |
| I12 | 未知外力 Git 現場不自動 reset、clean、force-delete |
| I13 | 每個 workspace root 只有一個 process-lifetime kernel-lock Dashboard；它只有在啟動清場以 durable root/repo/generation/session/process identity 確認停止 coordinator 與獨立 Agent/validator groups 後才 serve；不自動 resume，無法證明就 fail closed |

---

## 14. 影響面

| 區域 | 主要變更 |
|---|---|
| `engine/work.py` | plan v2 schema；merge done；state/control 身分核對 |
| `engine/loop.py` | state schema v2、handoff-after-plan、fleet-child merge 狀態機、sideband control、lease、merge prompt |
| `engine/fleet.py` | planning handoff、split、supervisor、queue、CAS journal/rollback、final、cleanup、REPORT |
| `engine/status.py` | fleet PID、merge/fleet projection、父 health 規則 |
| `engine/dashboard.py` | spawn fleet、workspace kind mutation gate、fleet state/control API、群組 stop/delete、移除 archive/restore/read-only mode、啟動前全 workspace 清場 |
| `engine/cli.py` | fleet entry/doctor/status routing與錯誤訊息 |
| `engine/prompts/*` | plan/exec 更新；新增 merge-sync、merge-confirm |
| `engine/prompt_templates.py` | external plan v2 契約與 placeholder drift |
| `engine/dashboard.config.shared.json` | 修正目前呼叫不存在 `npm test` 的 React validate 選項；改用實際存在的 `npm run check` / `npm run check:all` |
| `ui/src/shared/api/types.ts` | Phase、PlanTask track/scope、FleetState、MergeTransaction、WorkspaceKind |
| `ui/LauncherModal` | 並行參數；pause 預設 off；不相容舊 workspace 提示 |
| `ui/WorkspaceView` | parent 控制、child 唯讀、移除 fleet edit/archive |
| `ui/FleetOverview` / view model | parent-child grouping、queue、repair/rollback 狀態 |
| `ui/historyParser` / timeline / sparkline | merge stage 與 missing-DONE 語意 |
| README / GUIDE | agent-first 流程、刪除取代封存、外部資源並行限制、升級說明 |
| `tests/dry_run/`（新） | 完整 repo clone、隔離 config/workspace/venv、真 Codex CLI、fault injection、證據收集與 release gate orchestration |
| `ui/e2e/parallel-real-dry-run.spec.ts`（新） | production Dashboard + 真 Codex 的瀏覽器完整驗收；與快速 fake-agent E2E 分開 |
| `tests/e2e_server.py` | fake agent 輸出 plan v2；增加 parent/child/fleet fixture，但仍只負責快速 L3，不冒充真 Codex gate |
| `ui/playwright.real.config.ts` / `ui/package.json`（新/改） | 長跑 real-dry-run 使用動態 server URL、workers=1、retries=0、獨立 timeout/artifacts；新增 `test:dry-run`，不併入一般 60 秒 spec |
| `.gitignore` | 忽略本機 dry-run artifacts、trace、video、完整 console/state snapshot，避免證據誤入版控 |

production UI build 產物 `engine/ui/` 必須在 UI 修改後重建並通過 offline asset 檢查。

---

## 15. 測試計畫

### 15.1 Plan/state contract

- track 合法/非法名稱，特別是 `.`、`..`、leading dot、過長、`@final`。
- scope 選填、型別錯誤、未知欄位、track 上限。
- state schema v2；空 plan 的舊 state 也必須被拒絕。
- standalone/multi-track/final-only 入口路由。

### 15.2 Worktree 與 supervisor

- planning handoff 尚未產生 `fleet.json` 完整 plan 前收到 SIGINT/UI stop：必須先有可 resume journal、
  parent loop PID 清空、phase 顯示 stopped；resume 沿用同一 run-id 繼續 planning，不得誤進 cleaning/done。
- N track 建立、部分建立 crash、resume 冪等、branch/path 不符停機。
- child crash 自動重啟；人工 stop token 不重啟。
- parent graceful stop 級聯；parent kernel flock lease 釋放後 child 完成本輪停止。
- max-parallel 排程與 final 延後建立。
- submodule repo preflight 拒絕。

### 15.3 Child merge

- clean merge、真衝突、agent 修復 commit、issue/no-progress、red reset。
- track 無獨有 commit時的 fast-forward整合，不依賴第二 parent。
- merge-ready 後 integration tip 前進，自動回 sync。
- integration validate repair control，自動回 confirm。
- completed/green anchor 被改寫時 fail-closed。

### 15.4 CAS transaction crash matrix

- prepared 前/後 crash。
- update-ref 成功但 journal 未更新。
- ref-updated 後、worktree reset 前 crash。
- validating 中 crash。
- validate FAIL、rollback 前/後 crash。
- rollback 後 baseline FAIL。
- 外力把 ref 移到第三個 SHA。
- integration worktree 出現未知 dirty 狀態。

每個案例同時核對 ref、HEAD/index/worktree、fleet checkpoint、child status與可再次 resume。

### 15.5 Dashboard/API

- child 所有 standalone mutation 被拒絕。
- parent run/stop/delete 操作正確傳播。
- parent edit-config、awaiting-approval plan edit與所有 mutation 的 stale run-id/generation 被拒絕。
- Jobs 分頁正確標示 loop/fleet，且 fleet stop 只使用與目前 process PID 相符的 run-id。
- Dashboard 重開先取得 workspace-root singleton kernel lock，再做 startup sweep；第二個不同 port
  instance 不得讀設定、signal 或 bind。Sweep 去重 standalone、fleet supervisor、planning loop、child
  與 coordinator hard-crash 後 reparent 的 Agent/validator group；新 fleet 在 parent 建立前使用 root
  `.ops` pending identity，reset/import 明示 old→pending generation。Agent/validator 由 gate wrapper 在
  durable marker fsync 後才放行。所有停止依 root/repo/generation/session、PID/PGID、start-time、command
  與已凍結 member identity 重驗；leader 缺失或 numeric PGID/SID 可能重用時 fail closed，不猜測 signal。
  驗證 PID/PGID reuse、同名不同 root、marker replacement、corrupt truth、root scan failure、markerless
  Job startup window 與 shutdown root sweep failure；任何無法確認或仍存活時 Dashboard 不 bind/serve，
  成功後不自動 resume。
- 選配 approval token 綁定 plan hash，舊 token 無效。
- fleet PID/running/health/status/SSE 投影。
- archive/restore 路由與 UI 已移除，既有 `.archive` 不被自動刪除。
- legacy v1 standalone 只顯示 delete-only UI；run/edit/config/template 全隱藏且 API 繼續拒絕。
  驗證 live legacy PID 拒刪、corrupt v2 不誤降級、marker→journal crash 可重試，以及 inode reuse
  的同名 replacement 與新 job 都保留並要求 fresh confirmation。

### 15.6 外部資源

Worktree 不隔離外部服務。加入 fixture 或測試專案驗證：

- 兩個 validate 搶同一 port/DB/schema 時能顯示明確失敗並重試。
- Fleet 固定注入 `LOOP_TRACK_NAME/SAFE_NAME/INDEX/PORT`，並為每 track 分離 `TMPDIR`、
  `XDG_CACHE_HOME`、`npm_config_cache`。`--track-port-base 0` 逐 track 動態配置唯一 loopback
  port；非 0 時使用 `base + index - 1` 並在建立 workspace 前驗證範圍。
- `--track-env-json` 只接受大寫 env 名稱與 `{track}`、`{safe_track}`、`{index}`、`{port}`
  placeholder；不得覆蓋 coordinator/runtime 變數。因 config、fleet state與 evidence 會保留
  env template，名稱含 token/password/secret/credential/key 的欄位直接拒絕；此入口不得放 secrets。
- port 數字是協調契約，不是長期佔用 socket；child 服務必須實際綁定 `LOOP_TRACK_PORT`，
  遇外部 process 搶占要明確失敗，由 agent 在本軌處理。
- 無法證明 validate 可並行安全時，使用者可設 `--max-parallel 1`，功能仍正確。

### 15.7 回歸

- 現有 standalone 全測試保持綠。
- Python unit/integration、UI lint/build/offline、Playwright 依 repo 實際命令全綠。
- Prompt placeholder、保護檔案、state tamper、round token、reset、notify 回歸。

### 15.8 測試層級與最終 gate

測試分四層，前一層通過才能進下一層；L1–L3 都不能取代 L4：

| 層級 | Agent/目標 | 用途 | 是否 release gate |
|---|---|---|---|
| L1 unit | 不啟動 agent；mock Git/process/clock | schema、state transition、CAS/recovery 純邏輯 | 必須通過，但不充分 |
| L2 integration | scripted fake agent + 暫存小 repo | 故障注入、所有 crash point、快速重跑 | 必須通過，但不充分 |
| L3 production E2E | fake agent + production Dashboard + Playwright | API/SSE/UI mutation、瀏覽器回歸 | 必須通過，但不充分 |
| **L4 full-project local dry run** | **完整 loop-agent-lite clone + 真 Codex CLI + production Dashboard** | 驗證真 agent、真 repo、真 build/test、真 UI 長流程 | **唯一最終過關 gate** |

功能不得因 unit、fake-agent E2E 或人工閱讀通過就宣告完成。Release candidate 必須在本機對
**完整專案**完成 L4，且保存可追溯證據。任何需要直接改 state、手動 merge、手動修 code、
跳過驗證或改用 fake agent 才完成的 L4，一律判定失敗，修正後從乾淨 clone 重跑。

### 15.9 真 Codex CLI 契約

目前本機 `dashboard.config.local.json` 中 label=`codex` 的已設定命令為：

```text
codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.4
```

L4 規則：

1. Orchestrator 從個人設定讀取 label=`codex`，以現有 `norm_cmd`/shlex 規則正規化；不把
   個人設定整份複製進 artifacts。
2. 啟動前保存 `command -v codex`、`codex --version`、正規化命令與 model 名；不得記錄
   token、完整環境變數或 credential path。
3. 真 dry run 的 planning/exec/merge/confirm/final 每一輪都使用該 Codex 命令；不得局部
   改用 fake agent、Claude 或另一 model。
4. Codex 未安裝、版本命令失敗、認證/網路/權限失敗都視為 L4 未通過；不可 fallback 後
   宣稱成功。
5. 測試 harness 只控制隔離 repo、goal、驗證器與 fault injection，不替 Codex 寫 commit、
   回 done 或解 conflict。

未來使用者若更新個人 Codex 設定，L4 使用執行當下 label=`codex` 的值並把版本證據寫入
manifest；本文件中的 `gpt-5.4` 是此次已確認基線，不在程式中另寫第二份預設。

### 15.10 完整專案 dry-run 環境

不得在開發者目前 checkout 或其真 workspace 上做破壞性測試。`tests/dry_run/run_full_project.py`
負責建立：

```text
<temp-root>/
  source/       # git clone --no-hardlinks，固定 release candidate SHA
  workspace/    # LOOP_AGENT_WORKSPACE_ROOT
  home/         # LOOP_AGENT_HOME；只放本次隔離狀態
  venv/         # pip install -e source，從 public `loop` entrypoint 啟動
  config.json   # 只含本次 repo root、真 Codex entry、validate 與安全 defaults
  harness/      # repo 外的 immutable fault validator/control；啟動前後核對 SHA-256
  artifacts/    # manifest/log/state/git graph/screenshots/video/trace/report
```

環境準備：

1. 原 checkout 必須乾淨；記錄 release candidate SHA。L4 結束簽署前再次確認原 checkout 仍是
   同一 HEAD 且乾淨；執行期間若被其他程序改動，該次證據不可重現，直接 fail。
2. 以 `git clone --no-hardlinks` 建完整獨立 clone，避免 dry run 共用或移動原 repo refs。
3. 在 clone 設定測試 Git identity；建立並 commit 專用 `goal.md` 與必要 dry-run fixture，
   所有變更只存在 clone。
4. 建 temp venv、editable install clone；UI 執行 `npm ci`，不沿用原 checkout node_modules。
5. 以動態空閒 port 啟動 production `loop dashboard`；不得 reuse 既有 dashboard server。
6. 產生隔離 Dashboard config，只複製 label=`codex` 的命令；workspace/repo roots 指向 temp。
   這份 L4 config 將完整 validator 上限設為 15 分鐘；一般 Dashboard 的 120 秒預設不變。
   Production UI 必須斷言 Launcher 實際載入 900 秒，避免完整 Python + clean UI build 在進入
   Codex 前被過時的短 timeout 誤判失敗；UI startup wait 由此值加 30 秒推導，Launcher 一旦
   回報失敗就立即 fail，不空等長 timeout。後續 tracks、child、stop/resume、rollback、done
   等長等待也必須同時監看 Fleet terminal failure 並立即 fail。單次逾時仍 fail closed，且受
   四小時整體 gate 約束。
7. 每個 track 可注入獨立 TEMP/cache/port；integration Dashboard、Playwright fixture 與 child
   validate 不可共用固定 8876/8877。
8. 本階段不開放廣域 repo 搜尋/巡檢。Planning 只讀 goal、規劃書明列影響面與既有 plan 路徑；
   execution 只讀 task/ref/scope 直接點名的 source/test。禁止全庫列檔與掃描 `engine/ui/assets/**`、
   `ui/node_modules/**`、coverage、build、trace、video 等 generated/minified 產物；只有任務直接要求
   production artifact/source map 時才讀對應產物。資料不足時 agent 回 issue，不自行擴大讀取；
   廣域搜尋留待後續有需要再加入。若 prompt/harness 未守住此邊界，記為效率缺陷並保存耗時證據。

Child 每輪的完整專案 validate 使用無固定 server port的命令，例如：

```text
sh -c 'python3 -m unittest discover -s tests -t . -q && cd ui && npm ci --prefer-offline --no-audit --no-fund && npm run check'
```

`npm run check` 已包含 lint、TypeScript/build、offline assets，但不啟動固定 port Playwright。
每個 child worktree 都必須自行依 lockfile 執行 `npm ci`；integration clone 的 ignored
`ui/node_modules` 不會由 Git worktree 共用。各 track 使用獨立 `npm_config_cache`，不得因並行
安裝互相覆寫 cache 或誤把 integration checkout 的依賴存在視為 child 可用。
Validator subprocess 的 `LOOP_AGENT_WORKSPACE_ROOT` 與 `LOOP_AGENT_HOME` 也必須指向該 track
的隔離 `TMPDIR`；不得讓兩條 track 的完整 tests 因固定 fixture workspace 名稱互撞。
Dashboard 的「Agent CLI 執行確認」必須保留使用者 PATH/config，但在 spawn 被測 CLI 前移除
`LOOP_WS`、`LOOP_ROUND_TOKEN` 與全部 `LOOP_FLEET_*`；CLI smoke test 不得誤繼承目前 child
的 task/track 身分，否則 nested E2E 會測到 coordinator context 而非固定 prompt=`test`。
L4 subprocess 也不得整包繼承宿主 credential/token/password/secret 類 env；真 Codex 應使用既有
個人 config/keychain 認證。Harness 只在記憶體保留被移除之敏感 env 的值，用於 artifacts
exact-value 防漏掃描，不把變數名稱或值寫入 manifest。敏感名稱按 `_` 分段判定；例如
`SECRET_KEY_BASE`、`CLIENT_SECRET_JSON` 必須移除，但不得因一般名稱含相同字母片段而誤判
`MONKEY`、`KEYBOARD_LAYOUT`。
本 repo 的 production UI 產物 `engine/ui/` 受版控；frontend track 的 task/DoD 必須包含
`npm run build` 後提交對應 assets。每次 validate 結束都再次執行 `git status --porcelain`，
若 build 才產生未提交 asset，該輪不能算 done；不得把受版控 build output 誤當 ignored 產物。
全部 track 與 `@final` 合入後，再在最終 integration tree 單獨執行完整 release validation：

```text
python3 -m unittest discover -s tests -t . -q
cd ui && npm run check:all
```

shared config 的「react build+test+e2e」必須指向實際存在的 `npm run check:all`；L4 不得
使用不存在的 script 或以忽略錯誤通過。

### 15.11 L4 必跑場景

每個 release candidate 至少完成下列兩次獨立 full clone run；兩次都用真 Codex：

#### DR-1：自動規劃 + UI stop/resume + happy path

- Goal 同時包含可獨立的 backend、frontend 工作及跨軌 `@final` 驗收，實際修改本專案程式與測試。
- Gate 以每條 track 已 validated CAS 的 `expected_sha..candidate_sha` diff 歸屬交付：不同一般 track
  必須分別產生 `engine/`+`tests/` 與 `ui/`+受版控 `engine/ui/` production assets；另以
  `fixture_sha..final_sha` 確認上述改動都實際進入最終 integration，不以 plan 關鍵字代替證據。
- 不匯入預製 plan；由 Codex planning rounds 產生 track。
- 使用 shipped convergence defaults：flag threshold 10、exec done 3、merge done 2、
  max-parallel 4；不得為省時間降成 1。
- 從瀏覽器 Launcher 選擇完整 clone、label=`codex`、並行模式與實際 validate 命令後啟動。
- child 執行中從 parent UI 要求 graceful stop，確認全部 child 完成本輪後停止；再由 parent
  UI resume，不能透過 terminal 直接啟動 child。
- 等待一般 tracks、`@final`、integration validate、REPORT、child cleanup 全部完成。

#### DR-2：真 conflict + CAS rollback + agent repair

- 使用完整 clone與一份受版控的 adversarial plan/fixture；刻意讓兩條 track 修改同一測試
  contract，確保至少發生一次真 Git conflict。此場景允許從 UI 匯入 plan，因目的為固定
  故障路徑；不可由 harness 直接寫 parent state。
- Test-only integration validator 在 candidate 第一次進 integration worktree 時注入可重現、
  有明確錯誤訊息的 integration-only invariant；child 原驗證仍跑完整 backend + UI check。
- 必須觀察 CAS 前進、integration validate FAIL、CAS rollback、baseline validate PASS、
  repair control 送回原 child、Codex 實際產生修復 commit、再次 done/merge-ready/CAS PASS。
- Fault injection 只建立失敗條件，不得自行移除 invariant、替 agent commit 或直接把第二次
  驗證改成無條件成功。
- Repair control 必須明示 integration-only validator 在 child worktree 無法重現是預期行為；
  integration failure tail 是已 rollback 的權威證據，Agent 不得因 child local PASS 忽略修復。
- 不為測試 Issues UI 強迫 agent 先回報假 issue 或多跑一輪。Parent Issues 的 DR-2 證據直接來自
  真實 integration validate failure／rollback diagnostic，保留錯誤摘要、track、attempt 與修復導向。
- Fault validator 位於 target repo 外的 `harness/`，啟動前後核對 SHA-256；被 agent、測試或
  其他 process 修改即判 L4 失敗，避免透過改測試器假裝修好產品。

另外，CAS 各 crash point仍由 L2 scripted agent完整覆蓋；不要求用真 Codex 重跑每個
process-kill 時序，避免把非語意 fault matrix 變成昂貴且不穩定的模型測試。

### 15.12 前端實際 dry-run 完整驗收

新增獨立 Playwright project/spec `parallel-real-dry-run.spec.ts`。它連到上述 production
Dashboard 與完整 clone，**所有改變狀態的動作都透過畫面點擊/輸入**；允許 API/檔案只作
唯讀取證，不可用直接 POST 取代 UI 操作。L4 從啟動、長流程驗收到最終刪除全程使用同一個
可寫 production Dashboard，不另啟第二個 Dashboard 或切換 base URL。

必驗收：

- Launcher 正確顯示 integration ref、Codex CLI、parallel/max-parallel、validate 與 diff preview。
- 啟動後 parent/child grouping 正確；全域 task/workspace/attention 不重複計數。
- child 隱藏 edit/phase/set-task/reset/import/archive；parent 控制可用。
- planning、exec、sync、confirm、queued、CAS、validating、rollback、repairing、merged、
  final、cleaning、done 狀態依真 state/SSE 依序出現，頁面 reload/SSE reconnect 後不倒退。
- parent graceful stop/resume；child 不被誤判 crash，也不被 supervisor 提前重啟。
- DR-2 rollback/repair UI 顯示正確 track、attempt、錯誤摘要，不能短暫顯示已完成。
- Console 分流、Issues 聚合與導向、Prompt、History、Timeline、Report 都能開啟且內容屬於
  正確 parent/track；DR-2 Issues 必須顯示真 integration rollback failure，不使用人造 issue 標記；
  清理 child 後 parent REPORT/歷史仍可讀。
- FleetOverview、WorkspaceTabs、搜尋/篩選/排序、status favicon、CommandPalette、copy template
  不因 parent-child model 產生重複、失聯或錯誤 mutation。
- run 完成並保存 parent/track 證據後，仍在同一個 production Dashboard 從 UI 執行最終
  parent Delete 群組安全刪除；確認 Git worktree registration、child/parent workspace 與 tab
  全部消失，保留 branch 政策符合文件。
- 桌面主要 viewport 與至少一個窄 viewport 無遮擋/溢位；長 track 名、長 issue、長 console
  tail 不破版。

Real dry-run Playwright 使用 `workers=1`、`retries=0`；一般 UI assertion 使用短 timeout，
只有明確的 planning/track/stop/rollback/done 長流程使用 bounded budget，且等待期間同步監看
terminal Fleet failure 並立即 fail。整體最多 4 小時；失敗不得靠自動 retry 掩蓋，也不能因
一般 E2E 的 60 秒預設而誤殺正常 Codex 收斂。保留 trace、每個關鍵狀態 screenshot、video 與
browser console/network error。快速 `dashboard-flow.spec.ts` 繼續使用 fake agent，不與長跑
spec 混在一般 CI timeout。

Child prompt 會在 agent round 開始前落地，但 History 只會在第一輪完成後出現；真 UI 驗收
不得把「prompt 已可讀」誤當成「history 已落地」。第一輪 History 使用 agent 30 分鐘加完整
validator 15 分鐘與 60 秒緩衝的 bounded wait；History modal 是 snapshot，等待期間必須從畫面
反覆按「重新整理」，不能只延長靜態 assertion。停留 child detail 時以唯讀 parent state 同步
監看 terminal Fleet failure；不得用一般 30 秒 assertion 形成時序競爭。

### 15.13 證據與通過標準

每次 L4 產生 `artifacts/manifest.json`，至少包含：

- source SHA、開始/結束時間、OS/Python/Node/npm/Git/Codex 版本。
- 正規化 Codex command/model、測試場景、thresholds、dynamic ports；不含 secrets。
- parent/child workspace/run-id/track mapping。
- 各 track branch、candidate、CAS expected/new、rollback與最終 integration SHA。
- 每階段耗時、agent round count、restart/no-progress/repair次數。
- Python/UI/Playwright最終命令、exit code與 log path。
- REPORT、bounded console/history/state/fleet checkpoint snapshot、`git log --graph`、
  `git worktree list --porcelain`、`git status --porcelain`、`git fsck --full` 結果。
- Playwright trace/video/screenshot index。
- 敏感宿主 env 不寫入 artifacts；簽署前以其實際值 exact-match 掃描所有文字 artifacts 與 zip
  entry。命中時先移除受污染檔案，再以只含 artifact/entry 路徑的錯誤 fail，不得在錯誤或 manifest
  重印敏感值。

L4 通過必須全部成立：

1. DR-1、DR-2 都由設定好的真 Codex 完成，過程沒有人工 state/code/Git 介入。
2. 最終 integration tree乾淨，`git fsck --full`、完整 Python tests、`npm run check:all` 全綠。
3. 所有 candidate 都經 CAS + integration validate；DR-2 rollback證據完整且 ref 無遺失 commit。
4. 一般 track與 final 都 merged；成功 child worktree/workspace 已清理，parent REPORT 完整。
5. UI 所有必驗收項目通過，browser 無未解釋 console error、page error或失敗 network request。
   macOS/Chromium 長流程暫停 network I/O 時可能只在 console 產生精確的
   `net::ERR_NETWORK_IO_SUSPENDED`；若 SSE 之後自動恢復、最終狀態與 REPORT 仍可由 UI 讀取，
   可記為已解釋的 browser transient。不得以模糊字串過濾其他 network/console error。
6. artifacts 可由 source SHA + manifest 重現，且未包含 credential/token/完整個人 config。

任一條不成立即 release gate 失敗。修正後必須建立新的 clean full clone，完整重跑受影響場景；
不可在失敗 clone 上手動修到綠後補簽。

---

## 16. 分期路線圖

不再建立「人工合併」產品階段；各期以自動路徑為目標。

| 期 | 內容 | DoD | 估時 |
|---|---|---|---|
| P0 Breaking foundation | plan v2、state schema v2、workspace kind、prompt 契約、移除 archive/restore、standalone 回歸 | 舊 workspace 明確拒絕；單軌完整流程全綠；安全刪除可用 | 3–5 天 |
| P1 Fleet backend E2E | planning handoff、worktree/supervisor、child merge、merge-ready reopen、CAS journal/rollback、`@final`、cleanup | scripted fault matrix 全綠；另以完整 clone + 真 Codex 完成 CLI-only full-project smoke，無人工介入（DR-1 UI gate 留 P2） | 10–15 天 |
| P2 Dashboard/operations | parent-child grouping、fleet control API/SSE、stop/resume/delete、repair 狀態、通知 | production Dashboard + 真 Codex 完成 DR-1 瀏覽器 stop/resume/happy path；逐元件 UI 矩陣通過 | 6–10 天 |
| P3 Hardening / release gate | crash matrix、DR-2 conflict/rollback/repair、外部資源 env、磁碟/效能、選配 pause/retry caps、UX 打磨 | L1–L3 全綠，L4 DR-1/DR-2 clean full clone、真 Codex、實際前端 dry run與證據 manifest 全部通過 | 7–12 天 |

總估時約 26–42 個工作天。P1 是第一個可實際試用的自動化 vertical slice；P0 只是 breaking
foundation，不把人工合併包裝成正式 MVP。

---

## 17. 風險與對策

| 風險 | 對策 |
|---|---|
| 弱模型反覆解不好衝突 | 無狀態多輪、完整 DoD、issue context、綠點 reset；選配 retry cap |
| 拆軌錯誤造成衝突風暴 | Prompt 保守拆軌；agent 在 merge round 自行收斂；不以 scope hard gate |
| integration validate 環境耦合 | CAS journal、rollback、錯誤回送 child、自動再修 |
| fleet crash | 每個副作用前 intent、checkpoint、完整 crash matrix |
| agent 誤動 shared ref | expected SHA CAS 與 ref 監看；未知 ref 停機，不 reset 人類現場 |
| 外部服務/port/cache 互撞 | per-track env、max-parallel、文件化並行安全要求 |
| 磁碟成長 | max-parallel、成功自動移除 child checkout、失敗保留供診斷 |
| 自動 retry 成本失控 | UI 顯示輪次/成本；stuck-stop與 retry cap 均為選配，不預設打斷收斂 |
| 廣域巡檢讀入過多 repo/產物資料 | 本階段只允許 task/ref/scope 的針對性讀取，禁止 generated/minified 掃描；資料不足由 agent issue，不靜默擴張 |

---

## 18. 已定案決議

| # | 決議 |
|---|---|
| D1 | pause-after-plan 預設 off；不提供 pause-before-merge，merge transaction 自動收斂 |
| D2 | exec done threshold 維持 3；merge threshold 預設 2 |
| D3 | track 上限 8；max-parallel 預設 4 |
| D4 | scope 選填且只作提示，不做 overlap gate |
| D5 | integration branch 預設為啟動時 current branch，不寫死 main |
| D6 | 合入使用 `git update-ref new old` CAS + journal；integration validate 紅自動 rollback |
| D7 | 移除「merge commit 第二 parent」機械檢查，只保留祖先/綠燈/乾淨/done gate |
| D8 | child crash 預設自動重啟；人工 stop 以 token 區分 |
| D9 | 語意型 retry 預設不限；營運 cap 選配 |
| D10 | `@final` 在隔離 final worktree 執行並走同一 gate |
| D11 | 拆分後 plan/slice 唯讀，不支援 fleet-managed 人工跨軌編輯 |
| D12 | 移除 archive/restore；舊 workspace 不遷移、不 resume，改安全刪除 |
| D13 | 成功自動清 child worktree/workspace、保留 branch與 parent REPORT |
| D14 | submodule repo 第一版明確不支援 |
| D15 | Unit/fake-agent E2E 不足以過關；必須用完整本機 clone、設定好的 Codex CLI 與 production Dashboard 完成 L4 DR-1/DR-2 |
| D16 | 前端最終驗收必須從真 UI 操作完整流程並保存 trace/video/screenshots；直接 API 驅動不能取代 UI dry run |
| D17 | 本階段不開放 agent 廣域 repo 搜尋/巡檢；只允許 task/ref/scope 內針對性讀取，搜尋能力有實際需要再加入 |
| D18 | 移除 Dashboard read-only instance；L3/L4 都使用單一可操作 production Dashboard，唯讀觀測保留在 `loop status` 與 GET API |
| D19 | Dashboard 以 workspace-root singleton lease 串行化整個 instance 生命週期；每次啟動先依 durable coordinator/pending/runtime identity 停止 standalone/fleet/planning/child 與獨立 Agent/validator groups，無法確認清場或 identity ambiguous 就不 serve，後續只能手動啟動 |

---

## 附錄 A：兩軌衝突與 rollback 時序

```text
t0 integration=M0；A/B 從 M0 開跑
t1 A merge-ready(A1)
t2 CAS integration M0→A1；integration validate PASS；A merged
t3 B 發現 A1 不是 B HEAD 祖先
   → merge-sync agent 整合 A1、解衝突、validate、commit B2
   → confirm agent ×2 → merge-ready(B2)
t4 CAS integration A1→B2；integration validate FAIL
   → CAS rollback B2→A1；baseline validate PASS
   → repair control(error tail) 送 B；B 自行回 confirm
t5 B agent 修復環境耦合、commit B3；confirm ×2
t6 CAS integration A1→B3；integration validate PASS；B merged
t7 從 B3 建 final worktree；執行 @final；CAS + validate
t8 聚合 REPORT；清除 A/B/final worktree與 child workspace
```

## 附錄 B：文件銜接

- README 移除「不自動拆任務/合併」舊敘述，新增 agent-first fleet 流程。
- GUIDE 說明適合/不適合並行的 goal、外部資源隔離、submodule 限制。
- 升級說明明確寫：舊 plan/state/archive 不支援新版 resume；不自動刪既有資料。
