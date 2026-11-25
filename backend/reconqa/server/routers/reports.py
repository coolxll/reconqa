"""Findings, security/performance/Java reports, system doctor, and project trends."""

from __future__ import annotations

import csv
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from reconqa.config import doctor_check, load_project_config, settings
from reconqa.models.db import get_db
from reconqa.models.schemas import FindingOut, QualityGateRequest, QualityGateResult
from reconqa.models.tables import Finding, Project, Run, ScenarioModel, Target
from reconqa.modules.hub.acceptance_report import (
    build_acceptance_report_data,
    render_acceptance_report_html,
    render_acceptance_report_markdown,
)
from reconqa.modules.hub.capability_map import build_capability_map
from reconqa.modules.hub.chain_runner import list_checkpoints
from reconqa.modules.hub.postman_exporter import export_postman_collection
from reconqa.modules.hub.quality_gate import (
    GateLookupError,
    dispatch_webhooks,
    evaluate_project_quality_gate_db,
)
from reconqa.server.routers.projects import _compute_project_inventory
from reconqa.server.routers.scenarios import (
    _build_scenario_list_response,
    _enrich_scenario,
    _load_project_inventory_context,
    collect_project_scenario_assertions,
    sync_project_scenarios,
)
from reconqa.utils.io import read_json, read_jsonl, slugify
from reconqa.utils.proc import run_cmd_async

router = APIRouter(tags=["reports"])

_PD_AUTO_TOOLS = frozenset({"subfinder", "urlfinder", "tlsx", "katana", "httpx", "nuclei"})
_PY_AUTO_TOOLS: dict[str, str] = {
    "mitmdump": "mitmproxy",
    "schemathesis": "schemathesis",
}

_TOOL_METADATA: dict[str, dict[str, Any]] = {
    "mvn": {
        "purpose_zh": "编译并执行生成的 Java (TestNG + RestAssured + Allure) 接口自动化工程。",
        "purpose_en": "Compile and run generated Java (TestNG + RestAssured + Allure) API test projects.",
        "impact_zh": "不影响 HAR/cURL 导入、OpenAPI/关联分析、Java 代码生成及静态安全/性能报告；仅无法在本机直接执行 `mvn test`，可改用 Docker java-runner 容器执行。",
        "impact_en": "HAR/cURL import, OpenAPI/correlation, Java code generation, and static security/perf reports still work with zero external binaries; only local `mvn test` execution requires Maven or Docker.",
        "install_cmd": "sudo apt-get update && sudo apt-get install -y maven  # or: brew install maven",
        "docker_alt": "docker compose --profile runner run --rm java-runner",
        "auto_installable": False,
    },
    "allure": {
        "purpose_zh": "将 Java 测试产出的 `target/allure-results` 渲染为可交互 Allure HTML 报告。",
        "purpose_en": "Render `target/allure-results` into an interactive Allure HTML test report.",
        "impact_zh": "不影响 HAR/cURL 分析、Java 代码生成及平台内置 Allure 摘要看板；仅无法在本机生成独立静态 HTML 报告，可改用 Docker java-runner 容器。",
        "impact_en": "Does not block HAR/cURL analysis, Java codegen, or ReconQA's built-in Allure summary board; only local static HTML report generation needs Allure CLI or Docker.",
        "install_cmd": "brew install allure  # or download from https://allurereport.org/docs/install/",
        "docker_alt": "docker compose --profile runner run --rm java-runner",
        "auto_installable": False,
    },
    "java": {
        "purpose_zh": "OpenJDK 21+ 运行时，用于本地编译与执行生成的 Java 接口自动化套件。",
        "purpose_en": "OpenJDK 21+ runtime for compiling and running generated Java API test suites locally.",
        "impact_zh": "不影响任何 HAR/cURL 导入、报告分析或 Java 工程代码生成；仅无法在本机直接运行生成的测试，可使用 Docker java-runner 替代。",
        "impact_en": "Does not affect HAR/cURL import, analysis reports, or Java code generation; only local Java test execution needs JDK 21+ or Docker.",
        "install_cmd": "sudo apt-get install -y temurin-21-jdk  # or: brew install --cask temurin@21",
        "docker_alt": "docker compose --profile runner run --rm java-runner",
        "auto_installable": False,
    },
    "k6": {
        "purpose_zh": "本地执行自动生成的阶梯加压 k6 脚本并产出 `p95`/`429`/`5xx` 拐点分析。",
        "purpose_en": "Execute generated ramping k6 load scripts locally and analyze p95/429/5xx inflection points.",
        "impact_zh": "不影响 HAR 静态性能体检与 `scenario_ramp.js` 脚本生成；仅无法在当前主机直接触发 `run_k6` 实压，可下载脚本到压测机或使用 Docker k6 运行。",
        "impact_en": "HAR performance profiling and `scenario_ramp.js` generation still work; only local `run_k6` execution requires k6 CLI or Docker.",
        "install_cmd": "brew install k6  # or: https://grafana.com/docs/k6/latest/set-up/install-k6/",
        "docker_alt": "docker run --rm -i grafana/k6 run - < performance/k6/scenario_ramp.js",
        "auto_installable": False,
    },
    "mitmdump": {
        "purpose_zh": "手工测试代理录包（支持访问 `http://reconqa.local/scenario/<name>` 实时切分场景 HAR）。",
        "purpose_en": "Manual exploratory testing proxy recorder with live scenario switching into HAR 1.2.",
        "impact_zh": "不影响导入 Chrome DevTools / Charles / Fiddler 的 HAR 或粘贴 cURL/fetch；仅无法使用内置 `reconqa record --mitm` 代理录包。",
        "impact_en": "Importing HARs from Chrome DevTools/Charles/Fiddler or pasting cURL/fetch works unaffected; only built-in `reconqa record --mitm` proxy capture needs mitmproxy.",
        "install_cmd": "uv sync --extra recorder  # or: uv pip install mitmproxy",
        "docker_alt": "docker compose --profile recorder up -d mitmproxy",
        "auto_installable": True,
    },
    "nuclei": {
        "purpose_zh": "可选的主动安全配置/暴露面基线扫描引擎（需显式开启 `--nuclei`）。",
        "purpose_en": "Optional active misconfiguration and exposure scanner (enabled only when `--nuclei` is toggled).",
        "impact_zh": "不影响 HAR 静态安全审计、双账号 A/B 越权复测与 Burp 导出；仅在显式勾选 Nuclei 扫描时会跳过该子步骤。",
        "impact_en": "Static security audit, dual-account A/B IDOR matrix, and Burp export work without Nuclei; only optional active Nuclei scans are skipped.",
        "install_cmd": "./scripts/install-tools.sh ./bin --only nuclei",
        "docker_alt": "docker run --rm projectdiscovery/nuclei:latest -u https://api.example.com -jsonl",
        "auto_installable": True,
    },
    "docker": {
        "purpose_zh": "运行容器化 `java-runner`（Maven + JDK 21 + Allure）、`mitmproxy` 或 OWASP ZAP 服务。",
        "purpose_en": "Run containerized `java-runner` (Maven + JDK 21 + Allure), `mitmproxy`, or OWASP ZAP.",
        "impact_zh": "不影响核心 Python 引擎、Web 控制台、HAR/cURL 分析及代码/脚本生成；仅无法通过 Docker 替代本机缺失的 Maven/ZAP 环境。",
        "impact_en": "Core Python engine, Web Console, HAR/cURL analysis, and code/script generation work natively without Docker.",
        "install_cmd": "https://docs.docker.com/engine/install/",
        "docker_alt": None,
        "auto_installable": False,
    },
    "subfinder": {
        "purpose_zh": "可选被动子域名资产发现工具（仅在启用 Discovery 模块时调用）。",
        "purpose_en": "Optional passive subdomain enumeration tool (used only when Discovery module is enabled).",
        "impact_zh": "完全不影响 HAR/cURL 导入及三大核心能力（API 自动化/性能/安全）；仅 Discovery 被动资产收集会跳过子域名步骤。",
        "impact_en": "Zero impact on HAR/cURL import or the 3 core testing pillars; only optional Discovery subdomain enumeration is skipped.",
        "install_cmd": "./scripts/install-tools.sh ./bin --only subfinder",
        "docker_alt": None,
        "auto_installable": True,
    },
    "urlfinder": {
        "purpose_zh": "可选被动历史 URL 发现工具（仅在启用 Discovery 模块时调用）。",
        "purpose_en": "Optional passive historical URL discovery tool (used only when Discovery module is enabled).",
        "impact_zh": "完全不影响 HAR/cURL 导入及三大核心能力；仅 Discovery 被动历史 URL 收集会被跳过。",
        "impact_en": "Zero impact on HAR/cURL import or core testing pillars; only optional Discovery historical URL collection is skipped.",
        "install_cmd": "./scripts/install-tools.sh ./bin --only urlfinder",
        "docker_alt": None,
        "auto_installable": True,
    },
    "tlsx": {
        "purpose_zh": "可选 TLS 证书 SAN/CN 资产发现工具（仅在启用 Discovery 模块时调用）。",
        "purpose_en": "Optional TLS certificate SAN/CN discovery tool (used only when Discovery module is enabled).",
        "impact_zh": "完全不影响 HAR/cURL 导入及三大核心能力；仅 Discovery 证书域名提取步骤会被跳过。",
        "impact_en": "Zero impact on HAR/cURL import or core testing pillars; only optional Discovery TLS SAN extraction is skipped.",
        "install_cmd": "./scripts/install-tools.sh ./bin --only tlsx",
        "docker_alt": None,
        "auto_installable": True,
    },
    "katana": {
        "purpose_zh": "可选主动爬虫与 JS 链接发现工具（仅在启用 Active Discovery 时调用）。",
        "purpose_en": "Optional active web crawler and JS endpoint finder (used only for Active Discovery).",
        "impact_zh": "完全不影响 HAR/cURL 导入及三大核心能力；仅主动爬取发现步骤会被跳过。",
        "impact_en": "Zero impact on HAR/cURL import or core testing pillars; only optional active crawl discovery is skipped.",
        "install_cmd": "./scripts/install-tools.sh ./bin --only katana",
        "docker_alt": None,
        "auto_installable": True,
    },
    "httpx": {
        "purpose_zh": "可选存活探测与技术栈指纹识别工具（仅在启用 Active Discovery 时调用）。",
        "purpose_en": "Optional live host probing and tech-stack fingerprinting tool (Active Discovery only).",
        "impact_zh": "完全不影响 HAR/cURL 导入及三大核心能力；仅主动存活探测步骤会被跳过。",
        "impact_en": "Zero impact on HAR/cURL import or core testing pillars; only optional live host probing is skipped.",
        "install_cmd": "./scripts/install-tools.sh ./bin --only httpx",
        "docker_alt": None,
        "auto_installable": True,
    },
    "playwright": {
        "purpose_zh": "可选本地浏览器流量抓包辅助驱动（仅用于 `reconqa record` 捕获网络包，非 UI 自动化）。",
        "purpose_en": "Optional local browser traffic-capture helper for `reconqa record` (traffic capture only, not UI testing).",
        "impact_zh": "完全不影响 HAR/cURL/fetch/Raw HTTP 导入、mitmproxy 代理录包及所有下游生成与分析能力。",
        "impact_en": "Zero impact on HAR/cURL/fetch/Raw HTTP import, mitmproxy recording, or any downstream analysis/codegen.",
        "install_cmd": "uv sync --extra recorder && uv run playwright install chromium",
        "docker_alt": None,
        "auto_installable": False,
    },
    "schemathesis": {
        "purpose_zh": "可选基于 OpenAPI 3.0.3 的 L3 边界值/错类型接口 Fuzzing 工具。",
        "purpose_en": "Optional OpenAPI 3.0.3 contract fuzzing runner for L3 boundary and schema edge cases.",
        "impact_zh": "不影响 OpenAPI 合并与 Java L1/L2/L3 代码生成；仅在显式开启 `--run-schemathesis` 时需要。",
        "impact_en": "Does not affect OpenAPI merging or Java L1/L2/L3 codegen; only needed when `--run-schemathesis` is explicitly enabled.",
        "install_cmd": "uv sync --extra fuzz  # or: uv pip install schemathesis",
        "docker_alt": None,
        "auto_installable": True,
    },
}


class DoctorInstallRequest(BaseModel):
    tool: str


def _resolve_install_script() -> Path | None:
    candidates = [
        Path(__file__).resolve().parents[4] / "scripts" / "install-tools.sh",
        Path.cwd() / "scripts" / "install-tools.sh",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _enriched_doctor() -> dict[str, dict[str, Any]]:
    raw = doctor_check()
    enriched: dict[str, dict[str, Any]] = {}
    for tool_name, info in raw.items():
        meta = _TOOL_METADATA.get(tool_name, {})
        enriched[tool_name] = {
            **info,
            "purpose_zh": meta.get("purpose_zh", ""),
            "purpose_en": meta.get("purpose_en", ""),
            "impact_zh": meta.get(
                "impact_zh",
                "不影响 HAR/cURL 导入及核心报告与代码生成。",
            ),
            "impact_en": meta.get(
                "impact_en",
                "Does not block HAR/cURL import or core report/code generation.",
            ),
            "install_cmd": meta.get("install_cmd", info.get("install_hint", "")),
            "docker_alt": meta.get("docker_alt"),
            "auto_installable": bool(meta.get("auto_installable", False)),
        }
    return enriched


@router.get("/system/doctor")
async def get_system_doctor() -> dict[str, dict[str, Any]]:
    """Return enriched live external toolchain readiness for Web UI Overview."""
    return _enriched_doctor()


@router.post("/system/doctor/install")
async def install_doctor_tool(body: DoctorInstallRequest) -> dict[str, Any]:
    """Install a safe, allowlisted user-space CLI tool without sudo or shell interpolation."""
    tool = body.tool.strip().lower()
    doctor_before = _enriched_doctor()
    if tool not in doctor_before:
        raise HTTPException(status_code=400, detail=f"Unknown tool '{body.tool}'")

    info = doctor_before[tool]
    if not info.get("auto_installable"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Tool '{tool}' cannot be auto-installed safely from the server. "
                "Please follow the manual install command or Docker alternative."
            ),
        )

    cmd: list[str]
    if tool in _PD_AUTO_TOOLS:
        script = _resolve_install_script()
        if script is None:
            raise HTTPException(
                status_code=500,
                detail="Installer script 'scripts/install-tools.sh' not found on server.",
            )
        cmd = ["bash", str(script), str(settings.bin_dir), "--only", tool]
    elif tool in _PY_AUTO_TOOLS:
        pkg = _PY_AUTO_TOOLS[tool]
        uv_bin = shutil.which("uv")
        if uv_bin:
            cmd = [uv_bin, "pip", "install", "--python", sys.executable, pkg]
        else:
            cmd = [sys.executable, "-m", "pip", "install", pkg]
    else:
        raise HTTPException(status_code=400, detail=f"Tool '{tool}' is not auto-installable")

    res = await run_cmd_async(cmd, timeout=120)
    combined_output = "\n".join(part for part in (res.stdout.strip(), res.stderr.strip()) if part)
    refreshed = _enriched_doctor()
    is_ok = res.returncode == 0 and not res.timed_out and bool(refreshed[tool]["available"])

    return {
        "ok": is_ok,
        "tool": tool,
        "returncode": res.returncode,
        "output": combined_output[-8000:],
        "doctor": refreshed,
    }


async def _get_run_or_404(db: AsyncSession, run_id: str) -> Run:
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


@router.get("/runs/{run_id}/findings", response_model=list[FindingOut])
async def list_run_findings(
    run_id: str,
    category: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[Finding]:
    await _get_run_or_404(db, run_id)
    stmt = select(Finding).where(Finding.run_id == run_id)
    if category:
        stmt = stmt.where(Finding.category == category.lower())
    if severity:
        stmt = stmt.where(Finding.severity == severity.lower())
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.get("/runs/{run_id}/performance")
async def get_run_performance(
    run_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    perf_dir = Path(run.workspace_dir) / "performance"
    har_report_path = perf_dir / "har_perf_report.json"
    k6_analysis_path = perf_dir / "k6" / "k6_analysis.json"
    k6_script_path = perf_dir / "k6" / "scenario_ramp.js"

    return {
        "run_id": run_id,
        "har_profiler": read_json(har_report_path) if har_report_path.is_file() else None,
        "k6_analysis": read_json(k6_analysis_path) if k6_analysis_path.is_file() else None,
        "k6_script_available": k6_script_path.is_file(),
    }


@router.get("/runs/{run_id}/security")
async def get_run_security(
    run_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    sec_dir = Path(run.workspace_dir) / "security"
    static_path = sec_dir / "static_audit.json"
    auth_path = sec_dir / "auth_matrix.json"
    nuclei_path = sec_dir / "nuclei.jsonl"
    burp_dir = sec_dir / "burp_exports"

    return {
        "run_id": run_id,
        "static_audit": read_json(static_path) if static_path.is_file() else None,
        "auth_matrix": read_json(auth_path) if auth_path.is_file() else None,
        "nuclei": read_jsonl(nuclei_path) if nuclei_path.is_file() else [],
        "burp_exports_available": burp_dir.is_dir() and any(burp_dir.iterdir()),
    }


def _inspect_java_project_meta(java_dir: Path) -> dict[str, Any]:
    l1_cases = 0
    l2_chains = 0
    l3_checks = 0
    package_name = "com.reconqa.generated"
    java_version = "21"

    csv_path = java_dir / "src" / "test" / "resources" / "data" / "l1_smoke_endpoints.csv"
    if csv_path.is_file():
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as f:
                l1_cases = sum(1 for _ in csv.DictReader(f))
        except Exception:
            l1_cases = 0

    java_src = java_dir / "src" / "test" / "java"
    if java_src.is_dir():
        l2_files = list(java_src.rglob("chains/L2*.java"))
        l2_chains = len(l2_files)

        for l3_file in java_src.rglob("security/L3*.java"):
            try:
                content = l3_file.read_text(encoding="utf-8")
                l3_checks += len(re.findall(r"@Test\b", content))
            except Exception:
                pass

        for base_file in java_src.rglob("base/ApiBaseTest.java"):
            try:
                match = re.search(r"^\s*package\s+([a-zA-Z0-9_.]+)\.base\s*;", base_file.read_text(encoding="utf-8"), re.MULTILINE)
                if match:
                    package_name = match.group(1)
                    break
            except Exception:
                pass

    pom_path = java_dir / "pom.xml"
    if pom_path.is_file():
        try:
            pom_text = pom_path.read_text(encoding="utf-8")
            ver_match = re.search(
                r"<(?:maven\.compiler\.release|java\.version)>\s*([^<\s]+)\s*</",
                pom_text,
            )
            if ver_match:
                java_version = ver_match.group(1)
        except Exception:
            pass

    return {
        "l1_cases": l1_cases,
        "l2_chains": l2_chains,
        "l3_checks": l3_checks,
        "package_name": package_name,
        "java_version": java_version,
    }


def _parse_allure_results(results_dir: Path) -> list[dict[str, Any]]:
    if not results_dir.is_dir():
        return []
    parsed: list[dict[str, Any]] = []
    for result_file in sorted(results_dir.glob("*-result.json")):
        try:
            rec = read_json(result_file)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        labels: dict[str, str] = {}
        for lbl in rec.get("labels", []):
            if isinstance(lbl, dict) and lbl.get("name"):
                labels[str(lbl["name"]).lower()] = str(lbl.get("value", ""))
        start = rec.get("start")
        stop = rec.get("stop")
        if isinstance(start, (int, float)) and isinstance(stop, (int, float)):
            duration_ms = max(0, int(stop - start))
        else:
            duration_ms = int(rec.get("duration_ms", 0) or 0)
        status_details = rec.get("statusDetails")
        message = ""
        if isinstance(status_details, dict):
            message = str(status_details.get("message") or "")
        parsed.append(
            {
                "name": str(rec.get("name", result_file.stem)),
                "status": str(rec.get("status", "unknown")),
                "duration_ms": duration_ms,
                "epic": labels.get("epic", ""),
                "feature": labels.get("feature", ""),
                "story": labels.get("story", ""),
                "message": message,
            }
        )
    return parsed


@router.get("/runs/{run_id}/java-report")
async def get_run_java_report(
    run_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    run = await _get_run_or_404(db, run_id)
    java_dir = Path(run.workspace_dir) / "java-tests"
    generated = java_dir.is_dir() and (java_dir / "pom.xml").is_file()

    project_meta = _inspect_java_project_meta(java_dir)
    exec_summary_path = java_dir / "target" / "reconqa-execution-summary.json"
    execution_summary: dict[str, Any] | None = None
    if exec_summary_path.is_file():
        try:
            execution_summary = read_json(exec_summary_path)
        except Exception:
            execution_summary = None

    allure_results = _parse_allure_results(java_dir / "target" / "allure-results")

    allure_index = java_dir / "target" / "allure-report" / "index.html"
    allure_report_url = (
        f"/api/v1/runs/{run_id}/allure-report/index.html" if allure_index.is_file() else None
    )

    schemathesis_path = java_dir / "schemathesis_report.json"
    schemathesis: dict[str, Any] | None = None
    if schemathesis_path.is_file():
        try:
            schemathesis = read_json(schemathesis_path)
        except Exception:
            schemathesis = None

    return {
        "run_id": run_id,
        "generated": generated,
        "project_meta": project_meta,
        "execution_summary": execution_summary,
        "allure_results": allure_results,
        "allure_report_url": allure_report_url,
        "schemathesis": schemathesis,
    }


@router.get("/runs/{run_id}/allure-report/{file_path:path}")
async def serve_allure_report_asset(
    run_id: str,
    file_path: str,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    run = await _get_run_or_404(db, run_id)
    report_root = (Path(run.workspace_dir) / "java-tests" / "target" / "allure-report").resolve()
    target = (report_root / (file_path or "index.html")).resolve()
    if target != report_root and report_root not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid Allure report path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Allure report asset not found")
    return FileResponse(path=target)


@router.get("/projects/{project_id}/trends")
async def get_project_trends(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    res = await db.execute(
        select(Run).where(Run.project_id == project_id).order_by(Run.started_at.asc())
    )
    runs = list(res.scalars().all())
    points: list[dict[str, Any]] = []
    for r in runs:
        m = r.summary_metrics or {}
        points.append(
            {
                "run_id": r.id,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "total_endpoints": m.get("total_endpoints", 0),
                "har_live_endpoints": m.get("har_live_endpoints", 0),
                "coverage_pct": m.get("coverage_pct", 0.0),
                "correlation_chains": m.get("correlation_chains", 0),
                "findings_total": m.get("findings_total", 0),
                "findings_by_severity": m.get("findings_by_severity", {}),
                "slow_endpoints_count": m.get("slow_endpoints_count", 0),
            }
        )

    return {
        "project_id": project_id,
        "project_name": project.name,
        "total_runs": len(points),
        "trends": points,
    }


def _safe_read_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = read_json(path)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def _resolve_project_run(
    db: AsyncSession,
    project_id: str,
    run_id: str | None,
) -> Run | None:
    if run_id is not None:
        run_obj = await db.get(Run, run_id)
        if run_obj is None or run_obj.project_id != project_id:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found in project '{project_id}'",
            )
        return run_obj

    res = await db.execute(
        select(Run)
        .where(Run.project_id == project_id)
        .order_by(Run.finished_at.desc(), Run.started_at.desc())
    )
    all_runs = list(res.scalars().all())
    for r in all_runs:
        if r.status == "completed":
            return r
    return all_runs[0] if all_runs else None


@router.get("/projects/{project_id}/acceptance-report")
async def export_project_acceptance_report(
    project_id: str,
    format: str = Query(default="html"),
    run_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Export a standalone Acceptance Report in HTML, Markdown, or JSON format."""
    norm_fmt = format.strip().lower()
    if norm_fmt == "md":
        norm_fmt = "markdown"
    if norm_fmt not in {"html", "markdown", "json"}:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported report format '{format}'. Expected one of: html, markdown, json.",
        )

    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    selected_run = await _resolve_project_run(db, project_id, run_id)

    completed_runs, inventory = await _load_project_inventory_context(db, project)
    await sync_project_scenarios(db, project_id, inventory=inventory)

    sc_rows = list(
        (await db.execute(select(ScenarioModel).where(ScenarioModel.project_id == project_id)))
        .scalars()
        .all()
    )
    runs_by_id = {r.id: r for r in completed_runs}
    enriched_scenarios = [
        _enrich_scenario(row, inventory=inventory, runs_by_id=runs_by_id)[0] for row in sc_rows
    ]
    scenario_list = _build_scenario_list_response(project_id, enriched_scenarios)
    cap_map = build_capability_map(
        inventory=inventory,
        scenarios=enriched_scenarios,
        project_id=project.id,
        project_name=project.name,
    )

    chain_runs = list_checkpoints(project_id, run_id=run_id, limit=50)

    findings: list[Finding] = []
    har_perf_report: dict[str, Any] | None = None
    k6_analysis: dict[str, Any] | None = None
    if selected_run is not None:
        f_res = await db.execute(select(Finding).where(Finding.run_id == selected_run.id))
        findings = list(f_res.scalars().all())
        if selected_run.workspace_dir:
            ws = Path(selected_run.workspace_dir)
            har_perf_report = _safe_read_json_dict(ws / "performance" / "har_perf_report.json")
            k6_analysis = _safe_read_json_dict(ws / "performance" / "k6" / "k6_analysis.json")

    report_data = build_acceptance_report_data(
        project_id=project.id,
        project_name=project.name,
        project_slug=project.slug,
        run_id=selected_run.id if selected_run is not None else None,
        inventory=inventory,
        capability_map=cap_map,
        scenario_list=scenario_list,
        chain_runs=chain_runs,
        findings=findings,
        har_perf_report=har_perf_report,
        k6_analysis=k6_analysis,
    )

    fname_base = f"{slugify(project.slug or project.name)}-acceptance-report"
    if norm_fmt == "html":
        html_body = render_acceptance_report_html(report_data)
        return Response(
            content=html_body.encode("utf-8"),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname_base}.html"'},
        )
    if norm_fmt == "markdown":
        md_body = render_acceptance_report_markdown(report_data)
        return Response(
            content=md_body.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname_base}.md"'},
        )
    return report_data.model_dump(mode="json")


@router.get("/projects/{project_id}/export/postman")
async def export_project_postman_collection(
    project_id: str,
    run_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Export project asset library endpoints & correlation chains as a Postman Collection v2.1 JSON."""
    project, inventory, openapi_doc = await _compute_project_inventory(db, project_id)

    chains: list[dict[str, Any]] = []
    base_url = ""
    if run_id is not None:
        run_obj = await db.get(Run, run_id)
        if run_obj is None or run_obj.project_id != project_id:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found in project '{project_id}'",
            )
        if isinstance(run_obj.config_snapshot, dict):
            base_url = str(run_obj.config_snapshot.get("target_url") or "").strip()
        if run_obj.workspace_dir:
            corr_data = _safe_read_json_dict(Path(run_obj.workspace_dir) / "hub" / "correlation.json")
            if corr_data:
                chains = [ch for ch in (corr_data.get("chains") or []) if isinstance(ch, dict)]
    else:
        runs_res = await db.execute(
            select(Run)
            .where(Run.project_id == project_id)
            .order_by(Run.finished_at.desc(), Run.started_at.desc())
        )
        all_runs = list(runs_res.scalars().all())
        ordered_runs = [r for r in all_runs if r.status == "completed"] + [
            r for r in all_runs if r.status != "completed"
        ]
        seen_sigs: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for r in ordered_runs:
            if not base_url and isinstance(r.config_snapshot, dict):
                base_url = str(r.config_snapshot.get("target_url") or "").strip()
            if not r.workspace_dir:
                continue
            corr_data = _safe_read_json_dict(Path(r.workspace_dir) / "hub" / "correlation.json")
            if not corr_data:
                continue
            for ch in corr_data.get("chains") or []:
                if not isinstance(ch, dict):
                    continue
                sig = (
                    str(ch.get("name") or ""),
                    tuple(
                        (
                            str(st.get("method") or "").upper(),
                            str(st.get("path") or ""),
                        )
                        for st in (ch.get("steps") or [])
                        if isinstance(st, dict)
                    ),
                )
                if sig not in seen_sigs:
                    seen_sigs.add(sig)
                    chains.append(ch)

    if not base_url:
        t_res = await db.execute(select(Target).where(Target.project_id == project_id).limit(1))
        first_target = t_res.scalar_one_or_none()
        if first_target and first_target.base_url:
            base_url = first_target.base_url.strip()

    scenario_assertions = await collect_project_scenario_assertions(db, project_id)
    collection = export_postman_collection(
        project_id=project.id,
        project_name=project.name,
        inventory=inventory,
        openapi_doc=openapi_doc,
        chains=chains,
        scenario_assertions=scenario_assertions,
        base_url=base_url,
        run_id=run_id,
    )

    fname = f"{slugify(project.slug or project.name)}-postman-collection.json"
    return Response(
        content=json.dumps(collection, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/projects/{project_id}/gate", response_model=QualityGateResult)
async def evaluate_project_quality_gate(
    project_id: str,
    body: QualityGateRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> QualityGateResult:
    """Evaluate quality gate rules for a project run and optionally dispatch webhooks."""
    req = body or QualityGateRequest()
    proj_cfg = load_project_config()
    try:
        result, effective_cfg = await evaluate_project_quality_gate_db(
            db,
            project_id,
            req,
            project_config=proj_cfg,
        )
    except GateLookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    webhooks: dict[str, str | None] = {
        "feishu": effective_cfg.feishu_webhook,
        "dingtalk": effective_cfg.dingtalk_webhook,
        "slack": effective_cfg.slack_webhook,
    }
    if any(v for v in webhooks.values()) and (not result.passed or req.notify_on_pass):
        result.webhook_results = await dispatch_webhooks(
            webhooks,
            result,
            timeout_sec=effective_cfg.webhook_timeout_sec,
        )
    return result
