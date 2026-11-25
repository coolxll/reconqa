import { apiFetch } from "./client";
import type {
  ChainExecuteRequest,
  ChainResumeRequest,
  ChainRunOut,
} from "./types";

export function listChainRuns(
  projectId: string,
  runId?: string | null
): Promise<ChainRunOut[]> {
  const params = new URLSearchParams();
  if (runId && runId.trim()) {
    params.set("run_id", runId.trim());
  }
  const qs = params.toString();
  const base = `/projects/${encodeURIComponent(projectId)}/chain-runs`;
  return apiFetch<ChainRunOut[]>(qs ? `${base}?${qs}` : base);
}

export function getChainRun(
  projectId: string,
  chainRunId: string
): Promise<ChainRunOut> {
  return apiFetch<ChainRunOut>(
    `/projects/${encodeURIComponent(projectId)}/chain-runs/${encodeURIComponent(chainRunId)}`
  );
}

export function executeChain(
  projectId: string,
  payload: ChainExecuteRequest
): Promise<ChainRunOut> {
  return apiFetch<ChainRunOut>(
    `/projects/${encodeURIComponent(projectId)}/chains/execute`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    }
  );
}

export function resumeChainRun(
  projectId: string,
  chainRunId: string,
  payload: ChainResumeRequest
): Promise<ChainRunOut> {
  return apiFetch<ChainRunOut>(
    `/projects/${encodeURIComponent(projectId)}/chain-runs/${encodeURIComponent(chainRunId)}/resume`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    }
  );
}
