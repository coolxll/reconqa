import type {
  GateRuleResult,
  QualityGateRequest,
} from "@/api/types";

export interface QualityGateFormInput {
  runId?: string | null;
  baselineRunId?: string | null;
  maxP0ChainFailures?: string | number | null;
  maxNewHighSecurity?: string | number | null;
  maxCoverageDropPct?: string | number | null;
  maxP95Ms?: string | number | null;
  failOnHighWithoutBaseline?: boolean | null;
}

/**
 * Parse a safe filename from an HTTP Content-Disposition header.
 * Supports RFC 5987 `filename*=UTF-8''...`, quoted `filename="..."`, and unquoted `filename=...`.
 * Rejects path traversal segments (`..`, `/`, `\`).
 */
export function parseContentDispositionFilename(header: string | null): string | null {
  if (!header || !header.trim()) return null;

  // Prefer RFC 5987 filename*
  const extMatch = header.match(/filename\*\s*=\s*(?:UTF-8''|utf-8'')?("?)([^";\r\n]+)\1/i);
  let candidate: string | null = null;
  if (extMatch && extMatch[2]) {
    try {
      candidate = decodeURIComponent(extMatch[2].trim());
    } catch {
      candidate = extMatch[2].trim();
    }
  } else {
    const quotedMatch = header.match(/filename\s*=\s*"([^"\r\n]+)"/i);
    if (quotedMatch && quotedMatch[1]) {
      candidate = quotedMatch[1].trim();
    } else {
      const bareMatch = header.match(/filename\s*=\s*([^;\r\n]+)/i);
      if (bareMatch && bareMatch[1]) {
        candidate = bareMatch[1].trim().replace(/^['"]|['"]$/g, "");
      }
    }
  }

  if (!candidate) return null;
  const cleaned = candidate.trim();
  if (
    !cleaned ||
    cleaned === "." ||
    cleaned === ".." ||
    cleaned.includes("/") ||
    cleaned.includes("\\") ||
    cleaned.includes("..")
  ) {
    return null;
  }
  return cleaned;
}

/**
 * Trigger a browser file download from an in-memory Blob.
 * No-ops safely when called outside a DOM environment.
 */
export function triggerBlobDownload(blob: Blob, filename: string): void {
  if (
    typeof document === "undefined" ||
    typeof URL === "undefined" ||
    typeof URL.createObjectURL !== "function"
  ) {
    return;
  }
  const objectUrl = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

function parseOptionalNonNegativeNumber(
  raw: string | number | null | undefined,
  integerOnly: boolean
): number | undefined {
  if (raw === null || raw === undefined) return undefined;
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (!trimmed) return undefined;
    const num = Number(trimmed);
    if (!Number.isFinite(num) || num < 0) return undefined;
    return integerOnly ? Math.floor(num) : num;
  }
  if (typeof raw === "number") {
    if (!Number.isFinite(raw) || raw < 0) return undefined;
    return integerOnly ? Math.floor(raw) : raw;
  }
  return undefined;
}

/**
 * Build a sanitized QualityGateRequest payload from optional UI form inputs.
 * Preserves explicit `0` thresholds, omits blank/invalid fields, and never
 * attaches webhook URLs or webhook delivery keys.
 */
export function buildQualityGateRequest(form: QualityGateFormInput): QualityGateRequest {
  const payload: QualityGateRequest = {};

  const runId = form.runId?.trim();
  if (runId) {
    payload.run_id = runId;
  }

  const baselineRunId = form.baselineRunId?.trim();
  if (baselineRunId) {
    payload.baseline_run_id = baselineRunId;
  }

  const maxP0 = parseOptionalNonNegativeNumber(form.maxP0ChainFailures, true);
  if (maxP0 !== undefined) {
    payload.max_p0_chain_failures = maxP0;
  }

  const maxHighSec = parseOptionalNonNegativeNumber(form.maxNewHighSecurity, true);
  if (maxHighSec !== undefined) {
    payload.max_new_high_security = maxHighSec;
  }

  const maxCovDrop = parseOptionalNonNegativeNumber(form.maxCoverageDropPct, false);
  if (maxCovDrop !== undefined) {
    payload.max_coverage_drop_pct = maxCovDrop;
  }

  const maxP95 = parseOptionalNonNegativeNumber(form.maxP95Ms, false);
  if (maxP95 !== undefined) {
    payload.max_p95_ms = maxP95;
  }

  if (typeof form.failOnHighWithoutBaseline === "boolean") {
    payload.fail_on_high_without_baseline = form.failOnHighWithoutBaseline;
  }

  return payload;
}

export function getGateRuleState(
  rule: Pick<GateRuleResult, "passed" | "skipped">
): "passed" | "failed" | "skipped" {
  if (rule.skipped) return "skipped";
  return rule.passed ? "passed" : "failed";
}

export function formatGateMetricValue(
  rule: Pick<GateRuleResult, "rule">,
  value: number | null | undefined
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  if (rule.rule === "coverage_drop_pct") {
    return `${Number(value.toFixed(2))}%`;
  }
  if (rule.rule === "p95_latency_ms") {
    return `${Math.round(value)} ms`;
  }
  return String(value);
}

export function hasSyncFeedback(
  payload?: { synced_scenarios?: number | null; synced_assertions?: number | null } | null
): boolean {
  if (!payload) return false;
  return (payload.synced_scenarios ?? 0) > 0 || (payload.synced_assertions ?? 0) > 0;
}
