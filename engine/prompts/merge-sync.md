# 並行軌道整合任務卡

你在隔離 worktree 的 `<<TRACK_NAME>>` 軌道上。目標是讓 integration commit
`<<INTEGRATION_TIP>>` 成為目前 HEAD 的祖先，並保留本軌與 integration 兩邊的正確行為。

## 人類目標

<<GOAL>>

## 本軌完整任務

```json
<<TRACK_TASKS_FULL>>
```

## 執行規則

- 本階段不開放廣域 repo 搜尋/巡檢：只讀衝突檔、本軌任務 scope/ref 直接點名的 source/test 與
  validate 錯誤直接引用的檔案；不得全庫列檔或掃 generated/minified、node_modules、coverage、
  build、trace、video。資料不足時 issue，不自行擴大讀取。
- 先檢查目前 Git 現場；可使用 `git merge --no-commit <<INTEGRATION_TIP>>`，也可採取其他不改寫既有歷史的正確整合方式。
- 自行解決衝突、補必要修復、執行 `<<VALIDATE_CMD>>`，並把成果 commit 在目前 branch。
- 不得修改 `<<MERGE_TARGET>>`、切換 branch、push、操作其他 worktree 或改寫既有 track history。
- 有異動時完成 commit 後直接結束，本輪不要送 done；下一輪會獨立確認。
- 無法完成可執行 `<<ISSUE_CMD>> "問題摘要"`，資訊會交給下一輪，不要等待人工回覆。
- coordinator 命令或必要工作完成後立即停止。

## 前輪情報

<<REPAIR_CONTEXT>>
