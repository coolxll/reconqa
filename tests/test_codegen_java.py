"""Unit & integration tests for Module 4 (Java TestNG + RestAssured + Allure Generator)."""

from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from reconqa.modules.codegen_java.generator import generate_java_project
from reconqa.modules.hub.correlation import analyze_correlations
from reconqa.modules.hub.openapi_merger import merge_to_openapi
from reconqa.modules.recorder.har_importer import import_hars


def test_generate_java_project_structure_csv_schemas_and_no_secrets(
    fixtures_har_dir: Path, tmp_path: Path
) -> None:
    har_dir = tmp_path / "har"
    import_hars([fixtures_har_dir], har_dir, extract_js=False)

    hub_dir = tmp_path / "hub"
    merge_to_openapi(har_dir, hub_dir, target_domain="shop.example.test")
    analyze_correlations(har_dir, hub_dir, target_domain="shop.example.test")

    java_dir = tmp_path / "java-tests"
    result = generate_java_project(
        hub_dir / "openapi.yaml",
        java_dir,
        correlation_path=hub_dir / "correlation.json",
        package_name="com.reconqa.generated",
        java_version="21",
        base_uri="https://shop.example.test",
    )
    assert isinstance(result, dict)

    # Core Maven & TestNG files
    pom_xml = java_dir / "pom.xml"
    testng_xml = java_dir / "testng.xml"
    assert pom_xml.is_file()
    assert testng_xml.is_file()

    # Validate XML well-formedness
    pom_root = ET.fromstring(pom_xml.read_text(encoding="utf-8"))
    assert "project" in pom_root.tag
    testng_root = ET.fromstring(testng_xml.read_text(encoding="utf-8"))
    assert testng_root.tag == "suite"

    # Required Java source files
    pkg_dir = java_dir / "src" / "test" / "java" / "com" / "reconqa" / "generated"
    assert (pkg_dir / "base" / "ApiBaseTest.java").is_file()
    assert (pkg_dir / "util" / "ConfigUtils.java").is_file()
    assert (pkg_dir / "util" / "JsonUtils.java").is_file()
    assert (pkg_dir / "util" / "CsvUtils.java").is_file()
    assert (pkg_dir / "util" / "ReportUtils.java").is_file()
    assert (pkg_dir / "listener" / "TestExecutionListener.java").is_file()
    assert (pkg_dir / "smoke" / "L1SmokeContractTest.java").is_file()
    assert (pkg_dir / "security" / "L3AuthBoundaryTest.java").is_file()

    chain_files = list((pkg_dir / "chains").glob("L2*.java"))
    assert len(chain_files) >= 1, "Expected at least one generated L2 chain test class"
    # Ensure cleanup step or @AfterClass is present in chain tests
    chain_src = "\n".join(p.read_text(encoding="utf-8") for p in chain_files)
    assert "@AfterClass" in chain_src
    assert "dependsOnMethods" in chain_src

    # Resources: config.properties, allure.properties, logback-test.xml, CSV, schemas, payloads
    res_dir = java_dir / "src" / "test" / "resources"
    assert (res_dir / "config.properties").is_file()
    assert (res_dir / "allure.properties").is_file()
    assert (res_dir / "logback-test.xml").is_file()

    csv_file = res_dir / "data" / "l1_smoke_endpoints.csv"
    assert csv_file.is_file()
    with csv_file.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 1
    expected_cols = {
        "caseId",
        "scenario",
        "method",
        "path",
        "queryJsonPath",
        "headersJsonPath",
        "payloadJsonPath",
        "expectedStatus",
        "bizCodeField",
        "bizCodeValue",
        "schemaPath",
        "maxLatencyMs",
    }
    assert expected_cols.issubset(set(rows[0].keys()))

    # Verify externalized JSON query/header/payload & schema files referenced by CSV actually exist
    for row in rows:
        for col in ("queryJsonPath", "headersJsonPath", "payloadJsonPath", "schemaPath"):
            rel = (row.get(col) or "").strip()
            if rel:
                ref_file = res_dir / rel
                assert ref_file.is_file(), f"CSV column {col} references missing file: {rel}"
                json.loads(ref_file.read_text(encoding="utf-8"))

    # Verify JSON Schema Draft-07 files are valid Draft-07 (no OpenAPI 3.0 `nullable: true` left raw)
    schema_files = list((res_dir / "schemas").glob("*.json"))
    assert len(schema_files) >= 1
    for sf in schema_files:
        schema = json.loads(sf.read_text(encoding="utf-8"))
        assert "draft-07" in schema.get("$schema", "")
        assert '"nullable": true' not in json.dumps(schema)

    # Security hygiene: NO hardcoded auth tokens anywhere in generated Java project
    for path in java_dir.rglob("*"):
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="ignore")
            assert "token-account-a" not in content, f"Hardcoded secret leaked into {path}"
