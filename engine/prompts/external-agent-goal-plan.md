套用方式：使用者要求在同一次輸出中連同 goal.md 一併產生初版 plan.json；本節把上述 goal.md 契約與下述 plan.json 契約合併成同一份輸出契約。

## 合併輸出契約：goal.md 加初版 plan.json

上述 goal.md 契約中「不要加 plan JSON」與下述 plan.json 契約中「只輸出一個合法 JSON array」的單一輸出限制，改為以下合併格式；除此之外兩份契約的其他規則全部照舊適用：

1. 先輸出完整符合上述契約的 goal.md 內容，不要加前言、分析過程或 code fence。
2. goal.md 結束後，輸出單獨一行分隔線（僅供人工拆檔，不屬於任何一份檔案的內容）：`===== plan.json =====`
3. 分隔線之後輸出符合下述契約的 JSON array：第一個字元必須是 `[`，最後一個字元必須是 `]`，其後不得再有任何文字。

合併輸出的追蹤規則：

- plan.json 必須以你剛輸出的 goal.md 為唯一依據拆分：每個 task 涵蓋的 SC／AC／INV ID 必須出自 goal.md，且 goal.md 內每個 ID 依下述契約的覆蓋規則至少能追到一個 task；不得引入 goal.md 沒有的範圍、決策或驗收條件。
- goal.md 的「待確認事項」不阻擋拆分：受影響的第一個 task 依下述契約標明 human gate 與對應的待確認事項。
- 這份 plan.json 是初版草稿，供人工審查後匯入；不得因此弱化下述契約對 DoD 可驗證性的要求。
