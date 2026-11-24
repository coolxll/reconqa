"""Autonomous External-System Acceptance Glue APIs:
1. Auto-extract AuthProfile credentials from HAR traffic
2. Execute real external tools (java_test, schemathesis, nuclei, k6) with Scenario sync-back
3. Generate live AI Acceptance Prompt recipes (zero raw secret exposure)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.config import settings
from reconqa.models.db import get_db
from reconqa.models.schemas import (
    AcceptancePromptContextSummary,
    AcceptancePromptResponse,
    AuthProfileOut,
    ExecuteExternalToolRequest,
    ExecuteExternalToolResponse,
    ExtractCredentialRequest,
    ExtractCredentialResponse,
)
from reconqa.models.tables import AuthProfile, KnowledgeDoc, Project, Run, ScenarioModel, Target
from reconqa.modules.codegen_java.generator import generate_java_project
from reconqa.modules.codegen_java.schemathesis_run import (
    _parse_junit_report,
    _parse_stdout_fallback,
)
from reconqa.modules.hub.har_parser import normalize_path
from reconqa.modules.performance.k6_generator import generate_k6_script
from reconqa.modules.security.nuclei_runner import run_nuclei_scan
from reconqa.server.routers.projects import _load_credential_headers
from reconqa.server.routers.scenarios import (
    _build_scenario_list_response,
    _enrich_scenario,
    _load_project_inventory_context,
    _parse_assertions,
    _parse_negative_cases,
    collect_project_scenario_assertions,
    sync_project_scenarios,
)
from reconqa.utils.io import ensure_dir, read_json, write_json

router = APIRouter(tags=["acceptance"])

_AUTH_HEADER_KEYS = {
    "authorization",
    "accesstoken",
    "token",
    "x-access-token",
    "x-auth-token",
    "cookie",
}

_STATIC_EXTS = (
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
    ".map",
)

_API_PATH_HINTS = ("/api/", "/v1/", "/v2/", "/v3/", "/graphql", "/rest/", "/admin/", "/rpc/")

_AGENT_LABELS: dict[str, tuple[str, str]] = {
    "generic": ("通用 MCP AI Agent", "Generic MCP AI Agent"),
    "antigravity": ("Google Antigravity (agy)", "Google Antigravity (agy)"),
    "claude": ("Claude Code", "Claude Code"),
    "codex": ("OpenAI Codex CLI", "OpenAI Codex CLI"),
    "openclaw": ("OpenClaw Agent", "OpenClaw Agent"),
}


def _resolve_tool_bin(tool_name: str) -> str | None:
    """Resolve external binary across settings.resolve_bin, ~/.local/bin, and PATH."""
    found = settings.resolve_bin(tool_name)
    if found:
        return found
    local_bin = Path.home() / ".local" / "bin" / tool_name
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin.resolve())
    return shutil.which(tool_name)


async def _run_exec(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> tuple[int, str, str, bool]:
    """Run an external command safely via create_subprocess_exec (no shell interpolation)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode if proc.returncode is not None else 0,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
            False,
        )
    except TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        return 124, "", f"Process timed out after {timeout}s", True


def _redact_secrets_from_text(text: str, secret_values: list[str]) -> str:
    cleaned = text or ""
    for val in secret_values:
        s = (val or "").strip()
        if len(s) >= 6:
            cleaned = cleaned.replace(s, "[REDACTED]")
    return cleaned


def _find_run_har_files(workspace_dir: Path) -> list[Path]:
    files: list[Path] = []
    for subdir_name in ("har", "hars"):
        d = workspace_dir / subdir_name
        if d.is_dir():
            files.extend(sorted(p for p in d.glob("*.har") if p.is_file()))
    return files


def _is_usable_auth_value(value: str) -> bool:
    s = (value or "").strip()
    if not s:
        return False
    upper = s.upper()
    if "REDACTED" in upper or "***" in s or s.lower() in {"null", "undefined", "bearer", "bearer "}:
        return False
    return True


def _extract_auth_headers_from_hars(
    har_files: list[Path],
    *,
    target_host: str | None = None,
    preferred_hosts: set[str] | None = None,
) -> tuple[dict[str, str], str]:
    """Scan HAR files prioritizing 200 API XHR/JSON requests and extract auth headers."""
    want_host = (target_host or "").strip().lower()
    if want_host.startswith(("http://", "https://")):
        want_host = (urlparse(want_host).hostname or "").lower()

    pref_hosts = {h.lower().lstrip(".") for h in (preferred_hosts or set()) if h}

    # Track per-host score and best headers per header lowercase key:
    # host -> { "best_score": int, "headers": { lower_name: (score, orig_name, value) } }
    by_host: dict[str, dict[str, Any]] = {}

    for har_path in har_files:
        try:
            doc = json.loads(har_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        entries = (doc.get("log") or {}).get("entries") if isinstance(doc, dict) else None
        if not isinstance(entries, list):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            req = entry.get("request") or {}
            resp = entry.get("response") or {}
            url = str(req.get("url") or "")
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if not host:
                continue
            if want_host and host != want_host and not host.endswith("." + want_host):
                continue

            path_low = (parsed.path or "").lower()
            if any(path_low.endswith(ext) for ext in _STATIC_EXTS):
                continue

            raw_headers = req.get("headers") or []
            found_auth: list[tuple[str, str]] = []
            for h in raw_headers:
                if not isinstance(h, dict):
                    continue
                h_name = str(h.get("name") or "").strip()
                h_val = str(h.get("value") or "").strip()
                if h_name.lower() in _AUTH_HEADER_KEYS and _is_usable_auth_value(h_val):
                    found_auth.append((h_name, h_val))

            if not found_auth:
                continue

            status_code = int(resp.get("status") or 0)
            mime = str((resp.get("content") or {}).get("mimeType") or "").lower()
            score = 0
            if status_code == 200:
                score += 60
            elif 200 <= status_code < 300:
                score += 45
            elif 300 <= status_code < 400:
                score += 10
            if "json" in mime:
                score += 25
            if any(hint in path_low for hint in _API_PATH_HINTS):
                score += 15
            if any(host == ph or host.endswith("." + ph) for ph in pref_hosts):
                score += 20
            # Prioritize Bearer/token headers slightly over cookie-only requests
            if any(n.lower() != "cookie" for n, _ in found_auth):
                score += 10

            host_entry = by_host.setdefault(host, {"best_score": -1, "headers": {}})
            if score > host_entry["best_score"]:
                host_entry["best_score"] = score
            hdr_map: dict[str, tuple[int, str, str]] = host_entry["headers"]
            for h_name, h_val in found_auth:
                k_low = h_name.lower()
                prev = hdr_map.get(k_low)
                if prev is None or score >= prev[0]:
                    hdr_map[k_low] = (score, h_name, h_val)

    if not by_host:
        return {}, ""

    best_host, best_info = max(
        by_host.items(),
        key=lambda kv: (kv[1]["best_score"], len(kv[1]["headers"])),
    )
    extracted: dict[str, str] = {}
    for _, orig_name, val in best_info["headers"].values():
        extracted[orig_name] = val
    return extracted, best_host


async def _get_project_or_404(db: AsyncSession, project_id: str) -> Project:
    proj = await db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return proj


@router.post(
    "/projects/{project_id}/auth-profiles/extract-from-har",
    response_model=ExtractCredentialResponse,
)
async def extract_credential_from_har(
    project_id: str,
    body: ExtractCredentialRequest,
    db: AsyncSession = Depends(get_db),
) -> ExtractCredentialResponse:
    """Scan a project Run's HAR traffic and auto-extract/upsert a reusable AuthProfile."""
    await _get_project_or_404(db, project_id)

    source_run: Run | None = None
    har_files: list[Path] = []

    if body.run_id:
        run_obj = await db.get(Run, body.run_id)
        if run_obj is None or run_obj.project_id != project_id:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{body.run_id}' not found in project '{project_id}'",
            )
        source_run = run_obj
        har_files = _find_run_har_files(Path(run_obj.workspace_dir))
    else:
        runs_res = await db.execute(
            select(Run)
            .where(Run.project_id == project_id)
            .order_by(Run.finished_at.desc(), Run.started_at.desc())
        )
        all_runs = list(runs_res.scalars().all())
        ordered = [r for r in all_runs if r.status == "completed"] + [
            r for r in all_runs if r.status != "completed"
        ]
        for candidate in ordered:
            c_hars = _find_run_har_files(Path(candidate.workspace_dir))
            if c_hars:
                source_run = candidate
                har_files = c_hars
                break

    if source_run is None or not har_files:
        raise HTTPException(
            status_code=400,
            detail="No authentication headers found in HAR traffic",
        )

    targets_res = await db.execute(select(Target).where(Target.project_id == project_id))
    preferred_hosts: set[str] = set()
    for t in targets_res.scalars().all():
        if t.root_domain:
            preferred_hosts.add(t.root_domain.lower())
        if t.base_url:
            h = urlparse(t.base_url).hostname
            if h:
                preferred_hosts.add(h.lower())

    extracted_headers, source_host = _extract_auth_headers_from_hars(
        har_files,
        target_host=body.target_host,
        preferred_hosts=preferred_hosts,
    )
    if not extracted_headers:
        raise HTTPException(
            status_code=400,
            detail="No authentication headers found in HAR traffic",
        )

    profile_name = (body.name or "").strip() or f"Auto-Extracted Session ({source_host})"

    existing_res = await db.execute(
        select(AuthProfile).where(
            AuthProfile.project_id == project_id,
            AuthProfile.name == profile_name,
        )
    )
    profile = existing_res.scalar_one_or_none()
    if profile is not None:
        profile.role_tag = body.role_tag
        profile.extra_headers_ref = extracted_headers
    else:
        profile = AuthProfile(
            project_id=project_id,
            name=profile_name,
            role_tag=body.role_tag,
            extra_headers_ref=extracted_headers,
        )
        db.add(profile)

    await db.commit()
    await db.refresh(profile)

    return ExtractCredentialResponse(
        profile=AuthProfileOut.model_validate(profile),
        extracted_header_names=sorted(extracted_headers.keys()),
        source_run_id=source_run.id,
        source_host=source_host,
    )


def _match_test_record_to_assertion(
    record: dict[str, Any],
    *,
    target_endpoint: str,
    title: str,
    scenario_slug: str,
) -> bool:
    haystack = " ".join(
        [
            str(record.get("displayName") or ""),
            str(record.get("testMethod") or ""),
            str(record.get("testClass") or ""),
            str(record.get("story") or ""),
            str(record.get("failureMessage") or ""),
        ]
    ).lower()

    if title and title.lower() in haystack:
        return True

    target = (target_endpoint or "").strip()
    if target:
        parts = target.split(None, 1)
        t_path = parts[1] if len(parts) == 2 else target
        t_clean = t_path.split("?", 1)[0].strip().rstrip("/").lower()
        t_norm, _ = normalize_path(t_clean or "/")
        if t_clean and t_clean in haystack:
            return True
        if t_norm and t_norm.lower() in haystack:
            return True
        # Also match concrete segments like /api/v1/orders when method slug is step01_post_api_v1_orders
        slug_hint = re.sub(r"[^a-z0-9]+", "", t_clean)
        haystack_alnum = re.sub(r"[^a-z0-9]+", "", haystack)
        if len(slug_hint) >= 6 and slug_hint in haystack_alnum:
            return True

    if scenario_slug and scenario_slug.lower() in haystack:
        return True
    return False


async def _sync_scenarios_after_tool(
    db: AsyncSession,
    project_id: str,
    *,
    tool: str,
    exec_summary: dict[str, Any],
    test_records: list[dict[str, Any]],
) -> tuple[int, int, int]:
    """Automatically sync external tool results back to persisted Scenario assertions/negative cases."""
    res = await db.execute(
        select(ScenarioModel).where(
            ScenarioModel.project_id == project_id,
            ScenarioModel.status != "archived",
        )
    )
    rows = list(res.scalars().all())
    if not rows:
        return 0, 0, 0

    now = datetime.now(UTC)
    synced_scenarios = 0
    synced_assertions = 0
    synced_negative_cases = 0

    if tool == "java_test":
        suite_total = int(exec_summary.get("total") or 0)
        suite_failed = int(exec_summary.get("failed") or 0)

        for row in rows:
            assertions = _parse_assertions(row)
            if not assertions:
                continue
            row_changed = False
            for a in assertions:
                matched_recs = [
                    rec
                    for rec in test_records
                    if _match_test_record_to_assertion(
                        rec,
                        target_endpoint=a.target_endpoint,
                        title=a.title,
                        scenario_slug=row.slug,
                    )
                ]
                new_status: Literal["pending", "passed", "failed"] | None = None
                if matched_recs:
                    statuses = {str(r.get("status") or "").upper() for r in matched_recs}
                    if "FAILED" in statuses:
                        new_status = "failed"
                    elif "PASSED" in statuses:
                        new_status = "passed"
                elif suite_total > 0 and suite_failed == 0 and a.target_endpoint:
                    new_status = "passed"

                if new_status is not None and a.status != new_status:
                    a.status = new_status
                    row_changed = True
                    synced_assertions += 1

            if row_changed:
                row.assertions_json = json.dumps(
                    [a.model_dump() for a in assertions], ensure_ascii=False
                )
                row.updated_at = now
                synced_scenarios += 1

    elif tool == "schemathesis":
        server_errors = int(exec_summary.get("server_errors") or 0)
        target_nc_status: Literal["covered", "failed"] = "failed" if server_errors > 0 else "covered"
        for row in rows:
            neg_cases = _parse_negative_cases(row)
            if not neg_cases:
                continue
            row_changed = False
            for nc in neg_cases:
                if nc.category in {"validation", "boundary"} and nc.status != target_nc_status:
                    nc.status = target_nc_status
                    row_changed = True
                    synced_negative_cases += 1
            if row_changed:
                row.negative_cases_json = json.dumps(
                    [nc.model_dump() for nc in neg_cases], ensure_ascii=False
                )
                row.updated_at = now
                synced_scenarios += 1

    elif tool == "nuclei":
        high_crit = int(exec_summary.get("high_or_critical") or 0)
        target_sec_status: Literal["covered", "failed"] = "failed" if high_crit > 0 else "covered"
        for row in rows:
            neg_cases = _parse_negative_cases(row)
            if not neg_cases:
                continue
            row_changed = False
            for nc in neg_cases:
                if nc.category in {"auth", "security"} and nc.status != target_sec_status:
                    nc.status = target_sec_status
                    row_changed = True
                    synced_negative_cases += 1
            if row_changed:
                row.negative_cases_json = json.dumps(
                    [nc.model_dump() for nc in neg_cases], ensure_ascii=False
                )
                row.updated_at = now
                synced_scenarios += 1

    if synced_scenarios > 0:
        await db.commit()
    return synced_scenarios, synced_assertions, synced_negative_cases


def _parse_k6_summary(summary_path: Path) -> dict[str, Any]:
    if not summary_path.is_file():
        return {"p95_ms": 0.0, "http_req_failed_rate": 0.0, "checks_passed": 0}
    try:
        raw = read_json(summary_path)
    except Exception:
        return {"p95_ms": 0.0, "http_req_failed_rate": 0.0, "checks_passed": 0}
    metrics = raw.get("metrics") if isinstance(raw, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}
    dur = metrics.get("http_req_duration") or {}
    dur_vals = dur.get("values") if isinstance(dur.get("values"), dict) else dur
    p95 = float(dur_vals.get("p(95)") or dur_vals.get("avg") or 0.0) if isinstance(dur_vals, dict) else 0.0

    failed_m = metrics.get("http_req_failed") or {}
    failed_vals = failed_m.get("values") if isinstance(failed_m.get("values"), dict) else failed_m
    fail_rate = float(
        failed_vals.get("rate") or failed_vals.get("value") or 0.0
    ) if isinstance(failed_vals, dict) else 0.0

    checks_m = metrics.get("checks") or {}
    checks_vals = checks_m.get("values") if isinstance(checks_m.get("values"), dict) else checks_m
    checks_passed = int(checks_vals.get("passes") or 0) if isinstance(checks_vals, dict) else 0
    return {
        "p95_ms": round(p95, 2),
        "http_req_failed_rate": round(fail_rate, 4),
        "checks_passed": checks_passed,
    }


@router.post(
    "/projects/{project_id}/runs/{run_id}/execute-tool",
    response_model=ExecuteExternalToolResponse,
)
async def execute_run_external_tool(
    project_id: str,
    run_id: str,
    body: ExecuteExternalToolRequest,
    db: AsyncSession = Depends(get_db),
) -> ExecuteExternalToolResponse:
    """Execute a real external tool (java_test/schemathesis/nuclei/k6) and sync Scenario status."""
    await _get_project_or_404(db, project_id)
    run = await db.get(Run, run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(
            status_code=404,
            detail=f"Run '{run_id}' not found in project '{project_id}'",
        )

    tool = body.tool
    clamped_timeout = max(15, min(300, int(body.timeout_sec or 120)))
    run_dir = Path(run.workspace_dir)

    # Resolve target base_url
    effective_base_url = (body.base_url or "").strip()
    if not effective_base_url:
        cfg = run.config_snapshot or {}
        if isinstance(cfg, dict) and cfg.get("target_url"):
            effective_base_url = str(cfg["target_url"]).strip()
    if not effective_base_url:
        targets_res = await db.execute(select(Target).where(Target.project_id == project_id))
        first_target = targets_res.scalars().first()
        if first_target and first_target.base_url:
            effective_base_url = first_target.base_url.strip()

    # Load optional server-side credential headers
    cred_headers: dict[str, str] = {}
    if body.credential_id:
        profile = await db.get(AuthProfile, body.credential_id)
        if profile is None or profile.project_id != project_id:
            raise HTTPException(
                status_code=404,
                detail=f"Credential '{body.credential_id}' not found in project '{project_id}'",
            )
        target_host = (urlparse(effective_base_url).hostname or "").lower()
        cred_headers = _load_credential_headers(profile, target_host)

    secret_values = list(cred_headers.values())
    t0 = time.perf_counter()

    # Map tool name -> required external CLI binary
    required_bin_map = {
        "java_test": "mvn",
        "schemathesis": "schemathesis",
        "nuclei": "nuclei",
        "k6": "k6",
    }
    bin_path = _resolve_tool_bin(required_bin_map[tool])
    if not bin_path:
        return ExecuteExternalToolResponse(
            executed=False,
            tool=tool,
            status="tool_missing",
            duration_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            summary={},
            stdout_tail="",
            synced_scenarios=0,
            synced_assertions=0,
            synced_negative_cases=0,
            error=f"Tool '{tool}' is not installed. Visit /system/doctor to install it.",
        )

    exec_summary: dict[str, Any] = {}
    test_records: list[dict[str, Any]] = []
    stdout_tail = ""
    status_val = "passed"
    error_msg: str | None = None

    if tool == "java_test":
        java_dir = run_dir / "java-tests"
        if not (java_dir / "pom.xml").is_file() and (run_dir / "java_tests" / "pom.xml").is_file():
            java_dir = run_dir / "java_tests"

        if not (java_dir / "pom.xml").is_file():
            openapi_src = run_dir / "hub" / "openapi.yaml"
            if not openapi_src.is_file():
                openapi_src = run_dir / "hub" / "openapi.json"
            if not openapi_src.is_file():
                return ExecuteExternalToolResponse(
                    executed=False,
                    tool=tool,
                    status="error",
                    duration_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                    error="No generated Java project or OpenAPI spec found; run merge first.",
                )
            corr_path = run_dir / "hub" / "correlation.json"
            scenario_assertions = await collect_project_scenario_assertions(db, project_id)
            await asyncio.to_thread(
                generate_java_project,
                openapi_src,
                java_dir,
                correlation_path=corr_path if corr_path.is_file() else None,
                base_uri=effective_base_url or None,
                scenario_assertions=scenario_assertions or None,
            )

        env = os.environ.copy()
        if effective_base_url:
            env["RECONQA_BASE_URI"] = effective_base_url
        for hk, hv in cred_headers.items():
            low = hk.lower()
            if low == "cookie":
                env["RECONQA_AUTH_COOKIE"] = hv
            elif low in {"authorization", "token", "accesstoken", "x-access-token", "x-auth-token"}:
                env["RECONQA_AUTH_TOKEN"] = hv

        cmd = [bin_path, "-B", "test"]
        if effective_base_url:
            cmd.append(f"-DbaseUrl={effective_base_url}")
            cmd.append(f"-Dbase.uri={effective_base_url}")

        rc, out, err, timed_out = await _run_exec(
            cmd, cwd=java_dir, env=env, timeout=clamped_timeout
        )
        combined = "\n".join(p for p in (out.strip(), err.strip()) if p)
        stdout_tail = _redact_secrets_from_text(combined[-4000:], secret_values)

        summary_path = java_dir / "target" / "reconqa-execution-summary.json"
        total = passed = failed = skipped = 0
        summary_dur = 0.0
        if summary_path.is_file():
            try:
                parsed = read_json(summary_path)
                total = int(parsed.get("total") or 0)
                passed = int(parsed.get("passed") or 0)
                failed = int(parsed.get("failed") or 0)
                skipped = int(parsed.get("skipped") or 0)
                summary_dur = float(parsed.get("durationMs") or 0.0)
                if isinstance(parsed.get("tests"), list):
                    test_records = [t for t in parsed["tests"] if isinstance(t, dict)]
            except Exception:
                pass

        allure_ready = False
        allure_results = java_dir / "target" / "allure-results"
        allure_report = java_dir / "target" / "allure-report"
        allure_bin = _resolve_tool_bin("allure")
        if allure_bin and allure_results.is_dir() and any(allure_results.iterdir()):
            await _run_exec(
                [allure_bin, "generate", str(allure_results), "-o", str(allure_report), "--clean"],
                cwd=java_dir,
                timeout=60,
            )
            allure_ready = (allure_report / "index.html").is_file()

        exec_summary = {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "duration_ms": summary_dur,
            "allure_report_ready": allure_ready,
        }
        if timed_out:
            status_val = "timeout"
            error_msg = f"Maven test timed out after {clamped_timeout}s"
        elif rc == 0 and failed == 0:
            status_val = "passed"
        else:
            status_val = "failed"
            error_msg = f"Maven exited with code {rc} ({failed} failed)" if failed or rc != 0 else None

    elif tool == "schemathesis":
        openapi_path = run_dir / "hub" / "openapi.json"
        if not openapi_path.is_file():
            openapi_path = run_dir / "hub" / "openapi.yaml"
        if not openapi_path.is_file():
            return ExecuteExternalToolResponse(
                executed=False,
                tool=tool,
                status="error",
                duration_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                error="OpenAPI specification not found for this run; run merge first.",
            )
        if not effective_base_url:
            return ExecuteExternalToolResponse(
                executed=False,
                tool=tool,
                status="error",
                duration_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                error="Target base_url is required to run schemathesis.",
            )

        out_dir = ensure_dir(run_dir / "java-tests")
        junit_path = out_dir / "schemathesis_junit.xml"
        report_path = out_dir / "schemathesis_report.json"

        cmd = [
            bin_path,
            "run",
            str(openapi_path),
            "--url",
            effective_base_url,
            "--max-examples=5",
            "--checks=not_a_server_error,status_code_conformance",
            "--report",
            "junit",
            "--report-junit-path",
            str(junit_path),
        ]
        for hk, hv in cred_headers.items():
            cmd.extend(["--header", f"{hk}: {hv}"])

        rc, out, err, timed_out = await _run_exec(cmd, cwd=out_dir, timeout=clamped_timeout)
        combined = "\n".join(p for p in (out.strip(), err.strip()) if p)
        stdout_tail = _redact_secrets_from_text(combined[-4000:], secret_values)

        if junit_path.is_file():
            st_summary, st_findings = _parse_junit_report(junit_path)
        else:
            st_summary, st_findings = _parse_stdout_fallback(combined)

        server_errors = sum(
            1
            for f in st_findings
            if "500" in str(f.get("message") or "")
            or "server error" in str(f.get("message") or "").lower()
            or "not_a_server_error" in str(f.get("message") or "").lower()
            or f.get("kind") == "error"
        )
        if server_errors == 0 and re.search(r"5\d\d\s+internal\s+server\s+error", combined, re.I):
            server_errors = 1

        checks_passed = int(st_summary.get("passed") or 0)
        checks_failed = int(st_summary.get("failed") or 0) + int(st_summary.get("errors") or 0)
        exec_summary = {
            "checks_passed": checks_passed,
            "checks_failed": checks_failed,
            "server_errors": server_errors,
            "total": int(st_summary.get("total") or (checks_passed + checks_failed)),
        }
        if timed_out:
            status_val = "timeout"
            error_msg = f"Schemathesis timed out after {clamped_timeout}s"
        elif rc == 0 and checks_failed == 0 and server_errors == 0:
            status_val = "passed"
        else:
            status_val = "failed"
            error_msg = f"Schemathesis found {checks_failed} failure(s), {server_errors} server error(s)"

        write_json(
            report_path,
            {
                "status": status_val,
                "openapi_path": str(openapi_path),
                "base_url": effective_base_url,
                "summary": exec_summary,
                "findings": st_findings[:50],
                "stdout_tail": stdout_tail,
            },
        )

    elif tool == "nuclei":
        if not effective_base_url:
            return ExecuteExternalToolResponse(
                executed=False,
                tool=tool,
                status="error",
                duration_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                error="Target base_url is required to run nuclei.",
            )
        sec_dir = ensure_dir(run_dir / "security")
        nuc_res = await asyncio.to_thread(
            run_nuclei_scan,
            effective_base_url,
            sec_dir,
            severity="low,medium,high,critical",
            timeout=clamped_timeout,
        )
        findings = nuc_res.get("findings") if isinstance(nuc_res.get("findings"), list) else []
        by_sev: dict[str, int] = {}
        for f in findings:
            sev = str(f.get("severity") or "info").lower()
            by_sev[sev] = by_sev.get(sev, 0) + 1
        high_or_crit = by_sev.get("high", 0) + by_sev.get("critical", 0)
        exec_summary = {
            "findings_count": len(findings),
            "by_severity": by_sev,
            "high_or_critical": high_or_crit,
        }
        stdout_tail = _redact_secrets_from_text(str(nuc_res.get("stderr") or ""), secret_values)
        nuc_status = str(nuc_res.get("status") or "completed")
        if nuc_status == "timed_out":
            status_val = "timeout"
            error_msg = f"Nuclei timed out after {clamped_timeout}s"
        elif high_or_crit > 0:
            status_val = "failed"
            error_msg = f"Nuclei detected {high_or_crit} high/critical finding(s)"
        elif nuc_status == "error":
            status_val = "failed"
            error_msg = stdout_tail or "Nuclei execution failed"
        else:
            status_val = "passed"

    elif tool == "k6":
        perf_dir = ensure_dir(run_dir / "performance")
        k6_script = perf_dir / "k6" / "scenario_ramp.js"
        if not k6_script.is_file() and (run_dir / "perf" / "k6_load.js").is_file():
            k6_script = run_dir / "perf" / "k6_load.js"
        if not k6_script.is_file():
            har_dir = run_dir / "har"
            if not har_dir.is_dir():
                har_dir = run_dir / "hars"
            if not har_dir.is_dir():
                return ExecuteExternalToolResponse(
                    executed=False,
                    tool=tool,
                    status="error",
                    duration_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                    error="No k6 script or HAR directory found for this run.",
                )
            corr_path = run_dir / "hub" / "correlation.json"
            await asyncio.to_thread(
                generate_k6_script,
                har_dir,
                perf_dir,
                correlation_path=corr_path if corr_path.is_file() else None,
            )

        k6_summary_path = ensure_dir(perf_dir / "k6") / "k6_smoke_summary.json"
        env = os.environ.copy()
        if effective_base_url:
            env["BASE_URL"] = effective_base_url
        for hk, hv in cred_headers.items():
            low = hk.lower()
            if low == "cookie":
                env["AUTH_COOKIE"] = hv
            elif low in {"authorization", "token", "accesstoken", "x-access-token", "x-auth-token"}:
                env["AUTH_TOKEN"] = hv

        cmd = [
            bin_path,
            "run",
            "--vus",
            "2",
            "--duration",
            "5s",
            "--summary-export",
            str(k6_summary_path),
            str(k6_script),
        ]
        rc, out, err, timed_out = await _run_exec(
            cmd, cwd=k6_script.parent, env=env, timeout=clamped_timeout
        )
        combined = "\n".join(p for p in (out.strip(), err.strip()) if p)
        stdout_tail = _redact_secrets_from_text(combined[-4000:], secret_values)
        exec_summary = _parse_k6_summary(k6_summary_path)
        if timed_out:
            status_val = "timeout"
            error_msg = f"k6 timed out after {clamped_timeout}s"
        elif rc == 0:
            status_val = "passed"
        else:
            status_val = "failed"
            error_msg = f"k6 exited with code {rc}"

    synced_scenarios = synced_assertions = synced_negative_cases = 0
    if body.sync_scenarios and tool in {"java_test", "schemathesis", "nuclei"}:
        synced_scenarios, synced_assertions, synced_negative_cases = (
            await _sync_scenarios_after_tool(
                db,
                project_id,
                tool=tool,
                exec_summary=exec_summary,
                test_records=test_records,
            )
        )

    duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return ExecuteExternalToolResponse(
        executed=True,
        tool=tool,
        status=status_val,
        duration_ms=duration_ms,
        summary=exec_summary,
        stdout_tail=stdout_tail,
        synced_scenarios=synced_scenarios,
        synced_assertions=synced_assertions,
        synced_negative_cases=synced_negative_cases,
        error=error_msg,
    )


@router.get(
    "/projects/{project_id}/acceptance-prompt",
    response_model=AcceptancePromptResponse,
)
async def generate_acceptance_prompt(
    project_id: str,
    lang: Literal["zh", "en"] = Query(default="zh"),
    agent: str = Query(default="generic"),
    mcp_url: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> AcceptancePromptResponse:
    """Generate a live, secret-free Markdown AI Acceptance Prompt recipe for external AI Agents."""
    project = await _get_project_or_404(db, project_id)
    norm_agent = (agent or "generic").strip().lower()
    if norm_agent not in _AGENT_LABELS:
        norm_agent = "generic"
    agent_label_zh, agent_label_en = _AGENT_LABELS[norm_agent]

    targets = list(
        (await db.execute(select(Target).where(Target.project_id == project_id))).scalars().all()
    )
    credentials = list(
        (await db.execute(select(AuthProfile).where(AuthProfile.project_id == project_id)))
        .scalars()
        .all()
    )
    kb_docs = list(
        (
            await db.execute(
                select(KnowledgeDoc)
                .where(KnowledgeDoc.project_id == project_id)
                .order_by(KnowledgeDoc.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )

    completed_runs, inventory = await _load_project_inventory_context(db, project)
    await sync_project_scenarios(db, project_id, inventory=inventory)
    sc_rows = list(
        (await db.execute(select(ScenarioModel).where(ScenarioModel.project_id == project_id)))
        .scalars()
        .all()
    )
    runs_by_id = {r.id: r for r in completed_runs}
    enriched_scenarios = [
        _enrich_scenario(r, inventory=inventory, runs_by_id=runs_by_id)[0] for r in sc_rows
    ]
    sc_list = _build_scenario_list_response(project_id, enriched_scenarios)

    latest_run_id = completed_runs[-1].id if completed_runs else "<run_id>"
    har_live_count = int(inventory.get("har_live_count") or 0)
    total_endpoints = int(inventory.get("total_endpoints") or 0)
    coverage_pct = float(inventory.get("coverage_pct") or 0.0)
    by_prefix = inventory.get("by_prefix") or []
    uncovered_prefixes = [
        p
        for p in by_prefix
        if int(p.get("total") or 0) > int(p.get("covered") or 0)
    ][:6]

    needing_attention = [
        s
        for s in sc_list.scenarios
        if s.effectiveness_level in {"L0", "L1", "L2"} and s.status != "archived"
    ]

    # Build ZERO-SECRET lines (never print extra_headers_ref values or storage_state contents!)
    target_lines = (
        "\n".join(f"- `{t.base_url}` (domain: `{t.root_domain}`)" for t in targets)
        if targets
        else ("- （暂无已配置目标，请先在项目设置或 Explore 阶段补充）" if lang == "zh" else "- (No targets configured yet)")
    )
    cred_lines = (
        "\n".join(
            f"- **{c.name}** (id: `{c.id}`, role: `{c.role_tag}`, headers: `{', '.join(sorted((c.extra_headers_ref or {}).keys())) or 'storage_state'}`)"
            for c in credentials
        )
        if credentials
        else (
            "- （暂无已保存凭据 — 可在录制后调用 `reconqa_extract_credential` 从 HAR 自动提取）"
            if lang == "zh"
            else "- (No saved credentials yet — call `reconqa_extract_credential` after HAR capture)"
        )
    )
    kb_lines = (
        "\n".join(f"- [{d.doc_type}] {d.title}" for d in kb_docs[:12])
        if kb_docs
        else ("- （暂无知识库文档）" if lang == "zh" else "- (No Knowledge Base documents yet)")
    )
    prefix_lines = (
        "\n".join(
            f"- `{p['prefix']}`: {p['covered']}/{p['total']} ({p['coverage_pct']}%)"
            for p in uncovered_prefixes
        )
        if uncovered_prefixes
        else ("- 所有已发现前缀均已实测覆盖" if lang == "zh" else "- All discovered prefixes are live-covered")
    )
    attention_lines = (
        "\n".join(
            f"- `[{s.priority}]` `{s.slug}` — **{s.name}** (当前 `{s.effectiveness_level}`, 端点 {s.endpoint_count}, 断言 {s.assertion_count}, 异常用例 {s.negative_cases_covered_count}/{s.negative_case_count})"
            if lang == "zh"
            else f"- `[{s.priority}]` `{s.slug}` — **{s.name}** (level `{s.effectiveness_level}`, endpoints={s.endpoint_count}, assertions={s.assertion_count}, negatives={s.negative_cases_covered_count}/{s.negative_case_count})"
            for s in needing_attention[:12]
        )
        if needing_attention
        else (
            "- 所有核心业务场景均已达到 L3/L4 实效等级！"
            if lang == "zh"
            else "- All active business scenarios have reached L3/L4 effectiveness!"
        )
    )

    mcp_hint = f"\n- **ReconQA MCP URL**: `{mcp_url.strip()}`" if mcp_url and mcp_url.strip() else ""

    if lang == "zh":
        prompt_markdown = f"""# ReconQA 自主外部系统验收指令（{agent_label_zh}）

你正在对项目 **{project.name}**（`project_id`: `{project.id}`）执行端到端、面向测试实效（L0→L4）的自主验收闭环。{mcp_hint}

## 当前项目上下文（实时聚合，已脱敏）
- **项目 ID**: `{project.id}` | **最新已完成 Run**: `{latest_run_id}`
- **API 资产库覆盖率**: `{har_live_count} / {total_endpoints}`（`{coverage_pct}%` 实测覆盖）
- **核心场景实效达标率（P0/P1 达到 L3/L4）**: `{sc_list.p0_p1_effective_count} / {sc_list.p0_p1_total}`（`{sc_list.p0_p1_effective_pct}%`）

### 目标系统 Targets
{target_lines}

### 已保存测试凭据（仅名称与 ID，绝不暴露明文）
{cred_lines}

### 项目知识库（PRD / 设计 / 规则）
{kb_lines}

### 待补齐覆盖的 API 前缀
{prefix_lines}

### 仍停留在 L0/L1/L2、需补齐业务断言与异常边界的场景（共 {len(needing_attention)} 个）
{attention_lines}

---

## 4 阶段自主验收执行闭环（External-Tool Autonomous Loop）

### 阶段 1：上下文盘点与差距识别
1. 调用 `reconqa_get_project_inventory(project_id="{project.id}")` 盘点 `har-live` 与未覆盖的 `js-static` 接口。
2. 调用 `reconqa_list_scenarios(project_id="{project.id}")` 识别所有 `P0/P1` 且仍处于 `L0/L1/L2` 的业务场景。
3. 结合上方项目知识库中的 PRD/业务规则，梳理每个核心场景的真实业务预期（不仅是 HTTP 200）。

### 阶段 2：外部 Playwright 漫游录制与凭据自动提取
1. 使用外部 Playwright / 浏览器驱动访问未覆盖页面与核心业务流，保存分场景 `.har` 流量文件。
2. 调用 `reconqa_upload_har_files` 上传新录制 HAR 并触发 `merge` 合并分析。
3. 若项目尚无可用测试凭据或 Session 已过期，调用 `reconqa_extract_credential(project_id="{project.id}")` 从最新 Run HAR 中自动提取并保存鉴权凭据。

### 阶段 3：场景实效升级（L3 业务断言 + L4 异常/边界）与在线回放验证
1. 对每个 `P0/P1` 场景调用 `reconqa_upsert_scenario(project_id="{project.id}", slug="...", ...)`：
   - 补全清晰的中文业务名 `name`、`description`、`preconditions`、`business_domain` 与 `priority`；
   - 写入结构化 `assertions`（`target_endpoint`、`jsonpath`、`operator`、`expected`），ReconQA 将自动编译进 Java RestAssured 测试代码；
   - 写入 `negative_cases`（覆盖 `auth / validation / boundary / idempotency / state_machine / security`）。
2. 调用 `reconqa_replay_endpoint(project_id="{project.id}", ...)` 配合保存的 `credential_id` 实测验证关键接口响应结构与边界报错。

### 阶段 4：执行真实外部工具并自动回写场景状态
依次调用 `reconqa_run_external_tool(project_id="{project.id}", run_id="{latest_run_id}", tool=..., sync_scenarios=True)`：
1. `tool="java_test"`：运行 Maven + TestNG + RestAssured + Allure（自动执行编译后的业务断言并回写 `passed/failed`）。
2. `tool="schemathesis"`：运行 OpenAPI 边界/类型 Fuzzing（无 5xx 自动将 `validation/boundary` 负向用例回写为 `covered`）。
3. `tool="nuclei"`：运行安全配置扫描（无高危自动将 `auth/security` 用例回写为 `covered`）。
4. `tool="k6"`：运行安全限流基线压测（检查 `p95_ms` 与失败率）。

最后再次调用 `reconqa_list_scenarios(project_id="{project.id}")`，输出验收总结报告：覆盖率提升、P0/P1 达到 L3/L4 的实效比例、外部工具真实执行结果与未决风险清单。
"""
    else:
        prompt_markdown = f"""# ReconQA Autonomous External-System Acceptance Recipe ({agent_label_en})

You are running an end-to-end, effectiveness-driven (L0→L4) autonomous acceptance loop for project **{project.name}** (`project_id`: `{project.id}`).{mcp_hint}

## Live Project Context (Secret-Free Snapshot)
- **Project ID**: `{project.id}` | **Latest Completed Run**: `{latest_run_id}`
- **Master API Inventory Coverage**: `{har_live_count} / {total_endpoints}` (`{coverage_pct}%` live-verified)
- **P0/P1 Scenario Effectiveness (L3/L4)**: `{sc_list.p0_p1_effective_count} / {sc_list.p0_p1_total}` (`{sc_list.p0_p1_effective_pct}%`)

### Configured Targets
{target_lines}

### Saved Auth Credentials (Metadata Only — Zero Raw Secrets)
{cred_lines}

### Project Knowledge Base (PRDs / Specs / Notes)
{kb_lines}

### Top Uncovered API Prefixes
{prefix_lines}

### Scenarios Still at L0/L1/L2 Needing Assertions or Negative Cases ({len(needing_attention)})
{attention_lines}

---

## 4-Stage Autonomous Acceptance Loop (External Tools + ReconQA MCP)

### Stage 1: Inventory & Effectiveness Gap Triage
1. Call `reconqa_get_project_inventory(project_id="{project.id}")` to review live-covered vs. static JS endpoints.
2. Call `reconqa_list_scenarios(project_id="{project.id}")` to identify every `P0/P1` scenario stuck at `L0/L1/L2`.
3. Cross-reference the Project Knowledge Base docs above to define real business invariants beyond HTTP 200.

### Stage 2: Autonomous Playwright Roaming & Credential Extraction
1. Use external Playwright/browser automation to exercise uncovered routes and business flows, capturing per-scenario `.har` files.
2. Call `reconqa_upload_har_files` to ingest the new HARs and trigger `merge`.
3. If no credential exists or the session expired, call `reconqa_extract_credential(project_id="{project.id}")` to extract and store reusable auth headers from the latest Run HAR.

### Stage 3: Upgrade Scenarios to L3/L4 & Live Inline Replay
1. For each `P0/P1` scenario, call `reconqa_upsert_scenario(project_id="{project.id}", slug="...", ...)` to populate:
   - human-readable `name`, `description`, `preconditions`, `business_domain`, and `priority`;
   - structured `assertions` (`target_endpoint`, `jsonpath`, `operator`, `expected`) — automatically compiled into Java RestAssured tests;
   - `negative_cases` covering `auth`, `validation`, `boundary`, `idempotency`, `state_machine`, and `security`.
2. Verify critical endpoints and edge payloads live using `reconqa_replay_endpoint(project_id="{project.id}", ...)` with `credential_id`.

### Stage 4: Execute Real External Tools & Auto Sync-Back
Run `reconqa_run_external_tool(project_id="{project.id}", run_id="{latest_run_id}", tool=..., sync_scenarios=True)` across:
1. `tool="java_test"` — executes Maven + TestNG + RestAssured + Allure and syncs compiled business assertions to `passed`/`failed`.
2. `tool="schemathesis"` — executes OpenAPI contract/boundary fuzzing and syncs `validation`/`boundary` negative cases.
3. `tool="nuclei"` — runs security template checks and syncs `auth`/`security` negative cases.
4. `tool="k6"` — runs bounded smoke load test and reports `p95_ms` and `http_req_failed_rate`.

Finish by calling `reconqa_list_scenarios(project_id="{project.id}")` and presenting a concise acceptance summary (API coverage delta, P0/P1 L3/L4 effectiveness %, tool verdicts, and remaining risks).
"""

    return AcceptancePromptResponse(
        project_id=project.id,
        project_name=project.name,
        lang=lang,
        agent=norm_agent,
        prompt_markdown=prompt_markdown.strip(),
        context_summary=AcceptancePromptContextSummary(
            targets_count=len(targets),
            credentials_count=len(credentials),
            kb_docs_count=len(kb_docs),
            coverage_pct=coverage_pct,
            p0_p1_effective_pct=sc_list.p0_p1_effective_pct,
            scenarios_needing_attention=len(needing_attention),
        ),
    )
