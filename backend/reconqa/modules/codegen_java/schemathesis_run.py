"""Schemathesis L3 OpenAPI contract fuzzing runner and report summarizer."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from reconqa.config import settings
from reconqa.utils.io import ensure_dir, redact_header_value, write_json
from reconqa.utils.proc import run_cmd_sync

DEFAULT_CHECKS = [
    "not_a_server_error",
    "status_code_conformance",
    "response_schema_conformance",
]


def _parse_junit_report(junit_path: Path) -> tuple[dict[str, int], list[dict[str, Any]]]:
    summary = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    findings: list[dict[str, Any]] = []
    if not junit_path.exists():
        return summary, findings

    try:
        tree = ET.parse(junit_path)
        root = tree.getroot()
    except ET.ParseError:
        return summary, findings

    for testcase in root.iter("testcase"):
        summary["total"] += 1
        name = testcase.attrib.get("name", "unknown")
        classname = testcase.attrib.get("classname", "")
        failure = testcase.find("failure")
        error = testcase.find("error")
        skipped = testcase.find("skipped")

        if failure is not None:
            summary["failed"] += 1
            findings.append(
                {
                    "endpoint": name,
                    "suite": classname,
                    "kind": "failure",
                    "message": (failure.attrib.get("message") or "").strip(),
                    "details": (failure.text or "").strip()[:2000],
                }
            )
        elif error is not None:
            summary["errors"] += 1
            findings.append(
                {
                    "endpoint": name,
                    "suite": classname,
                    "kind": "error",
                    "message": (error.attrib.get("message") or "").strip(),
                    "details": (error.text or "").strip()[:2000],
                }
            )
        elif skipped is not None:
            summary["skipped"] += 1
        else:
            summary["passed"] += 1

    return summary, findings


def _parse_stdout_fallback(stdout: str) -> tuple[dict[str, int], list[dict[str, Any]]]:
    summary = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    findings: list[dict[str, Any]] = []

    passed_match = re.search(r"(\d+)\s+passed", stdout)
    failed_match = re.search(r"(\d+)\s+failed", stdout)
    error_match = re.search(r"(\d+)\s+errored?", stdout)
    if passed_match:
        summary["passed"] = int(passed_match.group(1))
    if failed_match:
        summary["failed"] = int(failed_match.group(1))
    if error_match:
        summary["errors"] = int(error_match.group(1))
    summary["total"] = summary["passed"] + summary["failed"] + summary["errors"]

    for line in stdout.splitlines():
        stripped = line.strip()
        if any(marker in stripped for marker in ("FAILED", "ERROR", "Server error", "Schema mismatch")):
            findings.append(
                {
                    "endpoint": stripped[:120],
                    "suite": "schemathesis",
                    "kind": "failure",
                    "message": stripped[:500],
                    "details": "",
                }
            )
    return summary, findings


def run_schemathesis(
    openapi_path: Path,
    base_url: str,
    out_dir: Path,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    """Run Schemathesis against an OpenAPI specification and write schemathesis_report.json.

    Gracefully returns a skipped report if the schemathesis binary is not installed.
    """
    ensure_dir(out_dir)
    report_path = out_dir / "schemathesis_report.json"
    junit_path = out_dir / "schemathesis_junit.xml"

    schemathesis_bin = settings.resolve_bin("schemathesis")
    if not schemathesis_bin:
        report: dict[str, Any] = {
            "status": "skipped",
            "reason": "schemathesis binary not found (install optional extra: reconqa[fuzz])",
            "openapi_path": str(openapi_path),
            "base_url": base_url,
            "checks": DEFAULT_CHECKS,
            "report_path": str(report_path),
            "summary": {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "findings": [],
        }
        write_json(report_path, report)
        return report

    cmd = [
        schemathesis_bin,
        "run",
        str(openapi_path),
        "--base-url",
        base_url,
        "--checks",
        ",".join(DEFAULT_CHECKS),
        "--junit-xml",
        str(junit_path),
    ]

    redacted_cmd = list(cmd)
    if headers:
        for key, value in headers.items():
            cmd.extend(["--header", f"{key}: {value}"])
            redacted_cmd.extend(["--header", f"{key}: {redact_header_value(key, value)}"])

    result = run_cmd_sync(cmd, timeout=timeout, cwd=out_dir)
    if junit_path.exists():
        summary, findings = _parse_junit_report(junit_path)
    else:
        summary, findings = _parse_stdout_fallback(result.stdout)

    if result.timed_out:
        status = "timeout"
    elif result.returncode == 0 and summary["failed"] == 0 and summary["errors"] == 0:
        status = "passed"
    else:
        status = "failed"

    report = {
        "status": status,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "openapi_path": str(openapi_path),
        "base_url": base_url,
        "checks": DEFAULT_CHECKS,
        "command": redacted_cmd,
        "report_path": str(report_path),
        "summary": summary,
        "findings": findings,
        "stdout_tail": result.stdout[-4000:] if result.stdout else "",
        "stderr_tail": result.stderr[-2000:] if result.stderr else "",
    }
    write_json(report_path, report)
    return report
