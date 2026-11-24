import { apiFetch } from "./client";
import type {
  DoctorInstallResult,
  DoctorToolStatus,
  Finding,
  JavaAllureReport,
  PerformanceReport,
  ProjectTrendsResponse,
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
