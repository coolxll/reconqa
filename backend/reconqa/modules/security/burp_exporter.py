"""Export flagged HAR requests as Raw HTTP (.http) files and executable cURL commands."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from reconqa.utils.io import ensure_dir, slugify, write_json

FLAGGED_VERDICTS = {
    "unauth_bypass",
    "confirmed_idor",
    "suspected_idor",
    "mutation_candidate",
}

SIGNING_HINTS = (
    "sign",
    "signature",
    "nonce",
    "x-sign",
    "x-signature",
    "timestamp",
    "x-timestamp",
    "x-nonce",
    "hmac",
    "digest",
    "encrypt",
)

STATIC_EXTS = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".map",
)


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


def _load_auth_matrix_flags(auth_matrix_path: Path | None) -> dict[str, list[str]]:
    """Return map of match-key (url, endpoint, or method+path) -> list of verdict labels."""
    flagged: dict[str, list[str]] = {}
    if not auth_matrix_path or not auth_matrix_path.exists():
        return flagged
    try:
        data = json.loads(auth_matrix_path.read_text(encoding="utf-8"))
    except Exception:
        return flagged

    rows: list[dict[str, Any]] = []
    for key in ("unauth_results", "idor_results"):
        val = data.get(key)
        if isinstance(val, list):
            rows.extend(r for r in val if isinstance(r, dict))

    for row in rows:
        verdict = str(row.get("verdict") or "")
        if verdict not in FLAGGED_VERDICTS:
            continue
        label = f"auth_matrix:{verdict}"
        for k in (
            str(row.get("url") or ""),
            str(row.get("endpoint") or ""),
            f"{str(row.get('method') or '').upper()} {str(row.get('normalized_path') or '')}".strip(),
        ):
            if k:
                flagged.setdefault(k, [])
                if label not in flagged[k]:
                    flagged[k].append(label)
    return flagged


def _detect_signing_hints(
    headers: list[dict[str, Any]],
    query_str: str,
    body_text: str,
) -> list[str]:
    hits: list[str] = []
    for h in headers:
        name = str(h.get("name") or "").strip().lower()
        if not name or name.startswith(":"):
            continue
        if any(hint in name for hint in SIGNING_HINTS):
            hits.append(f"signing_header:{name}")

    for qk, _ in parse_qsl(query_str):
        low = qk.lower()
        if any(hint in low for hint in SIGNING_HINTS):
            hits.append(f"signing_query:{qk}")

    if body_text:
        try:
            body_obj = json.loads(body_text)
        except Exception:
            body_obj = None
        if isinstance(body_obj, dict):
            for k in body_obj:
                low = str(k).lower()
                if any(hint in low for hint in SIGNING_HINTS):
                    hits.append(f"signing_body:{k}")
    return sorted(set(hits))


def _clean_headers(headers: list[dict[str, Any]], netloc: str) -> list[tuple[str, str]]:
    """Strip HTTP/2 pseudo-headers and ensure Host header is present."""
    cleaned: list[tuple[str, str]] = []
    has_host = False
    for h in headers:
        name = str(h.get("name") or "").strip()
        if not name or name.startswith(":"):
            continue
        low = name.lower()
        if low == "content-length":
            continue
        if low == "host":
            has_host = True
        cleaned.append((name, str(h.get("value") or "")))
    if not has_host and netloc:
        cleaned.insert(0, ("Host", netloc))
    return cleaned


def _format_raw_http(
    method: str,
    path_with_query: str,
    headers: list[tuple[str, str]],
    body_text: str,
) -> str:
    lines = [f"{method} {path_with_query} HTTP/1.1"]
    for name, val in headers:
        lines.append(f"{name}: {val}")
    if body_text:
        body_bytes = body_text.encode("utf-8")
        lines.append(f"Content-Length: {len(body_bytes)}")
    lines.append("")
    if body_text:
        lines.append(body_text)
    return "\r\n".join(lines) + ("\r\n" if not body_text else "")


def _format_curl(
    method: str,
    url: str,
    headers: list[tuple[str, str]],
    body_text: str,
) -> str:
    parts = ["curl", "-i", "-sS", "-X", shlex.quote(method)]
    for name, val in headers:
        if name.lower() in ("host", "content-length", "accept-encoding"):
            continue
        parts.extend(["-H", shlex.quote(f"{name}: {val}")])
    if body_text:
        parts.extend(["--data-raw", shlex.quote(body_text)])
    parts.append(shlex.quote(url))
    return " ".join(parts)


def export_burp_requests(
    har_dir: Path,
    out_dir: Path,
    *,
    auth_matrix_path: Path | None = None,
    target_domain: str | None = None,
    endpoints: list[str] | None = None,
) -> dict[str, Any]:
    """Export selected high-risk or signed HAR requests to `.http` and `replay_commands.sh`."""
    export_dir = ensure_dir(out_dir / "burp_exports")
    flagged_map = _load_auth_matrix_flags(auth_matrix_path)
    explicit_filters = [e.strip() for e in (endpoints or []) if e and e.strip()]

    exported: list[dict[str, Any]] = []
    curl_lines: list[str] = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# Auto-generated by ReconQA burp_exporter (HTTP/2 pseudo-headers stripped)",
        "",
    ]
    seen_req: set[tuple[str, str]] = set()

    for hp in _iter_har_files(har_dir):
        try:
            har_data = json.loads(hp.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue

        for entry in har_data.get("log", {}).get("entries", []):
            req = entry.get("request") or {}
            method = str(req.get("method") or "GET").upper()
            if method == "OPTIONS":
                continue
            url = str(req.get("url") or "")
            parsed = urlparse(url)
            host = parsed.hostname or ""
            path = parsed.path or "/"
            if not host or not _domain_matches(host, target_domain):
                continue
            if path.lower().endswith(STATIC_EXTS):
                continue

            req_headers = req.get("headers") or []
            body_text = str((req.get("postData") or {}).get("text") or "")
            ep_key = f"{method} {path}"
            reasons: list[str] = []

            if explicit_filters:
                matched_explicit = any(
                    flt == url
                    or flt == path
                    or flt == ep_key
                    or flt in url
                    or flt in ep_key
                    for flt in explicit_filters
                )
                if not matched_explicit:
                    continue
                reasons.append("explicit_endpoint_filter")
            else:
                if url in flagged_map:
                    reasons.extend(flagged_map[url])
                if ep_key in flagged_map:
                    reasons.extend(flagged_map[ep_key])
                for fk, freasons in flagged_map.items():
                    if fk.startswith(f"{method} ") and "{" in fk:
                        # Match simple template prefix when ID normalized
                        prefix = fk.split("{", 1)[0].split(" ", 1)[1]
                        if prefix and path.startswith(prefix):
                            reasons.extend(freasons)
                reasons.extend(_detect_signing_hints(req_headers, parsed.query, body_text))
                reasons = sorted(set(reasons))
                if not reasons:
                    continue

            dedup_key = (method, url)
            if dedup_key in seen_req:
                continue
            seen_req.add(dedup_key)

            idx = len(exported) + 1
            path_slug = slugify(path.strip("/") or "root")[:48]
            filename = f"{idx:03d}_{method.lower()}_{path_slug}.http"
            http_file = export_dir / filename

            path_with_query = f"{path}?{parsed.query}" if parsed.query else path
            cleaned_hdrs = _clean_headers(req_headers, parsed.netloc)
            raw_http = _format_raw_http(method, path_with_query, cleaned_hdrs, body_text)
            http_file.write_text(raw_http, encoding="utf-8")

            curl_cmd = _format_curl(method, url, cleaned_hdrs, body_text)
            curl_lines.append(f"# [{idx:03d}] {method} {url} ({', '.join(reasons)})")
            curl_lines.append(f"# Raw HTTP: {filename}")
            curl_lines.append(curl_cmd)
            curl_lines.append("")

            exported.append(
                {
                    "index": idx,
                    "method": method,
                    "url": url,
                    "path": path,
                    "reasons": reasons,
                    "http_file": str(http_file),
                    "filename": filename,
                }
            )

    replay_script = export_dir / "replay_commands.sh"
    replay_script.write_text("\n".join(curl_lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(replay_script, 0o755)

    result: dict[str, Any] = {
        "export_dir": str(export_dir),
        "replay_script": str(replay_script),
        "count": len(exported),
        "exported": exported,
    }
    write_json(export_dir / "manifest.json", result)
    return result
