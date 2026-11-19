interface Rate {
  numerator: number;
  denominator: number;
  value: number | null;
  wilson95: [number, number] | null;
}
export interface BenchmarkMetrics {
  label: string;
  scheduled: number;
  scored: number;
  verified_success: Rate;
  feature_pass: Rate;
  regression_pass: Rate;
  build_success: Rate;
  mean_agent_seconds: number | null;
  sample_agent_seconds_sd: number | null;
  input_tokens_total: number | null;
  output_tokens_total: number | null;
  cost_usd_total: number | null;
  failure_counts: Record<string, number>;
}
export interface Trial {
  requested_model?: string | null;
  configuration?: { model: string } | null;
  role: 'oracle' | 'agent' | 'control';
  origin: 'harbor' | 'test_double';
  label: string;
  attempt: number;
  agent_name: string | null;
  agent_version: string | null;
  resolved_models: string[];
  reward: 0 | 1 | null;
  trustworthy: boolean;
  failure: string | null;
  flags: string[];
  process: { argv: string[]; exit_code: number | null; console_log: string; timed_out: boolean } | null;
  submitted_files: { path: string; operation: string }[] | null;
}
export interface EvaluationReport {
  id: string;
  task_id: string;
  mode: 'agent' | 'control';
  state: 'queued' | 'running' | 'completed' | 'blocked' | 'quarantined' | 'failed';
  detail: string | null;
  failure: string | null;
  harbor_version: string | null;
  docker_engine: string | null;
  package: {
    snapshot_id: string; snapshot_sha256: string; starting_commit_sha: string | null;
    solution_sha256: string; test_suite_sha256: string; package_sha256: string;
    runtime_image: string; language: string; category: string; difficulty: string;
  } | null;
  oracle: Trial | null;
  baseline_control: Trial | null;
  trials: Trial[];
  metrics: BenchmarkMetrics[];
}
