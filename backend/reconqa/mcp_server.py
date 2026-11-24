"""Thin FastMCP server for ReconQA AI Agent collaboration (Human-in-the-Loop).

Proxies all operations to the running ReconQA FastAPI backend so every project,
knowledge document, exploration session, analysis run, and finding created by an
AI agent immediately appears live in the Vue 3 Web Console.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

try:
    from mcp.server.fastmcp import FastMCP

    _MCP_V2 = False
except (ImportError, ModuleNotFoundError):  # pragma: no cover - mcp>=2.0
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[assignment,no-redef]

    _MCP_V2 = True

DEFAULT_API_URL = "http://127.0.0.1:8000"
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_VALID_DOC_TYPES = frozenset(
    {"prd", "api_spec", "business_rule", "test_notes", "architecture", "other"}
)
_VALID_EXTERNAL_TOOLS = frozenset({"java_test", "schemathesis", "nuclei", "k6"})
_MAX_REPLAY_TEXT_CHARS = 4096
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed"})
_URL_RE = re.compile(r"https?://[^\s'\"<>\\]+", re.IGNORECASE)
_HOST_HEADER_RE = re.compile(r"(?im)^Host:\s*([a-zA-Z0-9._-]+(?::\d+)?)\s*$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalize_api_url(api_url: str | None = None) -> str:
    """Normalize a ReconQA base URL so it always points to `/api/v1`."""
    raw = (api_url or os.environ.get("RECONQA_API_URL") or DEFAULT_API_URL).strip()
    if not raw:
        raw = DEFAULT_API_URL
    if "://" not in raw:
        raw = f"http://{raw}"
    cleaned = raw.rstrip("/")
    if not cleaned.endswith("/api/v1"):
        cleaned = f"{cleaned}/api/v1"
    return cleaned


def should_bypass_env_proxy(url: str) -> bool:
    """Return True for loopback, private LAN, `.local`, and Tailscale CGNAT hosts."""
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return True
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _TAILSCALE_CGNAT)
    )


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "project"


def _truncate_str(value: str, limit: int = 400) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


def _compact_evidence(evidence: Any) -> Any:
    if isinstance(evidence, str):
        return _truncate_str(evidence, 400)
    if isinstance(evidence, list):
        return [_compact_evidence(item) for item in evidence[:10]]
    if isinstance(evidence, dict):
        out: dict[str, Any] = {}
        for idx, (k, v) in enumerate(evidence.items()):
            if idx >= 15:
                out["_truncated_keys"] = len(evidence) - 15
                break
            out[str(k)] = _compact_evidence(v)
        return out
    return evidence


def _compact_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "module": j.get("module"),
            "status": j.get("status"),
            "started_at": j.get("started_at"),
            "finished_at": j.get("finished_at"),
        }
        for j in jobs
    ]


def _detect_origin_from_text(content: str) -> tuple[str | None, str | None]:
    """Best-effort extraction of (origin, hostname) from cURL/fetch/Raw HTTP/HAR text."""
    for match in _URL_RE.finditer(content):
        candidate = match.group(0).rstrip(".,);'\"")
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.hostname:
            return f"{parsed.scheme}://{parsed.netloc}", parsed.hostname.lower()

    host_match = _HOST_HEADER_RE.search(content)
    if host_match:
        netloc = host_match.group(1).strip()
        host = netloc.split(":", 1)[0].lower()
        scheme = "http" if netloc.endswith(":80") or host in {"localhost", "127.0.0.1"} else "https"
        return f"{scheme}://{netloc}", host

    return None, None


def _resolve_har_paths(har_paths: list[str]) -> list[Path]:
    """Expand file and directory paths into a deduplicated ordered list of `.har` files."""
    if not har_paths:
        raise ValueError("Provide at least one .har file path or directory containing *.har files.")
    resolved: list[Path] = []
    seen: set[Path] = set()
    for raw in har_paths:
        item = str(raw or "").strip()
        if not item:
            continue
        candidate = Path(item).expanduser()
        if not candidate.exists():
            raise ValueError(f"HAR path does not exist: {item}")
        if candidate.is_dir():
            for har_file in sorted(candidate.glob("*.har")):
                if har_file.is_file():
                    key = har_file.resolve()
                    if key not in seen:
                        seen.add(key)
                        resolved.append(har_file)
        elif candidate.is_file():
            key = candidate.resolve()
            if key not in seen:
                seen.add(key)
                resolved.append(candidate)
    if not resolved:
        raise ValueError("No .har files found in the provided har_paths.")
    return resolved


def _detect_origin_from_har_files(har_files: list[Path]) -> tuple[str | None, str | None]:
    """Detect primary (origin, hostname) from one or more HAR 1.2 JSON files."""
    counts: Counter[tuple[str, str]] = Counter()
    for har_file in har_files:
        try:
            doc = json.loads(har_file.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        entries = (doc.get("log") or {}).get("entries") if isinstance(doc, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = str((entry.get("request") or {}).get("url") or "")
            parsed = urlparse(url)
            if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.hostname:
                counts[(f"{parsed.scheme}://{parsed.netloc}", parsed.hostname.lower())] += 1
    if not counts:
        return None, None
    return counts.most_common(1)[0][0]


class _ReconQAClient:
    """Thin async HTTP proxy around ReconQA's FastAPI `/api/v1` endpoints."""

    def __init__(
        self,
        api_url: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_base = normalize_api_url(api_url)
        self._transport = transport
        self._injected_client = http_client
        self._trust_env = not should_bypass_env_proxy(self.api_base)

    @asynccontextmanager
    async def _session(self, timeout: float = 60.0) -> AsyncIterator[httpx.AsyncClient]:
        if self._injected_client is not None:
            yield self._injected_client
            return
        async with httpx.AsyncClient(
            base_url=self.api_base,
            timeout=timeout,
            trust_env=self._trust_env,
            transport=self._transport,
        ) as client:
            yield client

    def _resolve_url(self, path: str) -> str:
        rel = path if path.startswith("/") else f"/{path}"
        if self._injected_client is not None and not str(self._injected_client.base_url):
            return f"{self.api_base}{rel}"
        return rel

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 60.0,
        allow_404: bool = False,
    ) -> Any:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        async with self._session(timeout=timeout) as client:
            try:
                resp = await client.request(
                    method,
                    self._resolve_url(path),
                    params=clean_params or None,
                    json=json_body,
                    timeout=timeout,
                )
            except httpx.RequestError as exc:
                raise RuntimeError(
                    f"ReconQA API unreachable at {self.api_base}: {exc}. "
                    "Ensure `reconqa serve` is running or set RECONQA_API_URL."
                ) from exc

        if allow_404 and resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(self._format_http_error(resp))
        if not resp.content:
            return {}
        return resp.json()

    async def request_multipart(
        self,
        path: str,
        *,
        files: list[tuple[str, tuple[str, bytes, str]]],
        data: dict[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> Any:
        clean_data = {k: str(v) for k, v in (data or {}).items() if v is not None}
        async with self._session(timeout=timeout) as client:
            try:
                resp = await client.post(
                    self._resolve_url(path),
                    files=files,
                    data=clean_data or None,
                    timeout=timeout,
                )
            except httpx.RequestError as exc:
                raise RuntimeError(
                    f"ReconQA API unreachable at {self.api_base}: {exc}. "
                    "Ensure `reconqa serve` is running or set RECONQA_API_URL."
                ) from exc

        if resp.status_code >= 400:
            raise RuntimeError(self._format_http_error(resp))
        if not resp.content:
            return {}
        return resp.json()

    async def request_text(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> str:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        async with self._session(timeout=timeout) as client:
            try:
                resp = await client.request(
                    method,
                    self._resolve_url(path),
                    params=clean_params or None,
                    timeout=timeout,
                )
            except httpx.RequestError as exc:
                raise RuntimeError(
                    f"ReconQA API unreachable at {self.api_base}: {exc}. "
                    "Ensure `reconqa serve` is running or set RECONQA_API_URL."
                ) from exc

        if resp.status_code >= 400:
            raise RuntimeError(self._format_http_error(resp))
        return resp.text

    @staticmethod
    def _format_http_error(resp: httpx.Response) -> str:
        detail = ""
        try:
            payload = resp.json()
            if isinstance(payload, dict) and payload.get("detail") is not None:
                detail = str(payload["detail"])
            else:
                detail = json.dumps(payload, ensure_ascii=False)
        except Exception:
            detail = resp.text.strip()
        return f"ReconQA API error {resp.status_code}: {_truncate_str(detail or resp.reason_phrase, 500)}"

    async def poll_run(
        self,
        run_id: str,
        *,
        max_wait_seconds: float,
        poll_interval: float = 0.4,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait_seconds
        run_data: dict[str, Any] = await self.request_json("GET", f"/runs/{run_id}")
        while (
            str(run_data.get("status", "")).lower() not in _TERMINAL_RUN_STATUSES
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(poll_interval)
            run_data = await self.request_json("GET", f"/runs/{run_id}")
        return run_data


def create_mcp_server(
    api_url: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastMCP:
    """Create a FastMCP server bound to a ReconQA FastAPI backend."""
    client = _ReconQAClient(api_url=api_url, transport=transport, http_client=http_client)

    mcp = FastMCP(
        name="ReconQA",
        instructions=(
            "ReconQA Human-in-the-Loop Web/API testing platform. "
            "Use explore-first discovery, persist PRDs/business rules in the Project Knowledge Base, "
            "ingest DevTools cURL/fetch/HAR traffic for OpenAPI & correlation modeling, "
            "and trigger Java automation, performance, or security modules on demand."
        ),
    )

    # --- 19 Token-Efficient MCP Tools ---

    async def _resolve_or_create_project(
        project_id: str | None,
        project_name: str | None,
        detected_origin: str | None,
        detected_host: str | None,
        source_label: str,
    ) -> tuple[str, str]:
        resolved_name = (project_name or "").strip() or detected_host or "Agent Traffic"
        if project_id:
            existing_proj = await client.request_json("GET", f"/projects/{project_id}")
            return project_id, str(existing_proj.get("name") or resolved_name)

        desired_slug = _slugify(resolved_name)
        projects = await client.request_json("GET", "/projects")
        matched = next(
            (
                p
                for p in (projects or [])
                if str(p.get("slug") or "").lower() == desired_slug
                or str(p.get("name") or "").strip().lower() == resolved_name.strip().lower()
            ),
            None,
        )
        if matched is not None:
            return str(matched["id"]), str(matched.get("name") or resolved_name)

        created = await client.request_json(
            "POST",
            "/projects",
            json_body={
                "name": resolved_name,
                "description": f"Auto-created from MCP {source_label} ({detected_host or resolved_name})",
            },
        )
        new_id = str(created["id"])
        new_name = str(created.get("name") or resolved_name)
        if detected_origin and detected_host:
            try:
                await client.request_json(
                    "POST",
                    f"/projects/{new_id}/targets",
                    json_body={
                        "root_domain": detected_host,
                        "base_url": detected_origin,
                        "scope_rules": {},
                    },
                )
            except RuntimeError:
                pass
        return new_id, new_name

    @mcp.tool()
    async def reconqa_list_projects() -> dict[str, Any]:
        """List all ReconQA projects visible in the Web Console."""
        projects = await client.request_json("GET", "/projects")
        items = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "slug": p.get("slug"),
                "description": _truncate_str(str(p.get("description") or ""), 200),
                "created_at": p.get("created_at"),
            }
            for p in (projects or [])
        ]
        return {"count": len(items), "projects": items}

    @mcp.tool()
    async def reconqa_create_project(
        name: str,
        description: str = "",
        root_domain: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        """Create a ReconQA project and optionally register its initial target domain/URL."""
        project = await client.request_json(
            "POST",
            "/projects",
            json_body={"name": name, "description": description},
        )
        target_out: dict[str, Any] | None = None
        if base_url or root_domain:
            raw_base = (base_url or "").strip()
            raw_domain = (root_domain or "").strip()
            if not raw_base and raw_domain:
                raw_base = (
                    raw_domain if "://" in raw_domain else f"https://{raw_domain.strip('/')}"
                )
            elif raw_base and "://" not in raw_base:
                raw_base = f"https://{raw_base}"
            if not raw_domain and raw_base:
                raw_domain = (urlparse(raw_base).hostname or raw_base).lower()
            target_out = await client.request_json(
                "POST",
                f"/projects/{project['id']}/targets",
                json_body={
                    "root_domain": raw_domain,
                    "base_url": raw_base,
                    "scope_rules": {},
                },
            )
        return {
            "project": {
                "id": project.get("id"),
                "name": project.get("name"),
                "slug": project.get("slug"),
                "description": project.get("description", ""),
            },
            "target": target_out,
        }

    @mcp.tool()
    async def reconqa_list_knowledge(
        project_id: str,
        doc_type: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        """Search or list PRDs, API specs, business rules, and notes in a project's Knowledge Base."""
        docs = await client.request_json(
            "GET",
            f"/projects/{project_id}/knowledge",
            params={"doc_type": doc_type, "q": q},
        )
        items = [
            {
                "id": d.get("id"),
                "title": d.get("title"),
                "doc_type": d.get("doc_type"),
                "source_kind": d.get("source_kind"),
                "tags": d.get("tags") or [],
                "related_endpoints": d.get("related_endpoints") or [],
                "external_url": d.get("external_url"),
                "size_bytes": d.get("size_bytes", 0),
                "excerpt": _truncate_str(str(d.get("content") or ""), 300),
                "updated_at": d.get("updated_at"),
            }
            for d in (docs or [])
        ]
        return {
            "project_id": project_id,
            "count": len(items),
            "docs": items,
            "full_digest_resource": f"reconqa://projects/{project_id}/knowledge",
        }

    @mcp.tool()
    async def reconqa_add_knowledge_doc(
        project_id: str,
        title: str,
        content: str,
        doc_type: str = "prd",
        tags: list[str] | None = None,
        related_endpoints: list[str] | None = None,
        external_url: str | None = None,
    ) -> dict[str, Any]:
        """Save a PRD, business rule, API note, or architecture reference into a project's Knowledge Base."""
        norm_type = (doc_type or "prd").strip().lower()
        if norm_type not in _VALID_DOC_TYPES:
            norm_type = "other"
        source_kind = "link" if (external_url and not content.strip()) else "markdown"
        doc = await client.request_json(
            "POST",
            f"/projects/{project_id}/knowledge",
            json_body={
                "title": title,
                "doc_type": norm_type,
                "source_kind": source_kind,
                "content": content,
                "external_url": external_url,
                "tags": tags or [],
                "related_endpoints": related_endpoints or [],
            },
        )
        return {
            "id": doc.get("id"),
            "project_id": doc.get("project_id"),
            "title": doc.get("title"),
            "doc_type": doc.get("doc_type"),
            "source_kind": doc.get("source_kind"),
            "tags": doc.get("tags") or [],
            "related_endpoints": doc.get("related_endpoints") or [],
            "external_url": doc.get("external_url"),
            "size_bytes": doc.get("size_bytes", 0),
        }

    @mcp.tool()
    async def reconqa_explore_from_seed(
        seed_curl: str | None = None,
        target_url: str | None = None,
        mode: str = "passive",
        probe_openapi_docs: bool = True,
        crawl_js: bool = True,
    ) -> dict[str, Any]:
        """Discover API endpoints, Swagger/OpenAPI docs, SPA routes, and sibling API hosts from a seed cURL or URL."""
        if not (seed_curl and seed_curl.strip()) and not (target_url and target_url.strip()):
            raise ValueError("Provide either seed_curl or target_url.")
        eff_mode = "active" if mode.strip().lower() == "active" else "passive"
        data = await client.request_json(
            "POST",
            "/explore",
            json_body={
                "seed_curl": seed_curl,
                "target_url": target_url,
                "mode": eff_mode,
                "probe_openapi_docs": probe_openapi_docs,
                "crawl_js": crawl_js,
            },
            timeout=90.0,
        )
        seed = data.get("seed") or {}
        endpoints = list(data.get("discovered_endpoints") or [])
        top_endpoints = [
            {
                "id": ep.get("id") or f"{ep.get('method', 'GET')} {ep.get('path', '/')}",
                "method": ep.get("method", "GET"),
                "path": ep.get("path", "/"),
                "source": ep.get("source", ""),
                "inferred_params": list(ep.get("inferred_params") or [])[:8],
            }
            for ep in endpoints[:40]
        ]
        openapi_docs = [
            {
                "url": doc.get("url"),
                "endpoints_count": doc.get("endpoints_count", 0),
            }
            for doc in (data.get("openapi_docs_found") or [])[:10]
        ]
        return {
            "explore_id": data.get("explore_id"),
            "mode": data.get("mode", eff_mode),
            "seed": {
                "type": seed.get("type"),
                "base_url": seed.get("base_url"),
                "root_domain": seed.get("root_domain"),
                "api_prefix": seed.get("api_prefix"),
                "auth_headers_detected": seed.get("auth_headers_detected") or [],
                "sibling_api_hosts": seed.get("sibling_api_hosts") or [],
            },
            "summary": data.get("summary") or {},
            "openapi_docs_found": openapi_docs,
            "top_endpoints": top_endpoints,
            "endpoints_truncated": max(0, len(endpoints) - len(top_endpoints)),
            "spa_routes_sample": list(data.get("spa_routes") or [])[:20],
            "warnings": list(data.get("warnings") or [])[:10],
        }

    @mcp.tool()
    async def reconqa_promote_explore_to_run(
        explore_id: str,
        project_id: str | None = None,
        project_name: str | None = None,
        modules: list[str] | None = None,
        selected_endpoints: list[str] | None = None,
    ) -> dict[str, Any]:
        """Promote an Explore session into a ReconQA testing run (defaults to fast ['merge'] hub analysis)."""
        effective_modules = list(modules) if modules else ["merge"]
        run_data = await client.request_json(
            "POST",
            f"/explore/{explore_id}/promote-to-run",
            json_body={
                "project_id": project_id,
                "project_name": project_name,
                "modules": effective_modules,
                "selected_endpoints": selected_endpoints,
            },
        )
        run_id = str(run_data["id"])
        if str(run_data.get("status", "")).lower() not in _TERMINAL_RUN_STATUSES:
            run_data = await client.poll_run(run_id, max_wait_seconds=10.0)
        return {
            "run_id": run_id,
            "project_id": run_data.get("project_id"),
            "status": run_data.get("status"),
            "modules": effective_modules,
            "jobs": _compact_jobs(run_data.get("jobs") or []),
            "summary_metrics": run_data.get("summary_metrics") or {},
            "resources": {
                "openapi": f"reconqa://runs/{run_id}/openapi",
                "correlation": f"reconqa://runs/{run_id}/correlation",
            },
        }

    @mcp.tool()
    async def reconqa_import_traffic_and_analyze(
        content: str,
        scenario: str = "01_agent",
        project_id: str | None = None,
        project_name: str | None = None,
        format: str = "auto",
    ) -> dict[str, Any]:
        """Import cURL, fetch, Raw HTTP, or HAR JSON into a project, run fast hub analysis, and return a compact summary."""
        if not content or not content.strip():
            raise ValueError("content must not be empty.")

        detected_origin, detected_host = _detect_origin_from_text(content)
        resolved_project_id, resolved_project_name = await _resolve_or_create_project(
            project_id,
            project_name,
            detected_origin,
            detected_host,
            source_label=f"traffic import ({scenario})",
        )

        run_created = await client.request_json(
            "POST",
            f"/projects/{resolved_project_id}/runs",
            params={"auto_start": "false"},
            json_body={
                "target_url": detected_origin,
                "modules": ["merge"],
                "trigger_source": "cli",
            },
        )
        run_id = str(run_created["id"])

        import_res = await client.request_json(
            "POST",
            f"/runs/{run_id}/import-text",
            json_body={
                "content": content,
                "scenario": scenario,
                "format": format,
            },
        )
        await client.request_json("POST", f"/runs/{run_id}/start")
        run_data = await client.poll_run(run_id, max_wait_seconds=15.0)

        return {
            "run_id": run_id,
            "project_id": resolved_project_id,
            "project_name": resolved_project_name,
            "status": run_data.get("status"),
            "import_summary": {
                "scenario": import_res.get("scenario", scenario),
                "detected_format": import_res.get("detected_format", format),
                "requests_imported": import_res.get("requests_imported", 0),
            },
            "jobs": _compact_jobs(run_data.get("jobs") or []),
            "summary_metrics": run_data.get("summary_metrics") or {},
            "resources": {
                "openapi": f"reconqa://runs/{run_id}/openapi",
                "correlation": f"reconqa://runs/{run_id}/correlation",
                "knowledge": f"reconqa://projects/{resolved_project_id}/knowledge",
            },
        }

    @mcp.tool()
    async def reconqa_upload_har_files(
        har_paths: list[str],
        project_id: str | None = None,
        project_name: str | None = None,
        scenario: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Upload one or more local .har files (or directories of *.har) into a ReconQA run and run fast ['merge'] analysis."""
        resolved_hars = _resolve_har_paths(har_paths)
        detected_origin, detected_host = _detect_origin_from_har_files(resolved_hars)
        resolved_project_id, resolved_project_name = await _resolve_or_create_project(
            project_id,
            project_name,
            detected_origin,
            detected_host,
            source_label="HAR upload",
        )

        run_created = await client.request_json(
            "POST",
            f"/projects/{resolved_project_id}/runs",
            params={"auto_start": "false"},
            json_body={
                "target_url": detected_origin,
                "modules": ["merge"],
                "trigger_source": "cli",
            },
        )
        run_id = str(run_created["id"])

        multipart_files = [
            ("files", (har_file.name, har_file.read_bytes(), "application/json"))
            for har_file in resolved_hars
        ]
        form_data = {"scenario": scenario} if (scenario and scenario.strip()) else None
        upload_res = await client.request_multipart(
            f"/runs/{run_id}/hars",
            files=multipart_files,
            data=form_data,
        )

        await client.request_json("POST", f"/runs/{run_id}/start")
        if wait:
            run_data = await client.poll_run(run_id, max_wait_seconds=20.0)
        else:
            run_data = await client.request_json("GET", f"/runs/{run_id}")

        metrics = dict(run_data.get("summary_metrics") or {})
        coverage = await client.request_json("GET", f"/runs/{run_id}/coverage", allow_404=True)
        coverage_pct = (
            coverage.get("coverage_pct")
            if isinstance(coverage, dict) and coverage.get("coverage_pct") is not None
            else metrics.get("coverage_pct", 0.0)
        )

        return {
            "run_id": run_id,
            "project_id": resolved_project_id,
            "project_name": resolved_project_name,
            "status": run_data.get("status"),
            "waited": wait,
            "uploaded_files": [p.name for p in resolved_hars],
            "total_hars": upload_res.get("total_hars", len(resolved_hars)),
            "jobs": _compact_jobs(run_data.get("jobs") or []),
            "summary_metrics": metrics,
            "coverage_pct": coverage_pct,
            "resources": {
                "openapi": f"reconqa://runs/{run_id}/openapi",
                "correlation": f"reconqa://runs/{run_id}/correlation",
                "knowledge": f"reconqa://projects/{resolved_project_id}/knowledge",
            },
        }

    @mcp.tool()
    async def reconqa_execute_run_modules(
        run_id: str,
        modules: list[str],
        config: dict[str, Any] | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Trigger on-demand modules ('java_gen', 'perf', 'security', 'merge') on an existing run."""
        if not modules:
            raise ValueError("Provide at least one module name (e.g. ['java_gen'], ['perf'], ['security']).")
        run_data = await client.request_json(
            "POST",
            f"/runs/{run_id}/execute-modules",
            json_body={"modules": modules, "config": config or {}},
        )
        if wait and str(run_data.get("status", "")).lower() not in _TERMINAL_RUN_STATUSES:
            run_data = await client.poll_run(run_id, max_wait_seconds=30.0, poll_interval=0.5)
        return {
            "run_id": run_id,
            "requested_modules": modules,
            "waited": wait,
            "status": run_data.get("status"),
            "jobs": _compact_jobs(run_data.get("jobs") or []),
            "summary_metrics": run_data.get("summary_metrics") or {},
        }

    @mcp.tool()
    async def reconqa_get_run_summary(run_id: str) -> dict[str, Any]:
        """Fetch compact status, job progress, metrics, coverage %, correlation count, and available artifacts for a run."""
        run_data, artifacts, coverage, correlation = await asyncio.gather(
            client.request_json("GET", f"/runs/{run_id}"),
            client.request_json("GET", f"/runs/{run_id}/artifacts", allow_404=True),
            client.request_json("GET", f"/runs/{run_id}/coverage", allow_404=True),
            client.request_json("GET", f"/runs/{run_id}/correlation", allow_404=True),
        )
        metrics = dict(run_data.get("summary_metrics") or {})
        coverage_pct = (
            coverage.get("coverage_pct")
            if isinstance(coverage, dict) and coverage.get("coverage_pct") is not None
            else metrics.get("coverage_pct", 0.0)
        )
        chains_count = (
            len(correlation.get("chains") or [])
            if isinstance(correlation, dict)
            else int(metrics.get("correlation_chains", 0) or 0)
        )
        variables_count = (
            len(correlation.get("variables") or [])
            if isinstance(correlation, dict)
            else int(metrics.get("correlated_variables", 0) or 0)
        )
        artifact_list = [
            {
                "kind": a.get("kind"),
                "rel_path": a.get("rel_path"),
                "size_bytes": a.get("size_bytes", 0),
            }
            for a in (artifacts or [])[:40]
        ]
        project_id = run_data.get("project_id")
        return {
            "run_id": run_id,
            "project_id": project_id,
            "status": run_data.get("status"),
            "trigger_source": run_data.get("trigger_source"),
            "started_at": run_data.get("started_at"),
            "finished_at": run_data.get("finished_at"),
            "jobs": _compact_jobs(run_data.get("jobs") or []),
            "summary_metrics": metrics,
            "coverage_pct": coverage_pct,
            "uncovered_endpoints_count": (
                coverage.get("uncovered_count") if isinstance(coverage, dict) else None
            ),
            "correlation_chains_count": chains_count,
            "correlated_variables_count": variables_count,
            "available_artifacts": artifact_list,
            "resources": {
                "openapi": f"reconqa://runs/{run_id}/openapi",
                "correlation": f"reconqa://runs/{run_id}/correlation",
                "knowledge": f"reconqa://projects/{project_id}/knowledge" if project_id else None,
            },
        }

    @mcp.tool()
    async def reconqa_list_findings(
        run_id: str,
        category: str | None = None,
        severity: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """List prioritized security, performance, and API findings for a run (compact, bounded output)."""
        clamped_limit = max(1, min(int(limit or 30), 100))
        findings = await client.request_json(
            "GET",
            f"/runs/{run_id}/findings",
            params={"category": category, "severity": severity},
        )
        rows = list(findings or [])
        severity_counts = dict(
            Counter(str(f.get("severity") or "info").lower() for f in rows)
        )
        rows.sort(
            key=lambda f: (
                _SEVERITY_ORDER.get(str(f.get("severity") or "info").lower(), 99),
                str(f.get("category") or ""),
                str(f.get("endpoint") or ""),
            )
        )
        compact_rows = [
            {
                "id": f.get("id"),
                "category": f.get("category"),
                "severity": f.get("severity"),
                "endpoint": f.get("endpoint"),
                "title": f.get("title"),
                "evidence": _compact_evidence(f.get("evidence") or {}),
            }
            for f in rows[:clamped_limit]
        ]
        return {
            "run_id": run_id,
            "total_matching": len(rows),
            "returned": len(compact_rows),
            "by_severity": severity_counts,
            "findings": compact_rows,
        }

    @mcp.tool()
    async def reconqa_get_project_inventory(
        project_id: str,
        source: str | None = None,
        prefix: str | None = None,
        limit: int = 80,
    ) -> dict[str, Any]:
        """Fetch a project's cumulative Master API Inventory (merged Explore + Runs) with optional source/prefix filters."""
        clamped_limit = max(1, min(int(limit or 80), 200))
        data = await client.request_json("GET", f"/projects/{project_id}/inventory")
        raw_endpoints = list(data.get("endpoints") or [])

        norm_source = source.strip().lower() if source and source.strip() else None
        norm_prefix = prefix.strip() if prefix and prefix.strip() else None
        if norm_prefix and not norm_prefix.startswith("/"):
            norm_prefix = f"/{norm_prefix}"

        filtered: list[dict[str, Any]] = []
        for ep in raw_endpoints:
            ep_source = str(ep.get("discovery_source") or "").lower()
            ep_path = str(ep.get("path") or "")
            if norm_source and ep_source != norm_source:
                continue
            if norm_prefix and not ep_path.startswith(norm_prefix):
                continue
            filtered.append(ep)

        compact_endpoints = [
            {
                "method": ep.get("method", "GET"),
                "path": ep.get("path", "/"),
                "summary": _truncate_str(str(ep.get("summary") or ""), 160),
                "discovery_source": ep.get("discovery_source", "js-static"),
                "host": ep.get("host", ""),
                "scenarios": list(ep.get("scenarios") or []),
                "status_codes": list(ep.get("status_codes") or []),
                "parameters": [
                    {"name": p.get("name"), "in": p.get("in")}
                    for p in (ep.get("parameters") or [])[:12]
                    if isinstance(p, dict) and p.get("name")
                ],
                "seen_in_runs_count": len(ep.get("seen_in_runs") or []),
            }
            for ep in filtered[:clamped_limit]
        ]
        return {
            "project_id": data.get("project_id", project_id),
            "project_name": data.get("project_name", ""),
            "runs_merged_count": data.get("runs_merged_count", 0),
            "explore_sessions_count": data.get("explore_sessions_count", 0),
            "total_endpoints": data.get("total_endpoints", len(raw_endpoints)),
            "har_live_count": data.get("har_live_count", 0),
            "js_static_count": data.get("js_static_count", 0),
            "coverage_pct": data.get("coverage_pct", 0.0),
            "by_prefix": list(data.get("by_prefix") or []),
            "scenarios": list(data.get("scenarios") or []),
            "matched_endpoints_count": len(filtered),
            "endpoints_truncated": max(0, len(filtered) - len(compact_endpoints)),
            "endpoints": compact_endpoints,
        }

    @mcp.tool()
    async def reconqa_list_scenarios(
        project_id: str,
        priority: str | None = None,
        effectiveness_level: str | None = None,
    ) -> dict[str, Any]:
        """List first-class business scenarios with P0-P3 priority and L0-L4 testing effectiveness metrics."""
        data = await client.request_json(
            "GET",
            f"/projects/{project_id}/scenarios",
            params={
                "priority": priority.strip().upper() if priority and priority.strip() else None,
                "effectiveness_level": (
                    effectiveness_level.strip().upper()
                    if effectiveness_level and effectiveness_level.strip()
                    else None
                ),
            },
        )
        compact_scenarios = [
            {
                "id": sc.get("id"),
                "slug": sc.get("slug"),
                "name": sc.get("name"),
                "priority": sc.get("priority", "P2"),
                "status": sc.get("status", "active"),
                "business_domain": sc.get("business_domain", ""),
                "owner": sc.get("owner", ""),
                "source": sc.get("source", "manual"),
                "effectiveness_level": sc.get("effectiveness_level", "L0"),
                "description": _truncate_str(str(sc.get("description") or ""), 240),
                "tags": list(sc.get("tags") or []),
                "endpoint_count": sc.get("endpoint_count", 0),
                "run_count": sc.get("run_count", 0),
                "last_run_id": sc.get("last_run_id"),
                "assertion_count": sc.get("assertion_count", 0),
                "assertions_passed_count": sc.get("assertions_passed_count", 0),
                "negative_case_count": sc.get("negative_case_count", 0),
                "negative_cases_covered_count": sc.get("negative_cases_covered_count", 0),
            }
            for sc in (data.get("scenarios") or [])
        ]
        return {
            "project_id": data.get("project_id", project_id),
            "total": data.get("total", len(compact_scenarios)),
            "p0_p1_total": data.get("p0_p1_total", 0),
            "p0_p1_effective_count": data.get("p0_p1_effective_count", 0),
            "p0_p1_effective_pct": data.get("p0_p1_effective_pct", 0.0),
            "by_priority": data.get("by_priority") or {},
            "by_effectiveness": data.get("by_effectiveness") or {},
            "scenarios": compact_scenarios,
        }

    @mcp.tool()
    async def reconqa_upsert_scenario(
        project_id: str,
        slug: str,
        name: str | None = None,
        description: str | None = None,
        preconditions: str | None = None,
        business_domain: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        tags: list[str] | None = None,
        assertions: list[dict[str, Any]] | None = None,
        negative_cases: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create or update a first-class business scenario by slug (source='ai-agent') with assertions and negative cases."""
        clean_slug = slug.strip()
        if not clean_slug:
            raise ValueError("slug must not be empty.")

        payload: dict[str, Any] = {
            "slug": clean_slug,
            "source": "ai-agent",
        }
        if name is not None and name.strip():
            payload["name"] = name.strip()
        if description is not None:
            payload["description"] = description
        if preconditions is not None:
            payload["preconditions"] = preconditions
        if business_domain is not None:
            payload["business_domain"] = business_domain
        if priority is not None:
            payload["priority"] = priority.strip().upper()
        if status is not None:
            payload["status"] = status.strip().lower()
        if owner is not None:
            payload["owner"] = owner
        if tags is not None:
            payload["tags"] = tags
        if assertions is not None:
            payload["assertions"] = assertions
        if negative_cases is not None:
            payload["negative_cases"] = negative_cases

        data = await client.request_json(
            "POST",
            f"/projects/{project_id}/scenarios",
            params={"upsert": "true"},
            json_body=payload,
        )
        return {
            "id": data.get("id"),
            "project_id": data.get("project_id", project_id),
            "slug": data.get("slug", clean_slug),
            "name": data.get("name", (name or "").strip()),
            "priority": data.get("priority", "P2"),
            "status": data.get("status", "active"),
            "business_domain": data.get("business_domain", ""),
            "owner": data.get("owner", ""),
            "source": data.get("source", "ai-agent"),
            "tags": list(data.get("tags") or []),
            "endpoint_count": data.get("endpoint_count", 0),
            "assertion_count": data.get("assertion_count", len(data.get("assertions") or [])),
            "negative_case_count": data.get(
                "negative_case_count", len(data.get("negative_cases") or [])
            ),
            "effectiveness_level": data.get("effectiveness_level", "L0"),
        }

    @mcp.tool()
    async def reconqa_extract_credential(
        project_id: str,
        run_id: str | None = None,
        name: str | None = None,
        role_tag: str = "account_a",
    ) -> dict[str, Any]:
        """Auto-extract auth headers from a run's HAR traffic into a saved project AuthProfile (never echoes raw secrets)."""
        payload: dict[str, Any] = {"role_tag": (role_tag or "account_a").strip() or "account_a"}
        if run_id and run_id.strip():
            payload["run_id"] = run_id.strip()
        if name and name.strip():
            payload["name"] = name.strip()

        data = await client.request_json(
            "POST",
            f"/projects/{project_id}/auth-profiles/extract-from-har",
            json_body=payload,
        )
        profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        return {
            "profile_id": profile.get("id") or data.get("profile_id"),
            "name": profile.get("name") or data.get("name", ""),
            "role_tag": profile.get("role_tag") or data.get("role_tag", payload["role_tag"]),
            "extracted_header_names": list(data.get("extracted_header_names") or []),
            "source_run_id": data.get("source_run_id", ""),
            "source_host": data.get("source_host", ""),
        }

    @mcp.tool()
    async def reconqa_replay_endpoint(
        project_id: str,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        body: Any = None,
        credential_id: str | None = None,
        timeout_sec: float = 15.0,
    ) -> dict[str, Any]:
        """Safely replay an HTTP endpoint via ReconQA with optional server-side AuthProfile injection (4KB response cap)."""
        eff_timeout = max(1.0, min(float(timeout_sec or 15.0), 30.0))
        payload: dict[str, Any] = {
            "method": (method or "GET").strip().upper(),
            "url": url.strip(),
            "headers": dict(headers or {}),
            "query_params": dict(query_params or {}),
            "body": body,
            "credential_id": credential_id,
            "timeout_sec": eff_timeout,
        }
        data = await client.request_json(
            "POST",
            f"/projects/{project_id}/replay",
            json_body=payload,
            timeout=eff_timeout + 10.0,
        )

        raw_body_text = str(data.get("body_text") or "")
        raw_body_json = data.get("body_json")
        truncated = bool(data.get("truncated", False))

        safe_body_json: Any = raw_body_json
        if raw_body_json is not None:
            try:
                serialized = json.dumps(raw_body_json, ensure_ascii=False)
                if len(serialized) > _MAX_REPLAY_TEXT_CHARS:
                    safe_body_json = None
                    if not raw_body_text:
                        raw_body_text = serialized
                    truncated = True
            except Exception:
                safe_body_json = None

        if len(raw_body_text) > _MAX_REPLAY_TEXT_CHARS:
            safe_body_text = _truncate_str(raw_body_text, _MAX_REPLAY_TEXT_CHARS)
            truncated = True
        else:
            safe_body_text = raw_body_text

        return {
            "status_code": int(data.get("status_code", 0) or 0),
            "status_text": str(data.get("status_text") or ""),
            "duration_ms": float(data.get("duration_ms", 0.0) or 0.0),
            "content_type": str(data.get("content_type") or ""),
            "body_json": safe_body_json,
            "body_text": safe_body_text,
            "truncated": truncated,
            "error": data.get("error"),
        }

    @mcp.tool()
    async def reconqa_run_external_tool(
        project_id: str,
        run_id: str,
        tool: str,
        credential_id: str | None = None,
        base_url: str | None = None,
        sync_scenarios: bool = True,
        timeout_sec: int = 120,
    ) -> dict[str, Any]:
        """Run an external verification tool ('java_test', 'schemathesis', 'nuclei', 'k6') and auto-sync Scenario L0-L4 status."""
        norm_tool = (tool or "").strip().lower()
        if norm_tool not in _VALID_EXTERNAL_TOOLS:
            raise ValueError(
                f"Unsupported tool '{tool}'. Expected one of: {', '.join(sorted(_VALID_EXTERNAL_TOOLS))}."
            )
        eff_timeout = max(15, min(int(timeout_sec or 120), 300))
        payload: dict[str, Any] = {
            "tool": norm_tool,
            "credential_id": credential_id,
            "base_url": base_url,
            "sync_scenarios": sync_scenarios,
            "timeout_sec": eff_timeout,
        }
        data = await client.request_json(
            "POST",
            f"/projects/{project_id}/runs/{run_id}/execute-tool",
            json_body=payload,
            timeout=float(eff_timeout) + 30.0,
        )
        return {
            "executed": bool(data.get("executed", False)),
            "tool": str(data.get("tool") or norm_tool),
            "status": str(data.get("status") or "unknown"),
            "duration_ms": float(data.get("duration_ms", 0.0) or 0.0),
            "summary": data.get("summary") or {},
            "synced_scenarios": int(data.get("synced_scenarios", 0) or 0),
            "synced_assertions": int(data.get("synced_assertions", 0) or 0),
            "synced_negative_cases": int(data.get("synced_negative_cases", 0) or 0),
            "stdout_tail": _truncate_str(str(data.get("stdout_tail") or ""), _MAX_REPLAY_TEXT_CHARS),
            "error": data.get("error"),
        }

    @mcp.tool()
    async def reconqa_get_capability_map(
        project_id: str,
        live_only: bool = True,
        module: str | None = None,
        role: str | None = None,
        has_writes: bool = False,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Fetch a project's Business Capability Map (Resource x Action x Role x State) with 5-dim QA metrics."""
        clamped_limit = max(1, min(int(limit or 30), 100))
        params: dict[str, Any] = {
            "live_only": "true" if live_only else "false",
            "has_writes": "true" if has_writes else "false",
        }
        if module and module.strip():
            params["module"] = module.strip()
        if role and role.strip():
            params["role"] = role.strip().lower()

        data = await client.request_json(
            "GET",
            f"/projects/{project_id}/capabilities",
            params=params,
        )
        raw_caps = list(data.get("capabilities") or [])
        compact_caps: list[dict[str, Any]] = []
        for cap in raw_caps[:clamped_limit]:
            raw_eps = list(cap.get("endpoints") or [])
            sample_endpoints: list[str] = []
            for ep in raw_eps[:8]:
                if isinstance(ep, dict):
                    method = str(ep.get("method") or "GET").upper()
                    path = str(ep.get("path") or "/")
                    sample_endpoints.append(f"{method} {path}")
                elif isinstance(ep, str):
                    sample_endpoints.append(ep)
            compact_caps.append(
                {
                    "key": cap.get("key", ""),
                    "module": cap.get("module", ""),
                    "resource": cap.get("resource", ""),
                    "display_name": cap.get("display_name", cap.get("key", "")),
                    "recommended_priority": cap.get("recommended_priority", "P2"),
                    "priority_score": cap.get("priority_score", 0),
                    "live_endpoints": cap.get("live_endpoints", 0),
                    "total_endpoints": cap.get("total_endpoints", 0),
                    "write_endpoints": cap.get("write_endpoints", 0),
                    "roles": list(cap.get("roles") or []),
                    "actions": list(cap.get("actions") or []),
                    "observed_statuses": cap.get("observed_statuses") or {},
                    "case_matrix": cap.get("case_matrix") or {},
                    "case_coverage_pct": cap.get("case_coverage_pct", 0.0),
                    "linked_scenarios": list(cap.get("linked_scenarios") or []),
                    "sample_endpoints": sample_endpoints,
                    "endpoints_truncated": max(0, len(raw_eps) - len(sample_endpoints)),
                }
            )
        return {
            "project_id": data.get("project_id", project_id),
            "project_name": data.get("project_name", ""),
            "metrics": data.get("metrics") or {},
            "modules": list(data.get("modules") or []),
            "total_matching_capabilities": len(raw_caps),
            "capabilities_truncated": max(0, len(raw_caps) - len(compact_caps)),
            "capabilities": compact_caps,
        }

    @mcp.tool()
    async def reconqa_promote_capability_scenario(
        project_id: str,
        capability_key: str,
        priority: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Promote a Business Capability into a 5-case effectiveness Scenario (Happy Path + Invalid Params + Illegal State + AuthZ + Idempotency)."""
        clean_key = (capability_key or "").strip()
        if not clean_key:
            raise ValueError("capability_key must not be empty.")

        payload: dict[str, Any] = {"capability_key": clean_key}
        if priority is not None and priority.strip():
            payload["priority"] = priority.strip().upper()
        if name is not None and name.strip():
            payload["name"] = name.strip()

        data = await client.request_json(
            "POST",
            f"/projects/{project_id}/capabilities/promote-scenario",
            json_body=payload,
        )
        return {
            "id": data.get("id"),
            "project_id": data.get("project_id", project_id),
            "slug": data.get("slug", ""),
            "name": data.get("name", ""),
            "priority": data.get("priority", "P2"),
            "status": data.get("status", "active"),
            "business_domain": data.get("business_domain", ""),
            "source": data.get("source", "ai-agent"),
            "endpoint_count": data.get("endpoint_count", 0),
            "assertion_count": data.get("assertion_count", len(data.get("assertions") or [])),
            "negative_case_count": data.get(
                "negative_case_count", len(data.get("negative_cases") or [])
            ),
            "effectiveness_level": data.get("effectiveness_level", "L0"),
        }

    # --- 3 MCP Resources ---

    @mcp.resource(
        "reconqa://runs/{run_id}/openapi",
        name="run_openapi_yaml",
        description="Merged OpenAPI 3.0.3 YAML specification for a ReconQA run.",
        mime_type="application/yaml",
    )
    async def resource_run_openapi(run_id: str) -> str:
        yaml_text = await client.request_text(
            "GET",
            f"/runs/{run_id}/openapi",
            params={"format": "yaml"},
        )
        max_chars = 200_000
        if len(yaml_text) > max_chars:
            return (
                f"# Truncated to first {max_chars} characters for agent context safety.\n"
                f"{yaml_text[:max_chars]}\n"
            )
        return yaml_text

    @mcp.resource(
        "reconqa://runs/{run_id}/correlation",
        name="run_correlation_graph",
        description="Data-flow Mermaid graph and correlated producer->consumer variables for a ReconQA run.",
        mime_type="application/json",
    )
    async def resource_run_correlation(run_id: str) -> str:
        data = await client.request_json("GET", f"/runs/{run_id}/correlation")
        variables = list(data.get("variables") or [])[:60]
        chains = [
            {
                "name": ch.get("name"),
                "scenario": ch.get("scenario"),
                "steps": [
                    {
                        "step": st.get("step"),
                        "method": st.get("method"),
                        "path": st.get("path"),
                        "expected_status": st.get("expected_status"),
                        "extracts": st.get("extracts") or {},
                        "uses": st.get("uses") or {},
                    }
                    for st in (ch.get("steps") or [])
                ],
            }
            for ch in (data.get("chains") or [])[:30]
        ]
        compact = {
            "run_id": run_id,
            "mermaid": str(data.get("mermaid") or ""),
            "variables_count": len(data.get("variables") or []),
            "chains_count": len(data.get("chains") or []),
            "variables": variables,
            "chains": chains,
            "filtered_constants": list(data.get("filtered_constants") or [])[:40],
        }
        return json.dumps(compact, ensure_ascii=False, indent=2)

    @mcp.resource(
        "reconqa://projects/{project_id}/knowledge",
        name="project_knowledge_digest",
        description="Markdown digest of PRDs, API specs, business rules, and notes stored in a project's Knowledge Base.",
        mime_type="text/markdown",
    )
    async def resource_project_knowledge(project_id: str) -> str:
        project, docs = await asyncio.gather(
            client.request_json("GET", f"/projects/{project_id}"),
            client.request_json("GET", f"/projects/{project_id}/knowledge"),
        )
        proj_name = project.get("name") or project_id
        lines = [f"# Project Knowledge Base: {proj_name}", ""]
        if project.get("description"):
            lines.extend([str(project["description"]), ""])
        if not docs:
            lines.append("_No knowledge documents recorded yet._")
            return "\n".join(lines)

        for idx, doc in enumerate(docs, start=1):
            title = doc.get("title") or f"Document {idx}"
            dtype = doc.get("doc_type") or "other"
            lines.append(f"## {idx}. [{dtype}] {title}")
            meta: list[str] = []
            if doc.get("tags"):
                meta.append(f"Tags: {', '.join(doc['tags'])}")
            if doc.get("related_endpoints"):
                meta.append(f"Endpoints: {', '.join(doc['related_endpoints'])}")
            if doc.get("external_url"):
                meta.append(f"URL: {doc['external_url']}")
            if meta:
                lines.append(" | ".join(meta))
            lines.append("")
            body = str(doc.get("content") or "").strip()
            if body:
                lines.append(_truncate_str(body, 8000))
            else:
                lines.append("_No inline text content (see external URL or uploaded file)._")
            lines.append("")
        return "\n".join(lines)

    return mcp


def run_mcp_server(
    api_url: str | None = None,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8001,
) -> None:
    """Start the ReconQA MCP server using stdio, streamable-http, or sse transport."""
    norm_transport = transport.strip().lower()
    if norm_transport not in {"stdio", "streamable-http", "sse"}:
        raise ValueError(
            f"Unsupported MCP transport '{transport}'. Expected one of: stdio, streamable-http, sse."
        )

    server = create_mcp_server(api_url=api_url)
    if _MCP_V2:
        if norm_transport == "stdio":
            server.run(transport="stdio")
        else:
            server.run(transport=norm_transport, host=host, port=port)  # type: ignore[arg-type]
    else:  # pragma: no cover - mcp 1.x fallback
        if hasattr(server, "settings"):
            server.settings.host = host
            server.settings.port = port
        server.run(transport=norm_transport)  # type: ignore[arg-type]


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the ReconQA MCP Server (Human-in-the-Loop AI Agent bridge)."
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="ReconQA FastAPI base URL (defaults to RECONQA_API_URL or http://127.0.0.1:8000).",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
        help="MCP transport protocol.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for HTTP/SSE transports.")
    parser.add_argument("--port", type=int, default=8001, help="Bind port for HTTP/SSE transports.")
    args = parser.parse_args()
    run_mcp_server(
        api_url=args.api_url,
        transport=args.transport,
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    _main()
