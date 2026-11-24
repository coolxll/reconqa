export type RoleTag = "account_a" | "account_b" | "guest";

export type TriggerSource = "cli" | "web" | "ci";

export interface Project {
  id: string;
  slug: string;
  name: string;
  description: string;
  created_at: string;
}

export interface ProjectCreate {
  name: string;
  slug?: string | null;
  description?: string;
}

export interface Target {
  id: string;
  project_id: string;
  root_domain: string;
  base_url: string;
  scope_rules: Record<string, unknown>;
}

export interface TargetCreate {
  root_domain: string;
  base_url: string;
  scope_rules?: Record<string, unknown>;
}

export interface AuthProfile {
  id: string;
  project_id: string;
  name: string;
  role_tag: RoleTag | string;
  storage_state_path: string | null;
  extra_headers_ref: Record<string, string>;
}

export interface AuthProfileCreate {
  name: string;
  role_tag: RoleTag;
  storage_state_path?: string | null;
  extra_headers_ref?: Record<string, string>;
}

export type KnowledgeDocType =
  | "prd"
  | "api_spec"
  | "business_rule"
  | "test_notes"
  | "architecture"
  | "other";

export type KnowledgeDocSourceKind = "markdown" | "file" | "link";

export interface KnowledgeDoc {
  id: string;
  project_id: string;
  title: string;
  doc_type: KnowledgeDocType | string;
  source_kind: KnowledgeDocSourceKind | string;
  content: string;
  file_path: string | null;
  original_filename: string | null;
  external_url: string | null;
  tags: string[];
  related_endpoints: string[];
  size_bytes: number;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeDocCreate {
  title: string;
  doc_type?: KnowledgeDocType;
  source_kind?: KnowledgeDocSourceKind;
  content?: string;
  external_url?: string | null;
  tags?: string[];
  related_endpoints?: string[];
}

export interface KnowledgeDocUpdate {
  title?: string;
  doc_type?: KnowledgeDocType;
  content?: string;
  external_url?: string | null;
  tags?: string[];
  related_endpoints?: string[];
}

export interface Job {
  id: string;
  run_id: string;
  module: string;
  status: string;
  log_path: string | null;
  output_meta: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunSummaryMetrics {
  total_endpoints?: number;
  har_live_endpoints?: number;
  coverage_pct?: number;
  correlation_chains?: number;
  findings_total?: number;
  findings_by_severity?: Record<string, number>;
  slow_endpoints_count?: number;
  [key: string]: unknown;
}

export interface Run {
  id: string;
  project_id: string;
  target_id: string | null;
  status: string;
  trigger_source: string;
  workspace_dir: string;
  config_snapshot: Record<string, unknown>;
  summary_metrics: RunSummaryMetrics;
  started_at: string | null;
  finished_at: string | null;
  jobs: Job[];
}

export interface RunCreate {
  target_id?: string | null;
  target_url?: string | null;
  modules?: string[];
  trigger_source?: TriggerSource;
  config?: Record<string, unknown>;
}

export interface ExecuteModulesConfig {
  target_url?: string | null;
  auth_a_profile_id?: string | null;
  auth_b_profile_id?: string | null;
  include_mutations?: boolean;
  run_nuclei?: boolean;
  run_k6?: boolean;
  java_package?: string;
  java_version?: string;
  [key: string]: unknown;
}

export interface ExecuteModulesRequest {
  modules: string[];
  config?: ExecuteModulesConfig;
}

export interface HarUploadResponse {
  run_id: string;
  import_result: Record<string, unknown>;
  har_files: string[];
  total_hars: number;
}

export interface HarReuseResponse {
  run_id: string;
  source_run_id: string;
  reused: string[];
  skipped_duplicates: string[];
  total_hars: number;
}

export type ImportTextFormat = "auto" | "curl" | "fetch" | "raw_http" | "har";

export interface ImportTextRequest {
  content: string;
  scenario?: string;
  format?: ImportTextFormat | string;
  target_domain?: string | null;
}

export interface ImportTextResponse {
  run_id?: string;
  scenario: string;
  detected_format: string;
  requests_imported: number;
  har_path: string;
  har_files?: string[];
  total_hars?: number;
  [key: string]: unknown;
}

export interface BurpExportRequest {
  endpoints?: string[] | null;
  target_domain?: string | null;
}

export interface BurpExportResponse {
  run_id: string;
  burp_export: {
    exported_count?: number;
    output_dir?: string;
    files?: string[];
    curl_script?: string;
    [key: string]: unknown;
  };
}

export interface Finding {
  id: string;
  run_id: string;
  category: string;
  severity: string;
  endpoint: string;
  title: string;
  evidence: Record<string, unknown>;
}

export interface Artifact {
  id: string;
  run_id: string;
  kind: string;
  rel_path: string;
  size_bytes: number;
  sha256: string;
}

// --- OpenAPI ---

export interface OpenApiOperation {
  summary?: string;
  operationId?: string;
  tags?: string[];
  parameters?: Array<Record<string, unknown>>;
  requestBody?: Record<string, unknown>;
  responses?: Record<string, Record<string, unknown>>;
  "x-discovery-source"?: string;
  "x-scenarios"?: string[];
  "x-biz-success"?: Record<string, unknown>;
  "x-perf-baseline"?: Record<string, unknown>;
  "x-custom-headers"?: Record<string, string> | string[];
  "x-js-snippet"?: string;
  [key: string]: unknown;
}

export type OpenApiPathItem = Record<string, OpenApiOperation | unknown>;

export interface OpenApiSpec {
  openapi?: string;
  info?: {
    title?: string;
    version?: string;
    description?: string;
    [key: string]: unknown;
  };
  servers?: Array<{ url: string; description?: string }>;
  paths: Record<string, OpenApiPathItem>;
  components?: Record<string, unknown>;
  [key: string]: unknown;
}

// --- Correlation & Coverage ---

export interface CorrelationEndpointRef {
  step: number;
  scenario: string;
  method: string;
  path: string;
  raw_url: string;
  location: "path" | "query" | "header" | "body" | "response" | null;
  param: string | null;
  jsonpath: string | null;
  sample: string | null;
}

export interface CorrelationVariable {
  var_name: string;
  producer: CorrelationEndpointRef;
  consumers: CorrelationEndpointRef[];
  confidence: number;
  filtered_reason: string | null;
}

export interface CorrelationChainStep {
  step: number;
  scenario: string;
  method: string;
  path: string;
  raw_url: string;
  request_headers: Record<string, string>;
  query_params: Record<string, string>;
  request_body: unknown;
  expected_status: number;
  extracts: Record<string, string>;
  uses: Record<string, string>;
}

export interface CorrelationChain {
  name: string;
  scenario: string;
  steps: CorrelationChainStep[];
  cleanup_step: CorrelationChainStep | null;
}

export interface CorrelationReport {
  variables: CorrelationVariable[];
  chains: CorrelationChain[];
  filtered_constants: string[];
  mermaid: string;
}

export interface CoveragePrefixStats {
  prefix: string;
  total: number;
  covered: number;
  uncovered: number;
  coverage_pct: number;
}

export interface UncoveredEndpoint {
  method: string;
  path: string;
  source: string;
  priority: "high" | "medium" | "low";
  scenarios: string[];
  js_snippet: string;
  inferred_params: string[];
}

export interface CoverageDiffReport {
  total_endpoints: number;
  covered_count: number;
  uncovered_count: number;
  js_static_inferred_uncovered_count: number;
  js_static_uncovered_count: number;
  coverage_pct: number;
  by_prefix: CoveragePrefixStats[];
  covered_endpoints: Array<Record<string, unknown>>;
  uncovered_endpoints: UncoveredEndpoint[];
}

// --- Performance ---

export interface PerfEndpointMetric {
  method?: string;
  path?: string;
  url?: string;
  status?: number;
  wait_ms?: number;
  ttfb_ms?: number;
  total_ms?: number;
  size_bytes?: number;
  content_type?: string;
  encoding?: string;
  count?: number;
  [key: string]: unknown;
}

export interface PerfAuditReport {
  total_requests: number;
  total_bytes?: number;
  slowest_ttfb: PerfEndpointMetric[];
  slowest_total: PerfEndpointMetric[];
  largest_responses: PerfEndpointMetric[];
  uncompressed_responses: PerfEndpointMetric[];
  duplicate_requests: PerfEndpointMetric[];
  cache_stats: {
    hit?: number;
    miss?: number;
    bypass?: number;
    dynamic?: number;
    hit_rate_pct?: number;
    [key: string]: unknown;
  };
  findings: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface K6AnalysisReport {
  p50_ms?: number;
  p90_ms?: number;
  p95_ms?: number;
  p99_ms?: number;
  rps?: number;
  total_requests?: number;
  http_429_rate?: number;
  http_5xx_rate?: number;
  error_rate?: number;
  diagnosis?: string;
  verdict?: string;
  recommendations?: string[];
  [key: string]: unknown;
}

export interface PerformanceReport {
  run_id: string;
  har_profiler: PerfAuditReport | null;
  k6_analysis: K6AnalysisReport | null;
  k6_script_available: boolean;
}

// --- Security ---

export interface SecurityAuditReport {
  hosts_audited: number;
  header_coverage: Record<string, Record<string, unknown>>;
  cookie_issues: Array<Record<string, unknown>>;
  secret_findings: Array<Record<string, unknown>>;
  findings: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface AuthMatrixResult {
  endpoint: string;
  method: string;
  verdict: string;
  baseline_status?: number;
  replay_status?: number;
  unauth_status?: number;
  similarity?: number;
  reason?: string;
  [key: string]: unknown;
}

export interface AuthMatrixReport {
  tested_endpoints: number;
  unauth_bypass_count: number;
  confirmed_idor_count: number;
  suspected_idor_count: number;
  mutation_candidate_count: number;
  results: AuthMatrixResult[];
  findings: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface SecurityReport {
  run_id: string;
  static_audit: SecurityAuditReport | null;
  auth_matrix: AuthMatrixReport | null;
  nuclei: Array<Record<string, unknown>>;
  burp_exports_available: boolean;
}

// --- Java / Allure ---

export interface JavaProjectMeta {
  l1_cases: number;
  l2_chains: number;
  l3_checks: number;
  package_name?: string;
  java_version?: string;
  [key: string]: unknown;
}

export interface AllureTestResult {
  name: string;
  status: string;
  duration_ms: number;
  epic?: string;
  feature?: string;
  story?: string;
  message?: string;
  [key: string]: unknown;
}

export interface JavaExecutionSummary {
  total?: number;
  passed?: number;
  failed?: number;
  broken?: number;
  skipped?: number;
  duration_ms?: number;
  executed_at?: string;
  [key: string]: unknown;
}

export interface JavaAllureReport {
  run_id: string;
  generated: boolean;
  project_meta: JavaProjectMeta;
  execution_summary: JavaExecutionSummary | null;
  allure_results: AllureTestResult[];
  allure_report_url: string | null;
  schemathesis: Record<string, unknown> | null;
}

// --- System Doctor ---

export interface DoctorToolStatus {
  available: boolean;
  path: string | null;
  category: string;
  install_hint: string;
  purpose_zh?: string;
  purpose_en?: string;
  impact_zh?: string;
  impact_en?: string;
  install_cmd?: string | null;
  docker_alt?: string | null;
  auto_installable?: boolean;
}

export interface DoctorInstallResult {
  ok: boolean;
  tool: string;
  returncode: number;
  output: string;
  doctor?: Record<string, DoctorToolStatus>;
}

// --- Trends ---

export interface TrendPoint {
  run_id: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  total_endpoints: number;
  har_live_endpoints: number;
  coverage_pct: number;
  correlation_chains: number;
  findings_total: number;
  findings_by_severity: Record<string, number>;
  slow_endpoints_count: number;
}

export interface ProjectTrendsResponse {
  project_id: string;
  project_name: string;
  total_runs: number;
  trends: TrendPoint[];
}

// --- SSE Events ---

export interface SseRunEvent {
  event: string;
  run_id?: string;
  module?: string;
  status?: string;
  message?: string;
  timestamp?: string;
  summary_metrics?: RunSummaryMetrics;
  jobs?: Array<{ id: string; module: string; status: string }>;
  [key: string]: unknown;
}
