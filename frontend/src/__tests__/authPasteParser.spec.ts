import { describe, expect, it } from "vitest";
import { parseAuthPaste } from "@/utils/authPasteParser";

describe("parseAuthPaste", () => {
  it("rejects empty and whitespace-only inputs", () => {
    const empty = parseAuthPaste("");
    expect(empty.ok).toBe(false);
    expect(empty.errorKey).toBe("empty");

    const ws = parseAuthPaste("   \n\t  ");
    expect(ws.ok).toBe(false);
    expect(ws.errorKey).toBe("empty");
  });

  it("rejects input containing only noisy browser headers", () => {
    const raw = [
      "GET /api/v1/health HTTP/1.1",
      "Host: api.example.com",
      "User-Agent: Mozilla/5.0",
      "Accept: application/json",
      "Accept-Encoding: gzip, deflate",
      "Sec-Fetch-Mode: cors",
      "Sec-CH-UA: \"Chromium\";v=\"120\"",
      "Origin: https://app.example.com",
    ].join("\r\n");

    const res = parseAuthPaste(raw);
    expect(res.ok).toBe(false);
    expect(res.errorKey).toBe("noAuthFound");
    expect(res.detectedUrl).toBe("https://api.example.com/api/v1/health");
  });

  it("parses Burp Raw HTTP GET with Authorization Bearer token", () => {
    const raw = [
      "GET /api/v1/orders?page=1 HTTP/1.1",
      "Host: shop.example.com",
      "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
      "User-Agent: Mozilla/5.0",
      "Accept: application/json",
      "Sec-Fetch-Site: same-origin",
    ].join("\r\n");

    const res = parseAuthPaste(raw);
    expect(res.ok).toBe(true);
    expect(res.sourceFormat).toBe("raw_http");
    expect(res.mode).toBe("bearer");
    expect(res.detectedUrl).toBe("https://shop.example.com/api/v1/orders?page=1");
    expect(res.tokenHeader).toBe("Authorization");
    expect(res.tokenPrefix).toBe("Bearer ");
    expect(res.token).toBe("eyJhbGciOiJIUzI1NiJ9.payload.sig");
  });

  it("uses http scheme for localhost and 127.0.0.1 in Raw HTTP", () => {
    const raw = [
      "GET /api/me HTTP/1.1",
      "Host: 127.0.0.1:8080",
      "Authorization: Token local-secret",
    ].join("\n");

    const res = parseAuthPaste(raw);
    expect(res.ok).toBe(true);
    expect(res.detectedUrl).toBe("http://127.0.0.1:8080/api/me");
    expect(res.mode).toBe("bearer");
    expect(res.tokenPrefix).toBe("Token ");
    expect(res.token).toBe("local-secret");
  });

  it("auto-detects Burp Raw HTTP POST login API with JSON body and extra fields", () => {
    const raw = [
      "POST /api/v1/auth/login HTTP/1.1",
      "Host: auth.example.com",
      "Content-Type: application/json",
      "",
      '{"username":"qa_admin","password":"P@ss123","tenant":"acme","remember":true}',
    ].join("\r\n");

    const res = parseAuthPaste(raw);
    expect(res.ok).toBe(true);
    expect(res.sourceFormat).toBe("raw_http");
    expect(res.mode).toBe("login_api");
    expect(res.loginUrl).toBe("https://auth.example.com/api/v1/auth/login");
    expect(res.loginMethod).toBe("POST");
    expect(res.loginContentType).toBe("json");
    expect(res.usernameField).toBe("username");
    expect(res.passwordField).toBe("password");
    expect(res.username).toBe("qa_admin");
    expect(res.password).toBe("P@ss123");
    expect(res.extraFields).toEqual({ tenant: "acme", remember: true });
  });

  it("auto-detects Raw HTTP POST login API with form-urlencoded body", () => {
    const raw = [
      "POST /oauth/token HTTP/1.1",
      "Host: sso.example.com",
      "Content-Type: application/x-www-form-urlencoded",
      "",
      "account=alice%40example.com&pwd=s3cr3t!&grant_type=password",
    ].join("\n");

    const res = parseAuthPaste(raw);
    expect(res.ok).toBe(true);
    expect(res.mode).toBe("login_api");
    expect(res.loginContentType).toBe("form");
    expect(res.usernameField).toBe("account");
    expect(res.passwordField).toBe("pwd");
    expect(res.username).toBe("alice@example.com");
    expect(res.password).toBe("s3cr3t!");
    expect(res.extraFields).toEqual({ grant_type: "password" });
  });

  it("parses Chrome Copy as cURL with ANSI-C $'...' quoting and escaped characters", () => {
    const curl = `curl 'https://api.example.com/v2/profile' \\
  -H $'authorization: Bearer tok\\\\en\\'123' \\
  -H 'sec-ch-ua: "Not_A Brand";v="8"' \\
  -H 'accept: */*'`;

    const res = parseAuthPaste(curl);
    expect(res.ok).toBe(true);
    expect(res.sourceFormat).toBe("curl");
    expect(res.mode).toBe("bearer");
    expect(res.detectedUrl).toBe("https://api.example.com/v2/profile");
    expect(res.token).toBe("tok\\en'123");
  });

  it("merges -H 'cookie: ...' and -b '...' case-insensitively in cURL", () => {
    const curl = `curl 'https://app.example.com/dashboard' \\
  -H 'cookie: sid=abc123; theme=dark' \\
  -b 'csrf=xyz789'`;

    const res = parseAuthPaste(curl);
    expect(res.ok).toBe(true);
    expect(res.sourceFormat).toBe("curl");
    expect(res.mode).toBe("cookie");
    expect(res.cookie).toBe("sid=abc123; theme=dark; csrf=xyz789");
    expect(res.headerNames).toEqual(["Cookie"]);
  });

  it("merges duplicate HTTP/2 lowercase cookie headers in Raw HTTP", () => {
    const raw = [
      "GET /api/cart HTTP/2",
      "host: store.example.com",
      "cookie: JSESSIONID=111",
      "cookie: XSRF-TOKEN=222",
      "Cookie: region=us",
      "user-agent: Chrome/120",
    ].join("\n");

    const res = parseAuthPaste(raw);
    expect(res.ok).toBe(true);
    expect(res.mode).toBe("cookie");
    expect(res.cookie).toBe("JSESSIONID=111; XSRF-TOKEN=222; region=us");
  });

  it("auto-detects PUT login API in cURL", () => {
    const curl = `curl -X PUT 'https://api.example.com/v1/session' \\
  -H 'Content-Type: application/json' \\
  -d '{"email":"bob@example.com","password":"pw1"}'`;

    const res = parseAuthPaste(curl);
    expect(res.ok).toBe(true);
    expect(res.mode).toBe("login_api");
    expect(res.loginMethod).toBe("PUT");
    expect(res.usernameField).toBe("email");
    expect(res.username).toBe("bob@example.com");
    expect(res.password).toBe("pw1");
  });

  it("auto-detects PATCH login API in cURL", () => {
    const curl = `curl --request PATCH 'https://api.example.com/v1/session' \\
  --data-raw '{"mobile":"13800138000","login_pwd":"secret"}'`;

    const res = parseAuthPaste(curl);
    expect(res.ok).toBe(true);
    expect(res.mode).toBe("login_api");
    expect(res.loginMethod).toBe("PATCH");
    expect(res.usernameField).toBe("mobile");
    expect(res.passwordField).toBe("login_pwd");
  });

  it("infers POST for cURL login API when --data-raw is given without -X", () => {
    const curl = `curl 'https://api.example.com/login' \\
  -H 'content-type: application/json' \\
  --data-raw $'{"username":"ops","password":"top\\\\"secret"}'`;

    const res = parseAuthPaste(curl);
    expect(res.ok).toBe(true);
    expect(res.mode).toBe("login_api");
    expect(res.loginMethod).toBe("POST");
    expect(res.username).toBe("ops");
    expect(res.password).toBe('top"secret');
  });

  it("parses Basic auth via cURL -u / --user flag", () => {
    const curl = `curl -u 'svc_user:pass:with:colons' https://api.example.com/metrics`;

    const res = parseAuthPaste(curl);
    expect(res.ok).toBe(true);
    expect(res.mode).toBe("basic");
    expect(res.username).toBe("svc_user");
    expect(res.password).toBe("pass:with:colons");
    expect(res.headerNames).toEqual(["Authorization"]);
  });

  it("decodes Authorization: Basic header in Raw HTTP", () => {
    const raw = [
      "GET /admin/stats HTTP/1.1",
      "Host: internal.example.com",
      "Authorization: Basic YWRtaW46cGFzczEyMw==",
    ].join("\n");

    const res = parseAuthPaste(raw);
    expect(res.ok).toBe(true);
    expect(res.mode).toBe("basic");
    expect(res.username).toBe("admin");
    expect(res.password).toBe("pass123");
  });

  it("extracts single custom token or API key header as bearer mode", () => {
    const raw = "X-API-Key: rk_live_987654321\nAccept: application/json\nUser-Agent: test";
    const res = parseAuthPaste(raw);
    expect(res.ok).toBe(true);
    expect(res.sourceFormat).toBe("headers");
    expect(res.mode).toBe("bearer");
    expect(res.tokenHeader).toBe("X-API-Key");
    expect(res.tokenPrefix).toBe("");
    expect(res.token).toBe("rk_live_987654321");
  });

  it("preserves multiple auth headers and strips noisy browser headers", () => {
    const raw = [
      "Authorization: Bearer jwt-token",
      "X-Tenant-Id: tenant-42",
      "Cookie: sid=s1",
      "User-Agent: Mozilla/5.0",
      "Accept-Language: zh-CN",
      "Sec-Fetch-Dest: empty",
      "Sec-CH-UA-Platform: \"macOS\"",
      "Referer: https://app.example.com/",
      "X-Requested-With: XMLHttpRequest",
    ].join("\n");

    const res = parseAuthPaste(raw);
    expect(res.ok).toBe(true);
    expect(res.sourceFormat).toBe("headers");
    expect(res.mode).toBe("headers");
    expect(res.headers).toEqual({
      Authorization: "Bearer jwt-token",
      "X-Tenant-Id": "tenant-42",
      Cookie: "sid=s1",
    });
    expect(res.headerNames).toHaveLength(3);
  });

  it("supports curl.exe, Windows CMD caret continuations, and long flags", () => {
    const curl = `curl.exe --url "https://gw.example.com/orders" ^\r\n  --header "X-Auth-Token: JWT header.body.sig" ^\r\n  --header "X-App-Id: portal-web"`;

    const res = parseAuthPaste(curl);
    expect(res.ok).toBe(true);
    expect(res.sourceFormat).toBe("curl");
    expect(res.mode).toBe("headers");
    expect(res.detectedUrl).toBe("https://gw.example.com/orders");
    expect(res.headers).toEqual({
      "X-Auth-Token": "JWT header.body.sig",
      "X-App-Id": "portal-web",
    });
  });

  it("handles raw Authorization header without Bearer/Token prefix", () => {
    const raw = "Authorization: raw-opaque-token-value";
    const res = parseAuthPaste(raw);
    expect(res.ok).toBe(true);
    expect(res.mode).toBe("bearer");
    expect(res.tokenHeader).toBe("Authorization");
    expect(res.tokenPrefix).toBe("");
    expect(res.token).toBe("raw-opaque-token-value");
  });

  it("falls back to non-noisy custom header when no standard auth header is present", () => {
    const raw = [
      "Host: api.example.com",
      "Custom-Signature: hmac-sha256=abcdef",
      "Accept: application/json",
    ].join("\n");

    const res = parseAuthPaste(raw);
    expect(res.ok).toBe(true);
    expect(res.mode).toBe("headers");
    expect(res.headers).toEqual({
      "Custom-Signature": "hmac-sha256=abcdef",
    });
  });

  it("ignores malformed non-header garbage text cleanly", () => {
    const res = parseAuthPaste("just some random text without colons");
    expect(res.ok).toBe(false);
    expect(res.errorKey).toBe("noAuthFound");
  });
});
