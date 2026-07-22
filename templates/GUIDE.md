# 初版計畫產出指引(在 loop 之外的一般 agent session 使用)

Dashboard 的「啟動／管理」可直接產生並下載兩種外部 Agent Prompt：Goal 產生器 Prompt 輸出
`goal.md`，Plan 拆分模板輸出可直接匯入的基礎 `plan.json`。兩者共用下列盤點與 DoD 骨架，
只在最後套用不同輸出契約；內建任務類型也涵蓋本目錄原有的 Java／EJB／JSP 模板。
Parallel 頁的 Plan Prompt 會額外要求 task 寫出人工並行審查所需的 repo 證據，但 Agent 仍不得
自行產生 `stack`；`stack` 是人類審完基礎 Plan 後才加入的執行拓撲。
固定契約維護在 `engine/prompts/external-agent-*.md`，前端不保存另一份副本；團隊可在
`dashboard.config.shared.json` 的 `prompt_templates` 新增專屬任務指引。

這份指引給「協助人類產出初版計畫」的 agent 看。產出物三件:

1. `goal.md` — 大目標,刻意粗略(例:API 全搬、邏輯等價),不要發明過度嚴格的限制。
   Goal 描述結果、範圍、限制與驗收,不指定 worker、batch 或 `stack`;普通與 Parallel 共用同一份真相。
   人類審完後 commit 進 target repo(或用 dashboard 的匯入按鈕代 commit)。
2. `plan.json` — 任務清單,格式 `[{"order":1,"task":"…","ref":"docs/analysis.md#段落"}]`:
   order 從 1 連續遞增;task 字數不限,寫到無前後文的工程師能動工(含 DoD);ref 選填。
   外部 Agent 的輸出只含 `order/task/ref`,不得推論或輸出 `stack`。人類審完後**貼進 dashboard
   啟動表單**:普通 Loop 可選規劃期或直接執行期;Parallel 必須先人工補好需要的 `stack`,
   再以 frozen plan 固定從執行期開跑。
3. (選配)深度分析文件 — 盤點表/行為規格等寫成 markdown commit 進 repo 任意路徑,
   由 plan.json 的 ref 指過去。**不要**叫 PLAN.md 放根目錄當「計畫」——計畫的真相只有 plan.json。

## Parallel Plan 的人工標註規則

基礎 Plan 產出後,由人類依 repo 與執行環境證據決定哪些**連續 tasks**可同批執行:

```json
[
  {"order":1,"task":"獨立任務 A;明列 working set 與 DoD","stack":1},
  {"order":2,"task":"獨立任務 B;使用隔離的驗證資源與 DoD","stack":1},
  {"order":3,"task":"依賴 A/B 結果的整合任務;DoD: ..."}
]
```

- `stack` 可省略;存在時必須是正整數,boolean 不允許。
- 相同 `stack` 只能出現在一個連續 order 區段;該區段是一個 batch。
- 未標 `stack` 的 task 自成 singleton batch;只給單一 task 一個 stack 仍不會產生並行。
- batches 依最小 order 串行;同一 batch 內才由 supervisor 依 `max_parallel` 派工。
- 同 stack tasks 的 working set、schema、生成物與語意/資料依賴不得重疊。
- validator 使用的 port、DB/schema、Docker Compose project、cache、lock、外部服務或全域環境必須隔離。
- 任一項證據不足就不標 stack,維持串行;不要為了看起來有並行而拆散不可獨立驗證的工作。

Dashboard 普通 Loop 會拒絕含 `stack` 的 plan,避免格式通過後靜默串行。Parallel 則要求非空 frozen
plan 並固定從 exec 起跑;planner 不會自動新增、刪除或重寫人工 stack。

## 通用骨架(每份初步規劃書都要有這四段)

1. **盤點(Inventory)**:把「全部」變成可枚舉的清單,一列一個單位(頁面/EJB/API/模組)。
   **清單沒有的列,就沒有「搬完/做完」的定義**——fail-closed 的前提是先有完整清單。
2. **行為規格**:每個單位寫 輸入→輸出、驗證規則、錯誤處理、邊界行為;證據一律附 `檔案:行號`,
   不准憑印象寫。
3. **拆件決策規則**:把「哪類東西放哪裡」的規則寫死在文件裡,讓執行 agent 不用猜。
4. **任務化 → plan.json**:把工作切成「一個無前後文的 agent 一輪做得完」的粒度,依依賴順序排列,
   每條附驗收標準(DoD),直接產成 plan.json。盤點/規格細節留在分析文件,任務用 ref 指過去。
   若這份 Plan 將交給 Parallel,每項 task 還要寫出可由 repo 證實的 working set、會讀寫的檔案/
   schema/生成物、與其他 order 的語意或資料依賴,以及 validator 共享資源的隔離方式;這些是給
   人類判斷 stack 的證據,不是要求 Agent 自行宣告「可並行」。
   普通 Loop 的規劃期 agent 會在此基礎上補漏(除非人類選擇直接進執行期);Parallel 不走規劃期。

## DoD 鐵律

- 能進 loop 當 validate 的 DoD 必須是**一條命令、exit code 判定**(如 `mvn test`)。
- 機器驗不了的(視覺還原度、UX 手感)不要塞進迴圈,列在「人工驗收」段留給人。
- 建議把「建測試骨架」排成最前面的任務,後面每個任務才有地方放證據。

## 完整性 gate(寫進規劃書結尾)

盤點表每一列,最終都要對得到「已完成任務 + 測試證據(檔:行)」。對不上的列 = 沒搬完。

## 與 loop 的銜接

- 普通 Loop:goal.md commit 進 repo(dashboard 可代 commit)→不含 stack 的 plan.json 貼進
  `Loop coordinator` 啟動表單→選規劃期或直接執行期→啟動。CLI 等效:先啟動一次讓計畫落 state,
  或走規劃期讓 agent create-plan。
- Parallel Loop:goal.md 必須已 commit 在目前 branch→人類審基礎 Plan 並補必要 stack→貼進
  `Parallel Loop` 啟動表單→固定從 exec 啟動。Supervisor 會建立受管 linked worktrees/task branches;
  不要自行啟多個普通 Loop 或讓 worker 直接合併 primary/peer branches。
- 普通 Loop 選規劃期起跑時,10 個 fresh-context agent 會連續獨立檢驗這份計畫,
  等於十雙眼睛掃過盤點是否有漏——初版可以粗,但骨架四段不能缺;
  對計畫有把握(或人已細審)就直接執行期,省下規劃輪。Parallel 不走這段規劃期,因為 v1 的 planner
  不理解人工 stack 語意。

Parallel 執行中的 lifecycle 由 parent supervisor 管理:Pause 在安全邊界停止並保留可續跑現場;
Resume 先 reconcile durable receipts/gate/worker identity 再繼續;Abort 保留已整合 commits、取消未整合
tasks,只清理可證明安全的 worktrees。Managed worker workspace 是唯讀,不要手改其 state、refs 或 artifacts。
