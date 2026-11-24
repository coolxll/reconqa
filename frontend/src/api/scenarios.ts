import { apiFetch } from "./client";
import type {
  ScenarioCreate,
  ScenarioDetail,
  ScenarioItem,
  ScenarioListResponse,
  ScenarioUpdate,
} from "./types";

export interface ScenarioListFilters {
  priority?: string;
  status?: string;
  business_domain?: string;
  effectiveness_level?: string;
  q?: string;
}

export function listProjectScenarios(
  projectId: string,
  filters?: ScenarioListFilters
): Promise<ScenarioListResponse> {
  const params = new URLSearchParams();
  if (filters?.priority && filters.priority !== "ALL") params.set("priority", filters.priority);
  if (filters?.status && filters.status !== "ALL") params.set("status", filters.status);
  if (filters?.business_domain && filters.business_domain !== "ALL") {
    params.set("business_domain", filters.business_domain);
  }
  if (filters?.effectiveness_level && filters.effectiveness_level !== "ALL") {
    params.set("effectiveness_level", filters.effectiveness_level);
  }
  if (filters?.q && filters.q.trim()) params.set("q", filters.q.trim());

  const qs = params.toString();
  const base = `/projects/${encodeURIComponent(projectId)}/scenarios`;
  return apiFetch<ScenarioListResponse>(qs ? `${base}?${qs}` : base);
}

export function syncProjectScenarios(projectId: string): Promise<ScenarioListResponse> {
  return apiFetch<ScenarioListResponse>(
    `/projects/${encodeURIComponent(projectId)}/scenarios/sync`,
    { method: "POST" }
  );
}

export function getScenarioDetail(
  projectId: string,
  scenarioId: string
): Promise<ScenarioDetail> {
  return apiFetch<ScenarioDetail>(
    `/projects/${encodeURIComponent(projectId)}/scenarios/${encodeURIComponent(scenarioId)}`
  );
}

export function createScenario(
  projectId: string,
  payload: ScenarioCreate,
  upsert = false
): Promise<ScenarioItem> {
  const qs = upsert ? "?upsert=true" : "";
  return apiFetch<ScenarioItem>(
    `/projects/${encodeURIComponent(projectId)}/scenarios${qs}`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    }
  );
}

export function updateScenario(
  projectId: string,
  scenarioId: string,
  payload: ScenarioUpdate
): Promise<ScenarioItem> {
  return apiFetch<ScenarioItem>(
    `/projects/${encodeURIComponent(projectId)}/scenarios/${encodeURIComponent(scenarioId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    }
  );
}

export function deleteScenario(projectId: string, scenarioId: string): Promise<void> {
  return apiFetch<void>(
    `/projects/${encodeURIComponent(projectId)}/scenarios/${encodeURIComponent(scenarioId)}`,
    { method: "DELETE" }
  );
}
