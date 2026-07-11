/** 空 workspace 的三步驟導引；元件本身不建立任何設定，所有變更仍由 Launcher 執行。 */
export default function GettingStarted({ readonly, onLaunch }: { readonly: boolean; onLaunch: () => void }) {
  return <section className="getting-started" aria-labelledby="getting-started-title">
    <div className="getting-started-head"><div><h2 id="getting-started-title">第一次使用，三步完成</h2><p>每一步都可回頭修改；啟動前仍會執行完整安全檢查。</p></div>{!readonly && <button type="button" className="primary-button" onClick={onLaunch}>開始設定</button>}</div>
    <ol>
      <li><span>1</span><div><strong>選擇 Code Repo</strong><p>Repo 必須是 Git repository，工作樹需乾淨，goal.md 應先 commit。</p></div></li>
      <li><span>2</span><div><strong>確認 Goal 與 Plan</strong><p>可沿用 repo 的 goal，或匯入 goal.md／plan.json；變更會先顯示 Diff。</p></div></li>
      <li><span>3</span><div><strong>驗證命令後啟動</strong><p>先執行 Validate 或完整健檢，確認 CLI、PATH、依賴和 timeout 正確。</p></div></li>
    </ol>
    <details><summary>常見啟動失敗</summary><ul><li>工作樹不乾淨：先 commit 或處理未追蹤檔。</li><li>Validate 找不到命令：確認個人 PATH 與 CLI 設定。</li><li>同 repo 已有 loop：同一 Git worktree 只允許單 writer。</li></ul></details>
  </section>;
}
