"""ReconQA Typer CLI entrypoint."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import typer
from rich.console import Console
from rich.table import Table

from reconqa.config import doctor_check, load_project_config, write_default_project_config

app = typer.Typer(
    name="reconqa",
    help="ReconQA: One-recording, multi-use Web/API testing platform (HAR 1.2 + OpenAPI 3.0.3).",
    no_args_is_help=True,
)
console = Console()


def _parse_stages(stages_str: str | None) -> list[dict[str, object]] | None:
    if not stages_str:
        return None
    stripped = stages_str.strip()
    if stripped.startswith("["):
        return json.loads(stripped)
    parsed: list[dict[str, object]] = []
    for chunk in stripped.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        dur, target = chunk.split(":", 1)
        parsed.append({"duration": dur.strip(), "target": int(target.strip())})
    return parsed or None


@app.command("init")
def cmd_init(
    output: Path = typer.Option(
        Path("reconqa.yaml"), "--output", "-o", help="Path to write config."
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing config file."),
) -> None:
    """Initialize a project-level reconqa.yaml configuration template."""
    if output.exists() and not force:
        console.print(
            f"[yellow]Config already exists:[/yellow] {output} (use --force to overwrite)"
        )
        raise typer.Exit(code=1)
    written = write_default_project_config(output)
    console.print(f"[green]Created ReconQA project config:[/green] {written}")


@app.command("doctor")
def cmd_doctor() -> None:
    """Check availability of external security, recording, Java, and k6 tools."""
    checks = doctor_check()
    table = Table(title="ReconQA Tool Environment Check")
    table.add_column("Tool", style="cyan", no_wrap=True)
    table.add_column("Status", style="bold")
    table.add_column("Path / Location")
    table.add_column("Notes")

    available_count = 0
    for tool_name, info in checks.items():
        ok = bool(info.get("available"))
        if ok:
            available_count += 1
        status = "[green]OK[/green]" if ok else "[yellow]MISSING[/yellow]"
        path = str(info.get("path") or "-")
        hint = str(info.get("hint") or info.get("install_hint") or "")
        table.add_row(tool_name, status, path, hint)

    console.print(table)
    console.print(
        f"[bold]{available_count}/{len(checks)}[/bold] tools available. "
        "Run [cyan]./scripts/install-tools.sh[/cyan] to install ProjectDiscovery binaries."
    )


@app.command("discover")
def cmd_discover(
    target: str = typer.Argument(..., help="Target domain or base URL."),
    mode: str = typer.Option("passive", "--mode", help="Discovery mode: passive or active."),
    depth: int = typer.Option(3, "--depth", help="Active crawl depth (katana)."),
    crawl_time: str = typer.Option("5m", "--crawl-time", help="Active crawl max duration."),
    out_dir: Path = typer.Option(Path("./workspaces/discovery"), "-o", "--out-dir"),
) -> None:
    """Run passive or active asset and endpoint discovery."""
    from reconqa.modules.discovery.runner import run_discovery

    console.print(f"[cyan]Running {mode} discovery against[/cyan] {target} -> {out_dir}")
    result = run_discovery(target, out_dir, mode=mode, depth=depth, crawl_time=crawl_time)
    console.print_json(data=result)


@app.command("record")
def cmd_record(
    url: str | None = typer.Argument(None, help="Start URL for Playwright recording."),
    interactive: bool = typer.Option(
        True,
        "--interactive/--auto",
        help="Use headed interactive browser recorder (--interactive) or headless auto (--auto).",
    ),
    mitm: bool = typer.Option(False, "--mitm", help="Launch mitmdump proxy recorder instead."),
    port: int = typer.Option(8080, "--port", help="mitmproxy listen port."),
    scenario: str = typer.Option("01_main", "--scenario", help="Initial scenario name."),
    storage_state: Path | None = typer.Option(
        None, "--storage-state", help="Playwright storage_state.json to load."
    ),
    save_storage_state: Path | None = typer.Option(
        None, "--save-storage-state", help="Export storage_state.json after interactive session."
    ),
    domain: str | None = typer.Option(None, "--domain", help="Target domain filter."),
    out_dir: Path = typer.Option(Path("./workspaces/manual/har"), "-o", "--out-dir"),
) -> None:
    """Record multi-scenario HARs via Playwright (interactive/auto) or mitmproxy."""
    if mitm:
        from reconqa.modules.recorder.mitm_addon import build_mitmdump_command

        cmd = build_mitmdump_command(out_dir, port=port, scenario=scenario, target_domain=domain)
        console.print(f"[cyan]Starting mitmdump recorder:[/cyan] {' '.join(cmd)}")
        console.print(
            "Switch scenarios via [bold]http://reconqa.local/scenario/<name>[/bold], "
            "flush via [bold]http://reconqa.local/flush[/bold]."
        )
        proc = subprocess.run(cmd, check=False)
        raise typer.Exit(code=proc.returncode)

    if not url:
        console.print("[red]URL argument is required unless --mitm is specified.[/red]")
        raise typer.Exit(code=2)

    from reconqa.modules.recorder.playwright_rec import record_auto, record_interactive

    if interactive:
        result = record_interactive(
            url,
            out_dir,
            storage_state=storage_state,
            save_storage_state=save_storage_state,
            initial_scenario=scenario,
            target_domain=domain,
        )
    else:
        result = record_auto(
            url,
            out_dir,
            scenario=scenario,
            storage_state=storage_state,
        )
    console.print_json(data=result)


@app.command("import-har")
def cmd_import_har(
    sources: list[Path] = typer.Argument(..., help="One or more .har files or directories."),
    scenario: str | None = typer.Option(None, "--scenario", help="Override scenario name."),
    domain: str | None = typer.Option(None, "--domain", help="Filter entries by target domain."),
    out_dir: Path = typer.Option(Path("./workspaces/imported/har"), "-o", "--out-dir"),
) -> None:
    """Import existing Chrome/Charles/Fiddler/Playwright HARs and extract embedded JS bundles."""
    from reconqa.modules.recorder.har_importer import import_hars
    from reconqa.modules.recorder.js_extractor import extract_js_endpoints

    result = import_hars(sources, out_dir, scenario_name=scenario, target_domain=domain)
    js_dir = out_dir / "js_bundles"
    if js_dir.is_dir() and any(js_dir.rglob("*.js")):
        js_res = extract_js_endpoints(js_dir, out_dir / "js_endpoints.json", target_domain=domain)
        result["js_extraction"] = {
            "out_file": str(out_dir / "js_endpoints.json"),
            "endpoints_found": len(js_res.get("endpoints", [])) if isinstance(js_res, dict) else 0,
        }
    console.print_json(data=result)


@app.command("import-text")
def cmd_import_text(
    source: str = typer.Argument(
        "-",
        help="Path to text file containing cURL / fetch / Raw HTTP / HAR JSON, or '-' for stdin.",
    ),
    scenario: str = typer.Option("01_devtools", "--scenario", help="Scenario name for output HAR."),
    fmt: str = typer.Option(
        "auto", "--format", help="Input format: auto, curl, fetch, raw_http, or har_json."
    ),
    domain: str | None = typer.Option(None, "--domain", help="Filter entries by target domain."),
    out_dir: Path = typer.Option(Path("./workspaces/imported/har"), "-o", "--out-dir"),
) -> None:
    """Import Chrome DevTools cURL / fetch / Raw HTTP / pasted HAR JSON into a HAR 1.2 file."""
    from reconqa.modules.recorder.text_importer import import_traffic_text

    if source == "-":
        content = sys.stdin.read()
    else:
        src_path = Path(source)
        if not src_path.is_file():
            console.print(f"[red]File not found:[/red] {src_path}")
            raise typer.Exit(code=1)
        content = src_path.read_text(encoding="utf-8")

    result = import_traffic_text(
        content,
        out_dir,
        scenario_name=scenario,
        format_hint=fmt,
        target_domain=domain,
    )
    if result.get("requests_imported", 0) == 0:
        console.print(
            f"[red]No valid requests parsed[/red] (detected_format={result.get('detected_format')})"
        )
        raise typer.Exit(code=1)
    console.print_json(data=result)


@app.command("merge")
def cmd_merge(
    run_dir: Path = typer.Option(Path("."), "--run-dir", help="Run workspace containing har/."),
    domain: str | None = typer.Option(None, "--domain", help="Target domain filter."),
    config: Path | None = typer.Option(None, "--config", help="Path to reconqa.yaml."),
) -> None:
    """Merge HARs + JS endpoints into OpenAPI 3.0.3, correlation chains, and coverage diff."""
    from reconqa.modules.hub.correlation import analyze_correlations
    from reconqa.modules.hub.coverage_diff import compute_coverage_diff
    from reconqa.modules.hub.openapi_merger import merge_to_openapi
    from reconqa.modules.recorder.js_extractor import extract_js_endpoints

    proj_cfg = load_project_config(config)
    har_dir = run_dir / "har"
    hub_dir = run_dir / "hub"

    js_endpoints_path = har_dir / "js_endpoints.json"
    js_bundles_dir = har_dir / "js_bundles"
    if (
        not js_endpoints_path.is_file()
        and js_bundles_dir.is_dir()
        and any(js_bundles_dir.rglob("*.js"))
    ):
        extract_js_endpoints(js_bundles_dir, js_endpoints_path, target_domain=domain)

    openapi_res = merge_to_openapi(
        har_dir,
        hub_dir,
        js_endpoints_path=js_endpoints_path if js_endpoints_path.is_file() else None,
        target_domain=domain,
        project_config=proj_cfg,
    )
    corr_res = analyze_correlations(har_dir, hub_dir, target_domain=domain)
    cov_res = compute_coverage_diff(hub_dir / "openapi.yaml", hub_dir)

    table = Table(title="Unified Data Hub Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("OpenAPI Paths", str(len(openapi_res.get("paths", {}))))
    table.add_row("Correlated Variables", str(len(corr_res.get("variables", []))))
    table.add_row("Correlated Chains", str(len(corr_res.get("chains", []))))
    table.add_row(
        "Live Coverage %",
        str(cov_res.get("coverage_pct", cov_res.get("overall_coverage_pct", 0.0))),
    )
    console.print(table)


@app.command("gen-java")
def cmd_gen_java(
    run_dir: Path = typer.Option(Path("."), "--run-dir", help="Run workspace containing hub/."),
    package: str = typer.Option("com.reconqa.generated", "--package", help="Java base package."),
    java_version: str = typer.Option("21", "--java-version", help="Target Java version (21/17)."),
    base_uri: str | None = typer.Option(None, "--base-uri", help="Default base URI."),
    run_schemathesis: bool = typer.Option(False, "--run-schemathesis", help="Run Schemathesis L3."),
    config: Path | None = typer.Option(None, "--config", help="Path to reconqa.yaml."),
) -> None:
    """Generate Java TestNG + RestAssured + Allure Maven project from OpenAPI + Correlation."""
    from reconqa.modules.codegen_java.generator import generate_java_project

    proj_cfg = load_project_config(config)
    openapi_path = run_dir / "hub" / "openapi.yaml"
    corr_path = run_dir / "hub" / "correlation.json"
    out_dir = run_dir / "java-tests"

    result = generate_java_project(
        openapi_path,
        out_dir,
        correlation_path=corr_path if corr_path.is_file() else None,
        package_name=package,
        java_version=java_version,
        base_uri=base_uri,
        project_config=proj_cfg,
    )
    if run_schemathesis and base_uri:
        from reconqa.modules.codegen_java.schemathesis_run import run_schemathesis as _run_st

        result["schemathesis"] = _run_st(openapi_path, base_uri, out_dir)
    console.print_json(data=result)


@app.command("audit")
def cmd_audit(
    run_dir: Path = typer.Option(Path("."), "--run-dir", help="Run workspace containing har/."),
    auth_a: Path | None = typer.Option(None, "--auth-a", help="Account A storage_state.json."),
    auth_b: Path | None = typer.Option(None, "--auth-b", help="Account B storage_state.json."),
    include_mutations: bool = typer.Option(
        False, "--include-mutations", help="Allow A/B replay on mutation endpoints."
    ),
    nuclei: bool = typer.Option(False, "--nuclei", help="Run nuclei active scan."),
    target_url: str | None = typer.Option(None, "--target-url", help="Target URL for nuclei."),
    export_burp: bool = typer.Option(
        True, "--export-burp/--no-export-burp", help="Export high-risk/signed Burp packages."
    ),
    domain: str | None = typer.Option(None, "--domain", help="Target domain filter."),
) -> None:
    """Run static security audit, A/B IDOR auth matrix, optional nuclei, and Burp export."""
    from reconqa.modules.security.auth_matrix import run_auth_matrix
    from reconqa.modules.security.burp_exporter import export_burp_requests
    from reconqa.modules.security.static_audit import run_static_audit

    har_dir = run_dir / "har"
    sec_dir = run_dir / "security"
    js_dir = har_dir / "js_bundles" if (har_dir / "js_bundles").is_dir() else None

    out: dict[str, object] = {}
    out["static_audit"] = run_static_audit(har_dir, sec_dir, js_dir=js_dir, target_domain=domain)

    if auth_a or auth_b:
        out["auth_matrix"] = run_auth_matrix(
            har_dir,
            sec_dir,
            auth_a_path=auth_a,
            auth_b_path=auth_b,
            include_mutations=include_mutations,
            target_domain=domain,
        )

    if export_burp:
        am_path = sec_dir / "auth_matrix.json"
        out["burp_export"] = export_burp_requests(
            har_dir,
            sec_dir,
            auth_matrix_path=am_path if am_path.is_file() else None,
            target_domain=domain,
        )

    if nuclei and target_url:
        from reconqa.modules.security.nuclei_runner import run_nuclei_scan

        out["nuclei"] = run_nuclei_scan(target_url, sec_dir)

    console.print_json(data=out)


@app.command("perf")
def cmd_perf(
    run_dir: Path = typer.Option(Path("."), "--run-dir", help="Run workspace containing har/."),
    proxy_url: str | None = typer.Option(None, "--proxy-url", help="HTTP/HTTPS proxy for k6."),
    stages: str | None = typer.Option(
        None,
        "--stages",
        help="k6 stages, e.g. '30s:10,1m:30,30s:0' or JSON array.",
    ),
    run_k6: bool = typer.Option(False, "--run-k6", help="Execute generated k6 script locally."),
    no_conn_reuse: bool = typer.Option(False, "--no-conn-reuse", help="Disable k6 keep-alive."),
    base_url: str | None = typer.Option(None, "--base-url", help="Override BASE_URL for k6."),
    domain: str | None = typer.Option(None, "--domain", help="Target domain filter."),
) -> None:
    """Profile HAR performance bottlenecks and generate/run k6 ramping load scripts."""
    from reconqa.modules.performance.har_profiler import analyze_har_performance
    from reconqa.modules.performance.k6_generator import generate_k6_script, run_k6_test

    har_dir = run_dir / "har"
    perf_dir = run_dir / "performance"
    corr_path = run_dir / "hub" / "correlation.json"

    out: dict[str, object] = {}
    out["har_profiler"] = analyze_har_performance(har_dir, perf_dir, target_domain=domain)
    out["k6_generator"] = generate_k6_script(
        har_dir,
        perf_dir,
        correlation_path=corr_path if corr_path.is_file() else None,
        stages=_parse_stages(stages),
        proxy_url=proxy_url,
        target_domain=domain,
    )
    if run_k6:
        script_path = perf_dir / "k6" / "scenario_ramp.js"
        out["k6_run"] = run_k6_test(
            script_path,
            perf_dir,
            base_url=base_url,
            proxy_url=proxy_url,
            no_conn_reuse=no_conn_reuse,
        )
    console.print_json(data=out)


@app.command("pipeline")
def cmd_pipeline(
    target_url: str | None = typer.Option(None, "--target-url", help="Target base URL."),
    har: list[Path] | None = typer.Option(
        None, "--har", help="Existing HAR file(s) or directory to import/reuse (repeatable)."
    ),
    modules: str | None = typer.Option(
        None,
        "--modules",
        help="Comma-separated modules (discovery,record,merge,java_gen,security,perf).",
    ),
    config: Path | None = typer.Option(None, "--config", help="Path to reconqa.yaml."),
    auth_a: Path | None = typer.Option(None, "--auth-a", help="Account A storage_state.json."),
    auth_b: Path | None = typer.Option(None, "--auth-b", help="Account B storage_state.json."),
    include_mutations: bool = typer.Option(False, "--include-mutations"),
    discovery_mode: str = typer.Option("passive", "--discovery-mode"),
    run_nuclei: bool = typer.Option(False, "--nuclei"),
    run_k6: bool = typer.Option(False, "--run-k6"),
    run_schemathesis: bool = typer.Option(False, "--run-schemathesis"),
    out_dir: Path = typer.Option(Path("./workspaces/cli_run"), "-o", "--out-dir"),
) -> None:
    """Run the full ReconQA pipeline end-to-end (supports reusing existing HARs via --har)."""
    from reconqa.pipeline import run_pipeline

    mod_list = [m.strip() for m in modules.split(",") if m.strip()] if modules else None
    summary = run_pipeline(
        out_dir,
        target_url=target_url,
        modules=mod_list,
        har_sources=har,
        config_path=config,
        auth_a=auth_a,
        auth_b=auth_b,
        include_mutations=include_mutations,
        discovery_mode=discovery_mode,
        run_nuclei=run_nuclei,
        run_k6=run_k6,
        run_schemathesis=run_schemathesis,
        log_cb=lambda msg: console.print(f"[dim]{msg}[/dim]"),
    )

    metrics = summary.get("summary_metrics", {})
    table = Table(title=f"ReconQA Pipeline Complete ({out_dir})")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Status", str(summary.get("status")))
    table.add_row("Endpoints", str(metrics.get("total_endpoints", 0)))
    table.add_row("Coverage %", str(metrics.get("coverage_pct", 0.0)))
    table.add_row("Correlation Chains", str(metrics.get("correlation_chains", 0)))
    table.add_row("Findings", str(metrics.get("findings_total", 0)))
    table.add_row("Artifacts", str(len(summary.get("artifacts", []))))
    console.print(table)


@app.command("serve")
def cmd_serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host."),
    port: int = typer.Option(8000, "--port", help="Bind port."),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload for development."),
) -> None:
    """Start the ReconQA FastAPI + Async Worker server."""
    import uvicorn

    uvicorn.run(
        "reconqa.server.app:app",
        host=host,
        port=port,
        reload=reload,
        timeout_graceful_shutdown=3,
    )


@app.command("mcp")
def cmd_mcp(
    api_url: str | None = typer.Option(
        None,
        "--api-url",
        help="ReconQA FastAPI base URL (defaults to RECONQA_API_URL or http://127.0.0.1:8000).",
    ),
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help="MCP transport: stdio, streamable-http, or sse.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host for HTTP/SSE transports."),
    port: int = typer.Option(8001, "--port", help="Bind port for HTTP/SSE transports."),
) -> None:
    """Start the ReconQA MCP server for AI Agent collaboration (Human-in-the-Loop)."""
    try:
        from reconqa.mcp_server import run_mcp_server
    except ImportError as exc:
        console.print(
            "[red]MCP server dependencies not installed.[/red] "
            "Run [cyan]uv sync --extra mcp[/cyan] and try again."
        )
        raise typer.Exit(code=1) from exc

    run_mcp_server(api_url=api_url, transport=transport, host=host, port=port)


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _print_gate_table(result: Any) -> None:
    status_style = "green" if result.passed else "red"
    status_word = "PASSED" if result.passed else "FAILED"
    table = Table(title=f"ReconQA Quality Gate — {result.target_label} ({status_word})")
    table.add_column("Rule", style="cyan", no_wrap=True)
    table.add_column("Status", style="bold")
    table.add_column("Actual", justify="right")
    table.add_column("Threshold", justify="right")
    table.add_column("Message")

    for rule in result.rules:
        if rule.skipped:
            badge = "[yellow]SKIP[/yellow]"
        elif rule.passed:
            badge = "[green]PASS[/green]"
        else:
            badge = "[red]FAIL[/red]"
        actual_str = "-" if rule.actual is None else str(rule.actual)
        thresh_str = "-" if rule.threshold is None else str(rule.threshold)
        table.add_row(rule.label, badge, actual_str, thresh_str, rule.message)

    console.print(table)
    console.print(f"[{status_style}]{result.summary}[/{status_style}]")

    if result.webhook_results:
        wh_table = Table(title="Webhook Notifications")
        wh_table.add_column("Provider", style="cyan")
        wh_table.add_column("Status", style="bold")
        wh_table.add_column("Target")
        wh_table.add_column("Details")
        for wh in result.webhook_results:
            wh_badge = "[green]SENT[/green]" if wh.sent else "[red]FAILED[/red]"
            detail = (
                f"HTTP {wh.status_code}"
                if wh.sent and wh.status_code is not None
                else (wh.error or f"HTTP {wh.status_code}")
            )
            wh_table.add_row(wh.provider, wh_badge, wh.url_masked, str(detail))
        console.print(wh_table)


def _validate_api_url(url: str) -> tuple[bool, str | None]:
    raw = (url or "").strip()
    if not raw:
        return False, "API URL is empty"
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return False, "API URL contains whitespace or control characters"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return False, "Malformed API URL"
    if (parts.scheme or "").lower() not in {"http", "https"}:
        return False, "API URL must use http:// or https://"
    if not parts.netloc or not parts.hostname:
        return False, "API URL is missing a valid host"
    try:
        port = parts.port
    except ValueError:
        return False, "Malformed API URL port"
    if port is not None and not (1 <= port <= 65535):
        return False, f"API URL port {port} is out of range (1..65535)"
    host = parts.hostname.strip().lower().rstrip(".")
    if host in {"169.254.169.254", "169.254.170.2", "metadata.google.internal", "metadata.goog"}:
        return False, f"Blocked cloud metadata host '{host}'"
    return True, None


@app.command("gate")
def cmd_gate(
    run_dir: Path | None = typer.Option(None, "--run-dir", help="Offline run workspace directory."),
    baseline_dir: Path | None = typer.Option(
        None, "--baseline-dir", help="Optional offline baseline workspace directory."
    ),
    chain_runs_dir: Path | None = typer.Option(
        None, "--chain-runs-dir", help="Optional directory containing chain_runs/*.json checkpoints."
    ),
    project_id: str | None = typer.Option(
        None, "--project-id", help="Project ID for backend DB or API evaluation."
    ),
    api_url: str | None = typer.Option(
        None,
        "--api-url",
        help="ReconQA API base URL (e.g. http://127.0.0.1:8000). If omitted with --project-id, queries local DB directly.",
    ),
    run_id: str | None = typer.Option(
        None, "--run-id", help="Target run ID (or filter for chain checkpoints)."
    ),
    baseline_run_id: str | None = typer.Option(
        None, "--baseline-run-id", help="Baseline run ID for coverage/security diff."
    ),
    batch_id: str | None = typer.Option(
        None, "--batch-id", help="Evaluate chain checkpoints tagged with a specific batch ID."
    ),
    execute_chains: bool = typer.Option(
        False,
        "--execute-chains",
        help="Execute correlation chains in a fresh batch before evaluating the gate (requires --project-id).",
    ),
    credential_id: str | None = typer.Option(
        None, "--credential-id", help="Optional credential ID when --execute-chains is enabled."
    ),
    base_url: str | None = typer.Option(
        None, "--base-url", help="Optional target base URL override when --execute-chains is enabled."
    ),
    config: Path | None = typer.Option(None, "--config", help="Path to reconqa.yaml."),
    max_p0_chain_failures: int | None = typer.Option(
        None,
        "--max-p0-chain-failures",
        min=0,
        help="Maximum allowed P0 chain/assertion failures (default: 0).",
    ),
    max_new_high_security: int | None = typer.Option(
        None,
        "--max-new-high-security",
        min=0,
        help="Maximum allowed new High/Critical security findings (default: 0).",
    ),
    max_coverage_drop: float | None = typer.Option(
        None,
        "--max-coverage-drop",
        min=0.0,
        help="Maximum allowed API coverage drop percentage vs baseline (default: 5.0).",
    ),
    max_p95_ms: float | None = typer.Option(
        None,
        "--max-p95-ms",
        min=0.0,
        help="Maximum allowed P95 latency in milliseconds (disabled if unset).",
    ),
    fail_on_high_without_baseline: bool | None = typer.Option(
        None,
        "--fail-on-high-without-baseline/--no-fail-on-high-without-baseline",
        help="Treat all High/Critical security findings as new when no baseline exists.",
    ),
    feishu_webhook: str | None = typer.Option(
        None, "--feishu-webhook", help="Feishu bot webhook URL."
    ),
    dingtalk_webhook: str | None = typer.Option(
        None, "--dingtalk-webhook", help="DingTalk bot webhook URL."
    ),
    slack_webhook: str | None = typer.Option(
        None, "--slack-webhook", help="Slack incoming webhook URL."
    ),
    webhook_timeout: float | None = typer.Option(
        None,
        "--webhook-timeout",
        min=0.5,
        help="Webhook HTTP timeout in seconds (default: 10.0).",
    ),
    notify_on_pass: bool = typer.Option(
        True,
        "--notify-on-pass/--no-notify-on-pass",
        help="Dispatch configured webhooks even when the quality gate passes.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print pure JSON result to stdout."),
) -> None:
    """Evaluate release Quality Gate rules and optionally send Feishu/DingTalk/Slack alerts."""
    from fastapi import HTTPException

    from reconqa.models.schemas import (
        ChainBatchExecuteRequest,
        QualityGateRequest,
        QualityGateResult,
    )
    from reconqa.modules.hub.quality_gate import (
        GateLookupError,
        dispatch_webhooks,
        evaluate_project_quality_gate_db,
        evaluate_run_dir_gate,
        merge_gate_config,
    )
    from reconqa.server.routers.chains import execute_project_chain_batch

    err_console = Console(stderr=True) if json_output else console

    if (run_dir is None) == (project_id is None):
        err_console.print(
            "[red]Specify exactly one of --run-dir (offline) or --project-id (backend).[/red]"
        )
        raise typer.Exit(code=2)
    if api_url is not None and project_id is None:
        err_console.print("[red]--api-url requires --project-id.[/red]")
        raise typer.Exit(code=2)
    if execute_chains and project_id is None:
        err_console.print("[red]--execute-chains requires --project-id.[/red]")
        raise typer.Exit(code=2)
    if run_dir is not None and not run_dir.is_dir():
        err_console.print(f"[red]Run directory does not exist:[/red] {run_dir}")
        raise typer.Exit(code=2)
    if baseline_dir is not None and not baseline_dir.is_dir():
        err_console.print(f"[red]Baseline directory does not exist:[/red] {baseline_dir}")
        raise typer.Exit(code=2)
    if chain_runs_dir is not None and not chain_runs_dir.is_dir():
        err_console.print(f"[red]Chain runs directory does not exist:[/red] {chain_runs_dir}")
        raise typer.Exit(code=2)
    if config is not None and not config.is_file():
        err_console.print(f"[red]Config file does not exist:[/red] {config}")
        raise typer.Exit(code=2)

    proj_cfg = load_project_config(config)
    req = QualityGateRequest(
        run_id=run_id,
        baseline_run_id=baseline_run_id,
        batch_id=batch_id,
        execute_chains=execute_chains,
        credential_id=credential_id,
        base_url=base_url,
        max_p0_chain_failures=max_p0_chain_failures,
        max_new_high_security=max_new_high_security,
        max_coverage_drop_pct=max_coverage_drop,
        max_p95_ms=max_p95_ms,
        fail_on_high_without_baseline=fail_on_high_without_baseline,
        feishu_webhook=feishu_webhook,
        dingtalk_webhook=dingtalk_webhook,
        slack_webhook=slack_webhook,
        webhook_timeout_sec=webhook_timeout,
        notify_on_pass=notify_on_pass,
    )
    effective_cfg = merge_gate_config(proj_cfg.gate, req)

    if run_dir is not None:
        result = evaluate_run_dir_gate(
            run_dir,
            baseline_dir=baseline_dir,
            chain_runs_dir=chain_runs_dir,
            run_id=run_id,
            batch_id=batch_id,
            gate_config=effective_cfg,
            project_name=proj_cfg.project_name,
        )
        webhooks: dict[str, str | None] = {
            "feishu": effective_cfg.feishu_webhook,
            "dingtalk": effective_cfg.dingtalk_webhook,
            "slack": effective_cfg.slack_webhook,
        }
        if any(v for v in webhooks.values()) and (not result.passed or notify_on_pass):
            result.webhook_results = _run_async(
                dispatch_webhooks(
                    webhooks,
                    result,
                    timeout_sec=effective_cfg.webhook_timeout_sec,
                )
            )
    elif api_url is not None:
        assert project_id is not None
        ok_url, url_err = _validate_api_url(api_url)
        if not ok_url:
            err_console.print(f"[red]Invalid --api-url:[/red] {url_err}")
            raise typer.Exit(code=2)
        clean_api = api_url.strip().rstrip("/")
        api_parts = urlsplit(clean_api)
        host = (api_parts.hostname or "").strip().lower().rstrip(".")
        is_loopback = host in {"127.0.0.1", "localhost", "::1"}
        http_timeout = max(30.0, effective_cfg.webhook_timeout_sec + 10.0)
        api_req = (
            QualityGateRequest(
                run_id=run_id,
                baseline_run_id=baseline_run_id,
                batch_id=batch_id,
                execute_chains=execute_chains,
                credential_id=credential_id,
                base_url=base_url,
                max_p0_chain_failures=effective_cfg.max_p0_chain_failures,
                max_new_high_security=effective_cfg.max_new_high_security,
                max_coverage_drop_pct=effective_cfg.max_coverage_drop_pct,
                max_p95_ms=effective_cfg.max_p95_ms,
                fail_on_high_without_baseline=effective_cfg.fail_on_high_without_baseline,
                feishu_webhook=effective_cfg.feishu_webhook,
                dingtalk_webhook=effective_cfg.dingtalk_webhook,
                slack_webhook=effective_cfg.slack_webhook,
                webhook_timeout_sec=effective_cfg.webhook_timeout_sec,
                notify_on_pass=notify_on_pass,
            )
            if config is not None
            else req
        )
        try:
            with httpx.Client(
                timeout=httpx.Timeout(http_timeout),
                follow_redirects=False,
                trust_env=not is_loopback,
            ) as client:
                resp = client.post(
                    f"{clean_api}/api/v1/projects/{project_id}/gate",
                    json=api_req.model_dump(mode="json", exclude_none=True),
                )
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            err_console.print("[red]Failed to reach ReconQA API.[/red]")
            raise typer.Exit(code=1) from None

        if resp.status_code >= 400:
            err_detail = ""
            try:
                body_err = resp.json()
                if isinstance(body_err, dict) and isinstance(body_err.get("detail"), str):
                    err_detail = f": {body_err['detail'][:200]}"
            except Exception:
                err_detail = ""
            err_console.print(f"[red]API returned HTTP {resp.status_code}{err_detail}[/red]")
            raise typer.Exit(code=1)

        result = QualityGateResult.model_validate(resp.json())
    else:
        assert project_id is not None

        async def _eval_local_db() -> QualityGateResult:
            import reconqa.models.db as db_mod
            from reconqa.models.tables import Run

            try:
                await db_mod.init_db()
                factory = db_mod.get_session_factory()
                async with factory() as db:
                    eff_req = req
                    if eff_req.execute_chains:
                        should_exec = True
                        if eff_req.run_id is not None:
                            cand_run = await db.get(Run, eff_req.run_id)
                            if cand_run is None or cand_run.project_id != project_id:
                                raise GateLookupError(
                                    f"Run '{eff_req.run_id}' not found in project '{project_id}'"
                                )
                            if cand_run.status != "completed":
                                should_exec = False
                        if should_exec:
                            batch_resp = await execute_project_chain_batch(
                                db,
                                project_id,
                                ChainBatchExecuteRequest(
                                    run_id=eff_req.run_id,
                                    base_url=eff_req.base_url,
                                    credential_id=eff_req.credential_id,
                                    sync_scenarios=True,
                                ),
                            )
                            eff_req = eff_req.model_copy(
                                update={
                                    "batch_id": batch_resp.batch_id,
                                    "run_id": eff_req.run_id or batch_resp.run_id,
                                }
                            )
                    gate_res, merged_cfg = await evaluate_project_quality_gate_db(
                        db,
                        project_id,
                        eff_req,
                        project_config=proj_cfg,
                    )
                webhooks_db: dict[str, str | None] = {
                    "feishu": merged_cfg.feishu_webhook,
                    "dingtalk": merged_cfg.dingtalk_webhook,
                    "slack": merged_cfg.slack_webhook,
                }
                if any(v for v in webhooks_db.values()) and (
                    not gate_res.passed or notify_on_pass
                ):
                    gate_res.webhook_results = await dispatch_webhooks(
                        webhooks_db,
                        gate_res,
                        timeout_sec=merged_cfg.webhook_timeout_sec,
                    )
                return gate_res
            finally:
                if db_mod._engine is not None:
                    await db_mod._engine.dispose()
                db_mod._engine = None
                db_mod._session_factory = None

        try:
            result = _run_async(_eval_local_db())
        except (GateLookupError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            err_console.print(f"[red]{detail}[/red]")
            raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _print_gate_table(result)

    if not result.passed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
