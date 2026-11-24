import { apiFetch, apiText, apiUpload } from "./client";
import type {
  Artifact,
  BurpExportRequest,
  BurpExportResponse,
  CorrelationReport,
  CoverageDiffReport,
  ExecuteModulesRequest,
  HarReuseResponse,
  HarUploadResponse,
  ImportTextRequest,
  ImportTextResponse,
  OpenApiSpec,
  Run,
  RunCreate,
} from "./types";

export function listProjectRuns(projectId: string): Promise<Run[]> {
  return apiFetch<Run[]>(`/projects/${encodeURIComponent(projectId)}/runs`);
}

export function createRun(
  projectId: string,
  payload: RunCreate,
  autoStart = true
): Promise<Run> {
  const qs = autoStart ? "" : "?auto_start=false";
  return apiFetch<Run>(`/projects/${encodeURIComponent(projectId)}/runs${qs}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getRun(runId: string): Promise<Run> {
  return apiFetch<Run>(`/runs/${encodeURIComponent(runId)}`);
}

export function uploadRunHars(
  runId: string,
  files: File[],
  options?: { scenario?: string; targetDomain?: string; target_domain?: string }
): Promise<HarUploadResponse> {
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }
  if (options?.scenario) {
    formData.append("scenario", options.scenario);
  }
  const targetDomain = options?.targetDomain ?? options?.target_domain;
  if (targetDomain) {
    formData.append("target_domain", targetDomain);
  }
  return apiUpload<HarUploadResponse>(`/runs/${encodeURIComponent(runId)}/hars`, formData);
}

export function reuseRunHars(
  runId: string,
  sourceRunIdOrPayload:
    | string
    | { source_run_id: string; scenarios?: string[] | null },
  scenarios?: string[]
): Promise<HarReuseResponse> {
  const payload =
    typeof sourceRunIdOrPayload === "string"
      ? {
          source_run_id: sourceRunIdOrPayload,
          scenarios: scenarios && scenarios.length > 0 ? scenarios : null,
        }
      : {
          source_run_id: sourceRunIdOrPayload.source_run_id,
          scenarios:
            sourceRunIdOrPayload.scenarios && sourceRunIdOrPayload.scenarios.length > 0
              ? sourceRunIdOrPayload.scenarios
              : null,
        };
  return apiFetch<HarReuseResponse>(`/runs/${encodeURIComponent(runId)}/hars/reuse`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function importRunText(
  runId: string,
  body: ImportTextRequest
): Promise<ImportTextResponse> {
  return apiFetch<ImportTextResponse>(`/runs/${encodeURIComponent(runId)}/import-text`, {
    method: "POST",
    body: JSON.stringify({
      content: body.content,
      scenario: body.scenario || "01_devtools",
      format: body.format || "auto",
      target_domain: body.target_domain ?? null,
    }),
  });
}

export function startRun(runId: string): Promise<Run> {
  return apiFetch<Run>(`/runs/${encodeURIComponent(runId)}/start`, {
    method: "POST",
  });
}

export function executeRunModules(
  runId: string,
  body: ExecuteModulesRequest
): Promise<Run> {
  return apiFetch<Run>(`/runs/${encodeURIComponent(runId)}/execute-modules`, {
    method: "POST",
    body: JSON.stringify({
      modules: body.modules,
      config: body.config ?? {},
    }),
  });
}

export function getRunOpenApi(runId: string): Promise<OpenApiSpec> {
  return apiFetch<OpenApiSpec>(`/runs/${encodeURIComponent(runId)}/openapi?format=json`);
}

export function getRunOpenApiYaml(runId: string): Promise<string> {
  return apiText(`/runs/${encodeURIComponent(runId)}/openapi?format=yaml`);
}

export function getRunCorrelation(runId: string): Promise<CorrelationReport> {
  return apiFetch<CorrelationReport>(`/runs/${encodeURIComponent(runId)}/correlation`);
}

export function getRunCoverage(runId: string): Promise<CoverageDiffReport> {
  return apiFetch<CoverageDiffReport>(`/runs/${encodeURIComponent(runId)}/coverage`);
}

export function listRunArtifacts(runId: string): Promise<Artifact[]> {
  return apiFetch<Artifact[]>(`/runs/${encodeURIComponent(runId)}/artifacts`);
}

export function triggerBurpExport(
  runId: string,
  body?: BurpExportRequest
): Promise<BurpExportResponse> {
  return apiFetch<BurpExportResponse>(`/runs/${encodeURIComponent(runId)}/burp-export`, {
    method: "POST",
    body: JSON.stringify(body ?? {}),
  });
}
