"""Security-hardened Deep Assertion Engine (22 operators + custom, cross-step vars & read-only SQL)."""

from __future__ import annotations

import ast
import json
import math
import operator as py_op
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from reconqa.config import settings
from reconqa.models.schemas import AssertionEvalResult, ScenarioAssertion
from reconqa.modules.discovery.auth_resolver import redact_secrets, resolve_env_ref

_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")
_SINGLE_VAR_RE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}$")
_ENV_DSN_REF_RE = re.compile(r"^(?:[Ee][Nn][Vv]:(RECONQA_[A-Za-z0-9_]+)|\$\{(RECONQA_[A-Za-z0-9_]+)\})$")
_FORBIDDEN_ENV_DSN_KEYS = frozenset({"RECONQA_DATABASE_URL"})
_SQL_FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|ATTACH|DETACH|"
    r"PRAGMA|EXEC|EXECUTE|CALL|GRANT|REVOKE|COPY|LOAD|"
    r"INTO\s+(?:OUTFILE|DUMPFILE)|INTO|FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE)\b",
    re.IGNORECASE,
)
_SQL_PARAM_VAR_RE = re.compile(
    r"'\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}'|\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}"
)
_URL_CRED_RE = re.compile(r"://[^/\s@]+@")
_EXTERNAL_REF_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_MAX_SQL_ROWS = 50


def extract_env_dsn_key(dsn_ref: str | None) -> str | None:
    """Extract the RECONQA_* environment variable name from a valid ENV ref, else None."""
    if not dsn_ref:
        return None
    m = _ENV_DSN_REF_RE.fullmatch(str(dsn_ref).strip())
    if not m:
        return None
    return m.group(1) or m.group(2)


def is_allowed_env_dsn_ref(dsn_ref: str | None) -> bool:
    """Return True iff dsn_ref strictly uses the ENV:RECONQA_* / ${RECONQA_*} allowlist (excluding internal DB)."""
    env_key = extract_env_dsn_key(dsn_ref)
    if not env_key:
        return False
    return env_key.upper() not in _FORBIDDEN_ENV_DSN_KEYS


# --- JSONPath extraction ---


def _tokenize_jsonpath(path: str) -> list[str | int]:
    """Tokenize JSONPath expressions like $.a.b[0].c, data.items[*].id, or ['key']."""
    raw = (path or "").strip()
    if not raw or raw == "$":
        return []
    if raw.startswith("$."):
        raw = raw[2:]
    elif raw.startswith("$"):
        raw = raw[1:]

    tokens: list[str | int] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == ".":
            i += 1
            continue
        if ch == "[":
            end = raw.find("]", i + 1)
            if end == -1:
                break
            inner = raw[i + 1 : end].strip()
            if inner == "*":
                tokens.append("*")
            elif (inner.startswith("'") and inner.endswith("'")) or (
                inner.startswith('"') and inner.endswith('"')
            ):
                tokens.append(inner[1:-1])
            else:
                try:
                    tokens.append(int(inner))
                except ValueError:
                    tokens.append(inner)
            i = end + 1
            continue

        start = i
        while i < n and raw[i] not in ".[":
            i += 1
        seg = raw[start:i].strip()
        if seg:
            tokens.append(seg)
    return tokens


def _walk_jsonpath(cur: Any, tokens: list[str | int], idx: int) -> tuple[bool, Any]:
    if idx >= len(tokens):
        return True, cur
    tok = tokens[idx]
    if tok == "*":
        if not isinstance(cur, list):
            return False, None
        if idx + 1 >= len(tokens):
            return True, list(cur)
        collected: list[Any] = []
        for item in cur:
            ok, val = _walk_jsonpath(item, tokens, idx + 1)
            if ok:
                collected.append(val)
        return True, collected
    if isinstance(tok, int):
        if isinstance(cur, list) and -len(cur) <= tok < len(cur):
            return _walk_jsonpath(cur[tok], tokens, idx + 1)
        return False, None
    if isinstance(cur, dict) and tok in cur:
        return _walk_jsonpath(cur[tok], tokens, idx + 1)
    return False, None


def extract_jsonpath(data: Any, path: str) -> tuple[bool, Any]:
    """Extract a value from parsed JSON data via JSONPath (supports $, dot, [idx], [*])."""
    clean = (path or "").strip()
    if not clean or clean == "$":
        return True, data
    if data is None:
        return False, None
    tokens = _tokenize_jsonpath(clean)
    if not tokens:
        return True, data
    return _walk_jsonpath(data, tokens, 0)


# --- Template rendering & safe arithmetic ---


def render_template_str(raw: str, context_vars: dict[str, Any] | None = None) -> str:
    """Replace {{var_name}} placeholders with str(context_vars[var_name]) when present."""
    text = str(raw)
    if not context_vars or "{{" not in text:
        return text

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context_vars and context_vars[key] is not None:
            return str(context_vars[key])
        return match.group(0)

    return _TEMPLATE_VAR_RE.sub(_repl, text)


_ALLOWED_BINOPS: dict[type[ast.operator], Any] = {
    ast.Add: py_op.add,
    ast.Sub: py_op.sub,
    ast.Mult: py_op.mul,
    ast.Div: py_op.truediv,
}
_ALLOWED_UNARYOPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: py_op.pos,
    ast.USub: py_op.neg,
}


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return num if math.isfinite(num) else None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        num = float(value)
        return num if math.isfinite(num) else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            num = float(s)
            return num if math.isfinite(num) else None
        except ValueError:
            return None
    return None


def _to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Decimal(str(value))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            dec = Decimal(s)
            return dec if dec.is_finite() else None
        except InvalidOperation:
            return None
    return None


def _eval_ast_node_decimal(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Expression):
        return _eval_ast_node_decimal(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            dec = _to_decimal(node.value)
            if dec is None:
                raise ValueError("Non-finite constant")
            return dec
        raise ValueError("Non-numeric constant")
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        res = _ALLOWED_UNARYOPS[type(node.op)](_eval_ast_node_decimal(node.operand))
        if not isinstance(res, Decimal) or not res.is_finite():
            raise ValueError("Non-finite result")
        return res
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left = _eval_ast_node_decimal(node.left)
        right = _eval_ast_node_decimal(node.right)
        if isinstance(node.op, ast.Div) and right == 0:
            raise ZeroDivisionError("Division by zero")
        res = _ALLOWED_BINOPS[type(node.op)](left, right)
        if not isinstance(res, Decimal) or not res.is_finite():
            raise ValueError("Non-finite result")
        return res
    raise ValueError(f"Disallowed AST node: {type(node).__name__}")


def safe_eval_arithmetic_decimal(
    expr: str, context_vars: dict[str, Any] | None = None
) -> Decimal | None:
    """Safely evaluate exact Decimal arithmetic expressions with {{var}} substitution via AST only."""
    raw = str(expr or "").strip()
    if not raw:
        return None
    ctx = context_vars or {}

    missing_or_invalid = False

    def _sub_num(match: re.Match[str]) -> str:
        nonlocal missing_or_invalid
        key = match.group(1)
        if key not in ctx:
            missing_or_invalid = True
            return match.group(0)
        dec = _to_decimal(ctx[key])
        if dec is None:
            missing_or_invalid = True
            return match.group(0)
        return f"({format(dec, 'f')})"

    substituted = _TEMPLATE_VAR_RE.sub(_sub_num, raw)
    if missing_or_invalid:
        return None

    try:
        tree = ast.parse(substituted, mode="eval")
        return _eval_ast_node_decimal(tree)
    except Exception:
        return None


def safe_eval_arithmetic(expr: str, context_vars: dict[str, Any] | None = None) -> float | None:
    """Safely evaluate numeric arithmetic expressions with {{var}} substitution via AST only."""
    dec = safe_eval_arithmetic_decimal(expr, context_vars)
    if dec is None:
        return None
    num = float(dec)
    return num if math.isfinite(num) else None


# --- Read-only SQL verification ---


def _strip_sql_comments(sql: str) -> str:
    """Remove -- line comments and /* */ block comments while preserving single-quoted literals."""
    out: list[str] = []
    i = 0
    n = len(sql)
    in_single = False
    while i < n:
        ch = sql[i]
        if in_single:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if ch == "'":
            in_single = True
            out.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            i += 2
            while i < n and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                break
            i = end + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _normalize_sync_dsn(dsn: str) -> str:
    """Convert async SQLAlchemy DSN schemes to their sync equivalents for read-only checks."""
    low = dsn.lower()
    if low.startswith("sqlite+aiosqlite:"):
        return "sqlite:" + dsn[len("sqlite+aiosqlite:") :]
    if low.startswith("postgresql+asyncpg:"):
        return "postgresql:" + dsn[len("postgresql+asyncpg:") :]
    return dsn


def _extract_sqlite_path(dsn: str) -> Path | None:
    norm = _normalize_sync_dsn(dsn).strip()
    if not norm.lower().startswith("sqlite:"):
        return None
    rest = norm[len("sqlite:") :]
    rest = rest.split("?", 1)[0]
    if rest in {"", ":memory:", "/:memory:"}:
        return None
    if rest.startswith("///"):
        raw_path = rest[3:]
    elif rest.startswith("//"):
        raw_path = rest[2:]
    else:
        raw_path = rest
    if not raw_path:
        return None
    try:
        return Path(raw_path).expanduser().resolve()
    except Exception:
        return None


def _is_internal_reconqa_db(resolved_dsn: str) -> bool:
    """Return True if resolved_dsn points at ReconQA's own application database."""
    norm_target = _normalize_sync_dsn(resolved_dsn).strip()
    norm_app = _normalize_sync_dsn(settings.database_url).strip()
    if norm_target and norm_app and norm_target.lower() == norm_app.lower():
        return True
    target_sqlite = _extract_sqlite_path(norm_target)
    if target_sqlite is not None:
        app_sqlite = _extract_sqlite_path(norm_app)
        if app_sqlite is not None and target_sqlite == app_sqlite:
            return True
        default_app_db = (settings.data_dir.resolve() / "reconqa.db").resolve()
        if target_sqlite == default_app_db:
            return True
    return False


def _extract_dsn_secrets(dsn: str, sync_dsn: str) -> list[str]:
    """Collect raw and decoded credential substrings from a DSN for error redaction."""
    secrets: list[str] = [dsn, sync_dsn]
    for candidate in (dsn, sync_dsn):
        try:
            parsed = urlparse(candidate)
            if parsed.password:
                secrets.append(parsed.password)
                secrets.append(unquote(parsed.password))
            if parsed.username and parsed.password:
                secrets.append(f"{parsed.username}:{parsed.password}")
                secrets.append(f"{unquote(parsed.username)}:{unquote(parsed.password)}")
        except Exception:
            pass
        if "://" in candidate and "@" in candidate:
            after_scheme = candidate.split("://", 1)[1]
            userinfo = after_scheme.split("@", 1)[0]
            if userinfo:
                secrets.append(userinfo)
                if ":" in userinfo:
                    raw_pwd = userinfo.split(":", 1)[1]
                    if raw_pwd:
                        secrets.append(raw_pwd)
                        secrets.append(unquote(raw_pwd))
    return [s for s in secrets if s]


def _sanitize_sql_error(exc: Exception, secrets: list[str]) -> str:
    """Sanitize SQLAlchemy/driver exceptions so DSN credentials are never leaked."""
    raw = str(exc)
    redacted = _URL_CRED_RE.sub("://[REDACTED]@", raw)
    redacted = redact_secrets(redacted, secrets)
    # Keep concise single-line summary without leaking internal driver parameter dumps
    first_line = redacted.splitlines()[0].strip() if redacted else exc.__class__.__name__
    if len(first_line) > 220:
        first_line = f"{first_line[:217]}..."
    return first_line or exc.__class__.__name__


def execute_readonly_sql(
    query: str,
    *,
    context_vars: dict[str, Any] | None = None,
    db_dsn_ref: str | None = None,
    timeout_sec: float = 5.0,
) -> tuple[bool, Any, str]:
    """Execute a strictly read-only, fail-closed, parameterized SELECT query using an ENV DSN."""
    env_key = extract_env_dsn_key(db_dsn_ref)
    if not env_key:
        return (
            False,
            None,
            "db_dsn_ref must use an ENV:RECONQA_* or ${RECONQA_*} reference; plaintext DSNs are rejected.",
        )
    if env_key.upper() in _FORBIDDEN_ENV_DSN_KEYS:
        return (
            False,
            None,
            f"Access to ReconQA's internal database ({env_key}) is forbidden; use a dedicated ENV:RECONQA_ASSERT_DB_* or ENV:RECONQA_TEST_DB_* reference.",
        )

    resolved_dsn = resolve_env_ref(str(db_dsn_ref or "")).strip()
    if not resolved_dsn:
        return (
            False,
            None,
            f"Environment variable referenced by '{db_dsn_ref}' is not set or empty.",
        )

    if _is_internal_reconqa_db(resolved_dsn):
        return (
            False,
            None,
            "Refused to query ReconQA's own internal application database.",
        )

    cleaned = _strip_sql_comments(str(query or "")).strip()
    if not cleaned:
        return False, None, "SQL query must not be empty."
    if ";" in cleaned:
        return False, None, "Multi-statement SQL queries (';') are forbidden."
    if not re.match(r"^SELECT\s+", cleaned, re.IGNORECASE):
        return False, None, "Only read-only SELECT queries are allowed."
    forbidden = _SQL_FORBIDDEN_RE.search(cleaned)
    if forbidden:
        return (
            False,
            None,
            f"Forbidden SQL keyword '{forbidden.group(1).upper()}' rejected in read-only mode.",
        )

    ctx = context_vars or {}
    bind_params: dict[str, Any] = {}
    missing_vars: list[str] = []

    def _bind_var(match: re.Match[str]) -> str:
        var_name = match.group(1) or match.group(2)
        if var_name not in ctx:
            missing_vars.append(var_name)
            return match.group(0)
        param_key = f"v_{len(bind_params)}"
        bind_params[param_key] = ctx[var_name]
        return f":{param_key}"

    parameterized_sql = _SQL_PARAM_VAR_RE.sub(_bind_var, cleaned)
    if missing_vars:
        return (
            False,
            None,
            f"Missing context variable(s) for SQL query: {', '.join(missing_vars)}",
        )

    from sqlalchemy import create_engine, text

    sync_dsn = _normalize_sync_dsn(resolved_dsn)
    connect_args: dict[str, Any] = {}
    if sync_dsn.lower().startswith("sqlite:"):
        connect_args["timeout"] = max(1.0, float(timeout_sec or 5.0))

    engine = None
    secrets_to_redact = _extract_dsn_secrets(resolved_dsn, sync_dsn)
    try:
        try:
            engine = create_engine(sync_dsn, connect_args=connect_args, pool_pre_ping=False)
        except ModuleNotFoundError as exc:
            mod_name = getattr(exc, "name", None) or "database driver"
            return (
                False,
                None,
                f"Read-only SQL driver unavailable ({mod_name}); install the sync driver for this database.",
            )

        dialect_name = (engine.dialect.name or "").lower()
        with engine.connect() as conn:
            if dialect_name == "sqlite":
                conn.execute(text("PRAGMA query_only = ON"))
                qo_val = conn.execute(text("PRAGMA query_only")).scalar()
                if str(qo_val).strip().lower() not in {"1", "true", "on"}:
                    return (
                        False,
                        None,
                        "Fail-closed read-only check failed: SQLite PRAGMA query_only was not enabled.",
                    )
                result = conn.execute(text(parameterized_sql), bind_params)
                rows = [tuple(row) for row in result.fetchmany(_MAX_SQL_ROWS)]
            elif dialect_name == "postgresql":
                ro_conn = conn.execution_options(postgresql_readonly=True)
                ro_conn.execute(text("SET TRANSACTION READ ONLY"))
                result = ro_conn.execute(text(parameterized_sql), bind_params)
                rows = [tuple(row) for row in result.fetchmany(_MAX_SQL_ROWS)]
            elif dialect_name in {"mysql", "mariadb"}:
                ro_conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                ro_conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
                result = ro_conn.execute(text(parameterized_sql), bind_params)
                rows = [tuple(row) for row in result.fetchmany(_MAX_SQL_ROWS)]
            else:
                return (
                    False,
                    None,
                    f"Unsupported SQL dialect '{dialect_name}' for fail-closed read-only verification.",
                )
        return True, rows, f"Returned {len(rows)} row(s)"
    except Exception as exc:
        safe_msg = _sanitize_sql_error(exc, secrets_to_redact)
        return False, None, f"Read-only SQL execution failed: {safe_msg}"
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


# --- Value comparison & assertion evaluation ---


def _is_empty_value(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str):
        return val.strip() == ""
    if isinstance(val, (list, dict, tuple, set)):
        return len(val) == 0
    return False


def _values_equal(actual: Any, expected_str: str) -> bool:
    """Compare actual against expected_str with exact int/Decimal/string precision (no 64-bit float loss)."""
    exp_clean = expected_str.strip()
    if isinstance(actual, bool):
        if exp_clean.lower() == "true":
            return actual is True
        if exp_clean.lower() == "false":
            return actual is False
        return False
    if actual is None:
        return exp_clean.lower() in {"null", "none", ""}
    if isinstance(actual, Decimal):
        exp_dec = _to_decimal(exp_clean)
        if exp_dec is not None:
            return actual == exp_dec
        return str(actual) == exp_clean
    if isinstance(actual, int):
        try:
            return actual == int(exp_clean)
        except ValueError:
            exp_dec = _to_decimal(exp_clean)
            if exp_dec is not None:
                return Decimal(actual) == exp_dec
            return False
    if isinstance(actual, float):
        exp_dec = _to_decimal(exp_clean)
        act_dec = _to_decimal(actual)
        if act_dec is not None and exp_dec is not None:
            if act_dec == exp_dec:
                return True
        exp_num = _to_number(exp_clean)
        if exp_num is not None:
            return abs(actual - exp_num) < 1e-6
        return str(actual) == exp_clean
    if isinstance(actual, str):
        return actual == expected_str or actual.strip() == exp_clean
    try:
        parsed_exp = json.loads(exp_clean)
        return actual == parsed_exp
    except Exception:
        return str(actual) == exp_clean


def _values_equal_any(actual: Any, expected_val: Any) -> bool:
    """Exact comparison for ints/strings/Decimals; float tolerance only when floats are involved."""
    if isinstance(actual, bool) or isinstance(expected_val, bool):
        return type(actual) is type(expected_val) and actual == expected_val
    if isinstance(actual, int) and isinstance(expected_val, int):
        return actual == expected_val
    if isinstance(actual, str) and isinstance(expected_val, str):
        return actual == expected_val
    if isinstance(actual, (int, Decimal)) or isinstance(expected_val, (int, Decimal)):
        if not isinstance(actual, float) and not isinstance(expected_val, float):
            dec_a = _to_decimal(actual)
            dec_b = _to_decimal(expected_val)
            if dec_a is not None and dec_b is not None:
                return dec_a == dec_b
            return str(actual) == str(expected_val)
    if isinstance(actual, float) or isinstance(expected_val, float):
        num_a = _to_number(actual)
        num_b = _to_number(expected_val)
        if num_a is not None and num_b is not None:
            return abs(num_a - num_b) < 1e-6
    if actual == expected_val:
        return True
    return str(actual) == str(expected_val)


def _dict_is_subset(subset: dict[str, Any], superset: dict[str, Any]) -> bool:
    for k, v in subset.items():
        if k not in superset:
            return False
        sv = superset[k]
        if isinstance(v, dict) and isinstance(sv, dict):
            if not _dict_is_subset(v, sv):
                return False
        elif not _values_equal_any(sv, v):
            return False
    return True


def _check_json_type(val: Any, expected_type: str) -> bool:
    t = expected_type.strip().lower()
    if t in {"null", "none"}:
        return val is None
    if t in {"boolean", "bool"}:
        return isinstance(val, bool)
    if t in {"integer", "int"}:
        return isinstance(val, int) and not isinstance(val, bool)
    if t in {"number", "float"}:
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if t in {"string", "str"}:
        return isinstance(val, str)
    if t in {"array", "list"}:
        return isinstance(val, list)
    if t in {"object", "dict"}:
        return isinstance(val, dict)
    return False


def _find_external_schema_ref(obj: Any) -> str | None:
    """Recursively detect external $ref URLs (http/https/file/etc.) to prevent SSRF."""
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str):
            clean = ref.strip()
            if _EXTERNAL_REF_SCHEME_RE.match(clean):
                return clean
        for v in obj.values():
            found = _find_external_schema_ref(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_external_schema_ref(item)
            if found is not None:
                return found
    return None


def _resolve_local_json_pointer(root_schema: dict[str, Any], ref: str) -> dict[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    cur: Any = root_schema
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur if isinstance(cur, dict) else None


def _safe_nonneg_int(val: Any) -> int | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int) and val >= 0:
        return val
    return None


def _validate_json_schema_fallback(
    instance: Any,
    schema: dict[str, Any],
    path: str = "$",
    root_schema: dict[str, Any] | None = None,
    _depth: int = 0,
) -> tuple[bool, str]:
    """Recursive JSON Schema subset validator used when jsonschema package is unavailable."""
    if _depth > 20:
        return False, f"At {path}: JSON Schema $ref nesting limit exceeded."
    root = root_schema if root_schema is not None else schema

    local_ref = schema.get("$ref")
    if isinstance(local_ref, str) and local_ref.strip().startswith("#/"):
        target = _resolve_local_json_pointer(root, local_ref.strip())
        if target is None:
            return False, f"At {path}: unresolved local $ref '{local_ref}'"
        ok, msg = _validate_json_schema_fallback(instance, target, path, root, _depth + 1)
        if not ok:
            return False, msg

    exp_type = schema.get("type")
    if isinstance(exp_type, str):
        if not _check_json_type(instance, exp_type):
            return False, f"At {path}: expected JSON type '{exp_type}', got '{type(instance).__name__}'"
    elif isinstance(exp_type, list):
        if not any(isinstance(t, str) and _check_json_type(instance, t) for t in exp_type):
            return False, f"At {path}: expected one of types {exp_type}, got '{type(instance).__name__}'"

    if "const" in schema and not _values_equal_any(instance, schema["const"]):
        return False, f"At {path}: expected const {schema['const']!r}, got {instance!r}"

    if "enum" in schema and isinstance(schema["enum"], list):
        if not any(_values_equal_any(instance, item) for item in schema["enum"]):
            return False, f"At {path}: value {instance!r} is not in enum {schema['enum']!r}"

    if isinstance(instance, (int, float, Decimal)) and not isinstance(instance, bool):
        inst_dec = _to_decimal(instance)
        for kw, op_check in (
            ("minimum", lambda a, b: a < b),
            ("maximum", lambda a, b: a > b),
            ("exclusiveMinimum", lambda a, b: a <= b),
            ("exclusiveMaximum", lambda a, b: a >= b),
        ):
            if kw in schema:
                bound_dec = _to_decimal(schema[kw])
                if bound_dec is None:
                    return False, f"At {path}: invalid schema constraint {kw}={schema[kw]!r}"
                if inst_dec is not None and op_check(inst_dec, bound_dec):
                    return False, f"At {path}: {instance} failed {kw} {schema[kw]}"

    if isinstance(instance, str):
        if "minLength" in schema:
            min_len = _safe_nonneg_int(schema["minLength"])
            if min_len is None:
                return False, f"At {path}: invalid schema constraint minLength={schema['minLength']!r}"
            if len(instance) < min_len:
                return False, f"At {path}: string length {len(instance)} < minLength {min_len}"
        if "maxLength" in schema:
            max_len = _safe_nonneg_int(schema["maxLength"])
            if max_len is None:
                return False, f"At {path}: invalid schema constraint maxLength={schema['maxLength']!r}"
            if len(instance) > max_len:
                return False, f"At {path}: string length {len(instance)} > maxLength {max_len}"
        if "pattern" in schema:
            if not isinstance(schema["pattern"], str):
                return False, f"At {path}: invalid schema constraint pattern={schema['pattern']!r}"
            try:
                if re.search(schema["pattern"], instance) is None:
                    return False, f"At {path}: string did not match pattern /{schema['pattern']}/"
            except re.error as exc:
                return False, f"At {path}: invalid schema regex pattern: {exc}"

    if isinstance(instance, list):
        if "minItems" in schema:
            min_items = _safe_nonneg_int(schema["minItems"])
            if min_items is None:
                return False, f"At {path}: invalid schema constraint minItems={schema['minItems']!r}"
            if len(instance) < min_items:
                return False, f"At {path}: array length {len(instance)} < minItems {min_items}"
        if "maxItems" in schema:
            max_items = _safe_nonneg_int(schema["maxItems"])
            if max_items is None:
                return False, f"At {path}: invalid schema constraint maxItems={schema['maxItems']!r}"
            if len(instance) > max_items:
                return False, f"At {path}: array length {len(instance)} > maxItems {max_items}"
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for idx, elem in enumerate(instance):
                ok, msg = _validate_json_schema_fallback(elem, items_schema, f"{path}[{idx}]", root, _depth + 1)
                if not ok:
                    return False, msg

    if isinstance(instance, dict):
        for req in schema.get("required") or []:
            if req not in instance:
                return False, f"At {path}: missing required property '{req}'"
        props = schema.get("properties")
        if isinstance(props, dict):
            for prop_name, prop_schema in props.items():
                if prop_name in instance and isinstance(prop_schema, dict):
                    ok, msg = _validate_json_schema_fallback(
                        instance[prop_name], prop_schema, f"{path}.{prop_name}", root, _depth + 1
                    )
                    if not ok:
                        return False, msg
        if schema.get("additionalProperties") is False and isinstance(props, dict):
            extras = [k for k in instance if k not in props]
            if extras:
                return False, f"At {path}: unexpected additional properties {extras}"

    return True, "Matched JSON Schema (fallback validator)"


def _validate_json_schema_value(instance: Any, schema: dict[str, Any]) -> tuple[bool, str]:
    ext_ref = _find_external_schema_ref(schema)
    if ext_ref is not None:
        return False, f"External $ref '{ext_ref}' is forbidden in json_schema assertions."

    try:
        import jsonschema  # type: ignore[import-untyped]

        jsonschema.validate(instance=instance, schema=schema)
        return True, "Matched JSON Schema"
    except ImportError:
        return _validate_json_schema_fallback(instance, schema)
    except Exception as exc:
        msg = getattr(exc, "message", None) or str(exc)
        return False, f"JSON Schema validation failed: {msg}"


def _contains_check(actual: Any, expected_str: str) -> bool:
    if isinstance(actual, str):
        return expected_str in actual
    if isinstance(actual, list):
        return any(_values_equal(item, expected_str) or expected_str in str(item) for item in actual)
    if isinstance(actual, dict):
        return expected_str in actual or expected_str in json.dumps(actual, ensure_ascii=False)
    return expected_str in str(actual)


def evaluate_assertion(
    assertion: ScenarioAssertion | dict[str, Any],
    *,
    status_code: int,
    duration_ms: float,
    response_headers: dict[str, str],
    body_json: Any,
    body_text: str,
    context_vars: dict[str, Any] | None = None,
    db_dsn_ref: str | None = None,
) -> AssertionEvalResult:
    """Evaluate a ScenarioAssertion (22 executable operators + custom) against an HTTP response."""
    if isinstance(assertion, ScenarioAssertion):
        aid = assertion.id
        title = assertion.title
        op = str(assertion.operator).strip().lower()
        jsonpath = str(assertion.jsonpath or "").strip()
        raw_expected = str(assertion.expected or "")
        note = str(assertion.note or "")
        assertion_dsn_ref = assertion.db_dsn_ref
    else:
        aid = str(assertion.get("id") or "")
        title = str(assertion.get("title") or "")
        op = str(assertion.get("operator") or "not_empty").strip().lower()
        jsonpath = str(assertion.get("jsonpath") or "").strip()
        raw_expected = "" if assertion.get("expected") is None else str(assertion.get("expected"))
        note = str(assertion.get("note") or "")
        assertion_dsn_ref = assertion.get("db_dsn_ref")

    ctx = context_vars or {}
    expected_rendered = render_template_str(raw_expected, ctx)
    eff_dsn_ref = (assertion_dsn_ref or "").strip() or (db_dsn_ref or "").strip() or None

    def _res(
        passed: bool,
        actual: Any = None,
        message: str = "",
        expected_out: str | None = None,
    ) -> AssertionEvalResult:
        return AssertionEvalResult(
            id=aid,
            title=title,
            operator=op,
            passed=passed,
            actual=actual,
            expected=expected_rendered if expected_out is None else expected_out,
            message=message,
        )

    if op == "custom":
        msg = "Custom manual rule noted" + (f": {note}" if note else "")
        return _res(True, actual=None, message=msg, expected_out=raw_expected)

    if op == "status_code":
        exp = expected_rendered.strip().lower()
        passed = False
        if re.fullmatch(r"[1-5]xx", exp):
            base = int(exp[0]) * 100
            passed = base <= status_code < base + 100
        elif "," in exp:
            codes: set[int] = set()
            for part in exp.split(","):
                p = part.strip()
                if p.isdigit():
                    codes.add(int(p))
            passed = status_code in codes
        elif exp.isdigit():
            passed = status_code == int(exp)
        return _res(
            passed,
            actual=status_code,
            message=(
                f"HTTP status {status_code} matched {expected_rendered}"
                if passed
                else f"Expected HTTP status {expected_rendered}, got {status_code}"
            ),
        )

    if op == "header_equals":
        hdr_name = jsonpath.strip()
        if not hdr_name:
            return _res(False, actual=None, message="Header name (jsonpath) is required for header_equals.")
        lower_hdrs = {str(k).lower(): str(v) for k, v in (response_headers or {}).items()}
        actual_hdr = lower_hdrs.get(hdr_name.lower())
        if actual_hdr is None:
            return _res(False, actual=None, message=f"Response header '{hdr_name}' not found.")
        passed = actual_hdr.strip() == expected_rendered.strip()
        return _res(
            passed,
            actual=actual_hdr,
            message=(
                f"Header '{hdr_name}' matched '{expected_rendered}'"
                if passed
                else f"Header '{hdr_name}' was '{actual_hdr}', expected '{expected_rendered}'"
            ),
        )

    if op == "latency_lt":
        threshold = _to_number(expected_rendered)
        if threshold is None:
            return _res(
                False,
                actual=duration_ms,
                message=f"Invalid numeric latency threshold: '{expected_rendered}'",
            )
        passed = float(duration_ms) < threshold
        return _res(
            passed,
            actual=duration_ms,
            message=(
                f"Latency {duration_ms:.2f}ms < {threshold}ms"
                if passed
                else f"Latency {duration_ms:.2f}ms exceeded {threshold}ms"
            ),
        )

    if op in {"sql_equals", "sql_exists"}:
        sql_query = jsonpath or (note if note.strip().lower().startswith("select") else "")
        ok, rows, sql_msg = execute_readonly_sql(
            sql_query,
            context_vars=ctx,
            db_dsn_ref=eff_dsn_ref,
        )
        if not ok:
            return _res(False, actual=None, message=sql_msg)
        if op == "sql_exists":
            count = len(rows)
            passed = count >= 1
            return _res(
                passed,
                actual=count,
                message=f"SQL query returned {count} row(s)",
            )
        if not rows or len(rows[0]) == 0:
            return _res(False, actual=None, message="SQL query returned 0 rows.")
        scalar = rows[0][0]
        passed = _values_equal(scalar, expected_rendered)
        return _res(
            passed,
            actual=scalar,
            message=(
                f"SQL scalar {scalar!r} matched {expected_rendered!r}"
                if passed
                else f"SQL scalar {scalar!r} did not equal {expected_rendered!r}"
            ),
        )

    # Left-hand {{var}} resolution for var_equals
    if op == "var_equals" and "{{" in jsonpath:
        lhs_single = _SINGLE_VAR_RE.match(jsonpath)
        if lhs_single:
            lhs_name = lhs_single.group(1)
            if lhs_name not in ctx:
                return _res(
                    False,
                    actual=None,
                    expected_out=raw_expected,
                    message=f"Left-hand context variable '{lhs_name}' not found.",
                )
            found, actual = True, ctx[lhs_name]
        else:
            lhs_arith = safe_eval_arithmetic(jsonpath, ctx)
            if lhs_arith is not None:
                found, actual = True, lhs_arith
            else:
                rendered_lhs = render_template_str(jsonpath, ctx)
                if _TEMPLATE_VAR_RE.search(rendered_lhs):
                    return _res(
                        False,
                        actual=None,
                        expected_out=raw_expected,
                        message=f"Unresolved left-hand variable in var_equals: '{jsonpath}'",
                    )
                found, actual = True, rendered_lhs
    elif jsonpath:
        found, actual = extract_jsonpath(body_json, jsonpath)
    else:
        found = True
        actual = body_json if body_json is not None else body_text

    if op == "is_empty":
        passed = (not found) or _is_empty_value(actual)
        return _res(
            passed,
            actual=actual if found else None,
            message="Value is empty" if passed else f"Expected empty value, got {actual!r}",
        )

    if op == "not_empty":
        passed = found and not _is_empty_value(actual)
        return _res(
            passed,
            actual=actual if found else None,
            message="Value is non-empty" if passed else f"Expected non-empty value at '{jsonpath or '$'}'",
        )

    if not found:
        return _res(False, actual=None, message=f"JSONPath '{jsonpath}' not found in response body.")

    if op == "equals":
        passed = _values_equal(actual, expected_rendered)
        return _res(
            passed,
            actual=actual,
            message="Values matched" if passed else f"Expected {expected_rendered!r}, got {actual!r}",
        )

    if op == "not_equals":
        passed = not _values_equal(actual, expected_rendered)
        return _res(
            passed,
            actual=actual,
            message="Values differed as expected" if passed else f"Expected value != {expected_rendered!r}",
        )

    if op in {"gt", "gte", "lt", "lte"}:
        dec_actual = _to_decimal(actual)
        dec_expected = _to_decimal(expected_rendered)
        if dec_actual is None or dec_expected is None:
            return _res(
                False,
                actual=actual,
                message=f"Numeric comparison '{op}' requires numeric actual ({actual!r}) and expected ({expected_rendered!r}).",
            )
        cmp_fns = {
            "gt": lambda a, b: a > b,
            "gte": lambda a, b: a >= b,
            "lt": lambda a, b: a < b,
            "lte": lambda a, b: a <= b,
        }
        passed = cmp_fns[op](dec_actual, dec_expected)
        return _res(
            passed,
            actual=actual,
            message=(
                f"{dec_actual} {op} {dec_expected} passed"
                if passed
                else f"Expected {dec_actual} {op} {dec_expected}"
            ),
        )

    if op == "contains":
        passed = _contains_check(actual, expected_rendered)
        return _res(
            passed,
            actual=actual,
            message=(
                f"Contained {expected_rendered!r}"
                if passed
                else f"Value did not contain {expected_rendered!r}"
            ),
        )

    if op == "not_contains":
        passed = not _contains_check(actual, expected_rendered)
        return _res(
            passed,
            actual=actual,
            message=(
                f"Did not contain {expected_rendered!r}"
                if passed
                else f"Value unexpectedly contained {expected_rendered!r}"
            ),
        )

    if op == "matches":
        try:
            passed = re.search(expected_rendered, str(actual)) is not None
        except re.error as exc:
            return _res(False, actual=actual, message=f"Invalid regex pattern: {exc}")
        return _res(
            passed,
            actual=actual,
            message=(
                f"Matched regex /{expected_rendered}/"
                if passed
                else f"Value {actual!r} did not match regex /{expected_rendered}/"
            ),
        )

    if op == "array_contains":
        if not isinstance(actual, list):
            return _res(False, actual=actual, message=f"Expected array at '{jsonpath or '$'}', got {type(actual).__name__}.")
        try:
            target_item = json.loads(expected_rendered)
        except Exception:
            target_item = expected_rendered
        if isinstance(target_item, dict):
            passed = any(
                isinstance(elem, dict) and _dict_is_subset(target_item, elem)
                for elem in actual
            )
        else:
            passed = any(
                _values_equal_any(elem, target_item) or _values_equal(elem, expected_rendered)
                for elem in actual
            )
        return _res(
            passed,
            actual=actual,
            message=(
                f"Array contained {expected_rendered}"
                if passed
                else f"Array did not contain {expected_rendered}"
            ),
        )

    if op in {"length_eq", "length_gt"}:
        if not isinstance(actual, (list, dict, str)):
            return _res(
                False,
                actual=actual,
                message=f"Operator '{op}' requires array, object, or string; got {type(actual).__name__}.",
            )
        try:
            exp_len = int(expected_rendered.strip())
        except ValueError:
            return _res(False, actual=len(actual), message=f"Invalid integer length: '{expected_rendered}'")
        act_len = len(actual)
        passed = (act_len == exp_len) if op == "length_eq" else (act_len > exp_len)
        return _res(
            passed,
            actual=act_len,
            message=(
                f"Length {act_len} satisfied {op} {exp_len}"
                if passed
                else f"Length {act_len} failed {op} {exp_len}"
            ),
        )

    if op == "type_is":
        passed = _check_json_type(actual, expected_rendered)
        return _res(
            passed,
            actual=type(actual).__name__,
            message=(
                f"Type matched '{expected_rendered}'"
                if passed
                else f"Expected type '{expected_rendered}', got '{type(actual).__name__}'"
            ),
        )

    if op == "json_schema":
        try:
            schema_obj = json.loads(expected_rendered)
            if not isinstance(schema_obj, dict):
                return _res(False, actual=actual, message="JSON Schema must be a JSON object.")
        except Exception as exc:
            return _res(False, actual=actual, message=f"Invalid JSON Schema JSON: {exc}")
        passed, msg = _validate_json_schema_value(actual, schema_obj)
        return _res(passed, actual=actual, message=msg)

    if op == "var_equals":
        raw_exp_stripped = raw_expected.strip()
        single_match = _SINGLE_VAR_RE.match(raw_exp_stripped)
        if single_match:
            var_name = single_match.group(1)
            if var_name not in ctx:
                return _res(
                    False,
                    actual=actual,
                    message=f"Context variable '{var_name}' not found.",
                    expected_out=raw_expected,
                )
            exp_val = ctx[var_name]
            passed = _values_equal_any(actual, exp_val)
            return _res(
                passed,
                actual=actual,
                expected_out=str(exp_val),
                message=(
                    f"Matched context var '{var_name}' ({exp_val!r})"
                    if passed
                    else f"Expected {exp_val!r} (from {{{{{var_name}}}}}), got {actual!r}"
                ),
            )

        # Exact Decimal arithmetic evaluator when expression contains an arithmetic operator
        if any(ch in raw_exp_stripped for ch in "+-*/()"):
            arith_dec = safe_eval_arithmetic_decimal(raw_exp_stripped, ctx)
            if arith_dec is not None:
                act_dec = _to_decimal(actual)
                if act_dec is None:
                    return _res(
                        False,
                        actual=actual,
                        expected_out=str(arith_dec),
                        message=f"Expected numeric value {arith_dec}, got non-numeric {actual!r}",
                    )
                passed = act_dec == arith_dec
                return _res(
                    passed,
                    actual=actual,
                    expected_out=str(arith_dec),
                    message=(
                        f"Matched expression '{raw_exp_stripped}' = {arith_dec}"
                        if passed
                        else f"Expected {arith_dec} (from '{raw_exp_stripped}'), got {act_dec}"
                    ),
                )

        if _TEMPLATE_VAR_RE.search(expected_rendered):
            return _res(
                False,
                actual=actual,
                expected_out=raw_expected,
                message=f"Unresolved variable or invalid expression in var_equals: '{raw_expected}'",
            )
        passed = _values_equal(actual, expected_rendered)
        return _res(
            passed,
            actual=actual,
            message=(
                f"Matched resolved value {expected_rendered!r}"
                if passed
                else f"Expected {expected_rendered!r}, got {actual!r}"
            ),
        )

    return _res(False, actual=actual, message=f"Unsupported assertion operator: '{op}'")
