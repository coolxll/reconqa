"""Authorization boundary & dual-account A/B IDOR/BOLA regression matrix.

Supports:
  1. Unauthenticated replay (strips auth headers/cookies on protected endpoints)
  2. Dual-account A/B horizontal access control replay (replays Account A's
     object-ID requests with Account B's credentials)

Safety default: read-only endpoints (GET and read-like POST) are replayed
automatically; state-mutating endpoints are classified as `mutation_candidate`
for manual/Burp verification unless `include_mutations=True` is explicitly set.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

import httpx

from reconqa.utils.io import ensure_dir, write_json

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
    ".eot",
    ".map",
    ".mp4",
    ".webm",
    ".webp",
)

AUTH_HEADER_NAMES = {
    "authorization",
    "cookie",
    "x-auth-token",
    "x-api-key",
    "token",
    "x-token",
    "proxy-authorization",
}

SKIP_REPLAY_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "accept-encoding",
    "keep-alive",
    "upgrade",
    "te",
}

READ_POST_HINTS = (
    "query",
    "list",
    "detail",
    "search",
    "get",
    "fetch",
    "find",
    "info",
    "view",
    "check",
    "count",
    "stat",
    "page",
    "lookup",
)

MUTATION_HINTS = (
    "create",
    "update",
    "delete",
    "remove",
    "pay",
    "cancel",
    "submit",
    "add",
    "save",
    "modify",
    "edit",
    "close",
    "confirm",
    "refund",
    "transfer",
    "withdraw",
    "bind",
    "unbind",
    "reset",
    "approve",
    "reject",
    "checkout",
    "claim",
    "purchase",
)

ID_PARAM_HINTS = (
    "id",
    "userid",
    "orderid",
    "orderno",
    "addressid",
    "cartid",
    "cartitemid",
    "itemid",
    "goodsid",
    "skuid",
    "uid",
    "accountid",
    "memberid",
    "recordid",
    "docid",
    "fileid",
    "ticketid",
    "invoiceid",
)

AUTH_ERROR_PHRASES = (
    "unauthorized",
    "forbidden",
    "unauthenticated",
    "not logged in",
    "login required",
    "access denied",
    "permission denied",
    "invalid token",
    "token expired",
    "未登录",
    "请登录",
    "无权限",
    "禁止访问",
)

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
HEX24_RE = re.compile(r"^[0-9a-fA-F]{24}$")
NUMERIC_ID_RE = re.compile(r"^\d{2,}$")
PREFIXED_ID_RE = re.compile(r"^[A-Za-z]{2,16}[-_][A-Za-z0-9_-]{3,}$")


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


def _decode_content(content: dict[str, Any]) -> str:
    text = content.get("text")
    if not text or not isinstance(text, str):
        return ""
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text, validate=False).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return text


def _is_id_segment(segment: str) -> bool:
    if not segment:
        return False
    return bool(
        NUMERIC_ID_RE.match(segment)
        or UUID_RE.match(segment)
        or HEX24_RE.match(segment)
        or PREFIXED_ID_RE.match(segment)
    )


def _normalize_path_and_extract_ids(path: str) -> tuple[str, dict[str, str]]:
    segments = path.split("/")
    norm_segments: list[str] = []
    id_segments: dict[str, str] = {}
    for idx, seg in enumerate(segments):
        if _is_id_segment(seg):
            prev = segments[idx - 1] if idx > 0 else "item"
            singular = prev[:-1] if prev.endswith("s") and len(prev) > 2 else prev
            param = f"{singular or 'resource'}Id"
            if param in id_segments:
                param = f"{param}_{idx}"
            id_segments[param] = seg
            norm_segments.append("{" + param + "}")
        else:
            norm_segments.append(seg)
    return "/".join(norm_segments), id_segments


def _is_id_key(key: str) -> bool:
    low = key.lower()
    if low in ID_PARAM_HINTS:
        return True
    if low.endswith(("id", "_id", "no", "_no", "uid")):
        return True
    return False


def _extract_id_params(
    path_ids: dict[str, str],
    query_str: str,
    body_json: Any,
) -> dict[str, str]:
    found: dict[str, str] = {}
    for k, v in path_ids.items():
        found[f"path.{k}"] = v
    for qk, qv in parse_qsl(query_str):
        if _is_id_key(qk) and qv:
            found[f"query.{qk}"] = qv

    def _walk(obj: Any, prefix: str, depth: int) -> None:
        if depth > 2:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (str, int)) and _is_id_key(str(k)) and str(v).strip():
                    found[f"{prefix}.{k}"] = str(v)
                elif isinstance(v, (dict, list)):
                    _walk(v, f"{prefix}.{k}", depth + 1)
        elif isinstance(obj, list):
            for i, item in enumerate(obj[:3]):
                _walk(item, f"{prefix}[{i}]", depth + 1)

    if body_json is not None:
        _walk(body_json, "body", 0)
    return found


def _is_mutation_endpoint(method: str, path: str) -> bool:
    m = method.upper()
    if m in ("GET", "HEAD"):
        return False
    if m in ("PUT", "DELETE", "PATCH"):
        return True
    low_path = path.lower()
    if any(h in low_path for h in MUTATION_HINTS):
        return True
    if m == "POST" and any(h in low_path for h in READ_POST_HINTS):
        return False
    return True


def _load_auth_context(
    auth_path: Path | None,
    extra_headers: dict[str, str] | None,
) -> dict[str, Any] | None:
    """Load cookies and headers from Playwright storage_state.json or a plain header dict."""
    cookies: list[dict[str, Any]] = []
    headers: dict[str, str] = {}

    if auth_path is not None and auth_path.exists():
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            if "cookies" in raw and isinstance(raw["cookies"], list):
                cookies.extend(c for c in raw["cookies"] if isinstance(c, dict))
            if "headers" in raw and isinstance(raw["headers"], dict):
                for k, v in raw["headers"].items():
                    headers[str(k)] = str(v)
            # Plain header mapping JSON (no storage_state schema keys)
            if "cookies" not in raw and "origins" not in raw and "headers" not in raw:
                for k, v in raw.items():
                    if isinstance(v, str):
                        headers[str(k)] = v

    if extra_headers:
        for k, v in extra_headers.items():
            headers[str(k)] = str(v)

    if not cookies and not headers:
        return None
    return {"cookies": cookies, "headers": headers}


def _cookie_header_for_host(cookies: list[dict[str, Any]], host: str) -> str:
    matched: list[str] = []
    fallback: list[str] = []
    h = host.lower()
    for c in cookies:
        name = str(c.get("name") or "").strip()
        val = str(c.get("value") or "")
        if not name:
            continue
        pair = f"{name}={val}"
        fallback.append(pair)
        domain = str(c.get("domain") or "").lstrip(".").lower()
        if not domain or h == domain or h.endswith("." + domain) or h in ("127.0.0.1", "localhost"):
            matched.append(pair)
    chosen = matched or fallback
    return "; ".join(chosen)


def _build_request_headers(
    har_headers: list[dict[str, Any]],
    host: str,
    *,
    auth_ctx: dict[str, Any] | None = None,
    strip_auth: bool = False,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in har_headers:
        name = str(h.get("name") or "").strip()
        if not name or name.startswith(":"):
            continue
        low = name.lower()
        if low in SKIP_REPLAY_HEADERS:
            continue
        if (strip_auth or auth_ctx is not None) and low in AUTH_HEADER_NAMES:
            continue
        out[name] = str(h.get("value") or "")

    if auth_ctx is not None and not strip_auth:
        cookie_str = _cookie_header_for_host(auth_ctx.get("cookies") or [], host)
        if cookie_str:
            out["Cookie"] = cookie_str
        for k, v in (auth_ctx.get("headers") or {}).items():
            # Replace case-insensitively
            existing_key = next((ek for ek in out if ek.lower() == k.lower()), None)
            if existing_key:
                del out[existing_key]
            out[k] = v
    return out


def _try_parse_json(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _flatten_json(obj: Any, prefix: str = "$", depth: int = 0) -> dict[str, str]:
    out: dict[str, str] = {}
    if depth > 5:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}"
            if isinstance(v, (dict, list)):
                out.update(_flatten_json(v, p, depth + 1))
            else:
                out[p] = json.dumps(v, ensure_ascii=False, sort_keys=True)
    elif isinstance(obj, list):
        for idx, v in enumerate(obj[:10]):
            p = f"{prefix}[{idx}]"
            if isinstance(v, (dict, list)):
                out.update(_flatten_json(v, p, depth + 1))
            else:
                out[p] = json.dumps(v, ensure_ascii=False, sort_keys=True)
    return out


def _has_meaningful_data(body_json: Any, body_text: str) -> bool:
    if isinstance(body_json, dict):
        if "data" in body_json:
            d = body_json["data"]
            if d is None or d == "" or d == [] or d == {}:
                return False
            return True
        return len(body_json) > 0
    if isinstance(body_json, list):
        return len(body_json) > 0
    return len(body_text.strip()) > 0


def _is_biz_success(status_code: int, body_json: Any, body_text: str) -> bool:
    if not (200 <= status_code < 300):
        return False
    if isinstance(body_json, dict):
        for key in ("code", "Code", "errCode", "errcode"):
            if key in body_json:
                val = body_json[key]
                if val not in (0, 200, "0", "200", "OK", "SUCCESS", "success"):
                    return False
        for key in ("success", "ok"):
            if key in body_json and body_json[key] is False:
                return False
        for key in ("msg", "message", "error", "errMsg", "detail"):
            if key in body_json and isinstance(body_json[key], str):
                low = body_json[key].lower()
                if any(phrase in low for phrase in AUTH_ERROR_PHRASES):
                    return False
    elif body_text:
        low_txt = body_text[:500].lower()
        if any(phrase in low_txt for phrase in AUTH_ERROR_PHRASES):
            return False
    return True


def _response_similarity(
    baseline_json: Any,
    baseline_text: str,
    replay_json: Any,
    replay_text: str,
) -> float:
    if baseline_json is not None and replay_json is not None:
        if baseline_json == replay_json:
            return 1.0
        fa = _flatten_json(baseline_json)
        fb = _flatten_json(replay_json)
        keys_a, keys_b = set(fa.keys()), set(fb.keys())
        union_keys = keys_a | keys_b
        if not union_keys:
            return 0.0
        shared_keys = keys_a & keys_b
        key_jaccard = len(shared_keys) / len(union_keys)
        val_matches = sum(1 for k in shared_keys if fa[k] == fb[k])
        val_ratio = val_matches / len(union_keys)
        return round(0.4 * key_jaccard + 0.6 * val_ratio, 4)

    if not baseline_text and not replay_text:
        return 1.0
    if not baseline_text or not replay_text:
        return 0.0
    return round(
        SequenceMatcher(None, baseline_text[:2000], replay_text[:2000]).ratio(),
        4,
    )


def _extract_candidates(
    har_dir: Path,
    target_domain: str | None,
) -> list[dict[str, Any]]:
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for hp in _iter_har_files(har_dir):
        try:
            har_data = json.loads(hp.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for entry in har_data.get("log", {}).get("entries", []):
            req = entry.get("request") or {}
            resp = entry.get("response") or {}
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
            has_auth = any(
                str(h.get("name") or "").lower() in AUTH_HEADER_NAMES
                and str(h.get("value") or "").strip()
                for h in req_headers
            )

            post_data = req.get("postData") or {}
            body_text = str(post_data.get("text") or "")
            body_json = _try_parse_json(body_text) if body_text else None
            content_type = str(post_data.get("mimeType") or "")

            resp_status = int(resp.get("status") or 0)
            resp_text = _decode_content(resp.get("content") or {})
            resp_json = _try_parse_json(resp_text)

            norm_path, path_ids = _normalize_path_and_extract_ids(path)
            id_params = _extract_id_params(path_ids, parsed.query, body_json)
            is_mutation = _is_mutation_endpoint(method, path)

            key = (method, norm_path)
            existing = candidates.get(key)
            # Prefer authenticated 2xx sample if duplicates exist
            score = (2 if has_auth else 0) + (1 if 200 <= resp_status < 300 else 0)
            if existing is None or score > existing["_score"]:
                candidates[key] = {
                    "_score": score,
                    "method": method,
                    "url": url,
                    "host": host,
                    "path": path,
                    "normalized_path": norm_path,
                    "endpoint": f"{method} {norm_path}",
                    "req_headers": req_headers,
                    "body_text": body_text,
                    "body_json": body_json,
                    "content_type": content_type,
                    "has_auth": has_auth,
                    "id_params": id_params,
                    "is_mutation": is_mutation,
                    "har_status": resp_status,
                    "har_body_text": resp_text,
                    "har_body_json": resp_json,
                }
    return list(candidates.values())


async def _send_replay(
    client: httpx.AsyncClient,
    cand: dict[str, Any],
    headers: dict[str, str],
) -> tuple[int, str, Any, str | None]:
    try:
        kwargs: dict[str, Any] = {"headers": headers}
        if cand["body_text"] and cand["method"] not in ("GET", "HEAD"):
            if cand["body_json"] is not None:
                kwargs["json"] = cand["body_json"]
            else:
                kwargs["content"] = cand["body_text"].encode("utf-8")
        resp = await client.request(cand["method"], cand["url"], **kwargs)
        text = resp.text
        return resp.status_code, text, _try_parse_json(text), None
    except Exception as exc:
        return 0, "", None, f"{type(exc).__name__}: {exc}"


async def run_auth_matrix_async(
    har_dir: Path,
    out_dir: Path,
    *,
    auth_a_path: Path | None = None,
    auth_b_path: Path | None = None,
    auth_a_headers: dict[str, str] | None = None,
    auth_b_headers: dict[str, str] | None = None,
    include_mutations: bool = False,
    target_domain: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Run async unauthenticated & dual-account A/B IDOR replay matrix."""
    ensure_dir(out_dir)
    auth_a = _load_auth_context(auth_a_path, auth_a_headers)
    auth_b = _load_auth_context(auth_b_path, auth_b_headers)
    candidates = _extract_candidates(har_dir, target_domain)

    unauth_results: list[dict[str, Any]] = []
    idor_results: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    use_trust_env = not any(
        str(c.get("host") or "").lower() in ("127.0.0.1", "localhost", "::1")
        for c in candidates
    )
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=use_trust_env,
    ) as client:
        for cand in candidates:
            is_protected = cand["has_auth"] or auth_a is not None
            has_ids = bool(cand["id_params"])
            is_mut = cand["is_mutation"]

            # Establish Account A baseline (live when auth_a provided & safe to call, else HAR)
            base_status = cand["har_status"]
            base_text = cand["har_body_text"]
            base_json = cand["har_body_json"]

            if auth_a is not None and (not is_mut or include_mutations):
                hdrs_a = _build_request_headers(
                    cand["req_headers"], cand["host"], auth_ctx=auth_a
                )
                st_a, txt_a, js_a, err_a = await _send_replay(client, cand, hdrs_a)
                if err_a is None and st_a > 0:
                    base_status, base_text, base_json = st_a, txt_a, js_a

            baseline_ok = _is_biz_success(base_status, base_json, base_text)

            # (a) Unauthenticated replay on protected endpoints
            if is_protected and cand["has_auth"]:
                if is_mut and not include_mutations:
                    unauth_results.append(
                        {
                            "method": cand["method"],
                            "endpoint": cand["endpoint"],
                            "url": cand["url"],
                            "normalized_path": cand["normalized_path"],
                            "is_mutation": True,
                            "baseline_status": base_status,
                            "replay_status": None,
                            "similarity": None,
                            "verdict": "mutation_candidate",
                            "error": None,
                        }
                    )
                else:
                    hdrs_unauth = _build_request_headers(
                        cand["req_headers"], cand["host"], strip_auth=True
                    )
                    st_u, txt_u, js_u, err_u = await _send_replay(client, cand, hdrs_unauth)
                    if err_u is not None:
                        verdict = "error"
                        sim = 0.0
                    else:
                        sim = _response_similarity(base_json, base_text, js_u, txt_u)
                        u_ok = _is_biz_success(st_u, js_u, txt_u)
                        u_data = _has_meaningful_data(js_u, txt_u)
                        if u_ok and u_data and (sim > 0.75 or (not baseline_ok and st_u == 200)):
                            verdict = "unauth_bypass"
                        else:
                            verdict = "blocked"

                    rec = {
                        "method": cand["method"],
                        "endpoint": cand["endpoint"],
                        "url": cand["url"],
                        "normalized_path": cand["normalized_path"],
                        "is_mutation": is_mut,
                        "baseline_status": base_status,
                        "replay_status": st_u if err_u is None else None,
                        "similarity": sim,
                        "verdict": verdict,
                        "error": err_u,
                    }
                    unauth_results.append(rec)
                    if verdict == "unauth_bypass":
                        findings.append(
                            {
                                "category": "auth_bypass",
                                "severity": "high",
                                "endpoint": cand["endpoint"],
                                "title": (
                                    f"Unauthenticated access succeeded on protected "
                                    f"endpoint {cand['endpoint']}"
                                ),
                                "evidence": rec,
                            }
                        )

            # (b) Dual-account A/B horizontal IDOR/BOLA replay
            if has_ids:
                if is_mut and not include_mutations:
                    rec_m = {
                        "method": cand["method"],
                        "endpoint": cand["endpoint"],
                        "url": cand["url"],
                        "normalized_path": cand["normalized_path"],
                        "id_params": list(cand["id_params"].keys()),
                        "is_mutation": True,
                        "baseline_status": base_status,
                        "replay_status": None,
                        "similarity": None,
                        "verdict": "mutation_candidate",
                        "error": None,
                    }
                    idor_results.append(rec_m)
                    findings.append(
                        {
                            "category": "idor",
                            "severity": "medium",
                            "endpoint": cand["endpoint"],
                            "title": (
                                f"Mutation IDOR candidate skipped by default: "
                                f"{cand['endpoint']}"
                            ),
                            "evidence": rec_m,
                        }
                    )
                elif auth_b is None:
                    idor_results.append(
                        {
                            "method": cand["method"],
                            "endpoint": cand["endpoint"],
                            "url": cand["url"],
                            "normalized_path": cand["normalized_path"],
                            "id_params": list(cand["id_params"].keys()),
                            "is_mutation": is_mut,
                            "baseline_status": base_status,
                            "replay_status": None,
                            "similarity": None,
                            "verdict": "skipped_no_auth_b",
                            "error": None,
                        }
                    )
                else:
                    hdrs_b = _build_request_headers(
                        cand["req_headers"], cand["host"], auth_ctx=auth_b
                    )
                    st_b, txt_b, js_b, err_b = await _send_replay(client, cand, hdrs_b)
                    if err_b is not None:
                        verdict = "error"
                        sim = 0.0
                    else:
                        sim = _response_similarity(base_json, base_text, js_b, txt_b)
                        b_ok = _is_biz_success(st_b, js_b, txt_b)
                        b_data = _has_meaningful_data(js_b, txt_b)
                        id_reflected = any(
                            v in txt_b for v in cand["id_params"].values() if len(v) >= 4
                        )
                        if b_ok and b_data and sim > 0.75:
                            if sim >= 0.85 or id_reflected:
                                verdict = "confirmed_idor"
                            else:
                                verdict = "suspected_idor"
                        else:
                            verdict = "blocked"

                    rec_i = {
                        "method": cand["method"],
                        "endpoint": cand["endpoint"],
                        "url": cand["url"],
                        "normalized_path": cand["normalized_path"],
                        "id_params": list(cand["id_params"].keys()),
                        "is_mutation": is_mut,
                        "baseline_status": base_status,
                        "replay_status": st_b if err_b is None else None,
                        "similarity": sim,
                        "verdict": verdict,
                        "error": err_b,
                    }
                    idor_results.append(rec_i)
                    if verdict in ("confirmed_idor", "suspected_idor"):
                        sev = "critical" if verdict == "confirmed_idor" else "high"
                        findings.append(
                            {
                                "category": "idor",
                                "severity": sev,
                                "endpoint": cand["endpoint"],
                                "title": (
                                    f"Dual-account {verdict} on {cand['endpoint']} "
                                    f"(similarity={sim:.2f})"
                                ),
                                "evidence": rec_i,
                            }
                        )
            elif is_mut and not include_mutations:
                # Non-ID mutation endpoints still tracked as mutation candidates for Burp export
                rec_mut = {
                    "method": cand["method"],
                    "endpoint": cand["endpoint"],
                    "url": cand["url"],
                    "normalized_path": cand["normalized_path"],
                    "id_params": [],
                    "is_mutation": True,
                    "baseline_status": base_status,
                    "replay_status": None,
                    "similarity": None,
                    "verdict": "mutation_candidate",
                    "error": None,
                }
                idor_results.append(rec_mut)

    summary = {
        "total_endpoints": len(candidates),
        "include_mutations": include_mutations,
        "auth_a_configured": auth_a is not None,
        "auth_b_configured": auth_b is not None,
        "unauth_tested": sum(
            1 for r in unauth_results if r["verdict"] not in ("mutation_candidate",)
        ),
        "unauth_bypass": sum(1 for r in unauth_results if r["verdict"] == "unauth_bypass"),
        "idor_tested": sum(
            1
            for r in idor_results
            if r["verdict"] not in ("mutation_candidate", "skipped_no_auth_b")
        ),
        "confirmed_idor": sum(1 for r in idor_results if r["verdict"] == "confirmed_idor"),
        "suspected_idor": sum(1 for r in idor_results if r["verdict"] == "suspected_idor"),
        "blocked": sum(1 for r in unauth_results + idor_results if r["verdict"] == "blocked"),
        "mutation_candidates": sum(
            1 for r in idor_results if r["verdict"] == "mutation_candidate"
        ),
        "errors": sum(1 for r in unauth_results + idor_results if r["verdict"] == "error"),
        "findings_count": len(findings),
    }

    report: dict[str, Any] = {
        "summary": summary,
        "unauth_results": unauth_results,
        "idor_results": idor_results,
        "findings": findings,
    }

    json_path = out_dir / "auth_matrix.json"
    md_path = out_dir / "auth_matrix.md"
    write_json(json_path, report)
    md_path.write_text(_render_markdown(report), encoding="utf-8")

    report["artifacts"] = {
        "json": str(json_path),
        "markdown": str(md_path),
    }
    return report


def run_auth_matrix(
    har_dir: Path,
    out_dir: Path,
    *,
    auth_a_path: Path | None = None,
    auth_b_path: Path | None = None,
    auth_a_headers: dict[str, str] | None = None,
    auth_b_headers: dict[str, str] | None = None,
    include_mutations: bool = False,
    target_domain: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Synchronous wrapper around `run_auth_matrix_async`."""
    coro = run_auth_matrix_async(
        har_dir,
        out_dir,
        auth_a_path=auth_a_path,
        auth_b_path=auth_b_path,
        auth_a_headers=auth_a_headers,
        auth_b_headers=auth_b_headers,
        include_mutations=include_mutations,
        target_domain=target_domain,
        timeout=timeout,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        "# Authorization & Dual-Account IDOR Matrix Report",
        "",
        f"- Endpoints analyzed: `{s['total_endpoints']}`",
        f"- Include mutations: `{s['include_mutations']}`",
        f"- Unauthenticated bypasses: `{s['unauth_bypass']}` / `{s['unauth_tested']}` tested",
        f"- Confirmed IDOR: `{s['confirmed_idor']}` | Suspected IDOR: `{s['suspected_idor']}` "
        f"| Blocked: `{s['blocked']}`",
        f"- Mutation candidates (for manual/Burp review): `{s['mutation_candidates']}`",
        "",
        "## 1. Unauthenticated Replay Results",
        "",
    ]
    if report["unauth_results"]:
        lines.append("| Endpoint | Baseline | Replay | Similarity | Verdict |")
        lines.append("| :--- | :---: | :---: | ---: | :---: |")
        for r in report["unauth_results"]:
            sim_s = f"{r['similarity']:.2f}" if r["similarity"] is not None else "-"
            rep_s = str(r["replay_status"]) if r["replay_status"] is not None else "-"
            lines.append(
                f"| `{r['endpoint']}` | {r['baseline_status']} | {rep_s} "
                f"| {sim_s} | `{r['verdict']}` |"
            )
    else:
        lines.append("- No protected endpoints queued for unauthenticated replay.")

    lines.extend(["", "## 2. Dual-Account A/B IDOR Results", ""])
    if report["idor_results"]:
        lines.append("| Endpoint | ID Params | Mutation | Baseline | Account B | Sim | Verdict |")
        lines.append("| :--- | :--- | :---: | :---: | :---: | ---: | :---: |")
        for r in report["idor_results"]:
            sim_s = f"{r['similarity']:.2f}" if r["similarity"] is not None else "-"
            rep_s = str(r["replay_status"]) if r["replay_status"] is not None else "-"
            ids = ", ".join(r["id_params"]) or "-"
            lines.append(
                f"| `{r['endpoint']}` | `{ids}` | `{r['is_mutation']}` "
                f"| {r['baseline_status']} | {rep_s} | {sim_s} | `{r['verdict']}` |"
            )
    else:
        lines.append("- No object-ID endpoints detected.")

    return "\n".join(lines).rstrip() + "\n"
