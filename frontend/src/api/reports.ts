import { API_BASE, apiBlob, apiFetch } from "./client";
import { triggerBlobDownload } from "@/utils/gateAndExport";
import type {
  AcceptanceReportData,
  AcceptanceReportFormat,
  DoctorInstallResult,
  DoctorToolStatus,
  Finding,
  JavaAllureReport,
  PerformanceReport,
  ProjectTrendsResponse,
  QualityGateRequest,
  QualityGateResult,
  SecurityReport,
} from "./types";

export function listRunFindings(
  runId: string,
  params?: { category?: string; severity?: string }
): Promise<Finding[]> {
  const search = new URLSearchParams();
  if (params?.category) {
    search.set("category", params.category);
  }
  if (params?.severity) {
    search.set("severity", params.severity);
  }
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return apiFetch<Finding[]>(`/runs/${encodeURIComponent(runId)}/findings${suffix}`);
}

export function getRunPerformance(runId: string): Promise<PerformanceReport> {
  return apiFetch<PerformanceReport>(`/runs/${encodeURIComponent(runId)}/performance`);
}

export function getRunSecurity(runId: string): Promise<SecurityReport> {
  return apiFetch<SecurityReport>(`/runs/${encodeURIComponent(runId)}/security`);
}

export function getRunJavaReport(runId: string): Promise<JavaAllureReport> {
  return apiFetch<JavaAllureReport>(`/runs/${encodeURIComponent(runId)}/java-report`);
}

export function getProjectTrends(projectId: string): Promise<ProjectTrendsResponse> {
  return apiFetch<ProjectTrendsResponse>(`/projects/${encodeURIComponent(projectId)}/trends`);
}

export async function getDoctorStatus(): Promise<Record<string, DoctorToolStatus>> {
  const raw = await apiFetch<
    Record<string, DoctorToolStatus> | { tools: Record<string, DoctorToolStatus> }
  >("/system/doctor");
  if (raw && typeof raw === "object" && "tools" in raw && raw.tools && typeof raw.tools === "object") {
    return raw.tools as Record<string, DoctorToolStatus>;
  }
  return raw as Record<string, DoctorToolStatus>;
}

export function installDoctorTool(tool: string): Promise<DoctorInstallResult> {
  return apiFetch<DoctorInstallResult>("/system/doctor/install", {
    method: "POST",
    body: JSON.stringify({ tool }),
  });
}

// --- Standalone Acceptance Report & Postman v2.1 Export (R4) ---

export function getAcceptanceReportUrl(
  projectId: string,
  options?: { format?: AcceptanceReportFormat; runId?: string | null }
): string {
  const params = new URLSearchParams();
  params.set("format", options?.format ?? "html");
  if (options?.runId && options.runId.trim()) {
    params.set("run_id", options.runId.trim());
  }
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}/acceptance-report?${params.toString()}`;
}

export function getPostmanExportUrl(
  projectId: string,
  runId?: string | null
): string {
  const params = new URLSearchParams();
  if (runId && runId.trim()) {
    params.set("run_id", runId.trim());
  }
  const qs = params.toString();
  const base = `${API_BASE}/projects/${encodeURIComponent(projectId)}/export/postman`;
  return qs ? `${base}?${qs}` : base;
}

export function getAcceptanceReport(
  projectId: string,
  runId?: string | null
): Promise<AcceptanceReportData> {
  const params = new URLSearchParams({ format: "json" });
  if (runId && runId.trim()) {
    params.set("run_id", runId.trim());
  }
  return apiFetch<AcceptanceReportData>(
    `/projects/${encodeURIComponent(projectId)}/acceptance-report?${params.toString()}`
  );
}

export function fetchAcceptanceReportBlob(
  projectId: string,
  options?: { format?: Exclude<AcceptanceReportFormat, "json">; runId?: string | null }
): Promise<{ blob: Blob; filename: string | null; contentType: string }> {
  const params = new URLSearchParams();
  params.set("format", options?.format ?? "html");
  if (options?.runId && options.runId.trim()) {
    params.set("run_id", options.runId.trim());
  }
  return apiBlob(
    `/projects/${encodeURIComponent(projectId)}/acceptance-report?${params.toString()}`
  );
}

export async function downloadAcceptanceReport(
  projectId: string,
  options?: { format?: Exclude<AcceptanceReportFormat, "json">; runId?: string | null }
): Promise<string> {
  const format = options?.format ?? "html";
  const { blob, filename } = await fetchAcceptanceReportBlob(projectId, {
    format,
    runId: options?.runId,
  });
  const ext = format === "markdown" ? "md" : "html";
  const resolvedName = filename || `${projectId}-acceptance-report.${ext}`;
  triggerBlobDownload(blob, resolvedName);
  return resolvedName;
}

export function fetchPostmanCollectionBlob(
  projectId: string,
  runId?: string | null
): Promise<{ blob: Blob; filename: string | null; contentType: string }> {
  const params = new URLSearchParams();
  if (runId && runId.trim()) {
    params.set("run_id", runId.trim());
  }
  const qs = params.toString();
  const base = `/projects/${encodeURIComponent(projectId)}/export/postman`;
  return apiBlob(qs ? `${base}?${qs}` : base);
}

export async function downloadPostmanCollection(
  projectId: string,
  runId?: string | null
): Promise<string> {
  const { blob, filename } = await fetchPostmanCollectionBlob(projectId, runId);
  const resolvedName = filename || `${projectId}-postman-collection.json`;
  triggerBlobDownload(blob, resolvedName);
  return resolvedName;
}

// --- Quality Gate API (R5) ---

export function evaluateProjectQualityGate(
  projectId: string,
  payload?: QualityGateRequest
): Promise<QualityGateResult> {
  return apiFetch<QualityGateResult>(
    `/projects/${encodeURIComponent(projectId)}/gate`,
    {
      method: "POST",
      body: JSON.stringify(payload ?? {}),
    }
  );
}
