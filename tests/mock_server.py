"""Lightweight in-process mock HTTP server for ReconQA tests, Maven E2E, and k6 E2E."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self
from urllib.parse import urlparse


class MockShopState:
    """Tracks requests and configurable security behaviors on the mock server."""

    def __init__(
        self,
        *,
        idor_vulnerable: bool = True,
        unauth_bypass_orders: bool = False,
    ) -> None:
        self.idor_vulnerable = idor_vulnerable
        self.unauth_bypass_orders = unauth_bypass_orders
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.cart_added: list[dict[str, Any]] = []
        self.cart_deleted: list[str] = []
        self.orders_submitted: list[dict[str, Any]] = []

    def record(self, method: str, path: str, headers: dict[str, str], body: Any = None) -> None:
        with self.lock:
            self.requests.append(
                {"method": method, "path": path, "headers": headers, "body": body}
            )


class MockShopServer:
    """Context manager wrapping a ThreadingHTTPServer bound to 127.0.0.1:0."""

    def __init__(
        self,
        *,
        idor_vulnerable: bool = True,
        unauth_bypass_orders: bool = False,
    ) -> None:
        self.state = MockShopState(
            idor_vulnerable=idor_vulnerable,
            unauth_bypass_orders=unauth_bypass_orders,
        )
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(
                self,
                status: int,
                payload: dict[str, Any],
                extra_headers: dict[str, str] | None = None,
            ) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                if extra_headers:
                    for k, v in extra_headers.items():
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def _read_json_body(self) -> Any:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0:
                    return None
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return raw.decode("utf-8", errors="replace")

            def _auth_token(self) -> str:
                auth = self.headers.get("Authorization", "")
                if auth.lower().startswith("bearer "):
                    return auth[7:].strip()
                return auth.strip()

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                body = self._read_json_body() if method in ("POST", "PUT", "PATCH") else None
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                state.record(method, path, hdrs, body)
                token = self._auth_token()

                # 1. GET /api/v1/items/search
                if method == "GET" and path == "/api/v1/items/search":
                    self._send_json(
                        200,
                        {
                            "code": 0,
                            "msg": "ok",
                            "data": {
                                "tenantId": "TENANT_STATIC_999",
                                "total": 1,
                                "list": [
                                    {
                                        "id": "ITEM-88421",
                                        "name": "ThinkPad X1",
                                        "price": 9999,
                                        "description": "ThinkPad X1 Carbon Gen 12",
                                    }
                                ],
                            },
                        },
                    )
                    return

                # 2. GET /api/v1/items/<id>
                if method == "GET" and path.startswith("/api/v1/items/"):
                    item_id = path.rsplit("/", 1)[-1]
                    self._send_json(
                        200,
                        {
                            "code": 0,
                            "msg": "ok",
                            "data": {
                                "tenantId": "TENANT_STATIC_999",
                                "itemId": item_id,
                                "skuId": "SKU-55102",
                                "stock": 12,
                            },
                        },
                        extra_headers={"Set-Cookie": "session_pref=dark_mode; Path=/"},
                    )
                    return

                # 3. POST /api/v1/cart/add (auth required)
                if method == "POST" and path == "/api/v1/cart/add":
                    if token not in ("token-account-a", "token-account-b"):
                        self._send_json(401, {"code": 401, "msg": "unauthorized"})
                        return
                    with state.lock:
                        state.cart_added.append(body if isinstance(body, dict) else {})
                    self._send_json(
                        200,
                        {
                            "code": 0,
                            "msg": "ok",
                            "data": {
                                "tenantId": "TENANT_STATIC_999",
                                "cartItemId": "CART-77309",
                            },
                        },
                    )
                    return

                # 4. DELETE /api/v1/cart/items/<cart_id> (auth required)
                if method == "DELETE" and path.startswith("/api/v1/cart/items/"):
                    if token not in ("token-account-a", "token-account-b"):
                        self._send_json(401, {"code": 401, "msg": "unauthorized"})
                        return
                    cart_id = path.rsplit("/", 1)[-1]
                    with state.lock:
                        state.cart_deleted.append(cart_id)
                    self._send_json(
                        200,
                        {"code": 0, "msg": "ok", "data": {"deleted": True}},
                    )
                    return

                # 5. GET /api/v1/orders/<order_id> (protected resource)
                if method == "GET" and path.startswith("/api/v1/orders/"):
                    order_id = path.rsplit("/", 1)[-1]
                    if not token and not state.unauth_bypass_orders:
                        self._send_json(401, {"code": 401, "msg": "unauthorized"})
                        return
                    if token == "token-account-b" and not state.idor_vulnerable:
                        self._send_json(403, {"code": 403, "msg": "forbidden"})
                        return
                    self._send_json(
                        200,
                        {
                            "code": 0,
                            "msg": "ok",
                            "data": {
                                "tenantId": "TENANT_STATIC_999",
                                "orderId": order_id,
                                "owner": "account_a",
                                "amount": 9999,
                                "status": "PAID",
                            },
                        },
                    )
                    return

                # 6. GET /api/v1/profile/leaked (unauth bypass endpoint for auth_matrix test)
                if method == "GET" and path == "/api/v1/profile/leaked":
                    self._send_json(
                        200,
                        {
                            "code": 0,
                            "msg": "ok",
                            "data": {
                                "userId": "USR-1001",
                                "account": "alice",
                                "tier": "vip",
                            },
                        },
                    )
                    return

                # 7. POST /api/v1/orders/submit (signed mutation endpoint)
                if method == "POST" and path == "/api/v1/orders/submit":
                    if token not in ("token-account-a", "token-account-b"):
                        self._send_json(401, {"code": 401, "msg": "unauthorized"})
                        return
                    with state.lock:
                        state.orders_submitted.append(body if isinstance(body, dict) else {})
                    self._send_json(
                        200,
                        {
                            "code": 0,
                            "msg": "ok",
                            "data": {
                                "tenantId": "TENANT_STATIC_999",
                                "orderId": "ORD-66202",
                                "status": "CREATED",
                            },
                        },
                    )
                    return

                self._send_json(404, {"code": 404, "msg": f"not found: {method} {path}"})

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

            def do_PUT(self) -> None:
                self._handle("PUT")

            def do_PATCH(self) -> None:
                self._handle("PATCH")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port: int = self._server.server_address[1]
        self.base_url: str = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> Self:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stop()
