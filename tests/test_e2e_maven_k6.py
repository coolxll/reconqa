"""Real end-to-end verification of generated Java (Maven/TestNG/RestAssured/Allure) & k6 scripts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from reconqa.modules.codegen_java.generator import generate_java_project
from reconqa.modules.hub.correlation import analyze_correlations
from reconqa.modules.hub.openapi_merger import merge_to_openapi
from reconqa.modules.performance.k6_generator import generate_k6_script, run_k6_test
from reconqa.modules.recorder.har_importer import import_hars
from tests.conftest import rewrite_har_base_url
from tests.mock_server import MockShopServer

RUN_E2E = os.environ.get("RECONQA_RUN_E2E", "0") == "1"


@pytest.mark.skipif(not RUN_E2E, reason="Set RECONQA_RUN_E2E=1 to run real Maven/Docker and k6 E2E")
def test_e2e_real_maven_testng_allure_and_k6(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    with MockShopServer(idor_vulnerable=False, unauth_bypass_orders=False) as srv:
        local_har_src = rewrite_har_base_url(
            fixtures_har_dir, tmp_path / "raw_har", srv.base_url
        )
        har_dir = tmp_path / "har"
        import_hars([local_har_src], har_dir, extract_js=True)

        hub_dir = tmp_path / "hub"
        merge_to_openapi(har_dir, hub_dir)
        analyze_correlations(har_dir, hub_dir)

        # --- 1. Real Maven + TestNG + RestAssured + Allure Verification ---
        java_dir = tmp_path / "java-tests"
        generate_java_project(
            hub_dir / "openapi.yaml",
            java_dir,
            correlation_path=hub_dir / "correlation.json",
            package_name="com.reconqa.generated",
            java_version="21",
            base_uri=srv.base_url,
        )

        mvn_bin = os.environ.get("RECONQA_MVN_BIN") or shutil.which("mvn")
        if not mvn_bin and Path("/tmp/maven/apache-maven-3.9.9/bin/mvn").is_file():
            mvn_bin = "/tmp/maven/apache-maven-3.9.9/bin/mvn"
        if mvn_bin:
            from urllib.parse import urlparse

            env = os.environ.copy()
            env["RECONQA_BASE_URI"] = srv.base_url
            env["RECONQA_AUTH_TOKEN"] = "token-account-a"
            mvn_args = [mvn_bin, "-B", "test"]
            proxy_url = env.get("HTTPS_PROXY") or env.get("https_proxy") or env.get("HTTP_PROXY") or env.get("http_proxy")
            if proxy_url:
                parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
                if parsed.hostname and parsed.port:
                    mvn_args.extend(
                        [
                            f"-Dhttp.proxyHost={parsed.hostname}",
                            f"-Dhttp.proxyPort={parsed.port}",
                            f"-Dhttps.proxyHost={parsed.hostname}",
                            f"-Dhttps.proxyPort={parsed.port}",
                            "-Dhttp.nonProxyHosts=localhost|127.0.0.1",
                        ]
                    )
            cmd = mvn_args
            cwd = java_dir
        else:
            # Reuse Maven local repository cache across runs for speed
            m2_cache = Path("/tmp/reconqa-m2-cache")
            m2_cache.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            cmd = [
                "docker",
                "run",
                "--rm",
                "--network",
                "host",
                "-v",
                f"{java_dir}:/work",
                "-v",
                f"{m2_cache}:/root/.m2",
                "-w",
                "/work",
                "-e",
                f"RECONQA_BASE_URI={srv.base_url}",
                "-e",
                "RECONQA_AUTH_TOKEN=token-account-a",
                "maven:3.9-eclipse-temurin-21",
                "mvn",
                "test",
            ]
            cwd = None

        proc = subprocess.run(
            cmd,
            check=False,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert proc.returncode == 0, (
            f"Maven test failed (rc={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )

        # Verify Allure results & ReconQA execution summary JSON
        allure_dir = java_dir / "target" / "allure-results"
        assert allure_dir.is_dir(), "target/allure-results directory not created"
        allure_jsons = list(allure_dir.glob("*-result.json"))
        assert len(allure_jsons) >= 1, f"No allure result json files found in {allure_dir}"

        summary_file = java_dir / "target" / "reconqa-execution-summary.json"
        assert summary_file.is_file(), "target/reconqa-execution-summary.json not created"
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        assert isinstance(summary, dict)

        # Verify teardown DELETE actually hit our mock server
        assert len(srv.state.cart_deleted) >= 1, (
            f"Expected @AfterClass teardown DELETE /api/v1/cart/items/{{id}} to hit mock server, got {srv.state.cart_deleted}"
        )

        # --- 2. Real k6 Verification ---
        perf_dir = tmp_path / "performance"
        generate_k6_script(
            har_dir,
            perf_dir,
            correlation_path=hub_dir / "correlation.json",
        )
        script_path = perf_dir / "k6" / "scenario_ramp.js"
        assert script_path.is_file()

        k6_bin = shutil.which("k6") or "/usr/bin/k6"
        assert Path(k6_bin).is_file(), f"k6 binary not found at {k6_bin}"

        k6_res = run_k6_test(
            script_path,
            perf_dir,
            base_url=srv.base_url,
            extra_args=[
                "--vus",
                "2",
                "--duration",
                "2s",
                "-e",
                "AUTH_TOKEN=token-account-a",
            ],
            timeout=60,
        )
        assert isinstance(k6_res, dict)
        assert k6_res.get("returncode", k6_res.get("exit_code", 0)) == 0, f"k6 failed: {k6_res}"
        diag = k6_res.get("diagnosis", {})
        assert diag.get("breached_thresholds") == [], f"Unexpected breached thresholds: {diag}"
        assert diag.get("inflection_detected") is False, f"False-positive inflection: {diag}"
        analysis_file = perf_dir / "k6" / "k6_analysis.json"
        assert analysis_file.is_file(), "k6_analysis.json not created"
