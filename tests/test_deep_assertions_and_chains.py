"""Comprehensive tests for Deep Assertions (22 operators + SQL), Java Codegen, Chain Runner Checkpoint/Resume, Replay Assertions, and MCP Chain Tools."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from reconqa.config import settings
from reconqa.mcp_server import create_mcp_server
from reconqa.models.schemas import ScenarioAssertion
from reconqa.modules.codegen_java.generator import _compile_scenario_assertion_java
from reconqa.modules.hub.assertion_engine import (
    evaluate_assertion,
    execute_readonly_sql,
    extract_jsonpath,
    safe_eval_arithmetic,
)

# ---------------------------------------------------------------------------
# Helpers & Local Stateful Order/Payment HTTP + SQLite Server
# ---------------------------------------------------------------------------


def _extract_mcp_json(raw: Any) -> Any:
    structured = getattr(raw, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    if hasattr(raw, "content"):
        raw = raw.content
    if isinstance(raw, tuple) and len(raw) == 2:
        content, structured_part = raw
        if isinstance(structured_part, dict):
            return structured_part
        raw = content
    if isinstance(raw, list) and raw:
        first = raw[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            return json.loads(text)
    if isinstance(raw, dict):
        return raw
    raise AssertionError(f"Unexpected MCP result shape: {type(raw)} {raw!r}")


class StatefulOrderServer:
    """Thread-safe local HTTP server backed by a real SQLite DB for Chain Runner E2E tests."""

    def __init__(self, db_path: Path, required_token: str = "Bearer super-secret-chain-token-999") -> None:
        self.db_path = db_path
        self.required_token = required_token
        self.calls: dict[str, int] = {"create_order": 0, "pay_order": 0, "get_order": 0}
        self._lock = threading.Lock()
        self._order_seq = 100
        self._init_sqlite()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _init_sqlite(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    amount INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    balance_after INTEGER NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> StatefulOrderServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send_json(self, status_code: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Service-Name", "OrderEngine")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw.decode("utf-8"))
                    return data if isinstance(data, dict) else {}
                except Exception:
                    return {}

            def _check_auth(self) -> bool:
                auth = self.headers.get("Authorization", "")
                if auth != outer.required_token:
                    self._send_json(401, {"code": 401, "message": "Unauthorized"})
                    return False
                return True

            def do_POST(self) -> None:  # noqa: N802
                if not self._check_auth():
                    return
                path = self.path.split("?", 1)[0]
                payload = self._read_json()

                if path == "/orders":
                    with outer._lock:
                        outer.calls["create_order"] += 1
                        outer._order_seq += 1
                        order_id = f"ORD-{outer._order_seq}"
                    amount = int(payload.get("amount", 50))
                    balance_before = 200
                    conn = sqlite3.connect(outer.db_path)
                    try:
                        conn.execute(
                            "INSERT INTO orders (id, amount, status, balance_after) VALUES (?, ?, ?, ?)",
                            (order_id, amount, "PENDING", balance_before),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    self._send_json(
                        201,
                        {
                            "code": 0,
                            "data": {
                                "order_id": order_id,
                                "amount": amount,
                                "balance_before": balance_before,
                                "status": "PENDING",
                                "items": [{"sku": "SKU-1", "qty": 1}],
                            },
                        },
                    )
                    return

                if path.startswith("/orders/") and path.endswith("/pay"):
                    order_id = path[len("/orders/") : -len("/pay")]
                    with outer._lock:
                        outer.calls["pay_order"] += 1
                    pay_code = str(payload.get("pay_code") or "")
                    if pay_code != "VALID-PAY":
                        self._send_json(
                            400,
                            {
                                "code": 4001,
                                "message": f"Invalid pay_code '{pay_code}' for {order_id}",
                            },
                        )
                        return
                    conn = sqlite3.connect(outer.db_path)
                    try:
                        row = conn.execute(
                            "SELECT amount, balance_after FROM orders WHERE id = ?",
                            (order_id,),
                        ).fetchone()
                        if row is None:
                            self._send_json(404, {"code": 404, "message": "Order not found"})
                            return
                        amount, bal_before = int(row[0]), int(row[1])
                        bal_after = bal_before - amount
                        conn.execute(
                            "UPDATE orders SET status = ?, balance_after = ? WHERE id = ?",
                            ("PAID", bal_after, order_id),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    self._send_json(
                        200,
                        {
                            "code": 0,
                            "data": {
                                "order_id": order_id,
                                "status": "PAID",
                                "balance_after": bal_after,
                            },
                        },
                    )
                    return

                self._send_json(404, {"code": 404, "message": "Not Found"})

            def do_GET(self) -> None:  # noqa: N802
                if not self._check_auth():
                    return
                path = self.path.split("?", 1)[0]
                if path.startswith("/orders/"):
                    order_id = path[len("/orders/") :]
                    with outer._lock:
                        outer.calls["get_order"] += 1
                    conn = sqlite3.connect(outer.db_path)
                    try:
                        row = conn.execute(
                            "SELECT id, amount, status, balance_after FROM orders WHERE id = ?",
                            (order_id,),
                        ).fetchone()
                    finally:
                        conn.close()
                    if row is None:
                        self._send_json(404, {"code": 404, "message": "Not Found"})
                        return
                    self._send_json(
                        200,
                        {
                            "code": 0,
                            "data": {
                                "order_id": row[0],
                                "amount": row[1],
                                "status": row[2],
                                "balance_after": row[3],
                                "tags": ["express", "verified"],
                            },
                        },
                    )
                    return
                self._send_json(404, {"code": 404, "message": "Not Found"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


@pytest.fixture()
def order_server(tmp_path: Path) -> Iterator[StatefulOrderServer]:
    db_path = tmp_path / "orders_e2e.db"
    with StatefulOrderServer(db_path=db_path) as srv:
        yield srv


# ---------------------------------------------------------------------------
# 1. Deep Assertion Engine Unit Tests (All New Operators + JSONPath + Arithmetic)
# ---------------------------------------------------------------------------


def test_assertion_engine_all_new_operators() -> None:
    body = {
        "code": 0,
        "empty_str": "",
        "empty_list": [],
        "message": "order created successfully",
        "items": [
            {"id": 101, "sku": "BOOK", "price": 25},
            {"id": 102, "sku": "PEN", "price": 5},
        ],
        "tags": ["vip", "fast"],
        "balance": 150,
        "active": True,
        "meta": {"region": "us-east"},
    }
    headers = {"Content-Type": "application/json", "X-Trace-Id": "trace-abc-123"}

    # JSONPath wildcard support
    found_ids, ids = extract_jsonpath(body, "$.items[*].id")
    assert found_ids is True
    assert ids == [101, 102]

    # is_empty
    r = evaluate_assertion(
        ScenarioAssertion(title="empty list", jsonpath="$.empty_list", operator="is_empty"),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    )
    assert r.passed is True
    r_fail = evaluate_assertion(
        ScenarioAssertion(title="non-empty tags", jsonpath="$.tags", operator="is_empty"),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    )
    assert r_fail.passed is False

    # not_contains
    r = evaluate_assertion(
        ScenarioAssertion(title="no error word", jsonpath="$.message", operator="not_contains", expected="failed"),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    )
    assert r.passed is True
    r_fail = evaluate_assertion(
        ScenarioAssertion(title="contains created", jsonpath="$.message", operator="not_contains", expected="created"),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    )
    assert r_fail.passed is False

    # array_contains (scalar + dict subset)
    r_scalar = evaluate_assertion(
        ScenarioAssertion(title="has vip tag", jsonpath="$.tags", operator="array_contains", expected="vip"),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    )
    assert r_scalar.passed is True
    r_subset = evaluate_assertion(
        ScenarioAssertion(
            title="has book item",
            jsonpath="$.items",
            operator="array_contains",
            expected='{"sku": "BOOK", "price": 25}',
        ),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    )
    assert r_subset.passed is True
    r_subset_miss = evaluate_assertion(
        ScenarioAssertion(
            title="missing item",
            jsonpath="$.items",
            operator="array_contains",
            expected='{"sku": "LAPTOP"}',
        ),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    )
    assert r_subset_miss.passed is False

    # length_eq & length_gt
    assert evaluate_assertion(
        ScenarioAssertion(title="2 items", jsonpath="$.items", operator="length_eq", expected="2"),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    ).passed is True
    assert evaluate_assertion(
        ScenarioAssertion(title=">1 items", jsonpath="$.items", operator="length_gt", expected="1"),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    ).passed is True
    assert evaluate_assertion(
        ScenarioAssertion(title=">2 items fails", jsonpath="$.items", operator="length_gt", expected="2"),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    ).passed is False

    # type_is
    for jp, exp_t in [
        ("$.items", "array"),
        ("$.meta", "object"),
        ("$.balance", "integer"),
        ("$.active", "boolean"),
        ("$.message", "string"),
    ]:
        res = evaluate_assertion(
            ScenarioAssertion(title=f"type {exp_t}", jsonpath=jp, operator="type_is", expected=exp_t),
            status_code=200,
            duration_ms=42.0,
            response_headers=headers,
            body_json=body,
            body_text=json.dumps(body),
        )
        assert res.passed is True, res.message

    # status_code (exact, csv, 2xx)
    for exp_s, ok in [("200", True), ("200,201", True), ("2xx", True), ("201", False)]:
        res = evaluate_assertion(
            ScenarioAssertion(title="status", operator="status_code", expected=exp_s),
            status_code=200,
            duration_ms=42.0,
            response_headers=headers,
            body_json=body,
            body_text=json.dumps(body),
        )
        assert res.passed is ok

    # header_equals (case-insensitive)
    r_hdr = evaluate_assertion(
        ScenarioAssertion(title="trace hdr", jsonpath="x-trace-id", operator="header_equals", expected="trace-abc-123"),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    )
    assert r_hdr.passed is True

    # latency_lt
    assert evaluate_assertion(
        ScenarioAssertion(title="fast", operator="latency_lt", expected="100"),
        status_code=200,
        duration_ms=42.5,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    ).passed is True
    assert evaluate_assertion(
        ScenarioAssertion(title="too slow", operator="latency_lt", expected="20"),
        status_code=200,
        duration_ms=42.5,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    ).passed is False

    # json_schema
    schema = json.dumps(
        {
            "type": "object",
            "required": ["code", "items", "balance"],
            "properties": {
                "code": {"type": "integer"},
                "balance": {"type": "number"},
                "items": {"type": "array"},
            },
        }
    )
    assert evaluate_assertion(
        ScenarioAssertion(title="schema ok", operator="json_schema", expected=schema),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    ).passed is True

    bad_schema = json.dumps({"type": "object", "required": ["nonexistent_field"]})
    assert evaluate_assertion(
        ScenarioAssertion(title="schema fail", operator="json_schema", expected=bad_schema),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
    ).passed is False

    # var_equals (direct + safe AST arithmetic)
    assert evaluate_assertion(
        ScenarioAssertion(title="balance arithmetic", jsonpath="$.balance", operator="var_equals", expected="{{before}} - 50"),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
        context_vars={"before": 200},
    ).passed is True
    assert evaluate_assertion(
        ScenarioAssertion(title="direct var", jsonpath="$.meta.region", operator="var_equals", expected="{{expected_region}}"),
        status_code=200,
        duration_ms=42.0,
        response_headers=headers,
        body_json=body,
        body_text=json.dumps(body),
        context_vars={"expected_region": "us-east"},
    ).passed is True

    # AST safety: reject arbitrary code execution attempts
    assert safe_eval_arithmetic("__import__('os').system('echo hacked')", {}) is None
    assert safe_eval_arithmetic("100 - 25 * 2", {}) == 50.0


# ---------------------------------------------------------------------------
# 2. Read-Only SQL Cross-Check & Security Tests
# ---------------------------------------------------------------------------


def test_readonly_sql_assertions_and_security(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = tmp_path / "sql_check.db"
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("CREATE TABLE orders (id TEXT PRIMARY KEY, status TEXT, amount INT)")
        conn.execute("INSERT INTO orders VALUES ('ORD-1', 'PAID', 99)")
        conn.execute("INSERT INTO orders VALUES ('ORD-2', 'CANCELLED', 10)")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("RECONQA_TEST_DB_DSN", f"sqlite:///{db_file}")

    # Pass: sql_equals with parameterized {{order_id}}
    r_ok = evaluate_assertion(
        ScenarioAssertion(
            title="DB order status PAID",
            jsonpath="SELECT status FROM orders WHERE id = {{order_id}}",
            operator="sql_equals",
            expected="PAID",
            db_dsn_ref="ENV:RECONQA_TEST_DB_DSN",
        ),
        status_code=200,
        duration_ms=10.0,
        response_headers={},
        body_json={},
        body_text="{}",
        context_vars={"order_id": "ORD-1"},
    )
    assert r_ok.passed is True
    assert r_ok.actual == "PAID"

    # Pass: sql_exists
    r_exists = evaluate_assertion(
        ScenarioAssertion(
            title="DB row exists",
            jsonpath="SELECT 1 FROM orders WHERE id = {{order_id}} AND amount = {{amount}}",
            operator="sql_exists",
            db_dsn_ref="${RECONQA_TEST_DB_DSN}",
        ),
        status_code=200,
        duration_ms=10.0,
        response_headers={},
        body_json={},
        body_text="{}",
        context_vars={"order_id": "ORD-1", "amount": 99},
    )
    assert r_exists.passed is True
    assert r_exists.actual == 1

    # Fail cleanly on mismatch
    r_mismatch = evaluate_assertion(
        ScenarioAssertion(
            title="DB status mismatch",
            jsonpath="SELECT status FROM orders WHERE id = {{order_id}}",
            operator="sql_equals",
            expected="REFUNDED",
            db_dsn_ref="ENV:RECONQA_TEST_DB_DSN",
        ),
        status_code=200,
        duration_ms=10.0,
        response_headers={},
        body_json={},
        body_text="{}",
        context_vars={"order_id": "ORD-1"},
    )
    assert r_mismatch.passed is False
    assert r_mismatch.actual == "PAID"

    # Security 1: Reject DELETE / UPDATE / DROP / INSERT / multi-statement ;
    dangerous_queries = [
        "DELETE FROM orders WHERE id = 'ORD-1'",
        "UPDATE orders SET status = 'HACKED'",
        "DROP TABLE orders",
        "SELECT status FROM orders; DROP TABLE orders",
        "/* comment */ DELETE FROM orders",
        "SELECT * FROM orders WHERE 1=1; SELECT 1",
    ]
    for bad_q in dangerous_queries:
        ok, rows, msg = execute_readonly_sql(
            bad_q,
            context_vars={},
            db_dsn_ref="ENV:RECONQA_TEST_DB_DSN",
        )
        assert ok is False, f"Should have rejected query: {bad_q}"
        assert rows is None
        assert msg

    # Security 2: Reject plaintext non-ENV DSN & mask on serialization
    plaintext_assertion = ScenarioAssertion(
        title="Plaintext DSN blocked",
        jsonpath="SELECT status FROM orders WHERE id = 'ORD-1'",
        operator="sql_equals",
        expected="PAID",
        db_dsn_ref=f"sqlite:///{db_file}",
    )
    r_plain = evaluate_assertion(
        plaintext_assertion,
        status_code=200,
        duration_ms=10.0,
        response_headers={},
        body_json={},
        body_text="{}",
    )
    assert r_plain.passed is False
    assert "ENV:RECONQA_" in r_plain.message
    dumped = plaintext_assertion.model_dump()
    assert dumped["db_dsn_ref"] != f"sqlite:///{db_file}"
    assert "•••" in str(dumped["db_dsn_ref"]) or dumped["db_dsn_ref"] == "***"

    # Security 3: Prevent SQL injection via {{order_id}} containing ' OR '1'='1
    r_sqli = evaluate_assertion(
        ScenarioAssertion(
            title="SQLi attempt blocked via parameterization",
            jsonpath="SELECT status FROM orders WHERE id = {{order_id}}",
            operator="sql_exists",
            db_dsn_ref="ENV:RECONQA_TEST_DB_DSN",
        ),
        status_code=200,
        duration_ms=10.0,
        response_headers={},
        body_json={},
        body_text="{}",
        context_vars={"order_id": "' OR '1'='1"},
    )
    assert r_sqli.passed is False
    assert r_sqli.actual == 0

    # Verify table intact
    conn = sqlite3.connect(db_file)
    try:
        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        assert count == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Java Codegen Compiles All New Operators Cleanly
# ---------------------------------------------------------------------------


def test_java_codegen_compiles_all_new_operators() -> None:
    cases = [
        {"title": "empty", "operator": "is_empty", "jsonpath": "$.errors", "expected": ""},
        {"title": "no err", "operator": "not_contains", "jsonpath": "$.msg", "expected": "error"},
        {"title": "arr item", "operator": "array_contains", "jsonpath": "$.tags", "expected": "vip"},
        {"title": "arr subset", "operator": "array_contains", "jsonpath": "$.items", "expected": '{"id": 1}'},
        {"title": "len eq", "operator": "length_eq", "jsonpath": "$.items", "expected": "2"},
        {"title": "len gt", "operator": "length_gt", "jsonpath": "$.items", "expected": "0"},
        {"title": "type", "operator": "type_is", "jsonpath": "$.items", "expected": "array"},
        {"title": "status", "operator": "status_code", "jsonpath": "", "expected": "201"},
        {"title": "header", "operator": "header_equals", "jsonpath": "X-Service-Name", "expected": "OrderEngine"},
        {"title": "latency", "operator": "latency_lt", "jsonpath": "", "expected": "500"},
        {"title": "var eq", "operator": "var_equals", "jsonpath": "$.balance", "expected": "{{before}} - 50"},
        {"title": "schema", "operator": "json_schema", "jsonpath": "", "expected": '{"type":"object"}'},
        {
            "title": "sql eq",
            "operator": "sql_equals",
            "jsonpath": "SELECT status FROM orders WHERE id = {{order_id}}",
            "expected": "PAID",
        },
        {
            "title": "sql exists",
            "operator": "sql_exists",
            "jsonpath": "SELECT 1 FROM orders WHERE id = {{order_id}}",
            "expected": "",
        },
        {"title": "custom", "operator": "custom", "jsonpath": "", "expected": "", "note": "Check audit log"},
    ]

    compiled_map: dict[str, list[str]] = {}
    for case in cases:
        res = _compile_scenario_assertion_java(case)
        assert res["comment"].startswith("// Business Assertion")
        assert isinstance(res["lines"], list) and len(res["lines"]) >= 1
        compiled_map[case["title"]] = res["lines"]

    assert any("Matchers.emptyOrNullString()" in ln for ln in compiled_map["empty"])
    assert any("Matchers.not(" in ln and "containsString" in ln for ln in compiled_map["no err"])
    assert any("Matchers.hasItem" in ln for ln in compiled_map["arr item"])
    assert any("Matchers.hasSize(2)" in ln for ln in compiled_map["len eq"])
    assert any("Matchers.hasSize(Matchers.greaterThan(0))" in ln for ln in compiled_map["len gt"])
    assert any("Matchers.instanceOf(java.util.List.class)" in ln for ln in compiled_map["type"])
    assert any("statusCode(201)" in ln for ln in compiled_map["status"])
    assert any('.header("X-Service-Name"' in ln for ln in compiled_map["header"])
    assert any("response.time()" in ln and "Matchers.lessThan(Long.valueOf(500L))" in ln for ln in compiled_map["latency"])
    assert any("TODO Cross-Step Var Assertion" in ln for ln in compiled_map["var eq"])
    assert any("JsonSchemaValidator.matchesJsonSchema" in ln for ln in compiled_map["schema"])
    assert any("TODO Read-Only SQL Verification" in ln for ln in compiled_map["sql eq"])
    assert any("TODO Read-Only SQL Verification" in ln for ln in compiled_map["sql exists"])


# ---------------------------------------------------------------------------
# 4, 5, 6. FastAPI Inline Replay Assertions + Chain Runner Checkpoint/Resume + MCP Tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_assertions_chain_checkpoint_resume_and_mcp(
    order_server: StatefulOrderServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "chains_test.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("RECONQA_TEST_ORDER_DB", f"sqlite:///{order_server.db_path}")

    from reconqa.models import db as db_mod

    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    from reconqa.server.app import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Create project + target + auth profile
            proj_resp = await client.post(
                "/api/v1/projects",
                json={"name": "Order Checkout QA", "description": "Deep assertion & chain resume test"},
            )
            assert proj_resp.status_code == 201
            project_id = proj_resp.json()["id"]

            await client.post(
                f"/api/v1/projects/{project_id}/targets",
                json={"root_domain": "127.0.0.1", "base_url": order_server.base_url},
            )
            prof_resp = await client.post(
                f"/api/v1/projects/{project_id}/auth-profiles",
                json={
                    "name": "Buyer Token",
                    "role_tag": "buyer",
                    "auth_type": "headers",
                    "extra_headers_ref": {"Authorization": order_server.required_token},
                },
            )
            assert prof_resp.status_code == 201
            cred_id = prof_resp.json()["id"]

            # --- 4. Inline Replay with Deep Assertions ---
            replay_resp = await client.post(
                f"/api/v1/projects/{project_id}/replay",
                json={
                    "method": "POST",
                    "url": f"{order_server.base_url}/orders",
                    "body": {"amount": 30},
                    "credential_id": cred_id,
                    "assertions": [
                        {"title": "201 Created", "operator": "status_code", "expected": "201"},
                        {"title": "Service header", "operator": "header_equals", "jsonpath": "X-Service-Name", "expected": "OrderEngine"},
                        {"title": "Fast response", "operator": "latency_lt", "expected": "5000"},
                        {"title": "Amount matches", "operator": "equals", "jsonpath": "$.data.amount", "expected": "30"},
                        {"title": "Items array len", "operator": "length_eq", "jsonpath": "$.data.items", "expected": "1"},
                    ],
                },
            )
            assert replay_resp.status_code == 200
            replay_data = replay_resp.json()
            assert replay_data["status_code"] == 201
            assert len(replay_data["assertion_results"]) == 5
            assert all(r["passed"] is True for r in replay_data["assertion_results"])

            # Reset call counters before Chain Runner test
            order_server.calls = {"create_order": 0, "pay_order": 0, "get_order": 0}

            # --- 5. Chain Runner + Checkpoint & Resume (断点续跑) ---
            steps = [
                {
                    "step": 1,
                    "name": "Create Order",
                    "method": "POST",
                    "path": "/orders",
                    "request_body": {"amount": 50},
                    "expected_status": 201,
                    "extracts": {
                        "order_id": "$.data.order_id",
                        "amount": "$.data.amount",
                        "balance_before": "$.data.balance_before",
                    },
                    "assertions": [
                        {"title": "Status PENDING", "jsonpath": "$.data.status", "operator": "equals", "expected": "PENDING"},
                    ],
                },
                {
                    "step": 2,
                    "name": "Pay Order",
                    "method": "POST",
                    "path": "/orders/{order_id}/pay",
                    "request_body": {"pay_code": "WRONG-CODE"},
                    "expected_status": 200,
                    "assertions": [
                        {"title": "Paid status", "jsonpath": "$.data.status", "operator": "equals", "expected": "PAID"},
                    ],
                },
                {
                    "step": 3,
                    "name": "Verify Order & DB",
                    "method": "GET",
                    "path": "/orders/{{order_id}}",
                    "expected_status": 200,
                    "assertions": [
                        {
                            "title": "Balance deducted by amount",
                            "jsonpath": "$.data.balance_after",
                            "operator": "var_equals",
                            "expected": "{{balance_before}} - {{amount}}",
                        },
                        {
                            "title": "Tags contains verified",
                            "jsonpath": "$.data.tags",
                            "operator": "array_contains",
                            "expected": "verified",
                        },
                        {
                            "title": "SQL DB status PAID",
                            "jsonpath": "SELECT status FROM orders WHERE id = {{order_id}}",
                            "operator": "sql_equals",
                            "expected": "PAID",
                        },
                        {
                            "title": "SQL DB row exists",
                            "jsonpath": "SELECT 1 FROM orders WHERE id = {{order_id}} AND balance_after = 150",
                            "operator": "sql_exists",
                        },
                    ],
                },
            ]

            exec_resp = await client.post(
                f"/api/v1/projects/{project_id}/chains/execute",
                json={
                    "chain_name": "Checkout E2E Chain",
                    "steps": steps,
                    "base_url": order_server.base_url,
                    "credential_id": cred_id,
                    "db_dsn_ref": "ENV:RECONQA_TEST_ORDER_DB",
                    "stop_on_failure": True,
                },
            )
            assert exec_resp.status_code == 200
            run1 = exec_resp.json()
            chain_run_id = run1["chain_run_id"]
            assert run1["status"] == "failed"
            assert run1["total_steps"] == 3
            assert run1["completed_steps"] == 1
            assert run1["failed_step_index"] == 1
            assert run1["step_results"][0]["status"] == "passed"
            assert run1["step_results"][1]["status"] == "failed"
            assert run1["step_results"][2]["status"] == "skipped"
            created_order_id = run1["context_vars"]["order_id"]
            assert created_order_id.startswith("ORD-")
            assert order_server.calls == {"create_order": 1, "pay_order": 1, "get_order": 0}

            # Verify checkpoint file on disk leaks ZERO raw auth tokens!
            cp_file = ws_dir / "chain_runs" / f"{chain_run_id}.json"
            assert cp_file.is_file()
            cp_raw_text = cp_file.read_text(encoding="utf-8")
            assert "super-secret-chain-token-999" not in cp_raw_text

            # Verify GET chain-runs & GET single chain-run
            list_resp = await client.get(f"/api/v1/projects/{project_id}/chain-runs")
            assert list_resp.status_code == 200
            assert any(item["chain_run_id"] == chain_run_id for item in list_resp.json())

            get_resp = await client.get(f"/api/v1/projects/{project_id}/chain-runs/{chain_run_id}")
            assert get_resp.status_code == 200
            assert get_resp.json()["failed_step_index"] == 1

            # Resume from failed step (step_index=1) with fixed pay_code body!
            resume_resp = await client.post(
                f"/api/v1/projects/{project_id}/chain-runs/{chain_run_id}/resume",
                json={
                    "override_step_body": {"pay_code": "VALID-PAY"},
                    "credential_id": cred_id,
                    "db_dsn_ref": "ENV:RECONQA_TEST_ORDER_DB",
                },
            )
            assert resume_resp.status_code == 200
            run2 = resume_resp.json()
            assert run2["status"] == "passed"
            assert run2["completed_steps"] == 3
            assert run2["failed_step_index"] is None
            assert run2["resumed_from_index"] == 1
            assert [s["status"] for s in run2["step_results"]] == ["passed", "passed", "passed"]
            # CRITICAL: Step 1 (create_order) MUST NOT have been re-executed!
            assert order_server.calls == {"create_order": 1, "pay_order": 2, "get_order": 1}
            # Verify Step 3 assertions (var_equals + array_contains + sql_equals + sql_exists + status_code) all passed
            step3_assertions = run2["step_results"][2]["assertions"]
            assert len(step3_assertions) == 5
            assert all(a["passed"] is True for a in step3_assertions)

            # Security: SSRF metadata URL blocked
            ssrf_resp = await client.post(
                f"/api/v1/projects/{project_id}/chains/execute",
                json={
                    "chain_name": "SSRF Attempt",
                    "steps": [{"step": 1, "method": "GET", "raw_url": "http://169.254.169.254/latest/meta-data/"}],
                },
            )
            assert ssrf_resp.status_code == 400

            # --- 6. MCP Tools: reconqa_execute_chain & reconqa_resume_chain ---
            mcp = create_mcp_server("http://testserver", transport=transport)
            tools = await mcp.list_tools()
            tool_names = {t.name for t in tools}
            assert "reconqa_execute_chain" in tool_names
            assert "reconqa_resume_chain" in tool_names

            order_server.calls = {"create_order": 0, "pay_order": 0, "get_order": 0}
            mcp_exec_raw = await mcp.call_tool(
                "reconqa_execute_chain",
                {
                    "project_id": project_id,
                    "chain_name": "MCP Checkout Chain",
                    "steps": steps,
                    "base_url": order_server.base_url,
                    "credential_id": cred_id,
                    "db_dsn_ref": "ENV:RECONQA_TEST_ORDER_DB",
                },
            )
            mcp_exec = _extract_mcp_json(mcp_exec_raw)
            assert mcp_exec["status"] == "failed"
            assert mcp_exec["failed_step_index"] == 1
            assert order_server.calls["create_order"] == 1

            mcp_resume_raw = await mcp.call_tool(
                "reconqa_resume_chain",
                {
                    "project_id": project_id,
                    "chain_run_id": mcp_exec["chain_run_id"],
                    "override_step_body": {"pay_code": "VALID-PAY"},
                    "credential_id": cred_id,
                    "db_dsn_ref": "ENV:RECONQA_TEST_ORDER_DB",
                },
            )
            mcp_resume = _extract_mcp_json(mcp_resume_raw)
            assert mcp_resume["status"] == "passed"
            assert mcp_resume["resumed_from_index"] == 1
            assert mcp_resume["completed_steps"] == 3
            assert order_server.calls == {"create_order": 1, "pay_order": 2, "get_order": 1}


# ---------------------------------------------------------------------------
# 7. Round 1 P1 Regression Tests
# ---------------------------------------------------------------------------


def test_p1_assertion_precision_left_var_json_schema_and_sql_security(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reconqa.modules.hub.assertion_engine import _validate_json_schema_fallback

    # 1. Exact 64-bit snowflake ID inequality in equals / not_equals / var_equals
    sf_a = "1234567890123456789"
    sf_b = "1234567890123456788"
    assert evaluate_assertion(
        ScenarioAssertion(title="64-bit str != ", jsonpath="$.id", operator="equals", expected=sf_b),
        status_code=200,
        duration_ms=5.0,
        response_headers={},
        body_json={"id": sf_a},
        body_text="{}",
    ).passed is False
    assert evaluate_assertion(
        ScenarioAssertion(title="64-bit int != ", jsonpath="$.id", operator="equals", expected=sf_b),
        status_code=200,
        duration_ms=5.0,
        response_headers={},
        body_json={"id": int(sf_a)},
        body_text="{}",
    ).passed is False
    assert evaluate_assertion(
        ScenarioAssertion(title="64-bit var_equals != ", jsonpath="$.id", operator="var_equals", expected="{{other_id}}"),
        status_code=200,
        duration_ms=5.0,
        response_headers={},
        body_json={"id": int(sf_a)},
        body_text="{}",
        context_vars={"other_id": int(sf_b)},
    ).passed is False

    # 2. Left-hand {{var}} in var_equals
    lhs_ok = evaluate_assertion(
        ScenarioAssertion(title="lhs var equals rhs var", jsonpath="{{order_id}}", operator="var_equals", expected="{{expected_id}}"),
        status_code=200,
        duration_ms=5.0,
        response_headers={},
        body_json={},
        body_text="{}",
        context_vars={"order_id": sf_a, "expected_id": sf_a},
    )
    assert lhs_ok.passed is True
    lhs_diff = evaluate_assertion(
        ScenarioAssertion(title="lhs var != rhs var", jsonpath="{{order_id}}", operator="var_equals", expected="{{expected_id}}"),
        status_code=200,
        duration_ms=5.0,
        response_headers={},
        body_json={},
        body_text="{}",
        context_vars={"order_id": sf_a, "expected_id": sf_b},
    )
    assert lhs_diff.passed is False

    # 3. json_schema rejects external $ref (SSRF protection)
    ext_schema = json.dumps({"$ref": "http://169.254.169.254/latest/meta-data/schema.json"})
    ext_res = evaluate_assertion(
        ScenarioAssertion(title="block external ref", operator="json_schema", expected=ext_schema),
        status_code=200,
        duration_ms=5.0,
        response_headers={},
        body_json={"a": 1},
        body_text="{}",
    )
    assert ext_res.passed is False
    assert "External $ref" in ext_res.message

    # 4. Fallback JSON Schema validator validates nested properties, constraints & enums
    nested_schema = {
        "type": "object",
        "required": ["order"],
        "properties": {
            "order": {
                "type": "object",
                "required": ["status", "amount"],
                "properties": {
                    "status": {"type": "string", "enum": ["PENDING", "PAID"]},
                    "amount": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
            }
        },
    }
    ok_fb, _ = _validate_json_schema_fallback({"order": {"status": "PAID", "amount": 50}}, nested_schema)
    assert ok_fb is True
    bad_fb, bad_msg = _validate_json_schema_fallback({"order": {"status": "BOGUS", "amount": 50}}, nested_schema)
    assert bad_fb is False
    assert "enum" in bad_msg
    bad_min, min_msg = _validate_json_schema_fallback({"order": {"status": "PAID", "amount": 0}}, nested_schema)
    assert bad_min is False
    assert "minimum" in min_msg

    # 5. SQL Security: RECONQA_DATABASE_URL rejected, INTO DUMPFILE rejected, password redacted from errors
    monkeypatch.setenv("RECONQA_DATABASE_URL", f"sqlite:///{tmp_path / 'internal.db'}")
    ok_app, _, msg_app = execute_readonly_sql(
        "SELECT 1",
        db_dsn_ref="ENV:RECONQA_DATABASE_URL",
    )
    assert ok_app is False
    assert "internal database" in msg_app.lower()

    # Also reject a custom env var pointing to settings.database_url
    app_db_path = tmp_path / "app_reconqa.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{app_db_path}")
    monkeypatch.setenv("RECONQA_TEST_ALIAS_APP_DB", f"sqlite:///{app_db_path}")
    ok_alias, _, msg_alias = execute_readonly_sql(
        "SELECT 1",
        db_dsn_ref="ENV:RECONQA_TEST_ALIAS_APP_DB",
    )
    assert ok_alias is False
    assert "internal" in msg_alias.lower()

    # Reject INTO DUMPFILE and FOR UPDATE
    test_db = tmp_path / "ro_sec.db"
    sqlite3.connect(test_db).close()
    monkeypatch.setenv("RECONQA_TEST_RO_DB", f"sqlite:///{test_db}")
    for bad_q in [
        "SELECT 1 INTO DUMPFILE '/tmp/pwn.txt'",
        "SELECT * FROM orders FOR UPDATE",
        "SELECT * FROM orders LOCK IN SHARE MODE",
    ]:
        ok_q, _, _ = execute_readonly_sql(bad_q, db_dsn_ref="ENV:RECONQA_TEST_RO_DB")
        assert ok_q is False, f"Expected rejection for: {bad_q}"

    # Password redaction on connection failure
    secret_pw = "MySuperSecretDbPass!987"
    monkeypatch.setenv(
        "RECONQA_TEST_BAD_PG",
        f"postgresql://dbuser:{secret_pw}@127.0.0.1:1/nonexistent_db",
    )
    ok_err, _, err_msg = execute_readonly_sql("SELECT 1", db_dsn_ref="ENV:RECONQA_TEST_BAD_PG")
    assert ok_err is False
    assert secret_pw not in err_msg
    assert "dbuser:" not in err_msg


def test_p1_java_codegen_l2_template_and_nonfinite_latency(tmp_path: Path) -> None:
    from reconqa.modules.codegen_java.generator import generate_java_project

    # latency_lt("inf") and latency_lt("nan") must never crash or produce invalid Java
    for bad_lat in ("inf", "-inf", "nan", "-10"):
        compiled = _compile_scenario_assertion_java(
            {"title": "bad latency", "operator": "latency_lt", "expected": bad_lat}
        )
        assert all("TODO" in ln for ln in compiled["lines"])

    # Generated L2ChainTest.java imports JsonSchemaValidator & compiles json_schema
    openapi_doc = {
        "openapi": "3.0.3",
        "info": {"title": "Test", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.test"}],
        "paths": {
            "/orders": {
                "post": {
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }
    corr_doc = {
        "variables": [],
        "chains": [
            {
                "name": "OrderFlow",
                "scenario": "checkout",
                "steps": [
                    {
                        "step": 1,
                        "scenario": "checkout",
                        "method": "POST",
                        "path": "/orders",
                        "raw_url": "https://api.example.test/orders",
                        "expected_status": 200,
                        "extracts": {},
                        "uses": {},
                    }
                ],
            }
        ],
    }
    openapi_file = tmp_path / "openapi.json"
    corr_file = tmp_path / "correlation.json"
    openapi_file.write_text(json.dumps(openapi_doc), encoding="utf-8")
    corr_file.write_text(json.dumps(corr_doc), encoding="utf-8")
    out_dir = tmp_path / "java_out"
    generate_java_project(
        openapi_path=openapi_file,
        out_dir=out_dir,
        correlation_path=corr_file,
        scenario_assertions=[
            {
                "id": "sc-schema-1",
                "title": "Order response schema",
                "target_endpoint": "POST /orders",
                "jsonpath": "",
                "operator": "json_schema",
                "expected": '{"type":"object"}',
            }
        ],
    )
    l2_files = list((out_dir / "src").rglob("chains/*.java"))
    assert len(l2_files) == 1
    l2_src = l2_files[0].read_text(encoding="utf-8")
    assert "import io.restassured.module.jsv.JsonSchemaValidator;" in l2_src
    assert "io.restassured.module.jsv.JsonSchemaValidator.matchesJsonSchema" in l2_src


@pytest.mark.asyncio
async def test_p1_correlation_bindings_duplicate_chains_cred_resume_redaction_and_scenarios(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2E regression test for correlation.json bindings, duplicate chain_index, credential reuse on resume,
    deep checkpoint redaction, and auto-bound ScenarioModel assertions."""

    recorded_requests: list[dict[str, Any]] = []
    required_auth = "Bearer p1-secret-token-xyz-777"

    class BindingTestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length", "0") or "0")
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("Authorization") != required_auth:
                self._send(401, {"error": "unauthorized"})
                return
            body = self._read_body()
            recorded_requests.append({"method": "POST", "path": self.path, "body": body})
            if self.path.startswith("/api/orders") and "/confirm" not in self.path:
                self._send(201, {"code": 0, "data": {"order_id": "FRESH-9001", "secret_token": "tok-leak-guard-555"}})
                return
            if self.path.startswith("/api/orders/FRESH-9001/confirm"):
                if body.get("data", {}).get("order_id") != "FRESH-9001":
                    self._send(400, {"code": 400, "message": "nested order_id mismatch"})
                    return
                if body.get("action") == "fail_once":
                    self._send(400, {"code": 400, "message": f"failed with token {required_auth}"})
                    return
                self._send(200, {"code": 0, "confirmed": True, "echo_id": "FRESH-9001"})
                return
            self._send(404, {"error": f"unexpected path {self.path}"})

        def do_GET(self) -> None:  # noqa: N802
            if self.headers.get("Authorization") != required_auth:
                self._send(401, {"error": "unauthorized"})
                return
            recorded_requests.append({"method": "GET", "path": self.path, "body": None})
            if self.path == "/api/orders/FRESH-9001?expand=items":
                self._send(200, {"code": 0, "status": "CONFIRMED", "order_id": "FRESH-9001"})
                return
            self._send(404, {"error": f"unexpected GET {self.path}"})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BindingTestHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    srv_url = f"http://127.0.0.1:{httpd.server_address[1]}"

    try:
        ws_dir = tmp_path / "workspaces"
        ws_dir.mkdir(parents=True, exist_ok=True)
        db_file = ws_dir / "p1_chains.db"
        monkeypatch.setattr(settings, "data_dir", ws_dir)
        monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

        from reconqa.models import db as db_mod

        monkeypatch.setattr(db_mod, "_engine", None)
        monkeypatch.setattr(db_mod, "_session_factory", None)

        from reconqa.server.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                proj = (await client.post("/api/v1/projects", json={"name": "P1 Binding QA"})).json()
                project_id = proj["id"]

                cred = (
                    await client.post(
                        f"/api/v1/projects/{project_id}/auth-profiles",
                        json={
                            "name": "P1 Auth",
                            "role_tag": "buyer",
                            "auth_type": "headers",
                            "extra_headers_ref": {"Authorization": required_auth},
                        },
                    )
                ).json()
                cred_id = cred["id"]

                # Create a saved ScenarioModel assertion targeting GET /api/orders/{id}
                sc_resp = await client.post(
                    f"/api/v1/projects/{project_id}/scenarios",
                    json={
                        "slug": "order-confirm-scenario",
                        "name": "Order Confirmation",
                        "priority": "P0",
                        "assertions": [
                            {
                                "id": "auto-sc-assert-1",
                                "title": "Scenario status CONFIRMED",
                                "target_endpoint": "GET /api/orders/{id}",
                                "jsonpath": "$.status",
                                "operator": "equals",
                                "expected": "CONFIRMED",
                            }
                        ],
                    },
                )
                assert sc_resp.status_code == 201

                # Create a Run and write correlation.json with TWO duplicate-named chains
                run_resp = await client.post(
                    f"/api/v1/projects/{project_id}/runs?auto_start=false",
                    json={"target_url": srv_url, "modules": ["merge"]},
                )
                assert run_resp.status_code == 201
                run_data = run_resp.json()
                run_id = run_data["id"]
                hub_dir = Path(run_data["workspace_dir"]) / "hub"
                hub_dir.mkdir(parents=True, exist_ok=True)
                (hub_dir / "correlation.json").write_text(
                    json.dumps(
                        {
                            "variables": [],
                            "chains": [
                                {
                                    "name": "DuplicateName",
                                    "scenario": "s1",
                                    "steps": [
                                        {
                                            "step": 1,
                                            "scenario": "s1",
                                            "method": "GET",
                                            "path": "/wrong-first-chain",
                                            "raw_url": f"{srv_url}/wrong-first-chain",
                                            "expected_status": 200,
                                        }
                                    ],
                                },
                                {
                                    "name": "DuplicateName",
                                    "scenario": "s2",
                                    "steps": [
                                        {
                                            "step": 1,
                                            "scenario": "s2",
                                            "method": "POST",
                                            "path": "/api/orders?token=leaked-query-tok-999",
                                            "raw_url": f"{srv_url}/api/orders?token=leaked-query-tok-999",
                                            "request_body": {"password": "raw-login-password-888", "item": "book"},
                                            "expected_status": 201,
                                            "extracts": {
                                                "order_id": "$.data.order_id",
                                                "secret_token": "$.data.secret_token",
                                            },
                                            "uses": {},
                                        },
                                        {
                                            "step": 2,
                                            "scenario": "s2",
                                            "method": "POST",
                                            "path": "/api/orders/{id}/confirm",
                                            "raw_url": f"{srv_url}/api/orders/STALE-OLD-ID/confirm",
                                            "request_body": {"action": "fail_once", "data": {"order_id": "STALE-OLD-ID"}},
                                            "expected_status": 200,
                                            "extracts": {},
                                            "uses": {
                                                "path:id": "{{order_id}}",
                                                "body:data.order_id": "{{order_id}}",
                                            },
                                        },
                                        {
                                            "step": 3,
                                            "scenario": "s2",
                                            "method": "GET",
                                            "path": "/api/orders/{id}",
                                            "raw_url": f"{srv_url}/api/orders/STALE-OLD-ID?expand=items",
                                            "query_params": {"expand": "items"},
                                            "expected_status": 200,
                                            "extracts": {},
                                            "uses": {"path:id": "{{order_id}}"},
                                        },
                                    ],
                                },
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

                # Execute chain_index=1 (disambiguating duplicate chain_name!)
                exec_resp = await client.post(
                    f"/api/v1/projects/{project_id}/chains/execute",
                    json={
                        "run_id": run_id,
                        "chain_name": "DuplicateName",
                        "chain_index": 1,
                        "credential_id": cred_id,
                        "include_scenario_assertions": True,
                    },
                )
                assert exec_resp.status_code == 200
                r1 = exec_resp.json()
                assert r1["chain_index"] == 1
                assert r1["credential_id"] == cred_id
                assert r1["status"] == "failed"
                assert r1["failed_step_index"] == 1
                # Step 2 MUST have called FRESH-9001 (NOT STALE-OLD-ID!) and nested body.data.order_id == FRESH-9001
                assert recorded_requests[1]["path"] == "/api/orders/FRESH-9001/confirm"
                assert recorded_requests[1]["body"]["data"]["order_id"] == "FRESH-9001"
                # Sensitive extracted var redacted in response & checkpoint
                assert r1["context_vars"]["secret_token"] == "[REDACTED]"

                # Verify checkpoint JSON redacts password, path query token, auth token & extracted secret
                cp_text = (ws_dir / "chain_runs" / f"{r1['chain_run_id']}.json").read_text(encoding="utf-8")
                assert "raw-login-password-888" not in cp_text
                assert "leaked-query-tok-999" not in cp_text
                assert "p1-secret-token-xyz-777" not in cp_text
                assert "tok-leak-guard-555" not in cp_text

                # Resume WITHOUT passing credential_id -> MUST reuse checkpoint's credential_id automatically!
                resume_resp = await client.post(
                    f"/api/v1/projects/{project_id}/chain-runs/{r1['chain_run_id']}/resume",
                    json={
                        "override_step_body": {"action": "ok", "data": {"order_id": "{{order_id}}"}},
                    },
                )
                assert resume_resp.status_code == 200
                r2 = resume_resp.json()
                assert r2["status"] == "passed"
                assert r2["credential_id"] == cred_id
                # Step 3 MUST hit /api/orders/FRESH-9001?expand=items (NO duplicate ?expand=items&expand=items!)
                assert recorded_requests[-1]["path"] == "/api/orders/FRESH-9001?expand=items"
                # Step 3 MUST have auto-attached the saved ScenarioModel assertion ("auto-sc-assert-1")
                step3_ids = [a["id"] for a in r2["step_results"][2]["assertions"]]
                assert "auto-sc-assert-1" in step3_ids
                assert all(a["passed"] is True for a in r2["step_results"][2]["assertions"])

                # Delete credential & try resuming again without new credential_id -> clean HTTP 400!
                del_resp = await client.delete(f"/api/v1/projects/{project_id}/auth-profiles/{cred_id}")
                assert del_resp.status_code in (200, 204)
                resume_deleted = await client.post(
                    f"/api/v1/projects/{project_id}/chain-runs/{r1['chain_run_id']}/resume",
                    json={},
                )
                assert resume_deleted.status_code == 400
                assert "credential" in resume_deleted.json()["detail"].lower()

                # Unresolved {{missing_var}} fails cleanly before network request
                unresolved_resp = await client.post(
                    f"/api/v1/projects/{project_id}/chains/execute",
                    json={
                        "chain_name": "Missing Var Test",
                        "base_url": srv_url,
                        "steps": [{"step": 1, "method": "GET", "path": "/api/orders/{{does_not_exist}}"}],
                    },
                )
                assert unresolved_resp.status_code == 200
                ur = unresolved_resp.json()
                assert ur["status"] == "failed"
                assert "Missing required chain variable(s): does_not_exist" in (ur["step_results"][0]["error"] or "")
    finally:
        httpd.shutdown()
        httpd.server_close()
