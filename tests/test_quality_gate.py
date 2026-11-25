"""Tests for R5 Quality Gate Core Engine, FastAPI Endpoint, CLI, and Webhooks."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import reconqa.models.db as db_mod
from reconqa.cli import app as cli_app
from reconqa.config import (
    GateConfig,
    ProjectConfig,
    Settings,
    load_project_config,
    settings,
    write_default_project_config,
)
from reconqa.models.tables import Finding, Project, Run, ScenarioModel
from reconqa.modules.hub.quality_gate import (
    dispatch_webhooks,
    evaluate_run_dir_gate,
    extract_run_p95_ms,
    format_webhook_payload,
    mask_webhook_url,
    validate_webhook_url,
)
from reconqa.server.app import create_app


def _setup_isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_name: str) -> Path:
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / db_name
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setattr(Settings, "resolve_bin", lambda self, name: None)
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)
    return ws_dir


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _make_chain_checkpoint(
    path: Path,
    *,
    chain_run_id: str,
    chain_name: str,
    status: str = "passed",
    project_id: str = "proj-1",
    run_id: str | None = "run-1",
    step_method: str = "POST",
    step_url: str = "https://api.example.test/api/v1/orders",
) -> None:
    now = datetime.now(UTC).isoformat()
    _write_json(
        path,
        {
            "chain_run_id": chain_run_id,
            "project_id": project_id,
            "run_id": run_id,
            "chain_name": chain_name,
            "status": status,
            "total_steps": 1,
            "completed_steps": 1 if status == "passed" else 0,
            "failed_step_index": None if status == "passed" else 0,
            "step_results": [
                {
                    "step_index": 0,
                    "step": 1,
                    "name": "Step 1",
                    "method": step_method,
                    "url": step_url,
                    "status": "passed" if status == "passed" else "failed",
                    "status_code": 200 if status == "passed" else 500,
                    "assertions": [],
                }
            ],
            "created_at": now,
            "updated_at": now,
        },
    )


def _build_offline_run_dir(
    root: Path,
    *,
    coverage_pct: float = 80.0,
    static_findings: list[dict[str, Any]] | None = None,
    perf_findings: list[dict[str, Any]] | None = None,
    k6_p95_ms: float | None = 120.0,
    har_p95_total_ms: float | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_json(
        root / "hub" / "coverage_diff.json",
        {
            "total_endpoints": 10,
            "covered_count": int(round(coverage_pct / 10)),
            "uncovered_count": 10 - int(round(coverage_pct / 10)),
            "coverage_pct": coverage_pct,
        },
    )
    _write_json(
        root / "security" / "static_audit.json",
        {"findings": static_findings or []},
    )
    if har_p95_total_ms is not None or perf_findings:
        _write_json(
            root / "performance" / "har_perf_report.json",
            {
                "summary": {
                    "total_requests": 10,
                    "p95_ttfb_ms": round((har_p95_total_ms or 100.0) * 0.7, 2),
                    "p95_total_ms": har_p95_total_ms or 100.0,
                },
                "findings": perf_findings or [],
            },
        )
    if k6_p95_ms is not None:
        _write_json(
            root / "performance" / "k6" / "k6_analysis.json",
            {
                "status": "completed",
                "metrics": {
                    "http_reqs_count": 50,
                    "rps": 10.0,
                    "latency_ms": {
                        "avg": 45.0,
                        "med": 40.0,
                        "p90": 90.0,
                        "p95": k6_p95_ms,
                        "max": k6_p95_ms + 30.0,
                    },
                },
            },
        )
    return root


class _RecordingWebhookHandler(BaseHTTPRequestHandler):
    records: list[dict[str, Any]] = []
    status_to_return: int = 200
    response_body: dict[str, Any] = {"ok": True}

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"raw": raw.decode("utf-8", errors="replace")}
        self.__class__.records.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        encoded = json.dumps(self.__class__.response_body).encode("utf-8")
        self.send_response(self.__class__.status_to_return)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _start_local_server(
    handler_cls: type[BaseHTTPRequestHandler],
) -> tuple[ThreadingHTTPServer, int, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


def test_quality_gate_offline_rules_pass_and_fail(tmp_path: Path) -> None:
    """Verify all 4 rules pass and fail independently in offline run-dir evaluation."""
    base_dir = _build_offline_run_dir(
        tmp_path / "baseline",
        coverage_pct=80.0,
        static_findings=[
            {
                "category": "security",
                "severity": "high",
                "endpoint": "GET /api/v1/legacy",
                "title": "Preexisting CORS issue",
            }
        ],
    )
    cur_dir = _build_offline_run_dir(
        tmp_path / "current_ok",
        coverage_pct=76.0,  # drop = 4.0% <= 5.0%
        static_findings=[
            {
                "category": "security",
                "severity": "high",
                "endpoint": "GET /api/v1/legacy",
                "title": "Preexisting CORS issue",
            }
        ],
        perf_findings=[
            {
                "category": "perf_slow_ttfb",
                "severity": "high",
                "endpoint": "GET /api/v1/slow",
                "title": "Slow TTFB should not trigger security rule",
            }
        ],
        k6_p95_ms=220.0,
    )
    _write_json(
        cur_dir / "hub" / "correlation.json",
        {
            "chains": [
                {"name": "p0_checkout_chain", "scenario": "checkout-flow", "steps": []},
                {"name": "p2_profile_chain", "scenario": "profile-view", "steps": []},
            ]
        },
    )
    _write_json(
        cur_dir / "hub" / "scenarios.json",
        [
            {"slug": "checkout-flow", "name": "Checkout", "priority": "P0", "assertions": []},
            {"slug": "profile-view", "name": "Profile", "priority": "P2", "assertions": []},
        ],
    )
    _make_chain_checkpoint(
        cur_dir / "chain_runs" / "c_p0.json",
        chain_run_id="c_p0",
        chain_name="p0_checkout_chain",
        status="passed",
    )
    # P2 chain failure must NOT count as a P0 chain failure
    _make_chain_checkpoint(
        cur_dir / "chain_runs" / "c_p2.json",
        chain_run_id="c_p2",
        chain_name="p2_profile_chain",
        status="failed",
    )

    res_pass = evaluate_run_dir_gate(
        cur_dir,
        baseline_dir=base_dir,
        gate_config=GateConfig(max_p95_ms=250.0),
    )
    assert res_pass.passed is True
    assert res_pass.status == "passed"
    assert res_pass.metrics.p0_chain_failures == 0
    assert res_pass.metrics.new_high_security_count == 0
    assert res_pass.metrics.high_security_count == 1
    assert res_pass.metrics.coverage_drop_pct == 4.0
    assert res_pass.metrics.p95_ms == 220.0
    assert res_pass.metrics.p95_source == "k6"

    # (a) Fail on P0 chain failure
    _make_chain_checkpoint(
        cur_dir / "chain_runs" / "c_p0.json",
        chain_run_id="c_p0",
        chain_name="p0_checkout_chain",
        status="failed",
    )
    res_p0_fail = evaluate_run_dir_gate(cur_dir, baseline_dir=base_dir)
    assert res_p0_fail.passed is False
    assert res_p0_fail.metrics.p0_chain_failures == 1
    assert next(r for r in res_p0_fail.rules if r.rule == "p0_chain_failures").passed is False

    # Restore P0 chain pass, verify (b) new Critical security finding fails
    _make_chain_checkpoint(
        cur_dir / "chain_runs" / "c_p0.json",
        chain_run_id="c_p0",
        chain_name="p0_checkout_chain",
        status="passed",
    )
    _write_json(
        cur_dir / "security" / "static_audit.json",
        {
            "findings": [
                {
                    "category": "security",
                    "severity": "high",
                    "endpoint": "GET /api/v1/legacy",
                    "title": "Preexisting CORS issue",
                },
                {
                    "category": "idor",
                    "severity": "critical",
                    "endpoint": "DELETE /api/v1/orders/{id}",
                    "title": "Cross-tenant order deletion",
                },
            ]
        },
    )
    res_sec_fail = evaluate_run_dir_gate(cur_dir, baseline_dir=base_dir)
    assert res_sec_fail.passed is False
    assert res_sec_fail.metrics.new_high_security_count == 1
    assert next(r for r in res_sec_fail.rules if r.rule == "new_high_security").passed is False

    # No baseline: default fail_on_high_without_baseline=False skips, True fails
    res_no_base_skip = evaluate_run_dir_gate(
        cur_dir,
        baseline_dir=None,
        gate_config=GateConfig(fail_on_high_without_baseline=False),
    )
    assert res_no_base_skip.passed is True
    sec_rule_skip = next(r for r in res_no_base_skip.rules if r.rule == "new_high_security")
    assert sec_rule_skip.skipped is True

    res_no_base_fail = evaluate_run_dir_gate(
        cur_dir,
        baseline_dir=None,
        gate_config=GateConfig(fail_on_high_without_baseline=True),
    )
    assert res_no_base_fail.passed is False
    assert res_no_base_fail.metrics.new_high_security_count == 2

    # (c) Coverage drop > 5% fails
    _write_json(
        cur_dir / "security" / "static_audit.json",
        {"findings": []},
    )
    _write_json(
        cur_dir / "hub" / "coverage_diff.json",
        {"total_endpoints": 10, "covered_count": 7, "coverage_pct": 70.0},
    )
    res_cov_fail = evaluate_run_dir_gate(cur_dir, baseline_dir=base_dir)
    assert res_cov_fail.passed is False
    assert res_cov_fail.metrics.coverage_drop_pct == 10.0
    assert next(r for r in res_cov_fail.rules if r.rule == "coverage_drop_pct").passed is False

    # (d) P95 threshold fail on k6 & fallback to har_perf_report.json
    _write_json(
        cur_dir / "hub" / "coverage_diff.json",
        {"total_endpoints": 10, "covered_count": 8, "coverage_pct": 80.0},
    )
    res_p95_fail = evaluate_run_dir_gate(
        cur_dir,
        baseline_dir=base_dir,
        gate_config=GateConfig(max_p95_ms=200.0),
    )
    assert res_p95_fail.passed is False
    assert next(r for r in res_p95_fail.rules if r.rule == "p95_latency_ms").passed is False

    # Remove k6_analysis.json and verify fallback to har_perf_report.json
    (cur_dir / "performance" / "k6" / "k6_analysis.json").unlink()
    _write_json(
        cur_dir / "performance" / "har_perf_report.json",
        {
            "summary": {
                "total_requests": 10,
                "p95_ttfb_ms": 310.0,
                "p95_total_ms": 480.0,
            },
            "findings": [],
        },
    )
    res_har_p95 = evaluate_run_dir_gate(
        cur_dir,
        baseline_dir=base_dir,
        gate_config=GateConfig(max_p95_ms=400.0),
    )
    assert res_har_p95.passed is False
    assert res_har_p95.metrics.p95_source == "har"
    assert res_har_p95.metrics.p95_ms == 480.0


def test_cli_gate_validation_and_config_override(tmp_path: Path) -> None:
    """Verify CLI argument validation (exit 2) and reconqa.yaml GateConfig override precedence."""
    runner = CliRunner()

    # Round-trip default config includes gate:
    default_cfg_path = tmp_path / "default_reconqa.yaml"
    write_default_project_config(default_cfg_path)
    loaded_default = load_project_config(default_cfg_path)
    assert isinstance(loaded_default, ProjectConfig)
    assert loaded_default.gate.max_coverage_drop_pct == 5.0

    # Missing both --run-dir and --project-id -> exit 2
    r_none = runner.invoke(cli_app, ["gate"])
    assert r_none.exit_code == 2

    # Both --run-dir and --project-id -> exit 2
    r_both = runner.invoke(
        cli_app,
        ["gate", "--run-dir", str(tmp_path), "--project-id", "proj-1"],
    )
    assert r_both.exit_code == 2

    # --api-url without --project-id -> exit 2
    r_api_only = runner.invoke(
        cli_app,
        ["gate", "--run-dir", str(tmp_path), "--api-url", "http://127.0.0.1:8000"],
    )
    assert r_api_only.exit_code == 2

    # Missing --run-dir directory -> exit 2
    r_missing = runner.invoke(
        cli_app,
        ["gate", "--run-dir", str(tmp_path / "does_not_exist")],
    )
    assert r_missing.exit_code == 2

    # reconqa.yaml overrides + CLI flag overrides
    base_dir = _build_offline_run_dir(tmp_path / "base", coverage_pct=80.0, k6_p95_ms=100.0)
    cur_dir = _build_offline_run_dir(tmp_path / "cur", coverage_pct=72.0, k6_p95_ms=350.0)

    cfg_file = tmp_path / "custom.yaml"
    cfg_file.write_text(
        """project_name: gated-shop
gate:
  max_coverage_drop_pct: 10.0
  max_p95_ms: 300.0
""",
        encoding="utf-8",
    )

    # With config alone: P95 350 > 300 fails (exit 1)
    r_cfg_fail = runner.invoke(
        cli_app,
        [
            "gate",
            "--run-dir",
            str(cur_dir),
            "--baseline-dir",
            str(base_dir),
            "--config",
            str(cfg_file),
            "--json",
        ],
    )
    assert r_cfg_fail.exit_code == 1
    payload_fail = json.loads(r_cfg_fail.stdout)
    assert payload_fail["passed"] is False
    assert payload_fail["project_name"] == "gated-shop"

    # CLI --max-p95-ms 400 overrides config max_p95_ms=300 -> passes (exit 0)
    r_override_pass = runner.invoke(
        cli_app,
        [
            "gate",
            "--run-dir",
            str(cur_dir),
            "--baseline-dir",
            str(base_dir),
            "--config",
            str(cfg_file),
            "--max-p95-ms",
            "400",
            "--json",
        ],
    )
    assert r_override_pass.exit_code == 0, r_override_pass.output
    payload_pass = json.loads(r_override_pass.stdout)
    assert payload_pass["passed"] is True


def test_cli_gate_offline_rich_and_json_output(tmp_path: Path) -> None:
    """Verify Rich table output and pure JSON output on both pass (0) and fail (1)."""
    runner = CliRunner()
    base_dir = _build_offline_run_dir(tmp_path / "base", coverage_pct=85.0, k6_p95_ms=120.0)
    pass_dir = _build_offline_run_dir(tmp_path / "pass", coverage_pct=84.0, k6_p95_ms=150.0)
    fail_dir = _build_offline_run_dir(tmp_path / "fail", coverage_pct=70.0, k6_p95_ms=150.0)

    r_rich_pass = runner.invoke(
        cli_app,
        ["gate", "--run-dir", str(pass_dir), "--baseline-dir", str(base_dir)],
    )
    assert r_rich_pass.exit_code == 0, r_rich_pass.output
    assert "PASSED" in r_rich_pass.output
    assert "P0 Chain" in r_rich_pass.output

    r_rich_fail = runner.invoke(
        cli_app,
        ["gate", "--run-dir", str(fail_dir), "--baseline-dir", str(base_dir)],
    )
    assert r_rich_fail.exit_code == 1
    assert "FAILED" in r_rich_fail.output

    r_json_pass = runner.invoke(
        cli_app,
        ["gate", "--run-dir", str(pass_dir), "--baseline-dir", str(base_dir), "--json"],
    )
    assert r_json_pass.exit_code == 0
    assert "\x1b[" not in r_json_pass.stdout
    data_pass = json.loads(r_json_pass.stdout)
    assert data_pass["passed"] is True
    assert len(data_pass["rules"]) == 4

    r_json_fail = runner.invoke(
        cli_app,
        ["gate", "--run-dir", str(fail_dir), "--baseline-dir", str(base_dir), "--json"],
    )
    assert r_json_fail.exit_code == 1
    assert "\x1b[" not in r_json_fail.stdout
    data_fail = json.loads(r_json_fail.stdout)
    assert data_fail["passed"] is False


@pytest.mark.asyncio
async def test_cli_gate_project_id_local_db_and_api_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify `reconqa gate --project-id` in local DB mode and `--api-url` HTTP mode."""
    ws_dir = _setup_isolated_env(tmp_path, monkeypatch, "cli_gate.db")
    await db_mod.init_db()
    factory = db_mod.get_session_factory()

    t0 = datetime.now(UTC) - timedelta(hours=1)
    t1 = datetime.now(UTC)
    run_base_ws = ws_dir / "run_base"
    run_cur_ws = ws_dir / "run_cur"
    _build_offline_run_dir(run_base_ws, coverage_pct=82.0, k6_p95_ms=100.0)
    _build_offline_run_dir(run_cur_ws, coverage_pct=80.0, k6_p95_ms=180.0)

    async with factory() as db:
        proj = Project(id="proj-cli-1", name="CLI Gate Shop", slug="cli-gate-shop")
        db.add(proj)
        db.add(
            Run(
                id="run-base",
                project_id="proj-cli-1",
                status="completed",
                workspace_dir=str(run_base_ws),
                summary_metrics={"coverage_pct": 82.0},
                started_at=t0,
                finished_at=t0,
            )
        )
        db.add(
            Run(
                id="run-cur",
                project_id="proj-cli-1",
                status="completed",
                workspace_dir=str(run_cur_ws),
                summary_metrics={"coverage_pct": 80.0},
                started_at=t1,
                finished_at=t1,
            )
        )
        db.add(
            ScenarioModel(
                id="sc-p0",
                project_id="proj-cli-1",
                slug="checkout-p0",
                name="Checkout P0",
                priority="P0",
                status="active",
                assertions_json=json.dumps(
                    [
                        {
                            "id": "a1",
                            "title": "Order created",
                            "target_endpoint": "POST /api/v1/orders",
                            "operator": "status_code",
                            "expected": "200",
                            "status": "passed",
                        }
                    ]
                ),
            )
        )
        await db.commit()

    # Dispose before CLI runs so CLI manages its own loop & engine cleanly
    if db_mod._engine is not None:
        await db_mod._engine.dispose()
    db_mod._engine = None
    db_mod._session_factory = None

    runner = CliRunner()

    # Local DB pass (exit 0)
    r_db_pass = runner.invoke(
        cli_app,
        ["gate", "--project-id", "proj-cli-1", "--max-p95-ms", "200", "--json"],
    )
    assert r_db_pass.exit_code == 0, r_db_pass.output
    res_pass = json.loads(r_db_pass.stdout)
    assert res_pass["passed"] is True
    assert res_pass["run_id"] == "run-cur"
    assert res_pass["baseline_run_id"] == "run-base"

    # Local DB fail when max_p95_ms is tight (exit 1)
    r_db_fail = runner.invoke(
        cli_app,
        ["gate", "--project-id", "proj-cli-1", "--max-p95-ms", "150", "--json"],
    )
    assert r_db_fail.exit_code == 1
    res_fail = json.loads(r_db_fail.stdout)
    assert res_fail["passed"] is False

    # Missing project in local DB -> exit 1
    r_db_missing = runner.invoke(
        cli_app,
        ["gate", "--project-id", "nonexistent-proj"],
    )
    assert r_db_missing.exit_code == 1

    # Online --api-url mode against local ThreadingHTTPServer
    class _ApiGateHandler(BaseHTTPRequestHandler):
        captured: list[dict[str, Any]] = []

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            raw = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            self.__class__.captured.append({"path": self.path, "body": raw})
            max_p95 = raw.get("max_p95_ms")
            passed = max_p95 is None or float(max_p95) >= 180.0
            resp_payload = {
                "project_id": "proj-cli-1",
                "project_name": "CLI Gate Shop",
                "target_label": "CLI Gate Shop",
                "run_id": "run-cur",
                "baseline_run_id": "run-base",
                "passed": passed,
                "status": "passed" if passed else "failed",
                "evaluated_at": datetime.now(UTC).isoformat(),
                "rules": [],
                "metrics": {"p95_ms": 180.0, "p95_source": "k6"},
                "summary": "PASSED" if passed else "FAILED",
                "webhook_results": [],
            }
            encoded = json.dumps(resp_payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server, port, _ = _start_local_server(_ApiGateHandler)
    try:
        base_url = f"http://127.0.0.1:{port}"
        r_api_pass = runner.invoke(
            cli_app,
            [
                "gate",
                "--project-id",
                "proj-cli-1",
                "--api-url",
                base_url,
                "--max-p95-ms",
                "250",
                "--json",
            ],
        )
        assert r_api_pass.exit_code == 0, r_api_pass.output
        assert json.loads(r_api_pass.stdout)["passed"] is True

        r_api_fail = runner.invoke(
            cli_app,
            [
                "gate",
                "--project-id",
                "proj-cli-1",
                "--api-url",
                base_url,
                "--max-p95-ms",
                "100",
                "--json",
            ],
        )
        assert r_api_fail.exit_code == 1
        assert json.loads(r_api_fail.stdout)["passed"] is False
        assert _ApiGateHandler.captured[0]["path"] == "/api/v1/projects/proj-cli-1/gate"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_api_post_project_gate_pass_fail_and_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify POST /api/v1/projects/{project_id}/gate 200 pass/fail, baseline resolution, and 404s."""
    ws_dir = _setup_isolated_env(tmp_path, monkeypatch, "api_gate.db")
    fastapi_app = create_app()

    async with fastapi_app.router.lifespan_context(fastapi_app):
        factory = db_mod.get_session_factory()
        t0 = datetime.now(UTC) - timedelta(hours=2)
        t1 = datetime.now(UTC) - timedelta(hours=1)
        t2 = datetime.now(UTC)

        ws_base = ws_dir / "ws_base"
        ws_good = ws_dir / "ws_good"
        ws_bad = ws_dir / "ws_bad"
        _build_offline_run_dir(ws_base, coverage_pct=85.0, k6_p95_ms=110.0)
        _build_offline_run_dir(ws_good, coverage_pct=82.0, k6_p95_ms=150.0)
        _build_offline_run_dir(ws_bad, coverage_pct=70.0, k6_p95_ms=650.0)

        async with factory() as db:
            db.add(Project(id="p1", name="Main Project", slug="main-project"))
            db.add(Project(id="p2", name="Other Project", slug="other-project"))
            db.add(
                Run(
                    id="r-base",
                    project_id="p1",
                    status="completed",
                    workspace_dir=str(ws_base),
                    summary_metrics={"coverage_pct": 85.0},
                    started_at=t0,
                    finished_at=t0,
                )
            )
            db.add(
                Run(
                    id="r-good",
                    project_id="p1",
                    status="completed",
                    workspace_dir=str(ws_good),
                    summary_metrics={"coverage_pct": 82.0},
                    started_at=t1,
                    finished_at=t1,
                )
            )
            db.add(
                Run(
                    id="r-bad",
                    project_id="p1",
                    status="completed",
                    workspace_dir=str(ws_bad),
                    summary_metrics={"coverage_pct": 70.0},
                    started_at=t2,
                    finished_at=t2,
                )
            )
            db.add(
                Run(
                    id="r-other",
                    project_id="p2",
                    status="completed",
                    workspace_dir=str(ws_good),
                    summary_metrics={"coverage_pct": 80.0},
                    started_at=t1,
                    finished_at=t1,
                )
            )
            db.add(
                Finding(
                    id="f-base-1",
                    run_id="r-base",
                    category="auth_bypass",
                    severity="high",
                    endpoint="GET /api/v1/admin",
                    title="Admin endpoint lacks auth",
                )
            )
            db.add(
                Finding(
                    id="f-good-1",
                    run_id="r-good",
                    category="auth_bypass",
                    severity="high",
                    endpoint="GET /api/v1/admin",
                    title="Admin endpoint lacks auth",
                )
            )
            db.add(
                Finding(
                    id="f-bad-1",
                    run_id="r-bad",
                    category="idor",
                    severity="critical",
                    endpoint="GET /api/v1/orders/123",
                    title="Confirmed IDOR on order lookup",
                )
            )
            db.add(
                ScenarioModel(
                    id="sc-p0-1",
                    project_id="p1",
                    slug="order-create",
                    name="Order Create",
                    priority="P0",
                    status="active",
                    assertions_json=json.dumps(
                        [
                            {
                                "id": "assert-1",
                                "title": "Code == 0",
                                "target_endpoint": "POST /api/v1/orders",
                                "operator": "equals",
                                "expected": "0",
                                "status": "passed",
                            }
                        ]
                    ),
                )
            )
            await db.commit()

        # Passed P0 checkpoint for r-good, failed P0 checkpoint for r-bad
        _make_chain_checkpoint(
            ws_dir / "chain_runs" / "cp_good.json",
            chain_run_id="cp_good",
            chain_name="order-create",
            status="passed",
            project_id="p1",
            run_id="r-good",
        )
        _make_chain_checkpoint(
            ws_dir / "chain_runs" / "cp_bad.json",
            chain_run_id="cp_bad",
            chain_name="order-create",
            status="failed",
            project_id="p1",
            run_id="r-bad",
        )

        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Pass on r-good compared against explicit r-base
            r_pass = await client.post(
                "/api/v1/projects/p1/gate",
                json={
                    "run_id": "r-good",
                    "baseline_run_id": "r-base",
                    "max_p95_ms": 200.0,
                },
            )
            assert r_pass.status_code == 200, r_pass.text
            d_pass = r_pass.json()
            assert d_pass["passed"] is True
            assert d_pass["status"] == "passed"
            assert d_pass["metrics"]["new_high_security_count"] == 0
            assert d_pass["metrics"]["coverage_drop_pct"] == 3.0
            assert d_pass["metrics"]["p95_ms"] == 150.0

            # 2. Fail on latest r-bad (auto-selected) vs auto-selected prior r-good
            r_fail = await client.post(
                "/api/v1/projects/p1/gate",
                json={"max_p95_ms": 200.0},
            )
            assert r_fail.status_code == 200, r_fail.text
            d_fail = r_fail.json()
            assert d_fail["passed"] is False
            assert d_fail["run_id"] == "r-bad"
            assert d_fail["baseline_run_id"] == "r-good"
            assert d_fail["metrics"]["p0_chain_failures"] == 1
            assert d_fail["metrics"]["new_high_security_count"] == 1
            assert d_fail["metrics"]["coverage_drop_pct"] == 12.0
            assert d_fail["metrics"]["p95_ms"] == 650.0
            assert all(r["passed"] is False for r in d_fail["rules"])

            # 3. 404 cases: missing project, missing run_id, cross-project run_id, missing baseline
            assert (
                await client.post("/api/v1/projects/missing-proj/gate", json={})
            ).status_code == 404
            assert (
                await client.post("/api/v1/projects/p1/gate", json={"run_id": "missing-run"})
            ).status_code == 404
            assert (
                await client.post("/api/v1/projects/p1/gate", json={"run_id": "r-other"})
            ).status_code == 404
            assert (
                await client.post(
                    "/api/v1/projects/p1/gate",
                    json={"run_id": "r-good", "baseline_run_id": "r-other"},
                )
            ).status_code == 404

        if db_mod._engine is not None:
            await db_mod._engine.dispose()
        db_mod._engine = None
        db_mod._session_factory = None


@pytest.mark.asyncio
async def test_webhook_payload_formats_and_local_http_delivery(tmp_path: Path) -> None:
    """Verify Feishu/DingTalk/Slack payload structures, secret masking, and local HTTP delivery."""
    run_dir = _build_offline_run_dir(tmp_path / "run_wh", coverage_pct=88.0, k6_p95_ms=110.0)
    gate_res = evaluate_run_dir_gate(
        run_dir,
        gate_config=GateConfig(max_p95_ms=200.0),
        project_name="Webhook Shop",
    )

    feishu_payload = format_webhook_payload("feishu", gate_res)
    assert feishu_payload["msg_type"] == "interactive"
    assert feishu_payload["card"]["header"]["template"] == "green"
    assert feishu_payload["card"]["elements"][0]["tag"] == "markdown"

    ding_payload = format_webhook_payload("dingtalk", gate_res)
    assert ding_payload["msgtype"] == "markdown"
    assert "[PASS]" in ding_payload["markdown"]["title"]
    assert "Webhook Shop" in ding_payload["markdown"]["text"]

    slack_payload = format_webhook_payload("slack", gate_res)
    assert "Webhook Shop" in slack_payload["text"]
    assert slack_payload["blocks"][0]["type"] == "header"
    assert len(slack_payload["blocks"][0]["text"]["text"]) <= 150
    assert slack_payload["blocks"][1]["type"] == "section"
    assert slack_payload["blocks"][1]["text"]["type"] == "mrkdwn"

    # Secret masking checks
    secret_feishu = "https://user:pass@open.feishu.cn/open-apis/bot/v2/hook/sec-token-12345"
    secret_ding = "https://oapi.dingtalk.com/robot/send?access_token=ding-secret-999&sign=hmac-abc"
    secret_slack = "https://hooks.slack.com/services/T00/B00/slack-secret-xyz"
    for raw, secret in (
        (secret_feishu, "sec-token-12345"),
        (secret_ding, "ding-secret-999"),
        (secret_slack, "slack-secret-xyz"),
    ):
        masked = mask_webhook_url(raw)
        assert secret not in masked
        assert "user:pass" not in masked
        assert "hmac-abc" not in masked

    # Real local ThreadingHTTPServer webhook delivery via CLI & dispatch_webhooks
    _RecordingWebhookHandler.records = []
    _RecordingWebhookHandler.status_to_return = 200
    server, port, _ = _start_local_server(_RecordingWebhookHandler)
    try:
        feishu_url = f"http://127.0.0.1:{port}/open-apis/bot/v2/hook/topsecret-feishu"
        ding_url = f"http://127.0.0.1:{port}/robot/send?access_token=topsecret-ding"
        slack_url = f"http://127.0.0.1:{port}/services/T00/B00/topsecret-slack"

        runner = CliRunner()
        res = runner.invoke(
            cli_app,
            [
                "gate",
                "--run-dir",
                str(run_dir),
                "--max-p95-ms",
                "200",
                "--feishu-webhook",
                feishu_url,
                "--dingtalk-webhook",
                ding_url,
                "--slack-webhook",
                slack_url,
                "--json",
            ],
        )
        assert res.exit_code == 0, res.output
        out_json = json.loads(res.stdout)
        assert len(out_json["webhook_results"]) == 3
        assert all(w["sent"] is True and w["status_code"] == 200 for w in out_json["webhook_results"])

        raw_stdout = res.stdout
        assert "topsecret-feishu" not in raw_stdout
        assert "topsecret-ding" not in raw_stdout
        assert "topsecret-slack" not in raw_stdout

        assert len(_RecordingWebhookHandler.records) == 3
        by_path = {rec["path"]: rec["body"] for rec in _RecordingWebhookHandler.records}
        assert by_path["/open-apis/bot/v2/hook/topsecret-feishu"]["msg_type"] == "interactive"
        assert by_path["/robot/send?access_token=topsecret-ding"]["msgtype"] == "markdown"
        assert "blocks" in by_path["/services/T00/B00/topsecret-slack"]

        # Verify httpx.MockTransport path as well
        seen_mock: list[str] = []

        def _mock_handler(request: httpx.Request) -> httpx.Response:
            seen_mock.append(str(request.url))
            return httpx.Response(200, json={"ok": True})

        deliveries = await dispatch_webhooks(
            {"feishu": "https://hooks.example.test/bot/secret-abc"},
            gate_res,
            transport=httpx.MockTransport(_mock_handler),
        )
        assert len(deliveries) == 1
        assert deliveries[0].sent is True
        assert "secret-abc" not in deliveries[0].url_masked
        assert seen_mock == ["https://hooks.example.test/bot/secret-abc"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_webhook_validation_ssrf_and_failure_resilience(tmp_path: Path) -> None:
    """Verify SSRF/scheme URL validation and graceful failure on HTTP 500 / 302 / network errors."""
    for bad_url in (
        "",
        "not-a-url",
        "file:///etc/passwd",
        "ftp://hooks.example.test/x",
        "https://hooks.example.test/a b",
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.170.2/v2/credentials",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
    ):
        ok, err = validate_webhook_url(bad_url)
        assert ok is False, f"Expected rejection for {bad_url}"
        assert err

    for good_url in (
        "http://127.0.0.1:8080/hook",
        "http://localhost:9000/hook",
        "https://open.feishu.cn/open-apis/bot/v2/hook/token",
    ):
        ok, err = validate_webhook_url(good_url)
        assert ok is True, f"Unexpected rejection for {good_url}: {err}"

    run_dir = _build_offline_run_dir(tmp_path / "run_res", coverage_pct=90.0, k6_p95_ms=80.0)
    gate_res = evaluate_run_dir_gate(run_dir)
    assert gate_res.passed is True

    # Invalid URL + HTTP 500 + HTTP 302 redirect blocked + ConnectError never crash or flip pass
    res_invalid = await dispatch_webhooks(
        {
            "feishu": "file:///etc/passwd",
            "slack": "http://169.254.169.254/latest/meta-data/",
        },
        gate_res,
    )
    assert len(res_invalid) == 2
    assert all(r.sent is False and r.error for r in res_invalid)

    res_500 = await dispatch_webhooks(
        {"dingtalk": "https://hooks.example.test/robot/send?access_token=abc"},
        gate_res,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(500, text="Internal Server Error")
        ),
    )
    assert res_500[0].sent is False
    assert res_500[0].status_code == 500

    res_302 = await dispatch_webhooks(
        {"slack": "https://hooks.example.test/services/T/B/sec"},
        gate_res,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        ),
    )
    assert res_302[0].sent is False
    assert res_302[0].status_code == 302

    def _raise_timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=req)

    res_timeout = await dispatch_webhooks(
        {"feishu": "https://hooks.example.test/open-apis/bot/v2/hook/sec"},
        gate_res,
        transport=httpx.MockTransport(_raise_timeout),
    )
    assert res_timeout[0].sent is False
    assert res_timeout[0].status_code is None
    assert "timed out" in (res_timeout[0].error or "")


def test_quality_gate_edge_cases_and_notify_on_pass_flag(tmp_path: Path) -> None:
    """Verify unmapped P0 chains, no-double-count, openapi fallback, k6 raw p(95), and --no-notify-on-pass."""
    # 1. OpenAPI fallback + raw k6 http_req_duration.values["p(95)"] + unmapped failed chain
    run_edge = tmp_path / "edge_run"
    _write_json(
        run_edge / "hub" / "openapi.json",
        {
            "openapi": "3.0.3",
            "paths": {
                "/api/a": {"get": {"x-discovery-source": "har-live"}},
                "/api/b": {"post": {"x-discovery-source": "js-static"}},
            },
        },
    )
    _write_json(
        run_edge / "performance" / "k6" / "k6_analysis.json",
        {
            "status": "completed",
            "metrics": {"http_req_duration": {"values": {"p(95)": 320.5}}},
        },
    )
    _write_json(
        run_edge / "hub" / "scenarios.json",
        [
            {
                "slug": "checkout-p0",
                "name": "Checkout P0",
                "priority": "P0",
                "assertions": [
                    {
                        "id": "a1",
                        "title": "Status 200",
                        "target_endpoint": "POST /api/v1/orders",
                        "operator": "status_code",
                        "expected": "200",
                        "status": "failed",
                    }
                ],
            }
        ],
    )
    # Linked failed P0 chain matching checkout-p0 -> should count 1 (not 2!)
    _make_chain_checkpoint(
        run_edge / "chain_runs" / "cp_linked.json",
        chain_run_id="cp_linked",
        chain_name="checkout-p0",
        status="failed",
    )
    res_linked = evaluate_run_dir_gate(
        run_edge,
        gate_config=GateConfig(max_p95_ms=300.0),
    )
    assert res_linked.passed is False
    assert res_linked.metrics.current_coverage_pct == 50.0
    assert res_linked.metrics.p95_ms == 320.5
    assert res_linked.metrics.p95_source == "k6"
    assert res_linked.metrics.p0_failed_chains == 1
    assert res_linked.metrics.p0_failed_assertions == 1
    assert res_linked.metrics.p0_chain_failures == 1

    # Unmapped failed chain (no matching scenario) is conservatively counted as P0 failure
    (run_edge / "hub" / "scenarios.json").unlink()
    res_unmapped = evaluate_run_dir_gate(run_edge)
    assert res_unmapped.passed is False
    assert res_unmapped.metrics.p0_chain_failures == 1

    # 2. --no-notify-on-pass skips webhook on pass, still sends on fail
    pass_dir = _build_offline_run_dir(tmp_path / "pass_wh", coverage_pct=90.0, k6_p95_ms=90.0)
    _RecordingWebhookHandler.records = []
    _RecordingWebhookHandler.status_to_return = 200
    server, port, _ = _start_local_server(_RecordingWebhookHandler)
    try:
        wh_url = f"http://127.0.0.1:{port}/services/T/B/sec-skip"
        runner = CliRunner()
        r_skip = runner.invoke(
            cli_app,
            [
                "gate",
                "--run-dir",
                str(pass_dir),
                "--slack-webhook",
                wh_url,
                "--no-notify-on-pass",
                "--json",
            ],
        )
        assert r_skip.exit_code == 0, r_skip.output
        assert json.loads(r_skip.stdout)["webhook_results"] == []
        assert len(_RecordingWebhookHandler.records) == 0

        r_fail_send = runner.invoke(
            cli_app,
            [
                "gate",
                "--run-dir",
                str(pass_dir),
                "--max-p95-ms",
                "50",
                "--slack-webhook",
                wh_url,
                "--no-notify-on-pass",
                "--json",
            ],
        )
        assert r_fail_send.exit_code == 1
        assert len(_RecordingWebhookHandler.records) == 1
        assert "[FAIL]" in _RecordingWebhookHandler.records[0]["body"]["text"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_regression_malformed_port_ssrf_and_short_token_masking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression tests for bugs #1, #2, and #6b: malformed ports, non-canonical IPv4 SSRF, and short token masking."""
    # 1. Malformed / out-of-range ports rejected cleanly
    for bad_port_url in (
        "https://hooks.example.com:abc/hook",
        "https://hooks.example.com:0/hook",
        "https://hooks.example.com:65536/hook",
    ):
        ok, err = validate_webhook_url(bad_port_url)
        assert ok is False, f"Expected port rejection for {bad_port_url}"
        assert err

    # 2. Non-canonical IPv4 SSRF literals rejected cleanly (zero DNS)
    for non_canonical_url in (
        "http://2852039166/latest/meta-data/",
        "http://0xa9fea9fe/latest/meta-data/",
        "http://0251.0376.0251.0376/latest/meta-data/",
        "http://169.254.1/latest/meta-data/",
        "http://0x7f000001/hook",
        "http://0177.0.0.1/hook",
    ):
        ok, err = validate_webhook_url(non_canonical_url)
        assert ok is False, f"Expected SSRF rejection for {non_canonical_url}"
        assert err

    # Canonical IPv6 loopback still allowed
    ok_v6, err_v6 = validate_webhook_url("http://[::1]:8080/hook")
    assert ok_v6 is True, err_v6

    # 6b. Short single-segment webhook path tokens are redacted
    masked_short = mask_webhook_url("https://hooks.example.com/tok12345")
    assert "tok12345" not in masked_short
    assert masked_short == "https://hooks.example.com/***"
    masked_bad_port = mask_webhook_url("https://hooks.example.com:abc/secret-token")
    assert "secret-token" not in masked_bad_port

    # 1b. dispatch_webhooks, CLI --json, CLI --api-url, and POST /gate never crash on malformed port
    run_dir = _build_offline_run_dir(tmp_path / "run_port", coverage_pct=90.0, k6_p95_ms=80.0)
    gate_res = evaluate_run_dir_gate(run_dir)
    deliveries = await dispatch_webhooks(
        {"slack": "https://hooks.example.com:abc/secret-hook"},
        gate_res,
    )
    assert len(deliveries) == 1
    assert deliveries[0].sent is False
    assert deliveries[0].error
    assert "secret-hook" not in deliveries[0].url_masked

    # Transport raising InvalidURL/ValueError is also caught cleanly
    def _raise_invalid_url(req: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("Invalid port: 'abc'")

    deliveries_tx = await dispatch_webhooks(
        {"slack": "https://hooks.example.com/services/T/B/sec"},
        gate_res,
        transport=httpx.MockTransport(_raise_invalid_url),
    )
    assert len(deliveries_tx) == 1
    assert deliveries_tx[0].sent is False
    assert "Invalid port" in (deliveries_tx[0].error or "")

    runner = CliRunner()
    r_cli_bad_wh = runner.invoke(
        cli_app,
        [
            "gate",
            "--run-dir",
            str(run_dir),
            "--slack-webhook",
            "https://hooks.example.com:abc/hook",
            "--json",
        ],
    )
    assert r_cli_bad_wh.exit_code == 0, r_cli_bad_wh.output
    cli_payload = json.loads(r_cli_bad_wh.stdout)
    assert cli_payload["passed"] is True
    assert cli_payload["webhook_results"][0]["sent"] is False

    r_cli_bad_api = runner.invoke(
        cli_app,
        [
            "gate",
            "--project-id",
            "proj-1",
            "--api-url",
            "http://127.0.0.1:abc",
        ],
    )
    assert r_cli_bad_api.exit_code == 2

    # POST /gate returns 200 with sent=False (never 500) on malformed webhook port
    _setup_isolated_env(tmp_path, monkeypatch, "port_api_gate.db")
    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        factory = db_mod.get_session_factory()
        async with factory() as db:
            db.add(Project(id="p-port", name="Port Check", slug="port-check"))
            await db.commit()

        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/v1/projects/p-port/gate",
                json={"slack_webhook": "https://hooks.example.com:abc/hook"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["webhook_results"][0]["sent"] is False
            assert body["webhook_results"][0]["error"]

        if db_mod._engine is not None:
            await db_mod._engine.dispose()
        db_mod._engine = None
        db_mod._session_factory = None


def test_regression_graphql_p0_hints_and_k6_skipped_fallback(tmp_path: Path) -> None:
    """Regression tests for bugs #3, #4, and #5: GraphQL fragment matching, priority_hints, and skipped k6."""
    # 3a. P1 GraphQL scenario targeting POST /graphql#GetOrderList with stripped chain step URL
    run_gql_p1 = tmp_path / "gql_p1"
    _build_offline_run_dir(run_gql_p1, coverage_pct=85.0, k6_p95_ms=100.0)
    _write_json(
        run_gql_p1 / "hub" / "scenarios.json",
        [
            {
                "slug": "gql-order-list",
                "name": "GQL Order List",
                "priority": "P1",
                "assertions": [
                    {
                        "id": "a1",
                        "title": "Status 200",
                        "target_endpoint": "POST /graphql#GetOrderList",
                        "operator": "status_code",
                        "expected": "200",
                        "status": "failed",
                    }
                ],
            }
        ],
    )
    _make_chain_checkpoint(
        run_gql_p1 / "chain_runs" / "cp_gql_p1.json",
        chain_run_id="cp_gql_p1",
        chain_name="gql_exec_chain",
        status="failed",
        step_method="POST",
        step_url="http://127.0.0.1:8080/graphql",
    )
    res_gql_p1 = evaluate_run_dir_gate(run_gql_p1)
    assert res_gql_p1.passed is True
    assert res_gql_p1.metrics.p0_chain_failures == 0

    # 3b. Different #fragment in step.url must NOT match POST /graphql#GetOrderList
    _make_chain_checkpoint(
        run_gql_p1 / "chain_runs" / "cp_gql_p1.json",
        chain_run_id="cp_gql_p1",
        chain_name="gql_other_chain",
        status="failed",
        step_method="POST",
        step_url="http://127.0.0.1:8080/graphql#OtherOp",
    )
    res_gql_diff_frag = evaluate_run_dir_gate(run_gql_p1)
    assert res_gql_diff_frag.metrics.p0_chain_failures == 1  # unmapped conservatively

    # 3c. P0 GraphQL scenario with stripped step.url avoids double-counting (1, not 2)
    run_gql_p0 = tmp_path / "gql_p0"
    _build_offline_run_dir(run_gql_p0, coverage_pct=85.0, k6_p95_ms=100.0)
    _write_json(
        run_gql_p0 / "hub" / "scenarios.json",
        [
            {
                "slug": "gql-delete-order",
                "name": "GQL Delete Order",
                "priority": "P0",
                "assertions": [
                    {
                        "id": "a1",
                        "title": "Status 200",
                        "target_endpoint": "POST /graphql#DeleteOrder",
                        "operator": "status_code",
                        "expected": "200",
                        "status": "failed",
                    }
                ],
            }
        ],
    )
    _make_chain_checkpoint(
        run_gql_p0 / "chain_runs" / "cp_gql_p0.json",
        chain_run_id="cp_gql_p0",
        chain_name="gql_p0_exec",
        status="failed",
        step_method="POST",
        step_url="http://127.0.0.1:8080/graphql",
    )
    res_gql_p0 = evaluate_run_dir_gate(run_gql_p0)
    assert res_gql_p0.passed is False
    assert res_gql_p0.metrics.p0_failed_chains == 1
    assert res_gql_p0.metrics.p0_chain_failures == 1

    # 5. priority_hints == "P0" with alias chain_name + step endpoint avoids double-counting
    run_hint = tmp_path / "hint_p0"
    _build_offline_run_dir(run_hint, coverage_pct=85.0, k6_p95_ms=100.0)
    _write_json(
        run_hint / "hub" / "scenarios.json",
        [
            {
                "slug": "p0-login",
                "name": "P0 Login",
                "priority": "P0",
                "assertions": [
                    {
                        "id": "a1",
                        "title": "Login 200",
                        "target_endpoint": "POST /api/login",
                        "operator": "status_code",
                        "expected": "200",
                        "status": "failed",
                    }
                ],
            }
        ],
    )
    now = datetime.now(UTC).isoformat()
    _write_json(
        run_hint / "chain_runs" / "cp_hint_p0.json",
        {
            "chain_run_id": "cp_hint_p0",
            "project_id": "proj-1",
            "run_id": "run-1",
            "chain_name": "alias_not_slug",
            "priority": "P0",
            "status": "failed",
            "total_steps": 1,
            "completed_steps": 0,
            "failed_step_index": 0,
            "step_results": [
                {
                    "step_index": 0,
                    "step": 1,
                    "name": "Login",
                    "method": "POST",
                    "url": "https://api.example.com/api/login",
                    "status": "failed",
                    "status_code": 500,
                    "assertions": [],
                }
            ],
            "created_at": now,
            "updated_at": now,
        },
    )
    res_hint = evaluate_run_dir_gate(run_hint)
    assert res_hint.passed is False
    assert res_hint.metrics.total_chain_runs == 1
    assert res_hint.metrics.p0_failed_chains == 1
    assert res_hint.metrics.p0_chain_failures == 1

    # 4. k6_analysis.json with status == "skipped" and non-empty placeholder metrics falls back to HAR
    run_k6_skip = tmp_path / "k6_skip"
    _build_offline_run_dir(
        run_k6_skip,
        coverage_pct=90.0,
        k6_p95_ms=None,
        har_p95_total_ms=999.0,
    )
    _write_json(
        run_k6_skip / "performance" / "k6" / "k6_analysis.json",
        {
            "status": "skipped",
            "reason": "k6 not installed",
            "metrics": {"latency_ms": {"p95": 0.0}},
            "summary": {"p95_ms": 0.0},
        },
    )
    p95_val, p95_src = extract_run_p95_ms(run_k6_skip)
    assert p95_src == "har"
    assert p95_val == 999.0


@pytest.mark.asyncio
async def test_regression_db_findings_fallback_when_findings_total_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for bug #6a: DB run with empty Finding table and findings_total > 0 loads disk findings."""
    ws_dir = _setup_isolated_env(tmp_path, monkeypatch, "db_findings_fallback.db")
    await db_mod.init_db()
    factory = db_mod.get_session_factory()

    run_ws = ws_dir / "run_disk_findings"
    _build_offline_run_dir(
        run_ws,
        coverage_pct=80.0,
        static_findings=[
            {
                "category": "idor",
                "severity": "critical",
                "endpoint": "DELETE /api/v1/users/{id}",
                "title": "Critical IDOR on disk",
            }
        ],
        k6_p95_ms=100.0,
    )

    async with factory() as db:
        db.add(Project(id="p-db-fb", name="DB Fallback", slug="db-fallback"))
        db.add(
            Run(
                id="r-db-fb",
                project_id="p-db-fb",
                status="completed",
                workspace_dir=str(run_ws),
                summary_metrics={"coverage_pct": 80.0, "findings_total": 1},
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        )
        await db.commit()

    if db_mod._engine is not None:
        await db_mod._engine.dispose()
    db_mod._engine = None
    db_mod._session_factory = None

    runner = CliRunner()
    res = runner.invoke(
        cli_app,
        [
            "gate",
            "--project-id",
            "p-db-fb",
            "--fail-on-high-without-baseline",
            "--json",
        ],
    )
    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    assert payload["passed"] is False
    assert payload["metrics"]["high_security_count"] == 1
    assert payload["metrics"]["new_high_security_count"] == 1
