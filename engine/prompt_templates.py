"""Dashboard 設定投影使用的外部 Agent prompt 模板目錄。

共用分析核心與 Goal/Plan 輸出契約放在 package prompt 資源，由 UI builder 只做安全替換；
團隊設定只能追加任務指引，不能取代系統契約。無效模板或資源會 fail-closed，
但不讓整個 Dashboard 失效。
"""
from functools import lru_cache
from importlib import resources
import re


MAX_TEAM_PROMPT_TEMPLATES = 50
TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PROMPT_PLACEHOLDER_RE = re.compile(r"<<[A-Z][A-Z0-9_]*>>")
PROMPT_MARKER_RE = re.compile(r"<<[^\r\n]*?>>")
MAX_PROMPT_RESOURCE_CHARS = 100_000
PROMPT_TEMPLATE_BUNDLE_SCHEMA_VERSION = 1
LIMITED_DISCOVERY_PREFIX = (
    "- 取證邊界：只使用原始需求、project context、既有 ref 或已知錯誤直接點名的路徑，"
    "並只在這些 source/test 目錄追蹤直接關聯；下列「盤點／全部／逐項」都只指這個已知邊界，"
    "不授權全 repo 列檔、廣域搜巡或讀 generated/dependency/build 產物。"
)
PROMPT_RESOURCE_SPECS = {
    "base": (
        "external-agent-base.md",
        {
            "<<OUTPUT_NAME>>": 1,
            "<<ORIGINAL_REQUIREMENT_JSON>>": 1,
            "<<PROJECT_CONTEXT_JSON>>": 1,
            "<<TEMPLATE_LABEL_JSON>>": 1,
            "<<TEMPLATE_DESCRIPTION_JSON>>": 1,
            "<<TEMPLATE_INSTRUCTIONS_JSON>>": 1,
            "<<MODE_CONTRACT>>": 1,
        },
        True,
    ),
    "goal": ("external-agent-goal.md", {}, False),
    "plan": ("external-agent-plan.md", {}, False),
    "missing_requirement": ("external-agent-missing.md", {"<<OUTPUT_NAME>>": 2}, False),
    "default_context": ("external-agent-default-context.md", {}, False),
    "team_template_example": ("external-agent-team-template-example.md", {}, False),
}
TEAM_TEMPLATE_KEYS = {
    "id", "label", "category", "description", "instructions", "requirement_placeholder",
}


BUILTIN_PROMPT_TEMPLATES = [
    {
        "id": "new-feature",
        "label": "開發新功能",
        "category": "開發",
        "description": "把產品需求轉成有邊界、可驗證且能逐步交付的目標或任務。",
        "requirement_placeholder": "例：新增可依狀態篩選 workspace 的功能，並保留重新整理後的選擇。",
        "instructions": """- 先找出已知範圍內的既有使用流程、相鄰功能、資料來源與可重用元件，不要另造平行架構。
- 驗收條件需要跨多個 task 追蹤時才編穩定 ID；簡單需求直接在 task 內寫清楚入口、正常／空／錯誤狀態與相容性影響，不為格式增加 ID。
- 只列實際有證據受影響的 UI、API／資料契約、服務邏輯、持久化、權限或背景工作；不要求固定六層矩陣，也不為未涉及層逐項製造 N/A。
- 優先切出可獨立驗證的垂直功能片段，避免只按檔案或技術層拆任務。""",
    },
    {
        "id": "bug-fix",
        "label": "診斷並修復 Bug",
        "category": "開發",
        "description": "從可重現證據定位根因，建立回歸測試後修復。",
        "requirement_placeholder": "例：完成的 workspace 仍顯示需關注，請找出原因並修正。",
        "instructions": """- 先定義最小重現條件、預期與實際結果，包含版本、資料、權限、時間或競態等必要前提；沒有重現證據時要標為待確認。
- 沿資料流追到第一個錯誤狀態的產生點，區分根因、放大因素與表面症狀。
- 同一任務內先建立會失敗的回歸測試再修到全綠；若無法穩定自動重現，規劃 instrumentation／replay 證據並說明限制，不得虛構紅燈測試。
- 檢查同一規則是否在其他入口重複實作，避免只補畫面上的症狀。""",
    },
    {
        "id": "behavior-preserving-refactor",
        "label": "保留行為的重構",
        "category": "開發",
        "description": "先釘住既有行為，再安全調整結構、責任或可維護性。",
        "requirement_placeholder": "例：整理 Dashboard 的統計投影邏輯，降低重複但不改變 API 行為。",
        "instructions": """- 先寫清楚重構動機與可量測的改善目標，再列出不可改變的外部行為、公開介面、資料格式、錯誤語意與副作用。
- 找出目前能證明行為且不綁內部實作的測試；不足處先補 characterization tests，並要求同一套測試在改動前後通過。
- 說明新的責任邊界、依賴方向與遷移順序，避免一次性大改造成不可定位的回歸。
- 將純結構改善與任何可觀察行為變更分開；後者必須明列為需求決策。""",
    },
    {
        "id": "project-logic-analysis",
        "label": "分析專案架構／邏輯",
        "category": "分析",
        "description": "在需求指定邊界內建立模組地圖，再深追指定流程的依賴與狀態真相來源。",
        "requirement_placeholder": "例：分析整個專案如何從 Dashboard 啟動 loop，以及狀態如何回到 Overview。",
        "instructions": """- 先界定要回答的架構問題、深追流程與排除項；只盤點需求/context/ref 已指向的模組，不自行擴張成 repo-wide 地圖。
- 從已知範圍內的目錄、建置檔、啟動入口與設定檔建立模組清單，說明每個模組責任及依賴方向。
- 對需求指定流程追蹤入口 → 核心邏輯 → 持久化／外部程序 → 消費端投影，標出狀態真相來源。
- 盤點跨模組契約、生命週期、錯誤處理、並行或鎖定邊界，以及測試覆蓋位置。
- 將直接從程式讀到的事實與推論分開；推論要附理由並列出仍需驗證的證據。""",
    },
    {
        "id": "code-logic-analysis",
        "label": "分析指定程式／呼叫鏈",
        "category": "分析",
        "description": "針對明確符號或事件，在界定範圍內追出 inbound callers、outbound callees、控制流與資料流。",
        "requirement_placeholder": "例：分析 dashboard.py 的 fleet_health_projection，包含所有呼叫端與需關注判定。",
        "instructions": """- 以檔案、完整符號／signature 或事件名稱鎖定目標，列出同名或 overload 歧義及選定依據。
- 在需求範圍內同時追 inbound callers 與 outbound callees，整理輸入來源、轉換步驟、輸出與副作用。
- 列出分支條件、預設值、例外路徑、早退、狀態變更、檔案／網路 I/O 與並行邊界。
- 對每個結論附 `檔案:行號`，無法定位者標為待確認；外部套件、generated code、reflection、事件訂閱或動態 dispatch 要標出停止邊界與未確認路徑。
- 說明既有測試如何覆蓋這條呼叫鏈，以及哪些邊界目前沒有證據。""",
    },
    {
        "id": "test-gap-analysis",
        "label": "分析測試缺口",
        "category": "品質",
        "description": "把需求、風險分支與既有測試做可追蹤的覆蓋對照。",
        "requirement_placeholder": "例：分析 workspace 永久刪除與中斷續刪流程的測試缺口，優先找誤刪或資料殘留風險。",
        "instructions": """- 先枚舉可觀察行為、風險分支與失敗模式，再對照現有 unit／integration／e2e 測試及 CI 實際執行路徑。
- 將缺口分類為完全缺少、斷言過弱、flaky、skipped、只證明 mock 或未被 CI 執行；不以單純 coverage 百分比代替分析。
- 建立需求或分支 → 測試檔與案例 → 缺口的追蹤關係，為每項選擇最低但足以可靠攔截回歸的測試層。
- 優先處理資料損壞、安全邊界、競態、錯誤恢復與曾發生的回歸。
- 說清楚 fixture、mock 與真實整合環境的界線，避免測試只證明 mock 本身。""",
    },
    {
        "id": "java-test-completion",
        "label": "補齊 Java 測試案例",
        "category": "品質",
        "description": "為既有 Java 專案的指定範圍補齊測試；unit／integration 層級依需求選擇，測試釘行為而非實作。",
        "requirement_placeholder": "例：為訂單服務的付款流程補齊測試，integration 用真實資料庫驗證交易行為，unit 覆蓋金額計算邊界。",
        "instructions": """- 先枚舉範圍內的可觀察行為與風險分支（正常、邊界、錯誤處理、並行），對照既有測試找出真缺口；已覆蓋的行為不重寫，缺口逐條對應到任務。
- 測試層級以需求指定為準（unit／integration 擇一或並用）；需求未指定時選擇「最低但足以可靠攔截回歸的層級」並附理由，不得全部堆到最上層。
- 從 repo 讀出實際測試棧與慣例（JUnit 版本、assertion 庫、命名、目錄結構、真實資料庫或內嵌替身、CI 實跑範圍），沿用不另創；repo 缺少但可在本次範圍內建立的測試設施，規劃成前置任務並由 agent 依需求與 repo 慣例選最小可用方案。只有建立設施會改變需求意圖、造成不可逆外部狀態或需要新外部權限時才列 human gate。
- unit 測試釘行為不釘實作：斷言輸入→輸出與副作用，不 mock 被測邏輯自身；integration 測試優先用真實依賴（資料庫、訊息、交易邊界），mock 只用於不可控外部系統並標明邊界，避免測試只證明 mock 本身。
- 每個新測試需先確認會因對應行為破壞而變紅（暫時破壞或 mutation 驗證），不得交付恆綠測試；時間、隨機、順序等 flaky 來源在測試內固定。
- DoD：範圍內行為條目全數對應到測試與 `檔案:行號`，以 repo 實際測試命令全綠為準；無法自動化的行為明列原因與替代驗證。""",
    },
    {
        "id": "react-playwright-testing",
        "label": "React Playwright 測試（mock 資料）",
        "category": "品質",
        "description": "用 Playwright 與受控 mock 資料為 React 前端補齊流程測試，斷言使用者可見行為而非內部實作。",
        "requirement_placeholder": "例：為訂單查詢頁補 Playwright 測試，API 一律用 mock 資料，涵蓋篩選、分頁、空資料與錯誤狀態。",
        "instructions": """- 先枚舉範圍內的使用者流程與狀態（載入、空資料、錯誤、權限、互動回饋），對照既有測試找缺口；未列入清單的流程不得視為已覆蓋。
- 從 repo 讀出實際 Playwright 設定與慣例（config、fixture、selector 策略、CI 實跑範圍）沿用之；repo 尚無 Playwright 時把初始化列為獨立前置任務，不與測試撰寫混在同一任務。
- mock 資料策略集中管理：用 Playwright network interception（route／fulfill）或 repo 既有 mock server 固定 API 回應；mock 形狀必須以真實 API 契約為依據並附契約來源（型別定義、schema 或後端程式 `檔案:行號`），不得自創欄位。
- 斷言使用者可見行為（文字、可及性角色、URL、可互動狀態）；selector 優先 role／label／testid，不綁 CSS 結構，不斷言元件內部 state 或實作細節。
- 等待一律用 Playwright 自動等待與明確條件，禁止固定 sleep；時間、隨機與動畫來源在測試內固定；每條測試先確認會因對應行為破壞而變紅。
- DoD：流程條目全數對應到測試與檔案位置，以 repo 實際 Playwright 命令全綠為準；mock 與真實契約的漂移風險明列，且逐項三選一：規劃最小 smoke 對真後端驗證；既有防線已鎖住同一契約（mock 由契約 schema 自動生成，或 CI 既有真後端契約測試）時附證據引用之，不另立任務；兩者皆不可行時列 human gate。不得三者皆無。""",
    },
    {
        "id": "characterization-test",
        "label": "為 legacy 模組鎖定現況行為",
        "category": "品質",
        "description": "在不改變行為的前提下為缺測試的遺留模組建立 characterization 測試，把現況行為（含既有怪異行為）釘成後續重構與遷移的等價基準。",
        "requirement_placeholder": "例：為訂單模組的計價與稅額邏輯建立 characterization 測試，改動前先鎖住現況行為，作為後續重構的等價基準。",
        "instructions": """- 先界定要鎖定的模組範圍與公開表面（public method、API、批次入口），逐表面枚舉可觀察輸出與副作用（回傳值、DB 寫入、訊息、檔案）；未列入清單的表面不得視為已鎖定。
- 鐵律：只記錄現況、不判斷對錯——斷言以實際執行結果為準，包含看起來像 bug 的行為；疑似 bug 另列清單交人裁決，不得順手修。禁止的是行為變更與靜默修復；保持外部可觀察行為不變的最小可測性調整（如 seam 提取、依賴注入點）允許，但需明列並以對照執行證明行為未變。
- 輸入組合以邊界與代表值系統化枚舉（正常、邊界、空值、非法、極端量）；行為受外部依賴（時間、隨機、DB 狀態、外部服務）影響時，先以可控 fixture 固定依賴並記錄固定方式，不得留下不穩定斷言。
- 斷言粒度取「足以偵測行為改變」的最粗層次：優先斷言輸出與持久化結果，不斷言內部呼叫順序或私有狀態，避免測試綁死實作、阻礙後續重構。
- 觀察點不足時允許最小侵入的觀察手段（讀資料庫、攔截出站呼叫），仍不得改動業務邏輯；確實無法安全觀察的行為明列為未鎖定邊界與 human gate。
- DoD：範圍內表面全數對應到測試與 `檔案:行號`，repo 實際測試命令全綠且重複執行穩定；「疑似 bug 現況」清單與未鎖定邊界隨成果一併交付。""",
    },
    {
        "id": "api-contract-testing",
        "label": "補齊 API 契約測試",
        "category": "品質",
        "description": "把對外 API 的請求／回應契約（狀態碼、錯誤格式、欄位語意、相容規則）鎖成可重跑的測試，作為重構與遷移的等價防線。",
        "requirement_placeholder": "例：為訂單服務全部對外 REST API 補契約測試，鎖住狀態碼、統一錯誤格式與分頁行為，作為搬移到 Spring Boot 3 的等價基準。",
        "instructions": """- 先盤點範圍內全部對外端點（路徑、方法、版本），逐端點枚舉成功、驗證失敗、授權失敗、資源不存在、衝突與伺服器錯誤的實際回應。
- 契約權威性逐項判定並記錄依據：
  - 已有權威契約（OpenAPI、CDC 契約）時，實作與契約不一致即為測試失敗或明確的治理決策，需 fail／escalate 不得靜默放行。
  - 沒有權威契約時以實際行為為基準建立相容防線，文件與實際行為的不一致列為發現。
  - 同一端點存在多份契約（多版 OpenAPI、CDC、文件）互相衝突或權威不明時，不得自行選定權威：列出衝突各方與 `檔案:行號`，先以實際行為鎖相容基線，權威判定依共用規則升級——Goal 列入待確認事項，Plan 在第一個受影響 task 標 human gate。
- 逐端點鎖定請求側與回應側：
  - 請求側含接受的 Content-Type、path／query／header／body 的必填與選填參數、request body schema 與驗證語意。
  - 回應側含狀態碼、回應 schema（欄位名、型別、必填性、null 語意）、錯誤 body 格式與錯誤碼、預設值與分頁／排序行為、Content-Type 與編碼。
  - repo 有統一回應包裝時其形狀在共用測試工具集中斷言，不在每條測試重複；沒有時省略這個軸，只在整體取證邊界摘要一次，不逐端點製造空項。
- 斷言「對外可觀察的形狀與語意」，不斷言內部實作；欄位比較用結構化比對並明列忽略欄位——僅限每次執行必然變動且不承載契約語意的欄位（如時間戳、trace id、隨機識別碼），狀態碼、錯誤碼與業務欄位不得列入忽略；不得整包字串 snapshot，避免雜訊改動造成全紅。
- 測試環境優先沿用 repo 既有整合測試棧（如 MockMvc／WebTestClient 或真實服務加真實資料庫）；依賴外部系統的端點以受控替身固定，替身形狀附真實契約來源，不得自創欄位。
- 相容規則明確化：新增欄位是否破壞消費端、未知欄位如何處理、enum 擴充語意，逐項寫成測試或明列為未定義行為交人裁決；版本共存窗口的行為差異分開鎖定。
- DoD：限定範圍內已由證據確認的端點行為都對應到測試與 `檔案:行號`，repo 實際測試命令全綠；只整理真正存在的文件／實際行為差異與重大未決 gate，不為未涉及情境補固定矩陣或空列。""",
    },
    {
        "id": "performance-analysis",
        "label": "分析效能瓶頸",
        "category": "品質",
        "description": "用可量測基準定位時間、記憶體或 I/O 熱點並規劃驗證。",
        "requirement_placeholder": "例：分析 Overview 載入 500 輪資料時的瓶頸並提出可驗證改善。",
        "instructions": """- 先定義具代表性的工作負載、資料規模、環境、warmup、重複次數、percentile／throughput／error rate 基準與目標；缺少量測時不可直接宣稱瓶頸。
- 沿熱路徑盤點演算法複雜度、重複 I/O、序列化、快取、鎖競爭與前端重繪。
- 將觀測到的瓶頸、合理假設與待量測項目分開；先安排 baseline／profiling 任務，只有證實熱點後才規劃最佳化。
- 每個改善只改一組可歸因變因，並列出正確性風險、資源交換與相同環境下的前後比較方式。""",
    },
    {
        "id": "api-data-flow-analysis",
        "label": "分析 API／資料流整合",
        "category": "分析",
        "description": "追蹤跨前後端或服務邊界的資料契約、轉換與錯誤語意。",
        "requirement_placeholder": "例：分析異常紀錄從 loop 寫入、Dashboard API 到前端展開 log 的完整資料流。",
        "instructions": """- 確認 authoritative contract、版本與所有 producer／consumer；沒有正式契約時，以實際 producer／consumer、測試與可觀測行為作現況真相，由 agent 建立符合需求與 repo 慣例的最小契約。多份候選衝突時先以原始需求、實際呼叫端與測試自行裁定；只有剩餘衝突會改變對外語意、安全／不可逆狀態或需要新外部權限時，才列重大未決 human gate。從生產者到消費者列出每段實際相關的 schema、欄位語意、預設值、驗證與相容策略。
- 分開追蹤同步與非同步路徑的成功、空資料、部分失敗、逾時、重試、取消、權限拒絕、重複／亂序投遞及 idempotency 行為。
- 標出資料真相來源、衍生投影、快取與失效時機，避免兩套狀態各自演進。
- 說明 auth context、版本相容窗口與 producer／consumer 部署先後；安排 contract test 與至少一條端到端驗證路徑。""",
    },
    {
        "id": "change-impact-analysis",
        "label": "分析變更影響／相容性",
        "category": "分析",
        "description": "從一項預定變更追出直接與間接受影響面，形成有證據的相容、部署與驗證方案。",
        "requirement_placeholder": "例：評估把 workspace status 欄位改成 enum 對 API、歷史 state、前端與既有 workspace 的影響。",
        "instructions": """- 先固定變更前基線、預定變更與不變條件，再盤點 callers／callees、schema、持久資料、事件、設定、批次 job、外部 consumer 與操作文件。
- 將每個候選影響面分類為需修改、相容但需驗證、不受影響或待確認；文字命中不等於語意受影響，文字未命中也不等於不受影響——持久資料、repo 外的 consumer、反射或動態組字串、設定驅動的引用等無法靠字面搜尋排除的面向，不得以搜尋未命中判「不受影響」，必須以資料樣本、契約／版本證據或消費端證據判定，取不到證據時列「待確認」；每項都要附判定證據。
- 分析舊／新版本共存窗口、資料遷移或 backfill、feature flag、部署順序、監控訊號與回滾條件，不得只列編譯期影響。
- 建立「變更 → 受影響面 → 驗證」對照，讓每個確認受影響項都能追到 task／DoD；未受影響候選不逐項造空列，只摘要限定取證邊界與仍未知的風險。""",
    },
    {
        "id": "incident-root-cause",
        "label": "分析生產事故根因",
        "category": "分析",
        "description": "以 log、metrics 與時間線證據重建事故因果鏈，區分緩解與根修並補上防復發驗證。",
        "requirement_placeholder": "例：昨晚 21:00 起訂單服務大量 timeout，約 40 分鐘後自行恢復，請找出根因並提出防復發方案。",
        "instructions": """- 先固定事故時間窗、影響面（服務、資料、使用者）與可取得的 log、metrics、trace、部署／設定變更紀錄；缺少的觀測資料列為待確認，不得以「查無資料」推定無事發生。
- 以證據重建時間線：第一個異常訊號、擴散路徑、緩解動作與恢復點；區分觸發條件、根因、放大因素與巧合事件，每項附 log 片段位置或 `檔案:行號`。
- 對候選根因沿程式碼與資料流驗證因果鏈；能安全重現時規劃非生產環境的重現步驟，無法重現者標示信心等級與還缺的證據，不得把相關性寫成因果。
- 區分立即緩解與根本修復；根修與會失敗的回歸測試放同一任務，依 red → green 完成（先確認測試因該缺陷變紅，再修到全綠），臨時緩解要列出移除條件與追蹤方式，避免永久化。
- 檢查同類故障的其他入口與監控缺口；規劃能在復發前攔截的告警或測試，並把時間線、根因與行動項寫成可保存的事故報告。""",
    },
    {
        "id": "security-boundary-analysis",
        "label": "分析安全／信任邊界",
        "category": "品質",
        "description": "盤點輸入、檔案、程序與權限邊界，依證據規劃加固。",
        "requirement_placeholder": "例：分析 Dashboard 接受 repo 路徑與設定檔時的路徑穿越、symlink 與命令注入風險。",
        "instructions": """- 先畫出信任邊界、資產、攻擊者可控輸入與高權限副作用，不把一般錯誤直接稱為漏洞。
- 逐入口檢查驗證、正規化、授權、symlink／路徑邊界、命令參數、資源上限與敏感資料輸出。
- 每個風險附可定位的程式證據、可利用前提、影響與現有緩解；不確定時標示待驗證。
- 依 impact × exploitability 排序，驗證方式必須安全且非破壞性；若原需求包含修復，任務才需加入負向測試並實際執行取證——修復前攻擊路徑測試為紅、修復後為綠，且既有測試全綠證明 fail-closed 不破壞合法流程。""",
    },
    {
        "id": "security-scan-remediation",
        "label": "資安掃描分診與修復",
        "category": "品質",
        "description": "把 SAST／資安掃描報告逐筆分診成有證據的真偽陽性判定，規劃不破壞行為的修復與重掃驗證。",
        "requirement_placeholder": "例：分診本次 Fortify 掃描的 30 筆發現，判定真偽陽性並規劃修復與抑制留痕。",
        "instructions": """- 先固定掃描工具與版本、規則集／政策、掃描範圍與報告對應的 revision；報告與目前程式碼版本不一致時列為影響判讀的待確認事項。
- 逐筆把發現對應到實際程式路徑與資料流，分類為已確認可利用、真陽性但已有緩解、誤報或待確認；不得把工具嚴重度直接當結論，也不得為清零而批次標誤報。
- 誤報與可接受風險的判定必須附程式證據與理由（如輸入不可達、上游已驗證），並依工具的正式機制（審核註記／抑制設定）留痕，不得靜默忽略。
- 修復優先用集中式防護（驗證層、encoder、parameterized query）並合併同一 root cause 的多筆發現；每項修復列出行為不變條件與對應回歸測試，且回歸測試以 repo 實際測試命令執行全綠為準。
- 修復任務含負向測試證明攻擊路徑已封閉；以相同規則集重跑掃描並比對前後結果作為機器 DoD，殘餘風險與接受決策列 human gate。若掃描命令或規則集無法從 repo／CI 證實、或掃描環境不在可用範圍，重跑比對改列 human gate（由持有掃描平台者執行並回附報告），不得虛構掃描命令充當機器 DoD。""",
    },
    {
        "id": "java-generic",
        "label": "Java 專案通用工作",
        "category": "開發",
        "description": "在既有 Java 專案規劃新功能、重構或批次修 Bug，行為條目逐條對應測試證據。",
        "requirement_placeholder": "例：在既有 Java 專案新增批次匯入，沿用目前分層與 ApiResponse 規格。",
        "instructions": """- 此模板只補充 Java 技術慣例；新功能、Bug、重構的行為分析仍依原始需求，不要把三者混成無界工作。
- 逐條列出輸入 → 輸出／副作用與需求依據；重構先列不可變行為，Bug 在同一任務走 red → green。
- 從限定範圍的 codebase 讀出本工作實際需要的分層、命名、序列化、回應與例外處理慣例；只記錄有證據且影響交付的慣例，不假設一定有 Mapper 或 ApiResponse，也不為不存在的候選逐項造空列。以已讀代表性入口及停止邊界摘要未涵蓋風險。
- 測試骨架只有在可重用且本身能綠燈驗收時才獨立成前置任務，否則測試與實作放同一任務；每個行為條目都要能對到任務與測試證據。
- 從 repo wrapper、multi-module 設定與 CI 判定實際 DoD（如 `./mvnw test` 或 `./gradlew test`）及執行目錄，不得預設 Maven。""",
    },
    {
        "id": "ejb-springboot-migration",
        "label": "EJB → Spring Boot 搬移",
        "category": "遷移",
        "description": "把 EJB／應用伺服器舊系統搬到需求指定的 Spring Boot／JDK 版本，守住交易、生命週期與容器服務語意。",
        "requirement_placeholder": "例：把舊專案全部 EJB 與對外 API 搬到 Spring Boot 3／Java 17，維持行為等價。",
        "instructions": """- 固定來源應用伺服器／Java EE 與目標 Spring Boot／JDK 的確切版本，再盤點 Stateless／Stateful／Singleton／MDB、JNDI、timer、interceptor、security、端點與平台 API。
- 依 descriptor → method → class → 預設值的實際優先序，逐 business method 確認有效 CMT 屬性，再對應 Spring `@Transactional`、rollback、timeout 與測試。
- 盤點 Stateful session、Singleton concurrency／lock、pooling、lifecycle callback、async 與 local／remote 呼叫語意，不得只處理可編譯的 annotation。
- 將 XA、持久 timer、BMT 與無法直接等價的容器能力列為明確 human gate，不自行替團隊決策。
- characterization／真實整合環境測試必須在搬移前後以同一套測試實際執行並通過，作為行為等價判準；交易語意逐 business method 要有執行通過的測試證據，不得只以 annotation 對應表為完成依據。`javax`→`jakarta`、Hibernate 與 JDK 移除模組只依目標版本的官方相容資訊判定。""",
    },
    {
        "id": "jsp-react-migration",
        "label": "JSP → React 搬移",
        "category": "遷移",
        "description": "把 JSP 頁面與其伺服器端互動逐頁搬到 React，以完整盤點及 repo 可用的多層驗證守住等價性。",
        "requirement_placeholder": "例：把舊系統指定目錄下全部 JSP 搬到 React，保持頁面與權限行為等價。",
        "instructions": """- 每支 JSP／fragment 及相鄰 controller、filter、custom tag、靜態資源、內嵌 Ajax、upload／download 都要列入 inventory，包含 URL、表單、session、權限與 i18n。
- 逐頁整理輸入、輸出、驗證、錯誤訊息與跳轉；碰 DB／session 的邏輯移到後端 API，純呈現留前端。
- 優先沿用目標 repo 的 build、元件與 e2e 測試棧；有穩定環境時自動化真後端 contract／smoke，否則才列 human gate，不得固定假設一定用 Playwright。
- 每個盤點列必須能追到完成任務與測試證據，且對應測試以 repo 實際命令執行全綠為準；未列入 inventory 的頁面不得視為已搬完。""",
    },
    {
        "id": "db-migration",
        "label": "資料庫平台搬移",
        "category": "遷移",
        "description": "在明確的來源／目標版本下搬移 schema、資料、查詢與交易行為，以差異驗證及可演練的切換方案守住等價性。",
        "requirement_placeholder": "例：把訂單模組從 Oracle 搬到 MariaDB，SQL 與交易行為維持等價。",
        "instructions": """- 固定來源／目標資料庫、driver／ORM dialect 的確切版本，以及範圍、資料量、允許停機、RPO／RTO；缺少者列為會阻擋切換設計的待確認事項。
- 盤點 repo 自有 SQL、ORM／Mapper、動態 SQL、DDL、view、procedure、trigger、sequence／identity、外部 job、連線池與交易設定；記錄搜尋方法，無法靜態枚舉者明列邊界。
- 針對限定範圍內實際命中的型別／轉型、函式、分頁、日期時區、NULL／空字串、encoding／collation、識別碼大小寫、鎖、隔離或錯誤語意差異整理相容要求；repo 使用點與官方版本證據分開引用。未命中的候選不逐項填空，只摘要取證方法與未知邊界。
- 規劃可重跑／續跑且有版本紀錄的 schema／資料搬移與切換演練；依實際資料模型與風險選擇筆數、checksum、業務 invariant、孤兒資料或 sequence 連續性等足以證明結果的機器比對，不強制固定五項。只有不可機器化且確實需要產品判斷的業務簽核才列人工驗收。
- 同一套 characterization／contract 測試要分別在來源與目標資料庫執行並比較結果、副作用及關鍵 query plan／效能門檻；無法取得來源或目標資料庫的可測環境時，列為阻擋等價驗證的待確認事項並在第一個受影響 task 標 human gate，明列替代證據（生產快照、查詢紀錄回放等）及其限制，不得只在單庫跑過即宣稱行為等價。
- 明列停止舊寫入、切換、驗證與回滾條件；切換後新寫入若讓回滾不再安全，必須標成 human gate，不得只寫 restore backup。""",
    },
    {
        "id": "oracle-mariadb-migration",
        "label": "Oracle → MariaDB 搬移",
        "category": "遷移",
        "description": "把 Oracle 專屬 SQL、語意差異與 PL/SQL 展開成可枚舉的必查清單，以雙庫對照測試守住行為等價。",
        "requirement_placeholder": "例：把帳務模組從 Oracle 19c 搬到 MariaDB 10.11，MyBatis XML 內全部 SQL 需行為等價。",
        "instructions": """- 固定來源 Oracle 與目標 MariaDB 的確切版本及 driver／ORM dialect；等價物依 MariaDB 版本判定（如 SEQUENCE、window function、recursive CTE 支援度），不得以「MySQL 相容」概括，版本能力引官方文件證據。
- 在需求／context／ref 已知的 SQL 範圍內做針對性取證（含 ORM／Mapper 動態 SQL、DDL、view、排程 job），以下 Oracle 專屬語法只作命中提示：
  - 結構與語法：DUAL、(+) 外連接、ROWNUM 分頁、CONNECT BY、MERGE、ROWID、隱含型別轉換。
  - 函式與日期：NVL／NVL2／DECODE、SYSDATE 與日期算術、TO_DATE／TO_CHAR 格式。
  - 取號：sequence.NEXTVAL／CURRVAL，對應 AUTO_INCREMENT／SEQUENCE 與 LAST_INSERT_ID()／ORM generated-key 取值路徑。
  - 只列實際命中項的 `檔案:行號` 與改寫方案；未命中的 token 不逐項造空列，改以一段 bounded 搜尋方法與動態拼接停止邊界摘要。隱含型別轉換、日期算術等無法靠字面搜尋排除的語意，僅對實際受影響的 schema 型別與比較述詞用雙庫測試下結論。
- 語意差異逐項驗證而非假設：空字串與 NULL（Oracle 視為同一、MariaDB 區分）、預設交易隔離級別與鎖行為、識別碼大小寫與 collation、VARCHAR2 的 byte／char 長度語意、NUMBER 對 DECIMAL 精度、DATE 含時間成分的型別對應；受影響的讀寫路徑要有測試證據，不得只改到語法可執行。
- PL/SQL（package、procedure、function、trigger、scheduler job）逐支判定改寫成對應的 MariaDB stored program（procedure／function／trigger／event）、搬到應用層或棄用；交易邊界與例外語意改寫後需測試證明，無法等價的能力（如 autonomous transaction）列 human gate，不自行替團隊決策。
- 同一套 characterization 測試分別在 Oracle 與 MariaDB 執行並比對結果、副作用與錯誤語意，優先沿用 repo 既有測試棧的真實資料庫環境；資料搬移以筆數、checksum 與業務 invariant 對帳。無法取得 Oracle 或 MariaDB 的可測環境時，列為阻擋等價驗證的待確認事項並標 human gate，明列替代證據及其限制，不得只在單庫跑過即宣稱行為等價。
- 明列停止舊寫入、驗證、切換與回滾條件；切換後新寫入若讓回滾不再安全，必須標成 human gate，不得只寫 restore backup。""",
    },
    {
        "id": "schema-data-rollout",
        "label": "Schema／資料回填上線",
        "category": "遷移",
        "description": "在同一資料平台安全變更 schema 或回填資料，涵蓋混合版本相容窗、可續跑 backfill、切換與移除舊結構。",
        "requirement_placeholder": "例：把 orders.status 由字串拆成 status code 與 history table，需不停機上線並回填既有資料。",
        "instructions": """- 固定資料量、寫入速率、允許停機、部署拓撲與 rollback 窗口，定義變更前後 schema、資料 invariant 及舊／新應用混跑期間的不變條件。
- 依 expand → migrate/backfill → switch reads/writes → contract 拆階段；只有證據顯示需要時才加入 dual-write／dual-read，並定義一致性與修復策略。
- Backfill 必須可分批、冪等、可重跑／續跑、限流且可觀測；說明 checkpoint、失敗重試、線上寫入競態及鎖／交易邊界。
- 每階段列出並實際執行向前／向後相容驗證與筆數／checksum／業務 invariant 對帳，結果全數通過才可進入下一階段，並定義監控與停止條件；資料或 schema destructive step 必須晚於相容窗、對帳全數通過並經人工確認 gate。
- 回滾方案要區分程式 rollback 與資料 rollback；若新寫入無法無損轉回舊格式，明確標示 point of no return 與替代恢復方式。""",
    },
    {
        "id": "dependency-upgrade",
        "label": "框架／依賴跳版",
        "category": "遷移",
        "description": "升級 JDK、框架或關鍵程式庫的大版本，把 breaking changes 展開成可枚舉、可驗證的搬移清單。",
        "requirement_placeholder": "例：把服務從 Spring Boot 2.7 升到 3.3（Java 17），列出全部 breaking changes 與修正任務。",
        "instructions": """- 固定目前與目標的精確版本，包含 runtime／JDK、build tool／plugin、直接依賴、lockfile 與實際解析的傳遞依賴；限定本次範圍，避免順手升級無關套件。
- 逐條對照適用版本的官方 migration guide／release notes／相容矩陣；外部變更附 URL、版本與章節，repo 使用點附 `檔案:行號`。
- 找不到直接使用點時，在限定範圍內再核對實際相關的 autoconfiguration、傳遞啟用、設定／預設值、啟動 log 或整合測試；記錄已查來源與停止邊界，不為每個未命中候選逐項造空列。
- 區分編譯期、啟動期與特定流量才出現的差異，涵蓋序列化、日期時區、反射、classpath、資料格式與對外契約，逐項安排測試或 runtime smoke。
- 依必須一起升級的相依群組切成連貫 checkpoint，每個 checkpoint 結束時 build／test／smoke 全綠；相容層只在確有跨版本共存需求時加入。
- 每個 checkpoint 使用 repo 實際命令並列出回退方式；無法維持相容或不可逆的能力列為 human gate。""",
    },
    {
        "id": "k8s-deployment-config",
        "label": "Kubernetes 部署設定",
        "category": "部署",
        "description": "依待部署服務的實際需求與參考專案慣例，建立可逐環境 render、schema 驗證且不含明文憑證的 Kubernetes 設定。",
        "requirement_placeholder": "例：參考 payment-service 的 Helm chart，為 order-service 完成 base／sit／prod 部署設置，需通過 helm lint 與 helm template。",
        "instructions": """- 明確區分待部署服務與參考專案，固定目標 Kubernetes／API 版本、namespace、環境清單、可用 CRD 與政策限制；服務值只從待部署服務取證，參考專案只提供結構慣例。
- 產出格式依原始需求 → 待部署 repo 既有慣例 → 參考 repo 依序判定；若都無訊號，由 agent 依需求與現有 toolchain 選擇最小可維護方案並記錄假設。只有來源衝突牽涉外部平台政策、不可逆狀態或新外部權限且現有證據無法裁定時才列 human gate。
- 只盤點有證據影響部署的 workload、Service、Ingress、ServiceAccount／RBAC、ConfigMap、Secret 外部引用、volume／PVC、HPA、PDB、NetworkPolicy、securityContext、probe、rollout、affinity／toleration或 image 注入點；未涉及種類不逐項造空列，改摘要取證邊界與仍未知風險。
- 從待部署服務確認 image、command／args、port、健康檢查、資源、環境變數、掛載與外部依賴，附 `檔案:行號`；不得複製參考服務的識別碼或專屬值。
- 共用內容放 base／chart defaults，環境差異只放 overlay／values 並逐環境列出；Secret 僅用佔位或受控外部引用，不得輸出明文憑證。
- 每個環境都以 repo 實際路徑驗證：pure YAML 做版本相符的 schema validation／dry-run；Kustomize 執行 `kustomize build <overlay-dir>` 後驗 schema；Helm 執行 `helm lint <chart-dir> -f <values-file>` 與 `helm template <release> <chart-dir> -f <values-file>` 後驗 schema。最終 DoD 的驗證命令與路徑不得保留 `<...>` 佔位符（上列命令中的 overlay-dir、chart-dir、values-file、release 須以實際值代入）；Secret 值依前條維持佔位或受控外部引用，不受此限。""",
    },
]


def _read_prompt_resource(filename):
    """透過 importlib.resources 讀 package data，讓 wheel／editable install 使用同一路徑。"""
    return (
        resources.files("engine").joinpath("prompts").joinpath(filename)
        .read_text(encoding="utf-8").strip()
    )


@lru_cache(maxsize=1)
def prompt_template_bundle():
    """載入 package 內的固定 prompt 資源；缺檔或 placeholder 漂移時整包 fail-closed。"""
    bundle = {}
    errors = []
    for key, (filename, expected_counts, contract_last) in PROMPT_RESOURCE_SPECS.items():
        try:
            value = _read_prompt_resource(filename)
        except (OSError, UnicodeError) as error:
            errors.append(f"固定 Prompt 資源 {filename} 無法讀取：{error}")
            continue
        if not value:
            errors.append(f"固定 Prompt 資源 {filename} 不可為空")
            continue
        if len(value) > MAX_PROMPT_RESOURCE_CHARS:
            errors.append(
                f"固定 Prompt 資源 {filename} 超過 {MAX_PROMPT_RESOURCE_CHARS} 字"
            )
            continue
        valid_markers = PROMPT_PLACEHOLDER_RE.findall(value)
        all_markers = PROMPT_MARKER_RE.findall(value)
        malformed = (
            len(all_markers) != len(valid_markers)
            or value.count("<<") != len(all_markers)
            or value.count(">>") != len(all_markers)
        )
        wrong_counts = {
            placeholder: (expected, valid_markers.count(placeholder))
            for placeholder, expected in expected_counts.items()
            if valid_markers.count(placeholder) != expected
        }
        unknown = sorted(set(valid_markers) - set(expected_counts))
        if malformed or wrong_counts or unknown:
            details = []
            if malformed:
                details.append("含格式錯誤的 <<...>> marker")
            if wrong_counts:
                details.append(
                    "次數錯誤 " + ", ".join(
                        f"{placeholder} 應為 {expected}、實際 {actual}"
                        for placeholder, (expected, actual) in sorted(wrong_counts.items())
                    )
                )
            if unknown:
                details.append(f"未知 {', '.join(unknown)}")
            errors.append(f"固定 Prompt 資源 {filename} placeholder 不合法：{'；'.join(details)}")
            continue
        if contract_last and not value.endswith("<<MODE_CONTRACT>>"):
            errors.append(f"固定 Prompt 資源 {filename} 必須以 <<MODE_CONTRACT>> 結尾")
            continue
        bundle[key] = value
    if errors or len(bundle) != len(PROMPT_RESOURCE_SPECS):
        return None, "；".join(errors) or "固定 Prompt 資源不完整"
    bundle["schema_version"] = PROMPT_TEMPLATE_BUNDLE_SCHEMA_VERSION
    return bundle, None


def _read_text(item, key, index, warnings, *, required=False, maximum=400):
    """驗證團隊模板文字欄位與長度；錯誤累積為 warning，不拖垮整份設定。"""
    if key not in item:
        if required:
            warnings.append(f"團隊 Prompt 模板第 {index} 筆缺少 {key}，已略過")
        return None
    value = item[key]
    if not isinstance(value, str):
        warnings.append(f"團隊 Prompt 模板第 {index} 筆的 {key} 必須是字串，已略過")
        return None
    value = value.strip()
    if required and not value:
        warnings.append(f"團隊 Prompt 模板第 {index} 筆的 {key} 不可為空，已略過")
        return None
    if len(value) > maximum:
        warnings.append(f"團隊 Prompt 模板第 {index} 筆的 {key} 超過 {maximum} 字，已略過")
        return None
    return value


def prompt_template_projection(cfg):
    """Merge built-ins with bounded, validated team templates from shared config."""
    templates = [
        {
            **item,
            "instructions": f"{LIMITED_DISCOVERY_PREFIX}\n{item['instructions']}",
            "source": "builtin",
        }
        for item in BUILTIN_PROMPT_TEMPLATES
    ]
    warnings = []
    raw_templates = cfg.get("prompt_templates")
    if raw_templates is None:
        return templates, warnings
    if not isinstance(raw_templates, list):
        warnings.append("團隊設定 prompt_templates 必須是 JSON array；目前只載入內建模板")
        return templates, warnings
    if len(raw_templates) > MAX_TEAM_PROMPT_TEMPLATES:
        warnings.append(
            f"團隊 Prompt 模板最多 {MAX_TEAM_PROMPT_TEMPLATES} 筆；其餘項目已略過"
        )

    used_ids = {item["id"] for item in templates}
    for index, item in enumerate(raw_templates[:MAX_TEAM_PROMPT_TEMPLATES], start=1):
        if not isinstance(item, dict):
            warnings.append(f"團隊 Prompt 模板第 {index} 筆必須是 JSON object，已略過")
            continue
        unknown = sorted(set(item) - TEAM_TEMPLATE_KEYS)
        if unknown:
            warnings.append(
                f"團隊 Prompt 模板第 {index} 筆含未知欄位 {', '.join(unknown)}，已略過"
            )
            continue
        template_id = _read_text(item, "id", index, warnings, required=True, maximum=64)
        label = _read_text(item, "label", index, warnings, required=True, maximum=80)
        instructions = _read_text(
            item, "instructions", index, warnings, required=True, maximum=12000
        )
        if template_id is None or label is None or instructions is None:
            continue
        if not TEMPLATE_ID_RE.fullmatch(template_id):
            warnings.append(
                f"團隊 Prompt 模板第 {index} 筆 id 只能使用小寫英數、點、底線或連字號，已略過"
            )
            continue
        if template_id in used_ids:
            warnings.append(f"團隊 Prompt 模板 id「{template_id}」重複，已略過")
            continue
        category = _read_text(item, "category", index, warnings, maximum=40)
        description = _read_text(item, "description", index, warnings, maximum=400)
        placeholder = _read_text(
            item, "requirement_placeholder", index, warnings, maximum=1200
        )
        # Optional fields with invalid types/lengths also make the entry ambiguous; skip it.
        if "category" in item and category is None:
            continue
        if "description" in item and description is None:
            continue
        if "requirement_placeholder" in item and placeholder is None:
            continue
        templates.append({
            "id": template_id,
            "label": label,
            "category": category or "團隊",
            "description": description or "團隊自訂任務類型",
            # Team guidance can specialize the work, but it cannot silently reopen the
            # broad-search/data-volume behavior intentionally disabled in this release.
            "instructions": f"{LIMITED_DISCOVERY_PREFIX}\n{instructions}",
            "requirement_placeholder": placeholder or "請在這裡貼上完整需求與已知限制。",
            "source": "team",
        })
        used_ids.add(template_id)
    return templates, warnings
