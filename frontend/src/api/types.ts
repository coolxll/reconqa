export type RoleTag = string;

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

export interface ProjectUpdate {
  name?: string;
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

export interface TargetUpdate {
  root_domain?: string;
  base_url?: string;
  scope_rules?: Record<string, unknown>;
}

export interface DiscoveredPortal {
  host: string;
  url: string;
  status_code: number;
  title: string;
  is_api_gateway: boolean;
  suggested_label: string;
}

export interface DiscoverPortalsResponse {
  root_domain: string;
  env_suffix: string;
  portals: DiscoveredPortal[];
}

export interface BatchTargetItem {
  root_domain: string;
  base_url: string;
  portal_label?: string;
}

export interface BatchTargetsResponse {
  added: Target[];
  skipped: number;
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
  name?: string | null;
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
  "x-host"?: string;
  "x-origin"?: string;
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

// --- Project Master API Inventory & Inline Replay ---

export interface InventorySeenInRun {
  run_id: string;
  run_name: string;
}

export interface InventoryEndpoint {
  method: string;
  path: string;
  summary?: string;
  discovery_source: "har-live" | "js-static-inferred" | "js-static" | "openapi-doc" | string;
  host?: string;
  custom_headers?: Record<string, string>;
  tags?: string[];
  scenarios: string[];
  seen_in_runs: InventorySeenInRun[];
  status_codes: number[];
  parameters?: Array<Record<string, unknown>>;
  request_schema?: Record<string, unknown> | null;
  response_schema?: Record<string, unknown> | null;
  request_example?: unknown;
  response_example?: unknown;
}

export interface InventoryPrefixStats {
  prefix: string;
  total: number;
  covered: number;
  coverage_pct: number;
}

export interface InventoryScenarioMeta {
  id: string;
  slug: string;
  name: string;
  priority: string;
  business_domain: string;
  effectiveness_level: string;
  description: string;
}

export interface ProjectInventoryResponse {
  project_id: string;
  project_name: string;
  runs_merged_count: number;
  explore_sessions_count: number;
  total_endpoints: number;
  har_live_count: number;
  js_static_count: number;
  coverage_pct: number;
  scenarios: string[];
  scenario_meta?: Record<string, InventoryScenarioMeta>;
  by_prefix: InventoryPrefixStats[];
  endpoints: InventoryEndpoint[];
}

// --- First-Class Business Scenarios & Effectiveness Model ---

export type ScenarioPriority = "P0" | "P1" | "P2" | "P3";
export type ScenarioStatus = "draft" | "active" | "verified" | "archived";
export type ScenarioSource = "har-auto" | "manual" | "ai-agent";
export type ScenarioEffectivenessLevel = "L0" | "L1" | "L2" | "L3" | "L4";

export type ScenarioAssertionOperator =
  | "equals"
  | "not_equals"
  | "not_empty"
  | "gt"
  | "gte"
  | "lt"
  | "lte"
  | "contains"
  | "matches"
  | "custom";

export type ScenarioAssertionStatus = "pending" | "passed" | "failed";

export type ScenarioNegativeCategory =
  | "auth"
  | "validation"
  | "boundary"
  | "idempotency"
  | "state_machine"
  | "security";

export type ScenarioNegativeStatus = "pending" | "covered" | "failed";

export interface ScenarioAssertion {
  id: string;
  title: string;
  target_endpoint: string;
  jsonpath: string;
  operator: ScenarioAssertionOperator;
  expected: string;
  note: string;
  status: ScenarioAssertionStatus;
}

export interface ScenarioNegativeCase {
  id: string;
  title: string;
  category: ScenarioNegativeCategory;
  expected_behavior: string;
  status: ScenarioNegativeStatus;
  note: string;
}

export interface ScenarioItem {
  id: string;
  project_id: string;
  slug: string;
  name: string;
  description: string;
  preconditions: string;
  business_domain: string;
  priority: ScenarioPriority;
  status: ScenarioStatus;
  owner: string;
  source: ScenarioSource;
  tags: string[];
  assertions: ScenarioAssertion[];
  negative_cases: ScenarioNegativeCase[];
  endpoint_count: number;
  run_count: number;
  last_run_id: string | null;
  last_run_name: string | null;
  assertion_count: number;
  assertions_passed_count: number;
  negative_case_count: number;
  negative_cases_covered_count: number;
  effectiveness_level: ScenarioEffectivenessLevel;
  created_at: string;
  updated_at: string;
}

export interface ScenarioLinkedRun {
  run_id: string;
  run_name: string;
  status: string;
  created_at: string;
}

export interface ScenarioDetail extends ScenarioItem {
  endpoints: InventoryEndpoint[];
  runs: ScenarioLinkedRun[];
}

export interface ScenarioListResponse {
  project_id: string;
  total: number;
  by_priority: Record<string, number>;
  by_effectiveness: Record<string, number>;
  p0_p1_total: number;
  p0_p1_effective_count: number;
  p0_p1_effective_pct: number;
  scenarios: ScenarioItem[];
}

export interface ScenarioCreate {
  slug?: string;
  name: string;
  description?: string;
  preconditions?: string;
  business_domain?: string;
  priority?: ScenarioPriority;
  status?: ScenarioStatus;
  owner?: string;
  source?: ScenarioSource;
  tags?: string[];
  assertions?: Array<Partial<ScenarioAssertion> & { title: string }>;
  negative_cases?: Array<Partial<ScenarioNegativeCase> & { title: string }>;
}

export interface ScenarioUpdate {
  name?: string;
  description?: string;
  preconditions?: string;
  business_domain?: string;
  priority?: ScenarioPriority;
  status?: ScenarioStatus;
  owner?: string;
  tags?: string[];
  assertions?: ScenarioAssertion[];
  negative_cases?: ScenarioNegativeCase[];
}

export interface ReplayRequest {
  method: string;
  url: string;
  headers?: Record<string, string>;
  query_params?: Record<string, string>;
  body?: unknown;
  credential_id?: string | null;
  timeout_sec?: number;
}

export interface ReplayResponse {
  status_code: number;
  status_text: string;
  duration_ms: number;
  response_headers: Record<string, string>;
  body_text: string;
  body_json?: unknown;
  content_type: string;
  truncated: boolean;
  error?: string | null;
}

export interface ReplayMatrixRequest {
  method: string;
  url: string;
  headers?: Record<string, string>;
  query_params?: Record<string, string>;
  body?: unknown;
  credential_ids?: string[] | null;
  include_anonymous?: boolean;
  timeout_sec?: number;
}

export interface ReplayMatrixEntry {
  credential_id: string | null;
  credential_name: string;
  role_tag: string;
  status_code: number;
  biz_code?: number | string | null;
  duration_ms: number;
  body_size: number;
  body_preview: string;
  verdict: "allowed" | "denied" | "error";
  error?: string | null;
}

export interface ReplayMatrixResponse {
  method: string;
  url: string;
  results: ReplayMatrixEntry[];
  allowed_count: number;
  denied_count: number;
  potential_bfla_warning: boolean;
}

// --- Autonomous Acceptance Glue APIs ---

export interface ExtractCredentialRequest {
  run_id?: string | null;
  name?: string | null;
  role_tag?: RoleTag;
  target_host?: string | null;
}

export interface ExtractCredentialResponse {
  profile: AuthProfile;
  extracted_header_names: string[];
  source_run_id: string;
  source_host: string;
}

export type ExternalToolName = "java_test" | "schemathesis" | "nuclei" | "k6";

export interface ExecuteExternalToolRequest {
  tool: ExternalToolName;
  credential_id?: string | null;
  base_url?: string | null;
  sync_scenarios?: boolean;
  timeout_sec?: number;
}

export interface ExecuteExternalToolResponse {
  executed: boolean;
  tool: string;
  status: string;
  duration_ms: number;
  summary: Record<string, unknown>;
  stdout_tail: string;
  synced_scenarios: number;
  synced_assertions: number;
  synced_negative_cases: number;
  error?: string | null;
}

export type AcceptancePromptAgent = "generic" | "antigravity" | "claude" | "codex" | "openclaw";
export type AcceptancePromptMode = "full" | "har_explore";

export interface AcceptancePromptContextSummary {
  targets_count: number;
  credentials_count: number;
  kb_docs_count: number;
  coverage_pct: number;
  p0_p1_effective_pct: number;
  scenarios_needing_attention: number;
}

export interface AcceptancePromptResponse {
  project_id: string;
  project_name: string;
  lang: string;
  agent: string;
  mode?: AcceptancePromptMode;
  prompt_markdown: string;
  context_summary: AcceptancePromptContextSummary;
}

// --- Business Capability Map (Resource x Action x Role x State) ---

export type CapabilitySlotName =
  | "happy_path"
  | "invalid_params"
  | "illegal_state"
  | "authz_role"
  | "consistency_idempotency";

export type CapabilitySlotStatusValue = "covered" | "pending" | "failed" | "missing";

export interface CapabilitySlotStatus {
  slot: CapabilitySlotName;
  status: CapabilitySlotStatusValue;
  count: number;
}

export interface CapabilityEndpointRef {
  method: string;
  path: string;
  action: string;
  role: string;
  discovery_source: string;
  host?: string | null;
  custom_headers?: Record<string, string>;
}

export interface CapabilityScenarioRef {
  id: string;
  slug: string;
  name: string;
  priority: ScenarioPriority;
  effectiveness_level?: string;
}

export interface BusinessCapability {
  key: string;
  module: string;
  resource: string;
  display_name: string;
  title_zh?: string;
  title_en?: string;
  is_core?: boolean;
  total_endpoints: number;
  live_endpoints: number;
  write_endpoints: number;
  read_endpoints: number;
  live_pct: number;
  roles: string[];
  actions: string[];
  observed_statuses: Record<string, string[]>;
  has_state_machine: boolean;
  priority_score: number;
  recommended_priority: ScenarioPriority;
  linked_scenarios: CapabilityScenarioRef[];
  case_matrix: CapabilitySlotStatus[];
  case_coverage_pct: number;
  endpoints: CapabilityEndpointRef[];
}

export interface CapabilityQualityMetrics {
  total_endpoints: number;
  total_capabilities: number;
  core_capabilities?: number;
  live_capabilities: number;
  target_capabilities: number;
  capability_coverage_pct: number;
  stateful_capabilities: number;
  state_transition_covered: number;
  state_coverage_pct: number;
  role_matrix_cells_total: number;
  role_matrix_cells_covered: number;
  role_matrix_coverage_pct: number;
  negative_slots_total: number;
  negative_slots_tested: number;
  negative_coverage_pct: number;
  invariant_assertions_total: number;
  invariant_assertions_passed: number;
  invariant_pass_rate_pct: number;
  slots_passed?: number;
  slots_failed?: number;
  slots_pending?: number;
  slots_missing?: number;
}

export interface CapabilityModuleSummary {
  module: string;
  capabilities: number;
  live_capabilities: number;
  endpoints: number;
}

export interface ProjectCapabilityMapResponse {
  project_id: string;
  project_name: string;
  metrics: CapabilityQualityMetrics;
  modules: CapabilityModuleSummary[];
  capabilities: BusinessCapability[];
}

export interface PromoteCapabilityRequest {
  capability_key: string;
  priority?: ScenarioPriority;
  name?: string;
  lang?: "zh" | "en";
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
