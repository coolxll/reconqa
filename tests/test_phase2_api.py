"""Phase 2 API & DevTools multi-format text importer tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from reconqa.config import settings
from reconqa.modules.hub.openapi_merger import merge_to_openapi


def test_text_importer_curl_fetch_raw_http_and_har_json(tmp_path: Path) -> None:
    from reconqa.modules.recorder.text_importer import import_traffic_text

    # 1. Multi-cURL (Chrome DevTools Copy all as cURL bash)
    curl_paste = r"""
curl 'https://shop.example.test/api/v1/items/search?keyword=laptop' \
  -H ':method: GET' \
  -H 'accept: application/json' \
  -b 'sid=session_demo_123' \
  --compressed

curl 'https://shop.example.test/api/v1/cart/add' \
  -X POST \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer token-account-a' \
  --data-raw $'{"skuId":"SKU-55102","qty":1}' \
  --compressed
"""
    curl_out = tmp_path / "curl_har"
    res_curl = import_traffic_text(curl_paste, curl_out, scenario_name="01_curl")
    assert res_curl["requests_imported"] == 2
    assert "curl" in res_curl["detected_format"].lower()
    har_path = Path(res_curl["har_path"])
    assert har_path.is_file()
    har_obj = json.loads(har_path.read_text(encoding="utf-8"))
    assert len(har_obj["log"]["entries"]) == 2
    # HTTP/2 pseudo-headers must be stripped
    for entry in har_obj["log"]["entries"]:
        names = [h["name"] for h in entry["request"].get("headers", [])]
        assert not any(n.startswith(":") for n in names), f"Pseudo-header left: {names}"

    # Verify downstream OpenAPI merge works on the synthetic HAR
    hub_dir = tmp_path / "hub_from_curl"
    spec = merge_to_openapi(curl_out, hub_dir)
    assert "/api/v1/items/search" in spec["paths"]
    assert "/api/v1/cart/add" in spec["paths"]

    # 2. Chrome DevTools Copy as fetch
    fetch_paste = """
fetch("https://shop.example.test/api/v1/orders/ORD-66201", {
  "headers": {
    "accept": "application/json",
    "authorization": "Bearer token-account-a"
  },
  "method": "GET"
});
fetch("https://shop.example.test/api/v1/cart/add", {
  "headers": {
    "content-type": "application/json"
  },
  "body": "{\\"skuId\\":\\"SKU-55102\\",\\"qty\\":2}",
  "method": "POST"
});
"""
    fetch_out = tmp_path / "fetch_har"
    res_fetch = import_traffic_text(fetch_paste, fetch_out, scenario_name="02_fetch")
    assert res_fetch["requests_imported"] == 2
    assert "fetch" in res_fetch["detected_format"].lower()
    assert Path(res_fetch["har_path"]).is_file()

    # 3. Raw HTTP (Burp / .http separated by ###)
    raw_http_paste = """
GET /api/v1/items/ITEM-88421 HTTP/1.1
Host: shop.example.test
Accept: application/json

###

POST /api/v1/orders/submit HTTP/1.1
Host: shop.example.test
Content-Type: application/json
X-Sign: deadbeef1234

{"skuId": "SKU-55102", "qty": 1}
"""
    raw_out = tmp_path / "raw_har"
    res_raw = import_traffic_text(raw_http_paste, raw_out, scenario_name="03_raw")
    assert res_raw["requests_imported"] == 2
    assert "http" in res_raw["detected_format"].lower()
    assert Path(res_raw["har_path"]).is_file()

    # 4. Pasted HAR 1.2 JSON
    har_json = json.dumps(
        {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "startedDateTime": "2026-01-15T10:00:00.000Z",
                        "time": 25,
                        "request": {
                            "method": "GET",
                            "url": "https://shop.example.test/api/v1/ping",
                            "httpVersion": "HTTP/1.1",
                            "headers": [],
                            "queryString": [],
                            "cookies": [],
                        },
                        "response": {
                            "status": 200,
                            "statusText": "OK",
                            "httpVersion": "HTTP/1.1",
                            "headers": [{"name": "Content-Type", "value": "application/json"}],
                            "cookies": [],
                            "content": {
                                "size": 12,
                                "mimeType": "application/json",
                                "text": '{"code":0}',
                            },
                        },
                        "timings": {"send": 1, "wait": 20, "receive": 4},
                    }
                ],
            }
        }
    )
    har_out = tmp_path / "pasted_har"
    res_har = import_traffic_text(har_json, har_out, scenario_name="04_har")
    assert res_har["requests_imported"] == 1
    assert "har" in res_har["detected_format"].lower()


@pytest.mark.asyncio
async def test_phase2_doctor_java_report_import_text_and_delete_auth_profile(
    fixtures_har_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "phase2_test.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

    from reconqa.models import db as db_mod

    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)

    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. GET /api/v1/system/doctor (including enriched UX fields)
            r_doc = await client.get("/api/v1/system/doctor")
            assert r_doc.status_code == 200, r_doc.text
            doc_body = r_doc.json()
            assert isinstance(doc_body, dict)
            tools_map = doc_body.get("tools", doc_body)
            assert "k6" in tools_map and "docker" in tools_map
            for req_key in (
                "available",
                "purpose_zh",
                "purpose_en",
                "impact_zh",
                "impact_en",
                "install_cmd",
                "auto_installable",
            ):
                assert req_key in tools_map["nuclei"], f"Missing {req_key} in nuclei doctor entry"
            assert tools_map["nuclei"]["auto_installable"] is True
            assert tools_map["docker"]["auto_installable"] is False
            assert tools_map["mvn"].get("docker_alt")

            # 1b. POST /api/v1/system/doctor/install strict allowlist validation
            r_bad_unknown = await client.post(
                "/api/v1/system/doctor/install", json={"tool": "not-a-real-tool; id"}
            )
            assert r_bad_unknown.status_code == 400, r_bad_unknown.text
            r_bad_os = await client.post(
                "/api/v1/system/doctor/install", json={"tool": "docker"}
            )
            assert r_bad_os.status_code == 400, r_bad_os.text

            # 2. Project + AuthProfile create & DELETE
            r_proj = await client.post(
                "/api/v1/projects",
                json={"name": "Phase2 Shop", "description": "Web Console API test"},
            )
            assert r_proj.status_code in (200, 201), r_proj.text
            project_id = r_proj.json()["id"]

            r_ap = await client.post(
                f"/api/v1/projects/{project_id}/auth-profiles",
                json={
                    "name": "Account B",
                    "role_tag": "account_b",
                    "extra_headers_ref": {"Authorization": "Bearer token-account-b"},
                },
            )
            assert r_ap.status_code in (200, 201), r_ap.text
            profile_id = r_ap.json()["id"]

            r_del = await client.delete(
                f"/api/v1/projects/{project_id}/auth-profiles/{profile_id}"
            )
            assert r_del.status_code in (200, 204), r_del.text

            r_list = await client.get(f"/api/v1/projects/{project_id}/auth-profiles")
            assert r_list.status_code == 200
            assert all(p["id"] != profile_id for p in r_list.json())

            # 3. Create Run, upload HARs, run merge + java_gen
            r_run = await client.post(
                f"/api/v1/projects/{project_id}/runs?auto_start=false",
                json={
                    "target_url": "https://shop.example.test",
                    "modules": ["merge", "java_gen"],
                    "trigger_source": "web",
                },
            )
            assert r_run.status_code in (200, 201), r_run.text
            run_id = r_run.json()["id"]

            files = [
                ("files", (p.name, p.read_bytes(), "application/json"))
                for p in sorted(fixtures_har_dir.glob("*.har"))
            ]
            r_up = await client.post(f"/api/v1/runs/{run_id}/hars", files=files)
            assert r_up.status_code in (200, 201), r_up.text

            r_start = await client.post(f"/api/v1/runs/{run_id}/start")
            assert r_start.status_code in (200, 202), r_start.text

            run_data = {}
            for _ in range(100):
                r_get = await client.get(f"/api/v1/runs/{run_id}")
                assert r_get.status_code == 200
                run_data = r_get.json()
                if run_data["status"] in ("succeeded", "failed", "completed"):
                    break
                await asyncio.sleep(0.1)
            assert run_data["status"] in ("succeeded", "completed"), run_data

            # 4. Pre-execution GET /api/v1/runs/{run_id}/java-report
            r_jr_pre = await client.get(f"/api/v1/runs/{run_id}/java-report")
            assert r_jr_pre.status_code == 200, r_jr_pre.text
            jr_pre = r_jr_pre.json()
            assert jr_pre["run_id"] == run_id
            assert jr_pre["generated"] is True
            assert jr_pre["project_meta"]["l1_cases"] >= 1
            assert jr_pre["project_meta"]["l2_chains"] >= 1
            assert jr_pre["execution_summary"] is None
            assert jr_pre["allure_results"] == []

            # 5. Seed mock reconqa-execution-summary.json + allure-results & re-query
            run_workspace = Path(run_data["workspace_dir"])
            target_dir = run_workspace / "java-tests" / "target"
            allure_dir = target_dir / "allure-results"
            allure_dir.mkdir(parents=True, exist_ok=True)

            mock_summary = {
                "suiteName": "ReconQA API Automation Suite",
                "total": 2,
                "passed": 1,
                "failed": 1,
                "skipped": 0,
                "durationMs": 420,
                "tests": [
                    {
                        "testClass": "com.reconqa.generated.smoke.L1SmokeContractTest",
                        "testMethod": "verifyEndpointContract",
                        "status": "PASSED",
                        "durationMs": 120,
                        "epic": "ReconQA API Automation",
                        "feature": "L1 Smoke",
                        "story": "Search",
                        "failureMessage": None,
                    },
                    {
                        "testClass": "com.reconqa.generated.security.L3AuthBoundaryTest",
                        "testMethod": "verifyOrderAuth",
                        "status": "FAILED",
                        "durationMs": 300,
                        "epic": "ReconQA API Automation",
                        "feature": "L3 Security",
                        "story": "Order Auth",
                        "failureMessage": "Expected 401 but got 200",
                    },
                ],
            }
            (target_dir / "reconqa-execution-summary.json").write_text(
                json.dumps(mock_summary), encoding="utf-8"
            )
            mock_allure = {
                "uuid": "11111111-2222-3333-4444-555555555555",
                "name": "verifyEndpointContract[L1_001]",
                "status": "passed",
                "start": 1700000000000,
                "stop": 1700000000120,
                "labels": [
                    {"name": "epic", "value": "ReconQA API Automation"},
                    {"name": "feature", "value": "L1 Smoke"},
                    {"name": "story", "value": "Search"},
                ],
                "statusDetails": {},
            }
            (allure_dir / "11111111-result.json").write_text(
                json.dumps(mock_allure), encoding="utf-8"
            )

            r_jr_post = await client.get(f"/api/v1/runs/{run_id}/java-report")
            assert r_jr_post.status_code == 200, r_jr_post.text
            jr_post = r_jr_post.json()
            assert jr_post["execution_summary"]["total"] == 2
            assert jr_post["execution_summary"]["passed"] == 1
            assert len(jr_post["allure_results"]) == 1
            assert jr_post["allure_results"][0]["name"] == "verifyEndpointContract[L1_001]"
            assert jr_post["allure_results"][0]["status"] == "passed"
            assert jr_post["allure_results"][0]["duration_ms"] == 120

            # 6. POST /api/v1/runs/{run_id}/import-text (DevTools cURL -> Run #2)
            r_run2 = await client.post(
                f"/api/v1/projects/{project_id}/runs?auto_start=false",
                json={
                    "target_url": "https://shop.example.test",
                    "modules": ["merge"],
                    "trigger_source": "web",
                },
            )
            assert r_run2.status_code in (200, 201)
            run2_id = r_run2.json()["id"]

            # Invalid/empty text -> 400
            r_bad = await client.post(
                f"/api/v1/runs/{run2_id}/import-text",
                json={"content": "not a valid curl or http request"},
            )
            assert r_bad.status_code == 400, r_bad.text

            # Valid cURL paste -> 200 + pipeline merge succeeds
            r_ok = await client.post(
                f"/api/v1/runs/{run2_id}/import-text",
                json={
                    "content": (
                        "curl 'https://shop.example.test/api/v1/items/search?keyword=mouse' "
                        "-H 'Accept: application/json'"
                    ),
                    "scenario": "01_curl_paste",
                    "format": "auto",
                },
            )
            assert r_ok.status_code in (200, 201), r_ok.text
            assert r_ok.json()["requests_imported"] == 1

            r_start2 = await client.post(f"/api/v1/runs/{run2_id}/start")
            assert r_start2.status_code in (200, 202), r_start2.text
            run2_data = {}
            for _ in range(100):
                r_g2 = await client.get(f"/api/v1/runs/{run2_id}")
                run2_data = r_g2.json()
                if run2_data["status"] in ("succeeded", "failed", "completed"):
                    break
                await asyncio.sleep(0.1)
            assert run2_data["status"] in ("succeeded", "completed"), run2_data

            # 7. Project Knowledge Base CRUD + upload + search + download + delete
            r_kb_create = await client.post(
                f"/api/v1/projects/{project_id}/knowledge",
                json={
                    "title": "Checkout Order PRD v1",
                    "doc_type": "prd",
                    "source_kind": "markdown",
                    "content": "# Checkout Rules\nInventory must be locked on /api/v1/orders/submit.",
                    "tags": ["checkout", "inventory"],
                    "related_endpoints": ["/api/v1/orders/submit"],
                },
            )
            assert r_kb_create.status_code in (200, 201), r_kb_create.text
            kb_id = r_kb_create.json()["id"]

            r_kb_upload = await client.post(
                f"/api/v1/projects/{project_id}/knowledge/upload",
                files={"file": ("api_notes.md", b"# API Notes\nRate limit is 100 RPS.\n", "text/markdown")},
                data={
                    "title": "Rate Limit Spec",
                    "doc_type": "business_rule",
                    "tags": "perf,ratelimit",
                    "related_endpoints": "/api/v1/items/search",
                },
            )
            assert r_kb_upload.status_code in (200, 201), r_kb_upload.text
            kb_up_id = r_kb_upload.json()["id"]
            assert "100 RPS" in r_kb_upload.json()["content"]

            r_kb_search = await client.get(
                f"/api/v1/projects/{project_id}/knowledge?doc_type=prd&q=Inventory"
            )
            assert r_kb_search.status_code == 200, r_kb_search.text
            assert len(r_kb_search.json()) == 1
            assert r_kb_search.json()[0]["id"] == kb_id

            r_kb_upd = await client.put(
                f"/api/v1/projects/{project_id}/knowledge/{kb_id}",
                json={"title": "Checkout Order PRD v2", "tags": ["checkout", "v2"]},
            )
            assert r_kb_upd.status_code == 200, r_kb_upd.text
            assert r_kb_upd.json()["title"] == "Checkout Order PRD v2"

            r_kb_dl = await client.get(
                f"/api/v1/projects/{project_id}/knowledge/{kb_up_id}/download"
            )
            assert r_kb_dl.status_code == 200, r_kb_dl.text
            assert b"100 RPS" in r_kb_dl.content

            r_kb_del = await client.delete(
                f"/api/v1/projects/{project_id}/knowledge/{kb_up_id}"
            )
            assert r_kb_del.status_code in (200, 204), r_kb_del.text

            # 8. Incremental on-demand execution: ["merge"] -> ["java_gen"] -> ["perf"] -> ["security"]
            r_inc_run = await client.post(
                f"/api/v1/projects/{project_id}/runs?auto_start=false",
                json={
                    "target_url": "https://shop.example.test",
                    "modules": ["merge"],
                    "trigger_source": "web",
                },
            )
            assert r_inc_run.status_code in (200, 201), r_inc_run.text
            inc_run_id = r_inc_run.json()["id"]

            inc_files = [
                ("files", (p.name, p.read_bytes(), "application/json"))
                for p in sorted(fixtures_har_dir.glob("*.har"))
            ]
            r_inc_up = await client.post(f"/api/v1/runs/{inc_run_id}/hars", files=inc_files)
            assert r_inc_up.status_code in (200, 201), r_inc_up.text
            r_inc_start = await client.post(f"/api/v1/runs/{inc_run_id}/start")
            assert r_inc_start.status_code in (200, 202), r_inc_start.text

            async def _wait_done(rid: str) -> dict:
                for _ in range(100):
                    resp = await client.get(f"/api/v1/runs/{rid}")
                    assert resp.status_code == 200
                    data = resp.json()
                    if data["status"] in ("succeeded", "completed", "failed"):
                        return data
                    await asyncio.sleep(0.1)
                raise AssertionError(f"Run {rid} timed out")

            done_merge = await _wait_done(inc_run_id)
            assert done_merge["status"] in ("succeeded", "completed"), done_merge
            assert (await client.get(f"/api/v1/runs/{inc_run_id}/openapi?format=json")).status_code == 200
            # Java not generated yet
            jr_empty = (await client.get(f"/api/v1/runs/{inc_run_id}/java-report")).json()
            assert jr_empty["generated"] is False

            # Step B: on-demand java_gen with custom package
            r_ex_java = await client.post(
                f"/api/v1/runs/{inc_run_id}/execute-modules",
                json={
                    "modules": ["java_gen"],
                    "config": {"java_package": "com.example.custom", "java_version": "21"},
                },
            )
            assert r_ex_java.status_code in (200, 202), r_ex_java.text
            done_java = await _wait_done(inc_run_id)
            assert done_java["status"] in ("succeeded", "completed"), done_java
            jr_built = (await client.get(f"/api/v1/runs/{inc_run_id}/java-report")).json()
            assert jr_built["generated"] is True
            assert jr_built["project_meta"]["package_name"] == "com.example.custom"

            # Step C: on-demand perf
            r_ex_perf = await client.post(
                f"/api/v1/runs/{inc_run_id}/execute-modules",
                json={"modules": ["perf"], "config": {"run_k6": False}},
            )
            assert r_ex_perf.status_code in (200, 202), r_ex_perf.text
            done_perf = await _wait_done(inc_run_id)
            assert done_perf["status"] in ("succeeded", "completed"), done_perf
            assert (await client.get(f"/api/v1/runs/{inc_run_id}/performance")).status_code == 200

            # Step D: on-demand security (and earlier java + perf + openapi artifacts remain intact!)
            r_ex_sec = await client.post(
                f"/api/v1/runs/{inc_run_id}/execute-modules",
                json={"modules": ["security"], "config": {"include_mutations": False, "run_nuclei": False}},
            )
            assert r_ex_sec.status_code in (200, 202), r_ex_sec.text
            done_sec = await _wait_done(inc_run_id)
            assert done_sec["status"] in ("succeeded", "completed"), done_sec
            assert (await client.get(f"/api/v1/runs/{inc_run_id}/security")).status_code == 200
            # Verify earlier findings/artifacts preserved (openapi, java-tests, perf, security all present!)
            arts = (await client.get(f"/api/v1/runs/{inc_run_id}/artifacts")).json()
            art_kinds = {a["kind"] for a in arts}
            assert "openapi" in art_kinds or any("openapi" in k for k in art_kinds)
            assert any("java" in k for k in art_kinds)
            findings = (await client.get(f"/api/v1/runs/{inc_run_id}/findings")).json()
            cats = {f["category"] for f in findings}
            assert any("perf" in c for c in cats), f"Perf findings lost after security run: {cats}"
            assert any("sec" in c or "header" in c or "cookie" in c for c in cats)

            # 9. Regression test (#85): mismatched target_url host MUST NOT silently drop HAR entries
            r_mis_tgt = await client.post(
                f"/api/v1/projects/{project_id}/targets",
                json={"root_domain": "127.0.0.1", "base_url": "http://127.0.0.1:9999"},
            )
            assert r_mis_tgt.status_code in (200, 201), r_mis_tgt.text
            mis_tgt_id = r_mis_tgt.json()["id"]

            r_mis_run = await client.post(
                f"/api/v1/projects/{project_id}/runs?auto_start=false",
                json={
                    "target_id": mis_tgt_id,
                    "target_url": "http://127.0.0.1:9999",
                    "modules": ["merge"],
                    "trigger_source": "web",
                },
            )
            assert r_mis_run.status_code in (200, 201), r_mis_run.text
            mis_run_id = r_mis_run.json()["id"]

            r_mis_imp = await client.post(
                f"/api/v1/runs/{mis_run_id}/import-text",
                json={
                    "content": "curl 'https://other.example.test/api/v1/ping' -H 'Accept: application/json'",
                    "scenario": "01_mismatch",
                    "format": "auto",
                },
            )
            assert r_mis_imp.status_code in (200, 201), r_mis_imp.text
            assert (await client.post(f"/api/v1/runs/{mis_run_id}/start")).status_code in (200, 202)
            mis_done = await _wait_done(mis_run_id)
            assert mis_done["status"] in ("succeeded", "completed"), mis_done
            assert mis_done["summary_metrics"].get("total_endpoints", 0) >= 1, mis_done
            mis_oa = (await client.get(f"/api/v1/runs/{mis_run_id}/openapi?format=json")).json()
            assert "/api/v1/ping" in mis_oa.get("paths", {})

            # 10. Human-readable auto run_name & DELETE /api/v1/runs/{run_id} (#118)
            assert mis_done["config_snapshot"].get("run_name"), f"Expected auto run_name: {mis_done['config_snapshot']}"
            mis_ws = Path(mis_done["workspace_dir"])
            assert mis_ws.is_dir()

            r_del_run = await client.delete(f"/api/v1/runs/{mis_run_id}")
            assert r_del_run.status_code == 200, r_del_run.text
            assert r_del_run.json().get("deleted") is True
            assert not mis_ws.exists(), f"Expected run workspace removed: {mis_ws}"

            r_del_run_again = await client.delete(f"/api/v1/runs/{mis_run_id}")
            assert r_del_run_again.status_code == 404

    if db_mod._engine is not None:
        await db_mod._engine.dispose()


@pytest.mark.asyncio
async def test_worker_monotonic_job_state_and_startup_self_heal(
    fixtures_har_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test (#110): monotonic job transitions, startup self-heal, and zero stuck running jobs."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "worker_race_test.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

    from sqlalchemy import select

    from reconqa.models import db as db_mod
    from reconqa.models.tables import Job, Project, Run
    from reconqa.server.worker import InProcessTaskRunner

    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)
    await db_mod.init_db()

    # 1. Seed a completed run with a stuck 'running' child job (simulating pre-fix DB state)
    session_factory = db_mod.get_session_factory()
    async with session_factory() as session:
        proj = Project(slug="race-proj", name="Race Proj")
        session.add(proj)
        await session.flush()

        run = Run(
            project_id=proj.id,
            status="completed",
            trigger_source="web",
            workspace_dir=str(ws_dir / "runs" / "stuck1"),
            config_snapshot={"modules": ["merge", "perf"]},
        )
        session.add(run)
        await session.flush()
        run_id = run.id

        job_merge = Job(run_id=run.id, module="merge", status="completed")
        job_perf = Job(run_id=run.id, module="perf", status="running")
        session.add_all([job_merge, job_perf])
        await session.commit()

    # 2. Startup self-healing must reconcile stuck 'running' child job of 'completed' run
    runner = InProcessTaskRunner()
    await runner.start()
    try:
        async with session_factory() as session:
            repaired = (
                await session.execute(
                    select(Job).where(Job.run_id == run_id, Job.module == "perf")
                )
            ).scalar_one()
            assert repaired.status == "completed", f"Expected self-healed completed, got {repaired.status}"
            assert repaired.finished_at is not None

        # 3. Monotonic guard: stale 'running' update MUST NOT downgrade a 'completed' job
        await runner._update_job_status(run_id, "perf", "running")
        async with session_factory() as session:
            after_stale = (
                await session.execute(
                    select(Job).where(Job.run_id == run_id, Job.module == "perf")
                )
            ).scalar_one()
            assert after_stale.status == "completed", (
                f"Stale running update downgraded completed job to {after_stale.status}"
            )
    finally:
        await runner.stop()

    # 4. Fast full pipeline run via FastAPI: ALL jobs in RunOut must finish 'completed' (zero stuck 'running')
    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_p = await client.post("/api/v1/projects", json={"name": "Fast Race Check"})
            assert r_p.status_code in (200, 201)
            pid = r_p.json()["id"]

            r_r = await client.post(
                f"/api/v1/projects/{pid}/runs?auto_start=false",
                json={"modules": ["merge", "perf", "security"], "trigger_source": "web"},
            )
            assert r_r.status_code in (200, 201)
            rid = r_r.json()["id"]

            files = [("files", (p.name, p.read_bytes(), "application/json")) for p in sorted(fixtures_har_dir.glob("*.har"))]
            assert (await client.post(f"/api/v1/runs/{rid}/hars", files=files)).status_code in (200, 201)
            assert (await client.post(f"/api/v1/runs/{rid}/start")).status_code in (200, 202)

            final_run: dict = {}
            for _ in range(100):
                final_run = (await client.get(f"/api/v1/runs/{rid}")).json()
                if final_run["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.05)

            assert final_run["status"] == "completed", final_run
            job_statuses = {j["module"]: j["status"] for j in final_run["jobs"]}
            assert job_statuses == {
                "merge": "completed",
                "perf": "completed",
                "security": "completed",
            }, f"Stuck/wrong job statuses after fast run: {job_statuses}"

    if db_mod._engine is not None:
        await db_mod._engine.dispose()


@pytest.mark.asyncio
async def test_worker_reconciles_orphaned_running_runs_and_unblocks_sse_on_shutdown(
    fixtures_har_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify #192: orphaned running/queued runs self-heal on startup & stop() unblocks SSE streams."""
    ws_dir = tmp_path / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_file = ws_dir / "orphan_recovery.db"
    monkeypatch.setattr(settings, "data_dir", ws_dir)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

    from sqlalchemy import select

    from reconqa.models import db as db_mod
    from reconqa.models.tables import Job, Project, Run
    from reconqa.server.worker import InProcessTaskRunner

    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)
    await db_mod.init_db()

    # 1. Seed an orphaned running Run (merge=completed, perf=queued) + an orphaned running Run with all jobs completed
    session_factory = db_mod.get_session_factory()
    orphan_ws = ws_dir / "runs" / "orphan_run_1"
    orphan_har_dir = orphan_ws / "har"
    orphan_har_dir.mkdir(parents=True, exist_ok=True)
    for har_src in sorted(fixtures_har_dir.glob("*.har")):
        (orphan_har_dir / har_src.name).write_bytes(har_src.read_bytes())

    async with session_factory() as session:
        proj = Project(slug="orphan-proj", name="Orphan Proj")
        session.add(proj)
        await session.flush()

        run_orphan = Run(
            project_id=proj.id,
            status="running",
            trigger_source="web",
            workspace_dir=str(orphan_ws),
            config_snapshot={"modules": ["merge", "perf"], "pending_modules": ["perf"]},
        )
        run_done_jobs = Run(
            project_id=proj.id,
            status="running",
            trigger_source="web",
            workspace_dir=str(ws_dir / "runs" / "orphan_run_2"),
            config_snapshot={"modules": ["merge"]},
        )
        session.add_all([run_orphan, run_done_jobs])
        await session.flush()
        orphan_id = run_orphan.id
        done_jobs_id = run_done_jobs.id

        session.add_all(
            [
                Job(run_id=orphan_id, module="merge", status="completed"),
                Job(run_id=orphan_id, module="perf", status="queued"),
                Job(run_id=done_jobs_id, module="merge", status="completed"),
            ]
        )
        await session.commit()

    # 2. Test runner.stop() immediately unblocks active subscribe_events() with server_shutdown
    runner = InProcessTaskRunner()
    await runner.start()
    received_events: list[dict] = []

    async def _listen() -> None:
        async for ev in runner.subscribe_events(orphan_id):
            received_events.append(ev)

    listener_task = asyncio.create_task(_listen())
    await asyncio.sleep(0.05)
    await runner.stop()
    await asyncio.wait_for(listener_task, timeout=1.5)
    assert any(ev.get("event") == "server_shutdown" for ev in received_events), received_events

    # 3. Verify startup reconciliation healed BOTH runs cleanly
    async with session_factory() as session:
        healed_orphan = await session.get(Run, orphan_id)
        healed_done = await session.get(Run, done_jobs_id)
        assert healed_orphan is not None and healed_done is not None
        assert healed_orphan.status == "failed"
        assert "pending_modules" not in (healed_orphan.config_snapshot or {})
        assert healed_done.status == "completed"

        perf_job = (
            await session.execute(
                select(Job).where(Job.run_id == orphan_id, Job.module == "perf")
            )
        ).scalar_one()
        assert perf_job.status == "failed"
        assert "Interrupted by server restart" in str((perf_job.output_meta or {}).get("error") or "")

    # 4. Verify POST /runs/{id}/execute-modules immediately succeeds (not 409!) and finishes perf
    from reconqa.server.app import create_app

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_exec = await client.post(
                f"/api/v1/runs/{orphan_id}/execute-modules",
                json={"modules": ["perf"]},
            )
            assert r_exec.status_code == 200, r_exec.text

            final_run: dict = {}
            for _ in range(100):
                final_run = (await client.get(f"/api/v1/runs/{orphan_id}")).json()
                if final_run["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.05)
            assert final_run["status"] == "completed", final_run
            job_map = {j["module"]: j["status"] for j in final_run["jobs"]}
            assert job_map["perf"] == "completed", job_map

    if db_mod._engine is not None:
        await db_mod._engine.dispose()
