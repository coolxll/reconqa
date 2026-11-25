import { apiFetch } from "./client";
import type { AuthProfile, DiscoveredPortal, Run } from "./types";

export type ExploreMode = "passive" | "active";

export type ExploreAuthType =
  | "none"
  | "profile"
  | "headers"
  | "bearer"
  | "cookie"
  | "basic"
  | "login_api"
  | "form_login";

export interface ExploreAuthConfig {
  auth_type: ExploreAuthType;
  credential_id?: string | null;
  token?: string | null;
  token_header?: string;
  token_prefix?: string;
  cookie?: string | null;
  headers?: Record<string, string>;
  username?: string | null;
  password?: string | null;
  login_url?: string | null;
  login_method?: string;
  login_content_type?: "json" | "form";
  username_field?: string;
  password_field?: string;
  extra_fields?: Record<string, unknown>;
  token_jsonpath?: string | null;
  username_selector?: string | null;
  password_selector?: string | null;
  submit_selector?: string | null;
  save_as_profile?: boolean;
  profile_name?: string | null;
  role_tag?: string;
  verify_url?: string | null;
  scope_domains?: string[];
}

export interface ExploreVerifyAuthRequest {
  verify_url?: string | null;
  target_url?: string | null;
  project_id?: string | null;
  credential_id?: string | null;
  auth?: ExploreAuthConfig | null;
}

export interface ExploreVerifyAuthResponse {
  valid: boolean;
  verdict: "valid" | "denied" | "error";
  auth_mode: string;
  header_names: string[];
  status_code: number;
  biz_code?: number | string | null;
  redirected_to?: string | null;
  duration_ms: number;
  message: string;
  warnings: string[];
}

export interface ExploreRequest {
  seed_curl?: string | null;
  target_url?: string | null;
  target_urls?: string[] | null;
  mode?: ExploreMode;
  probe_openapi_docs?: boolean;
  crawl_js?: boolean;
  depth?: number;
  merge?: boolean;
  project_id?: string | null;
  credential_id?: string | null;
  auth?: ExploreAuthConfig | null;
}

export interface ExploreAllTargetsRequest {
  target_ids?: string[] | null;
  mode?: ExploreMode;
  probe_openapi_docs?: boolean;
  crawl_js?: boolean;
  depth?: number;
  credential_id?: string | null;
  auth?: ExploreAuthConfig | null;
}

export interface ExploredTargetResult {
  id: string;
  portal_label?: string;
  base_url: string;
  endpoints_found: number;
}

export interface ExploreSeedInfo {
  type: "curl" | "url" | string;
  base_url: string;
  root_domain: string;
  seed_url: string;
  api_prefix: string;
  auth_headers_detected: string[];
  sibling_api_hosts?: string[];
  auth_mode?: string;
  auth_profile_id?: string | null;
  auth_profile_name?: string | null;
}

export interface ExploreSummary {
  endpoints_count: number;
  openapi_docs_count: number;
  spa_routes_count: number;
  subdomains_count: number;
  live_hosts_count: number;
  scans_count?: number;
}

export interface DiscoveredEndpoint {
  id: string;
  method: string;
  path: string;
  url: string;
  source: string;
  sources?: string[];
  inferred_params: string[];
  snippet: string;
}

export interface OpenApiDocFound {
  url: string;
  title: string;
  endpoints_count: number;
}

export interface ExploreScanRecord {
  scan_id: string;
  scanned_at: string;
  mode: string;
  seed_type: string;
  seed_url?: string;
  new_endpoints: number;
  new_subdomains: number;
}

export interface ExploreResult {
  explore_id: string;
  project_id?: string | null;
  created_at: string;
  updated_at?: string;
  mode: string;
  seed: ExploreSeedInfo;
  summary: ExploreSummary;
  scans_count?: number;
  scans?: ExploreScanRecord[];
  merged_hosts?: string[];
  seed_hosts?: string[];
  explored_targets?: ExploredTargetResult[];
  discovered_portals?: DiscoveredPortal[];
  discovered_endpoints: DiscoveredEndpoint[];
  openapi_docs_found: OpenApiDocFound[];
  spa_routes: string[];
  micro_frontends: Array<Record<string, unknown> | string>;
  subdomains: string[];
  live_hosts: Array<Record<string, unknown> | string>;
  warnings: string[];
  saved_auth_profile?: AuthProfile | null;
}

export interface ExploreListItem {
  explore_id: string;
  project_id?: string | null;
  created_at: string;
  updated_at?: string;
  mode: string;
  seed?: Partial<ExploreSeedInfo>;
  summary?: Partial<ExploreSummary>;
  base_url?: string;
  root_domain?: string;
  merged_hosts?: string[];
  seed_hosts?: string[];
  endpoints_count?: number;
  scans_count?: number;
  [key: string]: unknown;
}

export interface ExploreJobRequest extends ExploreRequest {
  explore_all_targets?: boolean;
  target_ids?: string[] | null;
}

export interface ExploreJob {
  job_id: string;
  project_id: string | null;
  status: "running" | "completed" | "failed";
  progress_pct: number;
  stage:
    | "init"
    | "probe_openapi"
    | "crawl_js"
    | "discover_portals"
    | "merge"
    | "finalize"
    | "completed"
    | "failed"
    | string;
  targets_total: number;
  targets_done: number;
  current_target: string;
  endpoints_found: number;
  explore_id: string | null;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  result?: ExploreResult | null;
}

export interface PromoteToRunRequest {
  project_id?: string | null;
  project_name?: string | null;
  modules?: string[];
  selected_endpoints?: string[] | null;
}

export function runExplore(body: ExploreRequest): Promise<ExploreResult> {
  return apiFetch<ExploreResult>("/explore", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function exploreAllProjectTargets(
  projectId: string,
  body?: ExploreAllTargetsRequest
): Promise<ExploreResult> {
  return apiFetch<ExploreResult>(
    `/projects/${encodeURIComponent(projectId)}/explore-all`,
    {
      method: "POST",
      body: JSON.stringify(body ?? {}),
    }
  );
}

export async function listExplores(projectId?: string | null): Promise<ExploreListItem[]> {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  const raw = await apiFetch<ExploreListItem[] | { items?: ExploreListItem[] }>(`/explore${qs}`);
  if (Array.isArray(raw)) {
    return raw;
  }
  if (raw && Array.isArray(raw.items)) {
    return raw.items;
  }
  return [];
}

export function getExplore(exploreId: string): Promise<ExploreResult> {
  return apiFetch<ExploreResult>(`/explore/${encodeURIComponent(exploreId)}`);
}

export function deleteExplore(
  exploreId: string
): Promise<{ deleted: boolean; explore_id: string }> {
  return apiFetch<{ deleted: boolean; explore_id: string }>(
    `/explore/${encodeURIComponent(exploreId)}`,
    { method: "DELETE" }
  );
}

export function promoteExploreToRun(
  exploreId: string,
  body: PromoteToRunRequest
): Promise<Run> {
  return apiFetch<Run>(`/explore/${encodeURIComponent(exploreId)}/promote-to-run`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function startExploreJob(payload: ExploreJobRequest): Promise<ExploreJob> {
  return apiFetch<ExploreJob>("/explore/jobs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function listExploreJobs(
  projectId?: string | null,
  activeOnly?: boolean
): Promise<ExploreJob[]> {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  if (activeOnly) params.set("active_only", "true");
  const qs = params.toString();
  const raw = await apiFetch<ExploreJob[] | { items?: ExploreJob[] }>(
    qs ? `/explore/jobs?${qs}` : "/explore/jobs"
  );
  if (Array.isArray(raw)) return raw;
  if (raw && Array.isArray(raw.items)) return raw.items;
  return [];
}

export function getExploreJob(jobId: string): Promise<ExploreJob> {
  return apiFetch<ExploreJob>(`/explore/jobs/${encodeURIComponent(jobId)}`);
}

export function verifyExploreAuth(
  payload: ExploreVerifyAuthRequest
): Promise<ExploreVerifyAuthResponse> {
  return apiFetch<ExploreVerifyAuthResponse>("/explore/verify-auth", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
