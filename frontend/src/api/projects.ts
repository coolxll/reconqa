import { API_BASE, apiFetch, apiUpload } from "./client";
import type {
  AcceptancePromptAgent,
  AcceptancePromptResponse,
  AuthProfile,
  AuthProfileCreate,
  ExecuteExternalToolRequest,
  ExecuteExternalToolResponse,
  ExtractCredentialRequest,
  ExtractCredentialResponse,
  KnowledgeDoc,
  KnowledgeDocCreate,
  KnowledgeDocType,
  KnowledgeDocUpdate,
  Project,
  ProjectCreate,
  ProjectInventoryResponse,
  ProjectUpdate,
  ReplayMatrixRequest,
  ReplayMatrixResponse,
  ReplayRequest,
  ReplayResponse,
  Target,
  TargetCreate,
  TargetUpdate,
} from "./types";

export function listProjects(): Promise<Project[]> {
  return apiFetch<Project[]>("/projects");
}

export function getProject(projectId: string): Promise<Project> {
  return apiFetch<Project>(`/projects/${encodeURIComponent(projectId)}`);
}

export function createProject(payload: ProjectCreate): Promise<Project> {
  return apiFetch<Project>("/projects", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateProject(projectId: string, payload: ProjectUpdate): Promise<Project> {
  return apiFetch<Project>(`/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteProject(projectId: string): Promise<{ deleted: boolean; id: string }> {
  return apiFetch<{ deleted: boolean; id: string }>(
    `/projects/${encodeURIComponent(projectId)}`,
    { method: "DELETE" }
  );
}

export function listTargets(projectId: string): Promise<Target[]> {
  return apiFetch<Target[]>(`/projects/${encodeURIComponent(projectId)}/targets`);
}

export function createTarget(projectId: string, payload: TargetCreate): Promise<Target> {
  return apiFetch<Target>(`/projects/${encodeURIComponent(projectId)}/targets`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateTarget(
  projectId: string,
  targetId: string,
  payload: TargetUpdate
): Promise<Target> {
  return apiFetch<Target>(
    `/projects/${encodeURIComponent(projectId)}/targets/${encodeURIComponent(targetId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    }
  );
}

export function deleteTarget(
  projectId: string,
  targetId: string
): Promise<{ deleted: boolean; id: string }> {
  return apiFetch<{ deleted: boolean; id: string }>(
    `/projects/${encodeURIComponent(projectId)}/targets/${encodeURIComponent(targetId)}`,
    { method: "DELETE" }
  );
}

export function listAuthProfiles(projectId: string): Promise<AuthProfile[]> {
  return apiFetch<AuthProfile[]>(`/projects/${encodeURIComponent(projectId)}/auth-profiles`);
}

export function createAuthProfile(
  projectId: string,
  payload: AuthProfileCreate
): Promise<AuthProfile> {
  return apiFetch<AuthProfile>(`/projects/${encodeURIComponent(projectId)}/auth-profiles`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteAuthProfile(
  projectId: string,
  profileId: string
): Promise<{ deleted: boolean; id: string }> {
  return apiFetch<{ deleted: boolean; id: string }>(
    `/projects/${encodeURIComponent(projectId)}/auth-profiles/${encodeURIComponent(profileId)}`,
    { method: "DELETE" }
  );
}

export function uploadStorageState(
  projectId: string,
  profileId: string,
  file: File
): Promise<AuthProfile> {
  const formData = new FormData();
  formData.append("file", file);
  return apiUpload<AuthProfile>(
    `/projects/${encodeURIComponent(projectId)}/auth-profiles/${encodeURIComponent(profileId)}/storage-state`,
    formData
  );
}

// --- Project Knowledge Base ---

export function listKnowledgeDocs(
  projectId: string,
  params?: { doc_type?: string; q?: string }
): Promise<KnowledgeDoc[]> {
  const search = new URLSearchParams();
  if (params?.doc_type && params.doc_type !== "all") {
    search.set("doc_type", params.doc_type);
  }
  if (params?.q && params.q.trim()) {
    search.set("q", params.q.trim());
  }
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return apiFetch<KnowledgeDoc[]>(
    `/projects/${encodeURIComponent(projectId)}/knowledge${suffix}`
  );
}

export function createKnowledgeDoc(
  projectId: string,
  payload: KnowledgeDocCreate
): Promise<KnowledgeDoc> {
  return apiFetch<KnowledgeDoc>(`/projects/${encodeURIComponent(projectId)}/knowledge`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function uploadKnowledgeDoc(
  projectId: string,
  file: File,
  options?: {
    title?: string;
    doc_type?: KnowledgeDocType;
    tags?: string[];
    related_endpoints?: string[];
  }
): Promise<KnowledgeDoc> {
  const formData = new FormData();
  formData.append("file", file);
  if (options?.title && options.title.trim()) {
    formData.append("title", options.title.trim());
  }
  formData.append("doc_type", options?.doc_type || "prd");
  if (options?.tags && options.tags.length > 0) {
    formData.append("tags", options.tags.join(","));
  }
  if (options?.related_endpoints && options.related_endpoints.length > 0) {
    formData.append("related_endpoints", options.related_endpoints.join(","));
  }
  return apiUpload<KnowledgeDoc>(
    `/projects/${encodeURIComponent(projectId)}/knowledge/upload`,
    formData
  );
}

export function updateKnowledgeDoc(
  projectId: string,
  docId: string,
  payload: KnowledgeDocUpdate
): Promise<KnowledgeDoc> {
  return apiFetch<KnowledgeDoc>(
    `/projects/${encodeURIComponent(projectId)}/knowledge/${encodeURIComponent(docId)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    }
  );
}

export function deleteKnowledgeDoc(
  projectId: string,
  docId: string
): Promise<{ deleted: boolean; id: string }> {
  return apiFetch<{ deleted: boolean; id: string }>(
    `/projects/${encodeURIComponent(projectId)}/knowledge/${encodeURIComponent(docId)}`,
    { method: "DELETE" }
  );
}

export function knowledgeDownloadUrl(projectId: string, docId: string): string {
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}/knowledge/${encodeURIComponent(docId)}/download`;
}

// --- Project Master API Inventory & Inline Replay ---

export function getProjectInventory(projectId: string): Promise<ProjectInventoryResponse> {
  return apiFetch<ProjectInventoryResponse>(
    `/projects/${encodeURIComponent(projectId)}/inventory`
  );
}

export function getProjectInventoryOpenApiUrl(projectId: string): string {
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}/inventory/openapi.json`;
}

export function replayEndpoint(
  projectId: string,
  payload: ReplayRequest
): Promise<ReplayResponse> {
  return apiFetch<ReplayResponse>(`/projects/${encodeURIComponent(projectId)}/replay`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function replayEndpointMatrix(
  projectId: string,
  payload: ReplayMatrixRequest
): Promise<ReplayMatrixResponse> {
  return apiFetch<ReplayMatrixResponse>(
    `/projects/${encodeURIComponent(projectId)}/replay-matrix`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    }
  );
}

// --- Autonomous External-System Acceptance Glue APIs ---

export function extractCredentialFromHar(
  projectId: string,
  payload?: ExtractCredentialRequest
): Promise<ExtractCredentialResponse> {
  return apiFetch<ExtractCredentialResponse>(
    `/projects/${encodeURIComponent(projectId)}/auth-profiles/extract-from-har`,
    {
      method: "POST",
      body: JSON.stringify(payload ?? {}),
    }
  );
}

export function executeRunExternalTool(
  projectId: string,
  runId: string,
  payload: ExecuteExternalToolRequest
): Promise<ExecuteExternalToolResponse> {
  return apiFetch<ExecuteExternalToolResponse>(
    `/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/execute-tool`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    }
  );
}

export function getAcceptancePrompt(
  projectId: string,
  options?: { lang?: "zh" | "en"; agent?: AcceptancePromptAgent }
): Promise<AcceptancePromptResponse> {
  const params = new URLSearchParams();
  if (options?.lang) params.set("lang", options.lang);
  if (options?.agent) params.set("agent", options.agent);
  const qs = params.toString();
  const base = `/projects/${encodeURIComponent(projectId)}/acceptance-prompt`;
  return apiFetch<AcceptancePromptResponse>(qs ? `${base}?${qs}` : base);
}
