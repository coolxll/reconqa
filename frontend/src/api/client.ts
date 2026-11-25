import { parseContentDispositionFilename } from "@/utils/gateAndExport";

export const API_BASE = "/api/v1";

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function buildUrl(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) {
    return path;
  }
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${normalized}`;
}

async function parseErrorDetail(res: Response): Promise<string> {
  const text = await res.text().catch(() => "");
  if (!text) {
    return `${res.status} ${res.statusText}`;
  }
  try {
    const parsed = JSON.parse(text) as { detail?: unknown; message?: string };
    if (typeof parsed.detail === "string") {
      return parsed.detail;
    }
    if (Array.isArray(parsed.detail)) {
      return parsed.detail
        .map((item: { msg?: string; loc?: unknown[] }) =>
          item.loc ? `${item.loc.join(".")}: ${item.msg ?? ""}` : (item.msg ?? JSON.stringify(item))
        )
        .join("; ");
    }
    if (typeof parsed.message === "string") {
      return parsed.message;
    }
  } catch {
    // Fall back to plain text body
  }
  return text.slice(0, 300);
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers ?? {});
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(buildUrl(path), {
    ...init,
    headers,
  });

  if (!res.ok) {
    throw new ApiError(res.status, await parseErrorDetail(res));
  }

  if (res.status === 204) {
    return undefined as T;
  }

  return (await res.json()) as T;
}

export async function apiText(path: string, init?: RequestInit): Promise<string> {
  const res = await fetch(buildUrl(path), init);
  if (!res.ok) {
    throw new ApiError(res.status, await parseErrorDetail(res));
  }
  return await res.text();
}

export async function apiBlob(
  path: string,
  init?: RequestInit
): Promise<{ blob: Blob; filename: string | null; contentType: string }> {
  const res = await fetch(buildUrl(path), init);
  if (!res.ok) {
    throw new ApiError(res.status, await parseErrorDetail(res));
  }
  const blob = await res.blob();
  const filename = parseContentDispositionFilename(res.headers.get("content-disposition"));
  const contentType = res.headers.get("content-type") || blob.type || "application/octet-stream";
  return { blob, filename, contentType };
}

export async function apiUpload<T>(path: string, formData: FormData): Promise<T> {
  const res = await fetch(buildUrl(path), {
    method: "POST",
    body: formData,
  });

  if (!res.ok) {
    throw new ApiError(res.status, await parseErrorDetail(res));
  }

  return (await res.json()) as T;
}

export function artifactDownloadUrl(runId: string, kind: string): string {
  return `${API_BASE}/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(kind)}`;
}

export function runEventsUrl(runId: string): string {
  return `${API_BASE}/runs/${encodeURIComponent(runId)}/events`;
}
