"""Unified ReconQA pipeline orchestrator shared by CLI and FastAPI Worker."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reconqa.config import load_project_config, settings
from reconqa.utils.io import ensure_dir, read_json, read_jsonl, sha256_file, write_json

DEFAULT_MODULES = ["merge", "java_gen", "security", "perf"]
ALL_MODULES = ["discovery", "record", "merge", "java_gen", "security", "perf"]


def _extract_domain(target_url: str | None, project_config: Any) -> str | None:
    if target_url:
        parsed = urlparse(target_url if "://" in target_url else f"https://{target_url}")
        if parsed.hostname:
            return parsed.hostname
    for attr in ("target_domain", "domain"):
        val = getattr(project_config, attr, None)
        if val:
            return str(val)
    target_obj = getattr(project_config, "target", None)
    if target_obj is not None:
        if isinstance(target_obj, dict):
            dom = target_obj.get("domain") or target_obj.get("root_domain")
            if dom:
                return str(dom)
            base = target_obj.get("base_url")
            if base:
                return urlparse(str(base)).hostname
        else:
            dom = getattr(target_obj, "domain", None) or getattr(target_obj, "root_domain", None)
            if dom:
                return str(dom)
            base = getattr(target_obj, "base_url", None)
            if base:
                return urlparse(str(base)).hostname
    base_url = getattr(project_config, "base_url", None)
    if base_url:
        return urlparse(str(base_url)).hostname
    return None


def _extract_base_url(target_url: str | None, project_config: Any) -> str | None:
    if target_url:
        return target_url
    base_url = getattr(project_config, "base_url", None)
    if base_url:
        return str(base_url)
    target_obj = getattr(project_config, "target", None)
    if isinstance(target_obj, dict) and target_obj.get("base_url"):
        return str(target_obj["base_url"])
    if target_obj is not None and getattr(target_obj, "base_url", None):
        return str(target_obj.base_url)
    return None


def _cfg_get(obj: Any, *keys: str, default: Any = None) -> Any:
    cur = obj
    for key in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
    return default if cur is None else cur


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def _collect_artifacts(run_dir: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    file_specs: list[tuple[str, Path]] = [
        ("spa_routes_json", run_dir / "discovery" / "spa_routes.json"),
        ("js_endpoints_json", run_dir / "har" / "js_endpoints.json"),
        ("openapi_yaml", run_dir / "hub" / "openapi.yaml"),
        ("openapi_json", run_dir / "hub" / "openapi.json"),
        ("correlation_json", run_dir / "hub" / "correlation.json"),
        ("correlation_mmd", run_dir / "hub" / "correlation.mmd"),
        ("coverage_diff_json", run_dir / "hub" / "coverage_diff.json"),
        ("coverage_diff_md", run_dir / "hub" / "coverage_diff.md"),
        ("schemathesis_report", run_dir / "java-tests" / "schemathesis_report.json"),
        ("static_audit_json", run_dir / "security" / "static_audit.json"),
        ("static_audit_md", run_dir / "security" / "static_audit.md"),
        ("auth_matrix_json", run_dir / "security" / "auth_matrix.json"),
        ("auth_matrix_md", run_dir / "security" / "auth_matrix.md"),
        ("nuclei_jsonl", run_dir / "security" / "nuclei.jsonl"),
        ("har_perf_json", run_dir / "performance" / "har_perf_report.json"),
        ("har_perf_md", run_dir / "performance" / "har_perf_report.md"),
        ("k6_script", run_dir / "performance" / "k6" / "scenario_ramp.js"),
        ("k6_analysis_json", run_dir / "performance" / "k6" / "k6_analysis.json"),
        ("run_summary_json", run_dir / "run_summary.json"),
    ]
    for kind, path in file_specs:
        if path.is_file():
            artifacts.append(
                {
                    "kind": kind,
                    "rel_path": path.relative_to(run_dir).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )

    har_dir = run_dir / "har"
    if har_dir.is_dir():
        for har_file in sorted(har_dir.glob("*.har")):
            artifacts.append(
                {
                    "kind": "har",
                    "rel_path": har_file.relative_to(run_dir).as_posix(),
                    "size_bytes": har_file.stat().st_size,
                    "sha256": sha256_file(har_file),
                }
            )

    dir_specs: list[tuple[str, Path]] = [
        ("java_tests", run_dir / "java-tests"),
        ("burp_exports", run_dir / "security" / "burp_exports"),
        ("k6_dir", run_dir / "performance" / "k6"),
    ]
    for kind, path in dir_specs:
        if path.is_dir() and any(path.rglob("*")):
            artifacts.append(
                {
                    "kind": kind,
                    "rel_path": path.relative_to(run_dir).as_posix(),
                    "size_bytes": _dir_size(path),
                    "sha256": "",
                }
            )

    return artifacts


def _normalize_finding(raw: dict[str, Any], default_category: str) -> dict[str, Any]:
    return {
        "category": str(raw.get("category") or default_category),
        "severity": str(raw.get("severity") or "info").lower(),
        "endpoint": str(raw.get("endpoint") or raw.get("url") or raw.get("path") or ""),
        "title": str(raw.get("title") or raw.get("name") or raw.get("type") or "Finding"),
        "evidence": raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {},
    }


def _collect_findings(run_dir: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    sources: list[tuple[Path, str]] = [
        (run_dir / "security" / "static_audit.json", "security"),
        (run_dir / "security" / "auth_matrix.json", "security"),
        (run_dir / "performance" / "har_perf_report.json", "performance"),
    ]
    for path, category in sources:
        if not path.is_file():
            continue
        try:
            data = read_json(path)
        except Exception:
            continue
        raw_list = data.get("findings", []) if isinstance(data, dict) else []
        for item in raw_list:
            if isinstance(item, dict):
                findings.append(_normalize_finding(item, category))

    nuclei_path = run_dir / "security" / "nuclei.jsonl"
    if nuclei_path.is_file():
        for rec in read_jsonl(nuclei_path):
            info = rec.get("info") if isinstance(rec.get("info"), dict) else {}
            findings.append(
                _normalize_finding(
                    {
                        "category": "security",
                        "severity": info.get("severity", rec.get("severity", "info")),
                        "endpoint": rec.get("matched-at", rec.get("host", "")),
                        "title": info.get("name", rec.get("template-id", "Nuclei Finding")),
                        "evidence": {
                            "template_id": rec.get("template-id", ""),
                            "matcher_name": rec.get("matcher-name", ""),
                        },
                    },
                    "security",
                )
            )
    return findings


def _ensure_js_extraction(
    har_dir: Path,
    target_domain: str | None,
    project_config: Any,
    log: Callable[[str], None],
) -> Path | None:
    js_bundles_dir = har_dir / "js_bundles"
    js_endpoints_path = har_dir / "js_endpoints.json"
    if js_bundles_dir.is_dir() and any(js_bundles_dir.rglob("*.js")):
        from reconqa.modules.recorder.js_extractor import extract_js_endpoints

        log("Extracting static API endpoints from JS bundles...")
        extra_prefixes = _cfg_get(project_config, "extra_api_prefixes", default=None)
        skip_prefixes = _cfg_get(project_config, "ignore_path_prefixes", default=None)
        extract_js_endpoints(
            js_bundles_dir,
            js_endpoints_path,
            target_domain=target_domain,
            extra_prefixes=extra_prefixes,
            skip_prefixes=skip_prefixes,
        )
        return js_endpoints_path
    return js_endpoints_path if js_endpoints_path.is_file() else None


def run_pipeline(
    run_dir: Path,
    *,
    target_url: str | None = None,
    modules: list[str] | None = None,
    har_sources: list[Path] | None = None,
    config_path: Path | None = None,
    auth_a: Path | None = None,
    auth_b: Path | None = None,
    include_mutations: bool = False,
    discovery_mode: str = "passive",
    run_nuclei: bool = False,
    run_k6: bool = False,
    run_schemathesis: bool = False,
    log_cb: Callable[[str], None] | None = None,
    auth_a_headers: dict[str, str] | None = None,
    auth_b_headers: dict[str, str] | None = None,
    module_cb: Callable[[str, str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute the end-to-end ReconQA pipeline into ``run_dir``."""
    run_dir = Path(run_dir).resolve()
    discovery_dir = ensure_dir(run_dir / "discovery")
    har_dir = ensure_dir(run_dir / "har")
    hub_dir = ensure_dir(run_dir / "hub")
    java_dir = ensure_dir(run_dir / "java-tests")
    security_dir = ensure_dir(run_dir / "security")
    perf_dir = ensure_dir(run_dir / "performance")

    def log(msg: str) -> None:
        if log_cb is not None:
            log_cb(msg)

    def emit_module(module: str, status: str, meta: dict[str, Any] | None = None) -> None:
        if module_cb is not None:
            module_cb(module, status, meta or {})

    started_at = datetime.now(UTC).isoformat()
    effective_config_path = config_path
    if effective_config_path is None and (run_dir / "reconqa.yaml").is_file():
        effective_config_path = run_dir / "reconqa.yaml"
    project_config = load_project_config(effective_config_path)

    effective_base_url = _extract_base_url(target_url, project_config)
    target_domain = _extract_domain(effective_base_url, project_config)
    selected_modules = list(modules) if modules else list(DEFAULT_MODULES)

    log(f"Starting ReconQA pipeline in {run_dir} (modules={selected_modules})")

    module_results: dict[str, Any] = {}
    warnings: list[str] = []

    # Step 0: Import existing HAR sources if supplied
    if har_sources:
        from reconqa.modules.recorder.har_importer import import_hars

        log(f"Importing {len(har_sources)} HAR source(s) into {har_dir}...")
        import_res = import_hars(
            [Path(s) for s in har_sources],
            har_dir,
            target_domain=target_domain,
            extract_js=True,
        )
        module_results["import_har"] = import_res
        _ensure_js_extraction(har_dir, target_domain, project_config, log)

    # Module 1: Discovery
    if "discovery" in selected_modules:
        emit_module("discovery", "running")
        try:
            from reconqa.modules.discovery.runner import run_discovery
            from reconqa.modules.discovery.spa_parser import extract_spa_routes

            discovery_target = effective_base_url or target_domain
            if discovery_target:
                log(f"[discovery] Running {discovery_mode} discovery on {discovery_target}...")
                disc_res = run_discovery(
                    discovery_target,
                    discovery_dir,
                    mode=discovery_mode,
                )
            else:
                log("[discovery] No target_url/domain configured; running SPA parser on local JS.")
                disc_res = {"skipped": True, "reason": "no target_url or domain"}
                if (har_dir / "js_bundles").is_dir():
                    disc_res["spa_routes"] = extract_spa_routes(
                        discovery_dir,
                        js_dir=har_dir / "js_bundles",
                    )
            module_results["discovery"] = disc_res
            emit_module("discovery", "completed", disc_res)
        except Exception as exc:
            err_meta = {"error": str(exc)}
            module_results["discovery"] = err_meta
            warnings.append(f"discovery failed: {exc}")
            emit_module("discovery", "failed", err_meta)
            log(f"[discovery] Failed: {exc}")

    # Module 2: Automated recording (if requested and no HARs imported yet)
    if "record" in selected_modules:
        emit_module("record", "running")
        try:
            if not effective_base_url:
                rec_res: dict[str, Any] = {"skipped": True, "reason": "no target_url provided"}
                warnings.append("record skipped: target_url not provided")
                log("[record] Skipped: target_url is required for automated recording.")
            else:
                from reconqa.modules.recorder.playwright_rec import record_auto

                log(f"[record] Running automated Playwright recording on {effective_base_url}...")
                scenarios_raw = _cfg_get(project_config, "scenarios", default=None)
                scenarios_cfg = (
                    [
                        s.model_dump() if hasattr(s, "model_dump") else dict(s)
                        for s in scenarios_raw
                    ]
                    if scenarios_raw
                    else None
                )
                rec_res = record_auto(
                    effective_base_url,
                    har_dir,
                    storage_state=auth_a,
                    extra_headers=auth_a_headers,
                    scenarios_config=scenarios_cfg,
                )
                _ensure_js_extraction(har_dir, target_domain, project_config, log)
            module_results["record"] = rec_res
            emit_module("record", "completed", rec_res)
        except Exception as exc:
            err_meta = {"error": str(exc)}
            module_results["record"] = err_meta
            warnings.append(f"record failed: {exc}")
            emit_module("record", "failed", err_meta)
            log(f"[record] Failed: {exc}")

    # Ensure JS endpoints extracted whenever JS bundles exist
    js_endpoints_path = _ensure_js_extraction(har_dir, target_domain, project_config, log)

    # Module 3: Hub Merge (OpenAPI 3.0.3 + Correlation + Coverage Diff)
    openapi_yaml = hub_dir / "openapi.yaml"
    correlation_json = hub_dir / "correlation.json"
    if "merge" in selected_modules:
        emit_module("merge", "running")
        try:
            from reconqa.modules.hub.correlation import analyze_correlations
            from reconqa.modules.hub.coverage_diff import compute_coverage_diff
            from reconqa.modules.hub.openapi_merger import merge_to_openapi

            log("[merge] Merging HARs + JS endpoints into OpenAPI 3.0.3...")
            openapi_res = merge_to_openapi(
                har_dir,
                hub_dir,
                js_endpoints_path=js_endpoints_path,
                target_domain=target_domain,
                project_config=project_config,
            )
            log("[merge] Analyzing data-flow correlations...")
            corr_res = analyze_correlations(
                har_dir,
                hub_dir,
                target_domain=target_domain,
            )
            log("[merge] Computing live vs static JS coverage diff...")
            cov_res = compute_coverage_diff(openapi_yaml, hub_dir)

            merge_meta = {
                "openapi_paths": len(openapi_res.get("paths", {}))
                if isinstance(openapi_res, dict)
                else 0,
                "correlation_variables": len(corr_res.get("variables", []))
                if isinstance(corr_res, dict)
                else 0,
                "correlation_chains": len(corr_res.get("chains", []))
                if isinstance(corr_res, dict)
                else 0,
                "coverage": cov_res,
            }
            module_results["merge"] = merge_meta
            emit_module("merge", "completed", merge_meta)
        except Exception as exc:
            err_meta = {"error": str(exc)}
            module_results["merge"] = err_meta
            warnings.append(f"merge failed: {exc}")
            emit_module("merge", "failed", err_meta)
            log(f"[merge] Failed: {exc}")

    # Module 4: Java TestNG + RestAssured + Allure Codegen (+ optional Schemathesis)
    if "java_gen" in selected_modules:
        emit_module("java_gen", "running")
        try:
            if not openapi_yaml.is_file():
                java_res: dict[str, Any] = {
                    "skipped": True,
                    "reason": "hub/openapi.yaml not found; run merge first",
                }
                warnings.append("java_gen skipped: openapi.yaml missing")
                log("[java_gen] Skipped: openapi.yaml not found.")
            else:
                from reconqa.modules.codegen_java.generator import generate_java_project

                package_name = _cfg_get(
                    project_config, "java", "package_name", default="com.reconqa.generated"
                )
                java_version = str(
                    _cfg_get(project_config, "java", "java_version", default="21")
                )
                log(
                    f"[java_gen] Generating Java {java_version} Maven project "
                    f"(package={package_name})..."
                )
                java_res = generate_java_project(
                    openapi_yaml,
                    java_dir,
                    correlation_path=correlation_json if correlation_json.is_file() else None,
                    package_name=package_name,
                    java_version=java_version,
                    base_uri=effective_base_url,
                    project_config=project_config,
                )
                if run_schemathesis and effective_base_url:
                    from reconqa.modules.codegen_java.schemathesis_run import (
                        run_schemathesis as _run_st,
                    )

                    log("[java_gen] Running Schemathesis contract fuzzing...")
                    java_res["schemathesis"] = _run_st(
                        openapi_yaml,
                        effective_base_url,
                        java_dir,
                        headers=auth_a_headers,
                    )
            module_results["java_gen"] = java_res
            emit_module("java_gen", "completed", java_res)
        except Exception as exc:
            err_meta = {"error": str(exc)}
            module_results["java_gen"] = err_meta
            warnings.append(f"java_gen failed: {exc}")
            emit_module("java_gen", "failed", err_meta)
            log(f"[java_gen] Failed: {exc}")

    # Module 5: Security Audit (Static + Auth Matrix + Burp Export + optional Nuclei)
    if "security" in selected_modules:
        emit_module("security", "running")
        try:
            from reconqa.modules.security.auth_matrix import run_auth_matrix
            from reconqa.modules.security.burp_exporter import export_burp_requests
            from reconqa.modules.security.static_audit import run_static_audit

            sec_meta: dict[str, Any] = {}
            js_dir = har_dir / "js_bundles" if (har_dir / "js_bundles").is_dir() else None
            log("[security] Running static security audit on HARs and JS bundles...")
            sec_meta["static_audit"] = run_static_audit(
                har_dir,
                security_dir,
                js_dir=js_dir,
                target_domain=target_domain,
            )

            eff_include_mutations = include_mutations or bool(
                _cfg_get(project_config, "security", "include_mutations", default=False)
            )
            if auth_a or auth_b or auth_a_headers or auth_b_headers:
                log("[security] Running auth boundary & A/B IDOR matrix...")
                sec_meta["auth_matrix"] = run_auth_matrix(
                    har_dir,
                    security_dir,
                    auth_a_path=auth_a,
                    auth_b_path=auth_b,
                    auth_a_headers=auth_a_headers,
                    auth_b_headers=auth_b_headers,
                    include_mutations=eff_include_mutations,
                    target_domain=target_domain,
                )

            auth_matrix_path = security_dir / "auth_matrix.json"
            log("[security] Exporting high-risk / signed requests to Burp format...")
            sec_meta["burp_export"] = export_burp_requests(
                har_dir,
                security_dir,
                auth_matrix_path=auth_matrix_path if auth_matrix_path.is_file() else None,
                target_domain=target_domain,
            )

            if run_nuclei and effective_base_url:
                from reconqa.modules.security.nuclei_runner import run_nuclei_scan

                log(f"[security] Running nuclei scan against {effective_base_url}...")
                sec_meta["nuclei"] = run_nuclei_scan(effective_base_url, security_dir)

            module_results["security"] = sec_meta
            emit_module("security", "completed", sec_meta)
        except Exception as exc:
            err_meta = {"error": str(exc)}
            module_results["security"] = err_meta
            warnings.append(f"security failed: {exc}")
            emit_module("security", "failed", err_meta)
            log(f"[security] Failed: {exc}")

    # Module 6: Performance Profiling & k6 Script Generation
    if "perf" in selected_modules:
        emit_module("perf", "running")
        try:
            from reconqa.modules.performance.har_profiler import analyze_har_performance
            from reconqa.modules.performance.k6_generator import generate_k6_script, run_k6_test

            perf_meta: dict[str, Any] = {}
            log("[perf] Profiling HAR performance metrics...")
            perf_meta["har_profiler"] = analyze_har_performance(
                har_dir,
                perf_dir,
                target_domain=target_domain,
            )

            stages = _cfg_get(project_config, "k6", "stages", default=None)
            proxy_url = _cfg_get(project_config, "k6", "proxy_url", default=settings.k6_proxy_url)
            no_conn_reuse = bool(_cfg_get(project_config, "k6", "no_conn_reuse", default=False))

            log("[perf] Generating k6 ramping load test script...")
            k6_gen_res = generate_k6_script(
                har_dir,
                perf_dir,
                correlation_path=correlation_json if correlation_json.is_file() else None,
                stages=stages,
                proxy_url=proxy_url,
                target_domain=target_domain,
            )
            perf_meta["k6_generator"] = k6_gen_res

            k6_script = perf_dir / "k6" / "scenario_ramp.js"
            if run_k6 and k6_script.is_file():
                log("[perf] Running local k6 load test...")
                perf_meta["k6_run"] = run_k6_test(
                    k6_script,
                    perf_dir,
                    base_url=effective_base_url,
                    proxy_url=proxy_url,
                    no_conn_reuse=no_conn_reuse,
                )

            module_results["perf"] = perf_meta
            emit_module("perf", "completed", perf_meta)
        except Exception as exc:
            err_meta = {"error": str(exc)}
            module_results["perf"] = err_meta
            warnings.append(f"perf failed: {exc}")
            emit_module("perf", "failed", err_meta)
            log(f"[perf] Failed: {exc}")

    # Aggregate findings & summary metrics
    findings = _collect_findings(run_dir)
    severity_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    for f in findings:
        sev = f["severity"]
        cat = f["category"]
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
        category_counts[cat] = category_counts.get(cat, 0) + 1

    cov_data: dict[str, Any] = {}
    cov_path = hub_dir / "coverage_diff.json"
    if cov_path.is_file():
        try:
            cov_data = read_json(cov_path)
        except Exception:
            cov_data = {}

    corr_data: dict[str, Any] = {}
    if correlation_json.is_file():
        try:
            corr_data = read_json(correlation_json)
        except Exception:
            corr_data = {}

    perf_data: dict[str, Any] = {}
    perf_path = perf_dir / "har_perf_report.json"
    if perf_path.is_file():
        try:
            perf_data = read_json(perf_path)
        except Exception:
            perf_data = {}

    summary_metrics: dict[str, Any] = {
        "modules_executed": selected_modules,
        "har_files_count": len(list(har_dir.glob("*.har"))),
        "total_endpoints": cov_data.get(
            "total_endpoints",
            _cfg_get(module_results, "merge", "openapi_paths", default=0),
        ),
        "har_live_endpoints": cov_data.get("har_live_count", cov_data.get("live_count", 0)),
        "js_only_endpoints": cov_data.get("uncovered_count", cov_data.get("js_only_count", 0)),
        "coverage_pct": cov_data.get("coverage_pct", cov_data.get("overall_coverage_pct", 0.0)),
        "correlation_variables": len(corr_data.get("variables", [])),
        "correlation_chains": len(corr_data.get("chains", [])),
        "findings_total": len(findings),
        "findings_by_severity": severity_counts,
        "findings_by_category": category_counts,
        "slow_endpoints_count": len(
            perf_data.get("slowest_by_ttfb", perf_data.get("slow_endpoints", []))
        ),
        "warnings_count": len(warnings),
    }

    finished_at = datetime.now(UTC).isoformat()
    summary: dict[str, Any] = {
        "run_dir": str(run_dir),
        "target_url": effective_base_url,
        "target_domain": target_domain,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": "completed" if not warnings else "completed_with_warnings",
        "modules": selected_modules,
        "summary_metrics": summary_metrics,
        "findings": findings,
        "warnings": warnings,
        "module_results": module_results,
        "artifacts": [],
    }

    summary_path = run_dir / "run_summary.json"
    write_json(summary_path, summary)
    summary["artifacts"] = _collect_artifacts(run_dir)
    write_json(summary_path, summary)

    log(
        f"Pipeline finished: {summary_metrics['total_endpoints']} endpoints, "
        f"{summary_metrics['findings_total']} findings, {len(summary['artifacts'])} artifacts."
    )
    return summary
