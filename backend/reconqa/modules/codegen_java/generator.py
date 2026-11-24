"""Java TestNG + RestAssured + Allure Maven project generator."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

import yaml
from jinja2 import Environment, FileSystemLoader

from reconqa.modules.codegen_java.schema_gen import (
    HTTP_METHODS,
    build_payload_rel_path,
    extract_response_schemas,
    sanitize_filename,
    strip_var_braces,
    write_parameterized_payload,
)
from reconqa.utils.io import ensure_dir, read_json, slugify, write_json

TEMPLATES_DIR = Path(__file__).parent / "templates"

JAVA_KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
    "class", "const", "continue", "default", "do", "double", "else", "enum",
    "extends", "false", "final", "finally", "float", "for", "goto", "if",
    "implements", "import", "instanceof", "int", "interface", "long", "native",
    "new", "null", "package", "private", "protected", "public", "record",
    "return", "short", "static", "strictfp", "super", "switch", "synchronized",
    "this", "throw", "throws", "transient", "true", "try", "var", "void",
    "volatile", "while", "yield", "sealed", "permits", "_",
}

AUTH_HEADER_NAMES = {
    "authorization",
    "cookie",
    "x-auth-token",
    "x-api-key",
    "proxy-authorization",
}

STANDARD_IGNORED_HEADERS = {
    "host",
    "content-length",
    "content-type",
    "accept",
    "accept-encoding",
    "accept-language",
    "user-agent",
    "connection",
    "origin",
    "referer",
    "cache-control",
    "pragma",
    "upgrade-insecure-requests",
    "dnt",
    "x-b3-traceid",
    "x-b3-spanid",
    "set-cookie",
}

CSV_HEADERS = [
    "caseId",
    "scenario",
    "method",
    "path",
    "queryJsonPath",
    "headersJsonPath",
    "payloadJsonPath",
    "expectedStatus",
    "bizCodeField",
    "bizCodeValue",
    "schemaPath",
    "maxLatencyMs",
]

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_PATH_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_-]*)\}")
_GROOVY_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BRACKET_KEY_RE = re.compile(r"""\[(['"])(.*?)\1\]""")


def escape_java_string(value: Any) -> str:
    """Escape a Python value for safe embedding inside a Java double-quoted string literal."""
    text = "" if value is None else str(value)
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def sanitize_java_identifier(raw: str, *, pascal: bool = False, default: str = "item") -> str:
    """Convert an arbitrary string into a valid, keyword-safe Java identifier."""
    tokens = [tok for tok in re.split(r"[^A-Za-z0-9]+", raw or "") if tok]
    if not tokens:
        tokens = [default]

    if pascal:
        ident = "".join(tok[0].upper() + tok[1:] for tok in tokens)
        if not ident or ident[0].isdigit():
            ident = "T" + ident
    else:
        first = tokens[0]
        rest = [tok[0].upper() + tok[1:] for tok in tokens[1:]]
        ident = first[0].lower() + first[1:] + "".join(rest)
        if not ident or ident[0].isdigit():
            ident = "v_" + ident

    if ident in JAVA_KEYWORDS:
        ident = f"{ident}Val"
    return ident


def sanitize_package_name(package_name: str) -> str:
    """Sanitize a dotted Java package name."""
    parts = [p.strip() for p in (package_name or "").split(".") if p.strip()]
    if not parts:
        return "com.reconqa.generated"
    cleaned: list[str] = []
    for part in parts:
        seg = re.sub(r"[^A-Za-z0-9_]", "_", part).lower()
        if not seg or seg[0].isdigit():
            seg = f"pkg_{seg}"
        if seg in JAVA_KEYWORDS:
            seg = f"{seg}_pkg"
        cleaned.append(seg)
    return ".".join(cleaned)


def jsonpath_to_gpath(jsonpath: str) -> str:
    """Convert standard JSONPath (e.g., $.data.list[0].id) to RestAssured GPath (data.list[0].id)."""
    expr = (jsonpath or "").strip()
    if not expr:
        return ""

    if expr.startswith("$."):
        expr = expr[2:]
    elif expr.startswith("$"):
        expr = expr[1:]

    # Convert bracketed string keys like ['item-id'] or ["item-id"] to dot notation
    def _bracket_repl(match: re.Match[str]) -> str:
        key = match.group(2)
        if _GROOVY_IDENT_RE.match(key):
            return f".{key}"
        escaped = key.replace("'", "\\'")
        return f".'{escaped}'"

    expr = _BRACKET_KEY_RE.sub(_bracket_repl, expr).removeprefix(".")

    # Quote individual hyphenated or non-identifier dot segments for Groovy GPath safety
    parts: list[str] = []
    in_quotes = False
    buf: list[str] = []
    for ch in expr:
        if ch == "'":
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "." and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))

    formatted: list[str] = []
    for seg in parts:
        if not seg:
            continue
        if seg.startswith(("'", "[")):
            formatted.append(seg)
            continue
        bracket_idx = seg.find("[")
        if bracket_idx > 0:
            head, tail = seg[:bracket_idx], seg[bracket_idx:]
        else:
            head, tail = seg, ""
        if _GROOVY_IDENT_RE.match(head):
            formatted.append(f"{head}{tail}")
        else:
            escaped = head.replace("'", "\\'")
            formatted.append(f"'{escaped}'{tail}")

    return ".".join(formatted)


def _get_attr_or_key(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _load_openapi(openapi_path: Path) -> dict[str, Any]:
    raw_text = openapi_path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw_text)
    if not isinstance(loaded, dict):
        raise TypeError(f"Invalid OpenAPI specification at {openapi_path}: expected mapping")
    return loaded


def _is_redacted_value(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    upper = text.upper()
    return "REDACTED" in upper or "***" in text


def _is_safe_custom_header(name: str, value: Any) -> bool:
    lower = name.strip().lower()
    if not lower:
        return False
    if lower in AUTH_HEADER_NAMES or lower in STANDARD_IGNORED_HEADERS:
        return False
    if lower.startswith(("sec-", ":")):
        return False
    return not _is_redacted_value(value)


def _extract_param_example(param: dict[str, Any]) -> Any:
    if "example" in param and param["example"] is not None:
        return param["example"]
    examples = param.get("examples")
    if isinstance(examples, dict):
        for ex_obj in examples.values():
            if isinstance(ex_obj, dict) and ex_obj.get("value") is not None:
                return ex_obj["value"]
    schema = param.get("schema")
    if isinstance(schema, dict):
        if schema.get("example") is not None:
            return schema["example"]
        if schema.get("default") is not None:
            return schema["default"]
        enum_vals = schema.get("enum")
        if isinstance(enum_vals, list) and enum_vals:
            return enum_vals[0]
    return None


def _all_operation_params(operation: dict[str, Any], path_item: dict[str, Any]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for source in (path_item.get("parameters"), operation.get("parameters")):
        if isinstance(source, list):
            for p in source:
                if isinstance(p, dict):
                    combined.append(p)
    return combined


def _resolve_concrete_path(
    path_template: str,
    operation: dict[str, Any],
    path_item: dict[str, Any],
) -> str:
    params_by_name: dict[str, dict[str, Any]] = {}
    for p in _all_operation_params(operation, path_item):
        if p.get("in") == "path" and isinstance(p.get("name"), str):
            params_by_name[p["name"]] = p

    def _repl(match: re.Match[str]) -> str:
        pname = match.group(1)
        pdef = params_by_name.get(pname)
        if pdef is not None:
            example = _extract_param_example(pdef)
            if example is not None and str(example).strip():
                return str(example).strip()
            schema = pdef.get("schema") or {}
            if schema.get("type") in ("integer", "number"):
                return "1"
        return "sample"

    return _PATH_PARAM_RE.sub(_repl, path_template)


def _extract_query_examples(
    operation: dict[str, Any],
    path_item: dict[str, Any],
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    for p in _all_operation_params(operation, path_item):
        if p.get("in") == "query" and isinstance(p.get("name"), str):
            example = _extract_param_example(p)
            if example is not None:
                query[p["name"]] = example
    return query


def _extract_safe_headers(
    operation: dict[str, Any],
    path_item: dict[str, Any],
) -> dict[str, str]:
    headers: dict[str, str] = {}
    custom = operation.get("x-custom-headers")
    if isinstance(custom, dict):
        for k, v in custom.items():
            if _is_safe_custom_header(str(k), v):
                headers[str(k)] = str(v)
    for p in _all_operation_params(operation, path_item):
        if p.get("in") == "header" and isinstance(p.get("name"), str):
            val = _extract_param_example(p)
            if _is_safe_custom_header(p["name"], val):
                headers[p["name"]] = str(val)
    return headers


def _synthesize_example_from_schema(schema: dict[str, Any], depth: int = 0) -> Any:
    if depth > 4:
        return {}
    if "example" in schema and schema["example"] is not None:
        return schema["example"]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((t for t in schema_type if t != "null"), "string")
    if schema_type == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if isinstance(props, dict):
            return {
                k: _synthesize_example_from_schema(v, depth + 1)
                for k, v in props.items()
                if isinstance(v, dict)
            }
        return {}
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            return [_synthesize_example_from_schema(items, depth + 1)]
        return []
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "boolean":
        return True
    return "sample"


def _extract_request_body_example(operation: dict[str, Any]) -> Any:
    req_body = operation.get("requestBody")
    if not isinstance(req_body, dict):
        return None
    content = req_body.get("content")
    if not isinstance(content, dict):
        return None
    for media_type, media_obj in content.items():
        if "json" not in str(media_type).lower() or not isinstance(media_obj, dict):
            continue
        if "example" in media_obj and media_obj["example"] is not None:
            return media_obj["example"]
        examples = media_obj.get("examples")
        if isinstance(examples, dict):
            for ex in examples.values():
                if isinstance(ex, dict) and ex.get("value") is not None:
                    return ex["value"]
        schema = media_obj.get("schema")
        if isinstance(schema, dict):
            return _synthesize_example_from_schema(schema)
    return None


def _extract_expected_status(operation: dict[str, Any]) -> int:
    responses = operation.get("responses")
    if isinstance(responses, dict):
        for code in sorted(
            (str(c) for c in responses if str(c).startswith("2") and str(c).isdigit()),
            key=lambda c: (0 if c == "200" else 1, int(c)),
        ):
            return int(code)
    return 200


def _extract_biz_success(
    operation: dict[str, Any] | None,
    global_biz_rules: Any = None,
    *,
    path: str = "",
    project_config: Any = None,
) -> tuple[str, str]:
    if isinstance(operation, dict):
        biz = operation.get("x-biz-success")
        if isinstance(biz, dict) and biz.get("field") is not None and biz.get("value") is not None:
            return str(biz["field"]), str(biz["value"])

    if project_config is not None and hasattr(project_config, "match_biz_success_rule"):
        matched = project_config.match_biz_success_rule(path)
        if isinstance(matched, dict) and matched.get("field") is not None and matched.get("value") is not None:
            return str(matched["field"]), str(matched["value"])

    if isinstance(global_biz_rules, list) and global_biz_rules:
        first_rule = global_biz_rules[0]
        field = _get_attr_or_key(first_rule, "field")
        value = _get_attr_or_key(first_rule, "value")
        if field is not None and value is not None and str(field).strip():
            return str(field), str(value)
    elif global_biz_rules is not None:
        field = _get_attr_or_key(global_biz_rules, "field")
        value = _get_attr_or_key(global_biz_rules, "value")
        if field is not None and value is not None and str(field).strip():
            return str(field), str(value)

    return "", ""


def _is_har_live(operation: dict[str, Any]) -> bool:
    source = operation.get("x-discovery-source")
    if source is None:
        return True
    return str(source).strip().lower() == "har-live"


def _is_authenticated_operation(
    method_upper: str,
    path: str,
    operation: dict[str, Any],
    path_item: dict[str, Any],
    spec: dict[str, Any],
    correlated_auth_ops: set[tuple[str, str]],
) -> bool:
    if (method_upper, path) in correlated_auth_ops:
        return True
    if operation.get("x-requires-auth") is True or operation.get("x-auth-required") is True:
        return True
    if "security" in operation:
        sec = operation.get("security")
        if isinstance(sec, list) and len(sec) > 0:
            return True
    else:
        global_sec = spec.get("security")
        if isinstance(global_sec, list) and len(global_sec) > 0:
            return True

    custom = operation.get("x-custom-headers")
    if isinstance(custom, dict):
        for k in custom:
            if str(k).strip().lower() in AUTH_HEADER_NAMES:
                return True

    for p in _all_operation_params(operation, path_item):
        if p.get("in") == "header" and str(p.get("name", "")).strip().lower() in AUTH_HEADER_NAMES:
            return True

    return False


def _build_l2_path_template(step: dict[str, Any]) -> str:
    raw_path = str(step.get("path") or "/")
    raw_url = str(step.get("raw_url") or "")
    uses = step.get("uses") or {}

    # Convert any existing {{var}} in path to ${var}
    path_template = re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", r"${\1}", raw_path)

    path_uses: dict[str, str] = {}
    for use_key, var_expr in uses.items():
        if isinstance(use_key, str) and use_key.startswith("path:"):
            param_name = use_key.split(":", 1)[1].strip()
            var_name = strip_var_braces(str(var_expr))
            if param_name and var_name:
                path_uses[param_name] = var_name

    # First replace explicitly matching {param_name} placeholders
    unmatched_vars: list[str] = []
    for param_name, var_name in path_uses.items():
        token = "{" + param_name + "}"
        if token in path_template:
            path_template = path_template.replace(token, f"${{{var_name}}}")
        else:
            unmatched_vars.append(var_name)

    # If there are unmatched path uses and remaining single-brace {param} placeholders, align them
    remaining_placeholders = _PATH_PARAM_RE.findall(path_template)
    for var_name, placeholder in zip(unmatched_vars, remaining_placeholders, strict=False):
        path_template = path_template.replace("{" + placeholder + "}", f"${{{var_name}}}", 1)

    # For any still-unresolved {param} placeholders, fill concrete segment from raw_url
    if _PATH_PARAM_RE.search(path_template) and raw_url:
        parsed_raw_path = urlparse(raw_url).path
        tpl_segs = path_template.split("/")
        raw_segs = parsed_raw_path.split("/")
        if len(tpl_segs) == len(raw_segs):
            rebuilt: list[str] = []
            for t_seg, r_seg in zip(tpl_segs, raw_segs, strict=True):
                if _PATH_PARAM_RE.fullmatch(t_seg) and r_seg:
                    rebuilt.append(r_seg)
                else:
                    rebuilt.append(t_seg)
            path_template = "/".join(rebuilt)

    return path_template


def _build_l2_step_model(
    step: dict[str, Any],
    *,
    step_idx: int,
    chain_slug: str,
    depends_on: str | None,
    payloads_root: Path,
    openapi_ops: dict[tuple[str, str], dict[str, Any]],
    global_biz_rules: Any,
    project_config: Any = None,
) -> tuple[dict[str, Any], bool]:
    method_upper = str(step.get("method") or "GET").upper()
    norm_path = str(step.get("path") or "/")
    scenario = str(step.get("scenario") or "default")
    raw_url = str(step.get("raw_url") or "")
    uses = step.get("uses") or {}

    path_template = _build_l2_path_template(step)

    # Headers: filter safe custom headers + apply header:* uses
    headers: dict[str, str] = {}
    for hk, hv in (step.get("request_headers") or {}).items():
        if _is_safe_custom_header(str(hk), hv):
            headers[str(hk)] = re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", r"${\1}", str(hv))
    for use_key, var_expr in uses.items():
        if isinstance(use_key, str) and use_key.startswith("header:"):
            hname = use_key.split(":", 1)[1].strip()
            var_name = strip_var_braces(str(var_expr))
            if hname and var_name:
                headers[hname] = f"${{{var_name}}}"

    # Query params: combine step.query_params + raw_url query + apply query:* uses
    query_params: dict[str, str] = {}
    if raw_url:
        for qk, qv in parse_qsl(urlparse(raw_url).query, keep_blank_values=True):
            query_params[qk] = qv
    for qk, qv in (step.get("query_params") or {}).items():
        if qv is not None:
            query_params[str(qk)] = re.sub(
                r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", r"${\1}", str(qv)
            )
    for use_key, var_expr in uses.items():
        if isinstance(use_key, str) and use_key.startswith("query:"):
            qname = use_key.split(":", 1)[1].strip()
            var_name = strip_var_braces(str(var_expr))
            if qname and var_name:
                query_params[qname] = f"${{{var_name}}}"

    # Request payload externalization
    payload_path = ""
    wrote_payload = False
    request_body = step.get("request_body")
    if request_body is not None and request_body != "":
        body_uses = {
            k: str(v) for k, v in uses.items() if isinstance(k, str) and k.startswith("body:")
        }
        op_slug = sanitize_filename(f"{chain_slug}_step{step_idx}_{method_upper}_{norm_path}")
        rel_payload = build_payload_rel_path(scenario, op_slug)
        abs_payload = payloads_root.parent / rel_payload
        write_parameterized_payload(request_body, abs_payload, body_uses)
        payload_path = rel_payload
        wrote_payload = True

    # Lookup OpenAPI operation for biz_success metadata
    op_obj = openapi_ops.get((method_upper, norm_path))
    biz_code_field, biz_code_value = _extract_biz_success(
        op_obj, global_biz_rules, path=norm_path, project_config=project_config
    )

    # Extracts -> RestAssured GPath
    extracts_list: list[dict[str, Any]] = []
    used_java_vars: set[str] = set()
    for var_name, jp in (step.get("extracts") or {}).items():
        gpath = jsonpath_to_gpath(str(jp))
        if not gpath:
            continue
        base_jvar = sanitize_java_identifier(f"extracted_{var_name}", pascal=False, default="val")
        jvar = base_jvar
        n = 2
        while jvar in used_java_vars:
            jvar = f"{base_jvar}{n}"
            n += 1
        used_java_vars.add(jvar)

        track_resource = method_upper in {"POST", "PUT", "PATCH"} or any(
            kw in str(var_name).lower() for kw in ("id", "cart", "order", "item", "token")
        )
        extracts_list.append(
            {
                "var_name": str(var_name),
                "java_var": jvar,
                "gpath": gpath,
                "track_resource": track_resource,
            }
        )

    # Collect required context variables (useful for cleanup guard)
    required_vars: list[str] = []
    for text_val in [path_template, *headers.values(), *query_params.values()]:
        for m in _PLACEHOLDER_RE.finditer(text_val):
            v = m.group(1) or m.group(2)
            if v and v not in required_vars:
                required_vars.append(v)
    if payload_path:
        payload_text = (payloads_root.parent / payload_path).read_text(encoding="utf-8")
        for m in _PLACEHOLDER_RE.finditer(payload_text):
            v = m.group(1) or m.group(2)
            if v and v not in required_vars:
                required_vars.append(v)

    method_suffix = sanitize_java_identifier(f"{method_upper.lower()}_{norm_path}", pascal=False)
    method_name = f"step{step_idx:02d}_{method_suffix}"

    step_model = {
        "method_name": method_name,
        "depends_on": depends_on,
        "method": method_upper,
        "path_template": path_template,
        "headers": headers,
        "query_params": query_params,
        "payload_path": payload_path,
        "expected_status": int(step.get("expected_status") or 200),
        "biz_code_field": biz_code_field,
        "biz_code_value": biz_code_value,
        "extracts": extracts_list,
        "required_vars": required_vars,
        "label": f"Step {step_idx} {method_upper} {norm_path}",
        "description": f"Step {step_idx}: {method_upper} {norm_path}",
    }
    return step_model, wrote_payload


def _collect_correlated_auth_ops(correlation_data: dict[str, Any] | None) -> set[tuple[str, str]]:
    auth_ops: set[tuple[str, str]] = set()
    if not isinstance(correlation_data, dict):
        return auth_ops
    for chain in correlation_data.get("chains") or []:
        if not isinstance(chain, dict):
            continue
        all_steps = list(chain.get("steps") or [])
        if isinstance(chain.get("cleanup_step"), dict):
            all_steps.append(chain["cleanup_step"])
        for step in all_steps:
            if not isinstance(step, dict):
                continue
            req_headers = step.get("request_headers") or {}
            if any(str(k).strip().lower() in AUTH_HEADER_NAMES for k in req_headers):
                method = str(step.get("method") or "GET").upper()
                path = str(step.get("path") or "/")
                auth_ops.add((method, path))
    return auth_ops


def generate_java_project(
    openapi_path: Path,
    out_dir: Path,
    *,
    correlation_path: Path | None = None,
    package_name: str = "com.reconqa.generated",
    java_version: str = "21",
    base_uri: str | None = None,
    project_config: Any = None,
) -> dict[str, Any]:
    """Generate a complete, compilable Java TestNG + RestAssured + Allure Maven project."""
    openapi_path = Path(openapi_path)
    out_dir = Path(out_dir)
    spec = _load_openapi(openapi_path)

    # Resolve optional project_config overrides
    java_cfg = _get_attr_or_key(project_config, "java")
    if package_name == "com.reconqa.generated" and java_cfg is not None:
        cfg_pkg = _get_attr_or_key(java_cfg, "package_name")
        if cfg_pkg:
            package_name = str(cfg_pkg)
    if java_version == "21" and java_cfg is not None:
        cfg_ver = _get_attr_or_key(java_cfg, "java_version")
        if cfg_ver:
            java_version = str(cfg_ver)

    latency_multiplier = 3.0
    if java_cfg is not None:
        cfg_mult = _get_attr_or_key(java_cfg, "latency_multiplier")
        if cfg_mult is not None:
            try:
                latency_multiplier = float(cfg_mult)
            except (TypeError, ValueError):
                latency_multiplier = 3.0

    global_biz_rules = _get_attr_or_key(project_config, "biz_success_rules")

    resolved_base_uri = base_uri
    if not resolved_base_uri and project_config is not None:
        target_cfg = _get_attr_or_key(project_config, "target")
        resolved_base_uri = _get_attr_or_key(target_cfg, "base_url") or _get_attr_or_key(
            project_config, "base_url"
        )
    if not resolved_base_uri:
        servers = spec.get("servers")
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            resolved_base_uri = servers[0].get("url")
    if not resolved_base_uri:
        resolved_base_uri = "https://api.example.com"
    resolved_base_uri = str(resolved_base_uri).rstrip("/")

    package_name = sanitize_package_name(package_name)
    pkg_rel_path = Path(*package_name.split("."))

    # Load correlation report if available
    correlation_data: dict[str, Any] | None = None
    if correlation_path is not None:
        corr_p = Path(correlation_path)
        if corr_p.exists():
            correlation_data = read_json(corr_p)

    correlated_auth_ops = _collect_correlated_auth_ops(correlation_data)

    # Prepare directory layout
    resources_dir = ensure_dir(out_dir / "src" / "test" / "resources")
    data_dir = ensure_dir(resources_dir / "data")
    params_dir = ensure_dir(data_dir / "params")
    payloads_dir = ensure_dir(resources_dir / "payloads")
    schemas_dir = ensure_dir(resources_dir / "schemas")

    java_root = ensure_dir(out_dir / "src" / "test" / "java" / pkg_rel_path)
    for sub in ("base", "util", "listener", "smoke", "chains", "security"):
        ensure_dir(java_root / sub)

    # 1. Extract Draft-07 JSON Schemas from OpenAPI 3.0.3
    schema_map = extract_response_schemas(spec, schemas_dir)

    # Index OpenAPI operations and gather common custom headers
    paths_obj = spec.get("paths") or {}
    openapi_ops: dict[tuple[str, str], dict[str, Any]] = {}
    header_freq: dict[str, tuple[str, int]] = {}
    har_live_op_count = 0

    for path, path_item in paths_obj.items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            openapi_ops[(method.upper(), path)] = op
            if _is_har_live(op):
                har_live_op_count += 1
                safe_hdrs = _extract_safe_headers(op, path_item)
                for hk, hv in safe_hdrs.items():
                    prev_val, prev_cnt = header_freq.get(hk, (hv, 0))
                    header_freq[hk] = (prev_val, prev_cnt + 1)

    # Promote custom headers present across >=50% of har-live endpoints into ApiBaseTest defaultHeaders
    default_custom_headers: dict[str, str] = {}
    if har_live_op_count > 0:
        for hk, (hv, cnt) in header_freq.items():
            if cnt >= max(2, int(har_live_op_count * 0.5)):
                default_custom_headers[hk] = hv

    # 2. Build L1 Smoke CSV + externalized query/header/payload JSON files
    l1_rows: list[list[str]] = []
    l3_cases: list[dict[str, str]] = []
    payload_count = 0
    l1_idx = 0
    l3_idx = 0

    for path, path_item in paths_obj.items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict) or not _is_har_live(op):
                continue

            method_upper = method.upper()
            concrete_path = _resolve_concrete_path(path, op, path_item)
            scenarios = op.get("x-scenarios")
            scenario_name = (
                str(scenarios[0])
                if isinstance(scenarios, list) and scenarios
                else "default"
            )
            query_examples = _extract_query_examples(op, path_item)
            safe_headers = _extract_safe_headers(op, path_item)
            expected_status = _extract_expected_status(op)
            biz_field, biz_val = _extract_biz_success(
                op, global_biz_rules, path=path, project_config=project_config
            )
            schema_rel_path = schema_map.get((method_upper, path), "")

            # L1: Read-only GET endpoints
            if method_upper == "GET":
                l1_idx += 1
                case_id = f"L1_{l1_idx:03d}"

                query_json_path = ""
                if query_examples:
                    q_file = params_dir / f"{case_id}_query.json"
                    write_json(q_file, query_examples)
                    query_json_path = f"data/params/{case_id}_query.json"

                headers_json_path = ""
                op_specific_headers = {
                    k: v for k, v in safe_headers.items() if default_custom_headers.get(k) != v
                }
                if op_specific_headers:
                    h_file = params_dir / f"{case_id}_headers.json"
                    write_json(h_file, op_specific_headers)
                    headers_json_path = f"data/params/{case_id}_headers.json"

                payload_json_path = ""
                body_ex = _extract_request_body_example(op)
                if body_ex is not None:
                    rel_p = build_payload_rel_path("l1", f"{case_id}_{path}")
                    write_parameterized_payload(body_ex, resources_dir / rel_p)
                    payload_json_path = rel_p
                    payload_count += 1

                perf = op.get("x-perf-baseline") or {}
                p95_ms = perf.get("p95_ms") if isinstance(perf, dict) else None
                if isinstance(p95_ms, (int, float)) and p95_ms > 0:
                    max_latency_ms = max(round(p95_ms * latency_multiplier), 3000)
                else:
                    max_latency_ms = 5000

                l1_rows.append(
                    [
                        case_id,
                        scenario_name,
                        method_upper,
                        concrete_path,
                        query_json_path,
                        headers_json_path,
                        payload_json_path,
                        str(expected_status),
                        biz_field,
                        biz_val,
                        schema_rel_path,
                        str(max_latency_ms),
                    ]
                )

            # L3: Authenticated endpoints (any HTTP method)
            if _is_authenticated_operation(
                method_upper, path, op, path_item, spec, correlated_auth_ops
            ):
                l3_idx += 1
                l3_case_id = f"L3_{l3_idx:03d}"

                l3_query_path = ""
                if query_examples:
                    q_file = params_dir / f"{l3_case_id}_query.json"
                    write_json(q_file, query_examples)
                    l3_query_path = f"data/params/{l3_case_id}_query.json"

                l3_headers_path = ""
                if safe_headers:
                    h_file = params_dir / f"{l3_case_id}_headers.json"
                    write_json(h_file, safe_headers)
                    l3_headers_path = f"data/params/{l3_case_id}_headers.json"

                l3_payload_path = ""
                if method_upper in ("POST", "PUT", "PATCH"):
                    body_ex = _extract_request_body_example(op)
                    payload_obj = body_ex if body_ex is not None else {}
                    rel_p = build_payload_rel_path("l3", f"{l3_case_id}_{method_upper}_{path}")
                    write_parameterized_payload(payload_obj, resources_dir / rel_p)
                    l3_payload_path = rel_p
                    payload_count += 1

                l3_cases.append(
                    {
                        "case_id": l3_case_id,
                        "method": method_upper,
                        "path": concrete_path,
                        "query_json_path": l3_query_path,
                        "headers_json_path": l3_headers_path,
                        "payload_json_path": l3_payload_path,
                        "biz_code_field": biz_field,
                        "biz_code_value": biz_val,
                    }
                )

    # Write L1 CSV
    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf, lineterminator="\n")
    writer.writerow(CSV_HEADERS)
    writer.writerows(l1_rows)
    (data_dir / "l1_smoke_endpoints.csv").write_text(csv_buf.getvalue(), encoding="utf-8")

    # 3. Build L2 Chain models & payloads
    l2_chains: list[dict[str, Any]] = []
    l2_chain_classes: list[str] = []
    used_class_names: set[str] = set()

    raw_chains = (correlation_data or {}).get("chains") or []
    for chain_idx, chain in enumerate(raw_chains, start=1):
        if not isinstance(chain, dict):
            continue
        raw_steps = list(chain.get("steps") or [])
        raw_cleanup = chain.get("cleanup_step")

        # Prevent duplicate execution if cleanup_step is also listed in steps
        if isinstance(raw_cleanup, dict):
            cleanup_sig = (
                raw_cleanup.get("step"),
                str(raw_cleanup.get("method") or "").upper(),
                str(raw_cleanup.get("path") or ""),
            )
            filtered_steps = [
                s
                for s in raw_steps
                if isinstance(s, dict)
                and (
                    s.get("step"),
                    str(s.get("method") or "").upper(),
                    str(s.get("path") or ""),
                )
                != cleanup_sig
            ]
            if filtered_steps:
                raw_steps = filtered_steps

        if not raw_steps:
            continue

        chain_name = str(chain.get("name") or chain.get("scenario") or f"chain_{chain_idx}")
        chain_scenario = str(chain.get("scenario") or chain_name)
        chain_slug = slugify(chain_name)

        pascal = sanitize_java_identifier(chain_name, pascal=True, default=f"Chain{chain_idx}")
        if not pascal.startswith("L2"):
            pascal = f"L2{pascal}"
        if not pascal.endswith("Test"):
            pascal = f"{pascal}Test"

        class_name = pascal
        n = 2
        while class_name in used_class_names:
            class_name = f"{pascal[:-4]}{n}Test"
            n += 1
        used_class_names.add(class_name)

        step_models: list[dict[str, Any]] = []
        prev_method: str | None = None
        for idx, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                continue
            step_model, wrote = _build_l2_step_model(
                raw_step,
                step_idx=idx,
                chain_slug=chain_slug,
                depends_on=prev_method,
                payloads_root=payloads_dir,
                openapi_ops=openapi_ops,
                global_biz_rules=global_biz_rules,
                project_config=project_config,
            )
            if wrote:
                payload_count += 1
            step_models.append(step_model)
            prev_method = step_model["method_name"]

        if not step_models:
            continue

        cleanup_model = None
        if isinstance(raw_cleanup, dict):
            cleanup_model, wrote = _build_l2_step_model(
                raw_cleanup,
                step_idx=len(step_models) + 1,
                chain_slug=f"{chain_slug}_cleanup",
                depends_on=None,
                payloads_root=payloads_dir,
                openapi_ops=openapi_ops,
                global_biz_rules=global_biz_rules,
                project_config=project_config,
            )
            if wrote:
                payload_count += 1

        l2_chains.append(
            {
                "name": chain_name,
                "scenario": chain_scenario,
                "class_name": class_name,
                "steps": step_models,
                "cleanup_step": cleanup_model,
            }
        )
        l2_chain_classes.append(class_name)

    # 4. Render Jinja2 templates
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["java_str"] = escape_java_string

    artifact_id = slugify(spec.get("info", {}).get("title", "reconqa-api-tests"))
    if not artifact_id or artifact_id == "default":
        artifact_id = "reconqa-api-tests"

    common_ctx: dict[str, Any] = {
        "package_name": package_name,
        "artifact_id": artifact_id,
        "java_version": str(java_version),
        "base_uri": resolved_base_uri,
        "latency_multiplier": f"{latency_multiplier:.1f}",
        "default_custom_headers": default_custom_headers,
        "has_l1_cases": len(l1_rows) > 0,
        "l2_chain_classes": l2_chain_classes,
        "has_l3_cases": len(l3_cases) > 0,
        "l3_cases": l3_cases,
    }

    static_templates: list[tuple[str, Path]] = [
        ("pom.xml.j2", out_dir / "pom.xml"),
        ("testng.xml.j2", out_dir / "testng.xml"),
        ("config.properties.j2", resources_dir / "config.properties"),
        ("allure.properties.j2", resources_dir / "allure.properties"),
        ("logback-test.xml.j2", resources_dir / "logback-test.xml"),
        ("ApiBaseTest.java.j2", java_root / "base" / "ApiBaseTest.java"),
        ("ConfigUtils.java.j2", java_root / "util" / "ConfigUtils.java"),
        ("JsonUtils.java.j2", java_root / "util" / "JsonUtils.java"),
        ("CsvUtils.java.j2", java_root / "util" / "CsvUtils.java"),
        ("ReportUtils.java.j2", java_root / "util" / "ReportUtils.java"),
        ("TestExecutionListener.java.j2", java_root / "listener" / "TestExecutionListener.java"),
        ("L1SmokeContractTest.java.j2", java_root / "smoke" / "L1SmokeContractTest.java"),
        ("L3AuthBoundaryTest.java.j2", java_root / "security" / "L3AuthBoundaryTest.java"),
    ]

    generated_files: list[str] = []
    for tpl_name, dest_path in static_templates:
        rendered = env.get_template(tpl_name).render(**common_ctx)
        ensure_dir(dest_path.parent)
        dest_path.write_text(rendered, encoding="utf-8")
        generated_files.append(str(dest_path.relative_to(out_dir)))

    chain_tpl = env.get_template("L2ChainTest.java.j2")
    for chain_model in l2_chains:
        dest_path = java_root / "chains" / f"{chain_model['class_name']}.java"
        rendered = chain_tpl.render(**common_ctx, chain=chain_model)
        dest_path.write_text(rendered, encoding="utf-8")
        generated_files.append(str(dest_path.relative_to(out_dir)))

    return {
        "out_dir": str(out_dir),
        "package_name": package_name,
        "java_version": str(java_version),
        "base_uri": resolved_base_uri,
        "pom_path": str(out_dir / "pom.xml"),
        "testng_path": str(out_dir / "testng.xml"),
        "l1_count": len(l1_rows),
        "l2_chain_count": len(l2_chain_classes),
        "l2_chain_classes": l2_chain_classes,
        "l3_count": len(l3_cases),
        "schema_count": len(schema_map),
        "payload_count": payload_count,
        "generated_files": generated_files,
    }
