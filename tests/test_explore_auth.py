"""Comprehensive tests for Authenticated Exploration, Login Exchange, Pre-flight Verify, Scope Isolation, Prompts & MCP."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

import reconqa.models.db as db_mod
from reconqa.config import Settings, settings
from reconqa.mcp_server import create_mcp_server
from reconqa.models.schemas import AuthProfileOut
from reconqa.modules.discovery.auth_resolver import (
    auto_extract_token,
    extract_json_path,
    headers_for_url,
    is_unsafe_auth_url,
    make_scope_request_hook,
    redact_secrets,
    resolve_env_ref,
    sanitize_url_for_storage,
)
from reconqa.modules.discovery.runner import run_discovery
from reconqa.utils.proc import CommandResult


class _AuthGatedMockServer:
    """In-process HTTP server requiring specific auth for /openapi.json, /static/app.js, and /api/v1/me."""

    def __init__(self) -> None:
        self.expected_bearer = "Bearer secret-bearer-token-xyz987"
        self.expected_cookie = "sid=secret-cookie-val-4567"
        self.expected_basic = "Basic " + base64.b64encode(b"qa_admin:Sup3rS3cretPwd!").decode("ascii")
        self.expected_custom_key = "custom-api-key-998877"
        self.login_token = "exchanged-jwt-token-abc12345"
        self.login_cookie = "session_id=exchanged-cookie-7890"
        self.received_headers: list[tuple[str, dict[str, str]]] = []

        openapi_doc = {
            "openapi": "3.0.3",
            "info": {"title": "Gated Demo API", "version": "1.0.0"},
            "paths": {
                "/api/v1/secret/orders": {
                    "get": {"summary": "List protected orders", "responses": {"200": {"description": "OK"}}}
                },
                "/api/v1/secret/refund": {
                    "post": {"summary": "Refund order", "responses": {"200": {"description": "OK"}}}
                },
            },
        }
        js_bundle = """
const routes = [{ path: "/secure-dashboard", name: "SecureDashboard" }];
function loadVault() {
  return axios.get("/api/v1/secret/vault-items");
}
"""
        index_html = (
            "<!doctype html><html><head><title>Gated Portal</title></head>"
            '<body><div id="app"></div><script src="/static/app.js"></script></body></html>'
        )
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send(
                self,
                code: int,
                body: str,
                content_type: str = "application/json",
                extra_headers: dict[str, str] | None = None,
            ) -> None:
                raw = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (extra_headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def _is_authorized(self) -> bool:
                authz = (self.headers.get("Authorization") or "").strip()
                cookie = (self.headers.get("Cookie") or "").strip()
                x_key = (self.headers.get("X-Api-Key") or "").strip()
                if authz == parent.expected_bearer:
                    return True
                if parent.expected_cookie in cookie:
                    return True
                if authz == parent.expected_basic:
                    return True
                if x_key == parent.expected_custom_key:
                    return True
                if authz == f"Bearer {parent.login_token}" and parent.login_cookie in cookie:
                    return True
                return False

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0]
                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
                if path == "/api/auth/login":
                    try:
                        payload = json.loads(raw_body)
                    except Exception:
                        payload = {}
                    if payload.get("username") == "qa_user" and payload.get("password") == "LoginPass#2026":
                        self._send(
                            200,
                            json.dumps({"code": 0, "data": {"accessToken": parent.login_token}}),
                            extra_headers={"Set-Cookie": f"{parent.login_cookie}; Path=/; HttpOnly"},
                        )
                    else:
                        self._send(401, json.dumps({"code": 401, "msg": "invalid credentials"}))
                    return
                self._send(404, '{"detail":"not found"}')

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                hdr_snapshot = {k.lower(): v for k, v in self.headers.items()}
                parent.received_headers.append((path, hdr_snapshot))

                if path == "/api/v1/verify-redirect":
                    self._send(302, "", extra_headers={"Location": "/login?next=/dashboard"})
                    return
                if path == "/api/v1/verify-biz-denied":
                    self._send(200, json.dumps({"code": 40100, "msg": "登录已过期，请重新登录"}))
                    return
                if path == "/api/v1/me":
                    if self._is_authorized():
                        self._send(200, json.dumps({"code": 0, "data": {"user": "qa_user"}}))
                    else:
                        self._send(401, json.dumps({"code": 401, "msg": "Unauthorized"}))
                    return

                if path == "/":
                    if self._is_authorized():
                        self._send(200, index_html, "text/html; charset=utf-8")
                    else:
                        self._send(401, "<html><body>Login Required</body></html>", "text/html")
                elif path == "/static/app.js":
                    if self._is_authorized():
                        self._send(200, js_bundle, "application/javascript; charset=utf-8")
                    else:
                        self._send(401, '{"detail":"unauthorized"}')
                elif path == "/openapi.json":
                    if self._is_authorized():
                        self._send(200, json.dumps(openapi_doc))
                    else:
                        self._send(401, '{"detail":"unauthorized"}')
                else:
                    self._send(404, '{"detail":"not found"}')

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self) -> _AuthGatedMockServer:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=3)


def _setup_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "auth_explore.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)
    return ws_dir


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
        text = getattr(raw[0], "text", None)
        if isinstance(text, str):
            return json.loads(text)
    if isinstance(raw, dict):
        return raw
    raise AssertionError(f"Unexpected MCP result: {raw!r}")


def test_auth_resolver_helpers_and_scope_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify env allowlist, redaction, SSRF URL check, JSON token extraction, and Burp scope isolation."""
    monkeypatch.setenv("RECONQA_SECRET_TOK", "tok-live-9999")
    monkeypatch.setenv("HOME_SECRET", "must-not-read")
    assert resolve_env_ref("ENV:RECONQA_SECRET_TOK") == "tok-live-9999"
    assert resolve_env_ref("${RECONQA_SECRET_TOK}") == "tok-live-9999"
    assert resolve_env_ref("ENV:HOME_SECRET") == ""
    assert resolve_env_ref("${HOME_SECRET}") == ""

    raw_pw = "MySecretPassword!42"
    b64_pw = base64.b64encode(raw_pw.encode("utf-8")).decode("ascii")
    redacted = redact_secrets(f"failed with {raw_pw} and {b64_pw}", [raw_pw])
    assert raw_pw not in redacted
    assert b64_pw not in redacted
    assert "[REDACTED]" in redacted

    assert is_unsafe_auth_url("http://169.254.169.254/latest/meta-data/") is True
    assert is_unsafe_auth_url("http://metadata.google.internal/") is True
    assert is_unsafe_auth_url("http://169.254.10.20/token") is True
    assert is_unsafe_auth_url("file:///etc/passwd") is True
    assert is_unsafe_auth_url("http://127.0.0.1:8080/login") is False
    assert is_unsafe_auth_url("https://api.example.test/login") is False

    assert extract_json_path({"data": {"accessToken": "abc123"}}, "$.data.accessToken") == "abc123"
    assert auto_extract_token({"result": {"access_token": "tok-auto-1"}}) == "tok-auto-1"
    assert auto_extract_token({"data": "raw-string-jwt"}) == "raw-string-jwt"

    # Burp Scope Isolation: ALL credential headers stripped out of scope; explicit scope_domains is sole allowlist
    hdrs = {
        "Authorization": "Bearer super-secret-jwt",
        "Cookie": "sid=super-secret-cookie",
        "X-Api-Key": "secret-key-999",
        "satoken": "custom-sa-token-888",
    }
    in_scope = headers_for_url("https://api.example.com/v1/users", hdrs, "example.com")
    assert in_scope["Authorization"] == "Bearer super-secret-jwt"
    assert in_scope["satoken"] == "custom-sa-token-888"

    out_of_scope = headers_for_url("https://cdn.thirdparty.io/app.js", hdrs, "example.com")
    assert out_of_scope == {}

    # When scope_domains is non-empty, it is the SOLE allowlist (does NOT OR with allowed_root_domain)
    explicit_scope = headers_for_url(
        "https://auth.partner.org/openapi.json",
        hdrs,
        "example.com",
        scope_domains=["partner.org"],
    )
    assert explicit_scope["Authorization"] == "Bearer super-secret-jwt"
    assert (
        headers_for_url(
            "https://api.example.com/v1/users",
            hdrs,
            "example.com",
            scope_domains=["partner.org"],
        )
        == {}
    )

    # URL sanitization strips userinfo and sensitive query params
    sanitized = sanitize_url_for_storage(
        "https://alice:TopSecret99@api.example.com/v1/me?token=tok_abc123&page=2&sig=deadbeef"
    )
    assert "alice" not in sanitized and "TopSecret99" not in sanitized
    assert "tok_abc123" not in sanitized and "deadbeef" not in sanitized
    assert "page=2" in sanitized

    # IPv6 link-local & IPv6-mapped metadata blocked
    assert is_unsafe_auth_url("http://[fe80::1]/meta") is True
    assert is_unsafe_auth_url("http://[::ffff:169.254.169.254]/latest/meta-data/") is True


def test_auth_profile_out_masks_login_config_and_preserves_env_refs() -> None:
    """Verify AuthProfileOut masks password/token in login_config while preserving ENV:RECONQA_* refs."""
    out = AuthProfileOut(
        id="ap-1",
        project_id="proj-1",
        name="Staging Login",
        role_tag="admin",
        auth_type="login_api",
        storage_state_path=None,
        extra_headers_ref={"Authorization": "Bearer super-secret-raw-token-1234"},
        login_config={
            "login_url": "https://api.example.test/auth/login",
            "username": "qa_admin",
            "password": "PlainPassword#9999",
            "token": "ENV:RECONQA_LOGIN_TOKEN",
            "token_header": "Authorization",
            "token_prefix": "Bearer ",
            "token_jsonpath": "data.accessToken",
            "password_field": "password",
            "extra_fields": {
                "tenant": "acme",
                "client_secret": "very-secret-client-val-8888",
                "client_assertion": "jwt-assertion-super-secret-7777",
                "api_key": "${RECONQA_CLIENT_KEY}",
            },
        },
    )
    dumped = out.model_dump()
    assert "PlainPassword#9999" not in json.dumps(dumped)
    assert "very-secret-client-val-8888" not in json.dumps(dumped)
    assert "jwt-assertion-super-secret-7777" not in json.dumps(dumped)
    assert "super-secret-raw-token-1234" not in json.dumps(dumped)
    assert dumped["login_config"]["password"] == "Plai•••9999"
    assert dumped["login_config"]["token"] == "ENV:RECONQA_LOGIN_TOKEN"
    assert dumped["login_config"]["extra_fields"]["api_key"] == "${RECONQA_CLIENT_KEY}"
    assert dumped["login_config"]["extra_fields"]["client_assertion"] == "jwt-•••7777"
    assert dumped["login_config"]["extra_fields"]["tenant"] == "***"
    assert dumped["login_config"]["token_header"] == "Authorization"
    assert dumped["login_config"]["token_jsonpath"] == "data.accessToken"
    assert dumped["login_config"]["username"] == "qa_admin"

    # Legacy NULL coercion for auth_type and login_config
    legacy_out = AuthProfileOut(
        id="ap-legacy",
        project_id="proj-1",
        name="Legacy",
        role_tag="account_a",
        auth_type=None,  # type: ignore[arg-type]
        storage_state_path=None,
        extra_headers_ref={},
        login_config=None,  # type: ignore[arg-type]
    )
    assert legacy_out.auth_type == "headers"
    assert legacy_out.login_config == {}


def test_run_discovery_passes_headers_and_redacts_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify run_discovery passes -H/-or to katana, strips raw headers, scopes httpx, and redacts straddling stderr."""
    captured_calls: list[tuple[list[str], str | None]] = []
    secret_tok = "Bearer ultra-secret-discovery-token-777"
    secret_cookie = "sid=ultra-secret-cookie-555"
    straddling_tok = "STRADDLE-SECRET-TOKEN-1234567890"

    monkeypatch.setattr(
        Settings,
        "resolve_bin",
        lambda self, name: (
            f"/usr/local/bin/{name}" if name in {"tlsx", "katana", "httpx"} else None
        ),
    )

    def _fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
        captured_calls.append((list(cmd), kwargs.get("input_text")))
        bin_name = Path(cmd[0]).name
        if bin_name == "tlsx":
            return CommandResult(
                cmd=cmd,
                returncode=0,
                stdout=json.dumps({"host": "portal.example.test", "san": ["other-vendor.net"]}) + "\n",
                stderr="",
            )
        if bin_name == "katana":
            return CommandResult(
                cmd=cmd,
                returncode=0,
                stdout=json.dumps(
                    {
                        "request": {
                            "endpoint": "https://portal.example.test/api/v1/items",
                            "raw": f"GET / HTTP/1.1\r\nAuthorization: {secret_tok}\r\n",
                            "headers": {"Authorization": secret_tok},
                        }
                    }
                )
                + "\n",
                stderr="",
            )
        # Put secret straddling byte 200 so redaction BEFORE truncation is tested!
        prefix = "x" * 185
        return CommandResult(
            cmd=cmd,
            returncode=1,
            stdout="",
            stderr=f"{prefix}{straddling_tok} and {secret_tok} and {secret_cookie}",
        )

    import reconqa.modules.discovery.runner as runner_mod

    monkeypatch.setattr(runner_mod, "run_cmd_sync", _fake_run_cmd)

    out_dir = tmp_path / "disc"
    summary = run_discovery(
        "https://portal.example.test",
        out_dir,
        mode="active",
        extra_headers={
            "Authorization": secret_tok,
            "Cookie": secret_cookie,
            "X-Straddle": straddling_tok,
            "Host": "should-be-skipped.test",
        },
        allowed_root_domain="example.test",
    )

    kat_calls = [c for c in captured_calls if Path(c[0][0]).name == "katana"]
    assert len(kat_calls) == 1
    kat_cmd = kat_calls[0][0]
    assert "-or" in kat_cmd
    assert f"Authorization: {secret_tok}" in kat_cmd
    assert all("should-be-skipped.test" not in arg for arg in kat_cmd)

    # katana_urls.jsonl must NOT contain raw request dumps or auth headers
    kat_jsonl = (out_dir / "katana_urls.jsonl").read_text(encoding="utf-8")
    assert secret_tok not in kat_jsonl
    assert '"raw"' not in kat_jsonl
    assert '"headers"' not in kat_jsonl

    # httpx must split in-scope (with -H) vs out-of-scope TLS SAN host (without -H)
    hx_calls = [c for c in captured_calls if Path(c[0][0]).name == "httpx"]
    assert len(hx_calls) == 2
    in_scope_call = next(c for c in hx_calls if "-H" in c[0])
    out_scope_call = next(c for c in hx_calls if "-H" not in c[0])
    assert "portal.example.test" in (in_scope_call[1] or "")
    assert "other-vendor.net" not in (in_scope_call[1] or "")
    assert "other-vendor.net" in (out_scope_call[1] or "")

    summary_text = (out_dir / "discovery_summary.json").read_text(encoding="utf-8")
    assert secret_tok not in summary_text
    assert secret_cookie not in summary_text
    assert straddling_tok not in summary_text
    assert "STRADDLE-SECRET" not in summary_text
    assert any("[REDACTED]" in w for w in summary["warnings"])


@pytest.mark.asyncio
async def test_legacy_db_migration_adds_auth_profile_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify init_db idempotently migrates legacy auth_profiles tables missing auth_type/login_config."""
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            """
            CREATE TABLE projects (
                id VARCHAR(32) PRIMARY KEY,
                slug VARCHAR(128) UNIQUE,
                name VARCHAR(128),
                description TEXT DEFAULT '',
                created_at DATETIME
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE auth_profiles (
                id VARCHAR(32) PRIMARY KEY,
                project_id VARCHAR(32) NOT NULL,
                name VARCHAR(128) NOT NULL,
                role_tag VARCHAR(64) DEFAULT 'account_a',
                storage_state_path VARCHAR(512),
                extra_headers_ref JSON DEFAULT '{}'
            )
            """
        )
        conn.execute(
            "INSERT INTO projects (id, slug, name) VALUES ('proj-leg', 'legacy', 'Legacy Project')"
        )
        conn.execute(
            "INSERT INTO auth_profiles (id, project_id, name, role_tag, extra_headers_ref) "
            "VALUES ('ap-leg', 'proj-leg', 'Old Cred', 'admin', '{\"X-Old\":\"123456789\"}')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)
    db_url = f"sqlite+aiosqlite:///{db_file}"
    await db_mod.init_db(db_url)
    # Run a second time to verify idempotency
    await db_mod.init_db(db_url)

    conn = sqlite3.connect(db_file)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(auth_profiles)").fetchall()}
        row = conn.execute(
            "SELECT id, name, auth_type, login_config FROM auth_profiles WHERE id='ap-leg'"
        ).fetchone()
    finally:
        conn.close()

    assert "auth_type" in cols
    assert "login_config" in cols
    assert row is not None
    assert row[1] == "Old Cred"
    assert row[2] == "headers"
    assert json.loads(row[3]) == {}

    # Also verify SQLAlchemy + AuthProfileOut reads the migrated legacy row cleanly
    from reconqa.models.tables import AuthProfile

    factory = db_mod.get_session_factory(db_url)
    async with factory() as session:
        loaded = await session.get(AuthProfile, "ap-leg")
        assert loaded is not None
        validated = AuthProfileOut.model_validate(loaded)
        assert validated.auth_type == "headers"
        assert validated.login_config == {}

    if db_mod._engine is not None:
        await db_mod._engine.dispose()


@pytest.mark.asyncio
async def test_explore_auth_modes_login_api_verify_profiles_prompts_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end tests for Bearer/Cookie/Basic/Headers/LoginAPI/Profile explore, verify-auth, test-login, prompts & MCP."""
    ws_dir = _setup_test_env(tmp_path, monkeypatch)

    from reconqa.server.app import create_app

    with _AuthGatedMockServer() as srv:
        fastapi_app = create_app()
        async with fastapi_app.router.lifespan_context(fastapi_app):
            transport = httpx.ASGITransport(app=fastapi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                # 0. Sanity: unauthenticated explore gets 0 endpoints from auth-gated server
                r_unauth = await client.post(
                    "/api/v1/explore",
                    json={"target_url": srv.base_url, "merge": False},
                )
                assert r_unauth.status_code == 201, r_unauth.text
                assert r_unauth.json()["summary"]["endpoints_count"] == 0

                # 1. Bearer, Cookie, Basic, Custom Headers unlock OpenAPI & JS without leaking secrets
                auth_modes_cases = [
                    (
                        "bearer",
                        {"auth_type": "bearer", "token": "secret-bearer-token-xyz987"},
                        ["secret-bearer-token-xyz987"],
                        "Authorization",
                    ),
                    (
                        "cookie",
                        {"auth_type": "cookie", "cookie": srv.expected_cookie},
                        ["secret-cookie-val-4567"],
                        "Cookie",
                    ),
                    (
                        "basic",
                        {"auth_type": "basic", "username": "qa_admin", "password": "Sup3rS3cretPwd!"},
                        ["Sup3rS3cretPwd!", srv.expected_basic],
                        "Authorization",
                    ),
                    (
                        "headers",
                        {"auth_type": "headers", "headers": {"X-Api-Key": srv.expected_custom_key}},
                        [srv.expected_custom_key],
                        "X-Api-Key",
                    ),
                ]
                for mode_name, auth_payload, secrets_to_hide, expected_hdr_name in auth_modes_cases:
                    r_exp = await client.post(
                        "/api/v1/explore",
                        json={
                            "target_url": srv.base_url,
                            "merge": False,
                            "auth": auth_payload,
                        },
                    )
                    assert r_exp.status_code == 201, f"{mode_name}: {r_exp.text}"
                    data = r_exp.json()
                    ep_paths = {ep["path"] for ep in data["discovered_endpoints"]}
                    assert "/api/v1/secret/orders" in ep_paths, mode_name
                    assert "/api/v1/secret/vault-items" in ep_paths, mode_name
                    assert data["seed"]["auth_mode"] == mode_name
                    assert expected_hdr_name in data["seed"]["auth_headers_detected"]

                    res_file = ws_dir / "explores" / data["explore_id"] / "explore_result.json"
                    raw_saved = res_file.read_text(encoding="utf-8")
                    for sec in secrets_to_hide:
                        assert sec not in raw_saved, f"Leaked secret in explore_result.json for {mode_name}"
                        assert sec not in json.dumps(data), f"Leaked secret in API response for {mode_name}"

                # 2. Login API exchange (JSON POST returning token + Set-Cookie) + save_as_profile=True
                proj_resp = await client.post("/api/v1/projects", json={"name": "Auth Explore Suite"})
                assert proj_resp.status_code == 201
                pid = proj_resp.json()["id"]
                await client.post(
                    f"/api/v1/projects/{pid}/targets",
                    json={"root_domain": "127.0.0.1", "base_url": srv.base_url},
                )

                r_login_exp = await client.post(
                    "/api/v1/explore",
                    json={
                        "target_url": srv.base_url,
                        "project_id": pid,
                        "merge": False,
                        "auth": {
                            "auth_type": "login_api",
                            "login_url": f"{srv.base_url}/api/auth/login",
                            "username": "qa_user",
                            "password": "LoginPass#2026",
                            "token_jsonpath": "data.accessToken",
                            "verify_url": f"{srv.base_url}/api/v1/me",
                            "save_as_profile": True,
                            "profile_name": "Auto Saved QA User",
                            "role_tag": "qa_admin",
                        },
                    },
                )
                assert r_login_exp.status_code == 201, r_login_exp.text
                login_data = r_login_exp.json()
                ep_paths = {ep["path"] for ep in login_data["discovered_endpoints"]}
                assert "/api/v1/secret/orders" in ep_paths
                assert "/api/v1/secret/vault-items" in ep_paths
                assert login_data["seed"]["auth_mode"] == "login_api"
                assert "Authorization" in login_data["seed"]["auth_headers_detected"]
                assert "Cookie" in login_data["seed"]["auth_headers_detected"]

                saved_prof = login_data.get("saved_auth_profile")
                assert saved_prof is not None
                assert saved_prof["name"] == "Auto Saved QA User"
                assert saved_prof["auth_type"] == "login_api"
                assert "LoginPass#2026" not in json.dumps(saved_prof)
                assert srv.login_token not in json.dumps(saved_prof)
                assert srv.login_cookie not in json.dumps(saved_prof)
                cred_id = saved_prof["id"]

                # 3. Saved credential_id works across /explore, /explore/jobs, and /projects/{id}/explore-all
                r_cred_exp = await client.post(
                    "/api/v1/explore",
                    json={
                        "target_url": srv.base_url,
                        "project_id": pid,
                        "credential_id": cred_id,
                        "merge": False,
                    },
                )
                assert r_cred_exp.status_code == 201, r_cred_exp.text
                assert "/api/v1/secret/orders" in {ep["path"] for ep in r_cred_exp.json()["discovered_endpoints"]}
                assert r_cred_exp.json()["seed"]["auth_mode"] == "profile"
                assert r_cred_exp.json()["seed"]["auth_profile_id"] == cred_id

                # Async /explore/jobs with credential_id
                r_job = await client.post(
                    "/api/v1/explore/jobs",
                    json={
                        "target_url": srv.base_url,
                        "project_id": pid,
                        "credential_id": cred_id,
                        "merge": False,
                    },
                )
                assert r_job.status_code == 202, r_job.text
                job_id = r_job.json()["job_id"]
                job_data: dict[str, Any] = {}
                for _ in range(100):
                    job_data = (await client.get(f"/api/v1/explore/jobs/{job_id}")).json()
                    if job_data["status"] in ("completed", "failed"):
                        break
                    await asyncio.sleep(0.05)
                assert job_data["status"] == "completed", job_data
                assert "/api/v1/secret/orders" in {
                    ep["path"] for ep in job_data["result"]["discovered_endpoints"]
                }
                assert srv.login_token not in json.dumps(job_data)

                # Batch /projects/{id}/explore-all with credential_id
                r_all = await client.post(
                    f"/api/v1/projects/{pid}/explore-all",
                    json={"credential_id": cred_id, "merge": False},
                )
                assert r_all.status_code == 201, r_all.text
                assert "/api/v1/secret/orders" in {ep["path"] for ep in r_all.json()["discovered_endpoints"]}
                assert srv.login_token not in json.dumps(r_all.json())

                # 4. POST /projects/{id}/auth-profiles/{pid}/test-login
                r_test_login = await client.post(
                    f"/api/v1/projects/{pid}/auth-profiles/{cred_id}/test-login"
                )
                assert r_test_login.status_code == 200, r_test_login.text
                tl_data = r_test_login.json()
                assert tl_data["ok"] is True
                assert tl_data["auth_type"] == "login_api"
                assert "Authorization" in tl_data["header_names"]
                assert "Cookie" in tl_data["header_names"]
                assert "LoginPass#2026" not in json.dumps(tl_data)
                assert srv.login_token not in json.dumps(tl_data)

                # 5. Burp-style Pre-flight Auth Verification: POST /api/v1/explore/verify-auth
                # 5a. Valid 200 with login_api
                r_v_ok = await client.post(
                    "/api/v1/explore/verify-auth",
                    json={
                        "verify_url": f"{srv.base_url}/api/v1/me",
                        "auth": {
                            "auth_type": "bearer",
                            "token": "secret-bearer-token-xyz987",
                        },
                    },
                )
                assert r_v_ok.status_code == 200, r_v_ok.text
                v_ok = r_v_ok.json()
                assert v_ok["valid"] is True
                assert v_ok["verdict"] == "valid"
                assert v_ok["status_code"] == 200
                assert "secret-bearer-token-xyz987" not in json.dumps(v_ok)

                # 5b. Denied 401 with bad token
                r_v_401 = await client.post(
                    "/api/v1/explore/verify-auth",
                    json={
                        "verify_url": f"{srv.base_url}/api/v1/me",
                        "auth": {"auth_type": "bearer", "token": "wrong-token-0000"},
                    },
                )
                assert r_v_401.status_code == 200
                assert r_v_401.json()["valid"] is False
                assert r_v_401.json()["verdict"] == "denied"
                assert r_v_401.json()["status_code"] == 401

                # 5c. Denied 302 redirect to /login (caught because follow_redirects=False!)
                r_v_302 = await client.post(
                    "/api/v1/explore/verify-auth",
                    json={
                        "verify_url": f"{srv.base_url}/api/v1/verify-redirect",
                        "project_id": pid,
                        "credential_id": cred_id,
                    },
                )
                assert r_v_302.status_code == 200
                v_302 = r_v_302.json()
                assert v_302["valid"] is False
                assert v_302["verdict"] == "denied"
                assert v_302["status_code"] == 302
                assert v_302["redirected_to"] == "/login?next=/dashboard"

                # 5d. Denied 200 with Chinese biz_code 40100
                r_v_biz = await client.post(
                    "/api/v1/explore/verify-auth",
                    json={
                        "verify_url": f"{srv.base_url}/api/v1/verify-biz-denied",
                        "project_id": pid,
                        "credential_id": cred_id,
                    },
                )
                assert r_v_biz.status_code == 200
                v_biz = r_v_biz.json()
                assert v_biz["valid"] is False
                assert v_biz["verdict"] == "denied"
                assert v_biz["biz_code"] == 40100

                # 5e. SSRF metadata block
                r_v_ssrf = await client.post(
                    "/api/v1/explore/verify-auth",
                    json={"verify_url": "http://169.254.169.254/latest/meta-data/"},
                )
                assert r_v_ssrf.status_code == 400

                # 6. Acceptance prompt (mode=har_explore) includes auth metadata & zero raw secrets
                for lang in ("zh", "en"):
                    r_prompt = await client.get(
                        f"/api/v1/projects/{pid}/acceptance-prompt",
                        params={"mode": "har_explore", "lang": lang},
                    )
                    assert r_prompt.status_code == 200, r_prompt.text
                    md = r_prompt.json()["prompt_markdown"]
                    assert "login_api" in md
                    assert f"{srv.base_url}/api/auth/login" in md
                    assert "qa_user" in md
                    assert "reconqa_list_auth_profiles" in md
                    assert "LoginPass#2026" not in md
                    assert srv.login_token not in md
                    assert srv.login_cookie not in md

                # 7. MCP reconqa_list_auth_profiles & reconqa_explore_from_seed(credential_id=...)
                mcp = create_mcp_server("http://testserver", transport=transport)
                list_res = _extract_mcp_json(
                    await mcp.call_tool("reconqa_list_auth_profiles", {"project_id": pid})
                )
                assert list_res["count"] == 1
                prof_meta = list_res["auth_profiles"][0]
                assert prof_meta["id"] == cred_id
                assert prof_meta["auth_type"] == "login_api"
                assert prof_meta["login_url"] == f"{srv.base_url}/api/auth/login"
                assert prof_meta["username"] == "qa_user"
                assert "Authorization" in prof_meta["header_names"]
                assert "LoginPass#2026" not in json.dumps(list_res)
                assert srv.login_token not in json.dumps(list_res)

                exp_mcp = _extract_mcp_json(
                    await mcp.call_tool(
                        "reconqa_explore_from_seed",
                        {
                            "target_url": srv.base_url,
                            "project_id": pid,
                            "credential_id": cred_id,
                        },
                    )
                )
                assert exp_mcp["seed"]["auth_mode"] == "profile"
                assert "Authorization" in exp_mcp["seed"]["auth_headers_detected"]
                assert any(ep["path"] == "/api/v1/secret/orders" for ep in exp_mcp["top_endpoints"])
                assert srv.login_token not in json.dumps(exp_mcp)

        if db_mod._engine is not None:
            await db_mod._engine.dispose()


def test_cross_origin_redirect_strips_custom_auth_headers() -> None:
    """Verify make_scope_request_hook strips custom auth headers (X-Api-Key / satoken) on 3xx cross-origin hops."""
    seen_by_host: dict[str, dict[str, str]] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        seen_by_host[host] = {k.lower(): v for k, v in request.headers.items()}
        if host == "app.example.test":
            return httpx.Response(
                302,
                headers={"Location": "https://cdn.thirdparty.io/bundle.js"},
            )
        return httpx.Response(200, text="console.log('ok');")

    auth_hdrs = {
        "Authorization": "Bearer top-secret-jwt",
        "X-Api-Key": "custom-secret-key",
        "satoken": "sa-secret-val-999",
    }
    scope_hook = make_scope_request_hook("example.test", None, list(auth_hdrs.keys()))
    with httpx.Client(
        transport=httpx.MockTransport(_handler),
        follow_redirects=True,
        event_hooks={"request": [scope_hook]},
    ) as client:
        resp = client.get("https://app.example.test/start", headers=auth_hdrs)
        assert resp.status_code == 200

    assert seen_by_host["app.example.test"]["authorization"] == "Bearer top-secret-jwt"
    assert seen_by_host["app.example.test"]["x-api-key"] == "custom-secret-key"
    assert seen_by_host["app.example.test"]["satoken"] == "sa-secret-val-999"
    assert "authorization" not in seen_by_host["cdn.thirdparty.io"]
    assert "x-api-key" not in seen_by_host["cdn.thirdparty.io"]
    assert "satoken" not in seen_by_host["cdn.thirdparty.io"]


@pytest.mark.asyncio
async def test_p1_security_hardening_verify_readonly_env_preservation_url_sanitization_and_batch_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Round 1 P1 fixes: read-only verify-auth, verify_url priority, empty-cred denial, ENV preservation, URL sanitization & batch scope/storage_state."""
    ws_dir = _setup_test_env(tmp_path, monkeypatch)
    monkeypatch.setenv("RECONQA_KEEP_HDR_TOK", "env-hdr-secret-1122")
    monkeypatch.setenv("RECONQA_BASIC_PWD", "EnvBasicPwd#99")
    monkeypatch.delenv("RECONQA_UNSET_TOKEN_XYZ", raising=False)

    login_calls = 0

    class _VerifyAndRefreshHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, code: int, body: str, ctype: str = "application/json") -> None:
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            nonlocal login_calls
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if self.path == "/auth/login":
                login_calls += 1
                self._send(200, json.dumps({"token": f"fresh-tok-{login_calls}-abcd"}))
                return
            self._send(404, "{}")

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            authz = (self.headers.get("Authorization") or "").strip()
            if path == "/public-home":
                self._send(200, "<html><body>Public Home</body></html>", "text/html")
            elif path == "/private-check":
                if authz == "Bearer fresh-tok-1-abcd" or authz == "Bearer fresh-tok-2-abcd":
                    self._send(200, json.dumps({"code": 0, "ok": True}))
                else:
                    self._send(401, json.dumps({"code": 401, "msg": "expired"}))
            else:
                self._send(404, "{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _VerifyAndRefreshHandler)
    base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()

    from reconqa.models.tables import AuthProfile
    from reconqa.server.app import create_app
    from reconqa.server.routers import explore as explore_router

    try:
        fastapi_app = create_app()
        async with fastapi_app.router.lifespan_context(fastapi_app):
            transport = httpx.ASGITransport(app=fastapi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pid = (await client.post("/api/v1/projects", json={"name": "P1 Hardening"})).json()["id"]

                # 1. verify-auth is strictly read-only (save_as_profile=True MUST NOT create a profile)
                r_v_ro = await client.post(
                    "/api/v1/explore/verify-auth",
                    json={
                        "project_id": pid,
                        "verify_url": f"{base_url}/public-home",
                        "auth": {
                            "auth_type": "bearer",
                            "token": "Bearer fresh-tok-1-abcd",
                            "save_as_profile": True,
                            "profile_name": "Should Not Be Saved",
                        },
                    },
                )
                assert r_v_ro.status_code == 200
                profs_after = (await client.get(f"/api/v1/projects/{pid}/auth-profiles")).json()
                assert profs_after == []

                # 2. verify-auth denies when ENV ref is unset (empty headers) even against public 200 homepage
                r_v_empty = await client.post(
                    "/api/v1/explore/verify-auth",
                    json={
                        "verify_url": f"{base_url}/public-home",
                        "auth": {"auth_type": "bearer", "token": "ENV:RECONQA_UNSET_TOKEN_XYZ"},
                    },
                )
                assert r_v_empty.status_code == 200
                assert r_v_empty.json()["valid"] is False
                assert r_v_empty.json()["verdict"] == "denied"

                # 3. Profile verify_url takes precedence over body.target_url!
                prof_login = (
                    await client.post(
                        f"/api/v1/projects/{pid}/auth-profiles",
                        json={
                            "name": "Expiring Login API",
                            "role_tag": "admin",
                            "auth_type": "login_api",
                            "extra_headers_ref": {"Authorization": "Bearer stale-expired-token"},
                            "login_config": {
                                "login_url": f"{base_url}/auth/login",
                                "username": "u",
                                "password": "p",
                                "verify_url": f"{base_url}/private-check",
                            },
                        },
                    )
                ).json()
                prof_login_id = prof_login["id"]

                # Passing public target_url without explicit verify_url must still hit profile's /private-check (401 on stale token)!
                r_v_prec = await client.post(
                    "/api/v1/explore/verify-auth",
                    json={
                        "project_id": pid,
                        "credential_id": prof_login_id,
                        "target_url": f"{base_url}/public-home",
                    },
                )
                assert r_v_prec.status_code == 200
                assert r_v_prec.json()["valid"] is False
                assert r_v_prec.json()["status_code"] == 401

                # 4. test-login on login_api forces fresh login exchange, updates cached token, and passes verify_url!
                r_tl_refresh = await client.post(
                    f"/api/v1/projects/{pid}/auth-profiles/{prof_login_id}/test-login"
                )
                assert r_tl_refresh.status_code == 200, r_tl_refresh.text
                assert r_tl_refresh.json()["ok"] is True
                assert login_calls == 1

                # Now verify-auth with the refreshed profile succeeds (200 on /private-check)!
                r_v_after_refresh = await client.post(
                    "/api/v1/explore/verify-auth",
                    json={
                        "project_id": pid,
                        "credential_id": prof_login_id,
                        "target_url": f"{base_url}/public-home",
                    },
                )
                assert r_v_after_refresh.status_code == 200
                assert r_v_after_refresh.json()["valid"] is True

                # 5. test-login on headers/basic with ENV:RECONQA_* does NOT overwrite DB with resolved plaintext
                prof_env_hdr = (
                    await client.post(
                        f"/api/v1/projects/{pid}/auth-profiles",
                        json={
                            "name": "Env Headers Cred",
                            "role_tag": "qa",
                            "auth_type": "headers",
                            "extra_headers_ref": {"X-Auth-Token": "ENV:RECONQA_KEEP_HDR_TOK"},
                        },
                    )
                ).json()
                prof_env_basic = (
                    await client.post(
                        f"/api/v1/projects/{pid}/auth-profiles",
                        json={
                            "name": "Env Basic Cred",
                            "role_tag": "qa",
                            "auth_type": "basic",
                            "login_config": {
                                "username": "qa_bot",
                                "password": "ENV:RECONQA_BASIC_PWD",
                            },
                        },
                    )
                ).json()
                r_tl_hdr = await client.post(
                    f"/api/v1/projects/{pid}/auth-profiles/{prof_env_hdr['id']}/test-login"
                )
                r_tl_basic = await client.post(
                    f"/api/v1/projects/{pid}/auth-profiles/{prof_env_basic['id']}/test-login"
                )
                assert r_tl_hdr.json()["ok"] is True
                assert r_tl_basic.json()["ok"] is True

                factory = db_mod.get_session_factory()
                async with factory() as session:
                    db_hdr = await session.get(AuthProfile, prof_env_hdr["id"])
                    db_basic = await session.get(AuthProfile, prof_env_basic["id"])
                    assert db_hdr is not None
                    assert db_basic is not None
                    assert db_hdr.extra_headers_ref == {"X-Auth-Token": "ENV:RECONQA_KEEP_HDR_TOK"}
                    assert db_basic.extra_headers_ref == {}

                # 6. URL with ?token=secret123 & user:pass@ is sanitized in explore_result.json & _EXPLORE_JOBS
                dirty_url = f"http://alice:SecPass88@127.0.0.1:{srv.server_address[1]}/public-home?token=secret123&view=1"
                r_dirty_job = await client.post(
                    "/api/v1/explore/jobs",
                    json={"target_url": dirty_url, "merge": False},
                )
                assert r_dirty_job.status_code == 202
                dirty_jid = r_dirty_job.json()["job_id"]
                assert "secret123" not in json.dumps(r_dirty_job.json())
                assert "SecPass88" not in json.dumps(r_dirty_job.json())
                dirty_job_final: dict[str, Any] = {}
                for _ in range(100):
                    dirty_job_final = (await client.get(f"/api/v1/explore/jobs/{dirty_jid}")).json()
                    if dirty_job_final["status"] in ("completed", "failed"):
                        break
                    await asyncio.sleep(0.05)
                assert dirty_job_final["status"] == "completed", dirty_job_final
                assert "secret123" not in json.dumps(dirty_job_final)
                assert "SecPass88" not in json.dumps(dirty_job_final)
                exp_file = ws_dir / "explores" / dirty_job_final["explore_id"] / "explore_result.json"
                raw_exp = exp_file.read_text(encoding="utf-8")
                assert "secret123" not in raw_exp
                assert "SecPass88" not in raw_exp

                # 7. Batch explore enforces root-domain scope & filters storage_state cookies per portal host
                state_file = ws_dir / "multi_portal_state.json"
                state_file.write_text(
                    json.dumps(
                        {
                            "cookies": [
                                {"name": "portal_a_sid", "value": "cookie-val-a-111", "domain": "a.example.test"},
                                {"name": "portal_b_sid", "value": "cookie-val-b-222", "domain": "b.example.test"},
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                captured_batch_calls: list[dict[str, Any]] = []

                def _fake_explore_from_seed(out_dir: Path, **kwargs: Any) -> dict[str, Any]:
                    captured_batch_calls.append(dict(kwargs))
                    t_url = str(kwargs.get("target_url") or "")
                    return {
                        "explore_id": str(kwargs.get("explore_id") or "exp1"),
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "mode": "passive",
                        "seed": {
                            "type": "url",
                            "base_url": t_url,
                            "root_domain": "example.test",
                            "seed_url": t_url,
                            "api_prefix": "",
                            "auth_headers_detected": [],
                            "auth_mode": "profile",
                            "sibling_api_hosts": [],
                        },
                        "summary": {"scans_count": 1, "endpoints_count": 0},
                        "scans_count": 1,
                        "scans": [],
                        "discovered_endpoints": [],
                        "discovered_portals": [],
                        "openapi_docs_found": [],
                        "spa_routes": [],
                        "micro_frontends": [],
                        "subdomains": [],
                        "live_hosts": [],
                        "warnings": [],
                    }

                monkeypatch.setattr(explore_router, "explore_from_seed", _fake_explore_from_seed)
                from reconqa.modules.discovery.auth_resolver import ResolvedAuth

                batch_auth = ResolvedAuth(
                    headers={"Cookie": "portal_a_sid=cookie-val-a-111; portal_b_sid=cookie-val-b-222"},
                    auth_header_names=["Cookie"],
                    auth_mode="profile",
                    storage_state_path=str(state_file),
                    non_storage_headers={"X-Shared": "shared-val-999"},
                )
                await explore_router._run_batch_explore_urls(
                    [
                        "https://a.example.test",
                        "https://b.example.test",
                        "https://unrelated.other.net",
                    ],
                    mode="passive",
                    probe_openapi_docs=False,
                    crawl_js=False,
                    discover_portals=False,
                    depth=1,
                    merge=False,
                    project_id=pid,
                    resolved_auth=batch_auth,
                )

                by_url = {c["target_url"]: c for c in captured_batch_calls}
                assert by_url["https://a.example.test"]["allowed_root_domain"] == "example.test"
                assert by_url["https://unrelated.other.net"]["allowed_root_domain"] == "example.test"
                assert by_url["https://a.example.test"]["extra_headers"]["Cookie"] == "portal_a_sid=cookie-val-a-111"
                assert by_url["https://b.example.test"]["extra_headers"]["Cookie"] == "portal_b_sid=cookie-val-b-222"
                assert "Cookie" not in by_url["https://unrelated.other.net"]["extra_headers"]

        if db_mod._engine is not None:
            await db_mod._engine.dispose()
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=3)
