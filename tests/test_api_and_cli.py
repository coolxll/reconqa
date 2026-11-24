"""Integration tests for FastAPI REST + SSE + Worker and Typer CLI."""

from __future__ import annotations

import asyncio
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from reconqa.cli import app as cli_app
from reconqa.config import settings


def test_cli_init_doctor_and_pipeline_with_har(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    runner = CliRunner()

    # 1. reconqa doctor
    res_doc = runner.invoke(cli_app, ["doctor"])
    assert res_doc.exit_code == 0, res_doc.output
    assert "k6" in res_doc.output.lower()

    # 2. reconqa init
    cfg_path = tmp_path / "reconqa.yaml"
    res_init = runner.invoke(cli_app, ["init", "--output", str(cfg_path)])
    assert res_init.exit_code == 0, res_init.output
    assert cfg_path.is_file()

    # 3. reconqa pipeline --har <fixtures_har_dir> --modules merge,java_gen,security,perf -o <out>
    run_out = tmp_path / "cli_run"
    res_pipe = runner.invoke(
        cli_app,
        [
            "pipeline",
            "--har",
            str(fixtures_har_dir),
            "--modules",
            "merge,java_gen,security,perf",
            "-o",
            str(run_out),
        ],
    )
    assert res_pipe.exit_code == 0, res_pipe.output
    assert (run_out / "hub" / "openapi.yaml").is_file()
    assert (run_out / "hub" / "correlation.json").is_file()
    assert (run_out / "hub" / "coverage_diff.json").is_file()
    assert (run_out / "java-tests" / "pom.xml").is_file()
    assert (run_out / "security" / "static_audit.json").is_file()
    assert (run_out / "performance" / "har_perf_report.json").is_file()
    assert (run_out / "performance" / "k6" / "scenario_ramp.js").is_file()
    assert (run_out / "run_summary.json").is_file()


@pytest.mark.asyncio
async def test_fastapi_full_lifecycle_har_upload_reuse_sse_artifacts_reports(
    fixtures_har_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate DB & workspaces into tmp_path
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "test_reconqa.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

    # Reset cached DB engine/session factory for isolated test database
    from reconqa.models import db as db_mod

    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Health check
            r_health = await client.get("/healthz")
            assert r_health.status_code == 200

            # 1. Create Project
            r_proj = await client.post(
                "/api/v1/projects",
                json={"name": "Demo Shop", "description": "E2E API test project"},
            )
            assert r_proj.status_code in (200, 201), r_proj.text
            project_id = r_proj.json()["id"]

            # 2. Add Target
            r_tgt = await client.post(
                f"/api/v1/projects/{project_id}/targets",
                json={
                    "root_domain": "shop.example.test",
                    "base_url": "https://shop.example.test",
                },
            )
            assert r_tgt.status_code in (200, 201), r_tgt.text
            target_id = r_tgt.json()["id"]

            # 3. Add AuthProfile
            r_auth = await client.post(
                f"/api/v1/projects/{project_id}/auth-profiles",
                json={
                    "name": "Account A",
                    "role_tag": "account_a",
                    "extra_headers_ref": {"Authorization": "Bearer token-account-a"},
                },
            )
            assert r_auth.status_code in (200, 201), r_auth.text

            # 4. Create Run #1 (auto_start=False so we upload HARs first)
            r_run = await client.post(
                f"/api/v1/projects/{project_id}/runs?auto_start=false",
                json={
                    "target_id": target_id,
                    "target_url": "https://shop.example.test",
                    "modules": ["merge", "java_gen", "security", "perf"],
                    "trigger_source": "web",
                },
            )
            assert r_run.status_code in (200, 201), r_run.text
            run_id = r_run.json()["id"]

            # 5. Upload fixture HARs to Run #1
            files = [
                (
                    "files",
                    (p.name, p.read_bytes(), "application/json"),
                )
                for p in sorted(fixtures_har_dir.glob("*.har"))
            ]
            r_up = await client.post(f"/api/v1/runs/{run_id}/hars", files=files)
            assert r_up.status_code in (200, 201), r_up.text

            # 6. Start Run #1
            r_start = await client.post(f"/api/v1/runs/{run_id}/start")
            assert r_start.status_code in (200, 202), r_start.text

            # Poll until run finishes (in-process worker runs quickly)
            run_data = {}
            for _ in range(100):
                r_get = await client.get(f"/api/v1/runs/{run_id}")
                assert r_get.status_code == 200
                run_data = r_get.json()
                if run_data["status"] in ("succeeded", "failed", "completed"):
                    break
                await asyncio.sleep(0.1)
            assert run_data["status"] in ("succeeded", "completed"), f"Run failed: {run_data}"

            # 7. Check SSE endpoint responds with text/event-stream
            async with client.stream("GET", f"/api/v1/runs/{run_id}/events") as sse_resp:
                assert sse_resp.status_code == 200
                assert "text/event-stream" in sse_resp.headers.get("content-type", "")
                chunks = []
                async for line in sse_resp.aiter_lines():
                    if line:
                        chunks.append(line)
                    if len(chunks) >= 2:
                        break
                assert len(chunks) >= 1

            # 8. Check Hub reports & artifacts endpoints
            r_oa = await client.get(f"/api/v1/runs/{run_id}/openapi?format=json")
            assert r_oa.status_code == 200
            assert "paths" in r_oa.json()

            r_corr = await client.get(f"/api/v1/runs/{run_id}/correlation")
            assert r_corr.status_code == 200
            assert "chains" in r_corr.json()

            r_cov = await client.get(f"/api/v1/runs/{run_id}/coverage")
            assert r_cov.status_code == 200

            r_findings = await client.get(f"/api/v1/runs/{run_id}/findings")
            assert r_findings.status_code == 200
            assert isinstance(r_findings.json(), list)

            r_perf = await client.get(f"/api/v1/runs/{run_id}/performance")
            assert r_perf.status_code == 200

            r_sec = await client.get(f"/api/v1/runs/{run_id}/security")
            assert r_sec.status_code == 200

            r_arts = await client.get(f"/api/v1/runs/{run_id}/artifacts")
            assert r_arts.status_code == 200
            assert len(r_arts.json()) >= 1

            # Download java-tests zip
            r_zip = await client.get(f"/api/v1/runs/{run_id}/artifacts/java-tests")
            assert r_zip.status_code == 200
            with zipfile.ZipFile(BytesIO(r_zip.content)) as zf:
                names = zf.namelist()
                assert any(n.endswith("pom.xml") for n in names), f"Missing pom.xml in zip: {names}"

            # 9. Reuse HARs in Run #2
            r_run2 = await client.post(
                f"/api/v1/projects/{project_id}/runs?auto_start=false",
                json={
                    "target_id": target_id,
                    "modules": ["merge"],
                    "trigger_source": "web",
                },
            )
            assert r_run2.status_code in (200, 201)
            run2_id = r_run2.json()["id"]

            r_reuse = await client.post(
                f"/api/v1/runs/{run2_id}/hars/reuse",
                json={"source_run_id": run_id},
            )
            assert r_reuse.status_code in (200, 201), r_reuse.text

            # 10. Project trends
            r_trends = await client.get(f"/api/v1/projects/{project_id}/trends")
            assert r_trends.status_code == 200

    if db_mod._engine is not None:
        await db_mod._engine.dispose()
