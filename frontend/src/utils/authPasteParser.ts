import type { ExploreAuthType } from "@/api/explore";

export interface ParsedAuthPasteResult {
  ok: boolean;
  sourceFormat: "curl" | "raw_http" | "headers";
  mode: Exclude<ExploreAuthType, "none" | "profile">;
  detectedUrl?: string;
  token?: string;
  tokenHeader?: string;
  tokenPrefix?: string;
  cookie?: string;
  headers?: Record<string, string>;
  username?: string;
  password?: string;
  loginUrl?: string;
  loginMethod?: string;
  loginContentType?: "json" | "form";
  usernameField?: string;
  passwordField?: string;
  extraFields?: Record<string, unknown>;
  headerNames: string[];
  errorKey?: string;
}

const NOISY_HEADERS = new Set([
  "host",
  "content-length",
  "content-type",
  "accept",
  "accept-encoding",
  "accept-language",
  "user-agent",
  "connection",
  "keep-alive",
  "origin",
  "referer",
  "pragma",
  "cache-control",
  "priority",
  "upgrade-insecure-requests",
  "dnt",
  "te",
  "if-none-match",
  "if-modified-since",
  "x-requested-with",
]);

const AUTH_HEADER_PRIORITY = /^(authorization|cookie|x-auth-token|x-token|token|access[-_]?token|x-access-token|x-api-key|api[-_]?key|x-csrf-token|x-xsrf-token|csrf-token|x-session-id|session-id|x-tenant-id|x-app-id|x-user-token|satoken|x-satoken)$/i;

const USERNAME_FIELD_RE = /^(user(name)?|account|login(_?name|_?id)?|email|mobile|phone|tel|identifier|uid|admin(_?name)?|staff(_?no)?)$/i;
const PASSWORD_FIELD_RE = /^(pass(word|wd)?|pwd|secret|password_?hash|login_?pwd)$/i;

function isNoisyHeader(name: string): boolean {
  const lower = name.trim().toLowerCase();
  if (!lower) return true;
  if (NOISY_HEADERS.has(lower)) return true;
  if (lower.startsWith("sec-ch-") || lower.startsWith("sec-fetch-")) return true;
  return false;
}

function setHeaderCaseInsensitive(
  headers: Record<string, string>,
  rawName: string,
  rawValue: string
): void {
  const name = rawName.trim();
  const value = rawValue.trim();
  if (!name || !value) return;

  const lower = name.toLowerCase();
  if (lower === "cookie") {
    let existing = headers["Cookie"] ?? "";
    for (const key of Object.keys(headers)) {
      if (key.toLowerCase() === "cookie" && key !== "Cookie") {
        existing = existing ? `${existing}; ${headers[key]}` : headers[key];
        delete headers[key];
      }
    }
    headers["Cookie"] = existing ? `${existing}; ${value}` : value;
    return;
  }

  const existingKey = Object.keys(headers).find((k) => k.toLowerCase() === lower);
  if (existingKey && existingKey !== name) {
    delete headers[existingKey];
  }
  headers[existingKey ?? name] = value;
}

function unquoteShellArg(token: string): string {
  const trimmed = token.trim();
  if (trimmed.startsWith("$'") && trimmed.endsWith("'") && trimmed.length >= 3) {
    return trimmed.slice(2, -1).replace(/\\([nrt'"\\])/g, (_, ch: string) => {
      switch (ch) {
        case "n":
          return "\n";
        case "r":
          return "\r";
        case "t":
          return "\t";
        case "'":
          return "'";
        case '"':
          return '"';
        case "\\":
          return "\\";
        default:
          return ch;
      }
    });
  }
  if (trimmed.startsWith("'") && trimmed.endsWith("'") && trimmed.length >= 2) {
    return trimmed.slice(1, -1);
  }
  if (trimmed.startsWith('"') && trimmed.endsWith('"') && trimmed.length >= 2) {
    return trimmed.slice(1, -1).replace(/\\(["\\])/g, "$1");
  }
  return trimmed;
}

function tokenizeCurl(raw: string): string[] {
  // Join bash/cmd line continuations
  const normalized = raw
    .replace(/\\\r?\n/g, " ")
    .replace(/\^\r?\n/g, " ")
    .trim();

  const tokens: string[] = [];
  let cur = "";
  let quote: "'" | '"' | null = null;
  let dollarQuote = false;
  let i = 0;

  while (i < normalized.length) {
    const ch = normalized[i];
    if (quote) {
      if (ch === "\\" && (quote === '"' || dollarQuote) && i + 1 < normalized.length) {
        cur += ch + normalized[i + 1];
        i += 2;
        continue;
      }
      if (ch === quote) {
        cur += ch;
        quote = null;
        dollarQuote = false;
        i += 1;
        continue;
      }
      cur += ch;
      i += 1;
      continue;
    }

    if (ch === "$" && normalized[i + 1] === "'") {
      cur += "$'";
      quote = "'";
      dollarQuote = true;
      i += 2;
      continue;
    }

    if (ch === "'" || ch === '"') {
      cur += ch;
      quote = ch;
      i += 1;
      continue;
    }

    if (/\s/.test(ch)) {
      if (cur) {
        tokens.push(cur);
        cur = "";
      }
      i += 1;
      continue;
    }

    cur += ch;
    i += 1;
  }

  if (cur) tokens.push(cur);
  return tokens;
}

function tryParseBodyCredentials(
  body: string,
  contentTypeHint: string
): {
  contentType: "json" | "form";
  usernameField: string;
  passwordField: string;
  username: string;
  password: string;
  extraFields: Record<string, unknown>;
} | null {
  const trimmed = body.trim();
  if (!trimmed) return null;

  let obj: Record<string, unknown> | null = null;
  let contentType: "json" | "form" = "json";

  if (trimmed.startsWith("{") || /json/i.test(contentTypeHint)) {
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        obj = parsed as Record<string, unknown>;
        contentType = "json";
      }
    } catch {
      // Fall through to form parser
    }
  }

  if (!obj && (trimmed.includes("=") || /form-urlencoded/i.test(contentTypeHint))) {
    try {
      const params = new URLSearchParams(trimmed);
      const record: Record<string, unknown> = {};
      let count = 0;
      params.forEach((val, key) => {
        if (key) {
          record[key] = val;
          count += 1;
        }
      });
      if (count > 0) {
        obj = record;
        contentType = "form";
      }
    } catch {
      return null;
    }
  }

  if (!obj) return null;

  let usernameField = "";
  let passwordField = "";
  for (const key of Object.keys(obj)) {
    if (!usernameField && USERNAME_FIELD_RE.test(key)) {
      usernameField = key;
    }
    if (!passwordField && PASSWORD_FIELD_RE.test(key)) {
      passwordField = key;
    }
  }

  if (!usernameField || !passwordField) return null;

  const username = String(obj[usernameField] ?? "");
  const password = String(obj[passwordField] ?? "");
  const extraFields: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) {
    if (k !== usernameField && k !== passwordField) {
      extraFields[k] = v;
    }
  }

  return {
    contentType,
    usernameField,
    passwordField,
    username,
    password,
    extraFields,
  };
}

function decodeBasicAuth(encoded: string): { username: string; password: string } | null {
  try {
    const decoded = atob(encoded.trim());
    const idx = decoded.indexOf(":");
    if (idx < 0) return null;
    return {
      username: decoded.slice(0, idx),
      password: decoded.slice(idx + 1),
    };
  } catch {
    return null;
  }
}

function classifyExtractedData(params: {
  sourceFormat: "curl" | "raw_http" | "headers";
  method: string;
  url?: string;
  allHeaders: Record<string, string>;
  body: string;
  basicUserPass?: string;
}): ParsedAuthPasteResult {
  const { sourceFormat, method, url, allHeaders, body, basicUserPass } = params;

  const contentTypeHeader =
    Object.entries(allHeaders).find(([k]) => k.toLowerCase() === "content-type")?.[1] ?? "";

  // 1. Check if this looks like a login API request (POST/PUT with username & password in body)
  const bodyCreds = tryParseBodyCredentials(body, contentTypeHeader);
  if (bodyCreds && url && /^(POST|PUT|PATCH)$/i.test(method || "POST")) {
    return {
      ok: true,
      sourceFormat,
      mode: "login_api",
      detectedUrl: url,
      loginUrl: url,
      loginMethod: (method || "POST").toUpperCase(),
      loginContentType: bodyCreds.contentType,
      usernameField: bodyCreds.usernameField,
      passwordField: bodyCreds.passwordField,
      username: bodyCreds.username,
      password: bodyCreds.password,
      extraFields: Object.keys(bodyCreds.extraFields).length > 0 ? bodyCreds.extraFields : undefined,
      headerNames: [],
    };
  }

  // 2. Check explicit Basic auth (-u user:pass)
  if (basicUserPass) {
    const idx = basicUserPass.indexOf(":");
    const username = idx >= 0 ? basicUserPass.slice(0, idx) : basicUserPass;
    const password = idx >= 0 ? basicUserPass.slice(idx + 1) : "";
    return {
      ok: true,
      sourceFormat,
      mode: "basic",
      detectedUrl: url,
      username,
      password,
      headerNames: ["Authorization"],
    };
  }

  // 3. Filter noisy headers & prioritize auth headers
  const authHeaders: Record<string, string> = {};
  const fallbackHeaders: Record<string, string> = {};

  for (const [rawKey, rawVal] of Object.entries(allHeaders)) {
    const key = rawKey.trim();
    const val = rawVal.trim();
    if (!key || !val || isNoisyHeader(key)) continue;

    if (AUTH_HEADER_PRIORITY.test(key) || /^x-/i.test(key)) {
      authHeaders[key] = val;
    } else {
      fallbackHeaders[key] = val;
    }
  }

  const selectedHeaders =
    Object.keys(authHeaders).length > 0 ? authHeaders : fallbackHeaders;
  const headerKeys = Object.keys(selectedHeaders);

  if (headerKeys.length === 0) {
    return {
      ok: false,
      sourceFormat,
      mode: "headers",
      detectedUrl: url,
      headerNames: [],
      errorKey: "noAuthFound",
    };
  }

  // Single header special cases: Bearer, Basic, Cookie
  if (headerKeys.length === 1) {
    const onlyKey = headerKeys[0];
    const onlyVal = selectedHeaders[onlyKey];
    const lowerKey = onlyKey.toLowerCase();

    if (lowerKey === "authorization") {
      const basicMatch = onlyVal.match(/^Basic\s+([A-Za-z0-9+/=]+)$/i);
      if (basicMatch) {
        const decoded = decodeBasicAuth(basicMatch[1]);
        if (decoded) {
          return {
            ok: true,
            sourceFormat,
            mode: "basic",
            detectedUrl: url,
            username: decoded.username,
            password: decoded.password,
            headerNames: [onlyKey],
          };
        }
      }

      const prefixMatch = onlyVal.match(/^(Bearer|Token|JWT)\s+(.+)$/i);
      if (prefixMatch) {
        return {
          ok: true,
          sourceFormat,
          mode: "bearer",
          detectedUrl: url,
          tokenHeader: onlyKey,
          tokenPrefix: `${prefixMatch[1]} `,
          token: prefixMatch[2].trim(),
          headerNames: [onlyKey],
        };
      }

      return {
        ok: true,
        sourceFormat,
        mode: "bearer",
        detectedUrl: url,
        tokenHeader: onlyKey,
        tokenPrefix: "",
        token: onlyVal,
        headerNames: [onlyKey],
      };
    }

    if (lowerKey === "cookie") {
      return {
        ok: true,
        sourceFormat,
        mode: "cookie",
        detectedUrl: url,
        cookie: onlyVal,
        headerNames: ["Cookie"],
      };
    }

    if (/token|api[-_]?key/i.test(lowerKey)) {
      const prefixMatch = onlyVal.match(/^(Bearer|Token|JWT)\s+(.+)$/i);
      return {
        ok: true,
        sourceFormat,
        mode: "bearer",
        detectedUrl: url,
        tokenHeader: onlyKey,
        tokenPrefix: prefixMatch ? `${prefixMatch[1]} ` : "",
        token: prefixMatch ? prefixMatch[2].trim() : onlyVal,
        headerNames: [onlyKey],
      };
    }
  }

  return {
    ok: true,
    sourceFormat,
    mode: "headers",
    detectedUrl: url,
    headers: selectedHeaders,
    headerNames: headerKeys,
  };
}

function parseCurlInput(raw: string): ParsedAuthPasteResult {
  const tokens = tokenizeCurl(raw);
  let method = "";
  let url = "";
  let basicUserPass = "";
  const headers: Record<string, string> = {};
  const dataParts: string[] = [];

  for (let i = 0; i < tokens.length; i += 1) {
    const tok = tokens[i];
    if (i === 0 && /^curl(\.exe)?$/i.test(tok)) continue;

    if (tok === "-X" || tok === "--request") {
      if (i + 1 < tokens.length) {
        method = unquoteShellArg(tokens[++i]).toUpperCase();
      }
      continue;
    }
    if (tok === "-H" || tok === "--header") {
      if (i + 1 < tokens.length) {
        const hdr = unquoteShellArg(tokens[++i]);
        const colon = hdr.indexOf(":");
        if (colon > 0) {
          setHeaderCaseInsensitive(headers, hdr.slice(0, colon), hdr.slice(colon + 1));
        }
      }
      continue;
    }
    if (tok === "-b" || tok === "--cookie") {
      if (i + 1 < tokens.length) {
        const cookieVal = unquoteShellArg(tokens[++i]);
        if (cookieVal) {
          setHeaderCaseInsensitive(headers, "Cookie", cookieVal);
        }
      }
      continue;
    }
    if (tok === "-u" || tok === "--user") {
      if (i + 1 < tokens.length) {
        basicUserPass = unquoteShellArg(tokens[++i]);
      }
      continue;
    }
    if (
      tok === "-d" ||
      tok === "--data" ||
      tok === "--data-raw" ||
      tok === "--data-binary" ||
      tok === "--data-ascii" ||
      tok === "--data-urlencode"
    ) {
      if (i + 1 < tokens.length) {
        dataParts.push(unquoteShellArg(tokens[++i]));
      }
      continue;
    }
    if (tok === "--url") {
      if (i + 1 < tokens.length) {
        url = unquoteShellArg(tokens[++i]);
      }
      continue;
    }
    if (!tok.startsWith("-")) {
      const unquoted = unquoteShellArg(tok);
      if (/^https?:\/\//i.test(unquoted) && !url) {
        url = unquoted;
      }
    }
  }

  const body = dataParts.join("&");
  const resolvedMethod = method || (body ? "POST" : "GET");

  return classifyExtractedData({
    sourceFormat: "curl",
    method: resolvedMethod,
    url: url || undefined,
    allHeaders: headers,
    body,
    basicUserPass: basicUserPass || undefined,
  });
}

function parseRawHttpInput(raw: string): ParsedAuthPasteResult {
  const normalized = raw.replace(/\r\n/g, "\n");
  const splitIdx = normalized.indexOf("\n\n");
  const headSection = splitIdx >= 0 ? normalized.slice(0, splitIdx) : normalized;
  const bodySection = splitIdx >= 0 ? normalized.slice(splitIdx + 2).trim() : "";

  const lines = headSection.split("\n").map((l) => l.trimEnd());
  const firstLine = (lines[0] ?? "").trim();
  const reqMatch = firstLine.match(
    /^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)(?:\s+HTTP\/[\d.]+)?$/i
  );

  const method = reqMatch ? reqMatch[1].toUpperCase() : "GET";
  const pathOrUrl = reqMatch ? reqMatch[2] : "";
  const headers: Record<string, string> = {};
  let hostHeader = "";

  for (let i = reqMatch ? 1 : 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (!line) continue;
    const colon = line.indexOf(":");
    if (colon <= 0) continue;
    const k = line.slice(0, colon).trim();
    const v = line.slice(colon + 1).trim();
    if (!k) continue;
    if (k.toLowerCase() === "host") {
      hostHeader = v;
    }
    setHeaderCaseInsensitive(headers, k, v);
  }

  let url: string | undefined;
  if (/^https?:\/\//i.test(pathOrUrl)) {
    url = pathOrUrl;
  } else if (hostHeader && pathOrUrl) {
    const isLocal = /^(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$/i.test(hostHeader);
    const scheme = isLocal ? "http" : "https";
    const normPath = pathOrUrl.startsWith("/") ? pathOrUrl : `/${pathOrUrl}`;
    url = `${scheme}://${hostHeader}${normPath}`;
  }

  return classifyExtractedData({
    sourceFormat: "raw_http",
    method,
    url,
    allHeaders: headers,
    body: bodySection,
  });
}

function parseMultiLineHeadersInput(raw: string): ParsedAuthPasteResult {
  const lines = raw.replace(/\r\n/g, "\n").split("\n");
  const headers: Record<string, string> = {};

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const colon = trimmed.indexOf(":");
    if (colon <= 0) continue;
    setHeaderCaseInsensitive(headers, trimmed.slice(0, colon), trimmed.slice(colon + 1));
  }

  return classifyExtractedData({
    sourceFormat: "headers",
    method: "GET",
    allHeaders: headers,
    body: "",
  });
}

export function parseAuthPaste(rawInput: string): ParsedAuthPasteResult {
  const raw = rawInput.trim();
  if (!raw) {
    return {
      ok: false,
      sourceFormat: "headers",
      mode: "headers",
      headerNames: [],
      errorKey: "empty",
    };
  }

  if (/^curl(\.exe)?\s+/i.test(raw)) {
    return parseCurlInput(raw);
  }

  const firstLine = raw.split(/\r?\n/, 1)[0]?.trim() ?? "";
  if (/^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S+(\s+HTTP\/[\d.]+)?$/i.test(firstLine)) {
    return parseRawHttpInput(raw);
  }

  return parseMultiLineHeadersInput(raw);
}
