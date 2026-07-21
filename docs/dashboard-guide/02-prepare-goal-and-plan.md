# 流程 02：準備 Goal 與 Plan

## 目的

在啟動 loop 前，準備「不可由 Agent 擅自改寫的目標」與「可以逐項執行的任務計畫」。Goal 是真相來源；Plan 是執行順序。寫得越清楚，後面的多輪 Agent 越容易一致判斷完成條件。

## Goal 與 Plan 的角色

| 內容 | 用途 | 是否必須 |
|---|---|---|
| `goal.md` | 描述背景、目標、範圍、限制、驗收條件與非目標 | 必須有可用版本；通常應 commit |
| `plan.json` | 由 `order`、`task`、選填 `ref` 組成的純任務陣列 | 選填；留空可讓規劃期 Agent 從零建立 |
| `PLAN.md` | 人類可讀的既有計畫／分析文件 | 專案可自行使用；啟動表單真正匯入的是 JSON plan |

## A. 準備 `goal.md`

### 建議的八個段落

1. 背景與問題。
2. 目標。
3. 非目標。
4. 功能需求。
5. 限制與不可破壞事項。
6. 成功條件／Scenario Criteria。
7. 驗收條件／Acceptance Criteria。
8. Definition of Done（實際命令與可觀測結果）。

DoD 不要只寫「測試通過」，應寫可執行的命令與預期結果，例如：

```markdown
## Definition of Done

- `python3 -m unittest discover -s tests -t . -q` exit 0。
- 新功能的正常、邊界與錯誤輸入都有測試。
- 工作樹乾淨，沒有未追蹤的驗證產物。
- 不新增第三方依賴。
```

### 最穩定做法：在 target repo commit Goal

```bash
git add goal.md
git commit -m "docs: define loop goal"
```

啟動表單的 `goal.md` 欄位留空時，Dashboard 沿用 repo 已 commit 的版本。這最容易稽核，也讓 preflight 能確認 Goal 的 Git 狀態。

### 要用新檔案時

在啟動表單 `goal.md` 欄選擇 `.md`、`.markdown` 或 `.txt` 檔。啟動會先做路徑與格式安全檢查，再處理 branch 或 state；Goal 錯誤不會留下半套 branch mutation。

## B. 使用 Goal 產生器（選用）

![Goal 產生器 Prompt 實際畫面](../assets/dashboard-guide/raw/goal-prompt-generator.jpg)

「Goal 產生器 Prompt」不會直接寫入 repo。它只在瀏覽器根據你的需求、專案限制與任務類型，產生一段可以交給另一個 Agent 的 Markdown prompt。

操作：

1. 點 `goal.md` 右側「Goal 產生器 Prompt」。
2. 選擇任務類型；不同類型會加入相應的分析要求。
3. 填「需求」：說明真正要解決的問題與預期結果。
4. 「專案限制」選填：語言、相容性、不可更動範圍、效能或安全限制。
5. 選填：勾選「同時產生初版 plan.json」。prompt 會在 goal.md 契約後附上 plan.json 拆分規則，Agent 會先輸出 goal.md，再輸出一行 `===== plan.json =====` 分隔線與初版 plan.json，方便人工拆成兩個檔案。
6. 產生並複製 prompt，交給適合的 Agent。
7. 審查 Agent 產出的 `goal.md`（若有勾選，連同初版 `plan.json`），不要未讀就匯入。

「Goal 成果模板」則是八段契約的參考骨架：

![Goal 成果模板實際畫面](../assets/dashboard-guide/raw/goal-template.jpg)

兩者差異：產生器是「請 Agent 寫 Goal 的指令」；成果模板是「Goal 最後應長什麼樣」。

## C. 準備 `plan.json`（選用）

合法格式：

```json
[
  {
    "order": 1,
    "task": "實作登入狀態儲存；加入正常、過期與竄改 token 測試；執行 pytest -q 全綠。",
    "ref": "goal.md#驗收條件"
  },
  {
    "order": 2,
    "task": "更新 API 文件與遷移說明，並執行文件連結檢查。"
  }
]
```

格式規則：

- 最外層必須是非空陣列。
- 每一項必須是物件。
- 只允許 `order`、`task`、`ref`。
- `order` 必須是整數、不得重複，且從 1 連續到項目數。
- `task` 必須是非空字串。
- `ref` 可省略、設為字串或 `null`。
- 不接受 `done`、`completed`、SHA 或整份 state；Plan 匯入不能偽造完成進度。

啟動表單可按「複製 JSON 範本」，或用「產生 Plan Prompt」：

![Plan Prompt 產生器實際畫面](../assets/dashboard-guide/raw/plan-prompt-generator.jpg)

Plan Prompt 的輸出契約只接受純 JSON array。產出後仍要人工檢查：

- 任務是否依依賴順序排列。
- 每項是否能由沒有前後文的工程師直接動工。
- 每項是否有明確驗收方式。
- 任務大小是否合理；不要把整個專案塞成一項，也不要拆成無法獨立驗證的微小動作。
- `ref` 是否真的存在；沒有真實參考就省略。

## D. 選擇匯入後的起始階段

只有貼入合法 `plan.json` 時，畫面才出現：

- 「規劃期」：讓 Agent 再審核／調整計畫，適合計畫尚未經多方確認。
- 「直接執行期」：直接從 task-1 開始，適合計畫已由人審核且驗收條件明確。

匯入會建立全新 state，不是把任務附加到舊進度。若只想調整停止 workspace 的 pending tasks，應使用 [Plan 編輯器](08-edit-plan-and-change-task.md)。

## 啟動前自我審查

- [ ] Goal 寫的是結果與限制，不是逐步替 Agent 微操。
- [ ] DoD 含實際可執行命令與清楚通過條件。
- [ ] Goal 已 commit，或明確選了要匯入的新檔。
- [ ] 若有 Plan，JSON 格式無紅字且 order 連續。
- [ ] 每一項 task 都能獨立實作、驗證與 commit。
- [ ] 起始階段選擇符合計畫成熟度。

下一步：[啟動新的 loop](03-launch-new-loop.md)。
