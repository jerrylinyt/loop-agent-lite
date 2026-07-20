/** Ralph runner 啟動表單：只收 ralph 參數（RALPH_CONTRACT §C + §I），送出後沿用 startup handshake。 */
import { useEffect, useMemo, useRef, useState } from "react";
import { getJson, postJson, waitForJobStartup } from "../../shared/api/client";
import type {
  ConfigResponse, RalphArgsStyle, RalphLaunchRequest, RalphPrdFormat,
  RalphUsageLimitAction, StartupResponse,
} from "../../shared/api/types";
import { RALPH_ARGS_TEMPLATES } from "../workspaces/ralphViewModel";

interface RepoStatus { tree_clean: boolean; branch?: string; error?: string }

/** 逗號分隔 → 去空白陣列（fallback_models）。 */
function parseCsv(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}
/** 空白／換行分隔 → token 陣列（自訂 args_template）。 */
function parseTemplate(value: string): string[] {
  return value.split(/\s+/).map((item) => item.trim()).filter(Boolean);
}

export default function RalphLaunchForm({ config, launchSignal, onLaunched, onStatus }: {
  config: ConfigResponse;
  /** LauncherModal footer 的「啟動」按鈕遞增此值以觸發送出，避免向上暴露 imperative handle。 */
  launchSignal: number;
  onLaunched: (name: string) => void;
  onStatus: (status: { canLaunch: boolean; launching: boolean; message: string }) => void;
}) {
  const ralph = config.ralph;
  const scripts = ralph?.scripts ?? [];
  const tools = ralph?.tools ?? [];
  const [repoChoice, setRepoChoice] = useState(() => config.repos[0] ?? "__custom__");
  const [customRepo, setCustomRepo] = useState("");
  const [repoStatus, setRepoStatus] = useState<RepoStatus | null>(null);
  const [name, setName] = useState("");
  const [commandMode, setCommandMode] = useState<"script" | "custom">(scripts.length ? "script" : "custom");
  const [scriptIndex, setScriptIndex] = useState(0);
  const [customCommand, setCustomCommand] = useState("");
  const [ralphDir, setRalphDir] = useState("");
  const [iterations, setIterations] = useState(ralph?.default_iterations ?? 5000);
  const [toolChoice, setToolChoice] = useState(() => tools[0] ?? "__custom__");
  const [customTool, setCustomTool] = useState("");
  const [model, setModel] = useState("");
  const [argsStyle, setArgsStyle] = useState<RalphArgsStyle>(ralph?.default_args_style ?? "positional");
  const [customArgsTemplate, setCustomArgsTemplate] = useState("");
  const [prdContent, setPrdContent] = useState("");
  const [prdFormat, setPrdFormat] = useState<RalphPrdFormat>("json");
  const [prdPath, setPrdPath] = useState(ralph?.prd_filenames?.[0] ?? "prd.json");
  const [newBranch, setNewBranch] = useState(false);
  const [usageLimitAction, setUsageLimitAction] = useState<RalphUsageLimitAction>(ralph?.default_usage_limit_action ?? "restart");
  const [fallbackModels, setFallbackModels] = useState((ralph?.default_fallback_models ?? []).join(", "));
  const [autoRestartMax, setAutoRestartMax] = useState(ralph?.default_auto_restart_max ?? 20);
  const [launching, setLaunching] = useState(false);
  const [message, setMessage] = useState("");

  const repo = repoChoice === "__custom__" ? customRepo.trim() : repoChoice;
  const tool = toolChoice === "__custom__" ? customTool.trim() : toolChoice;
  const commandReady = commandMode === "custom" ? !!customCommand.trim() : scripts.length > 0;
  const canLaunch = !launching && !!repo && commandReady && !!tool && iterations >= 1;

  useEffect(() => {
    if (!repo) return setRepoStatus(null);
    let active = true;
    void getJson<RepoStatus>(`/api/repo-status?repo=${encodeURIComponent(repo)}`).then((status) => {
      if (active) setRepoStatus(status);
    });
    return () => { active = false; };
  }, [repo]);

  useEffect(() => {
    onStatus({ canLaunch, launching, message });
  }, [canLaunch, launching, message, onStatus]);

  const resolvedArgs = useMemo(() => {
    const template = argsStyle === "custom" ? parseTemplate(customArgsTemplate) : RALPH_ARGS_TEMPLATES[argsStyle];
    return template
      .map((token) => token
        .replace("{iterations}", String(iterations))
        .replace("{tool}", tool || "tool")
        .replace("{model}", model.trim()))
      .filter((token) => token !== ""); // 空 {model} token 直接丟棄（RALPH_CONTRACT）
  }, [argsStyle, customArgsTemplate, iterations, tool, model]);
  const baseCommand = commandMode === "custom" ? customCommand.trim() : scripts[scriptIndex]?.cmd ?? "";
  const commandPreview = [baseCommand, ...resolvedArgs].filter(Boolean).join(" ");

  const doLaunch = async () => {
    if (!repo) return setMessage("錯誤：請選擇或輸入 repo");
    if (commandMode === "custom" && !customCommand.trim()) return setMessage("錯誤：請輸入 ralph 命令");
    if (commandMode === "script" && !scripts.length) return setMessage("錯誤：設定沒有可用的 ralph 腳本，請改用自訂命令");
    if (!tool) return setMessage("錯誤：請選擇或輸入 tool");
    setLaunching(true);
    setMessage("啟動中…");
    const body: RalphLaunchRequest = { runner: "ralph", repo, iterations, tool, args_style: argsStyle };
    if (name.trim()) body.name = name.trim();
    if (commandMode === "custom") body.ralph_custom = customCommand.trim();
    else body.ralph_idx = scriptIndex;
    if (ralphDir.trim()) body.ralph_dir = ralphDir.trim();
    if (model.trim()) body.model = model.trim();
    if (argsStyle === "custom") body.args_template = parseTemplate(customArgsTemplate);
    if (prdContent.trim()) { body.prd_content = prdContent; body.prd_format = prdFormat; }
    if (prdPath.trim()) body.prd_path = prdPath.trim();
    if (newBranch) body.new_branch = true;
    body.usage_limit_action = usageLimitAction;
    body.fallback_models = parseCsv(fallbackModels);
    body.auto_restart_max = autoRestartMax;

    const response = await postJson<StartupResponse>("/api/launch", body);
    if (response.error || !response.name || !response.pid) {
      setLaunching(false);
      return setMessage(`錯誤：${response.error ?? "啟動失敗"}`);
    }
    if (response.starting) {
      // 收到 pid 只代表已 spawn ralph；必須等 startup handshake ready 才算啟動成功。
      setMessage("啟動前檢查中…");
      const startup = await waitForJobStartup(response.name, response.pid, response.startup_timeout);
      if (startup.error) {
        setLaunching(false);
        return setMessage(`錯誤：${startup.error}`);
      }
    }
    setLaunching(false);
    setMessage(`成功：已啟動 ${response.name}（pid ${response.pid}）`);
    onLaunched(response.name);
  };

  // 讓 footer 的「啟動」以最新 state 送出：ref 追最新 closure，effect 只在 launchSignal 變動時觸發。
  const launchRef = useRef(doLaunch);
  launchRef.current = doLaunch;
  useEffect(() => {
    if (launchSignal > 0) void launchRef.current();
  }, [launchSignal]);

  return (
    <div className="form-grid launcher-form">
      <div className="form-field repo-select-field">
        <span>Repo</span>
        <select aria-label="Repo" value={repoChoice} onChange={(event) => setRepoChoice(event.target.value)}>
          {config.repos.map((item) => <option key={item} value={item}>{item}</option>)}
          <option value="__custom__">手動輸入…</option>
        </select>
      </div>
      {repoChoice === "__custom__" && <input value={customRepo} onChange={(event) => setCustomRepo(event.target.value)} placeholder="/path/to/target-repo" aria-label="Repo 路徑" />}
      {repoStatus && <div className={`repo-status${repoStatus.error || !repoStatus.tree_clean ? " warning" : ""}`}>
        {repoStatus.error ? `錯誤：${repoStatus.error}` : <>分支 {repoStatus.branch || "detached / unknown"} · 工作樹 {repoStatus.tree_clean ? "乾淨" : "警告：髒"}</>}
      </div>}

      <label>Workspace 名稱 <span className="label-help">留空＝repo 目錄名</span><input value={name} onChange={(event) => setName(event.target.value)} /></label>

      <div className="form-field">
        <div className="field-label-row">
          <span>Ralph 命令</span>
          <span className="field-actions" role="group" aria-label="命令來源">
            <button type="button" className={`text-button${commandMode === "script" ? " active-toggle" : ""}`} aria-pressed={commandMode === "script"} disabled={!scripts.length} onClick={() => setCommandMode("script")}>從清單</button>
            <button type="button" className={`text-button${commandMode === "custom" ? " active-toggle" : ""}`} aria-pressed={commandMode === "custom"} onClick={() => setCommandMode("custom")}>自訂</button>
          </span>
        </div>
        {commandMode === "script"
          ? <select aria-label="Ralph 腳本" value={scriptIndex} onChange={(event) => setScriptIndex(+event.target.value)} disabled={!scripts.length}>
              {scripts.length
                ? scripts.map((script, index) => <option key={script.cmd} value={index}>{script.label} — {script.cmd}</option>)
                : <option value={0}>（設定未提供 ralph 腳本）</option>}
            </select>
          : <input value={customCommand} onChange={(event) => setCustomCommand(event.target.value)} placeholder="sh /abs/ralph.sh" aria-label="自訂 ralph 命令" />}
      </div>

      <label>Ralph 目錄 <span className="label-help">選填，prd/progress 所在；預設＝ralph.sh 目錄</span><input value={ralphDir} onChange={(event) => setRalphDir(event.target.value)} placeholder="/abs/dir-with-prd" /></label>

      <div className="number-grid">
        <label>迭代上限<input type="number" min={1} max={1000000} value={iterations} onChange={(event) => setIterations(+event.target.value)} /></label>
        <div className="form-field">
          <span>Tool</span>
          {tools.length > 0 || toolChoice !== "__custom__"
            ? <select aria-label="Tool" value={toolChoice} onChange={(event) => setToolChoice(event.target.value)}>
                {tools.map((item) => <option key={item} value={item}>{item}</option>)}
                <option value="__custom__">自訂…</option>
              </select>
            : null}
          {toolChoice === "__custom__" && <input value={customTool} onChange={(event) => setCustomTool(event.target.value)} placeholder="opencode" aria-label="自訂 tool" />}
        </div>
        <label>模型 <span className="label-help">選填</span><input value={model} onChange={(event) => setModel(event.target.value)} placeholder="xxmodel" /></label>
      </div>

      <div className="form-field">
        <span>參數風格</span>
        <select aria-label="參數風格" value={argsStyle} onChange={(event) => setArgsStyle(event.target.value as RalphArgsStyle)}>
          <option value="positional">positional（公司版：iterations tool model）</option>
          <option value="snarktank">snarktank（原版：--tool tool iterations）</option>
          <option value="custom">custom（自訂模板）</option>
        </select>
      </div>
      {argsStyle === "custom" && <input value={customArgsTemplate} onChange={(event) => setCustomArgsTemplate(event.target.value)} placeholder="{iterations} {tool} {model}" aria-label="自訂 args 模板" />}
      {commandPreview && <p className="field-help">將執行：<code>{commandPreview}</code></p>}

      <div className="form-field">
        <div className="field-label-row"><label htmlFor="ralph-prd">匯入 PRD <span className="label-help">選填，寫入 ralph 目錄</span></label>
          <span className="field-actions">
            <label className="ralph-inline-select">格式
              <select aria-label="PRD 格式" value={prdFormat} onChange={(event) => setPrdFormat(event.target.value as RalphPrdFormat)}>
                <option value="json">json</option>
                <option value="md">md</option>
              </select>
            </label>
          </span>
        </div>
        <textarea id="ralph-prd" rows={4} value={prdContent} onChange={(event) => setPrdContent(event.target.value)} placeholder="留空＝沿用 repo 內既有 PRD" />
      </div>
      <label>PRD 檔名 <span className="label-help">相對 ralph 目錄，預設 prd.json</span><input value={prdPath} onChange={(event) => setPrdPath(event.target.value)} placeholder="prd.json" /></label>

      <label className="checkbox-row"><input type="checkbox" checked={newBranch} onChange={(event) => setNewBranch(event.target.checked)} />在新 branch 跑</label>

      <details className="advanced-settings">
        <summary>進階設定：用量上限自動重啟</summary>
        <div className="form-field">
          <span>命中用量上限時</span>
          <select aria-label="用量上限行為" value={usageLimitAction} onChange={(event) => setUsageLimitAction(event.target.value as RalphUsageLimitAction)}>
            <option value="restart">等 reset 後自動重啟</option>
            <option value="downgrade">降級模型即刻重啟</option>
            <option value="off">不處理</option>
          </select>
        </div>
        <label>降級鏈 fallback_models <span className="label-help">逗號分隔，空＝不降級</span><input value={fallbackModels} onChange={(event) => setFallbackModels(event.target.value)} placeholder="sonnet, haiku" /></label>
        <label>自動重啟上限<input type="number" min={0} value={autoRestartMax} onChange={(event) => setAutoRestartMax(+event.target.value)} /></label>
      </details>
    </div>
  );
}
