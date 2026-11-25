"""Comprehensive tests for M2 (R3a: GraphQL Endpoint Splitting & R3b: Built-In Dynamic Placeholder Variables)."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import reconqa.models.db as db_mod
from reconqa.config import Settings, settings
from reconqa.models.schemas import ChainStepInput, ScenarioAssertion
from reconqa.models.tables import ScenarioModel
from reconqa.modules.codegen_java.generator import generate_java_project
from reconqa.modules.hub.assertion_engine import (
    evaluate_assertion,
    execute_readonly_sql,
    is_valid_dynamic_var,
    render_template_str,
    resolve_dynamic_var,
    safe_eval_arithmetic_decimal,
)
from reconqa.modules.hub.capability_map import build_capability_map
from reconqa.modules.hub.chain_runner import (
    _find_unresolved_vars,
    _render_body_value,
    execute_chain,
)
from reconqa.modules.hub.coverage_diff import compute_coverage_diff
from reconqa.modules.hub.har_parser import normalize_path, parse_har_file
from reconqa.modules.hub.inventory import build_project_inventory
from reconqa.modules.hub.openapi_merger import merge_to_openapi
from reconqa.server.app import create_app


class LocalGraphQLEchoServer:
    """Thread-safe local HTTP server recording exact wire paths, headers, query, and JSON bodies."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> LocalGraphQLEchoServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(length) if length > 0 else b""
                body_str = raw_body.decode("utf-8", errors="replace")
                try:
                    parsed_json = json.loads(body_str) if body_str.strip() else None
                except Exception:
                    parsed_json = None

                split = urlsplit(self.path)
                req_record = {
                    "method": method,
                    "raw_request_target": self.path,
                    "path": split.path,
                    "query": {k: v[0] if len(v) == 1 else v for k, v in parse_qs(split.query).items()},
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body_text": body_str,
                    "body_json": parsed_json,
                }
                with outer._lock:
                    outer.requests.append(req_record)

                if split.path == "/graphql":
                    op_name = ""
                    if isinstance(parsed_json, dict):
                        op_name = str(parsed_json.get("operationName") or "")
                    if op_name == "GetOrderList":
                        resp_payload: dict[str, Any] = {
                            "data": {
                                "orders": [{"id": "ord-gql-1", "status": "paid"}],
                            }
                        }
                    elif op_name == "CreateOrder":
                        resp_payload = {
                            "data": {
                                "createOrder": {
                                    "id": "ord-gql-new",
                                    "echo": parsed_json,
                                }
                            }
                        }
                    else:
                        resp_payload = {
                            "data": {
                                "ok": True,
                                "echo": parsed_json,
                            }
                        }
                else:
                    resp_payload = {
                        "data": {
                            "path": split.path,
                            "query": req_record["query"],
                            "headers": req_record["headers"],
                            "body_json": parsed_json,
                            "body_text": body_str,
                        }
                    }

                encoded = json.dumps(resp_payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)


def _setup_isolated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_name: str
) -> Path:
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / db_name
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)
    return ws_dir


def _make_har_entry(
    *,
    method: str,
    url: str,
    req_mime: str = "application/json",
    req_text: str | None = None,
    resp_json: Any = None,
    status: int = 200,
    started: str = "2026-11-25T05:00:00.000Z",
    headers: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    req_obj: dict[str, Any] = {
        "method": method,
        "url": url,
        "headers": headers or [],
        "queryString": [],
        "cookies": [],
    }
    if req_text is not None:
        req_obj["postData"] = {"mimeType": req_mime, "text": req_text}
    resp_body = json.dumps(resp_json) if resp_json is not None else "{}"
    return {
        "startedDateTime": started,
        "time": 18.0,
        "timings": {"wait": 12.0},
        "request": req_obj,
        "response": {
            "status": status,
            "statusText": "OK",
            "headers": [{"name": "Content-Type", "value": "application/json"}],
            "content": {"mimeType": "application/json", "text": resp_body, "size": len(resp_body)},
        },
    }


# ---------------------------------------------------------------------------
# R3a: HAR Parser & OpenAPI Merger GraphQL Splitting
# ---------------------------------------------------------------------------


def test_normalize_path_preserves_graphql_fragment_and_strips_spa_and_query() -> None:
    assert normalize_path("/graphql#GetOrderList") == ("/graphql#GetOrderList", {})
    assert normalize_path("/graphql?v=1#GetOrderList") == ("/graphql#GetOrderList", {})
    assert normalize_path("/graphql/#GetOrderList?debug=1") == ("/graphql#GetOrderList", {})
    assert normalize_path("/api/v1/items/9823410#GetItem") == (
        "/api/v1/items/{itemId}#GetItem",
        {"itemId": "9823410"},
    )
    assert normalize_path("/app#/dashboard?tab=orders") == ("/app", {})
    assert normalize_path("/#!/home") == ("/", {})
    assert normalize_path("/graphql#") == ("/graphql", {})


def test_har_parser_splits_graphql_operations_without_rest_false_positives(tmp_path: Path) -> None:
    har_path = tmp_path / "graphql_session.har"
    har_doc = {
        "log": {
            "entries": [
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_text=json.dumps(
                        {
                            "operationName": "GetOrderList",
                            "variables": {"page": 1},
                            "query": "query GetOrderList($page: Int) { orders(page: $page) { id } }",
                        }
                    ),
                    resp_json={"data": {"orders": [{"id": "o1"}]}},
                    headers=[{"name": "Authorization", "value": "Bearer tok-1"}],
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/api/graphql",
                    req_text=json.dumps(
                        {
                            "query": "# leading comment\nmutation CreateOrder($input: OrderInput!) { createOrder(input: $input) { id } }",
                            "variables": {"input": {"sku": "SKU-1"}},
                        }
                    ),
                    resp_json={"data": {"createOrder": {"id": "o2"}}},
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_mime="application/graphql",
                    req_text="query GetUserProfile { me { id name } }",
                    resp_json={"data": {"me": {"id": "u1", "name": "Alice"}}},
                ),
                _make_har_entry(
                    method="GET",
                    url="https://shop.example.com/graphql?operationName=ListTags&query=query+ListTags+%7B+tags+%7D",
                    req_mime="",
                    req_text=None,
                    resp_json={"data": {"tags": ["a", "b"]}},
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_text=json.dumps(
                        [
                            {"operationName": "BatchQueryA", "query": "query BatchQueryA { a }"},
                            {"operationName": "BatchMutB", "query": "mutation BatchMutB { b }"},
                        ]
                    ),
                    resp_json=[{"data": {"a": 1}}, {"data": {"b": 2}}],
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_text=json.dumps({"query": "{ health }"}),
                    resp_json={"data": {"health": "ok"}},
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_text=json.dumps(
                        {"query": "query ($id: ID!) { node(id: $id) { id } }", "variables": {"id": "1"}}
                    ),
                    resp_json={"data": {"node": {"id": "1"}}},
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/api/search",
                    req_text=json.dumps({"query": "shoes"}),
                    resp_json={"code": 0, "items": ["sneakers"]},
                ),
            ]
        }
    }
    har_path.write_text(json.dumps(har_doc), encoding="utf-8")

    entries = parse_har_file(har_path, scenario="gql_flow", target_domain="shop.example.com")
    norm_paths = [e.normalized_path for e in entries]

    assert "/graphql#GetOrderList" in norm_paths
    assert "/api/graphql#CreateOrder" in norm_paths
    assert "/graphql#GetUserProfile" in norm_paths
    assert "/graphql#ListTags" in norm_paths
    assert "/graphql#BatchQueryA" in norm_paths
    assert "/graphql#BatchMutB" in norm_paths
    # Anonymous GraphQL queries stay /graphql
    anon_entries = [e for e in entries if e.normalized_path == "/graphql"]
    assert len(anon_entries) == 2
    assert all(e.graphql_operation_name is None for e in anon_entries)
    # Non-GraphQL REST search payload {"query": "shoes"} stays unaffected
    rest_entries = [e for e in entries if e.normalized_path == "/api/search"]
    assert len(rest_entries) == 1
    assert rest_entries[0].graphql_operation_name is None

    create_order = next(e for e in entries if e.normalized_path == "/api/graphql#CreateOrder")
    assert create_order.graphql_operation_name == "CreateOrder"
    assert create_order.graphql_operation_type == "mutation"

    batch_a = next(e for e in entries if e.normalized_path == "/graphql#BatchQueryA")
    batch_b = next(e for e in entries if e.normalized_path == "/graphql#BatchMutB")
    assert batch_a.response_json == {"data": {"a": 1}}
    assert batch_b.response_json == {"data": {"b": 2}}


def test_openapi_merger_splits_graphql_preserves_metadata_and_avoids_biz_poisoning(
    tmp_path: Path,
) -> None:
    har_dir = tmp_path / "har"
    har_dir.mkdir()
    out_dir = tmp_path / "hub"
    js_path = tmp_path / "js_endpoints.json"

    har_doc = {
        "log": {
            "entries": [
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/api/orders",
                    req_text=json.dumps({"amount": 100}),
                    resp_json={"code": 0, "msg": "ok", "data": {"id": 1}},
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_text=json.dumps(
                        {
                            "operationName": "GetOrderList",
                            "query": "query GetOrderList { orders { id } }",
                        }
                    ),
                    resp_json={"data": {"orders": [{"id": "o1"}]}},
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_mime="application/graphql",
                    req_text="mutation CreateOrder { createOrder { id } }",
                    resp_json={"data": {"createOrder": {"id": "o2"}}},
                ),
            ]
        }
    }
    (har_dir / "main.har").write_text(json.dumps(har_doc), encoding="utf-8")
    js_path.write_text(
        json.dumps(
            [
                {"method": "POST", "url": "https://shop.example.com/graphql#DeleteOrder"},
                {"method": "POST", "path": "/graphql", "operationName": "UpdateOrder"},
            ]
        ),
        encoding="utf-8",
    )

    spec = merge_to_openapi(
        har_dir, out_dir, js_endpoints_path=js_path, target_domain="shop.example.com"
    )
    paths = spec["paths"]

    assert "/graphql#GetOrderList" in paths
    assert "/graphql#CreateOrder" in paths
    assert "/graphql#DeleteOrder" in paths
    assert "/graphql#UpdateOrder" in paths
    assert "/api/orders" in paths

    op_get = paths["/graphql#GetOrderList"]["post"]
    op_create = paths["/graphql#CreateOrder"]["post"]
    op_rest = paths["/api/orders"]["post"]

    assert op_get["operationId"] != op_create["operationId"]
    assert op_get["tags"] == ["graphql"]
    assert op_create["tags"] == ["graphql"]
    assert op_get["x-graphql-operation"] == "GetOrderList"
    assert op_get["x-graphql-type"] == "query"
    assert op_create["x-graphql-operation"] == "CreateOrder"
    assert op_create["x-graphql-type"] == "mutation"

    # application/graphql raw body is preserved
    assert "application/graphql" in op_create["requestBody"]["content"]

    # REST {"code": 0} global fallback MUST NOT poison GraphQL {data} operations
    assert op_rest["x-biz-success"] == {"field": "code", "value": 0}
    assert op_get["x-biz-success"] is None
    assert op_create["x-biz-success"] is None


# ---------------------------------------------------------------------------
# R3a: Coverage Diff, Inventory, Capability Map & Java Codegen
# ---------------------------------------------------------------------------


def test_coverage_diff_inventory_capability_map_and_java_codegen_graphql(tmp_path: Path) -> None:
    spec_path = tmp_path / "openapi.json"
    spec = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/graphql#GetOrderList": {
                "post": {
                    "operationId": "postGraphqlGetOrderList",
                    "tags": ["graphql"],
                    "x-discovery-source": "har-live",
                    "x-requires-auth": True,
                    "x-scenarios": ["orders_gql"],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "example": {"operationName": "GetOrderList", "query": "query GetOrderList { orders { id } }"}
                            }
                        },
                    },
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/graphql#CreateOrder": {
                "post": {
                    "operationId": "postGraphqlCreateOrder",
                    "tags": ["graphql"],
                    "x-discovery-source": "js-static",
                    "x-scenarios": ["orders_gql"],
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/graphql#GetUserById": {
                "get": {
                    "operationId": "getGraphqlGetUserById",
                    "tags": ["graphql"],
                    "x-discovery-source": "har-live",
                    "responses": {"200": {"description": "OK"}},
                }
            },
        },
    }
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    # 1. Coverage Diff: distinct endpoints, shared prefix "/graphql"
    cov = compute_coverage_diff(spec_path, tmp_path / "cov")
    assert cov["total_endpoints"] == 3
    assert cov["covered_count"] == 2
    assert cov["uncovered_count"] == 1
    assert cov["by_prefix"] == [
        {"prefix": "/graphql", "total": 3, "covered": 2, "uncovered": 1, "coverage_pct": 66.67}
    ]

    # 2. Inventory: preserves explore operationName + run OpenAPI virtual endpoints, prefix "/graphql"
    class _FakeRun:
        def __init__(self, ws: Path) -> None:
            self.id = "run-gql-1"
            self.name = "GQL Run"
            self.workspace_dir = str(ws)
            self.config_snapshot = {"target_url": "https://api.example.com"}

    ws = tmp_path / "run_ws"
    (ws / "hub").mkdir(parents=True)
    (ws / "hub" / "openapi.json").write_text(json.dumps(spec), encoding="utf-8")
    inv, merged_spec = build_project_inventory(
        project_id="proj-1",
        project_name="GraphQL Shop",
        runs=[_FakeRun(ws)],
        explore_results=[
            {
                "seed": {"base_url": "https://api.example.com"},
                "discovered_endpoints": [
                    {"method": "POST", "path": "/graphql", "operationName": "DeleteOrder", "source": "js-static"}
                ],
            }
        ],
    )
    assert "/graphql#GetOrderList" in merged_spec["paths"]
    inv_paths = {ep["path"] for ep in inv["endpoints"]}
    assert "/graphql#GetOrderList" in inv_paths
    assert "/graphql#CreateOrder" in inv_paths
    assert "/graphql#DeleteOrder" in inv_paths
    assert inv["by_prefix"] == [
        {"prefix": "/graphql", "total": 4, "covered": 2, "coverage_pct": 50.0}
    ]

    # 3. Capability Map: module/resource split, camelCase action classification, and target linking
    scenarios = [
        {
            "id": "sc-1",
            "slug": "gql_orders",
            "name": "GraphQL Orders",
            "priority": "P0",
            "status": "active",
            "assertions": [
                {
                    "id": "a-list",
                    "title": "Orders list exists",
                    "target_endpoint": "POST https://api.example.com/graphql?v=1#GetOrderList",
                    "operator": "not_empty",
                    "jsonpath": "$.data.orders",
                    "status": "passed",
                }
            ],
            "negative_cases": [],
        }
    ]
    cap_map = build_capability_map(inv, scenarios, "proj-1", "GraphQL Shop")
    caps_by_key = {c["key"]: c for c in cap_map["capabilities"]}
    assert "graphql/getorderlist" in caps_by_key
    assert "graphql/createorder" in caps_by_key
    assert "graphql/getuserbyid" in caps_by_key
    assert "graphql/deleteorder" in caps_by_key

    assert "list" in caps_by_key["graphql/getorderlist"]["actions"]
    assert "create" in caps_by_key["graphql/createorder"]["actions"]
    assert "detail" in caps_by_key["graphql/getuserbyid"]["actions"]
    assert "delete" in caps_by_key["graphql/deleteorder"]["actions"]
    # Scenario assertion with ?v=1#GetOrderList linked ONLY to graphql/getorderlist
    list_slots = {s["slot"]: s["status"] for s in caps_by_key["graphql/getorderlist"]["case_matrix"]}
    assert list_slots["happy_path"] == "covered"
    create_slots = {s["slot"]: s["status"] for s in caps_by_key["graphql/createorder"]["case_matrix"]}
    assert create_slots["happy_path"] == "missing"

    # 4. Java Codegen: distinct method names, wire /graphql in CSV/L2/L3, and isolated assertion matching
    corr_path = tmp_path / "correlation.json"
    corr_path.write_text(
        json.dumps(
            {
                "chains": [
                    {
                        "name": "GraphQL Order Flow",
                        "scenario": "orders_gql",
                        "steps": [
                            {
                                "step": 1,
                                "method": "POST",
                                "path": "/graphql#GetOrderList",
                                "raw_url": "https://api.example.com/graphql",
                                "request_body": {"operationName": "GetOrderList", "query": "query GetOrderList { orders { id } }"},
                                "expected_status": 200,
                            },
                            {
                                "step": 2,
                                "method": "POST",
                                "path": "/graphql#CreateOrder",
                                "raw_url": "https://api.example.com/graphql",
                                "request_body": {"operationName": "CreateOrder", "query": "mutation CreateOrder { createOrder { id } }"},
                                "expected_status": 200,
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    java_out = tmp_path / "java_proj"
    scenario_assertions_for_java = [
        {
            "id": "sa-gql-list",
            "title": "GQL orders assertion",
            "target_endpoint": "POST /graphql#GetOrderList",
            "operator": "not_empty",
            "jsonpath": "$.data.orders",
        },
        {
            "id": "sa-unfrag",
            "title": "Unfragmented GraphQL assertion",
            "target_endpoint": "POST /graphql",
            "operator": "equals",
            "jsonpath": "$.data.health",
            "expected": "ok",
        },
    ]
    generate_java_project(
        spec_path,
        java_out,
        correlation_path=corr_path,
        scenario_assertions=scenario_assertions_for_java,
    )

    csv_text = (java_out / "src/test/resources/data/l1_smoke_endpoints.csv").read_text(encoding="utf-8")
    csv_rows = list(csv.reader(io.StringIO(csv_text)))
    assert len(csv_rows) == 2
    assert csv_rows[1][3] == "/graphql"

    l3_java = (java_out / "src/test/java/com/reconqa/generated/security/L3AuthBoundaryTest.java").read_text(
        encoding="utf-8"
    )
    assert '"/graphql"' in l3_java
    assert "#GetOrderList" not in l3_java

    l2_java = (java_out / "src/test/java/com/reconqa/generated/chains/L2GraphQLOrderFlowTest.java").read_text(
        encoding="utf-8"
    )
    assert "step01_postGraphqlGetOrderList" in l2_java
    assert "step02_postGraphqlCreateOrder" in l2_java
    assert 'resolvePlaceholders("/graphql", context)' in l2_java
    assert 'resolvePlaceholders("/graphql#GetOrderList"' not in l2_java
    assert "GQL orders assertion" in l2_java
    assert "Unfragmented GraphQL assertion" not in l2_java


# ---------------------------------------------------------------------------
# R3b: Built-In Dynamic Placeholder Variables (Unit & Engine Tests)
# ---------------------------------------------------------------------------


def test_dynamic_vars_resolution_types_overrides_and_invalid_handling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. render_template_str works when context_vars is None or {}
    t_before = int(time.time())
    rendered_none = render_template_str(
        "ts={{$timestamp}}&id={{$uuid}}&r={{$randInt(100,999)}}&d={{$isoDate}}", None
    )
    t_after = int(time.time())
    assert "{{" not in rendered_none
    parts = dict(kv.split("=", 1) for kv in rendered_none.split("&"))
    assert t_before <= int(parts["ts"]) <= t_after
    assert uuid.UUID(parts["id"]).version == 4
    assert 100 <= int(parts["r"]) <= 999
    assert parts["d"].endswith("Z")
    datetime.fromisoformat(parts["d"])

    # Whitespace & signed integers
    ok_signed, val_signed = resolve_dynamic_var("$randInt( -10 , -5 )")
    assert ok_signed is True
    assert -10 <= val_signed <= -5

    # Explicit context_vars override precedence
    assert render_template_str("{{$timestamp}}", {"$timestamp": 123456}) == "123456"

    # Invalid dynamic vars do not crash and stay unreplaced
    assert is_valid_dynamic_var("$timestamp") is True
    assert is_valid_dynamic_var("$uuid") is True
    assert is_valid_dynamic_var("$isoDate") is True
    assert is_valid_dynamic_var("$randInt(100, 999)") is True
    assert is_valid_dynamic_var("$randInt(999, 100)") is False
    assert is_valid_dynamic_var("$randInt(abc, 1)") is False
    assert is_valid_dynamic_var("$unknown") is False
    assert render_template_str("{{$unknown}}-{{$randInt(999,100)}}", {}) == "{{$unknown}}-{{$randInt(999,100)}}"

    # 2. _render_body_value preserves native int for exact $timestamp/$randInt, str for embedded/uuid/isoDate
    rendered_body = _render_body_value(
        {
            "ts": "{{$timestamp}}",
            "rand": "{{$randInt(100,999)}}",
            "id": "{{$uuid}}",
            "iso": "{{$isoDate}}",
            "tag": "order-{{$randInt(10,20)}}",
        },
        {},
    )
    assert isinstance(rendered_body["ts"], int)
    assert isinstance(rendered_body["rand"], int)
    assert 100 <= rendered_body["rand"] <= 999
    assert isinstance(rendered_body["id"], str)
    assert uuid.UUID(rendered_body["id"]).version == 4
    assert isinstance(rendered_body["iso"], str) and rendered_body["iso"].endswith("Z")
    assert isinstance(rendered_body["tag"], str) and rendered_body["tag"].startswith("order-")

    # 3. _find_unresolved_vars ignores valid dynamic vars and catches invalid/missing ones
    assert _find_unresolved_vars(
        {
            "u": "https://x.test/items/{{$uuid}}?ts={{$timestamp}}",
            "b": {"n": "{{$randInt(100,999)}}", "d": "{{$isoDate}}"},
        }
    ) == set()
    unresolved = _find_unresolved_vars(
        {
            "a": "{{$unknown}}",
            "b": "{{$randInt(999,100)}}",
            "c": "{{$randInt(abc,1)}}",
            "d": "{{missing_ctx}}",
        }
    )
    assert unresolved == {"$unknown", "$randInt(999,100)", "$randInt(abc,1)", "missing_ctx"}

    # 4. safe_eval_arithmetic_decimal, SQL binding, and var_equals
    arith_dec = safe_eval_arithmetic_decimal("{{$timestamp}} + 60", {})
    assert arith_dec is not None and int(arith_dec) >= t_before + 60

    db_file = tmp_path / "dyn_sql.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("RECONQA_TEST_M2_SQL", f"sqlite:///{db_file}")
    ok_sql, rows_sql, sql_msg = execute_readonly_sql(
        "SELECT n FROM t WHERE n = {{$randInt(42,42)}}",
        context_vars={},
        db_dsn_ref="ENV:RECONQA_TEST_M2_SQL",
    )
    assert ok_sql is True, sql_msg
    assert rows_sql == [(42,)]

    ev_lhs = evaluate_assertion(
        ScenarioAssertion(
            id="dyn-var-eq",
            title="randInt var_equals",
            operator="var_equals",
            jsonpath="{{$randInt(7,7)}}",
            expected="7",
        ),
        status_code=200,
        duration_ms=5.0,
        response_headers={},
        body_json={},
        body_text="{}",
        context_vars={},
    )
    assert ev_lhs.passed is True


# ---------------------------------------------------------------------------
# E2E: Chain Runner, Chains Router, and TryIt Replay (R3a + R3b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_runner_dynamic_vars_and_invalid_var_preflight() -> None:
    with LocalGraphQLEchoServer() as srv:
        out = await execute_chain(
            project_id="proj-dyn",
            chain_name="Dynamic Placeholder Chain",
            base_url=srv.base_url,
            initial_vars={},
            steps=[
                ChainStepInput(
                    step=1,
                    name="Echo Dynamic Vars",
                    method="POST",
                    path="/echo/{{$uuid}}",
                    query_params={"ts": "{{$timestamp}}"},
                    request_headers={"X-Req-Date": "{{$isoDate}}"},
                    request_body={
                        "ts": "{{$timestamp}}",
                        "code": "{{$randInt(100,999)}}",
                        "label": "run-{{$randInt(1,9)}}",
                    },
                    expected_status=200,
                    assertions=[
                        ScenarioAssertion(
                            id="a-ts",
                            title="timestamp echoed",
                            operator="matches",
                            jsonpath="$.data.body_json.ts",
                            expected=r"^\d{10,}$",
                        )
                    ],
                )
            ],
        )
        assert out.status == "passed"
        assert len(srv.requests) == 1
        sent = srv.requests[0]
        assert re.fullmatch(r"/echo/[0-9a-f-]{36}", sent["path"])
        assert isinstance(sent["body_json"]["ts"], int)
        assert isinstance(sent["body_json"]["code"], int)
        assert 100 <= sent["body_json"]["code"] <= 999
        assert sent["body_json"]["label"].startswith("run-")
        assert sent["headers"]["x-req-date"].endswith("Z")
        assert sent["query"]["ts"].isdigit()

        # Invalid dynamic var fails cleanly before HTTP I/O
        bad_out = await execute_chain(
            project_id="proj-dyn",
            chain_name="Invalid Dynamic Var Chain",
            base_url=srv.base_url,
            initial_vars={},
            steps=[
                ChainStepInput(
                    step=1,
                    name="Invalid Var",
                    method="POST",
                    path="/echo/test",
                    request_body={"bad": "{{$unknown}}", "inv": "{{$randInt(999,100)}}"},
                    expected_status=200,
                )
            ],
        )
        assert bad_out.status == "failed"
        assert len(srv.requests) == 1  # No extra HTTP request sent
        assert "Missing required chain variable(s)" in (bad_out.step_results[0].error or "")


@pytest.mark.asyncio
async def test_e2e_graphql_wire_strip_scenario_sync_isolation_and_replay_dynamic_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_isolated_env(tmp_path, monkeypatch, "m2_e2e.db")
    await db_mod.init_db()
    app = create_app()

    with LocalGraphQLEchoServer() as srv:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            proj = (await client.post("/api/v1/projects", json={"name": "M2 E2E"})).json()
            pid = proj["id"]
            await client.post(
                f"/api/v1/projects/{pid}/targets",
                json={"root_domain": "127.0.0.1", "base_url": srv.base_url},
            )

            # Seed 3 ScenarioModels:
            # 1) POST /graphql#GetOrderList (target of chain + replay)
            # 2) POST /graphql#CreateOrder (must stay isolated from GetOrderList)
            # 3) POST /graphql (unfragmented; must NOT be cross-synced by #GetOrderList!)
            async with db_mod.get_session_factory()() as session:
                session.add_all(
                    [
                        ScenarioModel(
                            id="sc-gql-list",
                            project_id=pid,
                            slug="gql-get-order-list",
                            name="GraphQL GetOrderList",
                            priority="P0",
                            status="active",
                            assertions_json=json.dumps(
                                [
                                    {
                                        "id": "gql-list-1",
                                        "title": "Orders present",
                                        "target_endpoint": "POST /graphql#GetOrderList",
                                        "operator": "equals",
                                        "jsonpath": "$.data.orders[0].status",
                                        "expected": "paid",
                                        "status": "pending",
                                    }
                                ]
                            ),
                        ),
                        ScenarioModel(
                            id="sc-gql-create",
                            project_id=pid,
                            slug="gql-create-order",
                            name="GraphQL CreateOrder",
                            priority="P0",
                            status="active",
                            assertions_json=json.dumps(
                                [
                                    {
                                        "id": "gql-create-1",
                                        "title": "CreateOrder id",
                                        "target_endpoint": "POST /graphql#CreateOrder",
                                        "operator": "not_empty",
                                        "jsonpath": "$.data.createOrder.id",
                                        "status": "pending",
                                    }
                                ]
                            ),
                        ),
                        ScenarioModel(
                            id="sc-gql-unfrag",
                            project_id=pid,
                            slug="gql-unfragmented",
                            name="GraphQL Base",
                            priority="P1",
                            status="active",
                            assertions_json=json.dumps(
                                [
                                    {
                                        "id": "gql-base-1",
                                        "title": "Orders present",
                                        "target_endpoint": "POST /graphql",
                                        "operator": "equals",
                                        "jsonpath": "$.data.orders[0].status",
                                        "expected": "paid",
                                        "status": "pending",
                                    }
                                ]
                            ),
                        ),
                    ]
                )
                await session.commit()

            # 1. Chain Execute on POST /graphql#GetOrderList:
            #    - Wire path MUST be /graphql (no #GetOrderList)
            #    - Only sc-gql-list (NOT sc-gql-unfrag or sc-gql-create) syncs to passed!
            chain_resp = await client.post(
                f"/api/v1/projects/{pid}/chains/execute",
                json={
                    "chain_name": "GQL Read Chain",
                    "base_url": srv.base_url,
                    "sync_scenarios": True,
                    "include_scenario_assertions": True,
                    "steps": [
                        {
                            "step": 1,
                            "name": "Fetch Orders GQL",
                            "method": "POST",
                            "path": "/graphql#GetOrderList",
                            "raw_url": f"{srv.base_url}/graphql",
                            "request_body": {
                                "operationName": "GetOrderList",
                                "query": "query GetOrderList { orders { id status } }",
                                "traceId": "{{$uuid}}",
                            },
                            "expected_status": 200,
                        }
                    ],
                },
            )
            assert chain_resp.status_code == 200
            chain_data = chain_resp.json()
            assert chain_data["status"] == "passed"
            assert chain_data["synced_scenarios"] == 1
            assert chain_data["synced_assertions"] == 1
            assert srv.requests[-1]["raw_request_target"] == "/graphql"
            assert uuid.UUID(srv.requests[-1]["body_json"]["traceId"]).version == 4

            async with db_mod.get_session_factory()() as session:
                sc_list = await session.get(ScenarioModel, "sc-gql-list")
                sc_create = await session.get(ScenarioModel, "sc-gql-create")
                sc_unfrag = await session.get(ScenarioModel, "sc-gql-unfrag")
                assert json.loads(sc_list.assertions_json)[0]["status"] == "passed"
                assert json.loads(sc_create.assertions_json)[0]["status"] == "pending"
                assert json.loads(sc_unfrag.assertions_json)[0]["status"] == "pending"

            # 2. TryIt Replay on POST .../graphql#CreateOrder with dynamic vars + context_vars:
            #    - Wire path MUST be /graphql
            #    - Dynamic vars resolve even when context_vars={} and with native int in body
            #    - ScenarioAssertion on POST /graphql#CreateOrder auto-binds & syncs to passed
            replay_resp = await client.post(
                f"/api/v1/projects/{pid}/replay",
                json={
                    "method": "POST",
                    "url": f"{srv.base_url}/graphql#CreateOrder",
                    "headers": {"X-Trace": "{{$uuid}}"},
                    "query_params": {"ts": "{{$timestamp}}"},
                    "body": {
                        "operationName": "CreateOrder",
                        "variables": {
                            "nonce": "{{$randInt(100,999)}}",
                            "createdAt": "{{$isoDate}}",
                        },
                    },
                    "include_scenario_assertions": True,
                    "sync_scenarios": True,
                },
            )
            assert replay_resp.status_code == 200
            r_data = replay_resp.json()
            assert r_data["status_code"] == 200
            assert r_data["synced_scenarios"] == 1
            assert r_data["synced_assertions"] == 1
            wire_req = srv.requests[-1]
            assert wire_req["path"] == "/graphql"
            assert "#" not in wire_req["raw_request_target"]
            assert uuid.UUID(wire_req["headers"]["x-trace"]).version == 4
            assert wire_req["query"]["ts"].isdigit()
            assert isinstance(wire_req["body_json"]["variables"]["nonce"], int)
            assert 100 <= wire_req["body_json"]["variables"]["nonce"] <= 999
            assert wire_req["body_json"]["variables"]["createdAt"].endswith("Z")

            async with db_mod.get_session_factory()() as session:
                sc_create = await session.get(ScenarioModel, "sc-gql-create")
                sc_unfrag = await session.get(ScenarioModel, "sc-gql-unfrag")
                assert json.loads(sc_create.assertions_json)[0]["status"] == "passed"
                assert json.loads(sc_unfrag.assertions_json)[0]["status"] == "pending"

            # 3. TryIt Replay SSRF guard MUST still block metadata IPs injected via context_vars
            ssrf_resp = await client.post(
                f"/api/v1/projects/{pid}/replay",
                json={
                    "method": "GET",
                    "url": "http://{{ssrf_host}}/latest/meta-data/",
                    "context_vars": {"ssrf_host": "169.254.169.254"},
                },
            )
            assert ssrf_resp.status_code == 400

            # 4. Replay Matrix strips #OpName on wire and resolves dynamic placeholders
            matrix_resp = await client.post(
                f"/api/v1/projects/{pid}/replay-matrix",
                json={
                    "method": "POST",
                    "url": f"{srv.base_url}/graphql#GetOrderList",
                    "body": {"operationName": "GetOrderList", "rand": "{{$randInt(10,99)}}"},
                },
            )
            assert matrix_resp.status_code == 200
            assert srv.requests[-1]["path"] == "/graphql"
            assert isinstance(srv.requests[-1]["body_json"]["rand"], int)

            # 5. Chain resume with dynamic placeholders & cap_* scenario enrichment with ?v=1#GetOrderList
            async with db_mod.get_session_factory()() as session:
                session.add(
                    ScenarioModel(
                        id="sc-cap-gql",
                        project_id=pid,
                        slug="cap_graphql_getorderlist",
                        name="Cap GraphQL GetOrderList",
                        priority="P0",
                        status="active",
                        assertions_json=json.dumps(
                            [
                                {
                                    "id": "cap-gql-1",
                                    "title": "Cap GQL assertion",
                                    "target_endpoint": f"POST {srv.base_url}/graphql?v=1#GetOrderList",
                                    "operator": "not_empty",
                                    "jsonpath": "$.data.orders",
                                    "status": "pending",
                                }
                            ]
                        ),
                    )
                )
                await session.commit()

            # Execute failing chain with step 1 passing (dynamic var) and step 2 failing, then resume
            fail_exec = await client.post(
                f"/api/v1/projects/{pid}/chains/execute",
                json={
                    "chain_name": "Resume Dynamic Chain",
                    "base_url": srv.base_url,
                    "sync_scenarios": False,
                    "steps": [
                        {
                            "step": 1,
                            "name": "Step 1 Dyn",
                            "method": "POST",
                            "path": "/echo/{{$uuid}}",
                            "request_body": {"rand": "{{$randInt(10,20)}}"},
                            "expected_status": 200,
                        },
                        {
                            "step": 2,
                            "name": "Step 2 Dyn Resume",
                            "method": "POST",
                            "path": "/echo/step2",
                            "request_body": {"nonce": "{{$uuid}}", "code": "{{expected_code}}"},
                            "expected_status": 200,
                        },
                    ],
                },
            )
            assert fail_exec.status_code == 200
            fail_data = fail_exec.json()
            assert fail_data["status"] == "failed"
            run_id_resume = fail_data["chain_run_id"]

            resume_resp = await client.post(
                f"/api/v1/projects/{pid}/chain-runs/{run_id_resume}/resume",
                json={"override_vars": {"expected_code": "ok-resumed"}, "sync_scenarios": False},
            )
            assert resume_resp.status_code == 200
            resumed_data = resume_resp.json()
            assert resumed_data["status"] == "passed"
            assert uuid.UUID(srv.requests[-1]["body_json"]["nonce"]).version == 4
            assert srv.requests[-1]["body_json"]["code"] == "ok-resumed"

            # Verify _enrich_scenario matches cap_* GraphQL endpoint when target_endpoint has ?v=1#GetOrderList
            run_resp = await client.post(
                f"/api/v1/projects/{pid}/runs?auto_start=false",
                json={"name": "GQL Enrich Run", "target_url": srv.base_url, "modules": ["merge"]},
            )
            assert run_resp.status_code == 201
            run_ws = Path(run_resp.json()["workspace_dir"])
            (run_ws / "hub").mkdir(parents=True, exist_ok=True)
            (run_ws / "hub" / "openapi.json").write_text(
                json.dumps(
                    {
                        "openapi": "3.0.3",
                        "servers": [{"url": srv.base_url}],
                        "paths": {
                            "/graphql#GetOrderList": {
                                "post": {
                                    "x-discovery-source": "har-live",
                                    "responses": {"200": {"description": "OK"}},
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            from reconqa.models.tables import Run

            async with db_mod.get_session_factory()() as session:
                db_run = await session.get(Run, run_resp.json()["id"])
                assert db_run is not None
                db_run.status = "completed"
                await session.commit()

            cap_sc = (
                await client.get(f"/api/v1/projects/{pid}/scenarios/sc-cap-gql")
            ).json()
            assert any(ep["path"] == "/graphql#GetOrderList" for ep in cap_sc["endpoints"])


def test_graphql_edge_cases_r2_regressions(tmp_path: Path) -> None:
    """Regression tests for Challenger R1 findings 1-4 and Reviewer fragment-first note."""
    # 1. har_parser: REST plain-English {"query": "query syntax tutorial"} MUST NOT split,
    #    fragment-first GraphQL document MUST extract GetUser, and anonymous brace query
    #    containing a field named `query` followed by `otherField { id }` MUST stay unsplit.
    har_path = tmp_path / "edge_cases.har"
    har_doc = {
        "log": {
            "entries": [
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/api/search",
                    req_text=json.dumps({"query": "query syntax tutorial"}),
                    resp_json={"code": 0, "items": ["doc1"]},
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_mime="application/graphql",
                    req_text=(
                        "fragment F on User { id name }\n"
                        "query GetUser($id: ID!) @cached { user(id: $id) { ...F } }"
                    ),
                    resp_json={"data": {"user": {"id": "u1"}}},
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_text=json.dumps({"query": "{ a { x } query otherField { id } }"}),
                    resp_json={"data": {"a": {"x": 1}}},
                ),
            ]
        }
    }
    har_path.write_text(json.dumps(har_doc), encoding="utf-8")
    parsed = parse_har_file(har_path, scenario="edge", target_domain="shop.example.com")

    search_entries = [e for e in parsed if e.normalized_path.startswith("/api/search")]
    assert len(search_entries) == 1
    assert search_entries[0].normalized_path == "/api/search"
    assert search_entries[0].graphql_operation_name is None
    assert search_entries[0].graphql_operation_type is None

    frag_first = next(e for e in parsed if e.normalized_path == "/graphql#GetUser")
    assert frag_first.graphql_operation_name == "GetUser"
    assert frag_first.graphql_operation_type == "query"

    anon_brace = next(
        e for e in parsed if e.normalized_path == "/graphql"
    )
    assert anon_brace.graphql_operation_name is None

    # 2. openapi_merger: non-2xx GraphQL (500 {"errors": ...}) and single-element batched
    #    GraphQL (200 [{"data": ...}]) MUST NOT inherit REST {"code": 0} global_fallback,
    #    and JS endpoint "/graphql#/dashboard" + operationName="JsAfterSpaHash" MUST attach.
    har_dir = tmp_path / "har_biz"
    har_dir.mkdir()
    biz_har = {
        "log": {
            "entries": [
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/api/search",
                    req_text=json.dumps({"query": "query syntax tutorial"}),
                    resp_json={"code": 0, "items": []},
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/api/failing-rest",
                    req_text=json.dumps({"x": 1}),
                    resp_json={"error": "upstream down"},
                    status=500,
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_text=json.dumps(
                        {
                            "operationName": "DeleteOrder",
                            "query": "mutation DeleteOrder { deleteOrder }",
                        }
                    ),
                    resp_json={"errors": [{"message": "boom"}]},
                    status=500,
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_text=json.dumps(
                        [{"operationName": "SingleBatchOp", "query": "query SingleBatchOp { ok }"}]
                    ),
                    resp_json=[{"data": {"ok": True}}],
                    status=200,
                ),
            ]
        }
    }
    (har_dir / "biz.har").write_text(json.dumps(biz_har), encoding="utf-8")
    js_spa = tmp_path / "js_spa.json"
    js_spa.write_text(
        json.dumps(
            [
                {
                    "method": "POST",
                    "path": "/graphql#/dashboard",
                    "operationName": "JsAfterSpaHash",
                    "source": "js-static",
                }
            ]
        ),
        encoding="utf-8",
    )
    spec = merge_to_openapi(
        har_dir, tmp_path / "hub_biz", js_endpoints_path=js_spa, target_domain="shop.example.com"
    )
    assert spec["paths"]["/api/search"]["post"]["x-biz-success"] == {"field": "code", "value": 0}
    assert spec["paths"]["/api/failing-rest"]["post"]["x-biz-success"] == {
        "field": "code",
        "value": 0,
    }
    assert spec["paths"]["/graphql#DeleteOrder"]["post"]["x-biz-success"] is None
    assert spec["paths"]["/graphql#SingleBatchOp"]["post"]["x-biz-success"] is None
    assert "/graphql#JsAfterSpaHash" in spec["paths"]

    # 3. inventory: explore endpoint "/graphql#/dashboard" + operation_name="ExploreAfterSpa"
    inv, _ = build_project_inventory(
        project_id="proj-edge",
        project_name="Edge Shop",
        runs=[],
        explore_results=[
            {
                "seed": {"base_url": "https://shop.example.com"},
                "discovered_endpoints": [
                    {
                        "method": "POST",
                        "path": "/graphql#/dashboard",
                        "operation_name": "ExploreAfterSpa",
                        "source": "js-static",
                    }
                ],
            }
        ],
    )
    inv_paths = {ep["path"] for ep in inv["endpoints"]}
    assert "/graphql#ExploreAfterSpa" in inv_paths

    # 4. capability_map: /api/v1/graphql#GetOrderList and /api/graphql#CreateOrder MUST be
    #    retained when include_noise=False under module="graphql".
    cap_map = build_capability_map(
        {
            "endpoints": [
                {
                    "method": "POST",
                    "path": "/api/v1/graphql#GetOrderList",
                    "discovery_source": "har-live",
                },
                {
                    "method": "POST",
                    "path": "/api/graphql#CreateOrder",
                    "discovery_source": "har-live",
                },
            ]
        },
        [],
        "proj-edge",
        "Edge Shop",
        include_noise=False,
    )
    caps_by_key = {c["key"]: c for c in cap_map["capabilities"]}
    assert "graphql/getorderlist" in caps_by_key
    assert "graphql/createorder" in caps_by_key
    assert caps_by_key["graphql/getorderlist"]["module"] == "graphql"
    assert caps_by_key["graphql/getorderlist"]["actions"] == ["list"]
    assert caps_by_key["graphql/createorder"]["module"] == "graphql"
    assert caps_by_key["graphql/createorder"]["actions"] == ["create"]


def test_graphql_underscore_op_names_batch_metadata_and_raw_graphql_correlation(
    tmp_path: Path,
) -> None:
    """P1 #8 & #9 (HAR/GraphQL): preserve _Lookup/Lookup/Lookup_, batch metadata, and application/graphql correlation."""
    from reconqa.modules.hub.correlation import analyze_correlations

    har_dir = tmp_path / "har_p1_gql"
    har_dir.mkdir()
    batch_payload_raw = json.dumps(
        [
            {"operationName": "_Lookup", "query": "query _Lookup { a }"},
            {"operationName": "Lookup", "query": "query Lookup { b }"},
            {"operationName": "Lookup_", "query": "query Lookup_ { c }"},
        ]
    )
    raw_gql_create = 'mutation CreateTicket { createTicket(title: "bug") { id } }'
    raw_gql_get = 'query GetTicket { ticket(id: "TKT-98765") { id status } }'

    har_doc = {
        "log": {
            "entries": [
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_text=batch_payload_raw,
                    resp_json=[
                        {"data": {"a": 1}},
                        {"data": {"b": 2}},
                        {"data": {"c": 3}},
                    ],
                    started="2026-11-25T05:00:01.000Z",
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_mime="application/graphql",
                    req_text=raw_gql_create,
                    resp_json={"data": {"createTicket": {"id": "TKT-98765"}}},
                    started="2026-11-25T05:00:02.000Z",
                    headers=[{"name": "Content-Type", "value": "application/graphql"}],
                ),
                _make_har_entry(
                    method="POST",
                    url="https://shop.example.com/graphql",
                    req_mime="application/graphql",
                    req_text=raw_gql_get,
                    resp_json={"data": {"ticket": {"id": "TKT-98765", "status": "OPEN"}}},
                    started="2026-11-25T05:00:03.000Z",
                    headers=[{"name": "Content-Type", "value": "application/graphql"}],
                ),
            ]
        }
    }
    har_file = har_dir / "gql_exact.har"
    har_file.write_text(json.dumps(har_doc), encoding="utf-8")

    # 1. Underscore operation names stay completely distinct & batch raw body/index preserved
    entries = parse_har_file(har_file, scenario="gql_exact", target_domain="shop.example.com")
    by_norm_path = {e.normalized_path: e for e in entries}
    assert "/graphql#_Lookup" in by_norm_path
    assert "/graphql#Lookup" in by_norm_path
    assert "/graphql#Lookup_" in by_norm_path

    e0 = by_norm_path["/graphql#_Lookup"]
    e1 = by_norm_path["/graphql#Lookup"]
    e2 = by_norm_path["/graphql#Lookup_"]
    assert e0.graphql_operation_name == "_Lookup"
    assert e1.graphql_operation_name == "Lookup"
    assert e2.graphql_operation_name == "Lookup_"
    assert [e0.graphql_batch_index, e1.graphql_batch_index, e2.graphql_batch_index] == [0, 1, 2]
    assert [e0.graphql_batch_size, e1.graphql_batch_size, e2.graphql_batch_size] == [3, 3, 3]
    assert e0.request_body_raw == batch_payload_raw
    assert e1.request_body_raw == batch_payload_raw
    assert e2.request_body_raw == batch_payload_raw

    # 2. Correlation preserves raw application/graphql request_body and Content-Type header
    corr_out = analyze_correlations(
        har_dir,
        tmp_path / "corr_out",
        target_domain="shop.example.com",
    )
    assert len(corr_out["chains"]) == 1
    steps = corr_out["chains"][0]["steps"]
    assert len(steps) == 2
    assert steps[0]["path"] == "/graphql#CreateTicket"
    assert steps[0]["request_body"] == raw_gql_create
    assert steps[0]["request_headers"].get("Content-Type") == "application/graphql"
    assert steps[1]["path"] == "/graphql#GetTicket"
    assert steps[1]["request_body"] == raw_gql_get
    assert steps[1]["request_headers"].get("Content-Type") == "application/graphql"
