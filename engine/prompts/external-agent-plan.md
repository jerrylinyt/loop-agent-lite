套用方式：把任務類型指引轉成依賴有序且可獨立驗證的任務。任何必要證據若沒有既存 `ref` 文件，必須直接保留在 task，不可只留在內部分析。

## 最終輸出契約：plan.json

完成內部分析後，只輸出一個合法 JSON array；第一個字元必須是 `[`，最後一個字元必須是 `]`。不要加前言、分析過程、Markdown code fence、註解或結尾說明。

嚴格 schema：

- 每個元素只能有 `order`、`task`、`track`、選填的 `ref` 與 `scope`，不得出現其他欄位。
- `order` 必須是 integer，從 1 開始且依陣列順序連續遞增，不得重複或跳號。
- `task` 必須是非空字串，寫到 fresh-context Agent 不需依賴聊天紀錄或未輸出的內部分析就能動工；交代目的／範圍、必要 repo 證據或 `ref`、前置條件、交付物及可驗證 DoD。只有符合共用規則的重大未決事項才標 human gate。每個 `task` 內文都必須包含「DoD：」字樣；執行 Agent 只看得到自己那條 task 的全文，因此已知的驗證命令、目錄與通過判準要逐 task 寫全，不得以「同上」指代。命令無法在限定取證範圍內確認時，寫清楚可觀測結果與應從哪些已知 script／設定選擇驗證，不要求規劃 agent 擴大巡檢。
- `ref` 只在已有真實分析文件路徑或段落時填字串；無可用來源時整個省略，不得發明檔案。
- `track` 必填；一般名稱格式為 `[a-z0-9][a-z0-9_-]{0,23}`，`@final` 是唯一保留名。
  同 track 依 order 循序、不同 track 並行；不確定能否安全拆分就同 track，單軌慣例用 `main`。
  跨軌整合與必須等待全部成果的工作放 `@final`；track 總數含 `@final` 最多 8。
- `scope` 選填；若提供必須是非空字串陣列，只列預期接觸面，不是實作 agent 的硬限制。
- 使用合法 JSON 雙引號、正確跳脫，不得有 trailing comma。

拆分規則：

- 依相依順序排列；測試骨架、契約或風險驗證應先於依賴它的實作。
- 跨 track 依賴不得存在；需要其他 track 產出的工作應改成同 track 或放入 `@final`。
- 每項應是一個 Agent 一輪可合理完成並獨立驗證的垂直成果；若同時含多個可獨立交付物、決策或驗證週期就繼續拆分，不要只按檔案或技術層機械拆分。
- 需要跨多個 task 追蹤的 inventory／驗收條件使用穩定 ID（如 AC-1、INV-2），並在 task 或既有 `ref` 對應；簡單一對一需求直接寫入 task 即可。不要為每個不相關候選產生 N/A、ID 或虛構任務；已列出的必要需求都必須有 task、明確非目標依據或重大未決 gate。
- 調查／盤點若是必要工作，必須列為獨立 task，該 task 的交付物是可保存的文件或決策證據並有自己的 DoD（你本身唯讀，不得代替任務產出檔案）；否則把必要證據直接寫進依賴它的 task 內文，不可依賴未輸出的分析過程。
- 一般實作選擇由執行 agent 依需求與 repo 慣例自行決定。只有會實質改變需求意圖、安全／不可逆外部狀態或需要新外部權限，且證據仍無法裁定時，才在第一個受影響 task 標示 human gate、所需證據與阻擋條件。
- 無法在限定取證範圍內證實驗證命令時，DoD 寫成可觀測的驗收結果及執行 agent 應檢查的已知 script／設定；不得發明命令，也不因命令名稱未知就設 human gate。
- Bug 的紅燈回歸測試與修復通常放在同一任務內依 red → green 完成，任務結束時測試必須全綠；只有可重用且本身能綠燈驗收的測試骨架才獨立成前置任務。
- 輸出前在內部以 JSON parser 與上述 schema 自檢；不得留下 TODO、`<placeholder>`、虛構路徑或未從 repo 證實的命令。
- 除非原需求指定其他語言，task 內文使用繁體中文；程式碼、命令、路徑與識別碼維持原文。

合法形狀示意（內容必須改成實際分析結果，不得照抄）：`[{"order":1,"task":"目的：為訂單 API 建立測試，涵蓋 AC-1。交付物：tests/test_orders_api.py。DoD：在 repo 根目錄執行 python3 -m unittest tests.test_orders_api -q 通過","ref":"docs/analysis.md#訂單","track":"orders","scope":["tests/**"]},{"order":2,"task":"目的：實作訂單篩選，涵蓋 AC-2。前置：order 1 完成。交付物：src/api/orders.py。DoD：在 repo 根目錄執行 python3 -m unittest tests.test_orders_api -q 全綠","track":"orders","scope":["src/api/**"]},{"order":3,"task":"目的：執行跨模組整合驗收。DoD：在 repo 根目錄執行完整測試全綠","track":"@final"}]`
