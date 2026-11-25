"""Export ReconQA Asset Library Endpoints & Correlation Chains to Postman Collection v2.1.0."""

from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any
from urllib.parse import unquote, urlsplit

from reconqa.modules.discovery.auth_resolver import redact_secrets, sanitize_url_for_storage
from reconqa.modules.hub.assertion_engine import _tokenize_jsonpath
from reconqa.modules.hub.chain_runner import (
    _contains_template,
    _is_sensitive_field_key,
    _is_sensitive_header_name,
    _is_sensitive_query_key,
    _mask_sensitive_dict_recursive,
)
from reconqa.modules.hub.coverage_diff import _group_prefix

POSTMAN_SCHEMA_V2_1 = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

_SINGLE_BRACE_PARAM_RE = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")
_DOUBLE_BRACE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")
_EMBEDDED_CRED_URL_RE = re.compile(r"(https?://)([^:/@\s]+):([^/@\s]+)@")
_INLINE_REGEX_FLAGS_RE = re.compile(r"^\(\?([ims]+)\)")
_COMMON_SHORT_SECRETS = frozenset(
    {
        "test",
        "true",
        "false",
        "null",
        "none",
        "user",
        "admin",
        "root",
        "pass",
        "demo",
        "guest",
        "token",
        "basic",
        "bearer",
    }
)
_BUILTIN_VARS = frozenset({"baseUrl", "authToken", "authCookie", "apiKey"})
_SERVER_ONLY_OPERATORS = frozenset({"sql_equals", "sql_exists", "custom"})


def _strip_fragment(path_or_url: str) -> str:
    return str(path_or_url or "").split("#", 1)[0]


def _extract_virtual_fragment(path_or_url: str) -> str:
    raw = str(path_or_url or "").strip()
    if "#" not in raw:
        return ""
    frag = raw.split("#", 1)[1].strip()
    if not frag or frag.startswith("/"):
        return ""
    return frag


def _strip_embedded_credential_urls(text: str) -> str:
    return _EMBEDDED_CRED_URL_RE.sub(r"\1[REDACTED]:[REDACTED]@", text)


def _record_collected_secret(raw: Any, collected_secrets: list[str]) -> None:
    s = str(raw or "").strip()
    if not s:
        return
    effective = s
    for pfx in ("bearer ", "basic ", "token "):
        if s.lower().startswith(pfx):
            effective = s[len(pfx) :].strip()
            break
    if len(effective) < 6 or effective.lower() in _COMMON_SHORT_SECRETS:
        return
    collected_secrets.append(effective)
    if effective != s:
        collected_secrets.append(s)


def _convert_single_brace_params(wire_path: str) -> tuple[str, list[dict[str, str]]]:
    """Convert OpenAPI `{id}` path params to Postman `:id` while preserving `{{var}}` templates."""
    path_vars: list[dict[str, str]] = []
    seen: set[str] = set()

    def _repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            path_vars.append({"key": name, "value": ""})
        return f":{name}"

    converted = _SINGLE_BRACE_PARAM_RE.sub(_repl, wire_path)
    return converted, path_vars


def _templatize_header(key: str, value: Any, collected_secrets: list[str]) -> str | None:
    """Drop pseudo/hop headers and templatize or redact sensitive header values."""
    clean_k = str(key or "").strip()
    if not clean_k or clean_k.startswith(":"):
        return None
    low = clean_k.lower()
    if low in {"content-length", "host"}:
        return None
    val_str = str(value if value is not None else "")
    if _is_sensitive_header_name(clean_k):
        if _contains_template(val_str):
            return val_str
        if val_str.strip():
            _record_collected_secret(val_str, collected_secrets)
        if low == "authorization":
            if val_str.strip().lower().startswith("basic "):
                return "Basic {{authBasic}}"
            return "Bearer {{authToken}}"
        if "cookie" in low:
            return "{{authCookie}}"
        if low in {"x-api-key", "apikey", "api-key"}:
            return "{{apiKey}}"
        if "token" in low:
            return "{{authToken}}"
        return "[REDACTED]"
    return val_str


def _sanitize_query_value(key: str, value: Any, collected_secrets: list[str]) -> str:
    val_str = str(value if value is not None else "")
    if _is_sensitive_query_key(key):
        if _contains_template(val_str):
            return val_str
        if val_str.strip():
            _record_collected_secret(val_str, collected_secrets)
            return "[REDACTED]"
    return val_str


def _sanitize_payload(payload: Any, collected_secrets: list[str]) -> Any:
    """Recursively redact sensitive fields in JSON bodies/examples while preserving `{{var}}`."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            ks = str(k)
            if _is_sensitive_field_key(ks) and not isinstance(v, (dict, list)):
                if _contains_template(v) or v is None or v == "":
                    out[ks] = v
                else:
                    _record_collected_secret(v, collected_secrets)
                    out[ks] = "[REDACTED]"
            else:
                out[ks] = _sanitize_payload(v, collected_secrets)
        return out
    if isinstance(payload, list):
        return [_sanitize_payload(item, collected_secrets) for item in payload]
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
                sanitized = _sanitize_payload(parsed, collected_secrets)
                return json.dumps(sanitized, ensure_ascii=False, indent=2)
            except Exception:
                pass
        if "://" in payload and "@" in payload:
            if stripped.startswith(("http://", "https://")) and not any(
                ch.isspace() for ch in stripped
            ):
                return _strip_embedded_credential_urls(sanitize_url_for_storage(payload))
            return _strip_embedded_credential_urls(payload)
        return payload
    return payload


def _extract_raw_query_pairs(raw_url: str, collected_secrets: list[str]) -> list[tuple[str, str]]:
    if not raw_url or "?" not in raw_url:
        return []
    no_frag = _strip_fragment(raw_url)
    _, query_part = no_frag.split("?", 1)
    if not query_part:
        return []
    pairs: list[tuple[str, str]] = []
    for chunk in query_part.split("&"):
        if not chunk:
            continue
        if "=" in chunk:
            rk, rv = chunk.split("=", 1)
        else:
            rk, rv = chunk, ""
        k = unquote(rk)
        v = rv if _contains_template(rv) else unquote(rv)
        pairs.append((k, _sanitize_query_value(k, v, collected_secrets)))
    return pairs


def _collect_userinfo_secrets(raw_url: str, collected_secrets: list[str]) -> None:
    if not raw_url:
        return
    try:
        parts = urlsplit(raw_url)
        if "@" in parts.netloc:
            userinfo = parts.netloc.rsplit("@", 1)[0]
            if userinfo:
                _record_collected_secret(userinfo, collected_secrets)
                if ":" in userinfo:
                    _, pwd = userinfo.split(":", 1)
                    if pwd:
                        _record_collected_secret(pwd, collected_secrets)
    except ValueError:
        pass


def _synthesize_schema_example(schema: Any, depth: int = 0) -> Any:
    if not isinstance(schema, dict) or depth > 3:
        return None
    if "example" in schema:
        return schema["example"]
    sch_type = str(schema.get("type") or "").lower()
    props = schema.get("properties")
    if isinstance(props, dict) and props:
        return {
            str(k): _synthesize_schema_example(v, depth + 1)
            for k, v in list(props.items())[:25]
        }
    if sch_type == "array":
        item_ex = _synthesize_schema_example(schema.get("items"), depth + 1)
        return [item_ex] if item_ex is not None else []
    if sch_type in {"integer", "number"}:
        return 0
    if sch_type == "boolean":
        return False
    return ""


def _build_postman_url(
    *,
    wire_path: str,
    query_items: list[dict[str, Any]],
    path_vars: list[dict[str, str]],
) -> dict[str, Any]:
    clean_path = wire_path if wire_path.startswith("/") else f"/{wire_path}"
    if clean_path != "/":
        clean_path = clean_path.rstrip("/") or "/"
    segments = [seg for seg in clean_path.strip("/").split("/") if seg]

    active_query = [
        f"{q['key']}={q.get('value', '')}"
        for q in query_items
        if not q.get("disabled", False)
    ]
    raw_url = "{{baseUrl}}" + (clean_path if clean_path != "/" else "/")
    if active_query:
        raw_url += "?" + "&".join(active_query)

    url_obj: dict[str, Any] = {
        "raw": raw_url,
        "host": ["{{baseUrl}}"],
        "path": segments,
    }
    if query_items:
        url_obj["query"] = query_items
    if path_vars:
        url_obj["variable"] = path_vars
    return url_obj


def _parse_numeric_expectation(raw: str) -> tuple[str, float | None]:
    """Classify numeric expectation as ('finite', val), ('nonfinite', None), or ('nonnumeric', None)."""
    try:
        val = float(str(raw or "").strip())
    except (ValueError, TypeError, OverflowError):
        return "nonnumeric", None
    if not math.isfinite(val):
        return "nonfinite", None
    return "finite", val


def _compile_assertion_js(assertion: Any, idx: int) -> tuple[list[str], bool]:
    """Compile a ScenarioAssertion into Postman `pm.test(...)` JS lines.

    Returns `(lines, needs_json_helper)`.
    """
    if hasattr(assertion, "model_dump"):
        a = assertion.model_dump()
    elif isinstance(assertion, dict):
        a = assertion
    else:
        return [], False

    op = str(a.get("operator") or "not_empty").strip()
    title = str(a.get("title") or f"Assertion {idx + 1}").strip()
    expected = str(a.get("expected") or "")
    jsonpath = str(a.get("jsonpath") or "").strip()
    safe_comment_title = title.replace("\r", " ").replace("\n", " ")

    if op in _SERVER_ONLY_OPERATORS:
        return [f"// Skipped server-only assertion ({op}): {safe_comment_title}"], False

    title_js = json.dumps(title, ensure_ascii=False)
    exp_js = json.dumps(expected, ensure_ascii=False)

    if op == "status_code":
        kind, val = _parse_numeric_expectation(expected)
        if kind == "nonfinite":
            return [f"// Skipped non-finite numeric expectation ({op}): {safe_comment_title}"], False
        if kind == "finite" and val is not None:
            try:
                code_num = int(val)
                return [
                    f"pm.test({title_js}, function () {{",
                    f"    pm.response.to.have.status({code_num});",
                    "});",
                ], False
            except (ValueError, TypeError, OverflowError):
                return [f"// Skipped non-finite numeric expectation ({op}): {safe_comment_title}"], False
        return [
            f"pm.test({title_js}, function () {{",
            f"    pm.expect(String(pm.response.code)).to.eql({exp_js});",
            "});",
        ], False

    if op == "header_equals":
        header_name = jsonpath
        header_expected = expected
        if not header_name and ":" in expected:
            h_part, v_part = expected.split(":", 1)
            header_name = h_part.strip()
            header_expected = v_part.strip()
        h_js = json.dumps(header_name or "content-type", ensure_ascii=False)
        hv_js = json.dumps(header_expected, ensure_ascii=False)
        return [
            f"pm.test({title_js}, function () {{",
            f"    pm.expect(String(pm.response.headers.get({h_js}) || \"\")).to.eql({hv_js});",
            "});",
        ], False

    if op == "latency_lt":
        kind, val = _parse_numeric_expectation(expected)
        if kind == "nonfinite":
            return [f"// Skipped non-finite numeric expectation ({op}): {safe_comment_title}"], False
        limit_ms = val if (kind == "finite" and val is not None) else 2000.0
        return [
            f"pm.test({title_js}, function () {{",
            f"    pm.expect(pm.response.responseTime).to.be.below({limit_ms});",
            "});",
        ], False

    if op == "var_equals":
        var_name = jsonpath.lstrip("$.").strip() or "var"
        var_js = json.dumps(var_name, ensure_ascii=False)
        return [
            f"pm.test({title_js}, function () {{",
            f"    pm.expect(String(pm.collectionVariables.get({var_js}))).to.eql({exp_js});",
            "});",
        ], False

    tokens = _tokenize_jsonpath(jsonpath)
    tokens_js = json.dumps(tokens, ensure_ascii=False)
    val_expr = f"_rqGet(_rqJson, {tokens_js})"

    if op == "json_schema":
        try:
            parsed_schema = json.loads(expected)
            if isinstance(parsed_schema, dict):
                schema_js = json.dumps(parsed_schema, ensure_ascii=False)
                return [
                    f"pm.test({title_js}, function () {{",
                    f"    pm.response.to.have.jsonSchema({schema_js});",
                    "});",
                ], False
        except Exception:
            pass
        return [
            f"pm.test({title_js}, function () {{",
            f"    pm.expect({val_expr}).to.not.be.undefined;",
            "});",
        ], True

    body_lines: list[str] = [
        f"pm.test({title_js}, function () {{",
        f"    var _val = {val_expr};",
    ]
    if op == "not_empty":
        body_lines.append("    pm.expect(_val).to.not.be.undefined;")
        body_lines.append("    pm.expect(_val).to.not.be.null;")
        body_lines.append('    if (typeof _val === "string" || Array.isArray(_val)) { pm.expect(_val.length).to.be.above(0); }')
    elif op == "is_empty":
        body_lines.append(
            '    pm.expect(_val === undefined || _val === null || _val === "" || '
            '(Array.isArray(_val) && _val.length === 0) || '
            '(typeof _val === "object" && !Array.isArray(_val) && Object.keys(_val).length === 0)).to.be.true;'
        )
    elif op == "equals":
        body_lines.append(f"    pm.expect(String(_val)).to.eql({exp_js});")
    elif op == "not_equals":
        body_lines.append(f"    pm.expect(String(_val)).to.not.eql({exp_js});")
    elif op in {"gt", "gte", "lt", "lte"}:
        cmp_map = {
            "gt": "above",
            "gte": "at.least",
            "lt": "below",
            "lte": "at.most",
        }
        kind, num_val = _parse_numeric_expectation(expected)
        if kind == "nonfinite":
            return [f"// Skipped non-finite numeric expectation ({op}): {safe_comment_title}"], False
        if kind == "finite" and num_val is not None:
            body_lines.append(f"    pm.expect(Number(_val)).to.be.{cmp_map[op]}({num_val});")
        else:
            body_lines.append(f"    pm.expect(Number(_val)).to.be.{cmp_map[op]}(Number({exp_js}));")
    elif op == "contains":
        body_lines.append(f"    pm.expect(String(_val)).to.include({exp_js});")
    elif op == "not_contains":
        body_lines.append(f"    pm.expect(String(_val)).to.not.include({exp_js});")
    elif op == "matches":
        regex_pat = expected
        js_flags_list: list[str] = []
        while True:
            m_flag = _INLINE_REGEX_FLAGS_RE.match(regex_pat)
            if not m_flag:
                break
            for ch in m_flag.group(1):
                if ch not in js_flags_list:
                    js_flags_list.append(ch)
            regex_pat = regex_pat[m_flag.end() :]
        pat_js = json.dumps(regex_pat, ensure_ascii=False)
        if js_flags_list:
            flags_js = json.dumps("".join(js_flags_list), ensure_ascii=False)
            reg_expr = f"new RegExp({pat_js}, {flags_js})"
        else:
            reg_expr = f"new RegExp({pat_js})"
        body_lines.append("    var _re = null;")
        body_lines.append(f"    try {{ _re = {reg_expr}; }} catch (e) {{ _re = null; }}")
        body_lines.append("    pm.expect(_re).to.not.be.null;")
        body_lines.append("    if (_re) { pm.expect(String(_val)).to.match(_re); }")
    elif op == "array_contains":
        body_lines.append("    pm.expect(Array.isArray(_val)).to.be.true;")
        body_lines.append(f"    pm.expect(_val.map(String)).to.include({exp_js});")
    elif op == "length_eq":
        kind, val = _parse_numeric_expectation(expected)
        if kind == "nonfinite":
            return [f"// Skipped non-finite numeric expectation ({op}): {safe_comment_title}"], False
        if kind == "finite" and val is not None:
            try:
                len_val = int(val)
                body_lines.append(f"    pm.expect(_val.length).to.eql({len_val});")
            except (ValueError, TypeError, OverflowError):
                return [f"// Skipped non-finite numeric expectation ({op}): {safe_comment_title}"], False
        else:
            body_lines.append(f"    pm.expect(_val.length).to.eql(Number({exp_js}));")
    elif op == "length_gt":
        kind, val = _parse_numeric_expectation(expected)
        if kind == "nonfinite":
            return [f"// Skipped non-finite numeric expectation ({op}): {safe_comment_title}"], False
        if kind == "finite" and val is not None:
            try:
                len_val = int(val)
                body_lines.append(f"    pm.expect(_val.length).to.be.above({len_val});")
            except (ValueError, TypeError, OverflowError):
                return [f"// Skipped non-finite numeric expectation ({op}): {safe_comment_title}"], False
        else:
            body_lines.append(f"    pm.expect(_val.length).to.be.above(Number({exp_js}));")
    elif op == "type_is":
        want_t = expected.strip().lower()
        if want_t == "array":
            body_lines.append("    pm.expect(Array.isArray(_val)).to.be.true;")
        elif want_t == "null":
            body_lines.append("    pm.expect(_val).to.be.null;")
        elif want_t in {"int", "integer", "float", "number"}:
            body_lines.append('    pm.expect(typeof _val).to.eql("number");')
        elif want_t in {"bool", "boolean"}:
            body_lines.append('    pm.expect(typeof _val).to.eql("boolean");')
        elif want_t in {"dict", "object"}:
            body_lines.append('    pm.expect(typeof _val === "object" && _val !== null && !Array.isArray(_val)).to.be.true;')
        else:
            body_lines.append(f"    pm.expect(typeof _val).to.eql({json.dumps(want_t or 'string')});")
    else:
        body_lines.append("    pm.expect(_val).to.not.be.undefined;")

    body_lines.append("});")
    return body_lines, True


def _build_test_event(
    *,
    expected_statuses: list[int] | None = None,
    extracts: dict[str, str] | None = None,
    assertions: list[Any] | None = None,
) -> list[dict[str, Any]]:
    script_blocks: list[str] = []
    needs_json_helper = bool(extracts)

    has_status_assertion = False
    compiled_assertions: list[list[str]] = []
    for idx, assertion in enumerate(assertions or []):
        op = (
            assertion.operator
            if hasattr(assertion, "operator")
            else (assertion.get("operator") if isinstance(assertion, dict) else "")
        )
        if str(op) == "status_code":
            has_status_assertion = True
        a_lines, a_needs_json = _compile_assertion_js(assertion, idx)
        if a_lines:
            compiled_assertions.append(a_lines)
        if a_needs_json:
            needs_json_helper = True

    if needs_json_helper:
        script_blocks.extend(
            [
                "function _rqGet(obj, tokens) {",
                "    var cur = obj;",
                "    for (var i = 0; i < tokens.length; i++) {",
                "        if (cur === null || cur === undefined) return undefined;",
                "        var tok = tokens[i];",
                '        if (tok === "*") {',
                "            if (!Array.isArray(cur)) return undefined;",
                "            var rest = tokens.slice(i + 1);",
                "            if (rest.length === 0) return cur;",
                "            var acc = [];",
                "            for (var j = 0; j < cur.length; j++) {",
                "                var v = _rqGet(cur[j], rest);",
                "                if (v !== undefined) acc.push(v);",
                "            }",
                "            return acc;",
                "        }",
                "        cur = cur[tok];",
                "    }",
                "    return cur;",
                "}",
                "var _rqJson = null;",
                "try { _rqJson = pm.response.json(); } catch (e) { _rqJson = null; }",
            ]
        )

    if not has_status_assertion and expected_statuses:
        clean_codes = sorted({int(c) for c in expected_statuses if isinstance(c, int) and c > 0})
        if len(clean_codes) == 1:
            code = clean_codes[0]
            script_blocks.extend(
                [
                    f'pm.test("Status code is {code}", function () {{',
                    f"    pm.response.to.have.status({code});",
                    "});",
                ]
            )
        elif len(clean_codes) > 1:
            codes_js = json.dumps(clean_codes)
            script_blocks.extend(
                [
                    'pm.test("Status code matches recorded codes", function () {',
                    f"    pm.expect({codes_js}).to.include(pm.response.code);",
                    "});",
                ]
            )

    if extracts:
        for idx, (var_name, jp) in enumerate(extracts.items()):
            tokens_js = json.dumps(_tokenize_jsonpath(str(jp)), ensure_ascii=False)
            var_js = json.dumps(str(var_name), ensure_ascii=False)
            script_blocks.extend(
                [
                    f"var _ext_{idx} = _rqGet(_rqJson, {tokens_js});",
                    f"if (_ext_{idx} !== undefined && _ext_{idx} !== null) {{",
                    f"    pm.collectionVariables.set({var_js}, _ext_{idx});",
                    "}",
                ]
            )

    for block in compiled_assertions:
        script_blocks.extend(block)

    if not script_blocks:
        return []
    return [
        {
            "listen": "test",
            "script": {
                "type": "text/javascript",
                "exec": script_blocks,
            },
        }
    ]


def _collect_template_vars(obj: Any, out: set[str]) -> None:
    if isinstance(obj, str):
        for m in _DOUBLE_BRACE_VAR_RE.finditer(obj):
            name = m.group(1).strip()
            if name and not name.startswith("$"):
                out.add(name)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_template_vars(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_template_vars(item, out)


def _redact_non_structural_text(text: str, secrets: list[str]) -> str:
    if _DOUBLE_BRACE_VAR_RE.fullmatch(text.strip()):
        return text
    cleaned = _strip_embedded_credential_urls(text)
    if secrets:
        cleaned = redact_secrets(cleaned, secrets)
    return cleaned


def _redact_postman_item(item: dict[str, Any], secrets: list[str]) -> dict[str, Any]:
    """Redact non-JS, non-structural values inside a Postman folder or request item."""
    if isinstance(item.get("item"), list):
        item["item"] = [
            _redact_postman_item(child, secrets) if isinstance(child, dict) else child
            for child in item["item"]
        ]
        return item

    if isinstance(item.get("name"), str):
        item["name"] = _strip_embedded_credential_urls(item["name"])

    req = item.get("request")
    if isinstance(req, dict):
        if isinstance(req.get("header"), list):
            for hdr in req["header"]:
                if isinstance(hdr, dict) and isinstance(hdr.get("value"), str):
                    hdr["value"] = _redact_non_structural_text(hdr["value"], secrets)

        url_obj = req.get("url")
        if isinstance(url_obj, dict) and isinstance(url_obj.get("query"), list):
            for q in url_obj["query"]:
                if isinstance(q, dict) and isinstance(q.get("value"), str):
                    q["value"] = _redact_non_structural_text(q["value"], secrets)
            if isinstance(url_obj.get("raw"), str):
                base_part = url_obj["raw"].split("?", 1)[0]
                active_q = [
                    f"{q.get('key', '')}={q.get('value', '')}"
                    for q in url_obj["query"]
                    if isinstance(q, dict) and not q.get("disabled", False)
                ]
                url_obj["raw"] = f"{base_part}?{'&'.join(active_q)}" if active_q else base_part

        body_obj = req.get("body")
        if isinstance(body_obj, dict):
            if isinstance(body_obj.get("raw"), str):
                body_obj["raw"] = _redact_non_structural_text(body_obj["raw"], secrets)
            for form_key in ("urlencoded", "formdata"):
                if isinstance(body_obj.get(form_key), list):
                    for entry in body_obj[form_key]:
                        if isinstance(entry, dict) and isinstance(entry.get("value"), str):
                            entry["value"] = _redact_non_structural_text(entry["value"], secrets)

    if isinstance(item.get("response"), list):
        for resp in item["response"]:
            if not isinstance(resp, dict):
                continue
            if isinstance(resp.get("name"), str):
                resp["name"] = _strip_embedded_credential_urls(resp["name"])
            if isinstance(resp.get("body"), str):
                resp["body"] = _redact_non_structural_text(resp["body"], secrets)
            if isinstance(resp.get("header"), list):
                for hdr in resp["header"]:
                    if isinstance(hdr, dict) and isinstance(hdr.get("value"), str):
                        hdr["value"] = _redact_non_structural_text(hdr["value"], secrets)

    return item


def _final_redact(collection: dict[str, Any], secrets: list[str]) -> dict[str, Any]:
    """Apply scoped redaction to non-JS/non-structural Postman values while preserving templates."""
    info = collection.get("info")
    if isinstance(info, dict) and isinstance(info.get("description"), str):
        info["description"] = _strip_embedded_credential_urls(info["description"])
    if isinstance(collection.get("item"), list):
        collection["item"] = [
            _redact_postman_item(folder, secrets) if isinstance(folder, dict) else folder
            for folder in collection["item"]
        ]
    return collection


def export_postman_collection(
    *,
    project_id: str,
    project_name: str,
    inventory: dict[str, Any],
    openapi_doc: dict[str, Any] | None = None,
    chains: list[dict[str, Any]] | None = None,
    scenario_assertions: list[dict[str, Any]] | None = None,
    base_url: str = "",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build a valid Postman Collection v2.1.0 dict from Project Asset Inventory and Chains."""
    from reconqa.server.routers.chains import (
        _apply_uses_to_step,
        _match_step_scenario_assertions,
    )

    collected_secrets: list[str] = []
    discovered_vars: set[str] = set()

    # Resolve baseUrl safely
    candidate_base = (base_url or "").strip()
    if not candidate_base and isinstance(openapi_doc, dict):
        servers = openapi_doc.get("servers") or []
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            candidate_base = str(servers[0].get("url") or "").strip()
    if not candidate_base:
        candidate_base = "https://api.example.com"
    _collect_userinfo_secrets(candidate_base, collected_secrets)
    resolved_base_url = sanitize_url_for_storage(candidate_base).rstrip("/") or "https://api.example.com"

    # Folder 1: Asset Library Endpoints
    endpoints = list((inventory or {}).get("endpoints") or [])
    grouped_endpoints: dict[str, list[dict[str, Any]]] = {}

    for ep in endpoints:
        if not isinstance(ep, dict):
            continue
        method = str(ep.get("method") or "GET").upper().strip()
        ep_path = str(ep.get("path") or "/")
        summary = str(ep.get("summary") or "").strip()
        wire_path = _strip_fragment(ep_path) or "/"
        pm_path, path_vars = _convert_single_brace_params(wire_path)

        # Headers
        header_map: dict[str, tuple[str, str]] = {}
        for hk, hv in (ep.get("custom_headers") or {}).items():
            clean_val = _templatize_header(str(hk), hv, collected_secrets)
            if clean_val is not None:
                header_map[str(hk).lower()] = (str(hk), clean_val)
        for p in ep.get("parameters") or []:
            if isinstance(p, dict) and str(p.get("in") or "").lower() == "header" and p.get("name"):
                hk = str(p["name"])
                ex_val = p.get("example") or ((p.get("schema") or {}).get("default")) or ""
                clean_val = _templatize_header(hk, ex_val, collected_secrets)
                if clean_val is not None and hk.lower() not in header_map:
                    header_map[hk.lower()] = (hk, clean_val)

        # Query parameters
        query_items: list[dict[str, Any]] = []
        seen_q: set[str] = set()
        for p in ep.get("parameters") or []:
            if isinstance(p, dict) and str(p.get("in") or "").lower() == "query" and p.get("name"):
                qk = str(p["name"])
                if qk in seen_q:
                    continue
                seen_q.add(qk)
                raw_qv = p.get("example") if p.get("example") is not None else (
                    (p.get("schema") or {}).get("default")
                )
                safe_qv = _sanitize_query_value(qk, raw_qv, collected_secrets)
                query_items.append(
                    {
                        "key": qk,
                        "value": safe_qv,
                        "disabled": not bool(p.get("required", False)),
                    }
                )

        # Body
        req_body_obj: dict[str, Any] | None = None
        raw_payload = ep.get("request_example")
        if raw_payload is None and isinstance(ep.get("request_schema"), dict):
            raw_payload = _synthesize_schema_example(ep["request_schema"])
        if raw_payload is not None and method not in {"GET", "HEAD"}:
            sanitized_body = _sanitize_payload(raw_payload, collected_secrets)
            if isinstance(sanitized_body, (dict, list)):
                raw_text = json.dumps(sanitized_body, ensure_ascii=False, indent=2)
            else:
                raw_text = str(sanitized_body)
            req_body_obj = {
                "mode": "raw",
                "raw": raw_text,
                "options": {"raw": {"language": "json"}},
            }
            if "content-type" not in header_map:
                header_map["content-type"] = ("Content-Type", "application/json")

        headers_list = [{"key": orig_k, "value": v, "type": "text"} for orig_k, v in header_map.values()]
        url_obj = _build_postman_url(
            wire_path=pm_path,
            query_items=query_items,
            path_vars=path_vars,
        )

        matched_assertions = _match_step_scenario_assertions(
            method, ep_path, "", scenario_assertions or []
        )
        events = _build_test_event(
            expected_statuses=list(ep.get("status_codes") or []),
            assertions=matched_assertions,
        )

        item_name = f"{method} {ep_path}"
        if summary and summary != item_name:
            item_name = f"{item_name} — {summary[:80]}"

        request_obj: dict[str, Any] = {
            "method": method,
            "header": headers_list,
            "url": url_obj,
        }
        if req_body_obj is not None:
            request_obj["body"] = req_body_obj

        responses: list[dict[str, Any]] = []
        if ep.get("response_example") is not None:
            sanitized_resp = _sanitize_payload(ep["response_example"], collected_secrets)
            resp_body_str = (
                json.dumps(sanitized_resp, ensure_ascii=False, indent=2)
                if isinstance(sanitized_resp, (dict, list))
                else str(sanitized_resp)
            )
            codes = [int(c) for c in (ep.get("status_codes") or []) if isinstance(c, int)]
            code = codes[0] if codes else 200
            responses.append(
                {
                    "name": "Recorded Example",
                    "status": "OK" if 200 <= code < 300 else f"HTTP {code}",
                    "code": code,
                    "body": resp_body_str,
                }
            )

        pm_item: dict[str, Any] = {
            "name": item_name,
            "request": request_obj,
            "response": responses,
        }
        if events:
            pm_item["event"] = events

        _collect_template_vars(pm_item, discovered_vars)

        tags = [str(t).strip() for t in (ep.get("tags") or []) if str(t).strip()]
        group_name = tags[0] if tags else _group_prefix(wire_path)
        grouped_endpoints.setdefault(group_name, []).append(pm_item)

    asset_subfolders = [
        {"name": grp_name, "item": grouped_endpoints[grp_name]}
        for grp_name in sorted(grouped_endpoints.keys())
    ]

    # Folder 2: Correlation Chains
    chain_subfolders: list[dict[str, Any]] = []
    for c_idx, ch in enumerate(chains or []):
        if not isinstance(ch, dict):
            continue
        ch_name = str(ch.get("name") or f"Chain {c_idx + 1}")
        raw_steps = [st for st in (ch.get("steps") or []) if isinstance(st, dict)]
        cleanup_step = ch.get("cleanup_step")
        step_entries: list[tuple[dict[str, Any], bool]] = [(st, False) for st in raw_steps]
        if isinstance(cleanup_step, dict):
            step_entries.append((cleanup_step, True))

        chain_items: list[dict[str, Any]] = []
        for st_dict, is_cleanup in step_entries:
            _collect_userinfo_secrets(str(st_dict.get("raw_url") or ""), collected_secrets)
            virtual_frag = (
                _extract_virtual_fragment(str(st_dict.get("path") or ""))
                or _extract_virtual_fragment(str(st_dict.get("raw_url") or ""))
            )
            explicit_step_name = str(st_dict.get("name") or "").strip()
            step_in = _apply_uses_to_step(st_dict)

            raw_step_path = str(step_in.path or "/")
            virtual_step_path = (
                raw_step_path
                if (not virtual_frag or "#" in raw_step_path)
                else f"{_strip_fragment(raw_step_path)}#{virtual_frag}"
            )
            wire_step_path = _strip_fragment(virtual_step_path) or "/"
            pm_step_path, step_path_vars = _convert_single_brace_params(wire_step_path)

            # Query params (explicit step_in.query_params + fallback from raw_url)
            q_pairs: dict[str, str] = {}
            for qk, qv in (step_in.query_params or {}).items():
                q_pairs[str(qk)] = _sanitize_query_value(str(qk), qv, collected_secrets)
            if not q_pairs and step_in.raw_url:
                for qk, qv in _extract_raw_query_pairs(step_in.raw_url, collected_secrets):
                    if qk not in q_pairs:
                        q_pairs[qk] = qv
            query_items = [{"key": k, "value": v} for k, v in q_pairs.items()]

            # Headers
            header_map = {}
            for hk, hv in (step_in.request_headers or {}).items():
                clean_val = _templatize_header(str(hk), hv, collected_secrets)
                if clean_val is not None:
                    header_map[str(hk).lower()] = (str(hk), clean_val)

            # Body
            req_body_obj = None
            if step_in.request_body is not None and step_in.method not in {"GET", "HEAD"}:
                sanitized_body = _mask_sensitive_dict_recursive(
                    _sanitize_payload(step_in.request_body, collected_secrets),
                    [],
                )
                if isinstance(sanitized_body, (dict, list)):
                    raw_text = json.dumps(sanitized_body, ensure_ascii=False, indent=2)
                else:
                    raw_text = str(sanitized_body)
                req_body_obj = {
                    "mode": "raw",
                    "raw": raw_text,
                    "options": {"raw": {"language": "json"}},
                }
                if "content-type" not in header_map:
                    header_map["content-type"] = ("Content-Type", "application/json")

            headers_list = [
                {"key": orig_k, "value": v, "type": "text"}
                for orig_k, v in header_map.values()
            ]
            url_obj = _build_postman_url(
                wire_path=pm_step_path,
                query_items=query_items,
                path_vars=step_path_vars,
            )

            matched_assertions = _match_step_scenario_assertions(
                step_in.method,
                virtual_step_path,
                step_in.raw_url,
                scenario_assertions or [],
            )
            combined_assertions = list(step_in.assertions or [])
            seen_a_ids = {a.id for a in combined_assertions}
            for ma in matched_assertions:
                if ma.id not in seen_a_ids:
                    combined_assertions.append(ma)
                    seen_a_ids.add(ma.id)

            for ext_key in (step_in.extracts or {}).keys():
                if ext_key:
                    discovered_vars.add(str(ext_key))

            events = _build_test_event(
                expected_statuses=[step_in.expected_status]
                if step_in.expected_status is not None
                else None,
                extracts=step_in.extracts,
                assertions=combined_assertions,
            )

            step_item_name = explicit_step_name or f"{step_in.method} {virtual_step_path}"
            if is_cleanup and "cleanup" not in step_item_name.lower():
                step_item_name = f"{step_item_name} (Cleanup)"

            req_obj: dict[str, Any] = {
                "method": step_in.method,
                "header": headers_list,
                "url": url_obj,
            }
            if req_body_obj is not None:
                req_obj["body"] = req_body_obj

            step_item: dict[str, Any] = {
                "name": step_item_name,
                "request": req_obj,
                "response": [],
            }
            if events:
                step_item["event"] = events

            _collect_template_vars(step_item, discovered_vars)
            chain_items.append(step_item)

        chain_subfolders.append({"name": ch_name, "item": chain_items})

    # Variables
    variables: list[dict[str, str]] = [
        {"key": "baseUrl", "value": resolved_base_url, "type": "string"},
        {"key": "authToken", "value": "", "type": "string"},
        {"key": "authCookie", "value": "", "type": "string"},
        {"key": "apiKey", "value": "", "type": "string"},
    ]
    for var_name in sorted(discovered_vars - _BUILTIN_VARS):
        variables.append({"key": var_name, "value": "", "type": "string"})

    col_name = f"{project_name} — ReconQA Collection"
    if run_id:
        col_name = f"{col_name} (Run {run_id[:8]})"

    collection: dict[str, Any] = {
        "info": {
            "_postman_id": str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"reconqa:postman:{project_id}:{run_id or 'all'}")
            ),
            "name": col_name,
            "description": (
                f"Exported from ReconQA project '{project_name}' (ID: {project_id}, "
                f"run: {run_id or 'all'}). Includes {len(endpoints)} asset endpoints and "
                f"{len(chain_subfolders)} correlation chains."
            ),
            "schema": POSTMAN_SCHEMA_V2_1,
        },
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{authToken}}", "type": "string"}],
        },
        "variable": variables,
        "item": [
            {"name": "Asset Library Endpoints", "item": asset_subfolders},
            {"name": "Correlation Chains", "item": chain_subfolders},
        ],
    }

    return _final_redact(collection, collected_secrets)
