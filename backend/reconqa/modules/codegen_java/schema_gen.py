"""OpenAPI 3.0.3 -> JSON Schema Draft-07 converter and request payload parameterizer."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from reconqa.utils.io import ensure_dir, slugify, write_json

DRAFT_07_SCHEMA_URI = "http://json-schema.org/draft-07/schema#"

OPENAPI_ONLY_KEYS = {
    "nullable",
    "discriminator",
    "xml",
    "externalDocs",
    "example",
    "examples",
    "deprecated",
    "readOnly",
    "writeOnly",
}

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

_MUSTACHE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_PATH_TOKEN_RE = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")


def sanitize_filename(name: str, default: str = "schema") -> str:
    """Produce a safe lowercase ASCII filename stem without extension."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", name.strip()).strip("_").lower()
    if not cleaned:
        return default
    return cleaned[:96]


def _resolve_ref(ref: str, root_spec: dict[str, Any] | None) -> dict[str, Any] | None:
    if not root_spec or not ref.startswith("#/"):
        return None
    parts = ref[2:].split("/")
    current: Any = root_spec
    for part in parts:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current if isinstance(current, dict) else None


def openapi_schema_to_draft07(
    schema: dict[str, Any],
    *,
    root_spec: dict[str, Any] | None = None,
    is_root: bool = True,
    _seen_refs: set[str] | None = None,
) -> dict[str, Any]:
    """Convert an OpenAPI 3.0.3 Schema Object to a valid JSON Schema Draft-07 object."""
    seen = set(_seen_refs) if _seen_refs else set()
    node = dict(schema)

    if "$ref" in node and isinstance(node["$ref"], str):
        ref = node["$ref"]
        if ref not in seen:
            target = _resolve_ref(ref, root_spec)
            if target is not None:
                seen.add(ref)
                merged = dict(target)
                for k, v in node.items():
                    if k != "$ref":
                        merged[k] = v
                node = merged

    nullable = bool(node.get("nullable", False))
    result: dict[str, Any] = {}
    if is_root:
        result["$schema"] = DRAFT_07_SCHEMA_URI

    for key, value in node.items():
        if key in OPENAPI_ONLY_KEYS or key.startswith("x-") or key == "$ref":
            continue
        if key == "properties" and isinstance(value, dict):
            result["properties"] = {
                prop_name: openapi_schema_to_draft07(
                    prop_schema,
                    root_spec=root_spec,
                    is_root=False,
                    _seen_refs=seen,
                )
                if isinstance(prop_schema, dict)
                else prop_schema
                for prop_name, prop_schema in value.items()
            }
        elif key == "items":
            if isinstance(value, dict):
                result["items"] = openapi_schema_to_draft07(
                    value,
                    root_spec=root_spec,
                    is_root=False,
                    _seen_refs=seen,
                )
            elif isinstance(value, list):
                result["items"] = [
                    openapi_schema_to_draft07(
                        item,
                        root_spec=root_spec,
                        is_root=False,
                        _seen_refs=seen,
                    )
                    if isinstance(item, dict)
                    else item
                    for item in value
                ]
        elif key == "additionalProperties":
            if isinstance(value, dict):
                result["additionalProperties"] = openapi_schema_to_draft07(
                    value,
                    root_spec=root_spec,
                    is_root=False,
                    _seen_refs=seen,
                )
            else:
                result["additionalProperties"] = value
        elif key in ("allOf", "anyOf", "oneOf") and isinstance(value, list):
            result[key] = [
                openapi_schema_to_draft07(
                    sub,
                    root_spec=root_spec,
                    is_root=False,
                    _seen_refs=seen,
                )
                if isinstance(sub, dict)
                else sub
                for sub in value
            ]
        elif key == "not" and isinstance(value, dict):
            result["not"] = openapi_schema_to_draft07(
                value,
                root_spec=root_spec,
                is_root=False,
                _seen_refs=seen,
            )
        elif key == "patternProperties" and isinstance(value, dict):
            result["patternProperties"] = {
                pat: openapi_schema_to_draft07(
                    sub,
                    root_spec=root_spec,
                    is_root=False,
                    _seen_refs=seen,
                )
                if isinstance(sub, dict)
                else sub
                for pat, sub in value.items()
            }
        elif key == "required" and isinstance(value, list):
            # Deduplicate while preserving order
            deduped: list[str] = []
            for item in value:
                if isinstance(item, str) and item not in deduped:
                    deduped.append(item)
            if deduped:
                result["required"] = deduped
        else:
            result[key] = copy.deepcopy(value)

    # Ensure required keys exist in properties when properties are defined
    if "required" in result and isinstance(result.get("properties"), dict):
        props = result["properties"]
        filtered_req = [req for req in result["required"] if req in props]
        if filtered_req:
            result["required"] = filtered_req
        else:
            result.pop("required", None)

    if nullable:
        if "type" in result:
            t = result["type"]
            if isinstance(t, str):
                if t != "null":
                    result["type"] = [t, "null"]
            elif isinstance(t, list) and "null" not in t:
                result["type"] = [*t, "null"]
        elif "anyOf" in result and isinstance(result["anyOf"], list):
            if not any(isinstance(s, dict) and s.get("type") == "null" for s in result["anyOf"]):
                result["anyOf"].append({"type": "null"})
        elif (
            "oneOf" in result
            and isinstance(result["oneOf"], list)
            and not any(isinstance(s, dict) and s.get("type") == "null" for s in result["oneOf"])
        ):
            result["oneOf"].append({"type": "null"})

        if "enum" in result and isinstance(result["enum"], list) and None not in result["enum"]:
            result["enum"] = [*result["enum"], None]

    return result


def _pick_2xx_json_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    # Prefer 200, then other 2xx responses
    preferred_codes = sorted(
        (str(code) for code in responses if str(code).startswith("2")),
        key=lambda c: (0 if c == "200" else 1, c),
    )
    for code in preferred_codes:
        resp_obj = responses.get(code)
        if not isinstance(resp_obj, dict):
            continue
        content = resp_obj.get("content")
        if not isinstance(content, dict):
            continue
        for media_type, media_obj in content.items():
            if "json" not in str(media_type).lower():
                continue
            if isinstance(media_obj, dict) and isinstance(media_obj.get("schema"), dict):
                return media_obj["schema"]
    return None


def extract_response_schemas(
    openapi_spec: dict[str, Any],
    schemas_dir: Path,
) -> dict[tuple[str, str], str]:
    """Extract 2xx JSON response schemas from OpenAPI 3.0.3 and write Draft-07 schema files.

    Returns a mapping of (HTTP_METHOD_UPPER, path) -> relative classpath path ("schemas/<op>.json").
    """
    ensure_dir(schemas_dir)
    schema_map: dict[tuple[str, str], str] = {}
    used_names: set[str] = set()

    paths = openapi_spec.get("paths") or {}
    if not isinstance(paths, dict):
        return schema_map

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            raw_schema = _pick_2xx_json_schema(operation)
            if not raw_schema:
                continue

            op_id = operation.get("operationId") or f"{method}_{path}"
            base_name = sanitize_filename(str(op_id), default=f"{method}_response")
            candidate = base_name
            suffix = 2
            while candidate in used_names:
                candidate = f"{base_name}_{suffix}"
                suffix += 1
            used_names.add(candidate)

            draft07 = openapi_schema_to_draft07(raw_schema, root_spec=openapi_spec, is_root=True)
            out_file = schemas_dir / f"{candidate}.json"
            write_json(out_file, draft07)
            schema_map[(method.upper(), path)] = f"schemas/{candidate}.json"

    return schema_map


def strip_var_braces(expr: str) -> str:
    """Convert '{{varName}}' or '${varName}' to 'varName'."""
    text = expr.strip()
    if text.startswith("{{") and text.endswith("}}"):
        return text[2:-2].strip()
    if text.startswith("${") and text.endswith("}"):
        return text[2:-1].strip()
    return text


def _convert_mustache_recursively(obj: Any) -> Any:
    if isinstance(obj, str):
        return _MUSTACHE_VAR_RE.sub(r"${\1}", obj)
    if isinstance(obj, list):
        return [_convert_mustache_recursively(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _convert_mustache_recursively(v) for k, v in obj.items()}
    return obj


def _set_by_path(obj: Any, field_path: str, placeholder: str) -> bool:
    cleaned = field_path.strip()
    if cleaned.startswith("$."):
        cleaned = cleaned[2:]
    elif cleaned.startswith("$"):
        cleaned = cleaned[1:]
    cleaned = cleaned.lstrip(".")
    if not cleaned:
        return False

    tokens: list[str | int] = []
    for match in _PATH_TOKEN_RE.finditer(cleaned):
        key_part, idx_part = match.groups()
        if key_part is not None:
            tokens.append(key_part)
        elif idx_part is not None:
            tokens.append(int(idx_part))
    if not tokens:
        return False

    current = obj
    for token in tokens[:-1]:
        if (isinstance(token, str) and isinstance(current, dict) and token in current) or (
            isinstance(token, int) and isinstance(current, list) and 0 <= token < len(current)
        ):
            current = current[token]
        else:
            return False

    last = tokens[-1]
    if (isinstance(last, str) and isinstance(current, dict) and last in current) or (
        isinstance(last, int) and isinstance(current, list) and 0 <= last < len(current)
    ):
        current[last] = placeholder
        return True
    return False


def _replace_key_recursive(obj: Any, target_key: str, placeholder: str) -> bool:
    replaced = False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == target_key and not isinstance(v, (dict, list)):
                obj[k] = placeholder
                replaced = True
            elif isinstance(v, (dict, list)) and _replace_key_recursive(v, target_key, placeholder):
                replaced = True
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)) and _replace_key_recursive(
                item, target_key, placeholder
            ):
                replaced = True
    return replaced


def parameterize_payload(payload: Any, body_uses: dict[str, str] | None = None) -> Any:
    """Replace dynamic fields in a JSON request payload with ${varName} placeholders."""
    cloned = _convert_mustache_recursively(copy.deepcopy(payload))
    if not body_uses:
        return cloned

    for param_key, var_expr in body_uses.items():
        field_spec = param_key
        if field_spec.startswith("body:"):
            field_spec = field_spec.split(":", 1)[1]
        var_name = strip_var_braces(var_expr)
        if not var_name:
            continue
        placeholder = f"${{{var_name}}}"
        if not _set_by_path(cloned, field_spec, placeholder):
            leaf_key = field_spec.split(".")[-1].split("[")[0]
            if leaf_key:
                _replace_key_recursive(cloned, leaf_key, placeholder)

    return cloned


def write_parameterized_payload(
    payload: Any,
    out_path: Path,
    body_uses: dict[str, str] | None = None,
) -> Path:
    """Parameterize a request body and write it to disk as UTF-8 JSON."""
    parameterized = parameterize_payload(payload, body_uses)
    return write_json(out_path, parameterized)


def build_payload_rel_path(scenario: str, op_name: str) -> str:
    """Build a clean classpath-relative payload path under payloads/<scenario>/<op>.json."""
    scenario_slug = slugify(scenario)
    op_slug = sanitize_filename(op_name, default="request")
    return f"payloads/{scenario_slug}/{op_slug}.json"
