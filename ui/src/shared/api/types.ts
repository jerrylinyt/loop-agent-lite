export type Phase = "plan" | "exec" | "done";

export interface WorkspaceSummary {
  name: string;
  phase: Phase | null;
  running: boolean;
  round?: number;
  flag?: number;
  completed?: number;
  plan_len?: number;
  done_count?: number;
  repo?: string;
}

export interface PlanTask {
  order: number;
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
  state_recovery_count?: number;
  last_state_recovery?: string | null;
  state_recovery_pending?: boolean;
  plan_version: number;
  current_order?: number;
  goal_changed?: boolean;
  plan?: PlanTask[];
  completed?: CompletedTask[];
  issues?: Issue[];
  task_reset_counts?: Record<string, number>;
  config?: DashboardConfig;
}

export interface SelectCommand {
  label: string;
  cmd: string;
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
  repo_roots?: string[];
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
}

export interface BootstrapResponse {
  readonly: boolean;
  preselect: string;
}
