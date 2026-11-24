"""Static security audit of HAR captures and JavaScript bundles.

Inspects security response headers, CORS policies, Set-Cookie attributes, and
redacted sensitive data/error leak patterns. Raw secrets and PII are NEVER
written to reports or logs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reconqa.utils.io import ensure_dir, write_json

SECURITY_HEADERS = (
    ("strict-transport-security", "HSTS", "medium"),
    ("content-security-policy", "CSP", "medium"),
    ("x-frame-options", "X-Frame-Options", "low"),
    ("x-content-type-options", "X-Content-Type-Options", "low"),
    ("referrer-policy", "Referrer-Policy", "low"),
)

CN_MOBILE_RE = re.compile(r"(?<![0-9])(1[3-9]\d)\d{4}(\d{4})(?![0-9])")
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9.-]+\.[A-Za-z]{2,10})(?![A-Za-z0-9.-])"
)
PRIVATE_IP_RE = re.compile(
    r"(?<![0-9.])((?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3})(?![0-9.])"
)
TEST_STAGING_URL_RE = re.compile(
    r"https?://[a-zA-Z0-9.-]*(?:test\d*|stage|staging|dev|pre|uat|sit|qa)[a-zA-Z0-9.-]*\.[a-z]{2,10}"
)
JWT_RE = re.compile(
    r"\b(eyJ[A-Za-z0-9_-]{8,})\.(eyJ[A-Za-z0-9_-]{8,})\.([A-Za-z0-9_-]{8,})\b"
)
HARDCODED_SECRET_RE = re.compile(
    r"""['"]?(appKey|appSecret|accessKeyId|secretKey|privateKey|apiKey|api_key|"""
    r"""client_secret|access_token|auth_token|aws_secret_access_key)['"]?\s*[:=]\s*"""
    r"""['"]([^'"\s]{6,120})['"]""",
    re.IGNORECASE,
)
PROVIDER_TOKEN_RE = re.compile(
    r"\b(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{16,})\b"
)
PEM_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
    r"(?:\s|\\r|\\n)+[A-Za-z0-9+/=]{32,}"
)
STACK_SQL_RE = re.compile(
    r"(Exception in thread|NullPointerException|SQLException|SQL syntax|"
    r"ORA-\d{5}|Traceback \(most recent call last\)|"
    r"at [a-zA-Z0-9_.$]+\([A-Za-z0-9_]+\.java:\d+\)|"
    r"SQLSTATE\[[A-Z0-9]+\])"
)

EMAIL_FALSE_POSITIVE_DOMAINS = (
    "example.com",
    "example.org",
    "github.com",
    "users.noreply.github.com",
    "npmjs.com",
    "npmjs.org",
    "w3.org",
    "apache.org",
    "schema.org",
    "webpack.js",
)

_VENDOR_JS_TOKENS = (
    "elementui",
    "element-ui",
    "element-plus",
    "chunk-libs",
    "chunk-vendors",
    "vendor",
    "echarts",
    "lodash",
    "tinymce",
    "moment",
    "xlsx",
    "quill",
    "pdf",
    "monaco",
)

_OSS_LICENSE_KEYWORDS = (
    "copyright",
    "license",
    "@author",
    "author:",
    "mit ",
    "contributors",
)


def _is_vendor_js_name(name_or_path: str) -> bool:
    low = Path(name_or_path).name.lower()
    return any(tok in low for tok in _VENDOR_JS_TOKENS)


def _iter_har_files(har_dir: Path) -> list[Path]:
    if har_dir.is_file() and har_dir.suffix.lower() == ".har":
        return [har_dir]
    if har_dir.is_dir():
        return sorted(p for p in har_dir.glob("*.har") if p.is_file())
    return []


def _domain_matches(host: str, target_domain: str | None) -> bool:
    if not target_domain:
        return True
    h = host.lower().strip(".")
    td = target_domain.lower().strip(".")
    return h == td or h.endswith("." + td) or td in h


def _decode_har_content(content: dict[str, Any]) -> str:
    text = content.get("text")
    if not text or not isinstance(text, str):
        return ""
    if content.get("encoding") == "base64":
        try:
            raw = base64.b64decode(text, validate=False)
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return text


def _headers_by_name(headers: list[dict[str, Any]] | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for h in headers or []:
        name = (h.get("name") or "").strip().lower()
        if not name or name.startswith(":"):
            continue
        out[name].append(str(h.get("value") or ""))
    return out


def _mask_secret(value: str) -> str:
    v = value.strip()
    if len(v) <= 8:
        return "***REDACTED***"
    return f"{v[:3]}****{v[-2:]}"


def _mask_email(local: str, domain: str) -> str:
    if len(local) <= 2:
        masked_local = "*" * max(1, len(local))
    else:
        masked_local = f"{local[0]}***{local[-1]}"
    return f"{masked_local}@{domain}"


def _mask_ip(ip: str) -> str:
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.*.*"
    return "***REDACTED***"


def _sanitize_context_window(snippet: str) -> str:
    """Mask any adjacent secrets/PII inside a before/after context slice and collapse whitespace."""
    s = re.sub(r"\s+", " ", snippet)
    s = PEM_KEY_RE.sub("-----BEGIN ***REDACTED*** PRIVATE KEY-----", s)
    s = JWT_RE.sub(lambda m: f"eyJ***...***{m.group(3)[-4:]}", s)
    s = PROVIDER_TOKEN_RE.sub(lambda m: _mask_secret(m.group(1)), s)
    s = HARDCODED_SECRET_RE.sub(
        lambda m: m.group(0).replace(m.group(2), _mask_secret(m.group(2))),
        s,
    )
    s = CN_MOBILE_RE.sub(lambda m: f"{m.group(1)}****{m.group(2)}", s)
    s = EMAIL_RE.sub(lambda m: _mask_email(m.group(1), m.group(2).lower()), s)
    s = PRIVATE_IP_RE.sub(lambda m: _mask_ip(m.group(1)), s)
    return s


def _build_redacted_context(
    text: str,
    start: int,
    end: int,
    masked_match: str,
    window: int = 45,
) -> str:
    """Build a compact redacted surrounding context snippet (~45 chars before/after).

    Sanitizes a wider margin (+120 chars) before trimming so adjacent key/value
    secrets that straddle the 45-char boundary are still fully redacted.
    """
    margin = window + 120
    wide_before_start = max(0, start - margin)
    wide_after_end = min(len(text), end + margin)

    sanitized_before = _sanitize_context_window(text[wide_before_start:start])
    sanitized_after = _sanitize_context_window(text[end:wide_after_end])

    before = sanitized_before[-window:] if len(sanitized_before) > window else sanitized_before
    after = sanitized_after[:window] if len(sanitized_after) > window else sanitized_after

    prefix = "..." if (wide_before_start > 0 or len(sanitized_before) > window) else ""
    suffix = "..." if (wide_after_end < len(text) or len(sanitized_after) > window) else ""
    return f"{prefix}{before}{masked_match}{after}{suffix}".strip()


def _scan_text_for_leaks(
    text: str,
    source_label: str,
    leaks: dict[str, list[dict[str, str]]],
    seen_keys: set[tuple[str, str, str]],
    *,
    is_js: bool = False,
    is_vendor_js: bool = False,
    observed_target_hosts: set[str] | None = None,
) -> None:
    if not text:
        return

    def _add(kind: str, sample: str, severity: str, context: str = "") -> None:
        key = (kind, source_label, sample)
        if key in seen_keys:
            return
        seen_keys.add(key)
        leaks[kind].append(
            {
                "source": source_label,
                "sample": sample,
                "severity": severity,
                "context": context,
            }
        )

    if not is_vendor_js:
        for m in CN_MOBILE_RE.finditer(text):
            masked = f"{m.group(1)}****{m.group(2)}"
            ctx = _build_redacted_context(text, m.start(), m.end(), masked)
            _add("cn_mobile", masked, "medium", ctx)
            break

        for m in EMAIL_RE.finditer(text):
            local, domain = m.group(1), m.group(2).lower()
            if any(
                domain == fp or domain.endswith("." + fp)
                for fp in EMAIL_FALSE_POSITIVE_DOMAINS
            ):
                continue
            if domain.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")):
                continue
            if is_js:
                ctx_check = text[max(0, m.start() - 120) : min(len(text), m.end() + 120)].lower()
                if any(kw in ctx_check for kw in _OSS_LICENSE_KEYWORDS):
                    continue
            masked = _mask_email(local, domain)
            ctx = _build_redacted_context(text, m.start(), m.end(), masked)
            _add("email", masked, "low", ctx)
            break

    for m in PRIVATE_IP_RE.finditer(text):
        masked = _mask_ip(m.group(1))
        ctx = _build_redacted_context(text, m.start(), m.end(), masked)
        _add("private_ip", masked, "low", ctx)
        break

    for m in TEST_STAGING_URL_RE.finditer(text):
        matched_url = m.group(0)
        matched_host = (urlparse(matched_url).hostname or "").lower()
        if observed_target_hosts and matched_host in observed_target_hosts:
            continue
        sample = matched_url[:80]
        ctx = _build_redacted_context(text, m.start(), m.end(), sample)
        _add("test_staging_url", sample, "low", ctx)
        break

    for m in HARDCODED_SECRET_RE.finditer(text):
        key_name, val = m.group(1), m.group(2)
        if val.lower() in {"placeholder", "changeme", "undefined", "null", "false", "true"}:
            continue
        masked_val = _mask_secret(val)
        ctx = _build_redacted_context(text, m.start(2), m.end(2), masked_val)
        _add("hardcoded_secret", f"{key_name}={masked_val}", "high", ctx)

    for m in PROVIDER_TOKEN_RE.finditer(text):
        masked = _mask_secret(m.group(1))
        ctx = _build_redacted_context(text, m.start(), m.end(), masked)
        _add("hardcoded_secret", masked, "high", ctx)

    pem_match = PEM_KEY_RE.search(text)
    if pem_match:
        masked_pem = "-----BEGIN ***REDACTED*** PRIVATE KEY-----"
        ctx = _build_redacted_context(text, pem_match.start(), pem_match.end(), masked_pem)
        _add("hardcoded_secret", masked_pem, "critical", ctx)

    for m in JWT_RE.finditer(text):
        token_tail = m.group(3)[-4:]
        masked = f"eyJ***...***{token_tail}"
        ctx = _build_redacted_context(text, m.start(), m.end(), masked)
        _add("jwt_token", masked, "high", ctx)
        break

    for m in STACK_SQL_RE.finditer(text):
        sample = m.group(0)[:80]
        ctx = _build_redacted_context(text, m.start(), m.end(), sample)
        _add("stack_trace_sql_error", sample, "medium", ctx)
        break


def run_static_audit(
    har_dir: Path,
    out_dir: Path,
    *,
    js_dir: Path | None = None,
    target_domain: str | None = None,
) -> dict[str, Any]:
    """Run static security audit across HAR responses and JS bundles."""
    ensure_dir(out_dir)
    har_files = _iter_har_files(har_dir)

    host_stats: dict[str, dict[str, Any]] = {}
    cookie_issues: list[dict[str, Any]] = []
    seen_cookie_keys: set[tuple[str, str, tuple[str, ...]]] = set()
    leaks: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen_leak_keys: set[tuple[str, str, str]] = set()
    cors_issues: list[dict[str, Any]] = []
    seen_cors_keys: set[tuple[str, str, str]] = set()

    total_entries = 0
    scanned_js_files = 0
    observed_target_hosts: set[str] = set()

    parsed_hars: list[dict[str, Any]] = []
    for hp in har_files:
        try:
            har_data = json.loads(hp.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        parsed_hars.append(har_data)
        for entry in har_data.get("log", {}).get("entries", []):
            url = str((entry.get("request") or {}).get("url") or "")
            host = (urlparse(url).hostname or "").lower()
            if host and _domain_matches(host, target_domain):
                observed_target_hosts.add(host)

    for har_data in parsed_hars:
        for entry in har_data.get("log", {}).get("entries", []):
            req = entry.get("request") or {}
            resp = entry.get("response") or {}
            url = str(req.get("url") or "")
            parsed = urlparse(url)
            host = parsed.hostname or ""
            path = parsed.path or "/"
            if not host or not _domain_matches(host, target_domain):
                continue

            total_entries += 1
            rh = _headers_by_name(resp.get("headers"))
            qh = _headers_by_name(req.get("headers"))
            content = resp.get("content") or {}
            mime = (
                content.get("mimeType")
                or (rh.get("content-type", [""])[0])
            ).split(";")[0].strip().lower()

            if host not in host_stats:
                host_stats[host] = {
                    "seen": 0,
                    "headers": {hdr: 0 for hdr, _, _ in SECURITY_HEADERS},
                    "cors_wildcard": 0,
                    "cors_dangerous": 0,
                    "servers": set(),
                }
            hs = host_stats[host]
            hs["seen"] += 1
            for hdr, _, _ in SECURITY_HEADERS:
                if hdr in rh and any(v.strip() for v in rh[hdr]):
                    hs["headers"][hdr] += 1

            for srv in rh.get("server", []):
                if srv.strip():
                    hs["servers"].add(srv.strip()[:60])

            # CORS audit
            acao = (rh.get("access-control-allow-origin", [""])[0]).strip()
            acac = (rh.get("access-control-allow-credentials", [""])[0]).strip().lower()
            req_origin = (qh.get("origin", [""])[0]).strip()
            if acao == "*":
                hs["cors_wildcard"] += 1
                if acac == "true":
                    hs["cors_dangerous"] += 1
                    ckey = (host, path, "wildcard_with_credentials")
                    if ckey not in seen_cors_keys:
                        seen_cors_keys.add(ckey)
                        cors_issues.append(
                            {
                                "host": host,
                                "endpoint": f"{host}{path}",
                                "kind": "wildcard_with_credentials",
                                "acao": acao,
                                "acac": acac,
                                "severity": "high",
                            }
                        )
                else:
                    ckey = (host, path, "wildcard_origin")
                    if ckey not in seen_cors_keys:
                        seen_cors_keys.add(ckey)
                        cors_issues.append(
                            {
                                "host": host,
                                "endpoint": f"{host}{path}",
                                "kind": "wildcard_origin",
                                "acao": acao,
                                "acac": acac or "false",
                                "severity": "low",
                            }
                        )
            elif acao and req_origin and acao == req_origin and acac == "true":
                hs["cors_dangerous"] += 1
                ckey = (host, path, "reflected_origin_with_credentials")
                if ckey not in seen_cors_keys:
                    seen_cors_keys.add(ckey)
                    cors_issues.append(
                        {
                            "host": host,
                            "endpoint": f"{host}{path}",
                            "kind": "reflected_origin_with_credentials",
                            "acao": acao[:80],
                            "acac": acac,
                            "severity": "high",
                        }
                    )

            # Set-Cookie audit (never record raw cookie value)
            for sc_val in rh.get("set-cookie", []):
                for line in sc_val.splitlines():
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    cookie_name = line.split("=", 1)[0].strip()
                    attrs_lower = line.lower()
                    missing: list[str] = []
                    if "httponly" not in attrs_lower:
                        missing.append("HttpOnly")
                    if "secure" not in attrs_lower:
                        missing.append("Secure")
                    if "samesite" not in attrs_lower:
                        missing.append("SameSite")
                    if missing and cookie_name:
                        ck = (host, cookie_name, tuple(missing))
                        if ck not in seen_cookie_keys:
                            seen_cookie_keys.add(ck)
                            sev = (
                                "medium"
                                if ("HttpOnly" in missing or "Secure" in missing)
                                else "low"
                            )
                            cookie_issues.append(
                                {
                                    "host": host,
                                    "endpoint": f"{host}{path}",
                                    "cookie_name": cookie_name,
                                    "missing_flags": missing,
                                    "severity": sev,
                                }
                            )

            # Sensitive leak scan on textual/API/JS responses
            is_js_resp = (
                "javascript" in mime
                or "ecmascript" in mime
                or path.lower().endswith((".js", ".mjs", ".cjs"))
            )
            is_text_like = is_js_resp or any(
                k in mime for k in ("json", "html", "xml", "text")
            )
            if is_text_like:
                body_text = _decode_har_content(content)
                if body_text:
                    _scan_text_for_leaks(
                        body_text,
                        f"{host}{path[:80]}",
                        leaks,
                        seen_leak_keys,
                        is_js=is_js_resp,
                        is_vendor_js=is_js_resp and _is_vendor_js_name(path),
                        observed_target_hosts=observed_target_hosts,
                    )

    # Scan standalone JS bundle directory (deduplicated by content SHA-256 across scenarios)
    effective_js_dir = js_dir
    if effective_js_dir is None and har_dir.is_dir() and (har_dir / "js_bundles").is_dir():
        effective_js_dir = har_dir / "js_bundles"

    if effective_js_dir and effective_js_dir.is_dir():
        seen_js_hashes: set[str] = set()
        for js_file in sorted(effective_js_dir.rglob("*.js")):
            if not js_file.is_file():
                continue
            try:
                raw_bytes = js_file.read_bytes()
            except Exception:
                continue
            digest = hashlib.sha256(raw_bytes).hexdigest()
            if digest in seen_js_hashes:
                continue
            seen_js_hashes.add(digest)
            scanned_js_files += 1
            js_text = raw_bytes.decode("utf-8", errors="replace")
            _scan_text_for_leaks(
                js_text,
                f"js:{js_file.name}",
                leaks,
                seen_leak_keys,
                is_js=True,
                is_vendor_js=_is_vendor_js_name(js_file.name),
                observed_target_hosts=observed_target_hosts,
            )

    # Normalize host_headers coverage and assemble normalized findings
    host_headers_out: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []

    for host, st in sorted(host_stats.items(), key=lambda kv: -kv[1]["seen"]):
        seen = st["seen"]
        coverage: dict[str, dict[str, Any]] = {}
        for hdr, label, sev in SECURITY_HEADERS:
            present = st["headers"][hdr]
            ratio = round(present / seen, 4) if seen else 0.0
            coverage[hdr] = {
                "label": label,
                "present": present,
                "total": seen,
                "coverage": ratio,
            }
            if seen > 0 and present == 0:
                findings.append(
                    {
                        "category": "security_header",
                        "severity": sev,
                        "endpoint": host,
                        "title": f"Missing {label} ({hdr}) header on {host}",
                        "evidence": {
                            "host": host,
                            "header": hdr,
                            "present": present,
                            "total": seen,
                        },
                    }
                )

        host_headers_out[host] = {
            "seen": seen,
            "coverage": coverage,
            "cors_wildcard": st["cors_wildcard"],
            "cors_dangerous": st["cors_dangerous"],
            "servers": sorted(st["servers"]),
        }

    for ci in cors_issues:
        findings.append(
            {
                "category": "cors",
                "severity": ci["severity"],
                "endpoint": ci["endpoint"],
                "title": f"Permissive CORS policy ({ci['kind']}) on {ci['endpoint']}",
                "evidence": ci,
            }
        )

    for ck_issue in cookie_issues:
        missing_str = ", ".join(ck_issue["missing_flags"])
        findings.append(
            {
                "category": "cookie",
                "severity": ck_issue["severity"],
                "endpoint": ck_issue["endpoint"],
                "title": f"Set-Cookie '{ck_issue['cookie_name']}' missing {missing_str}",
                "evidence": ck_issue,
            }
        )

    leaks_dict = {k: v for k, v in sorted(leaks.items())}
    for kind, items in leaks_dict.items():
        for item in items:
            findings.append(
                {
                    "category": "sensitive_leak",
                    "severity": item["severity"],
                    "endpoint": item["source"],
                    "title": f"Potential {kind} exposure (redacted) in {item['source']}",
                    "evidence": {
                        "leak_type": kind,
                        "source": item["source"],
                        "redacted_sample": item["sample"],
                        "context": item.get("context", ""),
                    },
                }
            )

    summary = {
        "har_files": len(har_files),
        "total_entries": total_entries,
        "hosts_audited": len(host_headers_out),
        "scanned_js_files": scanned_js_files,
        "cookie_issue_count": len(cookie_issues),
        "cors_issue_count": len(cors_issues),
        "sensitive_leak_count": sum(len(v) for v in leaks_dict.values()),
        "findings_count": len(findings),
    }

    report: dict[str, Any] = {
        "summary": summary,
        "host_headers": host_headers_out,
        "cors_issues": cors_issues,
        "cookie_issues": cookie_issues,
        "sensitive_leaks": leaks_dict,
        "findings": findings,
    }

    json_path = out_dir / "static_audit.json"
    md_path = out_dir / "static_audit.md"
    write_json(json_path, report)
    md_path.write_text(_render_markdown(report), encoding="utf-8")

    report["artifacts"] = {
        "json": str(json_path),
        "markdown": str(md_path),
    }
    return report


def _render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        "# Static Security Audit Report",
        "",
        "> Passive analysis of HAR responses and JS bundles. All sensitive values are redacted.",
        "",
        f"- HAR files analyzed: `{s['har_files']}`",
        f"- Responses audited: `{s['total_entries']}` across `{s['hosts_audited']}` hosts",
        f"- External JS files scanned: `{s['scanned_js_files']}`",
        f"- Total findings: `{s['findings_count']}`",
        "",
        "## 1. Security Header Coverage by Host",
        "",
    ]

    host_headers = report["host_headers"]
    if host_headers:
        lines.append(
            "| Host | Requests | HSTS | CSP | X-Frame-Options | XCTO | Referrer-Policy | "
            "CORS `*` | Dangerous CORS |"
        )
        lines.append("| :--- | ---: | :---: | :---: | :---: | :---: | :---: | ---: | ---: |")
        for host, data in host_headers.items():
            n = data["seen"]
            cov = data["coverage"]
            lines.append(
                f"| `{host}` | {n} "
                f"| {cov['strict-transport-security']['present']}/{n} "
                f"| {cov['content-security-policy']['present']}/{n} "
                f"| {cov['x-frame-options']['present']}/{n} "
                f"| {cov['x-content-type-options']['present']}/{n} "
                f"| {cov['referrer-policy']['present']}/{n} "
                f"| {data['cors_wildcard']} | {data['cors_dangerous']} |"
            )
    else:
        lines.append("- No host responses matched the filter.")

    lines.extend(["", "## 2. Set-Cookie Attribute Audit", ""])
    if report["cookie_issues"]:
        lines.append("| Host | Cookie | Missing Flags | Severity |")
        lines.append("| :--- | :--- | :--- | :---: |")
        for ck in report["cookie_issues"][:50]:
            flags = ", ".join(ck["missing_flags"])
            lines.append(
                f"| `{ck['host']}` | `{ck['cookie_name']}` | `{flags}` | `{ck['severity']}` |"
            )
    else:
        lines.append("- No insecure `Set-Cookie` headers detected.")

    lines.extend(["", "## 3. Redacted Sensitive Leak Findings", ""])
    if report["sensitive_leaks"]:
        for kind, items in report["sensitive_leaks"].items():
            lines.append(f"### `{kind}` ({len(items)})")
            for it in items[:20]:
                lines.append(f"- `{it['source']}` -> `{it['sample']}` (`{it['severity']}`)")
            lines.append("")
    else:
        lines.append("- No sensitive patterns detected.")

    return "\n".join(lines).rstrip() + "\n"
