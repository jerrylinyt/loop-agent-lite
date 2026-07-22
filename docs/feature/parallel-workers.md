# 規劃書：Worker Agent 並行執行（stack + worktree + supervisor gate）

狀態：Implementation v1（多輪 design/implementation review + GitHub 外部實作對照）
日期：2026-07-21

## 1. 背景、目標與邊界

目前 loop-agent-lite 的執行期是嚴格串行：單一 coordinator、單一 Git 工作樹，
`current_order` 逐條走 plan。任務即使互相獨立，也只能一條完成後才開始下一條。

本功能要在**不重寫既有 convergence engine**的前提下，讓人工確認為獨立的任務由
多個原生 `engine.loop` worker 並行執行，同時保留既有：

- done 多輪共識與 `changed → done_count 歸零`；
- protected-file tamper 偵測、green anchor、reset、red/stall/stuck-stop；
- 每輪 timeout、agent failure backoff、console/history/round log；
- 舊 plan 與普通單機 runner 的既有語意。

非目標：

- 不把多 worker 塞進 `loop.py` 的單一狀態機，也不做 in-process 多執行緒 coordinator。
- 不做跨 repo 或跨機器分散式排程。
- v1 不讓 planner 自動判斷 `stack`，不提供 Plan Editor 修改 `stack`。
- v1 不保證抵抗惡意或已被攻陷的 agent。linked worktree 共用 object DB 與 refs；
  本設計的安全邊界是「同一 OS 使用者下的合作式 agent + fail-closed 偵測」，不是 sandbox。
  若需 hostile-code isolation，應改用獨立 clone/container，非本功能擴充範圍。

## 2. MVP 使用路徑

v1 只接受**已凍結、直接從 exec 起跑**的 plan，避免同時改造規劃期與 supervisor handoff：

1. 人工準備或審核 `plan.json`，以 `stack` 標示可同批並行的連續任務。
2. CLI 以 `python -m engine.parallel start ... --import-plan <plan.json>` 啟動；或在
   Dashboard 選擇 `Parallel Loop`、貼入 plan，並選「從 exec 開始」。
3. `ParallelSupervisor` 取得 base workspace lock，再建立同process內受信任的mechanical
   `RepoExecutor`；executor持有primary Git run lock、audit durable operation lease後才做preflight與派工。
4. worker 仍跑原生 `engine.loop`；達 done threshold 時只送 gate request。supervisor event loop
   原子 claim request，真正的 primary branch transaction 由 repo executor 完成。
5. 全部 task 依 batch 整合、worker 退出且 worktree 清理完成後，base workspace 才投影為
   `phase="done"`，產生 run report 與 completed notify。

普通 Loop 在 import、state load 或 resume 讀到含 `stack` 的 frozen exec plan，都預設拒絕並
提示改用 Parallel Loop；只有明確傳入 `--allow-serial-stack` 才可忽略 `stack` 串行執行。
規劃期的 create-plan 與 plan→exec transition 一律不接受 `stack`，即使有 opt-in 也一樣，避免
不理解 stack 語意的 planner 靜默移除或重寫人工標註。Dashboard 的普通 Loop 模式同樣拒絕，
避免「格式通過但靜默串行」。

v1 不含自動 planning child。既有規劃期完全不變；使用者可先用普通 Loop +
`--pause-after-plan` 產生 plan，離線補上 `stack` 後，再從 parallel exec 入口啟動。

## 3. 架構總覽

```text
CLI / Dashboard Parallel Loop
        │
        ▼
ParallelSupervisor（base workspace 的唯一 owner）
  1. 持有 <base>/.run.lock（整個 run）
  2. 單一 event loop 寫 aggregate/base state、claim gate、監看 child exit
  3. 持有in-process RepoExecutor；後者持有primary Git run lock並負責durable operation fence
  4. 驗 plan/config/start SHA，建立 run_id 與 canonical safe sync ref
        │
        ├─────────────── 同一 batch，最多 max_parallel 個 ───────────────┐
        ▼                                                               ▼
worker task-3                                                     worker task-4
linked worktree + task branch                                    linked worktree + task branch
原生 engine.loop                                                  原生 engine.loop
done ≥ threshold                                                  done ≥ threshold
        │ gate request（無 Git side effect）                              │
        └───────────────────────┬────────────────────────────────────────┘
                                ▼
                atomic pending → claimed（線性化點）
                                │
                                ▼
              RepoExecutor durable operation/child-fence critical section
             expected-chain → exact SHA → prepared intent
             → ff-only merge → sync-ref CAS → receipt → response
```

RepoExecutor 不是agent、獨立process或第二套scheduler；它是supervisor內只執行封閉typed repo operation
的mechanical core。每個RepoExecutor外部child先經durable operation/child lease與containment barrier；managed
worker則使用獨立launch spool + child record + pre-GO authorization。OS lock與這些durable lease共同確保
child退出或被fence前沒有下一個primary writer。這個切法保留核心理念：每個worker仍由既有loop狀態機
獨立收斂；新增模組只負責資源隔離、派工、序列化整合與lifecycle，不複製
done/reset/tamper 邏輯。

## 4. 收斂與 exact-SHA 正確性

### 4.1 validated snapshot 必須在 Validate 後取得

現行 `loop.py` 先讀 `head_after/dirty`，再於 `process_exec_round` 執行 validator；validator
若寫入 tracked/untracked 檔或產生 commit，目前不會被本輪快照發現。worker 模式必須修正：

1. agent round 結束後先讀 `pre_validate_head/pre_validate_dirty`；
2. 執行 validator；
3. validator 返回後**重新讀** `post_validate_head/post_validate_dirty`；
4. 若 validator 改變 HEAD、index 或 working tree，本輪視為 `validator-side-effect`：
   `done_count` 歸零、寫 note/history，不呼叫 gate；下一輪才可對新狀態重新驗證；
5. 只有 validator rc=0、HEAD 未變且 worktree clean 時，`post_validate_head` 才是
   `validated_sha`，並可累計 done。

這項重查可共用到普通 loop，屬既有防線補強；不改變乾淨 validator 的行為。

### 4.2 gate invariant

supervisor claim 後由 RepoExecutor 在 merge critical section 內重新依序驗證：

1. run/session、task、request 與 immutable manifest 完全一致；
2. worker process/workspace/branch 都屬本 run，且
   `worker HEAD == task branch ref == validated_sha`；
3. worker index 與 working tree clean，沒有 merge/rebase/cherry-pick 進行中；
4. primary worktree clean，checked-out branch 仍是 frozen integration branch；
5. primary `HEAD == integration branch ref == safe sync ref == expected_head`；
   `expected_head` 只可由 `integration_start_sha + durable receipts` 推導；
6. `expected_head` 是 `validated_sha` 的祖先，才可 `git merge --ff-only <validated_sha>`；
7. merge 後 primary `HEAD == integration ref == validated_sha` 且 worktree clean；
8. 更新 safe sync ref、寫 receipt，再回覆 worker。

結果語意：

- 可 fast-forward：primary 變成**同一個被 configured validator 驗過的 commit/tree**。
  這不宣稱需求語意必然正確，只證明沒有把另一個未驗證 SHA 當成該輪結果。
- integration 已由別的合法 receipt 前進、因此無法 fast-forward：回 `stale-integration`，
  worker done 歸零，下一輪同步後重新跑完整 done threshold。
- primary/ref 出現 receipts 無法解釋的移動：`fatal-invariant`，整個 run 進 `blocked`，
  不把它當成一般 stale 自動修復。

在「batch 固定、integration 只由合法 gate 推進、agent 可完成同步」的前提下，K 個 worker
中單一 worker最多遇到 K-1 次 stale；agent 自身不收斂仍由既有 red/stall/stuck-stop 處理。

## 5. 詳細設計

### 5.1 plan schema 與人工 stack

`engine/work.py::validate_plan` 的 item 白名單由 `{order, task, ref}` 擴為
`{order, task, ref, stack}`：

- `stack` 可省略；存在時必須是正整數，bool 明確拒絕。
- 相同 `stack` 必須只出現在一個連續 order 區段；同值分散兩段視為錯誤。
- 相同 `stack` 的連續區段是一個 batch；未標 `stack` 的 task 自成大小 1 的 batch。
- batch 依最小 order 串行；batch 內 task 才並行。
- load/resume 時不得只驗型別：`validate_state_shape` 必須重跑完整 plan invariant，防止
  手改 state 繞過「單一連續區段」。

人工獨立性判準：同 stack 的 task 必須同時滿足：

- working set 不重疊，沒有檔案、schema、生成物或語意相依；
- validator 外部資源不衝突，例如固定 port、共用 DB/schema、Docker Compose project、
  全域 cache/lock 目錄；
- 任一條不確定就不標 stack。

`engine/prompts/plan.md` v1 **不加入 stack schema**，避免 planner 自動產生或重寫人工標註。
stack 只由 import 路徑帶入，且不論 Parallel Loop 或明確 opt-in 的普通串行 Loop，都必須直接
從 exec 起跑。

Dashboard/前端最小支援：

- `planValidation.ts` 與後端共用同一語意，驗正整數、bool、連續區段；
- `PlanTask` 增加唯讀 `stack?: number`，PlanTable 顯示 stack badge 與 batch 預覽；
- Launcher 的 Parallel Loop 範例包含 stack；普通 Loop 遇 stack 直接拒絕；
- Plan Editor 對含 stack 的 plan 隱藏/停用編輯並說明原因；後端仍以 409 拒絕，且 state
  完全不變，不能默默剝欄位。

### 5.2 `loop.py` 的窄幅 worker 擴充

新增旗標只在受管 worker 模式生效：

1. `--start-task N`：建立不可變 `assigned_order`，必須存在於 plan，且與 run manifest 一致。
   reset 後一律回 `assigned_order`；不得由 worker 恆空的 `completed` 或 `plan[0]` 推算。
2. `--stop-after-task`：gate 成功後寫入 `assignment` 終態並退出，不走全 plan 的
   `phase="done"`、RUN REPORT 或 completed notify。`phase` 保持 exec。
3. `--complete-gate-cmd <cmd>`：達 threshold 時，以環境變數傳
   `RUN_ID/TASK/REQUEST_ID/VALIDATED_SHA`。命令只是 gate IPC client，不可直接執行 Git merge。
4. `--integration-ref <safe-ref>`：只接受 supervisor 依 run_id 產生的
   `refs/heads/loop/<run_id>/integration`，供條件 prompt 注入。
5. `--managed-worker-resume`：只允許既有 `runner="parallel-worker"` state，並驗證
   run_id、assigned_order、parent workspace、repo binding、task branch 與 manifest hash。
   它可接手意外 crash 留下的 dirty 現場，不依賴一般 `--resume-interrupted` 的 round timestamp；
   不得搭配 `--import-plan` 或重建 state。
6. supervisor先建立不含state/console/`.run.lock`的canonical worker workspace骨架、發布launch
   `pending` reservation，再以不含payload PID的`guardian_ready` child record（guardian identity、argv
   hash、session）啟動`ManagedChildGuardian`。guardian收到supervisor ACK後只啟動阻塞中的inert
   bootstrap，先把精確PID/process-group或Job identity以`acked`落盤，再以atomic rename做launch
   `pending → claimed` CAS、重驗parent authority並發布綁定該PID的`authorized` response，最後才送GO。
   worker進入Loop前還會重驗claimed request、authorized response與自身process identity。Pause/Abort若先
   cancel pending，guardian必須關閉inert bootstrap且payload對workspace/console/`.run.lock`零副作用；若
   claim先贏，identity已可fence。reap後才標terminal，不靠`Popen`後補寫PID。合作式 threat model下
   不支援刻意逃離containment的惡意descendant。
7. `engine.work block --reason <text>` 新增為 managed worker專用 terminal signal；loop驗 dispatch
   token 後寫 `assignment.status="blocked"` 並退出。既有 `issue` 仍只作觀測、不改計數，不能拿來
   表示 unknown merge/human gate。exec prompt中的 human gate、task不可行/描述錯誤等既有
   `<<ISSUE_CMD>>` 路徑，在managed mode必須改注入同一個`block --reason` command；普通loop仍注入
   `issue`。prompt文案使用中性的「回報命令」，不得讓worker誤以為block後還會有下一輪。

首次 spawn 與 resume 必須分開：

- 首次：匯入完整 frozen plan，帶 `--start-phase exec --start-task N`；
- resume：只載入既有 worker state，**不得再次傳 `--import-plan`**，避免清空進度。

worker state 至少保存並驗證：

```json
{
  "runner": "parallel-worker",
  "managed_readonly": true,
  "parent_workspace": "base",
  "run_id": "a1b2c3d4",
  "assigned_order": 3,
  "assignment": {
    "status": "running",
    "validated_sha": null,
    "validated_round": null,
    "exit_reason": null,
    "pause_generation": 0,
    "gate_request": null
  },
  "run_config_hash": "...",
  "launch_spec_hash": "..."
}
```

worker 在啟動 gate client **之前**，先把 `{request_id, validated_sha, validated_round}` 原子寫入
`assignment.gate_request`；此時 `status` 仍是既有 enum 的 `running`。明確的 before-claim
busy/cancel 才可清除此欄位；timeout、malformed response、唯讀契約違反或
`recovery-required-after-claim` 必須保留它，讓 supervisor 能精確 reconcile。帶非空
`gate_request` 的 worker 不可自行 resume/retry。

自 durable worker state 與 ready ack 都成立後，所有 worker fail path（goal missing、reset broken、
stuck-stop、gate fatal、state invariant）都要先寫結構化 `assignment.status="blocked"` 與
`exit_reason`。只有 process 無此受控終態而消失才算 unexpected crash，允許 supervisor 在 restart
budget 內自動 resume。ready 前非零退出屬 provisioning failure，由 supervisor 將 manifest
assignment terminalize 為 blocked；不得把它當 unexpected crash 或嘗試 managed resume。

### 5.3 exec prompt 的條件同步段落

`engine/prompts/exec.md` 增加 `<<SYNC_INTEGRATION>>` placeholder，放在「收拾現場」之後、
「判斷任務完成」之前：

- 普通 loop 注入空字串，生成 prompt 後 fail-closed 檢查不得殘留 `<<...>>`。
- worker 只注入 supervisor 生成的安全 ref；不把使用者 branch 名直接插入 shell 命令。
- 若不存在 merge-in-progress，執行 `git merge --no-edit <safe-sync-ref>`；up-to-date 是 no-op。
- 若存在 `MERGE_HEAD`，先確認它是 safe sync ref 當前 tip 的祖先；成立才接續解衝突、validate、
  commit，且該輪不可 done。不成立代表未知 merge 現場，呼叫受管 `block` signal 後停止；
  不可只送 `issue`、不可自動 abort 掩蓋證據。
- managed worker遇human gate、task本身不可行或描述錯誤，一律走同一個terminal `block` signal；
  普通loop的`issue`觀測語意不變。工具/暫時性驗證失敗仍依既有red/stall/reset收斂，不誤判為block。
- 禁止 checkout/detach、rebase、force、`update-ref`、刪改 integration/sync/peer task refs，
  也禁止直接 merge peer task branch。
- 同步或解衝突產生 commit 時，沿用既有 `changed → done 歸零`，之後仍跑完整 threshold。

integration 使用安全 alias ref 的原因：合法 Git branch 仍可能含 shell metacharacter；manifest、
argv、prompt與 invariant一律使用 canonical完整名
`refs/heads/loop/<run_id>/integration`，可避免短名歧義與跨 Bash/PowerShell quoting漏洞。

### 5.4 run manifest、durable artifacts 與狀態機

base workspace 是使用者入口；run artifacts 放在：

```text
<base>/parallel/<run_id>/
  manifest.json
  aggregate.json
  run-config.json
  plan.json
  executor.json
  assignments/task-N.json
  requests/staging/<unique-temp>/request.json
  requests/pending/<request_id>.json
  requests/claimed/<request_id>.json
  requests/cancelled/<request_id>.json
  responses/<request_id>.json
  operations/<operation_id>.json
  children/task-N/<child_id>.json
  launches/{pending,claimed,cancelled}/<request_id>.json
  launches/responses/<request_id>.json
  workspace-containers/task-N.json
  worker-archive-authority/task-N.json
  controls/pending/<request_id>.json
  controls/claimed/<request_id>.json
  controls/responses/<request_id>.json
  controls/bootstrap.json
  intents/task-N.json
  receipts/task-N.json
  finalization.json
  supervisor-generation.json
  worker-archives/task-N/
```

所有 JSON 使用既有 atomic/checkpoint 寫法；spool payload必須先在staging完整寫入、flush/fsync並
close，再於短spool lock內驗request_id未存在後atomic publish，不能用`O_EXCL`建立final path後邊寫邊
讓consumer看見。`manifest/run-config/plan/assignments` 建立後 immutable並記 hash。
aggregate 只能由 supervisor event loop 單一 writer 修改；child monitor thread 只 enqueue event，
不得各自 read-modify-write。

Git common-dir的canonical位置另有一個所有合法mutator都必須遵守的
`loop-agent-lite.operation.json` sidecar；同時最多一個nonterminal operation lease。它不取代既有
OS run lock，而是補足owner被SIGKILL後的child fence。拿到primary lock的任何新owner都必須先檢查
lease；只要guardian/payload仍活或identity不確定就不得碰primary。

同一common-dir另有`loop-agent-lite.owner.json`。所有**未走RepoExecutor/ManagedChildGuardian**的
primary mutator，包括普通Loop/Ralph的agent/validator/tool child，以及Dashboard/CLI launcher的
checkout/add/commit等短Git child，都必須在global primary lock與短owner-marker lock下先claim並維護
active marker。固定欄位含schema、canonical repo、owner kind、workspace/state
絕對路徑、session/generation、owner identity、host boot identity，以及：

```text
child_state: idle → launching → child_running → child_reaped → idle
child_generation、kind、argv_hash
child_running另含pid、creation token、process-group/session或Job identity
```

owner在spawn前先寫/fsync `launching`。Windows所有non-guardian spawn必須共用受控helper：
`CREATE_SUSPENDED → assign kill-on-close Job → publish PID/creation token/Job identity並fsync child_running
→ ResumeThread`，payload與grandchild在Job containment前零執行；main child退出後還要確認Job active
process count為零才能寫child_reaped。禁止沿用現行`Popen`後才`AssignProcessToJobObject`的race。

POSIX以`start_new_session`啟動trusted shim，shim先阻塞在private inherited pipe；owner取得child
identity並原子寫/fsync `child_running`後才釋放pipe讓shim `exec` payload。wait後還要確認process group
消失才寫`child_reaped`，state checkpoint完成才回idle。Windows仍可能死在CreateProcess後、identity
落盤前而留下不執行的suspended child；POSIX也可能在shim建立後、identity落盤前只留下尚未獲准執行的
候選process。owner若死在`launching`，自動recovery一律禁止並標
`owner-child-identity-unknown`。只有host boot identity已改變（舊process必已消失），或marker記錄的
owner kind使用明確危險的manual-recovery且operator已終止所有候選child，才可generation-CAS接手；
Parallel supervisor與其他owner永遠不能替它猜。

manual surface是共用`engine.cli recover-owner <workspace> --acknowledge-child-gone`，只允許marker記錄的
owner kind/workspace/repo/session recovery、owner已死、已取得global lock且primary clean時接受，並寫
audit event；Parallel supervisor永遠不會在一般start/resume暗中使用此捷徑。無法確認時建議先重啟
host，由boot identity提供機械證據。

launcher在initializing base projection落盤後、owner marker terminalize前被hard-kill時，固定復原順序是：

```text
python -m engine.cli recover-owner <workspace> --acknowledge-child-gone
python -m engine.parallel resume <workspace>   # 或明確 abort
```

第一步必須先完成marker generation-CAS、child/owner absence proof與primary-clean audit；直接執行第二步
只會fail-closed，且不得先重播RepoExecutor operation或建立safe/task ref。

只有state/operation已安全checkpoint、marker為idle/child_reaped或可驗證的child_running已fence/reap，
且owner正常終結後，才可terminalize marker；可另複製audit archive，但canonical marker保留供下一代
generation CAS。長跑runner的「正常終結」包含completed、`pause-after-plan`與受控stop的安全quiesce，
不要求phase必為done；短操作則以bounded command result與clean/invariant checkpoint為準。owner被kill時
marker會跨任意workspace root保留。

run status：

```text
initializing → running → finalizing → completed
                  ├→ pause_requested → paused
                  ├→ cancel_requested → finalizing_cancel → cancelled
                  └→ blocked

paused/blocked ──Resume──→ initializing（reconcile後回原流程或再次blocked）
initializing/running/pause_requested/paused/blocked ──Abort──→ cancel_requested
finalizing/finalizing_cancel ──failure──→ blocked
blocked（terminal_intent=completed/cancelled）──Resume──→ finalizing/finalizing_cancel
```

aggregate另存`terminal_intent: null | completed | cancelled`，一旦設為cancelled不得清除，避免cancel
cleanup失敗後Resume誤派worker。run control transition固定為：

| current | Pause | Resume | Abort |
|---|---|---|---|
| initializing/running | `pause_requested`後quiesce到paused | already-running/conflict | 設terminal_intent=cancelled → cancel_requested |
| pause_requested | idempotent等待paused | conflict，先等pause完成 | 轉cancel_requested |
| paused | idempotent | 新supervisor → initializing/reconcile | 轉cancel_requested |
| blocked、terminal_intent=null | idempotent（已quiesced） | initializing/reconcile；不安全仍blocked | 轉cancel_requested |
| blocked、terminal_intent=completed | idempotent | 重播finalizing | conflict；completion已進durable finalization |
| blocked、terminal_intent=cancelled | idempotent | 只重試finalizing_cancel/cleanup | 同Resume，絕不復活worker |
| cancel_requested/finalizing_cancel | idempotent且繼續取消 | conflict | idempotent |
| finalizing | conflict；不打斷durable finalization | conflict | conflict；完成或以terminal_intent=completed轉blocked待重播 |
| completed/cancelled | no-op | reject terminal | reject terminal |

aggregate中的task必須分開保存「業務結果」與「資源生命週期」，避免cancelled/integrated被後續
cleanup狀態覆蓋：

```text
outcome: pending → integrated | blocked | cancelled
                         blocked ──explicit Abort──→ cancelled

resource_state:
queued → provisioning → running → gate_pending → gate_claimed → exited → cleaning → cleaned
             │           ├───────────┴→ pausing → paused ────────────┤（resume → provisioning）
             │           ├────────────→ crashed ─────────────────────┤（budget內 → provisioning）
             │           ├────────────→ recovery_required ───────────┤（reconcile裁決）
             │           └────────────→ exited（cancelled，或blocked後明確Abort）→ cleaning
             └───────────────────────────────────────────────────────→ cleanup_failed
```

UI/API可由`outcome + resource_state`投影單一顯示status，但durable artifact不可只保存投影值。
`cleanup_failed`不覆寫原outcome；它使run進blocked並保留現場。例如Abort後仍是
`outcome=cancelled, resource_state=cleanup_failed`，restart可繼續cleanup且不把任務復活。
`outcome=blocked`預設保留exited worktree供診斷；明確Abort先把未整合outcome轉cancelled才進cleaning。
`outcome=integrated`永不因Pause/Abort/cleanup failure改寫。

resource transition按event補齊：

| event | allowed resource_state | result |
|---|---|---|
| Pause queued | queued | 保持queued，不建立resource；run quiesce後paused |
| Pause active | provisioning/running/gate_pending/pausing | fence到safe boundary後paused；gate_claimed先reconcile |
| Pause already quiesced | paused/exited/cleaned/cleanup_failed | no-op；exited不開始cleanup，cleanup_failed不自動retry |
| Pause uncertain | crashed/recovery_required | 不spawn；先reconcile/fence，再投影paused或blocked |
| Pause during cleanup | cleaning | 不interrupt destructive operation；等待其原子結束為cleaned/cleanup_failed後再完成run quiesce |
| Resume work | paused/crashed | provisioning；restart budget與terminal_intent必須允許 |
| Reconcile gate | gate_claimed/recovery_required | 回gate_claimed續交易，或exited/blocked；不得回普通agent round |
| Abort before resource exists | queued | outcome=cancelled，直接cleaned |
| Abort with resource | provisioning/running/gate_pending/pausing/paused/crashed/recovery_required | outcome=cancelled，先fence/reap → exited → cleaning |
| Abort while claimed | gate_claimed | 先裁決receipt；已merge則outcome=integrated，否則cancelled/blocked，再exited → cleaning |
| Abort exited | exited | 未整合outcome轉cancelled、integrated保持，接著cleaning |
| Abort during cleanup | cleaning | 設terminal_intent=cancelled但不interrupt；未整合outcome轉cancelled，等待cleaned/cleanup_failed |
| Abort cleaned | cleaned | 未整合outcome轉cancelled、integrated保持；resource維持cleaned |
| Abort cleanup retry | cleanup_failed | 未整合outcome轉cancelled，重新TOCTOU檢查後進cleaning |
| Cleanup | exited | cleaning → cleaned或cleanup_failed |
| Cleanup retry | cleanup_failed | 重新TOCTOU檢查後cleaning → cleaned或cleanup_failed |

- aggregate task outcome/resource_state各使用上圖完整 enum；worker `assignment.status` 的穩定 enum 是
  `running | paused | recovery-required | integrated | blocked | cancelled`。`crashed/pausing/gate_*` 是 supervisor根據
  process、request與control artifacts投影的 orchestration狀態，不偽裝成 worker自報終態。
- Pause control 帶 monotonic `pause_generation`。managed loop平順消費後寫 assignment paused；若需
  force-kill，supervisor只有在已記 pause intent、fence active child且確認process消失後才投影 paused，
  否則是 crashed。`paused → provisioning/running` 必須由明確 Resume觸發。
- `gate_claimed` 不可被普通 Pause退回或當 busy；必須先完成 transaction或進
  recovery-required，再決定 integrated/blocked。
- receipt 只證明 `integrated`，**不代表 worker 已退出或可刪 worktree**。
- 本 supervisor spawn 的 child要求 `Popen.wait()`；從 crash接手的 adopted/orphan child無法
  `waitpid`，改以 PID/session identity已消失 + worker `.run.lock`/private Git lock已釋放作
  exited證據。兩者都還要確認 active-child group已fence且worktree clean，才可移除worktree。
- destructive cleanup 先保存 observation token（pid/session、branch SHA、dirty/lock狀態），取得
  cleanup lock後再讀一次；兩次不一致就放棄並重新reconcile，防止TOCTOU刪到復活中的sandbox。
- cleanup 失敗保留現場，標 `cleanup_failed` 並使 run blocked；不可硬刪 live cwd。
- fatal/stuck/goal-missing 不自動 respawn；只有 unexpected crash 可依 budget resume。
  `restart_count` 以每task monotonic欄位持久化，`--worker-restart-limit` 預設3；supervisor重啟
  不歸零，超過即blocked。

base `state.json` 是既有 UI/status 的 canonical projection，不另建第二套 convergence state：

- `runner: "parallel-supervisor"`；
- `loop.pid/session_id` 投影 supervisor，退出時可靠清除；
- `parallel` 投影 run_id、status、batch、task 摘要與 error；
- initializing/running/pause_requested/paused/cancel_requested/finalizing/finalizing_cancel/blocked/
  cancelled 時 `phase="exec"`；只有全部 task cleaned且
  finalization完成後才 `phase="done"`。cancelled產partial report與cancelled notify，但不冒充done；
- `completed` 由 receipts 依 plan order 投影，`base_sha=integration_before`、
  `sha=validated_sha`、`round=validated_round`，讓既有loader與task diff都成立；
- base `current_order` 是當前batch內最小未終結order，`current_task_base_sha=expected_head`、
  `done_count=0`（個別worker計數只在parallel task summary）、top-level `round=max(worker.round)`。

`finalizing` 使用 durable outbox：先原子寫run report與帶 `event_id` 的notify event，再投遞
`notify_cmd`；crash後重播未ack event，語意為at-least-once，命令可用新增 `{event_id}` placeholder
做冪等。report已存在時內容hash相同即視為完成。只有finalization receipt落盤後才標completed；
cancelled走相同流程但使用partial report/status。

worker projection 只顯示 assigned task/status，不把完整 plan 算成 N 份 fleet 工作量；worker
workspace 可留 console/history，但所有 repo-backed 操作在 worktree 清除後隱藏。

### 5.5 supervisor ownership、派工與 config snapshot

啟動固定順序：

1. Dashboard launcher先經central helper取得primary Git run lock、audit parallel operation lease並以
   generation CAS claim`owner kind=parallel-launcher` marker；只有audit通過才以`O_EXCL`建立每次submission
   唯一、檔名含raw SHA-256的`parallel-plan.<sha>.<uuid>.staged.json`，fsync檔案與parent directory後
   terminalize marker。不同Dashboard process不共用可覆寫的pending path；audit失敗時零primary/base state
   mutation。direct CLI可使用自己的plan path，但不經Dashboard staging；
2. supervisor先canonicalize config並檢查required secrets，之後才建立base workspace並取得
   `<workspace>/.run.lock`。在lock內以single no-follow regular-file read讀plan；Dashboard啟動必須比對argv
   帶入的raw SHA-256後才JSON/stack validate，direct CLI即使未帶expected hash也同樣只在lock內讀一次；
3. supervisor仍在base lock內claim`parallel-launcher` RepoOwnerFence、重驗primary clean/non-detached與
   branch/HEAD，封住Dashboard釋放marker到supervisor接手間的handoff空窗；
4. 選run_id（固定小寫hex），記`integration_start_sha`與branch，持久化immutable
   manifest/run-config/plan/per-task assignments與supervisor generation authority，並在
   任何RepoExecutor/ref mutation前先發布`initializing` base projection；這使launcher完成handoff後即使
   supervisor立刻死亡，也仍有可定位、可Resume/Abort的run；
5. 建立in-process RepoExecutor；它自行解析primary Git dir、取得`loop-agent-lite.run.lock`並audit
   common-dir operation lease。所有會碰primary的Git mutation（safe ref、worktree add/remove、merge）與
   startup validator都經bounded operation-child containment；PREFLIGHT驗primary/HEAD與唯一validator argv，
   Validate後重查HEAD/dirty；
6. 執行`INITIALIZE_RUN_REFS`，以expected-absent CAS建立canonical safe sync ref指向start SHA並寫
   operation receipt；manifest已存在但ref/receipt缺失時，只有「無init intent/result、無sync ref/
   operation lease、無gate/receipt/task resource且primary仍為start SHA」的精確pristine startup可重播
   PREFLIGHT與INITIALIZE。部分或非canonical journal一律blocked；receipt落盤後才派工。

RepoExecutor API是封閉operation enum：
`PREFLIGHT | INITIALIZE_RUN_REFS | CREATE_WORKTREE | GATE_MERGE | REMOVE_WORKTREE | SHUTDOWN`。
authority按operation最小化：

| operation | 必須綁定的authority |
|---|---|
| PREFLIGHT | 由manifest、immutable plan/run-config與start identity推導的pending-launch hash；只可執行唯一hash-bound validator argv |
| INITIALIZE_RUN_REFS | manifest hash + integration start branch/SHA + canonical refs expected absent；只可CAS建立run safe ref |
| CREATE_WORKTREE | manifest hash + assignment hash |
| GATE_MERGE | manifest hash + assignment hash + 已claimed request id/hash；只做exact-SHA/clean/ref invariant與固定Git transaction，不重跑validator |
| REMOVE_WORKTREE | manifest hash + assignment hash + terminal outcome/resource observation token |
| SHUTDOWN | supervisor/executor session + generation；不需要assignment |

所有request都帶operation_id與expected state；repo/path/ref由上述immutable authority推導，不接受caller
提供任意路徑或shell。RepoExecutor instance綁定supervisor session/generation並與supervisor同process；
supervisor死亡時不會留下可繼續收件的orphan executor，但可能留下已發布identity的operation child。
durable nonterminal operation lease會阻擋所有新mutator；新supervisor不adopt舊instance，而是在確認舊
supervisor與contained child已消失/fence後，以原request與byte-for-byte CAS重播。無法證明安全就
quiesce為blocked。

Dashboard 在checkout、寫goal、`git add/commit`前取得同一primary Git run lock，不得只靠
in-process `JOBS_LOCK`。handoff空窗若被其他合法runner搶先，executor取鎖失敗或supervisor
重驗branch/HEAD/hash失敗，便fail-closed且不派工。

branch/worktree：

- integration branch 是 supervisor 啟動時 primary checked-out branch，禁止 detached HEAD；
- safe sync ref：`refs/heads/loop/<run_id>/integration`；
- task ref：`refs/heads/loop/<run_id>/task-N`（UI可顯示短名）；
- worktree：`WORKSPACE_ROOT/<base>/worktrees/<run_id>-task-N`；
- worker workspace：`<base>--<run_id>-task-N`；
- supervisor在發布child reservation/ACK前先建立並fsync canonical worker workspace container；因此
  durable payload identity存在但container消失時是稽核證據遺失，不能猜成「尚未啟動」；
- 建立 worktree 前 resolve path，確認不在 target repo 內；若 target repo 是 loop-agent-lite 自身且
  default workspace root 落在 repo 裡，要求顯式 `LOOP_AGENT_WORKSPACE_ROOT` 指向 repo 外。

supervisor 將設定拆成兩層，避免「每個task不同欄位卻共用同一hash」：

- immutable `run-config.json`：repo/primary_repo、goal、plan_doc、agent_cmd、validate_cmd、
  flag/done/red/stall/stuck thresholds、round/validate timeout、agent backoff、notify、
  `max_parallel`與restart limit；
- immutable `assignments/task-N.json`：run_id、parent、assigned order、task/sync refs、derived paths、
  gate client command與 `run_config_hash`。每個worker保存自己的 `launch_spec_hash`，gate request
  同時帶兩個hash。

環境不可把完整 `os.environ` 序列化進artifact：只保存PATH additions與明確標為non-secret的
allowlisted values；API token等只保存必要的變數名稱/存在條件，值由啟動process繼承但不落盤。
worker process仍完整繼承當次supervisor ambient environment；凍結的PATH additions固定加在ambient PATH
之前，明列的non-secret覆寫為凍結值，required secret則按名稱從當次ambient取值。start缺secret時在
workspace/repo mutation前失敗；resume缺secret時仍先audit/fence既有executor/child，再投影blocked，
不把secret複製到worker state/console。
CLI分別以`--path-addition ABS_PATH`、`--non-secret-env NAME=JSON_SCALAR`與
`--required-secret-name NAME`建立此contract；名稱大小寫視為同一identity，non-secret與required-secret
不可重疊，且`PATH/PYTHONHOME/PYTHONPATH/PYTHONUTF8/LOOP_*`不可覆寫。Dashboard v1沒有non-secret／
required-secret編輯UI，只在首次Start把當時解析完成的`extra_path_dirs`凍結為absolute PATH additions；
Resume不重讀之後被修改的Dashboard PATH設定，只從目前supervisor ambient environment按immutable名稱
取得secret值。

所有 worker argv 由單一 builder 產生並測試 round-trip；不可讓 CLI、Dashboard、restart 各自
拼一套不同參數。notify 的 assignment 事件由 supervisor 統一送，worker 不冒充全域 completed。

同時執行數受 `--max-parallel` 控制（預設 2）。同 batch 超出上限者維持 queued；只有本 batch
全部進 cleaned，或存在 blocked/cancelled 的終止判定完成後，才考慮下一 batch。

Git auto-gc 不寫 repo config；supervisor/gate/worker 啟動的 Git command 以環境或逐命令
`-c gc.auto=0` 抑制。agent 自己執行 Git 不受此保證，列為合作式 trust boundary 的殘餘風險。

### 5.6 gate IPC、結果協定與 merge journal

`--complete-gate-cmd` 啟動的 client 只做：

1. 在`requests/staging/<unique-temp>`完整寫入request（run/task/request_id/validated SHA+round、
   兩層config hash/deadline），flush/fsync並close；再於短spool-transition lock內確認id未出現在任何
   state後atomic publish到`pending`。final path不可用`O_EXCL`開啟後直接串流寫入；
2. supervisor接單時在同一transition lock內以同filesystem atomic rename將`pending → claimed`；這是唯一
   linearization point。client deadline/lease-loss或Pause則競爭rename `pending → cancelled`，
   兩邊只有一方能成功；
3. client若成功cancel pending才可回busy/paused；若發現已claimed，就不得再回busy或重送，
   必須等terminal response，或在supervisor/executor消失時回recovery-required；
4. 輸出**唯一一行 JSON**並以固定exit code結束。

建議協定：

| rc | status | loop 行為 |
|---:|---|---|
| 0 | `merged` / `already-merged` | 寫 assignment integrated，正常退出 |
| 10 | `stale-integration` | done 歸零，注入 sync note |
| 11 | `busy` / `supervisor-lost-before-claim` | client取消pending成功；保留done，可安全重試 |
| 20 | `paused` | 寫assignment paused並退出，等待明確Resume |
| 21 | `cancelled` | 保存assignment cancelled，正常停止 |
| 30 | `fatal-invariant` | 保存 blocked reason，fail-closed |
| 31 | `recovery-required-after-claim` | 保存assignment recovery-required並退出；只由reconcile裁決 |

malformed JSON、未知 rc/status、欄位不一致一律當 fatal；不得靠解析人類字串猜結果。
`busy` 只可表示 supervisor **尚未claim request**。一旦claimed，普通
client timeout 不得強殺 merge；若 supervisor/merge 不明中斷，結果是 recovery-required/blocked，
不是可安全重試的 busy。

Pause先把run durable設為pause_requested，再停止claim；所有pending request以CAS移到cancelled並
回paused，所有claimed request必須完成/reconcile。restart需掃描pending/claimed/cancelled/response，
不能只看aggregate。

merge 與 receipt 無法成為單一 filesystem transaction，因此先寫 durable intent：

```json
{
  "state": "prepared",
  "run_id": "a1b2c3d4",
  "task": 3,
  "request_id": "...",
  "integration_before": "...",
  "integration_ref": "refs/heads/<frozen-branch>",
  "sync_ref": "refs/heads/loop/a1b2c3d4/integration",
  "sync_before": "...",
  "validated_sha": "...",
  "validated_round": 17,
  "launch_spec_hash": "...",
  "prepared_at": "..."
}
```

RepoExecutor 在common-dir merge lock內依序：驗expected chain → 寫prepared intent →
以受管空目錄停用repo hooks後執行
`git -c core.hooksPath=<owned-empty-dir> -c gc.auto=0 merge --ff-only --no-edit <validated_sha>` →
驗primary clean/exact SHA →
`git update-ref <sync-ref> <validated_sha> <sync_before>` 做compare-and-swap → 原子寫receipt →
intent標committed → response。receipt包含validated_round，供base completed投影。

RepoExecutor整個run持有既有global primary Git run lock，不把它handoff給child；另外在
`git rev-parse --git-common-dir`解析出的canonical位置使用operation-fence lock與上述sidecar，不能放在
linked worktree私有`--git-dir`。每個primary-touching operation都遵守：

1. 在global primary lock仍由RepoExecutor持有時，以operation-fence lock原子寫/fsync
   `reserved` lease（operation_id、nonce、generation、immutable spec hash、expected refs），再由同一
   executor CAS為`running`；operation lease只允許`reserved → running → terminal`；
2. 每個Git/validator child在同一lease內依序走`idle → launching → running → reaped → idle`。
   `launching`先保存argv hash但沒有identity；capture並fsync精確PID/session/containment後才是`running`；
3. POSIX trusted guardian先阻塞在inherited pipe/barrier，executor寫/fsync child `running`且收到
   subreaper-ready證據後才ack/exec；Windows direct operation child
   必須`CREATE_SUSPENDED → assign kill-on-close Job → publish PID + creation token + Job identity →
   fsync running → ResumeThread`；不得依賴`msvcrt.locking` handle繼承，因其lock是process-owned；
4. supervisor（連同in-process executor）被SIGKILL時global lock雖會釋放，common-dir nonterminal lease仍讓所有新mutator等待
   guardian/payload消失並reconcile。identity不確定一律blocked，不能猜child已死；
5. terminal lease只能由持operation-fence lock者，在guardian/payload group確定消失且journal outcome
   已對帳、immutable operation result已落盤後寫入。normal Pause/Abort也先kill/reap/fence，再由目前
   executor或recovery owner完成terminal transition。

普通Loop/Ralph/launcher接手前，terminal lease仍須有hash一致的immutable operation result；
`terminal_status=blocked`不是安全handoff證據，必須由原Parallel recovery依journal/ref matrix裁決成功後
才能放行。common-dir gate intent/receipt會跨run保留；audit只可在驗證artifact內的
`run_id/request_id/operation_id`後排除其他run的歷史，不能把舊run誤判成目前run的orphan。

「取得global primary lock → 檢查common-dir operation lease → audit owner marker」必須封裝在所有
primary mutator共用的central lock helper，普通Loop/Ralph、Parallel、Dashboard與CLI都不能略過。
owner marker規則是：

- active且session/generation等於目前合法owner：只允許該owner繼續；
- active但owner已死：只有marker記錄的同runner/workspace可嘗試recovery；child_running須有可驗的
  creation token，idle/child_reaped可直接對帳，launching則先要求host-restart證據或上述明確manual
  recovery。符合接手條件後，在短lock內驗舊generation並CAS成`recovering(new_generation)`，再由該
  recovery owner fence/reap、checkpoint與terminalize；其他新runner或短操作一律拒絕；
- terminal：新owner驗證舊generation後CAS成active新generation；不存在時才可no-replace建立；
- active/recovering絕不可blind replace，Dashboard checkout/commit也不能以「PID已死」略過。

固定鎖序為：base run lock → global primary Git run lock →（短暫）common operation-fence/merge lock →
owner-marker lock。永不持common short lock等待worker、child或長時間notify，避免deadlock。這個
mechanical containment只包trusted Git/validator command，不把merge權交給worker。

v1 operation lease保證的邊界是**RepoExecutor/parallel-managed operation/child**。普通Loop/Ralph為
維持核心相容性不改成guardian；Dashboard/CLI launcher短Git child也不另起RepoExecutor。這些未受
guardian管理的合法mutator一律使用上述repo-common owner marker child lifecycle，並透過central helper
拒絕任何既存parallel nonterminal lease與不屬於自己的active/recovering owner marker。

Parallel start/resume的legacy orphan audit以common-dir owner marker為權威，不依賴目前
`--workspace-root`。live owner通常也會被global lock拒絕；active marker若owner缺失/不符、記錄的
state非終態、存在interrupted round或child liveness無法證明，標`legacy-orphan-unknown`並fail-closed，
不做preflight或任何primary mutation。marker中的workspace/state path須作canonical/no-link/type驗證，
不可盲信後寫檔。只有marker記錄的原runner/workspace以明確recovery在generation CAS後，確認child消失
且state/clean tree收斂，才可terminalize marker；Parallel不得自行清除。workspace外部process仍屬
合作式threat boundary的unknown actor，由每次exact ref/clean invariant偵測；v1不宣稱可機械攔截
任意外部writer。

### 5.7 crash、restart、pause 與 abort

supervisor startup 必須先 reconcile，完成前不派新 task：

- 先取得global/operation-fence locks並裁決common-dir operation lease：live guardian/payload只可等待或
  fence；identity未知、creation token不符或PID reuse無法排除時保持blocked；identity已驗證且process
  group/Job確認全數消失時，才依result marker與下列journal矩陣reconcile並由owner terminalize lease。
  `reserved`若creator已死，可在短lock內與late guardian競爭CAS cancel，只有cancel贏且payload從未獲准
  啟動才視為安全；`child_running`confirmed-dead後一律依journal矩陣，不會永久卡死也不會直接放行。
- request已claimed、但無intent/receipt：先fence舊worker/client，驗request/assignment/hash與由receipts
  推導的expected chain。若primary/integration/sync仍為expected且clean，重跑gate invariant：已stale則
  原子補terminal stale response；仍可ff-only才寫prepared intent並繼續；任何operation lease、未知ref/
  dirty或無法證明payload未開始都blocked。
- prepared intent、無 receipt：
  - 先取得executor locks並fence舊worker active child；若舊gate client仍活，只能保持等待claimed
    request，不可回到agent round；
  - primary clean、`HEAD=integration_before`、`sync=sync_before`：重跑全部invariant後可重試merge；
  - primary clean、`HEAD=validated_sha`、`sync=sync_before`：CAS sync ref到validated後補receipt；
  - primary clean、`HEAD=validated_sha`、`sync=validated_sha`：直接補receipt；
  - 其他HEAD/sync組合、CAS失敗或primary dirty：run blocked，保留現場交人工。
- receipt存在：驗claimed request、intent、兩層hash與receipt chain相符；primary可位於該receipt SHA或
  後續合法receipt descendant，sync必須等於最新receipt tip。之後冪等補`intent=committed`、success
  response、aggregate與assignment integrated，再依child/process/lock推進exited/cleaning。
- success response無receipt、committed intent無receipt、cancelled request卻有intent/receipt、
  response/receipt/hash互相不符，或aggregate超前receipt：一律corrupted/blocked。
- `recovery-required-after-claim`只表示client已暫時退出、等待reconcile，**不是claimed request的
  terminal response**；recovery最後仍須產生stale/success/fatal等可證明結果。
- aggregate 落後 receipt：由 receipt 冪等補投影。
- running worker 無受控終態且 process 已死：標 crashed；在 restart budget 內以
  `--managed-worker-resume` 恢復。blocked/fatal/stuck/goal-missing 絕不自動 respawn。
- receipt 已存在但 child 還活著：維持 integrated，先等/停並 reap；不可先清 worktree。

平順 Pause（Dashboard「停止」、CLI `stop`）：

1. run → `pause_requested`，停止派工/claim，CAS取消pending gate並回paused；
2. claimed gate完成或進recovery-required，不在transaction中間普通中斷；
3. 對workers發帶 `pause_generation` 的stop-after-round；逾寬限後才依active-child lease
   interrupt/force-kill各自process group/Job Object並確認消失；
4. executor idle/shutdown，所有本代child reap或adopted child確認消失後，原子投影paused、清除
   base PID/session、釋放base/primary locks，supervisor退出。未整合worktree保留供resume。

Resume 先完整 reconcile，再啟動 queued/crashed/paused workers。Abort 是顯式破壞性操作：
停止/reap child，未整合 task 標 cancelled；已寫入 primary 的 receipts **不 rollback**；可安全清理
的 worktree才移除，最後 run=`cancelled`。base workspace 在下述`RUN_NONTERMINAL`且尚有 run
artifacts 時不得 Edit/Import/Phase/Set-task/Delete；要另起 run 必須先完成 resume 或 abort/cleanup。

中央base mutation guard明列
`initializing | running | pause_requested | paused | cancel_requested | finalizing | finalizing_cancel | blocked`
為`RUN_NONTERMINAL`。在這些狀態，即使PID暫空或supervisor已退出，Edit/Import/Phase/Set-task/Delete、
普通Run/Restart與checkout/goal寫入都必須在任何mutation前拒絕且state byte-equivalent；只允許唯讀
status/log/diff，以及依狀態合法的`parallel resume`或`parallel abort`。

control IPC使用與gate相同的stage/fsync/publish與atomic claim規則。request至少含run_id、request_id、
action、expected supervisor session/generation、monotonic control_generation與aggregate version；owner只可
claim符合目前generation的一筆並以single-writer event loop更新aggregate，response按request_id冪等。
stale generation明確回stale，不執行action。CLI/Dashboard不直接寫aggregate。

live owner判定以base `.run.lock`與exact session/generation metadata為準；lock明確held但metadata暫時
partial時bounded retry後fail-closed。live owner control使用controls spool自己的transition lock，只把
pending request送給該session，owner在claim前重驗aggregate version、control generation與action合法性。
no-owner control另以`controls/.bootstrap.lock`安裝唯一`bootstrap-control` intent；recovery process先取得
base `.run.lock`，再claim intent（bootstrap action線性化點），其後才做supervisor generation CAS與
reconcile。claimed Abort壓過稍後的Pause/Resume；非法finalizing/terminal action在publish/claim前拒絕，
不得前進任何authority。`applied`只能在重讀並驗證durable aggregate與base projection都已反映assigned
control generation後發布。併發control只能一個bootstrap claim成功，另一個接收既有結果或conflict，
不得另spawn owner。

paused與blocked都採「quiesce後supervisor退出」而非背景常駐；blocked也先停止worker/executor、
清PID並釋放locks，讓人可以修復primary。Resume/Abort遇live owner時送typed control IPC；確認沒有
owner時才spawn新supervisor。新process必須重新取locks與reconcile；若paused期間primary被其他
合法工作推進，exact expected chain不符就維持blocked，不會靜默接續。completed/cancelled為terminal。

supervisor捕捉SIGINT/SIGTERM並走上述Pause；沿用 `engine.platform_compat` 的process group/
Windows Job Object模式。若supervisor被強殺，未claim的client可CAS取消，claimed client只能標
recovery-required；worker本身沒有merge權。transaction fence由RepoExecutor存活時的global lock，
以及common-dir nonterminal operation lease與operation-child containment共同維持；新supervisor
只有在central lock helper確認lease可裁決後才能reconcile。

### 5.8 CLI、Dashboard、status 與 task diff

runner 型別擴充為：

```text
loop | ralph | parallel-supervisor | parallel-worker
```

Base workspace routing：

- `run/restart/resume` 看到 `parallel-supervisor` 時轉到 `engine.parallel resume`，不可啟普通 loop；
- `stop` 轉為 supervisor pause；另提供明確 `parallel abort`；
- completed/cancelled 不可普通 resume；blocked resume 先 reconcile，仍不安全就維持 blocked；
- POSIX/Windows PID 判定納入 `engine.parallel`，state PID/session 與 `.run.lock` owner 必須一致。

Worker workspace protection：

- 使用一個跨CLI/Dashboard/`engine.loop`共用的最低層
  `assert_workspace_operation_allowed(workspace, operation, dispatch_token)`。讀到
  `runner="parallel-worker"`或immutable pending assignment後，必須在reset/import/preflight/validator/
  repo mutation前執行；只有parent supervisor簽發且符合assignment hash的dispatch token可啟動或
  resume managed loop。
- CLI run/restart/resume/config/delete/stop、`init --force`、`check`與直接
  `engine.loop --reset-state/--import-plan/--preflight-only`全拒絕，提示操作parent supervisor；`check`
  會跑可能有side effect的validator，因此不視為唯讀。
- Dashboard 以中央 `managed_readonly` guard 拒絕所有 mutation API，包括 run/resume、drain、
  stop、edit-state、import-plan、edit-config、phase、set-task、delete、issue ack/clear；
- 只帶repo、不帶workspace name的repo-status/Validate/Agent測試同樣先反查managed worker與任何
  nonterminal Parallel base；短命validator/agent使用repo owner child fence，不能繞過中央owner邊界；
- 前端顯示 parent/run/task/status badge，只保留 console/history/read-only plan；不顯示 Resume、
  Edit、Delete、task diff等會依賴已清 worktree 的操作。
- 防止managed workspace永久累積：只有parent supervisor在task cleaned、child records全數reaped且
  `.run.lock`沒有live owner後，可把worker workspace整個目錄原子移到
  `<base>/parallel/<run_id>/worker-archives/task-N`。移動當下state、console與history會一併保留，終態一般
  workspace discovery不再看見它；一般CLI/UI仍無權直接刪。終態correctness authority是container marker、
  launch/child交叉證據與no-follow驗證且綁定immutable plan/receipt的checkpoint；move前另以immutable
  archive authority記錄checkpoint presence/hash，故cancelled worker事後遺失兩份checkpoint也能被偵測。
  console/history等觀測資料
  不宣稱具備後續防刪完整性。若crash發生在container預建後、child record前，只有exact pristine
  container可按cancelled task歸檔。

Base Dashboard v1 不做複雜甘特圖，只在既有 Workspace view 加：parallel badge、run status、
當前 batch、每個 task 狀態，以及 Pause/Resume/Abort 三個正確分流的控制。Launcher 增加
Parallel Loop runner；必須有合法 plan import 且 `start_phase=exec`。

Base workspace同樣先套用`RUN_NONTERMINAL`中央guard，再做runner routing；不能以「目前找不到PID」
作為放行mutation的條件。transition state只改顯示文案，不縮小guard集合。

status/fleet：

- `project_status` 辨識 base parallel 摘要與 supervisor PID；
- managed workers 可列為 child，但從 workspace_count、plan_len、completed、attention 等 fleet
  aggregate 排除，避免完整 plan 被重複 N 次；
- worker 投影 `parent_workspace/run_id/assigned_order/assignment`，不假裝是「task 1/N 停止中」；
- sync commit round 可標 `round_kind="sync"`；若 v1 UI 尚未呈現，至少不得把它算成 fleet
  attention，history仍保留稽核。

task diff 是 MVP 正確性的一部分，不列為美化：base `completed` 使用 receipt 的
`integration_before..validated_sha`，repo 固定使用 primary repo；worker 清理後隱藏 repo-backed
功能。這讓同 batch merge 順序不同時仍顯示各 assignment 的淨變更。

## 6. 必須 fail-closed 的 cross-file invariants

除了單檔 JSON shape，resume/gate 必須交叉驗證：

- run_id 是固定長度小寫 hex；branch/ref/worktree/workspace 名可由 run_id/order 唯一推導；
- manifest plan hash == immutable plan file == base state plan；
- `run_config_hash` 等於immutable `run-config.json` hash（base `config`只是不具獨立authority的projection）；
  每task `launch_spec_hash` 分別等於assignment、worker state與
  gate request，不把per-task欄位誤當同一份config；
- assigned_order 存在於 plan、task 唯一且符合當前 batch；
- stack 連續性在 import、state load、manifest load 三處語意一致；
- task outcome與resource_state transition各自合法，receipt SHA/intent/aggregate/base completed互相一致；
- request只能做 `pending → claimed | cancelled` 的atomic transition，claimed只能由terminal response
  或reconcile結束；同一request_id不可重用；
- integration expected chain 只能由 start SHA 依 receipts 的實際 merge順序推導；
- primary branch、HEAD、safe sync ref 未被未知 actor移動；
- executor identity/global lock、common-dir operation lease/guardian identity、worker child lease與
  PID/session互相一致；
- common-dir owner marker的canonical repo、owner kind、workspace/state或bounded operation、session/
  generation、global lock與child identity一致；active未知marker不可由任何非recorded recovery清除；
- receipt 只推進到 integrated，child exit/lock release/cleanup證據才能推後續狀態；restart_count與
  pause_generation只能monotonic增加；
- base completed必含order/sha/base_sha/validated round；terminal projection repair還必須驗證receipt與成功
  response精確對應、最後成功operation為同generation的SHUTDOWN、finalization outbox與REPORT hash、
  task worktree path/registry/ref均消失，以及worker launch/child/container/checkpoint等權威archive證據完整；
- 所有 artifact 路徑及其受控ancestor均 resolve 在該 base workspace/run dir內且不是symlink/reparse point。

任何 malformed/unknown enum、重複 assignment、receipt 斷鏈或路徑逸出皆 blocked，不做「盡量猜」。

## 7. 決策摘要

| 決策 | v1 採用 | 延後/拒絕原因 |
|---|---|---|
| 並行層位置 | 外層 supervisor + 原生 loop child | 不重寫 loop 單 writer 狀態機 |
| plan 來源 | 人工/import，直接 exec | planner 自動 stack 延後 |
| stack 語意 | 正整數、同值單一連續 batch | 不做 DAG/拓撲排序 |
| worker 身分 | 完整 plan + immutable assigned_order | 單 task plan 會扭曲 task id |
| gate writer | supervisor以atomic rename claim；只有RepoExecutor封閉operation path可寫primary | worker與gate client直接 merge 會留下 orphan writer |
| merge 依據 | post-Validate exact SHA + ff-only | branch name 可漂移 |
| stale 後共識 | 重跑完整 done threshold | 不縮短驗證安全邊界 |
| 並行上限 | `max_parallel=2` | 無上限會放大 validator資源競爭 |
| worktree | merge 後待 child reap 再刪，branch保留 | 不刪 live cwd；branch供稽核 |
| Dashboard | 可 launch/觀測 base，worker唯讀 | 不做複雜聚合視覺化 |
| Stop | resumable Pause；Abort另列 | 避免把停止誤當 rollback |

## 8. 風險與緩解

| 風險 | 緩解 |
|---|---|
| validator 寫檔後假完成 | Validate 後重讀 HEAD/dirty；side effect輪不得累計 done |
| branch/HEAD 分裂造成假完成 | exact SHA + HEAD/ref/clean invariant |
| merge 後、receipt 前 crash | prepared intent + deterministic recovery matrix |
| supervisor/in-process executor死後仍有orphan Git writer | gate client無Git side effect；global lock + common-dir operation lease + operation-child containment，lease裁決前新owner不可寫primary |
| request timeout/pause與claim競爭造成 ghost merge | `pending → claimed | cancelled` 以atomic rename作linearization point；claimed後只能等待terminal response或reconcile |
| worker死後 agent child仍在改檔 | pre-spawn reservation + guardian self-claim + process group/Job identity；fence/reap前禁止resume與cleanup |
| receipt後刪到 live worktree | integrated/exited/cleaned分離，reap與lock release後才清 |
| fatal worker被無限重啟 | blocked終態；只有 unexpected crash 有有限 restart budget |
| shared ref被 agent直接改寫 | safe ref、prompt禁止、expected receipt chain偵測；合作式 trust boundary明載 |
| Dashboard在 gate 中改 primary | 所有 primary mutation共用 Git run lock，啟動後再驗 start SHA |
| UI把 worker當普通 loop操作 | runner型別 + backend中央 readonly guard + 前端隱藏 mutation |
| fleet計畫總數膨脹 | managed worker排除 aggregate，base為唯一計數來源 |
| cleanup後 task diff壞掉 | base primary repo + receipt range；worker隱藏 repo-backed操作 |
| worktree落在 target repo內 | resolved containment檢查；必要時要求外部 workspace root |
| agent觸發 auto-gc | supervisor命令注入 gc.auto=0；殘餘風險接受 |
| 非guardian parent（Loop/Ralph/Dashboard/CLI launcher）死後留下child | child前先fsync Git common-dir owner marker；所有owner kind與workspace root都先audit，只有recorded recovery可在reap後terminalize |
| 非guardian owner死在Popen與child identity發布間 | marker維持launching並阻擋所有mutator；不做不可靠自動推斷，只接受host-restart證據或明確人工recovery |
| startup在manifest/base projection/ref journal各點死亡 | mutation前先發布initializing projection；只重播可證明pristine的PREFLIGHT/init，任何部分或額外journal皆blocked |
| terminal base projection落後或被竄改 | terminal aggregate保持唯一authoritative truth且repair不改寫；重驗receipt/response、SHUTDOWN generation、finalization/report、task ref/worktree與child/archive全套authority後才投影 |

## 9. GitHub 外部實作對照

本設計參考現有開源agent loop／orchestrator，但只採用能補強既有Loop核心的最小模式；下列`main`
連結於2026-07-21核對，實作決策以本文件與本repo測試為準，不把外部漂移當成本地authority：

| 專案 | 可借鏡做法 | v1 採用方式 | 明確不引入 |
|---|---|---|---|
| [Anthropic Ralph Wiggum plugin](https://github.com/anthropics/claude-code/tree/main/plugins/ralph-wiggum) | Stop hook重複同一prompt、local state、max iterations與明確cancel | 保留本專案既有Ralph／Loop的持續迭代、有限budget與顯式Pause/Abort；parallel只包在外層 | 不以hook取代原生`engine.loop`，也不把completion promise當merge correctness |
| [snarktank/ralph](https://github.com/snarktank/ralph) | 每輪fresh context，以Git、PRD與progress檔承接狀態 | immutable plan、Git SHA、receipt與history仍是跨輪可恢復證據 | 不另造第二套PRD/progress truth |
| [Gas Town](https://github.com/gastownhall/gastown/blob/main/docs/design/polecat-lifecycle-patrol.md) | worker worktree隔離、獨立merge角色、crash budget、destructive cleanup前TOCTOU recheck | worker不得直接寫primary；RepoExecutor、restart limit與cleanup observation token | 不導入多層AI patrol、mail system或通用agent組織模型 |
| [Overstory](https://github.com/jayminwest/overstory) | typed protocol、isolated worktree、FIFO merge queue、mechanical watchdog/checkpoint | typed gate response、single-writer序列化、PID/session/lock與durable journal | 不導入SQLite mail、AI watchdog、runtime adapter framework或多階角色 |
| [Sandcastle](https://github.com/mattpocock/sandcastle) | Abort殺in-flight process但保留worktree；dirty worktree不自動刪除 | Pause/Abort先fence/reap child；dirty/lock不明時fail-closed保留現場 | 不新增sandbox provider abstraction；hostile-code isolation仍延後 |

外部實作的共同方向是「worker隔離、merge集中、狀態可重建、清理保守」。本規劃維持
loop-agent-lite的核心：每個worker仍是原生convergence loop，supervisor只做派工與durable
coordination，RepoExecutor只做封閉且hash-bound的mechanical repo operation；不擴張成通用plugin平台
或自治agent組織。

## 10. 相容性

- 舊 plan 不含 `stack`；普通 `engine.loop` 的runner、plan與prompt語意不變。validator若在驗證時改動
  HEAD/index/worktree，新增的fail-closed處理是刻意的共通安全修正，不視為相容性回歸。
- `stack` 是新欄位；普通 loop只在遇到這個新語意時要求明確選 parallel或
  `--allow-serial-stack`，不影響舊資料。
- worker flag 都需完整組合與 supervisor manifest；一般使用者不能零碎打開某一旗標。
- 普通Loop/Ralph的agent argv、prompt與convergence流程不改；只在child前後增加repo-common owner marker
  lifecycle，並在取得primary lock時檢查parallel lease。Parallel遇legacy marker owner不明則
  fail-closed，不嘗試adopt不具operation lease的舊child。
- Dashboard/CLI既有checkout/add/commit argv與結果語意不變，但同樣包owner marker lifecycle；hard-kill
  後寧可要求recorded recovery，也不把短Git child當成不存在。
- Windows RepoExecutor/non-guardian direct child建立改為suspended後先進no-breakaway kill-on-close Job、
  durable publish identity再resume；managed worker則跨平台先啟動pipe-blocked inert Python bootstrap，
  Windows把bootstrap放入Job後依序acked、claim/authorize、GO。兩者都不改payload命令內容或正常結果。
- base state沿用既有 phase/loop/config/completed欄位，Dashboard可漸進投影，不另造一套
  completion truth；manifest/receipt只負責 orchestration與crash truth。

## 11. MVP 與延後項目

MVP 必須包含：

- stack schema、三處 invariant與普通 loop防靜默串行；
- post-Validate snapshot補強；
- supervisor、worktree/task branch、安全 sync ref與 canonical child config；
- worker assigned_order/reset/terminal/managed resume；
- gate IPC atomic claim/cancel、RepoExecutor single-writer、common-dir operation lease/child fence、prepared intent、receipt；
- managed worker launch spool、`guardian_ready/acked/reaped` child record、pre-GO claim/authorization barrier；
- run/task狀態機、structured block、reconcile、Pause/Resume/Abort、active-child fence/reap；
- common-dir non-guardian owner marker（Loop/Ralph/Dashboard/CLI launcher）、跨workspace-root orphan
  audit與明確fail-closed recovery提示；
- base/worker runner routing、backend readonly guard、最小 Dashboard launch/status/control；
- stack import驗證/badge/editor拒改、receipt-aware base task diff；
- durable finalization/report/notify outbox、parent-owned terminal cleanup；
- CLI/status/fleet投影與完整單元、整合、真實 worktree e2e。

延後：

- planner自動判斷/維護 stack；
- Plan Editor直接新增/重排 stack；
- DAG依賴、work stealing、跨 repo/主機；
- remerge threshold縮短；
- 甘特圖、拖曳排程、進階效能視覺化；
- hostile agent sandbox。

## 12. 實作階段與提交策略

規劃書收斂後先單獨 commit/push，再建立 `codex/parallel-workers` 分支。實作分段：

1. **Schema/loop safety**：stack validation、post-Validate snapshot、worker flags/state/prompt；
2. **Repo operation fence**：closed RepoExecutor API、central lock helper、common-dir lease、
   operation-child跨平台barrier/Job containment；
3. **Gate journal/recovery**：staged IPC、claim/cancel、intent/receipt/response與完整crash matrix；
4. **Supervisor lifecycle**：manifest、batch dispatch、managed worker guardian/child lease、reconcile、control IPC、pause/abort；
5. **CLI/Dashboard/UI**：launch/routing/readonly/status/task diff；
6. **Integration/e2e**：真 worktree、crash injection、Windows/POSIX lifecycle；
7. **Bug review**：逐輪 adversarial review與回歸，直到無已知 blocker/P1/P2。

每一階段通過該階段測試後各自 commit並 push；不把未通過測試的階段與下一階段混成一筆。

## 13. 測試計畫（全部為 MVP required）

### 13.1 單元與契約

- `validate_plan`：stack正整數/bool拒絕/同值兩段拒絕/連續放行；state/manifest load同規則。
- 普通 loop：含stack的import/state/resume預設拒絕，明確`--allow-serial-stack`且直接exec才串行；
  create-plan與plan→exec即使opt-in仍拒絕，舊plan與乾淨validator行為不變。
- validator寫 tracked、untracked、commit三種 side effect：本輪不得累計 done/gate。
- assigned_order：red/stall reset後仍回指定 task；不存在/不符 manifest fail。
- stop-after-task：不寫 phase done、不送全域 completed、不產全域 report。
- gate JSON：所有 status/rc、malformed、unknown、欄位不符；`pending/claimed/cancelled`只能做合法
  atomic transition，claimed後不得回busy。
- spool publish：consumer永遠讀不到未close/未fsync或partial JSON；duplicate request_id與
  publish-vs-claim/cancel競爭只有一個合法結果，staging殘留可安全回收。
- worker launch spool：`pending → claimed | cancelled`，只有guardian以ACKed payload identity重驗parent後
  可claim；response只接受綁同PID的`authorized|rejected`，未有`claimed + authorized`不得送GO。
- managed child record：`guardian_ready → acked → reaped`；cancel winner不得產生authorized、GO、
  console或worker `.run.lock`，claim winner的exact PID/birth/group/Job仍可fence。
- RepoExecutor operation lease：`reserved → running → terminal`；nested child各代嚴格
  `idle → launching → running → reaped → idle`，並驗nonce、generation、argv hash、identity/result與CAS。
- non-guardian owner marker：Loop/Ralph/Dashboard/CLI launcher各owner kind的
  idle/launching/child_running/child_reaped、host boot id、PID creation token與
  generation CAS；所有primary mutator拒絕別人的active/recovering marker，terminal→active與
  active→recovering不得blind replace；manual acknowledge只接受marker記錄的owner kind/workspace且寫audit。
- RepoExecutor authority table：preflight只認pending-launch、initialize refs認manifest/start SHA與
  expected-absent canonical ref、create/remove認manifest+assignment、gate另需claimed request、shutdown
  只認session；GATE_MERGE不啟validator且任意shell/path/ref均拒絕。
- prompt golden：普通模式無同步段/無 placeholder；worker含安全 ref且位置正確；合法與未知
  merge-in-progress兩條路；未知merge、human gate、task不可行/描述錯誤都會呼叫structured
  `work block`並停在assigned task，普通loop相同案例仍走既有`issue`。
- schema：runner/run_id/assignment/run config hash/launch spec hash/status transition/receipt chain/path
  containment；sync ref只能是canonical full ref `refs/heads/loop/<run_id>/integration`。
- canonical argv：自訂 goal/plan-doc/agent/validate/所有 threshold/timeout/notify/env初次與resume等價；
  resume不含 import-plan；artifact只保存allowlisted非secret env值，secret僅保存名稱/存在性。

### 13.2 supervisor/gate 整合

- 同 stack兩 task並行：A先merge，B stale → merge safe ref → 完整threshold → merge；最終
  primary含兩者且每個 receipt SHA都曾在乾淨tree上驗綠。
- 同時 gate request：claim與RepoExecutor/common-dir lock序列化，expected chain恰好逐筆前進。
- request deadline-vs-claim、Pause-vs-claim：只有atomic rename贏家可決定cancel或transaction；不得
  出現client收到busy/paused後才發生的ghost merge。
- unknown actor移動 integration/sync ref：blocked，不當 stale。
- crash injection：
  - request claimed後、prepared intent前；
  - prepared後、merge前；
  - merge後、safe sync ref CAS前；
  - safe sync ref CAS後、receipt前；
  - receipt後、success response前；
  - receipt後、aggregate前；
  - receipt存在但worker仍活著；
  - worktree cleanup失敗。
- recovery corruption matrix：success response無receipt、committed intent無receipt、cancelled request帶
  intent/receipt、hash或receipt chain不符皆blocked；receipt後已有後續合法receipt時仍可冪等補投影。
- operation lease在reserved/running與nested child launching/running/reaped/result各點kill supervisor；
  durable lease未fence/reconcile前任何新mutator都不能啟動第二個writer。
- startup在manifest寫後/ref CAS前、ref CAS後/init receipt前crash：只能冪等補同一canonical ref/receipt；
  ref已被未知actor建立或指向非start SHA則blocked。
- supervisor（連同in-process RepoExecutor）在active Git child期間被SIGKILL：新owner即使取得已釋放的global lock，
  central helper仍因nonterminal operation lease拒絕寫入；guardian/payload消失後才依HEAD/sync ref/
  intent確定性reconcile。
- worker在agent/validator child仍活時被SIGKILL：active-child identity可被adopt/fence，child消失與
  lock釋放前不得resume或cleanup。
- supervisor自己spawn的child用`Popen.wait()`；recovery採用的orphan以PID/session消失、worker/private
  lock釋放與active-child fence作reap證據，不假設仍可waitpid。
- supervisor死後舊RepoExecutor instance必然一同消失；若contained operation child仍活，新的in-process
  executor只可依nonterminal lease精確fence/reconcile原request，不得adopt舊instance或先派新operation。
- ordinary Loop/Ralph在child前marker已durable、正常reap/state checkpoint後才terminalize；以兩個不同
  workspace roots綁同repo，kill parent並保留child時，另一root的Parallel第一次audit即回
  legacy-orphan-unknown，checkout/goal/commit/pending artifact/preflight皆零mutation；handoff後第二次
  audit可抓住空窗新owner。只有原runner以generation CAS recovery成terminal/clean後才可啟動；普通
  runner的agent argv/prompt/convergence回歸測試維持不變；`pause-after-plan`與受控stop在child reap後
  可terminalize marker，既定「離線補stack再啟Parallel」路徑不被誤擋。
- Dashboard/CLI parallel launcher在checkout/add/commit child期間被SIGKILL：active marker持續阻擋
  ordinary/parallel/dashboard所有新mutator；child_running可fence，launching走boot/manual規則，bounded
  result與primary invariant checkpoint後才terminalize並允許handoff。
- marker crash windows逐一測：launching fsync後/Popen前、Popen後/identity publish前均保持
  owner-child-identity-unknown，普通新runner/Ralph/Dashboard/Parallel全拒絕且marker不變；只有boot id
  已變或明確manual recovery可接手。child_running publish後可依creation token fence/reap；child_reaped
  後、checkpoint前可冪等完成。每次kill都驗generation單調且沒有並行primary writer。
- worker child ACKed identity後、guardian claim/authorized/GO各點kill或Pause：cancel-vs-late-claim CAS；
  claim贏時exact identity可fence，cancel贏時不得有authorized、GO或payload workspace副作用；claim前後
  都重驗parent session/generation/attempt。
- fatal-invariant、stuck-stop、goal-missing不respawn；unexpected crash在budget內managed resume；
  同task第3次crash的restart_count跨supervisor restart仍持久並轉blocked。
- dirty between-round worker可managed resume；一般run/resume仍拒絕。
- Pause/Abort分別在 queued/provisioning/running/gate_pending/gate_claimed/integrated/cleaning時觸發；
  paused/blocked均須quiesce、清base PID、釋放base/primary lock並退出；Resume以新supervisor取lock後
  reconcile；已整合commit不rollback，所有child最後reap。
- cancelled task分別在clean、dirty與cleanup中途crash/restart：outcome持續為cancelled，
  resource_state獨立推進cleaned或cleanup_failed，絕不因resume cleanup而重新派工。
- finalizing在report寫入、notify送出、ack寫入各點crash：event_id/outbox可重播，report不重複分歧，
  notify維持at-least-once；cancelled只產partial report/cancelled notify且phase保持exec、不變成done。
- control IPC測live-owner claim/rejected-before-claim、owner在publish後死亡、no-owner bootstrap、
  Resume-vs-Abort併發、stale generation與duplicate request id；另逐點覆蓋bootstrap claim後/generation
  CAS前、aggregate save/base save間、claimed Abort遇後來Resume、finalizing Abort bytes不變，以及缺required
  secret但仍先fence live executor/child。任何路徑最多一個owner，且CLI/Dashboard不直接寫aggregate。
- terminal task只有parent確認child/lock消失且worktree安全後才archive/remove managed worker workspace；
  dirty、live cwd或觀測值改變時保留並轉cleanup_failed/blocked。
- worker container建立後、reservation/guardian/ACK前逐點kill：Abort可歸檔exact pristine container；一旦
  有payload identity，workspace/archive消失、child record未reaped或ancestor被link替換都必須blocked。
- launcher在handoff前被SIGKILL時保留same-repo owner marker；Resume不得暗中清除，須先走精確
  `recover-owner --acknowledge-child-gone`（或host reboot identity證據），再重新Resume/Abort。
- Dashboard checkout/goal commit在primary lock被占用時拒絕且不mutation。

### 13.3 CLI、Dashboard 與 UI

- Parallel Launcher含合法stack會啟 `engine.parallel`；普通Loop含stack明確拒絕。
- stack template、validation、badge、batch preview；Plan Editor前後端拒改且state byte-equivalent。
- base PID/status/Run/Resume/Stop/Abort依runner分流；完成後普通Run拒絕。
- Pause/Resume/Abort control process回傳唯一`job_id`並輪詢至exit 0；spawn成功本身不算操作成功，
  jobs list也用唯一id作UI key，避免同workspace多個control互相覆蓋。
- 兩個Dashboard process同時提交不同plan時，各自以`O_EXCL`建立唯一staged artifact並綁定exact raw
  SHA-256；勝出的supervisor只能在base `.run.lock`內讀取、驗證自己argv指定的artifact/hash，不得消費
  另一筆submission，也不得在驗證前建立run artifact或ref。
- worker所有mutation API與CLI命令被中央guard拒絕；逐一覆蓋CLI run/restart/resume/config/delete/
  stop/init-force/check與直接`engine.loop` reset/import/preflight，皆在validator/repo mutation前失敗且
  state byte-equivalent；UI只讀且不出現Resume/Edit/Delete。
- base在`RUN_NONTERMINAL`每個狀態（包含initializing/finalizing與pause/cancel transition）都拒絕
  Edit/Import/Phase/Set-task/Delete/普通Run，PID空缺也不得放行且state byte-equivalent。
- worker只顯assigned task，managed child不污染fleet totals/attention。
- base completed/task diff使用primary repo與receipt range；worktree刪除後仍可讀。
- terminal啟動的external supervisor也能被Dashboard正確辨識、pause/resume，不誤送loop drain marker。

### 13.4 真實 e2e 與回歸

- 真實 Git linked worktree + fake agent CLI跑完整兩-worker batch，驗 worktree建立/清理、branch
  留存、run_id隔離、safe ref與base report。
- 至少一條從Dashboard API/UI launch到supervisor/worker/receipt/completed的完整路徑。
- Windows與POSIX相容層測process group、lock/PID辨識、路徑containment；Windows RepoExecutor/
  non-guardian direct child驗`CREATE_SUSPENDED → assign no-breakaway Job → publish → resume`，managed
  worker另驗`inert bootstrap → Job attach → acked identity → claim/authorized → GO`。用立即產grandchild的
  adversarial child驗正常/kill路徑都不會在Job外殘留。POSIX驗兩種barrier-before-exec，non-guardian另驗
  session/group消失前marker不可terminal；兩平台都驗common-dir lease/marker會擋住新primary writer。
- 執行既有完整 Python與UI test suite，確認普通 loop與Ralph沒有回歸。
- 功能完成後至少兩輪獨立bug review：concurrency/crash一輪、UI/compat一輪；所有發現修正、
  補回歸測試，直到沒有已知 blocker/P1/P2。
