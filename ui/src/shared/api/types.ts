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
  state_recovery_count?: number;
  state_recovery_pending?: boolean;
  goal_changed?: boolean;
  loop_pid?: number | null;
  loop_started_at?: string | null;
  stale_loop_pid?: boolean;
  current_order?: number | null;
  current_task?: string;
  resume_available?: boolean;
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
}

export interface PlanEditTask {
  order: number | null;
  task: string;
  ref?: string | null;
}

export interface CompletedTask {
  order: number;
  sha: string;
  human?: boolean;
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
}

export interface StartupResponse {
  ok?: boolean;
  starting?: boolean;
  name?: string;
  pid?: number;
  startup_timeout?: number;
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
  state_recovery_count?: number;
  last_state_recovery?: string | null;
  state_recovery_pending?: boolean;
  plan_version: number;
  current_order?: number;
  goal_changed?: boolean;
  plan?: PlanTask[];
  completed?: CompletedTask[];
  issues?: Issue[];
  issues_acknowledged_round?: number;
  task_reset_counts?: Record<string, number>;
  config?: DashboardConfig;
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
  schema_version: 3;
  base: string;
  goal: string;
  goal_template: string;
  plan: string;
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
}

export interface JobInfo {
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
