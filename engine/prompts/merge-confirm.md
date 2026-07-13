# 並行軌道整合確認任務卡

你在隔離 worktree 的 `<<TRACK_NAME>>` 軌道上。integration target 是
`<<MERGE_TARGET>>`，本輪鎖定的 tip 是 `<<INTEGRATION_TIP>>`。

## 人類目標

<<GOAL>>

## 本軌完整任務與 DoD

```json
<<TRACK_TASKS_FULL>>
```

## 執行規則

- 本階段不開放廣域 repo 搜尋/巡檢：只讀本軌任務 scope/ref、實際 diff 與驗證錯誤直接引用的
  source/test；不得全庫列檔或掃 generated/minified、node_modules、coverage、build、trace、video。
  資料不足且無法在限定範圍安全完成時，執行 `<<ISSUE_CMD>> "問題摘要"` 後立即停止，不自行擴大讀取。
- 確認 `<<INTEGRATION_TIP>>` 是目前 HEAD 的祖先，逐一處理上方每個 task 的每一條 DoD；可在
  目前 child worktree 重現者必須實跑，並執行 `<<VALIDATE_CMD>>`。不得自行把一般 DoD 判成
  「不適用」而跳過。
  明確只能在 integration worktree 執行的 DoD 由 parent 在 merge-ready/CAS 後負責，child 不得
  自稱它已 PASS。首次 pre-CAS confirm 尚無 integration-only 結果是預期時序，不得因此報 issue
  或拒絕送 done；若 parent 驗證失敗，rollback 後會把該次權威錯誤放進修復情報再喚醒本軌。
- 發現缺陷就自行修復並 commit，然後結束；有異動的輪次不送 done，下一輪會重新確認。
- 上述可在 child 重現的 DoD 與 `<<VALIDATE_CMD>>` 全部通過、工作樹乾淨且本輪無異動時，
  執行 `<<DONE_CMD>>` 後立即停止。
- issue 命令只提供下一輪 agent context，不代表預設等待人工；一般實作判斷仍由你自行收斂。
- 不得修改 integration ref、切換 branch、push、操作其他 worktree 或改寫既有歷史。
- integration validate rollback 的資訊若出現在前輪情報，它是 integration worktree 已失敗且已 rollback
  的權威證據。integration-only validator 在 child worktree 無法重現是預期行為；必須直接依錯誤內容
  修復並 commit，不得因本地 validate PASS 就忽略，也不要把它轉成人工 gate。

## 前輪／修復情報

<<REPAIR_CONTEXT>>
