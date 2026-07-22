/** 前後端 JSON 契約型別；可選欄位代表舊 workspace 或錯誤投影可能尚未提供該資料。 */
export type Phase = "plan" | "exec" | "done";

export interface WorkspaceSummary {
  name: string;
  error?: string;
  phase: Phase | null;
  running: boolean;
  draining?: boolean;
  drain_claimed?: boolean;
  round?: number;
  flag?: number;
  completed?: number;
  plan_len?: number;
  done_count?: number;
  repo?: string;
  red_streak?: number;
  stall_rounds?: number;
  issues?: number;
  latest_issue?: string;
  unread_issues?: number;
  agent_failure_streak?: number;
  agent_backoff_seconds?: number;
  last_round_seconds?: number;
  last_round_timed_out?: boolean;
  round_started_at?: string | null;
  round_deadline_at?: string | null;
  round_interrupted_at?: string | null;
  last_green_sha?: string | null;
  state_recovery_count?: number;
  state_recovery_pending?: boolean;
  goal_changed?: boolean;
  loop_pid?: number | null;
  loop_started_at?: string | null;
  stale_loop_pid?: boolean;
  current_order?: number | null;
  current_task?: string;
  resume_available?: boolean;
  /** 缺省＝舊 loop coordinator；"ralph" 走 Ralph runner 投影與 UI。 */
  runner?: WorkspaceRunner;
  /** 僅 runner==="ralph" 時提供的 fleet 摘要。 */
  ralph?: RalphSummary;
  /** 僅 runner==="parallel-supervisor" 時提供的 base run 摘要。 */
  parallel?: ParallelSummary;
  /** Active managed worker 會留在 workspace 導覽，但不納入 fleet aggregate。 */
  managed_readonly?: boolean;
  parent_workspace?: string;
  run_id?: string;
  assigned_order?: number;
  assignment?: ParallelWorkerAssignment;
}

export interface FleetHealth {
  schema_version: number;
  status: "ok" | "degraded" | "error";
  workspace_count: number;
  running: number;
  attention: number;
  error_count: number;
  issues: number;
  unread_issues: number;
  agent_failures: number;
  round_timeouts: number;
  state_recoveries: number;
  goal_changes: number;
  stale_loop_pids: number;
  generated_at: string;
}

export interface FleetHistoryEntry {
  name: string;
  data: string;
  metrics?: Omit<RoundMetrics, "samples">;
}

/** 輪次耗時／完成統計的共同核心；per-run（RoundMetrics）與 fleet 聚合（FleetRoundMetrics）各自再擴充。 */
export interface RoundMetricsBase {
  limit: number;
  sample_count: number;
  average_seconds: number | null;
  p50_seconds: number | null;
  p95_seconds: number | null;
  max_seconds: number | null;
  slowest_round: number | null;
  timeout_count: number;
  timeout_rate_pct: number;
  missing_done_count: number;
  missing_done_rate_pct: number;
  history_truncated: boolean;
}

export interface FleetRoundMetrics extends RoundMetricsBase {
  workspace_count: number;
  slowest_workspace: string | null;
}

export interface RoundTelemetrySample {
  round: number;
  seconds: number;
  timed_out: boolean;
  missing_done: boolean;
  phase: string;
  task: string;
  signal: string;
  changed: boolean;
  rc: number | null;
  validate: string;
  timestamp: string;
}

export interface AnomalyRecord extends RoundTelemetrySample {
  workspace: string;
  log_id: string | null;
  log_truncated: boolean;
}

export interface AnomalyListResponse {
  error?: string;
  limit: number;
  total_count: number;
  records: AnomalyRecord[];
}

export interface AnomalyLogResponse {
  error?: string;
  id?: string;
  workspace?: string;
  round?: number;
  timestamp?: string;
  truncated?: boolean;
  data?: string;
}

export interface RoundMetrics extends RoundMetricsBase {
  error?: string;
  run?: "current" | "previous";
  samples: RoundTelemetrySample[];
}

export interface PlanTask {
  order: number;
  task: string;
  ref?: string | null;
  /** 人工標註的 parallel batch；一般 Plan Editor 不可修改。 */
  readonly stack?: number;
}

export interface PlanEditTask {
  order: number | null;
  task: string;
  ref?: string | null;
}

export interface CompletedTask {
  order: number;
  base_sha?: string;
  sha: string;
  round?: number;
  human?: boolean;
}

export interface TaskDiffCommit {
  sha: string;
  short_sha: string;
  author: string;
  date: string;
  subject: string;
}

export interface TaskDiffFile {
  path: string;
  old_path?: string | null;
  status: "added" | "deleted" | "modified" | "renamed" | "copied" | "type_changed" | "unmerged" | "unknown";
  status_code: string;
  similarity?: number | null;
  additions: number | null;
  deletions: number | null;
  binary: boolean;
}

export interface TaskDiffResponse {
  workspace?: string;
  task?: { order: number; title: string; human: boolean; round: number };
  comparison?: {
    mode: "task_range" | "previous_task" | "single_commit";
    base_sha: string | null;
    head_sha: string;
    base_source: "recorded" | "previous_task" | "single_commit";
    warning?: string | null;
  };
  commits?: TaskDiffCommit[];
  files?: TaskDiffFile[];
  stats?: { files: number; additions: number; deletions: number; binary_files: number };
  selected_file?: TaskDiffFile;
  patch?: string;
  patch_too_large?: boolean;
  patch_limit_bytes?: number;
  error?: string;
}

export interface Issue {
  round: number;
  where?: string;
  text: string;
  ts?: string;
}

export interface DashboardConfig {
  repo?: string;
  agent_cmd?: string;
  validate_cmd?: string;
  flag_threshold?: number;
  done_threshold?: number;
  round_timeout?: number;
  agent_backoff_max?: number;
  validate_timeout?: number;
  red_limit?: number;
  stall_limit?: number;
  /** 規劃收斂後暫停：不自動進入執行期，需人工按「運行」。 */
  pause_after_plan?: boolean;
  max_parallel?: number;
  worker_restart_limit?: number;
}

export interface StartupResponse {
  ok?: boolean;
  starting?: boolean;
  job_id?: string;
  name?: string;
  pid?: number;
  startup_timeout?: number;
  control?: "pause" | "abort";
  error?: string;
}

export interface StartupStatus {
  status?: "starting" | "ready" | "failed";
  pid?: number;
  rc?: number | null;
  error?: string;
  tail?: string;
}

export interface WorkspaceState {
  error?: string;
  phase: Phase;
  round: number;
  flag: number;
  done_count: number;
  red_streak: number;
  stall_rounds: number;
  agent_failure_streak?: number;
  agent_backoff_seconds?: number;
  agent_backoff_until?: string | null;
  last_round_seconds?: number;
  last_round_timed_out?: boolean;
  round_started_at?: string | null;
  round_deadline_at?: string | null;
  round_interrupted_at?: string | null;
  last_green_sha?: string | null;
  state_recovery_count?: number;
  last_state_recovery?: string | null;
  state_recovery_pending?: boolean;
  plan_version: number;
  current_order?: number;
  current_task_base_sha?: string | null;
  goal_changed?: boolean;
  plan?: PlanTask[];
  completed?: CompletedTask[];
  issues?: Issue[];
  issues_acknowledged_round?: number;
  task_reset_counts?: Record<string, number>;
  /** loop 用 DashboardConfig；ralph runner 另帶 RalphConfig 欄位（皆為可選，不影響 loop 讀取）。 */
  config?: DashboardConfig & RalphConfig;
  /** 缺省＝loop coordinator；"ralph" 時 RalphView 只讀 state.ralph 與 state.config。 */
  runner?: WorkspaceRunner;
  /** runner==="ralph" 的 state.ralph 區塊（見 RALPH_CONTRACT §A + §I）。 */
  ralph?: RalphState;
  /** parallel base 的 supervisor 投影。 */
  parallel?: ParallelSummary;
  /** managed worker 永遠為 true，前端不得顯示 mutation controls。 */
  managed_readonly?: boolean;
  parent_workspace?: string;
  run_id?: string;
  assigned_order?: number;
  assignment?: ParallelWorkerAssignment;
}

export interface SelectCommand {
  label: string;
  cmd: string;
}

export interface PromptTemplate {
  id: string;
  label: string;
  category: string;
  description: string;
  instructions: string;
  requirement_placeholder: string;
  source: "builtin" | "team";
}

export interface PromptTemplateBundle {
  schema_version: 4;
  base: string;
  goal: string;
  goal_template: string;
  plan: string;
  /** Goal 模式勾選「同時產生初版 plan.json」時，插在 goal 與 plan 契約之間的合併輸出契約。 */
  goal_plan_bridge: string;
  missing_requirement: string;
  team_template_example: string;
}

export interface ConfigResponse {
  error?: string;
  agent_cmds: SelectCommand[];
  validate_cmds: SelectCommand[];
  repos: string[];
  defaults: DashboardConfig;
  extra_path_dirs?: string[];
  resolved_extra_path_dirs?: string[];
  config_path?: string;
  personal_config_path?: string;
  project_config_path?: string;
  config_override?: boolean;
  notify_cmd?: string;
  repo_roots?: string[];
  prompt_templates?: PromptTemplate[];
  prompt_template_bundle?: PromptTemplateBundle | null;
  prompt_template_bundle_error?: string | null;
  prompt_template_warnings?: string[];
  /** Ralph runner 啟動投影（RALPH_CONTRACT §G + §I）；缺省＝後端尚未提供 ralph 支援。 */
  ralph?: RalphConfigProjection;
}

export interface JobInfo {
  id?: string;
  kind?: string;
  name: string;
  repo: string;
  pid: number;
  alive: boolean;
  rc?: number | null;
  tail?: string;
}

export interface IncrementalResponse {
  size: number;
  data: string;
  truncated?: boolean;
  run?: "current" | "previous";
  error?: string;
}

export interface BootstrapResponse {
  readonly: boolean;
  preselect: string;
}

/* ------------------------------------------------------------------ *
 * Ralph runner（RALPH_CONTRACT）：第二種 workspace runner 的 JSON 契約。
 * 全部欄位皆為可選，反映後端可能尚未提供 ralph 支援或錯誤投影缺欄位。
 * ------------------------------------------------------------------ */

export type WorkspaceRunner = "loop" | "ralph" | "parallel-supervisor" | "parallel-worker";
/** POST /api/launch 使用的 runner selector；parallel 啟動後投影為 parallel-supervisor。 */
export type LaunchRunner = "loop" | "ralph" | "parallel";

export type ParallelRunStatus =
  | "initializing" | "running" | "pause_requested" | "paused"
  | "cancel_requested" | "finalizing" | "finalizing_cancel" | "blocked"
  | "completed" | "cancelled";

export interface ParallelTaskStatus {
  order: number;
  batch: number;
  outcome: "pending" | "integrated" | "blocked" | "cancelled";
  resource_state: string;
  restart_count: number;
  error?: string | null;
}

export interface ParallelSummary {
  run_id?: string;
  status?: ParallelRunStatus;
  terminal_intent?: "completed" | "cancelled" | null;
  batch?: number | null;
  tasks?: ParallelTaskStatus[];
  error?: string | null;
}

export interface ParallelWorkerAssignment {
  status?: "running" | "paused" | "recovery-required" | "integrated" | "blocked" | "cancelled";
  validated_sha?: string | null;
  validated_round?: number | null;
  exit_reason?: string | null;
  pause_generation?: number;
}
/** ralph.sh 退出後的終態原因（RALPH_CONTRACT §A）。 */
export type RalphExitReason =
  | "completed"
  | "iterations_exhausted"
  | "failed"
  | "interrupted"
  | "usage_limit_giveup";
export type RalphArgsStyle = "positional" | "snarktank" | "custom";
export type RalphUsageLimitAction = "restart" | "downgrade" | "off";
export type RalphPrdFormat = "json" | "md";

/** state.config（ralph runner）追加欄位；與 DashboardConfig 交集使用，皆可選避免影響 loop。 */
export interface RalphConfig {
  runner?: WorkspaceRunner;
  ralph_cmd?: string;
  ralph_dir?: string;
  iterations?: number;
  tool?: string;
  model?: string;
  args_template?: string[];
  prd_path?: string;
  notify_cmd?: string;
  /** usage-limit 自動重啟／降級（RALPH_CONTRACT §I）。 */
  usage_limit_action?: RalphUsageLimitAction;
  fallback_models?: string[];
  auto_restart_max?: number;
  usage_limit_patterns?: string[];
  auto_restart_backoff_max_sec?: number;
}

/** state.ralph.stories 與 /api/ralph/prd 的 story；state 版只帶 id/title/passes/priority。 */
export interface RalphStory {
  id: string;
  title: string;
  passes: boolean;
  priority?: number;
  description?: string;
  acceptanceCriteria?: string[];
  notes?: string;
}

/** state.ralph.usage_limit（命中時，RALPH_CONTRACT §I）。 */
export interface RalphUsageLimit {
  detected_at?: string;
  matched?: string;
  action: "waiting" | "downgraded" | "giveup";
  resume_at?: string | null;
  reset_source?: "parsed" | "backoff";
  wait_seconds?: number;
  from_model?: string;
  to_model?: string;
}

/** state.ralph 區塊（RALPH_CONTRACT §A + §I）。 */
export interface RalphState {
  prd_format?: RalphPrdFormat | null;
  prd_path?: string;
  project?: string;
  branch_name?: string;
  stories?: RalphStory[];
  stories_total?: number;
  stories_done?: number;
  iteration?: number;
  max_iterations?: number;
  base_sha?: string;
  head_sha?: string;
  commit_count?: number;
  last_commit?: string;
  progress_bytes?: number;
  sentinel_complete?: boolean;
  stalled?: boolean;
  exit_code?: number | null;
  exit_reason?: RalphExitReason | null;
  prd_error?: string | null;
  updated_at?: string;
  active_model?: string;
  restart_attempt?: number;
  usage_limit?: RalphUsageLimit | null;
}

/** WorkspaceSummary.ralph（RALPH_CONTRACT §B）；usage_limit 供 fleet ⏳ 標記（§I）。 */
export interface RalphSummary {
  stories_done?: number;
  stories_total?: number;
  iteration?: number;
  max_iterations?: number;
  sentinel_complete?: boolean;
  stalled?: boolean;
  exit_reason?: RalphExitReason | null;
  usage_limit?: RalphUsageLimit | null;
}

/** GET /api/ralph/prd 回應（RALPH_CONTRACT §E）。 */
export interface RalphPrdResponse {
  error?: string;
  prd_format?: RalphPrdFormat | null;
  prd_path?: string;
  project?: string;
  branch_name?: string;
  stories?: RalphStory[];
  stories_total?: number;
  stories_done?: number;
  raw?: string;
}

/** config_projection.ralph（RALPH_CONTRACT §G + §I）。 */
export interface RalphScript {
  label: string;
  cmd: string;
}
export interface RalphConfigProjection {
  scripts: RalphScript[];
  tools: string[];
  default_iterations: number;
  prd_filenames: string[];
  default_args_style: RalphArgsStyle;
  default_usage_limit_action?: RalphUsageLimitAction;
  default_fallback_models?: string[];
  default_auto_restart_max?: number;
}

/** POST /api/launch（runner:"ralph"）請求（RALPH_CONTRACT §C + §I）。 */
export interface RalphLaunchRequest {
  runner: "ralph";
  repo: string;
  name?: string;
  ralph_idx?: number;
  ralph_custom?: string;
  ralph_dir?: string;
  iterations: number;
  tool: string;
  model?: string;
  args_style: RalphArgsStyle;
  args_template?: string[];
  prd_content?: string;
  prd_format?: RalphPrdFormat;
  prd_path?: string;
  new_branch?: boolean;
  usage_limit_action?: RalphUsageLimitAction;
  fallback_models?: string[];
  auto_restart_max?: number;
}
