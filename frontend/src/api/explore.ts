import { apiFetch } from "./client";
import type { Run } from "./types";

export type ExploreMode = "passive" | "active";

export interface ExploreRequest {
  seed_curl?: string | null;
  target_url?: string | null;
  mode?: ExploreMode;
  probe_openapi_docs?: boolean;
  crawl_js?: boolean;
  depth?: number;
  merge?: boolean;
}

export interface ExploreSeedInfo {
  type: "curl" | "url" | string;
  base_url: string;
  root_domain: string;
  seed_url: string;
  api_prefix: string;
  auth_headers_detected: string[];
  sibling_api_hosts?: string[];
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
  created_at: string;
  updated_at?: string;
  mode: string;
  seed: ExploreSeedInfo;
  summary: ExploreSummary;
  scans_count?: number;
  scans?: ExploreScanRecord[];
  discovered_endpoints: DiscoveredEndpoint[];
  openapi_docs_found: OpenApiDocFound[];
  spa_routes: string[];
  micro_frontends: Array<Record<string, unknown> | string>;
  subdomains: string[];
  live_hosts: Array<Record<string, unknown> | string>;
  warnings: string[];
}

export interface ExploreListItem {
  explore_id: string;
  created_at: string;
  updated_at?: string;
  mode: string;
  seed?: Partial<ExploreSeedInfo>;
  summary?: Partial<ExploreSummary>;
  base_url?: string;
  root_domain?: string;
  endpoints_count?: number;
  scans_count?: number;
  [key: string]: unknown;
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

export async function listExplores(): Promise<ExploreListItem[]> {
  const raw = await apiFetch<ExploreListItem[] | { items?: ExploreListItem[] }>("/explore");
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
