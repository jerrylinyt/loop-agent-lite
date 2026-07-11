"""Dashboard 設定投影使用的外部 Agent prompt 模板目錄。

共用分析核心與 Goal/Plan 輸出契約固定在 UI builder；團隊設定只能追加任務指引，
不能取代系統契約。無效的團隊模板會略過並轉成 warning，不讓整個 Dashboard 失效。
"""
import re


MAX_TEAM_PROMPT_TEMPLATES = 50
TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
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
        "instructions": """- 先找出既有使用流程、相鄰功能、資料來源與可重用元件，不要另造平行架構。
- 明列使用者入口、正常流程、空狀態、錯誤狀態、權限或唯讀邊界及相容性影響。
- 將 UI、資料契約、持久化與測試需求互相對應；若需求跨層，說清楚每層責任。
- 優先切出可獨立驗證的垂直功能片段，避免只按檔案或技術層拆任務。""",
    },
    {
        "id": "bug-fix",
        "label": "診斷並修復 Bug",
        "category": "開發",
        "description": "從可重現證據定位根因，建立回歸測試後修復。",
        "requirement_placeholder": "例：完成的 workspace 仍顯示需關注，請找出原因並修正。",
        "instructions": """- 先定義最小重現條件、預期結果與實際結果；沒有重現證據時要標為待確認。
- 沿資料流追到第一個錯誤狀態的產生點，區分根因、放大因素與表面症狀。
- 修復前先規劃會失敗的回歸測試，並涵蓋造成問題的邊界條件。
- 檢查同一規則是否在其他入口重複實作，避免只補畫面上的症狀。""",
    },
    {
        "id": "behavior-preserving-refactor",
        "label": "保留行為的重構",
        "category": "開發",
        "description": "先釘住既有行為，再安全調整結構、責任或可維護性。",
        "requirement_placeholder": "例：整理 Dashboard 的統計投影邏輯，降低重複但不改變 API 行為。",
        "instructions": """- 先列出不可改變的外部行為、公開介面、資料格式、錯誤語意與副作用。
- 找出目前能證明行為的測試；不足處先補 characterization tests，再開始搬動程式。
- 說明新的責任邊界、依賴方向與遷移順序，避免一次性大改造成不可定位的回歸。
- 將純結構改善與任何可觀察行為變更分開；後者必須明列為需求決策。""",
    },
    {
        "id": "project-logic-analysis",
        "label": "分析專案架構／邏輯",
        "category": "分析",
        "description": "盤點模組、入口、依賴、主要流程與狀態真相來源。",
        "requirement_placeholder": "例：分析整個專案如何從 Dashboard 啟動 loop，以及狀態如何回到 Overview。",
        "instructions": """- 從目錄、建置檔、啟動入口與設定檔建立模組清單，說明每個模組責任及依賴方向。
- 追蹤主要使用流程的入口 → 核心邏輯 → 持久化／外部程序 → UI 投影，標出真相來源。
- 盤點跨模組契約、生命週期、錯誤處理、並行或鎖定邊界，以及測試覆蓋位置。
- 將直接從程式讀到的事實與推論分開；推論要附理由並列出仍需驗證的證據。""",
    },
    {
        "id": "code-logic-analysis",
        "label": "分析指定程式／呼叫鏈",
        "category": "分析",
        "description": "針對檔案、函式、API 或事件追出完整控制流與資料流。",
        "requirement_placeholder": "例：分析 dashboard.py 的 fleet_health_projection，包含所有呼叫端與需關注判定。",
        "instructions": """- 明確鎖定指定符號與所有直接／間接呼叫端，追出輸入來源、轉換步驟、輸出與副作用。
- 列出分支條件、預設值、例外路徑、早退、狀態變更、檔案／網路 I/O 與並行邊界。
- 對每個重要結論附 `檔案:行號`；若動態呼叫無法靜態確認，要明確標記。
- 說明既有測試如何覆蓋這條呼叫鏈，以及哪些邊界目前沒有證據。""",
    },
    {
        "id": "test-gap-analysis",
        "label": "分析測試缺口",
        "category": "品質",
        "description": "把需求、風險分支與既有測試做可追蹤的覆蓋對照。",
        "requirement_placeholder": "例：分析 workspace 封存／還原流程的測試缺口，優先找資料遺失風險。",
        "instructions": """- 先枚舉可觀察行為、風險分支與失敗模式，再對照現有 unit／integration／e2e 測試。
- 建立需求或分支 → 測試檔與案例 → 缺口的追蹤關係，不以單純 coverage 百分比代替分析。
- 優先處理資料損壞、安全邊界、競態、錯誤恢復與曾發生的回歸。
- 說清楚 fixture、mock 與真實整合環境的界線，避免測試只證明 mock 本身。""",
    },
    {
        "id": "performance-analysis",
        "label": "分析效能瓶頸",
        "category": "品質",
        "description": "用可量測基準定位時間、記憶體或 I/O 熱點並規劃驗證。",
        "requirement_placeholder": "例：分析 Overview 載入 500 輪資料時的瓶頸並提出可驗證改善。",
        "instructions": """- 先定義工作負載、資料規模、環境、目前基準與目標，缺少量測時不可直接宣稱瓶頸。
- 沿熱路徑盤點演算法複雜度、重複 I/O、序列化、快取、鎖競爭與前端重繪。
- 將觀測到的瓶頸、合理假設與待量測項目分開，規劃可重複的 benchmark 或 profiling 證據。
- 每個改善都要列出正確性風險、資源交換與前後基準比較方式。""",
    },
    {
        "id": "api-data-flow-analysis",
        "label": "分析 API／資料流整合",
        "category": "分析",
        "description": "追蹤跨前後端或服務邊界的資料契約、轉換與錯誤語意。",
        "requirement_placeholder": "例：分析異常紀錄從 loop 寫入、Dashboard API 到前端展開 log 的完整資料流。",
        "instructions": """- 從生產者到消費者列出每一段 schema、欄位語意、預設值、驗證與相容策略。
- 追蹤成功、空資料、部分失敗、逾時、重試、取消與權限拒絕的端到端行為。
- 標出資料真相來源、衍生投影、快取與失效時機，避免兩套狀態各自演進。
- 對跨層變更安排 contract test 與至少一條端到端驗證路徑。""",
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
- 修復任務要含負向測試，並確認 fail-closed 行為不破壞合法流程。""",
    },
    {
        "id": "java-generic",
        "label": "泛用 Java 工作",
        "category": "既有模板",
        "description": "對應 templates/java-generic.md，適用新功能、重構與批次修 Bug。",
        "requirement_placeholder": "例：在既有 Java 專案新增批次匯入，沿用目前分層與 ApiResponse 規格。",
        "instructions": """- 逐條列出輸入 → 輸出／副作用與需求依據；重構先列不可變行為，Bug 先列重現測試。
- 從 codebase 讀出分層、命名、Mapper、回應包裝與例外處理慣例，讓後續執行者不必猜。
- 優先建立測試骨架；每個行為條目都要能對到任務與測試證據。
- 預設機器 DoD 為 `mvn -q test`，若專案實際命令不同，必須以 repo 證據修正。""",
    },
    {
        "id": "ejb-springboot-migration",
        "label": "EJB → Spring Boot 搬移",
        "category": "既有模板",
        "description": "對應 templates/ejb-to-springboot.md，強調交易、容器服務與平台跳版。",
        "requirement_placeholder": "例：把舊專案全部 EJB 與對外 API 搬到 Spring Boot 3／Java 17，維持行為等價。",
        "instructions": """- 完整盤點 EJB、descriptor 覆寫、JNDI、timer、interceptor、security、端點、JMS／MDB 與平台 API。
- 逐 business method 確認 EJB CMT 有效交易屬性，對應 Spring `@Transactional`、rollback、timeout 與測試。
- 將 XA、持久 timer、BMT 與無法直接等價的容器能力列為明確 human gate，不自行替團隊決策。
- 規劃 characterization／Testcontainers 證據，並涵蓋 `javax`→`jakarta`、Hibernate 與 JDK 移除模組。""",
    },
    {
        "id": "jsp-react-migration",
        "label": "JSP → React 搬移",
        "category": "既有模板",
        "description": "對應 templates/jsp-to-react.md，以逐頁盤點與三層驗證確保完整。",
        "requirement_placeholder": "例：把舊系統指定目錄下全部 JSP 搬到 React，保持頁面與權限行為等價。",
        "instructions": """- 每支 JSP 與共用 fragment 都要列入 inventory，包含 URL、include、taglib、scriptlet、JSTL、表單、session 與 i18n。
- 逐頁整理輸入、輸出、驗證、錯誤訊息與跳轉；碰 DB／session 的邏輯移到後端 API，純呈現留前端。
- 規劃 build、元件測試、Playwright e2e 三層機器驗證；視覺還原與真後端煙測列人工驗收。
- 每個盤點列必須能追到完成任務與測試證據，未列入 inventory 的頁面不得視為已搬完。""",
    },
]


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
    templates = [{**item, "source": "builtin"} for item in BUILTIN_PROMPT_TEMPLATES]
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
            "instructions": instructions,
            "requirement_placeholder": placeholder or "請在這裡貼上完整需求與已知限制。",
            "source": "team",
        })
        used_ids.add(template_id)
    return templates, warnings
